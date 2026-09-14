#!/bin/bash
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Julien Gainza
#
# moOde -> MQTT bridge - installer.
#
# Nothing here touches the moOde tree, so a moOde update never overwrites it.
# Re-run after editing moode-mqtt.conf or moode-mqtt.py.
#
# Usage: sudo ./install.sh

set -u

SRC_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ok] %s\n' "$*"; }
warn() { printf '[!]  %s\n' "$*"; }

if [ "$(id -u)" != 0 ]; then
	warn "run me as root: sudo $0"
	exit 1
fi

if [ ! -f "$SRC_DIR/moode-mqtt.conf" ]; then
	warn "moode-mqtt.conf is missing"
	say  "     cp moode-mqtt.conf.sample moode-mqtt.conf, fill in the broker"
	say  "     credentials, then re-run. It is gitignored on purpose."
	exit 1
fi

CHANGED_ANY=0
CHANGED_UNIT=0

deploy() { # <src> <dst> <mode> <owner:group>
	local src="$SRC_DIR/$1" dst="$2" mode="$3" owner="$4"
	if [ ! -f "$src" ]; then
		warn "missing source: $src"
		exit 1
	fi
	if cmp -s "$src" "$dst"; then
		say "     unchanged  $dst"
		return 0
	fi
	install -o "${owner%:*}" -g "${owner#*:}" -m "$mode" "$src" "$dst"
	ok "installed  $dst"
	CHANGED_ANY=1
	[ "$dst" = /etc/systemd/system/moode-mqtt.service ] && CHANGED_UNIT=1
	return 0
}

say "-- Dependencies"
MISSING=""
for pkg in python3-paho-mqtt python3-musicpd; do
	dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
	say "     installing:$MISSING"
	# One apt-get call for the whole set: dpkg -i style per-package installs can
	# skip one silently and leave a half-configured system.
	apt-get install -y $MISSING || { warn "apt-get failed"; exit 1; }
	ok "installed:$MISSING"
else
	say "     already present: python3-paho-mqtt python3-musicpd"
fi

say
say "-- Files"
# 0640 root:www-data: the config carries the broker password and the service
# runs as www-data.
deploy moode-mqtt.py      /usr/local/bin/moode-mqtt.py            0755 root:root
deploy moode-mqtt.conf    /etc/moode-mqtt.conf                    0640 root:www-data
deploy moode-mqtt.service /etc/systemd/system/moode-mqtt.service  0644 root:root

say
say "-- Service"
[ "$CHANGED_UNIT" = 1 ] && systemctl daemon-reload
systemctl enable --quiet moode-mqtt 2>/dev/null
systemctl restart moode-mqtt
ok "moode-mqtt enabled and restarted"

say
say "-- Checks"
RC=0

# The broker connection is the one failure that leaves the service "active" while
# doing nothing useful, so read it from the log rather than trusting systemd.
# Wait for the answer instead of sampling once: the bridge forks `moodeutl
# --mooderel` (a PHP call) before connecting, which on a Pi puts the connection
# 4-5 s after the restart - long enough to warn about a perfectly healthy run.
LOG=""
for _ in $(seq 1 20); do
	sleep 1
	LOG=$(journalctl -u moode-mqtt -n 30 --no-pager 2>/dev/null)
	printf '%s' "$LOG" | grep -q "MQTT connected\|MQTT connect failed" && break
done

if systemctl is-active --quiet moode-mqtt; then
	ok "service is running"
else
	warn "service is not running - journalctl -u moode-mqtt -n 30"
	RC=1
fi

if printf '%s' "$LOG" | grep -q "MQTT connected"; then
	ok "connected to the broker"
elif printf '%s' "$LOG" | grep -q "MQTT connect failed"; then
	warn "broker refused the connection - check host/credentials in /etc/moode-mqtt.conf"
	warn "  $(printf '%s' "$LOG" | grep 'MQTT connect failed' | tail -1)"
	RC=1
else
	warn "no broker connection yet - journalctl -u moode-mqtt -f"
	RC=1
fi

# vol.sh is the only correct volume path; without it the bridge would desync the
# WebUI knob on a hardware mixer.
if [ -x /var/www/util/vol.sh ]; then
	ok "vol.sh present"
else
	warn "/var/www/util/vol.sh missing - volume commands will do nothing"
	RC=1
fi

say
if [ "$RC" = 0 ]; then
	[ "$CHANGED_ANY" = 1 ] && say "Done - bridge updated." || say "Done - already up to date."
else
	say "Done with warnings - see above."
fi
exit $RC
