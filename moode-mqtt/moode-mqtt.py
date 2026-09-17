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
# - Commands ALWAYS go through moOde's REST API (www/command/index.php), never
#   straight to MPD or vol.sh. That endpoint carries moOde's internal mechanisms:
#   set_volume propagates to multiroom receivers and refuses while a renderer is
#   active, toggle_play_pause knows the radio rule. Bypassing it drops all of
#   that silently.
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
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

import musicpd
import paho.mqtt.client as mqtt

CONF_PATH = os.environ.get('MOODE_MQTT_CONF', '/etc/moode-mqtt.conf')
SQLDB = '/var/local/www/db/moode-sqlite3.db'
MOODE_API = 'http://localhost/command/index.php'

# How moOde decides something is a radio stream (inc/mpd.php): an http file with
# no duration. The WebUI then shows "Radio station" as the artist and uses that
# label to pick stop-over-pause, but the label only exists in moOde's own PHP
# rendering - MPD itself reports whatever tags the stream carries, often none.
# So test the cause, not the label.
def is_radio_stream(song, status):
    return song.get('file', '').startswith('http') and not status.get('duration')


# Text fields are never published empty. An ICY radio stream carries only
# StreamTitle - MPD reports no artist and no album at all for one - and a local
# file can simply be untagged (measured: a library where two albums carry a
# genre and the third does not). An empty string is a valid HA state, distinct
# from unknown, so it makes automations fire on the blanks between tracks.

def display_source(cfg_rows, is_radio):
    """What is actually feeding the output, using moOde's own labels
    (playerlib.js). This is derived from system state, not guessed from tags.

    A renderer wins over MPD: while AirPlay or Spotify plays, MPD is stopped and
    its currentsong still describes the track before that.
    """
    for flag, label in RENDERER_LABELS:
        if cfg_rows.get(flag) == '1':
            if flag == 'inpactive':
                name = cfg_rows.get('audioin', '').strip()
                return ('%s Input' % name) if name else 'Input'
            return label
    if cfg_rows.get('rxactive') == '1':
        return 'Multiroom Receiver'
    return 'Radio' if is_radio else 'Library'


def volume_scope(cfg_rows):
    """What moOde's volume knob actually attenuates.

    'hardware' - the card's ALSA mixer, downstream of everything, so it applies
                 to renderers too.
    'mpd'      - `mpc volume`, MPD only. A renderer playing is then untouched by
                 the knob, and by our volume commands.
    'none'     - Fixed 0dB: vol.sh exits without changing anything.

    moOde denies renderers the hardware mixer and makes them attenuate in
    software (inc/renderer.php), so with a hardware mixer there are two stages:
    the renderer's own level, then this one. The knob only describes this one.
    """
    mixer = cfg_rows.get('mpdmixer', '')
    if mixer == 'none':
        return 'none'
    return 'hardware' if mixer == 'hardware' else 'mpd'


def format_quality(params, status=None):
    """Readable output format from ALSA hw_params.

    Preferred over MPD's status 'audio' because MPD reports nothing at all while
    a renderer holds the device, and this is the real thing anyway: resampling
    and CamillaDSP included.

    The format designators handled here mirror moOde's own parser in
    inc/alsa.php (getAlsaHwParams) - keep the two in step.
    """
    if not params:
        return ''
    fmt = params.get('format', '')
    rate = (params.get('rate', '') or '').split(' ')[0]
    if not fmt or not rate.isdigit():
        return ''

    khz_only = '%g kHz' % (int(rate) / 1000)

    # S/PDIF carries its samples in a subframe whose name says nothing about the
    # depth, so the digits in it are not a bit count. MPD knows the real depth
    # when it is the one playing; nothing does otherwise, so report the rate
    # alone rather than inventing a number.
    if fmt == 'IEC958_SUBFRAME_LE':
        mpd_bits = ((status or {}).get('audio') or '').split(':')
        if len(mpd_bits) > 1 and mpd_bits[1].isdigit():
            return '%s bit / %s' % (mpd_bits[1], khz_only)
        return khz_only

    head = fmt.split('_')[0]                       # S32, S24, FLOAT, DSD
    if fmt.startswith('DSD'):
        # DSD_U32_BE at 88200 carries 32 bits per frame: 88200 x 32 = DSD64.
        carrier = ''.join(c for c in fmt.split('_')[1] if c.isdigit()) or '8'
        multiple = int(rate) * int(carrier) / 44100
        if multiple == int(multiple):
            return 'DSD%d' % multiple
        return 'DSD %g kHz' % (int(rate) / 1000)

    khz = '%g' % (int(rate) / 1000)
    bits = ''.join(c for c in head if c.isdigit())
    if bits:
        return '%s bit / %s kHz' % (bits, khz)
    return '%s / %s kHz' % (head.lower(), khz)


