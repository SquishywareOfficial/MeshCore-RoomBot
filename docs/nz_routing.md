# Nationwide offline walking routes

The optional Routino backend calculates walking routes from a local New Zealand
OpenStreetMap graph. It uses the existing LINZ place catalogue to resolve names;
it does not require internet access, an LLM, or precomputed distances for every
town pair. `distance` remains explicitly enabled in the host config.

Status: implemented and verified on 13 October 2026 with the national snapshot
and Pi Zero 2 W. The old sample table still works without this backend, but only
covers Hamilton-Raglan. This optional backend enables national mapped walking
queries; not every pair is guaranteed to have a mapped accessible path.

## Prepare once while internet is available

Preparation is separate from bot lookup. Download the national `.osm.pbf` from
[Geofabrik](https://download.geofabrik.de/australia-oceania/new-zealand.html),
choose a dated snapshot and retain its checksum, original URL/date and ODbL
attribution. Our input is `new-zealand-261011.osm.pbf`:

```text
SHA256 18412e3bf7e6a2a7eb96fa67d159f786f224104c9f8202a07e722c49aa28dde1
```

Build the unmodified [Routino 3.4.4 source](https://www.routino.org/download/routino-3.4.4.tgz)
on Linux. Install a C compiler/make, zlib and bzip2 development packages.
Use the same source version on the preparation computer and Pi:

```sh
sudo apt-get install build-essential libz-dev libbz2-dev time
curl -fLO https://www.routino.org/download/routino-3.4.4.tgz
sha256sum routino-3.4.4.tgz
tar -xzf routino-3.4.4.tgz
make -C routino-3.4.4 -j2
```

Our source archive SHA256:
`a7017ff04313fa4fb53b995de50feda39ea5e7867bb40a3f9c886187af702830`.
Executable names differ from Debian's packaged Routino, which may have a
different version; use the explicit matching executable path below.

From the bot checkout, build a new snapshot directory that does not exist:

```sh
python3 tools/build_routing_graph.py \
  --source /path/to/routino-3.4.4 \
  --input /path/to/new-zealand-261011.osm.pbf \
  --output /path/to/nz-20261011-foot-v1 \
  --source-url https://download.geofabrik.de/australia-oceania/new-zealand-261011.osm.pbf \
  --revision geofabrik-nz-2026-10-11-nz-foot-v1
```

Preprocessing may need much more working disk/RAM than lookup. The builder
preserves pedestrian access rules, excludes conditional access it cannot
evaluate, and removes non-pedestrian modes from the graph. It keeps isolated
components and short segments rather than deleting potentially useful paths.
Straight-path simplification remains Routino's default. Its build log records
the actual command. A failed build has no manifest and cannot be enabled.

The resulting four `.mem` files, XML configuration, copyright/license and
`manifest.json` form one immutable snapshot. Copy the whole completed snapshot
to the Pi; do not copy or enable unfinished intermediate `.tmp` files.
The manifest pins engine version, profile, source revision and per-file SHA256.
Runtime verifies those hashes before first use and refuses version/profile
mismatches or changed files. Desktop and Pi architectures are separately
validated with the real graph; rebuilding natively is needed if incompatible.

## Configure the host bot

Import the supplied LINZ places as described in [offline setup](offline_usage.md).
Merge this block into the existing config, leaving its other fields in place:

```json
"routing": {
  "backend": "routino",
  "graph_dir": "/home/sysop/meshcore-routing-lab/nz-20261011-foot-v1-complete",
  "executable": "/home/sysop/meshcore-routing-lab/routino-3.4.4/src/router-slim",
  "timeout_seconds": 90,
  "max_snap_metres": 1000,
  "output_limit_bytes": 8388608,
  "cache_entries": 1000
}
```

These Pi validation paths are examples, not an installed production service.
An actual matching [validation config](../examples/nz_routing_config.json) is
supplied. It uses a separate `routing-validation-data` directory beside the
checkout, so deliberately importing places with that config does not use the
normal bot's data/state. Edit its paths when installing elsewhere.
Relative graph paths resolve under `data_dir`; executable paths are explicit or
resolved through PATH (use an absolute path for deployments). `~` is supported
in graph paths, not shell-expanded in executable arguments. Old configs retain
`backend: "none"` and their original saved-table behaviour.

Check the config and graph before connecting USB:

```sh
python -m room_bot --config /path/to/config.json --check-config
python -m room_bot.routing --graph /path/to/nz-20261011-foot-v1 \
  --executable /path/to/routino-3.4.4/src/router-slim
```

The second command does checksum/profile/version verification. `--check-config`
only validates config syntax/values; it does not verify or download the graph.
No trusted date is required for geographic lookup.

## Queries and limits

```text
/bot location distance Hamilton to Raglan
/bot location distance Christchurch to Rangiora, Canterbury walk
/bot location distance Dunedin to Mosgiel
/bot location distance Hamilton to Raglan straight
/bot next
```

Walking queries use the national graph when configured, including pairs also
present in the old sample table. Road queries retain their separately labelled
saved-route behaviour; this backend does not claim nationwide driving routes.
Straight-line queries use coordinates without calling the router.

The pedestrian profile minimizes mapped path length with neutral preferences,
excludes motorways and ferries, and respects processed foot/access/barrier tags.
It cannot establish current legal access, closures, safety, terrain difficulty
or complete mapping. Replies say these are mapped estimates. Missing/disconnected
paths, inaccessible endpoints and timeouts return unavailable rather than an
invented distance. For example, a walking-only North-to-South Island query cannot
include a ferry; island-to-island paths are unavailable.

Endpoints are named-place representative points, snapped to the closest usable
path. The default rejects endpoints over 1,000 m away; replies show both snap
gaps, which are not included in the route distance or offered as directions.
There is no street-address geocoder. The settlement catalogue does not include
every NZ geographic feature. Route totals are rounded by Routino to 10 m; bot
answers use 0.1 km. `walktime` remains a separate pace calculation, not a claim
that a route can safely be completed in that time.

The worker handles one route at a time, bounded by the configured timeout/output
limit; a long route delays subsequent requests in that process. All messages
still use its durable outbox and byte/paging limits. Native router output is
spooled to temporary disk, never accumulated without bounds in Python RAM.
Expected failures never expose arbitrary native error output to room members.

Successful results persist in `data_dir/routing-cache.sqlite3`, bounded to the
configured number of entries. Keys include direction, exact resolved endpoint
coordinates/IDs, graph manifest/profile and snap bound. Restart reuses results;
changing snapshots changes the cache namespace. This database is disposable
and separate from delivery state, community records and content imports.

## Refresh and verification

Build/download a new versioned directory during an explicit refresh; verify it,
then repoint config and restart the bot. Keep the prior snapshot until the new
one passes checks. Never replace individual graph files while the bot uses them.
Offline operation uses the stored snapshot, not live OSM/closure information.

```sh
python3 tools/validate_routing_profiles.py --source /path/to/routino-3.4.4
python3 tools/benchmark_routing.py --graph /path/to/nz-20261011-foot-v1 \
  --executable /path/to/routino-3.4.4/src/router-slim \
  --output /path/to/national-routing-results.json
python -m unittest discover -s tests -v
```

The first command builds synthetic graphs and actually tests the native engine,
including access/barriers, ferries/motorways, conditional tags and one-way roads.
The second runs real NZ pairs across both islands, long journeys and disconnected
islands using temporary content/cache databases. Its timings exclude LoRa
delivery, and its child RSS includes the native router (not just Python).
Subprocess starts are cold but OS page cache may already be warm. No power
consumption or radio-delivery performance is implied.

### Measured results, 13 October 2026

National graph: **138,381,382 bytes (132 MiB)**, 3,537,037 retained nodes.
Desktop preprocessing took 3m58.55s with about 1 GiB peak RAM. Routino 3.4.4 built
the graph on x86-64 Linux and successfully read it on the Pi's ARM64 Linux.

| Tested pair | Mapped walking km | Pi uncached time |
| --- | --- | --- |
| Hamilton-Raglan | 44.65 | 0.23 s |
| Auckland-Whangārei | 201.60 | 3.83 s |
| Wellington-Porirua | 19.66 | 0.55 s |
| Christchurch-Rangiora, Canterbury | 28.26 | 0.94 s |
| Dunedin-Mosgiel | 14.38 | 0.32 s |
| Invercargill-Bluff | 26.75 | 0.24 s |
| Hamilton-Wellington | 545.59 | 5.72 s |
| Christchurch-Invercargill | 632.94 | 7.26 s |
| Wellington-Christchurch | No connected walking path; ferries excluded | 19.77 s |

Pi native-router peak RSS **134,020 KiB (131 MiB)**; Python peak **30,600 KiB
(29.9 MiB)**. First snapshot verification took **3.66 s**; subsequent cached
results were a few milliseconds. These tests fit the Pi's measured available
RAM without increasing swap configuration; they are not a guarantee that every
possible route uses the same resources. Timeout/output bounds remain necessary.
Graph storage and process RAM are separate measures.

All **96 Python tests** pass on Windows and Pi with the corrected catalogue.
All **12 real native-engine
profile scenarios** pass on WSL. Real NZ queries additionally check snapped
endpoints, monotonic totals, per-segment geometric gaps with simplification
tolerance, section sums and restart-persistent caches. The Auckland-Whangārei
path was inspected at its 3,078 retained points and named SH16/SH14 corridor;
it was not silently treated as a driving route over Auckland Harbour Bridge.
This does not establish present pedestrian access/safety or field verification.
An additional desktop query on Chatham Island, Waitangi-Te One, returns 5.12 km.
The catalogue lists this Waitangi under historic source area Wellington; use its
full name/area or ID to distinguish it from the Northland Waitangi. An isolated
Stewart Island settlement pair correctly returns no connected mapped route.

The real Pi dispatcher was also exercised with a simulated input post and the
native graph: Dunedin-Mosgiel returns 14.4 km and stays within four 151-byte
prefixed replies, with no USB access or room posts. System swap was not enlarged;
26 MiB was in use after the first tests (8 MiB before routing), so this is not
a claim of zero swap activity. No bot startup service or live delivery was tested.

Local complete snapshot is in ignored `routing-snapshots/nz-20261011-foot-v1-complete`;
raw benchmark/geometry evidence is in ignored `routing-snapshots/evidence`.
The Pi has the snapshot under `/home/sysop/meshcore-routing-lab/` and the tested
code under `/home/sysop/MeshCore-RoomBot-routing-validation-20261013`.
The graph transfer archive SHA256 is
`a86c7909538ea1dc7256512ef7507619c62b544c6eb8c323cc3d62f8a80e3133`.
The original Pi bot installation/state were not replaced. Live USB/phone
delivery still requires the Pi-to-room cable; no bot service was activated.

### Place-coordinate correction

The LINZ service has some rounded `crd_latitude`/`crd_longitude` attributes that
can differ greatly from the feature's precise point geometry. The importer now
requests WGS84 geometry (`returnGeometry=true`, `outSR=4326`) and uses only that,
with validation and no coarse-attribute fallback. All 4,903 supplied points were
refreshed; 324 differed from the previous attributes by more than 100 m.
Mosgiel's geometry is near -45.875435,170.347640, whereas the old attributes were
-45,170. The corrected route is 14.38 km; older 144.42 km benchmark evidence is
obsolete. Re-import the updated place dataset if you installed the old catalogue:

```sh
python -m room_bot.data --config /path/to/config.json import datasets/places/linz-settlements-2026-10-13.json
```

This replaces that content dataset transactionally, not delivery/history or
community state. Routing cache keys include coordinates so corrected endpoints
do not reuse older routes. Old data and a correction audit remain in the local
ignored evidence folder, not the distributed import directory.

## Data and software licenses

Graph data is derived from OpenStreetMap contributors under ODbL 1.0; preserve
[OSM attribution](https://www.openstreetmap.org/copyright) and applicable
database redistribution obligations. LINZ endpoint coordinates retain their
separate CC BY 4.0 source attribution. Routino and its derived profiles/tagging
XML are AGPL-3.0-or-later; the snapshot contains source XML, original notices and
`COPYING-Routino`. The Python bot/adapter remains separately MIT licensed and
invokes the native program as a subprocess. No graph binary or upstream engine
is silently included in the ordinary bot source/validation archive.
