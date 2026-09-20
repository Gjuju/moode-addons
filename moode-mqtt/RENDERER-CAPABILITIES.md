# What each renderer offers beyond what the bridge uses

A working note, filled in as each renderer gets a backend. The point is to decide
**once, at the end** what is worth exposing - not to expose things because they
happen to exist.

Everything here is measured on a real box unless marked otherwise. "Used" means
the bridge already does something with it today.

---

## Qobuz Connect - pibuz 2.4.1

Control surface: an HTTP daemon on `127.0.0.1:8182`, unauthenticated by its own
default (`[server] token` is opt-in), reachable by `www-data`. Plus an SSE event
stream. Measured on .9, 2026-09-17.

> Not in moOde 10.3.5 stock: `/usr/bin/pibuz` is absent on the Pi .179. Today this
> backend only runs on a box tracking develop.

### Used

| what | where |
|---|---|
| play, pause, toggle, stop, next, previous | `POST /api/playback/<verb>` |
| volume, mute | `POST /api/playback/volume` - `{volume\|delta\|mute}`, 0.0-1.0 |
| level + muted reported back | `GET /api/status` -> `playback` |
| is it alive | same call |

### Available, not used

**Transport**
- `seek` - absolute (`{"position": s}`) or relative (`{"delta": s}`)
- `shuffle` - on / off / toggle
- `repeat` - off / all / one
- stop-after-track (`stop_after_track_id` in the queue state)

**Queue**
- read the whole queue: `current_track`, `upcoming`, `history`, `current_index`,
  `total_tracks`
- `queue/add`, `queue/clear`, `queue/jump`

**Content** - a whole Qobuz client, really
- `search`, `browse` (`album`, `artist`), `discover`, `reco`, `radio`
- favorites: list, add, remove
- playlists: list, read, create, delete, update, add/remove tracks
- `lyrics`

**Metadata richer than moOde's `qbzmeta.json`**
- `track_id`, `album_id`, `artwork_url`, `duration_secs`
- `bit_depth`, `sample_rate`, `hires`, `streamable`, `parental_warning`
- live `position` in seconds, `queue_len`, `buffer_progress`
- `gapless_next_track_id`, `gapless_ready`
- ...but **no codec name**: moOde composes `FLAC 24/192 kHz` itself, pibuz never
  says "FLAC" anywhere (checked `/api/status`, `/api/queue`, `/api/info`).

**Audio chain**
- `device_open`, `device_present` - the renderer's own view of the ALSA device
- `bit_perfect` mode (`DirectHardware` / `PluginFallback` / `Disabled`)
- `sample_rate` (decoded stream) vs `output_sample_rate` (device) - the two
  differing **is** resampling, stated rather than inferred

**Diagnostics**
- `last_errors.auth`, `.stream`, `.transport`
- cache L1/L2: bytes, track count, budget, directory
- `network.online`, `uptime_secs`, `version`, memory class
- QConnect: `enabled`, `is_active`, `session_active`, `pairing`, `state`,
  `device_name`

**Push instead of polling**
- `GET /api/events` - SSE. Would replace the 1 Hz read for this renderer, and is
  the only push source available anywhere in the bridge, MPD's idle aside.

**Unverified**
- MPRIS on D-Bus (`org.mpris.MediaPlayer2`). pibuz's own source says it is only
  published where a session bus exists and returns nothing gracefully on a
  headless daemon - never observed on a bus here, so do not count on it.

---

## AirPlay - shairport-sync 5.5.1

**Implemented**: transport and volume, no mute. The one that works on a **stock Pi**: the moOde binary is
built `metadata-mqtt-dbus-mpris` (verified on .179, 10.3.5). Measured on .9 with
a real AirPlay 2 stream from the OwnTone bench in
`~/Code/MoodePerso/airplay-test-sender` (`timing=PTP`, so the AirPlay 2 path,
not the RAOP fallback), 2026-09-17.

Two control surfaces, and they are alternatives, not layers:

**MPRIS on the system bus** - `org.mpris.MediaPlayer2.ShairportSync`,
`/org/mpris/MediaPlayer2`.

| what | detail |
|---|---|
| transport | `Play` `Pause` `PlayPause` `Stop` `Next` `Previous` — all six natively, `PlayPause` **is** the toggle |
| also | `Seek`, `SetPosition`, `SetVolume`, `OpenUri` |
| declared capability | `CanControl` `CanGoNext` `CanGoPrevious` `CanPlay` `CanPause`, and `CanSeek` **false** on a stream — MPRIS states its own limits, which maps straight onto the backend contract |
| metadata | `xesam:title`, `artist`, `album`, `genre`, `mpris:artUrl` — an **absolute `file://`** path, where moOde's cache keeps a relative one |
| volume | `SetVolume` works; the sender may clamp it, and the reported value can trail the command |
| modes | `LoopStatus`, `Shuffle`, `Rate` writable. Set aside on purpose |

