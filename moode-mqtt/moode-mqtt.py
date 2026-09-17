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
# - Commands go through moOde's REST API (www/command/index.php) wherever moOde
#   HAS a mechanism, never straight to MPD or vol.sh. That endpoint carries
#   moOde's internal mechanisms: set_volume propagates to multiroom receivers and
#   refuses while a renderer is active, toggle_play_pause knows the radio rule.
#   Bypassing it drops all of that silently.
#   The exception is a renderer, where moOde has no mechanism at all to bypass:
#   command/index.php has no transport command for one, and moOde's own WebUI
#   offers only "disconnect" while one plays (playerlib.js). A renderer backend
#   fills that gap - it does not route around anything.
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
import urllib.error
import urllib.parse
import urllib.request

import dbus
import musicpd
import paho.mqtt.client as mqtt

_SYSTEM_BUS = None

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

# The verbs a backend must implement to be registered at all. Every real
# candidate does (moOde/MPD, pibuz, shairport-sync, Bluetooth AVRCP), so
# advertising them one by one would be machinery for a case that does not exist.
TRANSPORT_VERBS = ('play', 'pause', 'toggle', 'stop', 'next', 'previous')
TRANSPORT_ALIASES = {'prev': 'previous'}

# pibuz (Qobuz Connect) control API. Unauthenticated by its own default, and
# reachable by www-data - both measured, not assumed.
DBUS_PROPS = 'org.freedesktop.DBus.Properties'
DBUS_OBJMGR = 'org.freedesktop.DBus.ObjectManager'

# Bluetooth. The phone is the source and this player the sink, so the phone
# exposes the media player and we are the remote.
BLUEZ = 'org.bluez'
BLUEZ_PLAYER = 'org.bluez.MediaPlayer1'
BLUEALSA = 'org.bluealsa'
BLUEALSA_ROOT = '/org/bluealsa'
BLUEALSA_PCM = 'org.bluealsa.PCM1'
# BlueALSA packs both channels into one uint16 - high byte left, low byte right
# - and in each byte bit 7 is mute with the level in bits 0-6. Measured: level
# 34 on both channels reads 0x2222, and muting it reads 0xa2a2, so a mute keeps
# the level rather than zeroing it.
BT_MUTE_BIT = 0x80
BT_LEVEL_MAX = 0x7F

# AirPlay. shairport-sync publishes MPRIS on the SYSTEM bus, plus its own
# interface carrying what MPRIS has no room for.
MPRIS_NAME = 'org.mpris.MediaPlayer2.ShairportSync'
MPRIS_PATH = '/org/mpris/MediaPlayer2'
MPRIS_PLAYER = 'org.mpris.MediaPlayer2.Player'
SHAIRPORT_NAME = 'org.gnome.ShairportSync'
SHAIRPORT_PATH = '/org/gnome/ShairportSync'
SHAIRPORT_IFACE = 'org.gnome.ShairportSync'

PIBUZ_API = 'http://127.0.0.1:8182'
# One local HTTP read per publish cycle, and only while Qobuz plays. Cheap
# against a Rust daemon on loopback - unlike a PHP fork, which is why moOde's
# own state is still read directly.
PIBUZ_POLL_INTERVAL = 1.0

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


# Renderer control backends
#
# One backend serves one source: the cfg_system flag moOde raises while that
# renderer plays, or None for moOde's own player. The bridge picks the backend
# matching what is playing and routes every command to it; a source with no
# backend gets its controls withdrawn in Home Assistant rather than a button that
# moves something else.
#
# Contract: implement all six transport verbs, or do not register. Volume is
# optional and declared, because it genuinely varies - AVRCP has no volume, and
# even moOde's own has none on a fixed 0dB output.


