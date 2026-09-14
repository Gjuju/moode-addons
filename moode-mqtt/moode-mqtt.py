#!/usr/bin/env python3
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 Julien Gainza
#
# moOde -> MQTT bridge with Home Assistant discovery.
#
# Publishes what moOde knows (player state, whether audio is actually flowing,
# which app owns the local display) and accepts transport/volume commands back.
#
# Design notes that matter:
#
# - Volume ALWAYS goes through /var/www/util/vol.sh. That script owns volknob,
#   volmute and the choice between amixer and mpc depending on mpdmixer. Calling
#   `mpc volume` directly leaves the WebUI knob stale whenever the mixer is
#   hardware.
# - "Audio active" is read from the ALSA substream (hw_params), not from MPD, so
#   AirPlay / Spotify / Bluetooth / line-in count too. touchmon.php uses the same
#   source. MPD closes the device between tracks, hence AUDIO_OFF_DELAY.
# - The database is opened read-only. Writing cfg_system behind moOde's back
#   desyncs its PHP session cache.
# - A single thread publishes. The MPD idle thread only wakes it, so there is no
#   race between the two producers.

import configparser
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import musicpd
import paho.mqtt.client as mqtt

CONF_PATH = os.environ.get('MOODE_MQTT_CONF', '/etc/moode-mqtt.conf')
SQLDB = '/var/local/www/db/moode-sqlite3.db'
VOL_SH = '/var/www/util/vol.sh'

# How moOde decides something is a radio stream (inc/mpd.php): an http file with
# no duration. The WebUI then shows "Radio station" as the artist and uses that
# label to pick stop-over-pause, but the label only exists in moOde's own PHP
# rendering - MPD itself reports whatever tags the stream carries, often none.
# So test the cause, not the label.
def is_radio_stream(song, status):
    return song.get('file', '').startswith('http') and not status.get('duration')


# Republish the player topic at least this often even when nothing changed, so a
# subscriber that joined late gets a fresh elapsed without us flooding the broker
# once a second.
PLAYER_REFRESH = 30.0

# Reading the screen state costs a sudo fork. On a headless box (no X at all,
# the common Pi case) it will never answer, so stop asking every second.
DISPLAY_RECHECK_HEADLESS = 60.0

# cfg_system flags moOde sets while a non-MPD source is playing (common.php,
# chkRendererActive()).
RENDERER_FLAGS = ('btactive', 'aplactive', 'spotactive', 'qbzactive',
                  'slactive', 'paactive', 'rbactive', 'inpactive')

TRANSPORT_CMDS = ('play', 'pause', 'stop', 'toggle', 'next', 'previous', 'prev')

running = True


def log(msg):
    print(msg, flush=True)


# Configuration


def load_config():
    if not os.path.exists(CONF_PATH):
        log('FATAL: %s not found (copy moode-mqtt.conf.sample)' % CONF_PATH)
        sys.exit(1)

    parser = configparser.ConfigParser()
    parser.read(CONF_PATH)
    broker = parser['broker']
    moode = parser['moode'] if parser.has_section('moode') else {}

    cfg = {
        'host': broker.get('host', 'localhost'),
        'port': broker.getint('port', 1883),
        'username': broker.get('username', '') or None,
        'password': broker.get('password', '') or None,
        'client_id': broker.get('client_id', 'moode-mqtt'),
        'instance': moode.get('instance', 'moode'),
        'friendly_name': moode.get('friendly_name', 'moOde'),
        'prefix': moode.get('topic_prefix', 'moode'),
        'discovery_prefix': moode.get('discovery_prefix', 'homeassistant'),
        'poll_interval': float(moode.get('poll_interval', 1.0)),
        'audio_off_delay': float(moode.get('audio_off_delay', 5.0)),
        'volume_step': int(moode.get('volume_step', 5)),
        'mpd_host': moode.get('mpd_host', 'localhost'),
        'mpd_port': int(moode.get('mpd_port', 6600)),
    }
    return cfg


# Reading moOde's state


def db_read(params):
    """Read cfg_system rows. Read-only: never write the DB behind moOde's back."""
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % SQLDB, uri=True, timeout=2)
        placeholders = ','.join('?' * len(params))
        rows = conn.execute(
            'SELECT param, value FROM cfg_system WHERE param IN (%s)' % placeholders,
            params).fetchall()
        conn.close()
        return dict(rows)
    except sqlite3.Error as err:
        log('db_read: %s' % err)
        return {}