**shairport-sync's own MQTT client** - `enable_remote = "yes"` in the `mqtt`
section of `/etc/shairport-sync.conf` publishes and accepts `play`, `pause`,
`playpause`, `stop`, `nextitem`, `previtem`, `volumeup`, `volumedown`,
`mutetoggle` under `<topic>/remote`, and can even announce itself to Home
Assistant. **Zero code**, but a separate MQTT client with its own topics and its
own HA device, beside this bridge rather than inside it. moOde only ever `sed`s
individual keys in that file, so the setting survives a moOde update.

Three measured behaviours that a backend has to respect:

- **`systemctl is-active shairport-sync` says `inactive` while it runs.** moOde
  starts it as a child of **php-fpm**, not through the systemd unit, and the unit
  is `disabled`. Any reachability check based on the unit would be wrong every
  time. Use the D-Bus name.
- **`PlaybackStatus` does not follow the sender.** It stayed `"Playing"` through
  a pause, a play and two `PlayPause` calls, while the ALSA substream went
  `RUNNING` → `PREPARED` → `RUNNING`. The commands *did* reach the sender — the
  substream proves it — but the reported state is not to be trusted. The bridge
  already takes `state` from the substream, so this costs nothing; it would have
  cost a lot if MPRIS had looked authoritative.
- **the volume read back is not the volume sent.** The sender clamps to its own
  ceiling (the bench's `max_volume = 6` gave exactly 6/11 = `0.545455` for any
  higher request), and the property trailed the command by more than 5 s in one
  sequence while updating within 2 s in another. Latency not characterised.

---

## Bluetooth - AVRCP

**Implemented**: transport, volume, mute and metadata. Measured on .9 with a
Xiaomi 15T Pro connected and playing, 2026-09-17. `bluetoothd` runs with no
`--noplugin`, so a2dp and avrcp are loaded on a stock setup.

The roles are worth stating: the phone is the **source**, the player is the
**sink**, so the phone exposes the media player and the bridge acts as the
remote. A `player0` node appears under the device on D-Bus as soon as it
connects.

### Used

| what | where |
|---|---|
| Play, Pause, Stop, Next, Previous | `org.bluez.MediaPlayer1` on `…/dev_XX/player0` |
| `Status` | same object — read only to compose the toggle AVRCP does not have |
| volume and mute | `org.bluealsa.PCM1.Volume`, one uint16: high byte left, low byte right, bit 7 mute, bits 0-6 level (0-127). Measured: level 34 reads `0x2222`, muted `0xa2a2`, so a mute keeps the level |
| metadata | `Track` — Title, Artist, Album, Genre, Duration (ms) |
| source format | `org.bluealsa.PCM1.Codec` — the codec alone, as moOde shows it |
| decoded format | `org.bluealsa.PCM1` `Format` + `Sampling` + `Channels`. `Format` is the numeric form of the name `bluealsa-cli` prints: `S24_LE` reads `0x8418`, low byte `0x18` = 24 |

Both object paths carry the device address, so they are looked up through each
service's `ObjectManager` and dropped when the device leaves.

The same `Volume` is what bluez publishes on `MediaTransport1` — verified equal
on both sides — so this is one control, not two.

### Available, not used

| what | where |
|---|---|
| position | `Position`, in ms — the payload carries no playing position at all |
| TrackNumber, NumberOfTracks | `Track` |
| `Running` | `org.bluealsa.PCM1` |
| the app playing on the phone | `Name` = `Qobuz` |

There is **no artwork in AVRCP at all**, so `cover_url` stays empty for
Bluetooth rather than borrowing one from somewhere else.

Two measured caveats on the metadata: a track with no `Genre` key leaves that
field empty, and the phone's app decides what `Duration` means — one reported
60 s for a stream. The bridge passes on what it was told.

Measured behaviour worth carrying into the backend:

- **there is no Toggle**, so it has to be composed from `Status` - the six-verb
  contract still holds, it is just one line.
- **`Previous` follows the player's own rule**, not ours: sent ~3.5 s into a
  track it restarted that track rather than going back. The call succeeded; the
  phone decided. Nothing to fix, but do not promise "previous track".
- every property is `emits-change`, so `PropertiesChanged` would give **push**
  instead of polling - the second push source available anywhere in the bridge.
- `Repeat`, `Shuffle`, `Scan`, `Equalizer` are writable. Set aside on purpose.

**n=1.** One phone, one app. AVRCP target support varies between phones, and
what `Previous` does varies between apps.

### Available, not used

`FastForward`, `Rewind`, `Hold`, `Press`, `Release` (raw AVRCP key events), and
`Browsable` / `Searchable` / `Playlist` for browsing the phone's library.

---

## Squeezelite - LMS (Lyrion 9.1.2), squeezelite 2.0.0-1541