class Backend:
    """What the bridge needs from any source it can drive."""

    flag = None            # cfg_system flag this backend serves; None = moOde/MPD
    label = 'moOde'

    def reachable(self):
        """Is the mechanism answering right now? Checked every publish cycle."""
        return True

    def volume_usable(self, cfg_rows):
        """Can this backend move the volume, given moOde's current config?

        Each backend answers from the state it actually depends on, rather than
        the bridge guessing on its behalf: moOde's mixer scope says nothing about
        a renderer's own software volume.
        """
        return False

    def mute_usable(self, cfg_rows):
        """Separate from volume, because they genuinely come apart.

        AirPlay has a volume and no readable mute at all: shairport-sync offers
        `mutetoggle` with nothing to read back, and a switch that toggles blind
        would show a state it does not know. Everything else answers the same as
        its volume.
        """
        return self.volume_usable(cfg_rows)

    def volume_state(self):
        """(level 0-100, muted, scope) as this backend sees it, or None.

        None means the bridge keeps reporting moOde's own knob. A backend that
        moves its own volume MUST answer here, or the number in Home Assistant
        would come from one player while the slider moved another.
        """
        return None

    def transport(self, verb):
        raise NotImplementedError

    def set_volume(self, level):
        raise NotImplementedError

    def step_volume(self, direction, amount):
        raise NotImplementedError

    def set_mute(self, wanted):
        """wanted: True to mute, False to unmute, None to toggle."""
        raise NotImplementedError


class MoodeBackend(Backend):
    """moOde's own player, driven through its REST API."""

    flag = None
    label = 'moOde'

    def volume_usable(self, cfg_rows):
        # A fixed 0dB output has no volume to move: moOde's own volume command
        # changes nothing there.
        return volume_scope(cfg_rows) != 'none'

    def transport(self, verb):
        if verb == 'toggle':
            # moOde already knows to stop a radio and pause anything else
            moode_api('toggle_play_pause')
        else:
            moode_api(verb)

    def set_volume(self, level):
        moode_api('set_volume %d' % level)

    def step_volume(self, direction, amount):
        moode_api('set_volume %s %d' % ('-up' if direction == 'up' else '-dn', amount))

    def set_mute(self, wanted):
        """set_volume -mute is a TOGGLE, so honour an explicit on/off request."""
        if wanted is not None:
            current = moode_api('get_volume') or {}
            if (current.get('muted') == 'yes') == wanted:
                return
        moode_api('set_volume -mute')


def dbus_bus():
    """The system bus, opened on first use.

    Lazily, because a player that never sees a renderer never needs it, and
    because a failure here has to withdraw a control rather than kill the daemon.
    """
    global _SYSTEM_BUS
    if _SYSTEM_BUS is None:
        _SYSTEM_BUS = dbus.SystemBus()
    return _SYSTEM_BUS


def dbus_prop(service, path, interface, name):
    """One property, or None if anything at all went wrong.

    A renderer's D-Bus objects come and go with the device, so "not there" is an
    ordinary answer and not worth a log line on every cycle.
    """
    try:
        obj = dbus_bus().get_object(service, path)
        return dbus.Interface(obj, DBUS_PROPS).Get(interface, name)
    except Exception:
        return None


def dbus_set_prop(service, path, interface, name, value):
    try:
        obj = dbus_bus().get_object(service, path)
        dbus.Interface(obj, DBUS_PROPS).Set(interface, name, value)
        return True
    except Exception as err:
        log('D-Bus set %s.%s failed: %s' % (interface, name, err))
        return False


def dbus_call(service, path, interface, method, *args):
    try:
        obj = dbus_bus().get_object(service, path)
        getattr(dbus.Interface(obj, interface), method)(*args)
        return True
    except Exception as err:
        log('D-Bus %s.%s failed: %s' % (interface, method, err))
        return False


