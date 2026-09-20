#!/bin/bash
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Julien Gainza
#
# Removes the moOde MQTT bridge.
#
# By default it first clears the retained topics this box published. That step
# matters: the bridge publishes retained, so without it Home Assistant keeps the
# device forever, greyed out as unavailable, long after the daemon is gone.
#
# Usage: sudo ./uninstall.sh [--keep-retained] [--purge-deps]
#
#   --keep-retained  leave the broker alone (use when reinstalling shortly)
#   --purge-deps     also apt purge python3-paho-mqtt (installed by install.sh;
#                    python3-musicpd is left alone, moOde ships it)

set -u

CONF=/etc/moode-mqtt.conf
LIBDIR=/usr/local/lib/moode-mqtt
# Where the daemon lived up to 1.2.0, removed too so nothing is left behind.
LEGACY_BIN=/usr/local/bin/moode-mqtt.py
UNIT=/etc/systemd/system/moode-mqtt.service

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ok] %s\n' "$*"; }
warn() { printf '[!]  %s\n' "$*"; }

KEEP_RETAINED=0
PURGE_DEPS=0
for arg in "$@"; do
	case $arg in
		--keep-retained) KEEP_RETAINED=1 ;;
		--purge-deps)    PURGE_DEPS=1 ;;
		*) warn "unknown option: $arg"; exit 1 ;;
	esac
done

if [ "$(id -u)" != 0 ]; then
	warn "run me as root: sudo $0"
	exit 1
fi

# Stop first, so nothing republishes what we are about to clear.
say "-- Service"
if systemctl list-unit-files moode-mqtt.service >/dev/null 2>&1 && [ -f "$UNIT" ]; then
	systemctl stop moode-mqtt 2>/dev/null
	systemctl disable --quiet moode-mqtt 2>/dev/null
	ok "stopped and disabled"
else
	say "     no unit installed"
fi

say
say "-- Broker"
if [ "$KEEP_RETAINED" = 1 ]; then
	say "     --keep-retained: leaving retained topics in place"
	say "     (Home Assistant will keep showing this device as unavailable)"
elif [ ! -f "$CONF" ]; then
	warn "$CONF is gone, cannot reach the broker to clear retained topics"
	warn "  Home Assistant will keep this device until you clear them by hand"
else
	# The config is still here, so we know where to connect and as whom. Clear
	# every retained topic by publishing an empty retained payload over it.
	python3 - "$CONF" <<'PY'
import configparser, sys, time
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("     python3-paho-mqtt is gone, cannot clear retained topics")
    sys.exit(0)

cp = configparser.ConfigParser(); cp.read(sys.argv[1])
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
fi

say
say "-- Files"
for f in "$UNIT" "$LEGACY_BIN" "$CONF"; do
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

if [ "$PURGE_DEPS" = 1 ]; then
	say
	say "-- Dependencies"
	# Only what install.sh added and nothing else uses; musicpd belongs to moOde.
	if dpkg -s python3-paho-mqtt >/dev/null 2>&1; then
		apt-get purge -y python3-paho-mqtt && ok "purged python3-paho-mqtt"
	else
		say "     python3-paho-mqtt not installed"
	fi
fi

say
say "Done. The config carried the broker password and has been removed;"
say "the copy in this directory (moode-mqtt.conf) is still here if you kept one."