The odd one out: **squeezelite itself offers nothing**. `squeezelite -?` lists
slimproto, LIRC and GPIO as its only inputs — no socket, no D-Bus, no HTTP — and
moOde passes no `-V`, so even the volume is software inside it, driven by the
server. Every capability below therefore belongs to LMS, on another machine.
This is the only backend here that leaves the box.

The server is not configured anywhere: squeezelite finds it by UDP broadcast and
writes the answer nowhere, but it holds a TCP connection to it, so the address
comes out of `/proc/net/tcp` — the peer on port `3483`. Read from `/proc` rather
than from `ss`: same information, no fork, no privileges.

### Used

| what | where |
|---|---|
| play, pause, stop | `play` · `pause 1` · `stop` |
| toggle | `pause` **with no argument** — LMS's own toggle, nothing to compose |
| next, previous | `playlist index +1` · `playlist index -1` |
| volume | `mixer volume <0-100>`, and `+N` / `-N` for a relative step — no read needed first |
| mute | `mixer muting 1 \| 0 \| toggle` |
| level + mute reported back | `status` → `mixer volume`, **the sign is the mute** |
| metadata | `status` tags `algdKroTI` — artist, album, genre, duration (seconds), artwork, type, sample size and rate |
| source format | `type` + `samplesize` + `samplerate` |
| is it alive | the same `status` call |

### Available, not used

| what | where | note |
|---|---|---|
| playing position | `status` → `time` | the **only** renderer anywhere here that reports one. Deliberately dropped: the payload no longer carries a position at all, because a field that ticks every second is a republish every second |
| push notifications | port **9090**, send `listen 1` | one line per event, no polling — measured working. Not retained: a plain read costs no CPU, only a wait |
| power | `power 0 \| 1` | stops and resumes. This is what `slpower.sh` hooks to write `slactive` |
| shuffle, repeat | `playlist shuffle` · `playlist repeat` | state seen in `status`; commands not tried. Set aside on purpose |
| seek | `time <seconds>` | not tried |
| the whole library | `titles` · `albums` · `artists` · `playlists` · `favorites` | browsing and queueing, far beyond this bridge |
| queue editing | `playlist add \| load \| insert \| delete` | |
| multiple players, sync groups | `players` · `sync` | one LMS can drive several boxes at once |
| bitrate as text | `status` → `bitrate` | e.g. `98kbps VBR` — text, not the number the payload carries |

**Measured traps, each of which would have produced a wrong reading:**

- **a mute is a NEGATIVE volume.** Muted at 20, `mixer volume` answers `-20` —
  the level is kept in the magnitude, as BlueALSA keeps it under its mute bit.
  One `status` therefore answers both, and `mixer muting` never has to be asked.
- **`mixer muting` answers nothing at all until something has set it once**: the
  key is absent rather than `0`. Taking the mute from the sign sidesteps this.
- **LMS invents placeholders**: an untagged file comes back as `No Artist`,
  `No Album`, `No Genre`. Measured, all three. They are filtered — an invented
  placeholder reaching Home Assistant would look like a real tag.
- **`type` holds two different kinds of value**: a three-letter code for a local
  file (`flc`), and a ready-made label for a stream (`MP3 Radio`). Uppercasing
  it blindly turned the second into `MP3 RADIO`.
- **a stream keeps its tags in `remoteMeta`**, leaving the playlist entry holding
  the bare URL. Reading only one of the two reports a radio as having no artist.
- **`slactive` is not a play state.** moOde writes it from `slpower.sh`, which
  LMS drives on *power* commands: measured at `1` with squeezelite running and
  no server reachable at all.
- LMS reports no **decoded** format, so `oformat` is left empty rather than
  guessed. `samplesize`/`samplerate` describe the source file.

**Cost.** A `status` read is ~1 ms of server work on top of the plain network
round trip, and waiting on it costs no CPU: the bridge measured 0.33 % of a core
with Squeezelite playing against 0.32 % idle. LMS authentication is optional and
off by default; with it on the call answers `401` and the controls are withdrawn.

**n=1.** One LMS version, one squeezelite version, one box.

---

## Still to measure

- Spotify - librespot has no local control interface at all. Everything goes
  through Spotify Connect. This one is a real no.
- Plexamp - serves the Plex player protocol on port **32500**, unauthenticated
  on loopback: moOde ships `www/util/plexamp_status.py`, which polls
  `/player/timeline/poll` there (and is called from nowhere). Transport, volume
  and metadata live in the same URL space. Read, **not measured** — Plexamp
  needs a Plex account and a claim code to install.
- RoonBridge - RAAT, proprietary; moOde's own guide sends everything to the Roon
  app. No local endpoint. Treat as a no, like Spotify.
- UPnP - **not a renderer**: `upmpdcli` is a façade over MPD. Measured on the
  box, it holds two connections to `127.0.0.1:6600` and opens no ALSA device,
  which is why moOde raises no flag for it. MPD plays, so nothing is withdrawn
  and moOde's own backend already drives it.