def dbus_find(service, interface, root='/'):
    """First object under `service` implementing `interface`, or None.

    Looked up rather than configured: the path carries the device address, so it
    changes with every phone that connects.
    """
    try:
        obj = dbus_bus().get_object(service, root)
        managed = dbus.Interface(obj, DBUS_OBJMGR).GetManagedObjects()
    except Exception:
        return None
    for path, interfaces in managed.items():
        if interface in interfaces:
            return str(path)
    return None


def pibuz_request(path, body=None, method='POST'):
    """One call to pibuz's control API. Parsed JSON, or None if it did not work.

    No Authorization header. pibuz ships with `[server] token` unset, which its
    own source documents as an unauthenticated control plane on loopback and LAN
    alike, and no qbzd.toml exists unless someone writes one. If a token IS set
    the call answers 401, and that is reported as what it is: the bridge keeps no
    copy of anyone's secret, so there is nothing here to leak or to rotate.
    """
    data = json.dumps(body).encode() if body is not None else b''
    req = urllib.request.Request(PIBUZ_API + path, data=data, method=method)
    if body is not None:
        req.add_header('Content-Type', 'application/json')

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode('utf-8', 'replace').strip()
    except urllib.error.HTTPError as err:
        if err.code == 401:
            log('pibuz refused %s: a token is set in its qbzd.toml. The bridge '
                'holds no copy of it, so Qobuz controls stay off.' % path)
        else:
            log('pibuz %s failed: HTTP %s' % (path, err.code))
        return None
    except Exception as err:
        log('pibuz %s failed: %s' % (path, err))
        return None

    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


class PibuzBackend(Backend):
    """Qobuz Connect, driven through pibuz's own HTTP API.

    Nothing is being routed around here: moOde has no command for this renderer,
    and its WebUI offers only "disconnect" while one plays. pibuz is the only
    thing that can move it.

    Its six transport routes carry exactly our verb names, so no translation
    table earns its keep. pibuz is started by moOde on demand and is simply
    absent the rest of the time, so "not answering" is a normal state, not a
    fault: the controls are withdrawn and that is all.
    """

    flag = 'qbzactive'
    label = 'Qobuz'

    def __init__(self):
        self.status = None
        self.read_at = 0.0

    def poll(self):
        """One /api/status read per publish cycle, shared by every caller.

        It answers "is pibuz there" and "where is its volume" at the same time,
        so knowing whether to offer the controls costs no extra call.
        """
        now = time.monotonic()
        if now - self.read_at >= PIBUZ_POLL_INTERVAL:
            self.read_at = now
            self.status = pibuz_request('/api/status', method='GET')
        return self.status

    def invalidate(self):
        """Re-read on the next cycle instead of serving a level we just moved."""
        self.read_at = 0.0

    def reachable(self):
        return self.poll() is not None

    def volume_usable(self, cfg_rows):
        # pibuz has its own software volume, so moOde's mixer scope does not
        # apply: a fixed 0dB output does not stop it.
        return True

    def volume_state(self):
        playback = (self.poll() or {}).get('playback') or {}
        level = playback.get('volume')
        if level is None:
            return None
        # pibuz works in 0.0-1.0, Home Assistant and moOde in 0-100. Measured:
        # /api/status reports the LIVE level, so it reads 0 while muted and
        # comes back on unmute - pibuz keeps the pre-mute level to itself
        # (nominal_volume is applied on its command routes, not here).
        return int(round(level * 100)), bool(playback.get('muted')), 'renderer'

    def transport(self, verb):
        if pibuz_request('/api/playback/%s' % verb) is None:
            self.invalidate()

    def set_volume(self, level):
        self.volume_cmd({'volume': max(0, min(100, level)) / 100.0})

    def step_volume(self, direction, amount):
        delta = amount / 100.0
        self.volume_cmd({'delta': delta if direction == 'up' else -delta})

    def set_mute(self, wanted):
        # pibuz takes the explicit form, so unlike moOde there is no need to
        # read the current state before deciding.
        self.volume_cmd({'mute': 'toggle' if wanted is None
                         else ('on' if wanted else 'off')})

    def volume_cmd(self, body):
        pibuz_request('/api/playback/volume', body)
        self.invalidate()