def alsa_card_is_open(cfg_rows):
    """True when the output substream is open, whatever opened it.

    Mirrors moodeutl --hwparams: in multiroom transmitter mode the real output
    is the ALSA Loopback, not the configured card.
    """
    if cfg_rows.get('multiroom_tx') == 'On':
        try:
            with open('/proc/asound/Loopback/pcm0p/info') as fh:
                card = next(line.split(': ')[1].strip()
                            for line in fh if line.startswith('card'))
        except (OSError, StopIteration):
            return False
    else:
        card = cfg_rows.get('cardnum', '0')

    try:
        with open('/proc/asound/card%s/pcm0p/sub0/hw_params' % card) as fh:
            return fh.read().strip() != 'closed'
    except OSError:
        return False


def display_power():
    """on / standby / unknown.

    Two unrelated mechanisms can blank the screen: worker.php's scn_blank (only
    while Peppy is displayed) and the plain X DPMS timeout armed in .xinitrc
    (input inactivity, no relation to audio). This reports the result of either,
    which is why it must not be used to decide whether the amp should be on.
    """
    try:
        env = dict(os.environ, DISPLAY=':0')
        out = subprocess.run(['sudo', '-E', 'xset', 'q'], env=env, timeout=5,
                             capture_output=True, text=True).stdout
    except (subprocess.SubprocessError, OSError):
        return 'unknown'

    for line in out.splitlines():
        if 'Monitor is ' in line:
            # "Monitor is On" / "in Standby" / "in Suspend" / "Off" - the state
            # is not always one word, so keep everything after the marker.
            state = line.split('Monitor is ', 1)[1].strip()
            return 'on' if state == 'On' else 'standby'
    return 'unknown'


