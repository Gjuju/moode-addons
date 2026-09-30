#!/bin/bash
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Julien Gainza
#
# Removes the moOde MQTT bridge: the systemd unit and the files install.sh left.
# Dependencies are kept. It asks whether to also remove the device from the home
# automation system, by clearing the retained topics this box published.
#
# Usage: sudo ./uninstall.sh

set -u

CONF=/etc/moode-mqtt.conf
LIBDIR=/usr/local/lib/moode-mqtt
# Where the daemon lived up to 1.2.0, removed too so nothing is left behind.
LEGACY_BIN=/usr/local/bin/moode-mqtt.py
UNIT=/etc/systemd/system/moode-mqtt.service
VERSION_FILE=/etc/moode-mqtt.version
SQLDB=/var/local/www/db/moode-sqlite3.db

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ok] %s\n' "$*"; }
warn() { printf '[!]  %s\n' "$*"; }

if [ "$(id -u)" != 0 ]; then
	warn "run me as root: sudo $0"
	exit 1
fi

# Stop first, so nothing republishes what we may clear.
say "-- Service"
if [ -f "$UNIT" ]; then
	systemctl stop moode-mqtt 2>/dev/null
	systemctl disable --quiet moode-mqtt 2>/dev/null
	ok "stopped and disabled"
else
	say "     no unit installed"
fi

say
say "-- Broker"
INST=$(sed -n 's/^instance *= *//p' "$CONF" 2>/dev/null)
INST=${INST:-moode}
BUILTIN_INST=""
if [ "$(sqlite3 "$SQLDB" "SELECT value FROM cfg_system WHERE param='mqttsvc'" 2>/dev/null)" = 1 ]; then
	BUILTIN_INST=$(sqlite3 "$SQLDB" "SELECT value FROM cfg_mqtt WHERE param='instance'" 2>/dev/null)
fi
if [ ! -f "$CONF" ]; then
	say "     $CONF is gone, retained topics left in place"
elif [ "$INST" = "$BUILTIN_INST" ]; then
	say "     moOde's built-in MQTT publishes instance '$INST' now, retained topics kept"
else
	read -r -p "Remove the device from the home automation system? [Y/n] " answer
	case $answer in
		[nN]*) say "     retained topics kept" ;;
		*)
	# Clear every retained topic by publishing an empty retained payload over it.
	python3 - "$CONF" <<'PY'
import configparser, sys, time
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("     python3-paho-mqtt is gone, cannot clear retained topics")
    sys.exit(0)

cp = configparser.ConfigParser(interpolation=None); cp.read(sys.argv[1])
b = cp["broker"]; m = cp["moode"]
inst = m.get("instance", "moode")
prefix = m.get("topic_prefix", "moode")
disc = m.get("discovery_prefix", "homeassistant")
state_sub, disc_sub = "%s/%s/#" % (prefix, inst), "%s/+/%s/+/config" % (disc, inst)
found = set()

def on_connect(c, u, f, rc, p=None):
    c.subscribe(state_sub); c.subscribe(disc_sub)

def on_message(c, u, msg):
    if msg.retain and msg.payload:
        found.add(msg.topic)

cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
if b.get("username"):
    cl.username_pw_set(b["username"], b.get("password", ""))
cl.on_connect = on_connect; cl.on_message = on_message
try:
    cl.connect(b.get("host", "localhost"), int(b.get("port", 1883)), 30)
except Exception as err:
    print("[!]  broker unreachable (%s), retained topics left in place" % err)
    sys.exit(0)
cl.loop_start()
time.sleep(6)

for topic in sorted(found):
    cl.publish(topic, "", retain=True)
time.sleep(3)
print("[ok] cleared %d retained topics for instance '%s'" % (len(found), inst))
cl.loop_stop()
PY
			;;
	esac
fi

say
say "-- Files"
for f in "$UNIT" "$LEGACY_BIN" "$CONF" "$VERSION_FILE"; do
	if [ -f "$f" ]; then
		rm -f "$f"
		ok "removed  $f"
	else
		say "     absent   $f"
	fi
done
if [ -d "$LIBDIR" ]; then
	rm -rf "$LIBDIR"
	ok "removed  $LIBDIR"
else
	say "     absent   $LIBDIR"
fi
systemctl daemon-reload

say
say "Done. The config carried the broker password and has been removed;"
say "the copy in this directory (moode-mqtt.conf) is still here if you kept one."
