# moode-mqtt — moOde ↔ MQTT bridge with Home Assistant discovery

Publishes moOde's state to an MQTT broker and accepts transport/volume commands
back, so Home Assistant can drive automations. Validated both on moOde running
on Debian x86_64 and on a stock moOde 10.3.4 Pi image; nothing in it is
platform-specific.

## Why not just Home Assistant's native MPD integration

HA's MPD integration gives you a `media_player` for free, and it is a fine thing
to run alongside this bridge. Three things it cannot do:

- **Volume.** It talks to MPD directly. moOde's level lives in `cfg_system.volknob`
  and, when `mpdmixer` is *hardware*, MPD does not carry it at all — so the WebUI
  knob goes stale. This bridge routes every volume change through moOde's own
  REST API, which owns `volknob`, `volmute`, the amixer-vs-mpc choice and the
  propagation to multiroom receivers.
- **Non-MPD sources.** AirPlay, Spotify Connect, Qobuz, Bluetooth and line-in
  never touch MPD. This bridge reads the ALSA substream instead, so they count.
- **The local display.** Not visible to MPD at all.

Note that Home Assistant has **no `media_player` platform over MQTT discovery**,
so what appears in HA is sensors, buttons, a number and a switch — which is what
automations want anyway.

## Install

You need a moOde player, an MQTT broker already running somewhere on your
network (Mosquitto, the Home Assistant add-on, …), and SSH access to the player.
`git` is part of the moOde image, so nothing has to be installed first.

**On the player**, as the moOde user:

```bash
git clone https://github.com/Gjuju/moode-addons.git
cd moode-addons/moode-mqtt
cp moode-mqtt.conf.sample moode-mqtt.conf
nano moode-mqtt.conf        # broker host, username, password, instance
sudo ./install.sh
```

The installer pulls `python3-paho-mqtt` and `python3-musicpd` from apt, installs
the daemon and its systemd unit, then checks the things that can silently be
wrong: a wrong password leaves the service `active` and mute, a failed `enable`
leaves it working until the next reboot, and an unreachable REST API leaves it
publishing state while accepting no command at all. Expect:

```
[ok] service is running
[ok] enabled at boot
[ok] connected to the broker
[ok] moOde REST API answers
```

The entities appear in Home Assistant on their own, under a device named after
`friendly_name`. Nothing to add to your HA configuration.

To update later: `git pull` in that directory, then `sudo ./install.sh` again.
It is re-runnable and only restarts what changed; your `moode-mqtt.conf` is
never overwritten.

**What to fill in.** Only the first block usually matters:

```ini
[broker]
host = 192.168.1.x
username =                  # leave empty for an anonymous broker
password =

[moode]
instance = moode            # topic prefix and HA device id - pick it once
friendly_name = moOde       # the name shown in Home Assistant
```

`moode-mqtt.conf` is **gitignored** — only `moode-mqtt.conf.sample` is
committed, so a `git pull` never touches your credentials.

**Edit the copy in this directory, not the one in `/etc`.** The installer copies
this directory's `moode-mqtt.conf` to `/etc/moode-mqtt.conf`, so changes made
directly in `/etc` are overwritten on the next run. It lives in `/etc` because
the service runs as `www-data` and the player's home directory is `0700`, which
makes anything under it unreadable to the service; the installed copy is also
`0640 root:www-data`, where the working one is world-readable.

### Deploying from another machine

Editing on a workstation and pushing to the player works too, and is what the
multi-box recipe below builds on:

```bash
rsync -a --exclude .git ./ moode@<box>:~/moode-mqtt/
ssh moode@<box> 'cd ~/moode-mqtt && sudo ./install.sh'
```

### More than one box

Keep one config per box (`moode-mqtt.conf`, `moode-mqtt.conf.pi`, …) — all
gitignored by the `moode-mqtt.conf.*` rule. Both `instance` **and** `client_id`
must differ between boxes:

- a shared `instance` puts both boxes on the same topics and on the same Home
  Assistant device;
- a shared `client_id` is worse and less obvious — MQTT allows one connection
  per client id, so the broker kicks each box off as the other connects, and
  they flap forever.

Deploy a second box by keeping its own config out of the sync and landing it
under the expected name:

```bash
rsync -a --exclude .git --exclude __pycache__ --exclude 'moode-mqtt.conf' \
      ./ moode@<box>:~/moode-mqtt/
scp moode-mqtt.conf.pi moode@<box>:~/moode-mqtt/moode-mqtt.conf
ssh moode@<box> 'cd ~/moode-mqtt && sudo ./install.sh'
```

## Updating moOde

**Nothing to reinstall.** Neither a moOde update nor a re-run of a moOde
installer touches this add-on, which was verified rather than assumed:

- the bridge shares no file with moOde — `/usr/local/bin/moode-mqtt.py`,
  `/etc/moode-mqtt.conf`, `/etc/systemd/system/moode-mqtt.service` — and no
  moOde installer sweeps those directories;
- no `pkill` or `killall` in moOde can match `moode-mqtt.py`;
- no `autoremove` runs, so `python3-paho-mqtt` and `python3-musicpd` stay;
- the unit stays enabled, so it comes back on the reboot that follows.

MPD restarts during an update and the bridge loses its connection; it reconnects
on its own, retrying every 5 s. Nothing to do.

**What to check instead.** The risk is not the bridge disappearing — it is the
bridge surviving and reading the new moOde wrongly. It duplicates a little of
moOde's logic (ALSA format designators, the radio test, the renderer cache
paths, `cfg_system` column names), and if a moOde release moves one of those,
the bridge keeps publishing, quietly wrong. So after a **major** moOde update,
look once at what comes out rather than reinstalling:

```bash
mosquitto_sub -h <broker> -u <user> -P <pass> -t 'moode/<id>/player' -C 1
```

Play a local file and a radio station, and check `source`, `artist`
and `title` against what moOde's own WebUI shows. Two minutes, and it covers
exactly what could have drifted. The duplicated spots each carry a pointer to
their original in moOde's source — see *Why not call moOde's own code*.

The one case that does need `sudo ./install.sh` again is updating **the bridge
itself**:

```bash
cd moode-addons && git pull && cd moode-mqtt && sudo ./install.sh
```

Your `moode-mqtt.conf` is gitignored, so a pull never touches your credentials.

## Uninstall

```bash
sudo ./uninstall.sh                  # the normal case
sudo ./uninstall.sh --purge-deps     # also apt purge python3-paho-mqtt
sudo ./uninstall.sh --keep-retained  # leave the broker alone (reinstalling soon)
```

It stops the service first, then **clears the retained topics this box
published**, then removes the unit, the daemon and `/etc/moode-mqtt.conf` (which
carries the broker password).

That middle step is the one worth understanding. Everything here is published
retained, which means the broker — not Home Assistant — keeps the last value
forever. Deleting the device in the HA interface therefore does not stick: HA
re-reads the retained discovery message on its next restart and recreates it.
The real removal is an **empty retained payload** published over each discovery
topic, which is what the script does (19 topics on a box with a display, fewer
on a headless one). You can do the same by hand from HA with
Developer tools → Actions → `mqtt.publish`, empty payload, `retain: true` — it
is still the broker you are editing, just through HA.

Delete the deployment directory too if you kept one on the box: it holds a copy
of `moode-mqtt.conf`, password included.

## Topics

Instance id is `instance` from the config. Everything is published **retained,
on change only**.

| topic | payload |
|---|---|
| `moode/<id>/availability` | `online` / `offline` (MQTT LWT) |
| `moode/<id>/audio` | `ON` / `OFF` — the ALSA output substream, any source |
| `moode/<id>/player` | JSON, see below |
| `moode/<id>/update/available` | `ON` / `OFF` — a newer moode-mqtt has been published |
| `moode/<id>/controls/available` | `online` / `offline` — whether the transport buttons should be used |
| `moode/<id>/volume/available` | `online` / `offline` — whether the volume control should be used |
| `moode/<id>/mute/available` | `online` / `offline` — whether the mute switch should be used |
| `moode/<id>/display/app` | `webui` / `peppy` / `none` |
| `moode/<id>/display/power` | `ON` / `OFF` |

### The `player` payload

