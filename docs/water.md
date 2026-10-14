# Offline mapped-path approaches to water

`/bot location water ORIGIN [walk|straight]` calculates approaches **on demand**, using
indexed water/path/barrier geometry. There is no national table of precalculated
approach points. It uses the same saved place, street and WGS84
coordinate resolver as distance/bearing. Walking is the default. Examples:

```text
/bot location water Hamilton
/bot location water Dinsdale Hamilton
/bot location water Tattersfield Place Hamilton
/bot location water -37.7313255, 175.2215203
/bot location water street:32455893@point
/bot location water Raglan straight
```

Maui Street Hamilton has two mapped components; the command asks you to select
one, just like distance. A unique street uses its approximate reference point
without requiring `@point`; the answer labels that approximation. A suburb plus
city (such as `Dinsdale Hamilton` or `Dinsdale, Hamilton`) resolves the suburb's
point near that city, rather than starting at the city centre. City hints use a
20 km proximity filter, not municipal boundaries. Coordinates are the most
precise starting point. No house-number or intersection lookup is implemented.
A named-place or street reference point is not inferred to be your position.

Every result starts with **Drinking-water safety unknown. Do not assume safe to
drink.** Access, salinity where untagged, water presence today and potability are
separate unknowns. A map feature is not an assurance that water can be collected.
This tool does not recommend a universal treatment method. See the official
[LAWA toxic-algae guidance](https://www.lawa.org.nz/learn/factsheets/can-i-swim-here/toxic-algae)
and [drinking-water notices](https://www.waterservicesauthority.govt.nz/for-the-public/drinking-water-in-an-emergency/drinking-water-notices).

## What distance means

The endpoint is a **mapped pedestrian-path/road point near water**, not a water
centroid or an invented bank access point. Normal replies give that path point,
the distance and the remaining gap to water. `/bot location details` gives the nearby
mapped water boundary/stream centreline and additional search information.
The gap and router endpoint snaps are excluded from the distance. There are no
directions or claimed permission to cross that gap. Bridges, cliffs, private
land and unmapped hazards may still prevent collection.

Walking compares actual native pedestrian routes to a bounded shortlist of
approach points. It says **best tested approach**, never globally nearest water. Default search: 5 km radius around the selected origin, closest
12 computed approach points by straight-line distance. Geometry work has a
5-second budget and a 20,000 path-piece/section scan cap; it reports partial
searches when capped. Routing has a separate 30-second shared budget (including first graph
verification). A failed route is excluded; failed/limited candidates are reported.
No route or no data does not mean that there is no water nearby.

Explicit `straight` mode ranks those same locally computed path approaches by great-circle
distance. It is **not distance to every nearest shoreline point**, bank access,
or walking distance. It needs no installed router. Walking never silently falls
back to straight mode. Normal successful replies fit in two messages, keeping
the warning, distance, approach coordinates and access/gap limitations. Enable
`details` for `/bot location details` (extra search/snap information) and `source` for
`/bot source water` (dataset provenance). These optional replies are sent to the
shared room and may use `/bot next` for paging. Normal replies contain no source
URLs or internal origin metadata.

Water route endpoints have a maximum 10 m snap, reduced if the configured precise
or general routing snap bound is smaller. This applies to native and cached
results. Origins use the existing place/street/coordinate snap policy.

## Data preparation and coverage

Preparation uses the same pinned national Geofabrik OpenStreetMap PBF as the
pedestrian graph. It includes typed `natural=water` lake/river polygons, old
riverbank areas, and stream centrelines. Pyosmium assembles closed ways and
multipolygon relations, including islands/holes. Geometry is retained in the
index; no name point or lake centroid is substituted. River centrelines without
bank polygons, unspecified water areas, ponds, reservoirs and artificial drains
are deliberately outside this first dataset's scope.

Known tidal, saline/brackish, wastewater/industrial and restricted features are
excluded. Untagged salinity remains unknown; this cannot identify every coastal
or polluted feature. Intermittent status is reported as yes/no/unknown.

Eligible paths exclude explicit access/foot restrictions, conditional permission,
bridges, tunnels, fords and motorroads. Source path segments are split into
sections at most 50 m long during a local query. The query pairs points on those sections with
actual nearby boundary/line geometry with a 2..40 m gap. Points are deduplicated
in approximate 30 m cells per path/feature. Source segments intersecting eligible
water are excluded, as are candidates within 40 m of mapped cliffs, barriers,
bridges, tunnels or fords. These conservative exclusions still do not certify
safe/legal bank access; missing map tags are not positive access evidence.

Source tagging references:
[waterways](https://wiki.openstreetmap.org/wiki/Tag:waterway%3Driver),
[water polygons](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dwater),
[tidal status](https://wiki.openstreetmap.org/wiki/Key:tidal),
[access](https://wiki.openstreetmap.org/wiki/Key:access).

Preparation only groups original vertices into indexed pieces of at most 64
vertices, preserving connected ends and the full source water geometry. It does
not calculate every path/water pair. Runtime keeps at most eight full water
geometries (each at most 2 MiB WKB). Oversized pieces and dense per-section
water/barrier searches are rejected. Oversized full features are reported as
skipped, so no complete/global-nearest claim is made. Successful walking routes
use the existing bounded persistent route cache.

Prepare on a desktop/Linux host with pyosmium and Shapely installed (for example,
`python3 -m pip install -r requirements-water-prep.txt`). Those are
**pyosmium is preparation-only**. Runtime water queries need Shapely and NumPy,
standard-library SQLite, and the already installed Routino router for walking.
Install the optional runtime dependency on the bot host:

```sh
python -m pip install -r requirements-water.txt
```

Geometry dependencies load only when water lookup is requested. Other bot
commands do not require them; missing dependencies return unavailable.

```sh
python3 tools/build_water_index.py \
  --input /path/to/new-zealand-261011.osm.pbf \
  --output /path/to/nz-water-20261011-v2.sqlite3 \
  --source-url https://download.geofabrik.de/australia-oceania/new-zealand-261011.osm.pbf \
  --revision "Geofabrik NZ 2026-10-11 water-geometry-v2"
```

The output stages under `.building` and becomes the final file only after
preparation/SQLite checks complete. Existing outputs are refused. A completed
extraction checkpoint permits explicit `--resume-extracted` with the same input
SHA256, URL, revision and output name, so indexing can restart without reading
the PBF again. Journaling stays enabled. Interrupted builds remain unactivated;
extraction interruptions require a fresh output name.
Record the output SHA256 and verify it after transferring. Stop the bot before
replacing a dataset: runtime opens immutable read-only snapshots.

Install the verified file as `water.sqlite3` beside your `offline.sqlite3`.
Keep graph and streets installations from [routing](nz_routing.md) and
[origins](origins.md) when needed. Add `water` to `enabled_commands`; defaults
remain ping/help/about. No data import, service activation or room messages
happen merely by checking configuration.

Optional config field (all values are validated):

```json
"water": {
  "radius_metres": 5000,
  "candidate_limit": 12,
  "scan_limit": 20000,
  "route_budget_seconds": 30,
  "geometry_budget_seconds": 5
}
```

Limits: radius 100..20,000 m, candidates 1..50, spatial scan 100..50,000,
route budget 1..90 seconds, geometry budget 0.1..15 seconds. Increasing them increases work and can delay other
bot requests. Failed storage/schema/source metadata/oversized dense searches
return unavailable rather than guessing or silently ignoring candidates.

The index includes OpenStreetMap geometry and must retain source/revision/hash
metadata and ODbL attribution when shared. Python code remains MIT. Neither the
OSM dataset nor this command provides live closures, quality monitoring or proof
of complete national water/access coverage.

## Verification status

Windows: 124 tests pass, including geometry indexing, on-demand calculation, crossing/barrier
exclusions, route-versus-straight ranking, bounded search/time, strict endpoint
snap, missing/invalid data and offline byte/paging checks.
National geometry index contains 94,599 water features, 698,576 path pieces and
293,984 barrier pieces (407,425,024 bytes). SHA256:
`3667cf3142f9b507dea56bf3cfd4e046eef00f4c7e3c2d562851d3c1aa61c97d`.

Ten real dispatcher queries passed on the Pi Zero 2 W, including both main
islands, a Chatham lookup, street ambiguity and missing coverage. Sample timings:
Hamilton 15.54 s including initial graph verification; supplied Maui-area
coordinates 6.32 s; selected Maui component 6.58 s; Christchurch 5.35 s;
Dunedin 7.61 s; Chatham straight-line lookup 0.72 s. Python peak RSS was
50,404 KiB (49.2 MiB). Recorded child-process peak was also 50,404 KiB;
that includes process launch and is not a replacement for the previously
measured larger native long-route footprint.

Limited geometry searches were explicitly reported. Wellington's sampled
query had no candidate before the limit, so no water-absence claim was made.
Christchurch's desktop/Pi result differed slightly (0.25 vs 0.28 km) because
the time budget can produce different candidate subsets. This is another reason
the result is labelled shortest tested, not the global nearest source.

Verified data and code are in the isolated Pi validation installation. Original
bot deployment/state and room firmware are untouched. Live Pi USB/phone
verification remains separate and has not been performed for this feature.