class BluezBackend(Backend):
    """Bluetooth: AVRCP for transport, BlueALSA for the mixer.

    Two services, each owning what it owns. bluez carries the remote-control
    session with the phone; BlueALSA carries the local mixer, whose level is the
    same value bluez publishes on MediaTransport1 - measured equal on both sides
    - plus a real mute flag bluez does not expose at all.

    We are the remote here, not the player, so the phone decides what a command
    does: `Previous` past the first seconds of a track restarts it instead of
    going back. That is the phone's rule, and not something to correct.
    """

    flag = 'btactive'
    label = 'Bluetooth'

    VERBS = {'play': 'Play', 'pause': 'Pause', 'stop': 'Stop',
             'next': 'Next', 'previous': 'Previous'}

    def __init__(self):
        self.player = None
        self.pcm = None

    def forget(self):
        """Both paths carry the device address, so a disconnect invalidates them."""
        self.player = None
        self.pcm = None

    def player_path(self):
        if self.player is None:
            self.player = dbus_find(BLUEZ, BLUEZ_PLAYER)
        return self.player

    def pcm_path(self):
        if self.pcm is None:
            self.pcm = dbus_find(BLUEALSA, BLUEALSA_PCM, BLUEALSA_ROOT)
        return self.pcm

    def reachable(self):
        path = self.player_path()
        if path is None:
            return False
        if dbus_prop(BLUEZ, path, BLUEZ_PLAYER, 'Status') is None:
            self.forget()                  # the device left, the path is stale
            return False
        return True

    def volume_usable(self, cfg_rows):
        return self.raw_volume() is not None

    def raw_volume(self):
        """(level 0-127, muted) exactly as BlueALSA holds it, or None.

        Kept on BlueALSA's own scale so a mute or a step does not round-trip
        through 0-100 and drift.
        """
        pcm = self.pcm_path()
        raw = dbus_prop(BLUEALSA, pcm, BLUEALSA_PCM, 'Volume') if pcm else None
        if raw is None:
            self.pcm = None
            return None
        left = (int(raw) >> 8) & 0xFF
        return left & BT_LEVEL_MAX, bool(left & BT_MUTE_BIT)

    def write_volume(self, level, muted):
        byte = (BT_MUTE_BIT if muted else 0) | max(0, min(BT_LEVEL_MAX, level))
        # Both channels together: this bridge has one volume, not a balance.
        return dbus_set_prop(BLUEALSA, self.pcm_path(), BLUEALSA_PCM, 'Volume',
                             dbus.UInt16((byte << 8) | byte))

    def volume_state(self):
        raw = self.raw_volume()
        if raw is None:
            return None
        level, muted = raw
        return int(round(level * 100.0 / BT_LEVEL_MAX)), muted, 'renderer'

    def transport(self, verb):
        path = self.player_path()
        if path is None:
            return
        if verb == 'toggle':
            # AVRCP has no toggle, so ask what it is doing before deciding.
            status = str(dbus_prop(BLUEZ, path, BLUEZ_PLAYER, 'Status') or '')
            verb = 'pause' if status == 'playing' else 'play'
        if not dbus_call(BLUEZ, path, BLUEZ_PLAYER, self.VERBS[verb]):
            self.forget()

    def set_volume(self, level):
        raw = self.raw_volume()
        if raw is None:
            return
        self.write_volume(int(round(max(0, min(100, level)) * BT_LEVEL_MAX / 100.0)),
                          raw[1])

    def step_volume(self, direction, amount):
        raw = self.raw_volume()
        if raw is None:
            return
        step = int(round(amount * BT_LEVEL_MAX / 100.0))
        self.write_volume(raw[0] + (step if direction == 'up' else -step), raw[1])

    def set_mute(self, wanted):
        raw = self.raw_volume()
        if raw is None:
            return
        level, muted = raw
        wanted = (not muted) if wanted is None else wanted
        if wanted != muted:
            self.write_volume(level, wanted)


