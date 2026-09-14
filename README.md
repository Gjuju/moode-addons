# moode-addons

Third-party add-ons for the [moOde audio player](https://moodeaudio.org/).

**Not affiliated with the moOde project.** Nothing here is installed, endorsed or
supported by moOde or by Tim Curtis — these are independent pieces that install
*on top of* a working moOde, and they are not the on-demand renderer plugins that
moOde downloads by itself.

Each add-on lives in its own directory with its own `install.md`: what it does,
how to install it, and the measured behaviour and caveats behind the design.

| add-on | what it does |
|---|---|
| [`moode-mqtt/`](moode-mqtt/) | moOde ↔ MQTT bridge with Home Assistant discovery — player state, real audio activity, local display state, plus transport and volume commands |

## Design rules

These hold across everything in this repo:

- **Nothing in the moOde tree is modified.** Each add-on is a daemon, a unit file
  and a config of its own. A moOde update never has to know they exist.
- **Go through moOde's own entry points**, never around them. Volume goes through
  `/var/www/util/vol.sh` (which owns `volknob`, `volmute` and the amixer-vs-mpc
  choice); the config database is opened **read-only**, because writing
  `cfg_system` behind moOde's back desyncs its PHP session cache and the WebUI
  then looks stuck.
- **Pi and non-Pi alike.** Tested on a stock moOde Pi image and on moOde running
  on Debian x86_64. Anything platform-specific is called out in that add-on's
  `install.md`.
- **No credentials in git.** Configs holding secrets are gitignored next to a
  committed `.sample`.

## Licence

GPL-3.0-or-later, the same licence as moOde. See [LICENSE](LICENSE).
