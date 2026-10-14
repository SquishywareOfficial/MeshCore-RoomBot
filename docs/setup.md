# Repeatable bot setup

Run `setup_bot.py` from a copy of this bot checkout with **Python 3.10+**.
The installer creates a dedicated virtual environment and application snapshot,
installs dependencies, imports supplied content, validates it and writes a
launcher. It neither connects to USB nor starts a service. Existing config,
cursor/outbox state and community data are preserved.

## Full New Zealand installation: Pi OS 64-bit / Linux

Use a 64-bit little-endian OS. This prepared graph was tested on Linux x86_64
and Raspberry Pi aarch64. The small native router is compiled for the actual
host. National map preprocessing is **not** performed on the Pi Zero.

On Debian/Pi OS, the native compiler needs these system prerequisites:

```sh
sudo apt install python3 build-essential libz-dev libbz2-dev
```

Clone the standalone bot repository, then run the installer:

```sh
git clone https://github.com/SquishywareOfficial/MeshCore-RoomBot.git
cd MeshCore-RoomBot
python3 setup_bot.py --profile nz
```

The installer downloads the checksum-pinned [NZ data release](https://github.com/SquishywareOfficial/MeshCore-RoomBot/releases/tag/nz-data-20261011-v2) automatically. To reuse a downloaded archive or transfer it to another host, supply it explicitly:

```sh
python3 setup_bot.py --profile nz --bundle-file /path/to/nz-20261011-v2.zip
```

An HTTPS mirror can be used with `--bundle-url https://...`; it must match the
shipped `installer/nz-offline-data.json` checksum/size. The manifest is tied to
the prepared data version, never an unchecked `latest` download. Copying the
archive to an SD card avoids downloading it again on each installation. Python
packages and the Routino source still need Internet/cache access; this is not
yet a complete air-gapped package-wheel distribution.

The NZ archive is **361,927,085 bytes (about 345 MiB)**. It supplies roughly
704 MiB of uncompressed map data: national pedestrian routing files, the street
index and on-demand water geometry index, with licenses/source notices.
Allow around **2.5 GiB free** for archive, staging, environment and installed
data. Setup checks free space. Downloaded complete files are cached and
verified before reuse. Downloads retry up to three times; incomplete/corrupt
files are not activated. An interrupted setup can be rerun after confirming
the previous process is no longer running and removing its stale `.setup.lock`
if present. Data schemas, file hashes, engine version and pedestrian profile
are validated before new configuration is made active.

The C build defaults to one compile job for the Pi Zero; `--jobs 2` or more is
available on a capable host. Matching unmodified Routino 3.4.4 source is
downloaded and retained locally, along with its license. Setup does not run
`sudo`, modify system Python, install OS packages or configure Wi-Fi/NTP.

## Basic installation: Windows, Linux or Pi

```sh
python setup_bot.py --profile basic
```

Basic setup also installs geographic bearings/place/towns, reference guides,
calculators, tides/daylight and community handlers. It imports the supplied
LINZ settlement points, two Get Ready references, original-XIAO guides, Raglan
tides and Hamilton–Raglan saved routes. It does **not** install national
walking/street/water indexes; those require the NZ profile.

Full NZ native routing on Windows requires running setup in Linux/WSL. The
temporary Windows-COM-to-WSL bridge used for development is not installed as
a production Windows service by this script. Basic setup runs natively on
Windows. Both profiles bootstrap a checksum-pinned pip wheel in the isolated
environment even when the host lacks `ensurepip`/global pip, then use the
project requirement files (including offline WMM2025, and optional Astral/
Shapely as selected). No raw preparation dependency such as pyosmium is needed.

All profiles use the supplied snapshot data. Tide coverage remains Raglan
2026 plus the first three days of 2027; the installer does not imply full 2027
coverage, fresh weather/news/alerts or Wikipedia answers. Missing planned
features are not enabled.

## Start explicitly

Default installation directory: `~/.meshcore-room-bot`. Override with `--root`.

```sh
python3 ~/.meshcore-room-bot/run_bot.py --port /dev/ttyACM0
```

Windows example:

```powershell
python "$env:USERPROFILE\.meshcore-room-bot\run_bot.py" --port COM26
```

Use the device's actual serial port. After checking host date/time, add
`--clock-trusted` for compass bearings, tides and daylight. Setup never persists
clock trust or silently replays the archive. A new cursor starts at archive end
as before; an existing installation uses the same `state.sqlite3`.

Stop the bot before rerunning setup against that installation. Reruns reuse
verified downloads and completed native builds. Versioned app/data snapshots
avoid overwriting current datasets. A pre-existing `config.json` is **not**
overwritten: a `config.*.proposed.json` is written for review/merging instead.
Keep the existing delivery state when upgrading. If you adopt a proposed config
with a different `data_dir`, existing community records remain in the old
directory; migrate their SQLite database with the bot stopped before switching.
The installer does not guess which private data to migrate. No private records/settings
from the machine that prepared the bundle are included in it.

## Prepare/publish a new data version

The existing graph/street/water builders remain separate desktop preparation
tools. See [routing](nz_routing.md), [streets](origins.md) and [water](water.md).
After building and verifying the three snapshots from the same OSM extract:

```sh
python3 tools/package_offline_data.py \
  --graph /path/to/completed-graph \
  --streets /path/to/streets.sqlite3 \
  --water /path/to/water.sqlite3 \
  --router /path/to/matching/router-slim \
  --output /path/to/new-distribution-folder \
  --id nz-NEW-DATE-vN
```

This whitelists map assets, verifies common source hashes/graph profile/version,
keeps attribution/licenses and removes local build paths from exported metadata.
It excludes all private SQLite state, routing caches, config, TODOs, logs,
credentials and preprocessing intermediates. Publish the ZIP as a versioned
release asset, set its HTTPS URL in the generated manifest, then copy that
manifest to `installer/nz-offline-data.json` in the bot checkout. Preserve the
data license notices and original Routino source availability.

Commit the small manifest/installer/docs; keep the large ZIP as a release asset,
not a Git blob. Never silently replace a previously published data version.