class AirPlayBackend(Backend):
    """AirPlay, driven over shairport-sync's MPRIS interface.

    MPRIS carries all six verbs, `PlayPause` included, so nothing is composed.

    Two measured traps shape this:

    - `systemctl is-active shairport-sync` reads `inactive` the whole time it is
      playing, and the unit is `disabled`: moOde runs it as a child of php-fpm.
      Its own `Active` property is the honest sign it is there.
    - `PlaybackStatus` does NOT follow the sender - it stayed "Playing" across a
      pause the ALSA substream clearly registered - so it is never read here.
      `state` keeps coming from the substream, as it already did.
    """

    flag = 'aplactive'
    label = 'AirPlay'

    VERBS = {'play': 'Play', 'pause': 'Pause', 'toggle': 'PlayPause',
             'stop': 'Stop', 'next': 'Next', 'previous': 'Previous'}

    def reachable(self):
        return bool(dbus_prop(SHAIRPORT_NAME, SHAIRPORT_PATH,
                              SHAIRPORT_IFACE, 'Active'))

    def volume_usable(self, cfg_rows):
        return dbus_prop(MPRIS_NAME, MPRIS_PATH, MPRIS_PLAYER,
                         'Volume') is not None

    def mute_usable(self, cfg_rows):
        # MPRIS has no mute, and shairport's own `mutetoggle` reports nothing
        # back. A switch has to show a state; this one would be guessing.
        return False

    def volume_state(self):
        vol = dbus_prop(MPRIS_NAME, MPRIS_PATH, MPRIS_PLAYER, 'Volume')
        if vol is None:
            return None
        # MPRIS works in 0.0-1.0. Measured: the sender clamps to its own ceiling
        # and the value trails the command, so this reports where the sender says
        # it is - never what was asked for.
        return int(round(float(vol) * 100)), False, 'renderer'

    def transport(self, verb):
        dbus_call(MPRIS_NAME, MPRIS_PATH, MPRIS_PLAYER, self.VERBS[verb])

    def set_volume(self, level):
        dbus_call(MPRIS_NAME, MPRIS_PATH, MPRIS_PLAYER, 'SetVolume',
                  dbus.Double(max(0, min(100, level)) / 100.0))

    def step_volume(self, direction, amount):
        state = self.volume_state()
        if state is None:
            return
        target = state[0] + (amount if direction == 'up' else -amount)
        self.set_volume(max(0, min(100, target)))


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

        # Every source the bridge can drive, keyed by the flag it serves.
        self.backends = {b.flag: b for b in (MoodeBackend(), PibuzBackend(),
                                             BluezBackend(), AirPlayBackend())}
        # Whatever is playing now. The publisher thread sets it, the MQTT thread
        # reads it, so a command can land on a source that stopped less than a
        # cycle ago - true before backends existed as well.
        self.backend = self.backends[None]
        self.source_label = 'moOde'
        self.volume_ok = False
        self.mute_ok = False

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

    def backend_for(self, active_flag, renderer_active):
        """The backend driving what is playing, or None when nothing drives it.

        renderer_active is wider than active_flag: a multiroom receiver (rxactive)
        raises no renderer flag yet still owns the output, so keying on the flag
        alone would hand it moOde's backend and offer controls that move the wrong
        player.
        """
        if not renderer_active:
            return self.backends[None]
        # active_flag is None for a multiroom receiver: look up nothing, or the
        # None key would hand back moOde's own backend.
        return self.backends.get(active_flag) if active_flag else None

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
    #
    # Parsing the payload is the bridge's job; acting on it is the backend's. A
    # source with no backend is refused out loud instead of being sent to
    # whatever else happens to listen.

    def for_command(self, what):
        backend = self.backend
        if backend is None:
            log('%s ignored: %s has no backend' % (what, self.source_label))
            return None
        return backend

    def cmd_volume(self, payload):
        parts = payload.split()
        if not parts:
            return

        backend = self.for_command('volume command')
        if backend is None:
            return
        if not self.volume_ok:
            log('volume command ignored: no volume to move on %s' % backend.label)
            return

        if parts[0] in ('up', 'dn', 'down'):
            amount = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() \
                else self.cfg['volume_step']
            backend.step_volume('up' if parts[0] == 'up' else 'dn', amount)
        elif parts[0].isdigit():
            backend.set_volume(int(parts[0]))
        else:
            log('unknown volume payload: %r' % payload)

    def cmd_mute(self, payload):
        wanted = payload.lower()
        if wanted in ('on', 'true', '1', 'mute'):
            state = True
        elif wanted in ('off', 'false', '0', 'unmute'):
            state = False
        else:
            state = None                              # anything else toggles

        backend = self.for_command('mute command')
        if backend is None:
            return
        if not self.mute_ok:
            log('mute command ignored: %s has no mute to read' % backend.label)
            return
        backend.set_mute(state)

    def cmd_transport(self, payload):
        verb = payload.lower()
        verb = TRANSPORT_ALIASES.get(verb, verb)
        if verb not in TRANSPORT_VERBS:
            log('unknown transport payload: %r' % payload)
            return

        backend = self.for_command('transport %r' % verb)
        if backend is not None:
            backend.transport(verb)

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

        # Pick the backend for whatever is playing, and let Home Assistant offer
        # exactly what that backend can do. A source with no backend keeps its
        # controls withdrawn rather than letting a button move something it does
        # not reach - moOde does the same, its renderer indicator covers the
        # playback screen entirely.
        #
        # Transport: a renderer plays while MPD is stopped, so moOde's own
        # commands would reach MPD and not what is heard.
        # Volume: the renderer sets its own level from its app, and on a
        # hardware mixer raising the DAC to compensate would stay raised once
        # MPD takes the output back - loud. Also withdrawn on a fixed 0dB
        # output, where moOde's volume changes nothing at all.
        self.backend = self.backend_for(active_flag, renderer_active)
        self.source_label = (dict(RENDERER_LABELS).get(active_flag, 'the renderer')
                             if renderer_active else 'moOde')

        self.check_for_update()
        usable = self.backend is not None and self.backend.reachable()
        self.volume_ok = usable and self.backend.volume_usable(cfg_rows)
        self.mute_ok = usable and self.backend.mute_usable(cfg_rows)
        self.publish('controls/available', 'online' if usable else 'offline')
        self.publish('volume/available', 'online' if self.volume_ok else 'offline')
        self.publish('mute/available', 'online' if self.mute_ok else 'offline')

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
        # The backend driving the sound owns the level it reports. Without this
        # the number in Home Assistant would come from MPD while the slider
        # beside it moved a renderer.
        vol_state = self.backend.volume_state() if self.backend else None
        if vol_state is not None:
            volume, muted, scope = vol_state
        else:
            volume = int(cfg_rows.get('volknob') or 0)
            muted = cfg_rows.get('volmute') == '1'
            scope = volume_scope(cfg_rows)
        if renderer_active:
            state = 'play' if card_open else 'stop'
        else:
            state = status.get('state', 'unknown')

        player = {
            'state': state,
            # moOde's knob, not MPD's own volume: with a hardware mixer MPD does
            # not carry moOde's level. While a renderer with a backend plays,
            # this is that renderer's level instead - see above. Always the real
            # value; whether the control should be *used* right now is carried
            # by its own availability topic.
            'volume': volume,
            'volume_scope': scope,
            'mute': muted,
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
            # Its own gate, not the volume's: AirPlay has a level to move and no
            # mute to read.
        }, extra_availability=self.topic('mute/available'))
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
