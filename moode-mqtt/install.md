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
  knob goes stale. This bridge routes every volume change through
  `/var/www/util/vol.sh`, which owns `volknob`, `volmute` and the amixer-vs-mpc
  choice.
- **Non-MPD sources.** AirPlay, Spotify Connect, Qobuz, Bluetooth and line-in
  never touch MPD. This bridge reads the ALSA substream instead, so they count.
- **The local display.** Not visible to MPD at all.

Note that Home Assistant has **no `media_player` platform over MQTT discovery**,
so what appears in HA is sensors, buttons, a number and a switch — which is what
automations want anyway.

## Install

```bash
cp moode-mqtt.conf.sample moode-mqtt.conf     # fill in the broker credentials
rsync -a --exclude .git ./ moode@<box>:~/moode-mqtt/
ssh moode@<box> 'cd ~/moode-mqtt && sudo ./install.sh'
```

`moode-mqtt.conf` is **gitignored** — it holds the broker password and is
deployed to `/etc/moode-mqtt.conf` as `0640 root:www-data`. Only
`moode-mqtt.conf.sample` is committed.

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

The installer pulls `python3-paho-mqtt` and `python3-musicpd` (both in trixie:
paho 2.1.0, musicpd 0.9.2), installs the daemon plus its unit, and checks that
the broker connection actually came up — a wrong password otherwise leaves the
service `active` and silent.

Re-runnable; it only restarts what changed.

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
| `moode/<id>/display/app` | `webui` / `peppy` / `none` |
| `moode/<id>/display/power` | `ON` / `OFF` |

### The `player` payload

| key | value |
|---|---|
| `state` | `play` / `pause` / `stop` |
| `volume`, `mute` | moOde's knob level and mute flag |
| `artist`, `title`, `album` | **exactly what the source reports, or empty** |
| `station` | stream `Name`, empty off a radio |
| `source` | `Radio`, `Library`, or the active renderer |
| `quality` | `24 bit / 96 kHz`, `DSD64` — from ALSA, so every source |
| `source_format` | what a renderer received: `Vorbis 320 kbps`, `FLAC 16/44.1 kHz` |
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

Measured on all four, with the bridge running:

| renderer | artist/title/album | `source_format` | notes |
|---|---|---|---|
| AirPlay | yes | `ALAC 16/44.1 kHz 2ch` | artwork path is **relative** |
| Spotify | yes | `Vorbis 320 kbps` | duration in ms |
| Qobuz | yes | `FLAC 16/44.1 kHz` | duration in seconds |
| Bluetooth | **no** | — | moOde keeps no cache for it; `source`, `state` and `quality` still work |

Three measured traps in those caches:

- **the duration unit is not the same across renderers.** AirPlay and Spotify
  report milliseconds, Qobuz reports seconds. moOde carries the same split in
  `playerlib.js` (`timeDivisor`). Measured: a Spotify track came back as
  `363866` and a Qobuz one as `501`.
- **an inactive renderer leaves an empty file, not a stale one** — moOde
  truncates the cache to zero — so there is no risk of reading yesterday's track.
- **AirPlay's `cover_url` is a path, not a URL** (`imagesw/airplay-covers/…`),
  where Spotify and Qobuz give absolute `https://` links. The bridge prefixes it
  with `web_base_url`, which defaults to `http://<hostname>.local` — set it to
  `http://<ip>` in the config when mDNS does not resolve from Home Assistant.

`quality` is read from ALSA `hw_params` rather than from MPD, precisely so it
keeps working here: it is what the DAC is actually fed, whoever opened it.

In automations, treat an empty field as "not provided":
`{{ states('sensor.<id>_artist') | length > 0 }}` rather than a test against
`unknown`.

`player` carries `elapsed`, which changes every cycle, so change detection
deliberately ignores that one field: the topic is republished when anything else
moves, and otherwise once every 30 s (`PLAYER_REFRESH`). Without that the broker
would get one retained message per second forever.

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
`binary_sensor.<id>_audio`, `binary_sensor.<id>_display_power`,
`sensor.<id>_{state,title,artist,album,station,source,quality,display_app}`,
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

## Pi compatibility

Nothing here is x86-specific. Verified against a stock **moOde 10.3.4** Pi
image on trixie (2026-09-14), with the bridge deployed and driven from Home
Assistant:

- systemd is PID 1 and `is-system-running` reports `running`, so the unit works.
  moOde's own worker runs from `rc.local` as root there, which this service is
  independent of.
- `/var/local/www/db/moode-sqlite3.db` is owned by `www-data` on the Pi exactly
  as on x86, so `vol.sh` works from a `www-data` daemon. This was the one thing
  that could have forced a different user, and it does not.
- `www-data ALL=(ALL) NOPASSWD: ALL` is present on stock moOde too.
- `python3-musicpd` is already installed; `python3-paho-mqtt` (2.1.0) comes from
  apt via `install.sh`.

**Headless boxes**: that Pi had no local display (`localdisplay` disabled, no
Xorg). `xset q` then answers nothing, and the bridge publishes **nothing** on
`display/power` rather than a made-up `OFF` — the HA entity stays `unknown`,
which is the truth. It also backs the check off to once a minute instead of
forking a `sudo xset` every second for an answer that will never come.
`display/app` still reports `none`, which is accurate: no app is on screen.

## Why not call moOde's own code

moOde already implements most of this: `getAlsaHwParams()` (`inc/alsa.php`),
`chkRendererActive()` (`inc/common.php`), the radio test in `inc/mpd.php`, and
`audioinfo.php`, which consolidates source, metadata cache and formats exactly
as the bridge does. Reusing it directly was ruled out for two reasons:

- **No MQTT client for PHP in Debian.** `apt-cache search mqtt` returns no PHP
  binding, so a PHP daemon — the natural way to `require` moOde's own includes,
  as `worker.php` and `touchmon.php` do — would need a composer or PECL
  dependency, outside the distribution.
- **Those endpoints are pages, not an API.** `audioinfo.php` opens a PHP session
  and returns presentation data; `engine-mpd.php` is long-polling built for the
  browser. Calling them once a second means forking php-fpm at 1 Hz and tying
  this bridge to moOde's front end.

So the bridge reads `/proc` and the database directly, and **duplicates a small
amount of moOde's logic** — which is a real cost, not a free choice: the ALSA
format designators, the radio test and the renderer flags all exist in moOde
already. The duplicated spots carry a pointer to their original; keep them in
step when rebasing onto a new moOde.

One case makes the cost concrete: `IEC958_SUBFRAME_LE`, the S/PDIF designator,
contains digits that are not a bit depth. moOde handles it explicitly; this
parser first reported `958 bit / 44.1 kHz` for it.

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
stuck. Every change goes through `vol.sh` or `mpc`.

**Runs as `www-data`**, the web server user — the sqlite DB is owned by
`www-data`, so a root daemon would leave root-owned journal files behind.
`www-data` has NOPASSWD sudo in moOde, which is what reading `xset q` needs.