| key | value |
|---|---|
| `state` | `play` / `pause` / `stop` |
| `volume`, `mute` | the level and mute flag of whatever is playing — moOde's knob, or the renderer's own when the bridge can drive that renderer |
| `volume_scope` | `hardware` / `mpd` / `none` / `renderer` — what the knob attenuates |
| `artist`, `title`, `album` | **exactly what the source reports, or empty** |
| `station` | stream `Name`, empty off a radio |
| `source` | `Radio`, `Library`, or the active renderer |
| `source_format` | what a renderer received: `Vorbis 320 kbps`, `FLAC 16/44.1 kHz`, `aptX-HD` |
| `decoded_format` | what came out of its decoder: `PCM 24/48 kHz, 2ch` — see *Three formats* |
| `cover_url` | renderer artwork URL, empty otherwise |
| `audio` | raw MPD field `44100:24:2`, empty while a renderer plays |
| `genre`, `date`, `bitrate`, `file`, `is_radio`, `elapsed`, `duration`, `renderer_active` | as reported |

**One key, one value.** A field carries the value MPD gives for it, or nothing.
No placeholder text, no station name spilling into `artist` or `album`, and in
particular **no splitting of the ICY `StreamTitle`** into artist and title: that
field is free text, nothing guarantees it is `Artist - Title` rather than the
reverse, a show name or an advert, and splitting it would manufacture a
confidence the data does not carry. So on a radio you typically get a filled
`title` and an empty `artist` — which is what the stream actually provides.

`source` is the one computed value, and it is computed from system state rather
than guessed from text: moOde's `cfg_system` renderer flags, then `is_radio`.
It is also why the MPD text fields are **blanked while a renderer is active** —
MPD is stopped then, and its `currentsong` still describes the track from
before, so carrying it over would report a song that is not playing.

### The volume knob does not always control what you hear

moOde denies renderers the hardware mixer and makes each one attenuate in
software ([`inc/renderer.php`] sets `alsa_hardware_volume false`), so with a
hardware mixer the signal goes through **two stages**: the renderer's own level,
set from the Spotify or Qobuz app, then moOde's knob. The knob only ever
describes the second one.

What it reaches depends entirely on `mpdmixer`, which is what `volume_scope`
reports:

| `volume_scope` | the knob drives | while another source holds the output |
|---|---|---|
| `hardware` | the card's ALSA mixer, downstream of everything | applies to it too, being downstream |
| `mpd` | MPD's own mixer, nothing else | **controls nothing audible** |
| `none` | nothing — Fixed 0dB | nothing, ever |
| `renderer` | that renderer's own software level | it *is* what is playing |

`volume` always carries a real number — whichever player it belongs to. It is
never published as null or omitted: whether the control should be *used* right
now is carried by `volume/available`, not by a hole in the payload.

So a control is published as unavailable **whenever the bridge cannot reach what
is actually playing**. Each one lists its gate alongside the bridge's own
availability with `availability_mode: all`, and Home Assistant greys it out:
nothing to read wrongly, nothing to press or drag by accident, no automation
acting on something it does not reach.

| topic | goes offline when | gates |
|---|---|---|
| `controls/available` | nothing can drive what is playing | the six transport buttons |
| `volume/available` | the same, **or** there is no volume to move | the volume |
| `mute/available` | the same, **or** there is no mute to read | the mute switch |

Mute has its own gate because the two genuinely come apart: **AirPlay has a
level to move and no mute at all.** MPRIS carries none, and shairport-sync's own
`mutetoggle` reports nothing back — a switch has to show a state, and that one
would be guessing.

