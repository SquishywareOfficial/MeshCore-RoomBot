# Shared coordinate and street origins

Implemented for `/bot location place`, `/bot location distance`, `/bot location bearing`,
`/bot location towns` and `/bot location water`. They share
`room_bot.locations.resolve_location` for the same input forms.
No new commands are enabled by default.

## Input forms

```text
/bot location place Hamilton
/bot location place -37.7313255, 175.2215203
/bot location distance -37.7313255,175.2215203 to Raglan
/bot location place Maui Street Hamilton
/bot location place Maui St, Hamilton
/bot location distance street:32455893@point to Raglan
/bot location distance -37.7313255,175.2215203 to street:32455893@point
/bot location distance -37.7313255,175.2215203 to Hamilton straight
/bot location bearing -37.7313255,175.2215203 to street:32455893@point
/bot location towns -37.7313255,175.2215203
```

Coordinates use decimal WGS84 **latitude first, longitude second**. Comma spaces
are optional. Invalid bounds, nonfinite values and exponents are rejected.
Coordinates specify the input point, not proof of GPS/map accuracy. Geographic
queries still require the offline content database/place catalogue setup.

The street index contains **92,447 connected named road/path components** derived
from **232,454 named OSM ways** in the NZ 2026-10-11 extract. It does not contain
house numbers or an intersection/address geocoder. Roads with the same name are
merged only when they share actual OSM nodes; separate components remain separate.
Crossing lines without a shared node are not joined. Name matching ignores case
and macrons and expands common suffixes such as `St`/`Street` and `Rd`/`Road`.

A locality hint (`Maui Street Hamilton` or `Maui Street, Hamilton`) filters to
geometries within **20 km** of that locality's representative point. It is a
proximity hint, not a city/suburb boundary or an address assertion. Ambiguous
localities/components require an explicit choice; no nearest street is silently
selected. A very common national name exceeding the 2,000-candidate bound asks
for a stable street ID instead of guessing from a truncated list.

Maui Street has two mapped disconnected components in this snapshot:
`street:32424537` and `street:32455893`. The bot lists both with coordinates.
Choose the relevant component or give your actual GPS coordinates. IDs are tied
to the source snapshot's lowest way ID in each component; refreshes may change
component membership. No permanent street-address identity is promised.

A street is not one position. For a route, explicitly append **`@point`** to use
its labelled approximate representative point, for example
`/bot location distance Maui Street Hamilton @point to Raglan` when the street match is
unique. Ambiguity still requires a listed ID. `/bot location place STREET` may preview
the representative coordinates without treating it as your position.
`/bot location towns STREET` and `/bot location water STREET` also permit this
point and label it explicitly. `Dinsdale Hamilton` or `Dinsdale, Hamilton`
resolves the suburb point near Hamilton; it does not fall back to Hamilton
centre. City hints filter places within 20 km, not municipal boundaries.
Optional street length/precision information is available with `/bot location details`.
The representative is on the mapped geometry at half the aggregate length of
source ways sorted by ID; branching roads do not have a unique midpoint. This
is a selected approximation, not geocoding a user's address. Use actual GPS
coordinates for a precise origin.

Bearing defaults to magnetic compass using offline WMM2025; `true` and `both`
options are described in [the bearing guide](magnetic_bearings.md). The magnetic
correction is evaluated at the selected origin and needs a confirmed date.

Town-to-town behaviour remains compatible. Straight-line queries remain clearly
labelled and road mode still only uses its separately imported saved routes.
The implemented natural-water lookup uses these origins; this change
does not implement rivers/lakes, safe drinking-water data or radio frequencies.

## Install the optional street index

Runtime uses Python's SQLite/stdlib, with **no new Pi pip dependency**. Place
`streets.sqlite3` beside `offline.sqlite3` in the configured `data_dir`. Verify
the supplied index SHA256 before copying; it is read-only at lookup time:

```text
461196aa95a74dfce645d634f489a92e89b2d0b3d39fd367f6ca7141f876aa8b
```

The uncompressed database is **191,762,432 bytes (183 MiB)**. It is separate from
delivery state, community records and the 132 MiB routing graph. Existing place
queries continue to work without the street index; coordinate parsing also works
without that optional index. Unknown streets remain unavailable.

Prepare on a capable Linux host using the already downloaded OSM PBF:

```sh
sudo apt-get install python3-pyosmium
python3 tools/build_street_index.py \
  --input /path/to/new-zealand-261011.osm.pbf \
  --output /path/to/nz-streets-20261011.sqlite3 \
  --source-url https://download.geofabrik.de/australia-oceania/new-zealand-261011.osm.pbf \
  --revision geofabrik-nz-2026-10-11
```

Preparation used pyosmium 3.7.0, 1m54.57s and about 1.07 GiB peak RAM on WSL;
this is not a Pi preprocessing estimate. The tool refuses existing output/staging
files and only renames the completed database into place after source processing,
component construction and metadata commits. Failed staging files are not usable
indexes. Refresh into a new versioned file, verify it, then replace the runtime
index between bot sessions. The source SHA256/revision and ODbL attribution are
stored inside the database. Source PBF hash matches the routing snapshot.

This index is OpenStreetMap-derived data under ODbL 1.0, separate from MIT Python
code; preserve attribution and database redistribution obligations. No upstream
LINZ road API token is required. Generic bot code archives exclude SQLite files.

## Origin attachment and cache behaviour

`routing.precise_snap_metres` defaults to **100 m** for coordinates and selected
street points. It is also bounded by `routing.max_snap_metres` (default 1,000 m
for named-place representatives). Each endpoint's limit is checked independently,
on both computed and cached results. A point too far from a usable walking path
returns unavailable rather than quietly moving the route a kilometre away.

Replies report the actual snap gaps, which are not included in the walking
distance and are not instructions for a safe/legal off-path connection. The
graph cannot prove the user can reach a path across a wall, cliff or private land.
Changing coordinates, snapshot or effective snap limits changes the cache key.

## Verification

```sh
python -m unittest discover -s tests -v
python3 tools/benchmark_origins.py \
  --streets /path/to/nz-streets-20261011.sqlite3 \
  --graph /path/to/nz-20261011-foot-v1-complete \
  --executable /path/to/routino-3.4.4/src/router-slim \
  --output /path/to/origins-results.json
```

Desktop: **105 tests pass**, plus actual native street/coordinate routes and
seven dispatcher scenarios with the national index, no network or USB access.
The selected Maui Street component to Raglan gives about **45.7 km**, and the
user's example coordinate to that component about **0.9 km**, snapped **26 m / 0 m**.
These are mapped estimates with the existing access/safety limitations.
Pi actual dispatcher/native graph checks pass for all seven scenarios, with
**31,848 KiB (31.1 MiB)** Python peak RSS in these short-route tests. The previous
national long/disconnected-route measurements still apply; this lower short-route
result does not replace that worst-case measurement. All **105 tests pass on
Windows and Pi** (Pi 15.051s), and final config validation passes. No USB/room posts were sent;
live phone delivery remains unverified. See [national routing setup](nz_routing.md).

The source index is available locally in ignored
`routing-snapshots/streets/nz-streets-20261011.sqlite3`, and on Pi at
`/home/sysop/meshcore-routing-lab/nz-streets-20261011.sqlite3`. The isolated
validation installation's `routing-validation-data/streets.sqlite3` points to
that verified snapshot. The original Pi bot/state are unchanged; no service starts
merely from installing the index. Relative documentation links remain source-local.