def local_address(host, port):
    """This box's address as seen from the broker.

    Asking the routing table which source address reaches the broker beats
    listing interfaces: a player can have both Ethernet and Wi-Fi up with
    different addresses, and the one that reaches the broker is the one Home
    Assistant will reach too. No packet is sent - connect() on a UDP socket only
    fixes the route.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((host, port))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return ''


def installed_version():
    """What install.sh deployed. Empty for an install predating the VERSION file."""
    try:
        with open(VERSION_PATH) as fh:
            return fh.read().strip()
    except OSError:
        return ''


def published_version():
    """The published VERSION of this sub-project, or None when unreachable.

    None is not "up to date": a missing answer must leave the sensor as it was
    rather than claim anything.
    """
    try:
        with urllib.request.urlopen(VERSION_URL, timeout=15) as resp:
            return resp.read().decode('utf-8', 'replace').strip()
    except Exception as err:
        log('update check failed: %s' % err)
        return None


def read_renderer_meta(flag):
    """moOde caches each renderer's metadata as JSON with plain keys, written by
    the renderer itself: /var/local/www/{apl,spot,qbz}meta.json. Empty file means
    the renderer is not reporting anything."""
    path, divisor = RENDERER_META_FILES.get(flag, (None, 1))
    if not path:
        return {}
    try:
        with open(path) as fh:
            text = fh.read().strip()
        meta = json.loads(text) if text else {}
    except (OSError, ValueError):
        return {}
    if meta.get('duration'):
        try:
            meta['duration'] = float(meta['duration']) / divisor
        except (TypeError, ValueError):
            meta['duration'] = 0
    return meta


# Republish the player topic at least this often even when nothing changed, so a
# subscriber that joined late gets a fresh elapsed without us flooding the broker
# once a second.
PLAYER_REFRESH = 30.0

# Update check: the VERSION file of this sub-project, not the repo's HEAD - the
# repo holds other add-ons, and their commits are none of this bridge's business.
VERSION_PATH = '/etc/moode-mqtt.version'
VERSION_URL = ('https://raw.githubusercontent.com/Gjuju/moode-addons'
               '/main/moode-mqtt/VERSION')
UPDATE_CHECK_INTERVAL = 12 * 3600

# Reading the screen state costs a sudo fork. On a headless box (no X at all,
# the common Pi case) it will never answer, so stop asking every second.
DISPLAY_RECHECK_HEADLESS = 60.0

# cfg_system flags moOde sets while a non-MPD source is playing (common.php,
# chkRendererActive()), each with the label its own WebUI shows (playerlib.js).
RENDERER_LABELS = (
    ('inpactive', 'Input'),
    ('btactive', 'Bluetooth'),
    ('aplactive', 'AirPlay'),
    ('spotactive', 'Spotify'),
    ('qbzactive', 'Qobuz'),
    ('slactive', 'Squeezelite'),
    ('paactive', 'Plexamp'),
    ('rbactive', 'RoonBridge'),
)
RENDERER_FLAGS = tuple(flag for flag, _ in RENDERER_LABELS)

# Each renderer's metadata cache, and the divisor its "duration" needs. They do
# not agree on the unit: AirPlay and Spotify report milliseconds, Qobuz seconds.
# moOde carries the same split in playerlib.js (timeDivisor).
RENDERER_META_FILES = {
    'aplactive': ('/var/local/www/aplmeta.json', 1000),
    'spotactive': ('/var/local/www/spotmeta.json', 1000),
    'qbzactive': ('/var/local/www/qbzmeta.json', 1),
}

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
        # AirPlay artwork is cached as a path relative to moOde's web root, unlike
        # the absolute URLs Spotify and Qobuz hand over, so it needs a base to be
        # reachable from Home Assistant. Left empty it is detected; set it for a
        # reverse proxy or any other special case.
        'web_base_url': (moode.get('web_base_url', '') or '').rstrip('/'),
        'update_check': (moode.get('update_check', 'yes') or 'yes').lower()
                        not in ('no', 'false', '0', 'off'),
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


def read_hw_params(cfg_rows):
    """Parsed ALSA hw_params of the output substream, or None when it is closed.

    This is the one place that knows what the DAC is actually being fed, whoever
    opened it - MPD, AirPlay, Qobuz, Bluetooth. Mirrors moodeutl --hwparams: in
    multiroom transmitter mode the real output is the ALSA Loopback.
    """
    if cfg_rows.get('multiroom_tx') == 'On':
        try:
            with open('/proc/asound/Loopback/pcm0p/info') as fh:
                card = next(line.split(': ')[1].strip()
                            for line in fh if line.startswith('card'))
        except (OSError, StopIteration):
            return None
    else:
        card = cfg_rows.get('cardnum', '0')

    try:
        with open('/proc/asound/card%s/pcm0p/sub0/hw_params' % card) as fh:
            text = fh.read().strip()
    except OSError:
        return None
    # moOde's own parser treats both of these as "nothing playing" (inc/alsa.php,
    # getAlsaHwParams).
    if not text or text in ('closed', 'no setup'):
        return None

    params = {}
    for line in text.splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            params[key.strip()] = value.strip()
    return params


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


def moode_api(cmd):
    """Send a command through moOde's own REST API (www/command/index.php).

    Not straight to MPD or to vol.sh: that endpoint carries moOde's internal
    mechanisms, and bypassing it drops them silently. `set_volume` propagates the
    change to multiroom receivers and refuses while a renderer is active;
    `toggle_play_pause` knows moOde's radio rule; anything else is relayed to
    MPD. It is a REST API by design - its own source notes it handles "CLI based
    REST commands sent for example by curl".

    Commands are rare, so an HTTP call costs nothing. The 1 Hz state read stays
    direct, where a PHP fork per second would not be free.
    """
    url = '%s?cmd=%s' % (MOODE_API, urllib.parse.quote(cmd))
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode('utf-8', 'replace').strip()
    except Exception as err:
        log('moOde API %r failed: %s' % (cmd, err))
        return None

    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return body
    # moOde answers a refusal rather than an error status
    if isinstance(data, dict) and data.get('alert'):
        log('moOde refused %r: %s' % (cmd, data['alert']))
    return data


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
        self.version = installed_version()
        self.web_base = cfg['web_base_url']
        self.update_checked_at = 0.0
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

    def resolve_web_base(self):
        """Configured value wins; otherwise detect, falling back to mDNS."""
        if self.cfg['web_base_url']:
            self.web_base = self.cfg['web_base_url']
            return
        ip = local_address(self.cfg['host'], self.cfg['port'])
        self.web_base = ('http://%s' % ip) if ip else \
                        ('http://%s.local' % socket.gethostname())
        log('artwork base URL: %s (detected)' % self.web_base)

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
        self.resolve_web_base()
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
            moode_api('set_volume %s %d' % (direction, amount))
        elif parts[0].isdigit():
            moode_api('set_volume %s' % parts[0])
        else:
            log('unknown volume payload: %r' % payload)

    def cmd_mute(self, payload):
        """set_volume -mute is a TOGGLE, so honour an explicit on/off request."""
        wanted = payload.lower()
        current = moode_api('get_volume') or {}
        muted = current.get('muted') == 'yes'

        if wanted in ('on', 'true', '1', 'mute') and muted:
            return
        if wanted in ('off', 'false', '0', 'unmute') and not muted:
            return
        moode_api('set_volume -mute')

    def cmd_transport(self, payload):
        cmd = payload.lower()
        if cmd not in TRANSPORT_CMDS:
            log('unknown transport payload: %r' % payload)
            return

        if cmd == 'toggle':
            # moOde already knows to stop a radio and pause anything else
            moode_api('toggle_play_pause')
        elif cmd == 'prev':
            moode_api('previous')
        else:
            moode_api(cmd)

    # State collection and publishing

    def mpd_connect(self, client):
        try:
            client.disconnect()
        except Exception:
            pass
        client.connect(self.cfg['mpd_host'], self.cfg['mpd_port'])

    def collect_and_publish(self):
        cfg_rows = db_read(list(RENDERER_FLAGS) +
                           ['volknob', 'volmute', 'cardnum', 'multiroom_tx', 'mpdmixer',
                            'local_display', 'peppy_display', 'rxactive',
                            'audioin'])

        active_flag = next((f for f in RENDERER_FLAGS if cfg_rows.get(f) == '1'), None)
        renderer_active = active_flag is not None or cfg_rows.get('rxactive') == '1'
        hw = read_hw_params(cfg_rows)
        card_open = hw is not None

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

        # Withdraw the controls in Home Assistant rather than letting them move
        # something they do not reach. moOde does the same: its renderer
        # indicator covers the playback screen entirely.
        #
        # Transport: a renderer plays while MPD is stopped, so these reach MPD
        # and not what is heard.
        # Volume: the renderer sets its own level from its app, and on a
        # hardware mixer raising the DAC to compensate would stay raised once
        # MPD takes the output back - loud. Also withdrawn on a fixed 0dB
        # output, where vol.sh changes nothing at all.
        scope_now = volume_scope(cfg_rows)
        self.check_for_update()
        self.publish('controls/available', 'offline' if renderer_active else 'online')
        volume_usable = scope_now != 'none' and not renderer_active
        self.publish('volume/available', 'online' if volume_usable else 'offline')

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
        station = song.get('name', '').strip()

        # A renderer holds the device and MPD is stopped: its currentsong and its
        # state both describe the track from before. Take the metadata from the
        # renderer's own cache, and the play state from the device being open.
        meta = read_renderer_meta(active_flag) if active_flag else {}
        scope = volume_scope(cfg_rows)
        if renderer_active:
            state = 'play' if card_open else 'stop'
        else:
            state = status.get('state', 'unknown')

        player = {
            'state': state,
            # The knob, not MPD's own volume: with a hardware mixer MPD does
            # not carry moOde's level. Always the real value - whether the
            # control should be *used* right now is carried by its own
            # availability topic instead.
            'volume': int(cfg_rows.get('volknob') or 0),
            'volume_scope': scope,
            'mute': cfg_rows.get('volmute') == '1',
            'source': display_source(cfg_rows, is_radio),
            'station': '' if renderer_active else station,
            # Empty rather than invented: one key, one value. No placeholder
            # text and no station name spilling into artist or album. While a
            # renderer plays, these come from its own metadata cache.
            'artist': (meta.get('artist') or '').strip() if renderer_active
                      else song.get('artist', '').strip(),
            'title': (meta.get('title') or '').strip() if renderer_active
                     else song.get('title', '').strip(),
            'album': (meta.get('album') or '').strip() if renderer_active
                     else song.get('album', '').strip(),
            'quality': format_quality(hw, status),
            'audio': '' if renderer_active else status.get('audio', ''),
            # What the renderer says it received, e.g. "FLAC 16/44.1 kHz"
            'source_format': (meta.get('sformat') or '').strip(),
            'file': '' if renderer_active else song.get('file', ''),
            'cover_url': self.absolute_cover(meta.get('cover_url') or ''),
            # Raw extras: useful in templates, deliberately not exposed as
            # entities since they are absent often enough to blink.
            'genre': song.get('genre', ''),
            'date': song.get('date', ''),
            'bitrate': int(status.get('bitrate') or 0),
            'is_radio': False if renderer_active else is_radio,
            'elapsed': 0.0 if renderer_active else float(status.get('elapsed') or 0),
            'duration': float(meta.get('duration') or 0) if renderer_active
                        else float(status.get('duration') or 0),
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

    def absolute_cover(self, url):
        """Spotify and Qobuz give absolute URLs; AirPlay gives a path under
        moOde's web root. Anything not already absolute gets the base prefixed."""
        url = url.strip()
        if not url or url.startswith(('http://', 'https://')):
            return url
        return '%s/%s' % (self.web_base, url.lstrip('/'))

    def check_for_update(self):
        """Compare the installed VERSION with the published one, every 12 h.

        Deliberately the sub-project's VERSION rather than the repository HEAD:
        moode-addons holds other add-ons, and a commit to one of those is not an
        update to this bridge.
        """
        if not self.cfg['update_check'] or not self.version:
            return
        now = time.monotonic()
        if self.update_checked_at and now - self.update_checked_at < UPDATE_CHECK_INTERVAL:
            return
        self.update_checked_at = now

        latest = published_version()
        if latest is None:
            return          # unreachable: leave the sensor as it was
        if latest != self.version:
            log('update available: %s installed, %s published' % (self.version, latest))
        self.publish('update/available', 'ON' if latest != self.version else 'OFF')

    # Home Assistant discovery

    def device_block(self):
        return {
            'identifiers': [self.cfg['instance']],
            'name': self.cfg['friendly_name'],
            'manufacturer': 'moOde audio',
            'model': self.release,
            'sw_version': self.version or 'unknown',
        }

    def publish_discovery(self):
        inst = self.cfg['instance']
        dev = self.device_block()
        avail = self.topic('availability')
        player = self.topic('player')

        def announce(platform, object_id, config, extra_availability=None):
            config.update({
                'device': dev,
                'unique_id': '%s_%s' % (inst, object_id),
                'object_id': '%s_%s' % (inst, object_id),
            })
            if extra_availability:
                # availability_mode 'all': available only while every listed
                # topic says so, so this adds to the bridge's own liveness
                # rather than replacing it.
                config['availability'] = [{'topic': avail},
                                          {'topic': extra_availability}]
                config['availability_mode'] = 'all'
            else:
                config['availability_topic'] = avail
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
        if self.cfg['update_check'] and self.version:
            announce('binary_sensor', 'update', {
                'name': 'Update available',
                'state_topic': self.topic('update/available'),
                'device_class': 'update',
            })
        announce('binary_sensor', 'renderer', {
            'name': 'Renderer',
            'state_topic': player,
            'value_template': "{{ 'ON' if value_json.renderer_active else 'OFF' }}",
            'icon': 'mdi:cast-audio',
        })
        announce('binary_sensor', 'display_power', {
            'name': 'Display',
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
                                   ('album', 'Album', 'mdi:album'),
                                   ('source', 'Source', 'mdi:import'),
                                   ('station', 'Station', 'mdi:radio'),
                                   ('quality', 'Quality', 'mdi:high-definition')):
            announce('sensor', field, {
                'name': label,
                'state_topic': player,
                'value_template': '{{ value_json.%s }}' % field,
                'icon': icon,
            })
        # Declared but off by default: nothing consumes the artwork yet, and an
        # entity the user can switch on in one click beats one that has to be
        # added later. HA fetches the URL itself, so the box must be reachable
        # from it - see web_base_url for the AirPlay case.
        announce('image', 'cover', {
            'name': 'Cover',
            'url_topic': player,
            'url_template': '{{ value_json.cover_url }}',
            'enabled_by_default': False,
        })
        announce('number', 'volume', {
            'name': 'Volume',
            'state_topic': player,
            'value_template': '{{ value_json.volume }}',
            'command_topic': self.topic('cmd/volume'),
            'min': 0, 'max': 100, 'step': 1,
            'icon': 'mdi:volume-high',
        }, extra_availability=self.topic('volume/available'))
        announce('switch', 'mute', {
            'name': 'Mute',
            'state_topic': player,
            'value_template': "{{ 'on' if value_json.mute else 'off' }}",
            'state_on': 'on', 'state_off': 'off',
            'command_topic': self.topic('cmd/mute'),
            'payload_on': 'on', 'payload_off': 'off',
            'icon': 'mdi:volume-off',
        }, extra_availability=self.topic('volume/available'))
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
            }, extra_availability=self.topic('controls/available'))

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