The gate is not "a renderer is playing" but "nothing here can drive it". Those
were the same thing until the bridge learned to drive renderers directly, and
they still are for a renderer it has no backend for — see
[Driving the renderer itself](#driving-the-renderer-itself).

The reasons the two gates differ: transport buttons otherwise talk to MPD, which
is *stopped* during a renderer, so they would not reach what is playing. The
volume gate adds the cases where there is nothing to move at all — a fixed 0dB
output, where moOde's volume command exits without changing anything.

moOde does the same in its own way: its renderer indicator covers the playback
screen entirely, so its own transport controls are not reachable either. Where
this bridge is **stricter** is a renderer it cannot drive on a hardware mixer:
moOde lets you raise the DAC there, and that raise **stays** once MPD takes the
output back, which is loud — much worse when an automation does it than a
person. Where it is **less** strict is a renderer it *can* drive, because then
it moves that renderer's own software level, which disappears with the stream.

`volume` keeps carrying a real value throughout — moOde's knob, or the
renderer's own — since it is always a true piece of some player's state. Only
the *control* is withdrawn.

moOde's own ceiling applies to everything the bridge does **to moOde**, because
that volume goes through moOde's API: set **Configure → Audio → Max volume**
(`volume_mpd_max`) and neither the WebUI nor Home Assistant can exceed it. It
does not apply to a renderer's own level, which moOde does not manage either.

### While a renderer plays

AirPlay, Spotify, Qobuz, Bluetooth and line-in never touch MPD, and MPD is
*stopped* during them — its `currentsong` and its `state` still describe the
track from before. Reporting those would be plainly wrong, so:

- `artist`, `title`, `album`, `duration` and `cover_url` come from the
  renderer's own cache, which moOde keeps as JSON with plain keys:
  `/var/local/www/{apl,spot,qbz}meta.json`. Still one key, one value.
- `state` is taken from the ALSA device being open, not from MPD.
- `file`, `station`, `audio` and `is_radio` are blanked — they describe MPD's
  idea of the world, which is stale at that moment.
- `volume`, `mute` and `volume_scope` come from the renderer itself **when the
  bridge can drive it**, so the number in Home Assistant belongs to the same
  player the slider moves. Otherwise they stay moOde's, and the control is
  withdrawn rather than left pointing at the wrong player.

Measured on all four, with the bridge running:

| renderer | artist/title/album | `source_format` | notes |
|---|---|---|---|
| AirPlay | yes | `ALAC 16/44.1 kHz 2ch` | artwork path is **relative** |
| Spotify | yes | `Vorbis 320 kbps` | duration in ms |
| Qobuz | yes | `FLAC 16/44.1 kHz` | duration in seconds |
| Bluetooth | yes | `aptX-HD` | not from a cache — moOde keeps none — but straight from AVRCP |

Bluetooth is the exception to the sentence above: moOde caches nothing for it, so
its metadata comes from the phone over AVRCP, on the same object the transport
controls use. That covers artist, title, album, genre and duration; BlueALSA
names the codec and gives the decoded depth and rate.

The one thing it does not carry is **artwork** — AVRCP has none at all, so
`cover_url` stays empty rather than borrowing one.

What the phone reports is the phone's business: a track with no `Genre` key
leaves `genre` empty, and an app that calls a stream 60 seconds long puts 60 in
`duration`. Both measured. The bridge passes on what it was told.

### Three formats, not one

moOde's Audio Information separates them, and so does the payload. They differ on
every lossy source, and confusing them is easy:

| moOde's label | payload key | Bluetooth example |
|---|---|---|
| Source format | `source_format` | `aptX-HD` — a lossy codec has no bit depth of its own |
| Decoded to | `decoded_format` | `PCM 24/48 kHz, 2ch` — what came out of the decoder |
| Output format | *not published* | see below |

**The output format is deliberately gone.** It used to be a `quality` sensor read
from ALSA `hw_params`, and it overstated the resolution on every box whose chain
pads: a 24-bit stream fed to a DAC taking `S32_LE` was reported as `32 bit`, and
the padding carries no music. The figure described the container, not the
recording. `decoded_format` is the honest one, and it is what moOde's own Audio
Information calls *Decoded to*.

Both keys are what the renderer says, so both are empty off a renderer.

Three measured traps in those caches:

- **the duration unit is not the same across renderers.** AirPlay and Spotify
  report milliseconds, Qobuz reports seconds. moOde carries the same split in
  `playerlib.js` (`timeDivisor`). Measured: a Spotify track came back as
  `363866` and a Qobuz one as `501`.
- **an inactive renderer leaves an empty file, not a stale one** — moOde
  truncates the cache to zero — so there is no risk of reading yesterday's track.
- **AirPlay's `cover_url` is a path, not a URL** (`imagesw/airplay-covers/…`),
  where Spotify and Qobuz give absolute `https://` links. The bridge prefixes it
  with `web_base_url`, which is **detected** when left empty: the source address
  that reaches the broker, asked of the routing table rather than guessed from
  the interface list, so a player with both Ethernet and Wi-Fi up gives the
  address traffic actually leaves by. It is resolved again on every broker
  connection, so a changed network is followed. Set the option only for a
  reverse proxy or a similar special case.

In automations, treat an empty field as "not provided":
`{{ states('sensor.<id>_artist') | length > 0 }}` rather than a test against
`unknown`.

`player` carries `elapsed`, which changes every cycle, so change detection
deliberately ignores that one field: the topic is republished when anything else
moves, and otherwise once every 30 s (`PLAYER_REFRESH`). Without that the broker
would get one retained message per second forever.

### Driving the renderer itself

moOde offers **no way at all** to drive a renderer: `command/index.php` has no
transport command for one, and moOde's own WebUI shows only a *disconnect*
button while one plays. So there is nothing to bypass here — a command sent to a
renderer goes to the only thing that can move it, its own daemon.

The bridge keeps one **backend per source**, chosen from the `cfg_system` flag
moOde raises for it. A backend implements all six transport verbs or it is not
registered at all; volume is separate, because it genuinely varies. A source
with no backend keeps its controls withdrawn, exactly as before.

| source | transport | volume, mute | through |
|---|---|---|---|
| moOde's own player | yes | yes, unless fixed 0dB | moOde's REST API |
| **Qobuz Connect** | yes | yes | pibuz's HTTP API on `127.0.0.1:8182` |
| **Bluetooth** | yes | yes | AVRCP (`org.bluez.MediaPlayer1`) for transport, BlueALSA for the mixer |
| **AirPlay** | yes | volume only | shairport-sync's MPRIS on the system bus |
| Spotify | no | no | librespot exposes no local control at all; everything goes through Spotify Connect |
| line-in, Squeezelite, Plexamp, RoonBridge | — | — | not looked at |
| multiroom receiver | — | — | withdrawn; the sound is another box's |

What each renderer's own interface offers, measured box in hand, is kept in
[`RENDERER-CAPABILITIES.md`](RENDERER-CAPABILITIES.md) — including the parts
this bridge deliberately does not use.

The same six payloads on `cmd/transport` work whatever is playing. Nothing in
Home Assistant changes: **no entity is added** for this, the existing buttons
and slider simply stop being greyed out for a source the bridge can drive.

**Qobuz / pibuz specifics.** pibuz is unauthenticated by its own default — its
source documents `[server] token` as opt-in — so the bridge sends no credential
and **keeps no copy of one**. If you do set a token in `qbzd.toml`, pibuz answers
`401`, the bridge logs that plainly and the Qobuz controls stay withdrawn rather
than failing silently. Note also that pibuz listens on `0.0.0.0`, not just
loopback: that is its own choice, not something this bridge introduces.

Volume is a `0.0`–`1.0` float there against moOde's `0`–`100`, converted by the
backend. `mute` is sent as an explicit `on`/`off` — unlike moOde's, whose command
is a toggle and has to be read back first.

Measured on a live Qobuz Connect session: `pause`, `play`, `next` and `previous`
all followed, `dn 5` / `up 5` moved pibuz's level and not MPD's, and the `player`
payload reported pibuz's number throughout.

| command topic | payload |
|---|---|
| `moode/<id>/cmd/volume` | `up` · `dn` · `up 5` · `dn 5` · `0`–`100` |
| `moode/<id>/cmd/mute` | `on` · `off` (absolute, not a toggle) |
| `moode/<id>/cmd/transport` | `play` · `pause` · `stop` · `toggle` · `next` · `previous` |

`toggle` follows the WebUI rule: a radio stream is **stopped**, anything else is
**paused**. A radio is detected the way `inc/mpd.php:764` does it — an `http`
file with no duration. Not by the `Radio station` artist label the WebUI shows:
that label is produced by moOde's own PHP rendering and never reaches MPD, which
reports whatever tags the stream carries, often none at all.

## Home Assistant

Discovery is automatic: the box shows up as one device named after
`friendly_name`. Entities, where `<id>` is your `instance` value:
`binary_sensor.<id>_audio`, `binary_sensor.<id>_renderer`,
`binary_sensor.<id>_display_power`, `binary_sensor.<id>_update`,
`sensor.<id>_{state,title,artist,album,station,source,display_app}`,
`number.<id>_volume`, `switch.<id>_mute`, and six buttons (play, pause, stop,
toggle, next, previous).

An `image.<id>_cover` entity is declared **disabled by default**: the artwork is
published but nothing consumes it yet, so enable it in HA the day you want it
rather than carrying an entity nobody reads. Home Assistant fetches the URL
itself, so the box has to be reachable from it.

Below, an amp switched on with the music and off after a silence — one
automation each, in the editor's YAML mode, with `<id>` and `switch.amp`
replaced by your own. The silence delay lives **in HA** with `for:`, not in the
bridge, so it can be retuned without touching the player.

Amp on as soon as anything plays:

```yaml
description: ''
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.<id>_audio
    to: "on"
conditions: []
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.amp
```

Amp off after 15 minutes of silence:

```yaml
description: ''
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.<id>_audio
    to: "off"
    for: "00:15:00"
conditions: []
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.amp
```

`mode: single` is right here: both automations are edge-triggered and there is
nothing to queue if one fires while the other is still running.

### Update notifications

`binary_sensor.<id>_update` turns on when a newer **moode-mqtt** has been
published. Update with the command in *Updating moOde* above; the sensor clears
on the next check.

It compares `/etc/moode-mqtt.version`, deployed by the installer, against this
sub-project's `VERSION` file as published — **not** the repository's HEAD.
`moode-addons` holds other add-ons, and a commit to one of those is not an
update to this bridge.

This is the only thing here that reaches outside your network: one plain GET of
a text file, every 12 hours. Set `update_check = no` in the config to disable it,
and the sensor is not even declared. An unreachable network leaves the sensor as
it was rather than claiming anything, and an install predating the `VERSION`
file simply does not check.

The installed version also shows as the device's firmware version in Home
Assistant.

### Branching on the renderer

`binary_sensor.<id>_renderer` answers a different question from availability:
*who holds the output*, rather than *can I act on it*. The two no longer move
together — Qobuz sets the sensor **on** while its controls stay perfectly
usable — so pick the one that matches what the automation actually needs.

Use it when the automation cares that an external source is playing: don't touch
the queue, don't announce the next library track. An automation that presses
`Play` on a source with no backend targets an unavailable entity, which Home
Assistant refuses and logs — harmless, but noise a condition avoids.

```yaml
conditions:
  - condition: state
    entity_id: binary_sensor.<id>_renderer
    state: "off"
```

The other way round, to react *to* a renderer — dimming the lights when someone
casts to the player, without the local library doing the same:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.<id>_renderer
    to: "on"
```

It pairs with `sensor.<id>_source`: the boolean answers "is an external source
holding the output", the text answers "which one".

## Pi compatibility

Nothing here is x86-specific. Verified against a stock **moOde 10.3.4** Pi
image on trixie (2026-09-14), with the bridge deployed and driven from Home
Assistant:

- systemd is PID 1 and `is-system-running` reports `running`, so the unit works.
  moOde's own worker runs from `rc.local` as root there, which this service is
  independent of.
- `/var/local/www/db/moode-sqlite3.db` is owned by `www-data` on the Pi exactly
  as on x86, so the daemon reads it as `www-data`. This was the one thing that
  could have forced a different user, and it does not.
- `www-data ALL=(ALL) NOPASSWD: ALL` is present on stock moOde too.
- `python3-musicpd` is already installed; `python3-paho-mqtt` (2.1.0) comes from
  apt via `install.sh`.

**Headless boxes**: that Pi had no local display (`localdisplay` disabled, no
Xorg). `xset q` then answers nothing, and the bridge publishes **nothing** on
`display/power` rather than a made-up `OFF` — the HA entity stays `unknown`,
which is the truth. It also backs the check off to once a minute instead of
forking a `sudo xset` every second for an answer that will never come.
`display/app` still reports `none`, which is accurate: no app is on screen.

## How much of moOde's own code is used

**Every command moOde has a mechanism for goes through moOde's REST API**,
`www/command/index.php`, never straight to MPD or to `vol.sh`. That endpoint is
a REST API by design — its own source notes it handles *"CLI based REST commands
sent for example by curl"* — and it carries internal mechanisms that are
invisible from outside:

| command | what moOde does that calling the tool directly would skip |
|---|---|
| `set_volume` | propagates the change to **multiroom receivers** (`updReceiverVol`), and refuses outright while a renderer is active |
| `toggle_play_pause` | applies moOde's radio rule — stop a stream, pause a file |
| anything else | relayed to MPD, after moOde's own argument validation |

The multiroom propagation is the one worth naming: a bridge calling `vol.sh`
directly changes the master level and leaves the receivers where they were, with
nothing to indicate it.

The exception is a **renderer**, where moOde has no mechanism to use in the first
place — `set_volume` refusing while one is active is moOde stating exactly that.
Those commands go to the renderer's own daemon, which is the only thing that can
move it. See [Driving the renderer itself](#driving-the-renderer-itself).

**The state read stays direct** — `/proc` and the database, at 1 Hz. Routing it
through PHP too would fork php-fpm every second, forever, on hardware that may
be a Pi. Commands are rare enough that the same cost is nothing.

That split leaves **some duplicated logic** on the read side, and it is a real
cost rather than a free choice: the ALSA format designators, the radio test and
the renderer flags all exist in moOde already (`getAlsaHwParams()` in
`inc/alsa.php`, `chkRendererActive()` in `inc/common.php`, the test in
`inc/mpd.php`). The duplicated spots carry a pointer to their original; keep
them in step when moving to a new moOde, and see *Updating moOde* above.

One case makes that cost concrete: `IEC958_SUBFRAME_LE`, the S/PDIF designator,
contains digits that are not a bit depth. moOde handles it explicitly; this
parser first reported `958 bit / 44.1 kHz` for it.

Note that a PHP daemon — the natural way to `require` moOde's includes, as
`worker.php` and `touchmon.php` do — is not an option here: `apt-cache search
mqtt` returns no PHP binding in Debian, so it would need a composer or PECL
dependency from outside the distribution.

## Measured behaviour and caveats

**Do not drive the amp from the display state.** Two unrelated mechanisms blank
the screen and only one of them has anything to do with audio:

- `worker.php` `chkPeppyScnBlank()` implements moOde's documented "screen off
  after playback has stopped" (`scn_blank`) — but `worker.php:2029` gates it on
  `peppy_display == '1'`, so it only counts **while Peppy is on screen**.
- `.xinitrc` arms `xset s 600 0` / `xset dpms 600 0 0`: plain X **input**
  inactivity, 10 minutes, entirely unrelated to whether music is playing.

Measured on an x86 box with a touch panel (2026-09-14): `scn_blank=600`,
`peppy_display=0`, `local_display=1`, `peppy_scn_blank_active=0`, and yet
`xset q` reported `Monitor is in Standby`. The screen was off, and `scn_blank` had nothing to do
with it — it was the DPMS timeout. With `touchmon_svc=1` the two also fight each
other: playback stops → touchmon switches back to the WebUI within seconds →
`peppy_display` drops to 0 → the `scn_blank` countdown stops before it finishes.
That last step is read from the code and consistent with the snapshot above, not
an observed full cycle.

Hence `audio`, computed from the ALSA substream, is the signal for the amp;
`display/*` is published for information.

**`audio` debounce.** MPD closes the ALSA device between tracks, which is why
`touchmon.php` requires `TOUCHMON_CLOSED_COUNT` consecutive closed readings. The
bridge does the same with `audio_off_delay` (default 5 s). It turns **on**
immediately.

**No writes to `cfg_system`.** The database is opened read-only. Writing it
behind moOde's back desyncs its PHP session cache, and the WebUI then looks
stuck. Every change goes through moOde's REST API, or — for a renderer moOde
cannot drive — through that renderer's own daemon.

**Runs as `www-data`**, the web server user — the sqlite DB is owned by
`www-data`, so a root daemon would leave root-owned journal files behind.
`www-data` has NOPASSWD sudo in moOde, which is what reading `xset q` needs.