def moode_release():
    try:
        return subprocess.run(['moodeutl', '--mooderel'], timeout=10,
                              capture_output=True, text=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return 'unknown'


# Running commands


def run(args):
    try:
        subprocess.run(args, timeout=10, capture_output=True)
    except (subprocess.SubprocessError, OSError) as err:
        log('command failed %s: %s' % (args, err))


def mpc(*args):
    run(['mpc'] + list(args))


def vol(*args):
    run([VOL_SH] + [str(a) for a in args])


# The bridge


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = '%s/%s' % (cfg['prefix'], cfg['instance'])
        self.wake = threading.Event()
        self.published = {}
        self.player_sig = None
        self.player_published_at = 0.0
        self.audio_closed_since = None
        self.audio_state = False
        self.display_power_state = 'unknown'
        self.display_power_checked_at = 0.0
        self.release = moode_release()
        self.status_cli = musicpd.MPDClient()

        client_args = {'client_id': cfg['client_id']}
        try:
            # paho-mqtt 2.x requires an explicit callback API version; trixie
            # ships 2.1. Keep 1.x working so the script is not pinned to it.
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **client_args)
        except AttributeError:
            self.client = mqtt.Client(**client_args)

        if cfg['username']:
            self.client.username_pw_set(cfg['username'], cfg['password'])
        self.client.will_set(self.topic('availability'), 'offline', retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def topic(self, suffix):
        return '%s/%s' % (self.base, suffix)

    def publish(self, suffix, payload, force=False):
        """Publish retained, but only on change: no flood, no stale retained."""
        if not force and self.published.get(suffix) == payload:
            return
        self.published[suffix] = payload
        self.client.publish(self.topic(suffix), payload, retain=True)

    # MQTT callbacks

    def on_connect(self, client, userdata, flags, rc, properties=None):
        # paho 2.x hands over a ReasonCode, 1.x a plain int.
        failure = getattr(rc, 'is_failure', None)
        if failure if failure is not None else rc != 0:
            log('MQTT connect failed rc=%s' % rc)
            return
        log('MQTT connected to %s:%s' % (self.cfg['host'], self.cfg['port']))
        client.publish(self.topic('availability'), 'online', retain=True)
        for sub in ('cmd/volume', 'cmd/mute', 'cmd/transport'):
            client.subscribe(self.topic(sub))
        self.publish_discovery()
        # A reconnect starts from an empty broker state as far as we know, so
        # republish everything rather than trusting our change detection.
        self.published.clear()
        self.player_sig = None
        self.wake.set()

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode('utf-8', 'replace').strip()
        topic = msg.topic

        try:
            if topic.endswith('/cmd/volume'):
                self.cmd_volume(payload)
            elif topic.endswith('/cmd/mute'):
                self.cmd_mute(payload)
            elif topic.endswith('/cmd/transport'):
                self.cmd_transport(payload)
        except Exception as err:                      # never let a bad payload kill the loop
            log('command error on %s (%r): %s' % (topic, payload, err))

        self.wake.set()

    # Commands

    def cmd_volume(self, payload):
        parts = payload.split()
        step = self.cfg['volume_step']

        if not parts:
            return
        if parts[0] in ('up', 'dn', 'down'):
            direction = '-up' if parts[0] == 'up' else '-dn'
            amount = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else step
            vol(direction, amount)
        elif parts[0].isdigit():
            vol(parts[0])
        else:
            log('unknown volume payload: %r' % payload)

    def cmd_mute(self, payload):
        """vol.sh -mute is a TOGGLE, so honour an explicit on/off request."""
        wanted = payload.lower()
        muted = db_read(['volmute']).get('volmute') == '1'

        if wanted in ('on', 'true', '1', 'mute') and muted:
            return
        if wanted in ('off', 'false', '0', 'unmute') and not muted:
            return
        vol('-mute')

    def cmd_transport(self, payload):
        cmd = payload.lower()
        if cmd not in TRANSPORT_CMDS:
            log('unknown transport payload: %r' % payload)
            return

        if cmd == 'toggle':
            state, is_radio = self.player_snapshot()
            if state == 'play':
                mpc('stop' if is_radio else 'pause')
            else:
                mpc('play')
        elif cmd == 'prev':
            mpc('prev')
        elif cmd == 'previous':
            mpc('prev')
        else:
            mpc(cmd)

    def player_snapshot(self):
        try:
            status = self.status_cli.status()
            song = self.status_cli.currentsong()
        except Exception:
            try:
                self.mpd_connect(self.status_cli)
                status = self.status_cli.status()
                song = self.status_cli.currentsong()
            except Exception as err:
                log('MPD unreachable for toggle: %s' % err)
                return 'unknown', False
        return status.get('state', 'unknown'), is_radio_stream(song, status)

    # State collection and publishing

    def mpd_connect(self, client):
        try:
            client.disconnect()
        except Exception:
            pass
        client.connect(self.cfg['mpd_host'], self.cfg['mpd_port'])

    def collect_and_publish(self):
        cfg_rows = db_read(list(RENDERER_FLAGS) +
                           ['volknob', 'volmute', 'cardnum', 'multiroom_tx',
                            'local_display', 'peppy_display'])

        renderer_active = any(cfg_rows.get(flag) == '1' for flag in RENDERER_FLAGS)
        card_open = alsa_card_is_open(cfg_rows)

        # Audio on is immediate; audio off has to survive audio_off_delay, because
        # MPD closes the ALSA device between tracks (touchmon.php does the same
        # with TOUCHMON_CLOSED_COUNT).
        if card_open or renderer_active:
            self.audio_closed_since = None
            self.audio_state = True
        elif self.audio_state:
            now = time.monotonic()
            if self.audio_closed_since is None:
                self.audio_closed_since = now
            elif now - self.audio_closed_since >= self.cfg['audio_off_delay']:
                self.audio_state = False

        self.publish('audio', 'ON' if self.audio_state else 'OFF')

        if cfg_rows.get('peppy_display') == '1':
            app = 'peppy'
        elif cfg_rows.get('local_display') == '1':
            app = 'webui'
        else:
            app = 'none'
        self.publish('display/app', app)

        now = time.monotonic()
        if (self.display_power_state != 'unknown'
                or now - self.display_power_checked_at >= DISPLAY_RECHECK_HEADLESS):
            self.display_power_state = display_power()
            self.display_power_checked_at = now
        # Headless box: publish nothing rather than inventing an OFF. The HA
        # entity then stays "unknown", which is the truth - there is no screen.
        if self.display_power_state != 'unknown':
            self.publish('display/power',
                         'ON' if self.display_power_state == 'on' else 'OFF')

        try:
            status = self.status_cli.status()
            song = self.status_cli.currentsong()
        except Exception:
            try:
                self.mpd_connect(self.status_cli)
                status = self.status_cli.status()
                song = self.status_cli.currentsong()
            except Exception as err:
                log('MPD status unavailable: %s' % err)
                return

        is_radio = is_radio_stream(song, status)
        player = {
            'state': status.get('state', 'unknown'),
            # The knob, not MPD's own volume: with a hardware mixer MPD does not
            # carry moOde's level.
            'volume': int(cfg_rows.get('volknob') or 0),
            'mute': cfg_rows.get('volmute') == '1',
            'artist': song.get('artist', ''),
            'title': song.get('title', ''),
            'album': song.get('album', ''),
            'file': song.get('file', ''),
            'is_radio': is_radio,
            'elapsed': float(status.get('elapsed') or 0),
            'duration': float(status.get('duration') or 0),
            'renderer_active': renderer_active,
        }

        # elapsed moves every single cycle, so comparing the whole payload would
        # republish once a second forever. Compare everything else, and let
        # PLAYER_REFRESH carry the position.
        sig = json.dumps({k: v for k, v in player.items() if k != 'elapsed'},
                         sort_keys=True)
        now = time.monotonic()
        if sig != self.player_sig or now - self.player_published_at >= PLAYER_REFRESH:
            self.player_sig = sig
            self.player_published_at = now
            self.publish('player', json.dumps(player, sort_keys=True), force=True)

    # Home Assistant discovery

    def device_block(self):
        return {
            'identifiers': [self.cfg['instance']],
            'name': self.cfg['friendly_name'],
            'manufacturer': 'moOde audio',
            'model': self.release,
        }

    def publish_discovery(self):
        inst = self.cfg['instance']
        dev = self.device_block()
        avail = self.topic('availability')
        player = self.topic('player')

        def announce(platform, object_id, config):
            config.update({
                'device': dev,
                'availability_topic': avail,
                'unique_id': '%s_%s' % (inst, object_id),
                'object_id': '%s_%s' % (inst, object_id),
            })
            self.client.publish(
                '%s/%s/%s/%s/config' % (self.cfg['discovery_prefix'], platform,
                                        inst, object_id),
                json.dumps(config), retain=True)

        # The amp trigger: on as soon as anything plays, off only after the
        # debounce. Let HA hold the long delay with `for:`.
        announce('binary_sensor', 'audio', {
            'name': 'Audio active',
            'state_topic': self.topic('audio'),
            'device_class': 'running',
            'icon': 'mdi:speaker',
        })
        announce('binary_sensor', 'display_power', {
            'name': 'Display on',
            'state_topic': self.topic('display/power'),
            'icon': 'mdi:monitor',
        })
        announce('sensor', 'display_app', {
            'name': 'Display app',
            'state_topic': self.topic('display/app'),
            'icon': 'mdi:monitor-dashboard',
        })
        announce('sensor', 'state', {
            'name': 'State',
            'state_topic': player,
            'value_template': '{{ value_json.state }}',
            'icon': 'mdi:play-circle',
        })
        for field, label, icon in (('title', 'Title', 'mdi:music-note'),
                                   ('artist', 'Artist', 'mdi:account-music'),
                                   ('album', 'Album', 'mdi:album')):
            announce('sensor', field, {
                'name': label,
                'state_topic': player,
                'value_template': '{{ value_json.%s }}' % field,
                'icon': icon,
            })
        announce('number', 'volume', {
            'name': 'Volume',
            'state_topic': player,
            'value_template': '{{ value_json.volume }}',
            'command_topic': self.topic('cmd/volume'),
            'min': 0, 'max': 100, 'step': 1,
            'icon': 'mdi:volume-high',
        })
        announce('switch', 'mute', {
            'name': 'Mute',
            'state_topic': player,
            'value_template': "{{ 'on' if value_json.mute else 'off' }}",
            'state_on': 'on', 'state_off': 'off',
            'command_topic': self.topic('cmd/mute'),
            'payload_on': 'on', 'payload_off': 'off',
            'icon': 'mdi:volume-off',
        })
        for cmd, label, icon in (('toggle', 'Play/Pause', 'mdi:play-pause'),
                                 ('play', 'Play', 'mdi:play'),
                                 ('pause', 'Pause', 'mdi:pause'),
                                 ('stop', 'Stop', 'mdi:stop'),
                                 ('next', 'Next', 'mdi:skip-next'),
                                 ('previous', 'Previous', 'mdi:skip-previous')):
            announce('button', cmd, {
                'name': label,
                'command_topic': self.topic('cmd/transport'),
                'payload_press': cmd,
                'icon': icon,
            })

    # Threads

    def idle_loop(self):
        """Block on MPD idle and wake the publisher: no polling latency."""
        cli = musicpd.MPDClient()
        while running:
            try:
                self.mpd_connect(cli)
                self.wake.set()
                while running:
                    cli.idle('player', 'mixer', 'options', 'playlist')
                    self.wake.set()
            except Exception as err:
                log('MPD idle lost (%s), retrying' % err)
                time.sleep(5)

    def run(self):
        self.client.connect_async(self.cfg['host'], self.cfg['port'], keepalive=60)
        self.client.loop_start()

        threading.Thread(target=self.idle_loop, daemon=True).start()

        while running:
            self.wake.wait(timeout=self.cfg['poll_interval'])
            self.wake.clear()
            try:
                self.collect_and_publish()
            except Exception as err:
                log('publish cycle failed: %s' % err)

        self.client.publish(self.topic('availability'), 'offline', retain=True)
        self.client.loop_stop()


def main():
    def stop(signum, frame):
        global running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    cfg = load_config()
    log('moode-mqtt starting: instance=%s broker=%s:%s' %
        (cfg['instance'], cfg['host'], cfg['port']))
    Bridge(cfg).run()


if __name__ == '__main__':
    main()
