# Offline bot data and services plan

Recorded: 13 October 2026, Pacific/Auckland.

Status: IMPLEMENTATION IN PROGRESS. Optional offline components are implemented
locally; see [current setup and coverage](offline_usage.md). The local ignored
TODO_OfflineData.md is the detailed execution/progress handoff. The table below
includes future online/cache extensions as well as implemented offline commands.

## Purpose and scope

Provide useful information through the MeshCore room when the bot host has no
internet connection. The mesh, room server, and bot host must still be operating.
The target host is a Raspberry Pi Zero 2 W connected directly to the room by USB.
The Python bot is now separate from the firmware in MeshCore-RoomBot.

Wikipedia retrieval and tiny-model experiments remain separate work. These
features should function without an LLM. Use structured lookups, local search,
and deterministic calculations rather than generated emergency instructions.

The user initially requested recording these ideas, then authorized local
implementation on 13 October 2026. Live deployment/enablement is separate.
Online weather/forecast services, NZ-wide weather alerts, news and GeoNet quake information remain separate additions. Offline work must not discard those requirements.

## Feature priorities

Suggested first group: community noticeboard, emergency reference, local
directory, and offline tide tables. Exact implementation order is not yet chosen.

| Feature | Proposed commands | Offline behaviour |
| --- | --- | --- |
| Community noticeboard | `/bot notice add ...`, `/bot notice list`, `/bot notice show ID` | Store and retrieve member reports such as blocked roads, charging availability, or requests for supplies. |
| Emergency reference | `/bot emergency TOPIC`, `/bot emergency next` | Retrieve short source-labelled passages from saved official NZ guidance. |
| Local directory | `/bot directory TOPIC` | Look up preconfigured contacts, radio frequencies, agreed meeting points, and local mesh instructions. |
| Tide predictions | `/bot location tides COASTAL_LOCATION` | Look up downloaded annual LINZ predictions for the requested supported location/date. |
| Voluntary check-ins | `/bot checkin [TEXT]`, `/bot lastseen USER` | Record explicit user check-ins and retrieve the most recent one. |
| Offline place and distance data | `/bot location distance FROM to TO [walk|road|straight]`, `/bot location place NAME`, `/bot location towns LOCATION` | Look up saved route distances or calculate explicitly labelled straight-line distances; retrieve coordinates and nearby saved places. |
| Daylight calculations | `/bot location sun [LOCATION]` | Compute sunrise/sunset locally for a known location and valid date. |
| Practical references | `/bot guide TOPIC` | Retrieve MeshCore commands, wiring guides, or selected equipment instructions. |
| Calculators | `/bot calc ...` | Run a small explicitly supported set of unit, battery/solar, or coordinate calculations. |
| Saved online snapshots | Existing planned weather/forecast/news/quake commands | Optionally show last successfully downloaded information with issue/fetch times and freshness status. |

These are separate from fresh internet-backed information. Without a new source
of information, saved data cannot report new warnings, news, or earthquakes.
Community reports and locally attached sensors are possible new offline inputs,
but neither should be presented as official warnings or regional weather data.

## Source material and data rules

### Emergency reference

Start with official NZ Civil Defence / Get Ready guidance for earthquakes,
flooding, loss of power, and emergency supplies. Store the source text, title,
URL, revision date when supplied, retrieval date, and topic identifiers.

Source: https://getready.govt.nz/prepared/household/impacts/no-phone-or-internet

Build a reviewed text collection from appropriate downloadable material. Do not
assume that a saved web page or PDF is already searchable, or that arbitrary
machine extraction preserves its instructions. Preserve meaning, sequence,
qualifiers, and attribution when preparing short passages. Verify reuse terms
before distributing a bundled collection. Medical/first-aid material, if added
later, must come from a separately selected authoritative source.

Search can initially use topic keywords and a small SQLite full-text index.
Give a short matching passage and offer paging. Say when no relevant topic is
available; do not invent an answer. Explicitly label this as reference guidance,
not a live emergency instruction from authorities.

### Local directory

Maintain an operator-reviewed directory rather than scraping live contacts on
every request. Store category, location, description, source, and review date.
Radio frequencies must be checked for the intended locality before inclusion.
Meeting points are agreed local plans. A saved building address must not be
described as an open evacuation centre unless current activation is confirmed.
Keep publication of personal contact information an explicit operator choice.

Get Ready recommends retaining important numbers, meeting plans, and knowledge
of emergency radio stations; the directory is our proposed implementation.

#### Broadcast radio frequency directory (PLANNED)

User requested this follow-on on 13 October 2026. Proposed `/bot radio` defaults
to Hamilton; `/bot radio PLACE` selects another NZ area, and
`/bot radio PLACE emergency` filters to broadcasters explicitly identified by
current local Civil Defence/emergency-management guidance. These commands are
not registered yet. Existing directory lookup can serve reviewed radio records,
but no national station snapshot is currently bundled.

Store an offline NZ-wide table of broadcaster/station, area served, frequency,
explicit band/unit (AM kHz versus FM MHz), source URL, retrieval/review date,
transmitter coordinates where available, and evidence for emergency designation.
Source current broadcaster frequency guides and local Civil Defence directories;
do not generate numbers from memory or assume one network frequency is national.
RNZ's official frequency guide is an initial source candidate:
https://www.rnz.co.nz/assets/cms_uploads/000/000/045/Radio_NZ_Frequency_List.pdf
Local guidance example:
https://www.getprepared.nz/get-ready/home-ready/stay-informed

Refresh explicitly while internet is available. Every lookup remains offline
and identifies its source/review date. Distinguish recorded transmitter/frequency
information from guaranteed reception, station uptime or an actual live warning.
Distance to the nearest transmitter alone cannot determine best reception,
especially in valleys. Broadcast AM/FM is the initial scope; amateur repeaters,
UHF CB, maritime and other frequency categories need separately sourced tables
and clear category/mode distinctions, rather than being mixed into emergency
broadcast results. Preserve existing room reply limits and pagination.

### Tide predictions

Use official downloadable LINZ annual CSV predictions where available:

- https://www.linz.govt.nz/products-services/tides-and-tidal-streams/tide-predictions/tide-predictions-list-view
- https://www.linz.govt.nz/guidance/marine-information/tide-prediction-guidance/tide-prediction-formats

Require a coastal location every time; there is no Hamilton or Raglan default.
Download and validate annual files before relying on them offline. Store source
year, supported location, source URL, datum, and source time conventions.
Handle adjacent dates and days with three tide events. Preserve NZ daylight
saving conventions without applying DST twice. Missing years/locations are an
explicit unavailable result, not a request to substitute a nearby harbour.
Secondary-port offsets require their own deliberate implementation.

Return compact next high/low events with location and local date/time. These are
astronomical predictions, not observations or storm-surge/tsunami warnings.
Actual CSV import and formatting have not been implemented or validated.

### Community noticeboard and check-ins

Keep these records in a host-side database separate from the room archive and
the bot's existing cursor/outbox schema. Identify authors by their full public
key; do not rely on a phone display name that the USB post interface does not
provide. Any friendly aliases need an explicit mapping and ambiguity handling.

Notices need an ID, author, text, received time or explicit unknown-time marker,
category, expiry policy, and active/withdrawn state. A report must retain its
community origin. Provide bounded listing, detail, and a way for its author or
an explicitly authorized operator to withdraw it. Set storage limits before
implementation; do not create an unbounded log.

Check-ins are voluntary statements. Display "last checked in", not "is safe".
Do not treat every message, an advertisement, or bot output as a check-in.
Decide whether optional check-in text is allowed and how much is retained.
Shared room replies are visible to room members; there is no private DM output
in the current bot interface.

Administrative authorization cannot be inferred from command text, a name, or
an alleged role: current USB posts do not contain a room admin-role flag. Use
trusted local administration or an explicitly configured public-key policy.
Make event processing idempotent by room identity plus input sequence so a
retry after a crash cannot duplicate a notice or check-in.

### Offline geography, distances, and walking information

User requirement: provide text answers about geography without transmitting a
map. Example: how far Hamilton is from Raglan if someone might need to walk.
Required coverage, clarified on 13 October 2026: walking distances must support
places throughout New Zealand offline. The Hamilton-Raglan city-to-city table
is a test subset, not completion of this requirement. Place-coordinate coverage
alone does not establish walking-route coverage.

Keep three different distance types explicit:

- Straight-line: calculate from saved coordinates, using a tested geodesic or
  great-circle calculation. It crosses terrain and water; it is not a route.
- Road: a saved driving/road route distance. It does not establish pedestrian
  access and must not be relabelled as a walking distance.
- Walking: a reviewed saved pedestrian route distance, or later a distance
  computed from a local graph with a pedestrian access profile.

Proposed command: `/bot location distance Hamilton to Raglan [walk|road|straight]`.
Walking is the proposed default because this feature was requested for walking
planning. If no walking entry exists, report that absence and offer an explicitly
labelled straight-line result; never silently substitute road or straight-line
kilometres. These command details are proposals to settle before implementation.

Keep the implemented NZ place catalogue and sample route table. Add a locally
prepared national pedestrian routing graph so arbitrary supported town pairs
can be calculated offline, without downloading millions of pairwise routes.
Preprocess on a more capable machine where practical, and benchmark the actual
graph and routing process on the Pi Zero 2 W. Do not assume that the graph fits
the existing small SQLite content database or that desktop timings apply to Pi.
Runtime must need no online geocoding, routing service, map renderer, or LLM.

The LINZ Gazetteer / NZGB place-name dataset is a source candidate for names,
feature types, locations, and region disambiguation, not a source of walking
route distances:

- https://www.linz.govt.nz/guidance/place-naming/how-use-new-zealand-gazetteer
- https://data.linz.govt.nz/layer/51681-nz-place-names-nzgb/

Store stable place IDs, canonical names, aliases, region, feature type, WGS84
coordinates, provenance, and data revision. Preserve macrons in output while
allowing case/macron-insensitive lookup. Parse multiword names around ` to `
without guessing token boundaries. Ask for disambiguation for multiple matches.
Define the representative town endpoint, such as town centre, and label it;
centre-to-centre distance is not a distance from the user's actual home.

Store route rows keyed by origin ID, destination ID, travel mode, and dataset
revision. Include distance in metres, route description or major waypoints,
endpoint definitions, source/profile, and date. A sparse table of supported
pairs is sufficient initially. Straight-line distances can be calculated on
request instead of storing every pair. Do not assume a directed route has an
identical reverse route; store reverse results explicitly unless verified.
Missing pairs, disconnected paths, and unavailable modes remain unavailable.

Required next stage: preprocess an OpenStreetMap pedestrian graph for local
routing. Inspect foot/access permissions, barriers, bridges, motorways, ferries,
and route continuity rather than using a driving profile. Source design notes:
https://wiki.openstreetmap.org/wiki/Guidelines_for_pedestrian_navigation
https://wiki.openstreetmap.org/wiki/Key:foot
Verify current attribution/license obligations before bundling or redistributing
data. Full NZ graph routing is now implemented with Routino and benchmarked on
the Zero 2 W; see the measured results and remaining mapping limits below.
Geofabrik supplies a national OSM PBF extract:
https://download.geofabrik.de/australia-oceania/new-zealand.html
Routino is the initial engine candidate to evaluate, not an implemented backend:
https://www.routino.org/documentation/usage.html
https://www.routino.org/documentation/configuration.html
https://www.routino.org/benchmarks/benchmark-slim.html
Its pedestrian profile and disk-backed slim variant have now been implemented
and measured on our Pi with the national extract. See [routing setup and actual
results](nz_routing.md). The old benchmarks were not used as Pi proof.
Nationwide coverage means supported NZ endpoints can be queried. It cannot
guarantee every pair has a mapped legal pedestrian path. A walking-only query
must not quietly insert a Cook Strait ferry or connect disconnected islands.
A snapshot route cannot confirm current closures, flood damage, bridge integrity,
or safety. Distinguish community closure reports from verified route data.

Other useful text lookups to consider:

- `/bot location place NAME`: coordinates, region, and feature type.
- `/bot location bearing [compass|true|both] FROM to TO`: initial magnetic compass
  bearing by default, with explicit true/both options. Offline WMM2025 requires
  a confirmed date; see magnetic_bearings.md. This is not route directions.
- `/bot location towns LOCATION`: closest saved towns/localities by straight-line
  distance. Future hospitals/meeting-point/facility searches need separate
  commands and reviewed data. A mapped location does not establish service or
  supply availability; water features do not establish drinking-water safety.
- `/bot location walktime KM [PACE]`: a simple elapsed-time estimate at a stated pace,
  excluding rests, terrain, weather, and access restrictions. Do not claim an
  arrival time when the clock is unknown or a route is unavailable.
- Later, selected stored route summaries: major waypoints, terrain/ascent if
  supported by a separately validated dataset, and references to relevant notices.

Validate representative known routes, precise endpoint matching, walk versus
road versus straight modes, unknown pairs, ambiguous/macron/multiword names,
coordinate units, reverse routes, disconnected graphs, and offline operation.
Measure imported database size and Pi query memory/latency. Keep replies within
existing byte/paging limits, and do not include an invented numerical distance
in examples before a real source has been imported and verified.

### Nearest mapped inland water (IMPLEMENTED and verified on Pi)

User request, 13 October 2026: locate nearby rivers/lakes as possible water sources,
preferably by shortest walking distance, explicitly without knowing whether the
water is drinkable. Record this as required follow-on Step 13 in TODO_OfflineData.md.

Implemented command: `/bot location water ORIGIN [walk|straight]`, default walking. Origins:
named settlement, named street plus locality, or decimal WGS84 latitude/longitude.
User examples: `/bot location water Maui Street Hamilton` and
`/bot location water -37.7313255, 175.2215203`. Require an origin;
the USB room post does not contain the sender's GPS position. Do not imply that
the configured Hamilton default or a town centre is the user's current position.
Keep a separately sourced future drinking-water-tap lookup distinct from natural
water features. The registered command remains disabled by default. It calculates
approaches on demand from an indexed national geometry snapshot rather than
precalculating every possible point; see [water setup and limits](water.md).

The shared origin resolver is now implemented for place/distance/bearing/towns/water:
decimal WGS84 coordinates and an optional national OSM street-component index.
See [origin inputs and implementation limits](origins.md). Water approach code
now reuses this resolver; national-data/Pi validation is in progress. See
[the water command, scope and installation](water.md). House-number
address and intersection lookup remain unimplemented. Coordinate input is finite
and latitude-first, supports comma spacing and validates WGS84 bounds; routing
attachment checks snapshot coverage. Extra decimals do not prove GPS/map accuracy.

The prepared OSM name/geometry index joins connected same-name ways using actual
shared node IDs. A locality hint means within 20 km of a named-place representative
point, not a municipal boundary; ambiguous components require an ID. Street-only
queries specify a line/area, not a user position: routes require explicit @point
to accept a labelled approximate representative point, or actual coordinates.
Do not invent a house number/location. Precise origins now default to a 100 m
attachment bound versus 1 km for town representatives, checked on each endpoint.
If off-path access cannot be established, report a gap rather than pretending
the route begins exactly at the given GPS coordinate.

Use nationwide offline river/stream geometry and lake shore polygons. A named
river's Gazetteer point or a lake centroid is not an appropriate access endpoint.
Start by evaluating these official LINZ Topo50 sources and their actual extent,
fields, current revision and CC BY attribution (including separate island layers
where required); OSM water/access geometry from our pinned PBF is an alternative:

- https://data.linz.govt.nz/layer/50327-nz-river-centrelines-topo-150k/
- https://data.linz.govt.nz/layer/50293-nz-lake-polygons-topo-150k/
- https://github.com/linz/nz-river-names/blob/master/db/docs/source/introduction.rst

The river-name pilot combines rivers, drains and canals; do not import all of
them as freshwater supplies. Exclude known saline/tidal/coastal water and sewage,
wastewater or industrial ponds. Preserve missing classification, salinity and
seasonality as unknown rather than inventing freshwater/perennial status.
Mapping does not establish that water is present today, accessible or safe.

Find mapped bank/shore approaches from the pedestrian graph and valid access
geometry. A bridge over water or a path above a cliff is not proof that water
can be collected there. Never route into a lake, onto a river centreline, through
private land or across an invented last off-path segment. Return approach
coordinates, mapped walking distance, remaining gap and unverified access status.
The implemented first version's straight-line mode measures to the nearest
locally computed mapped-path approach, separately labelled; it does not claim access or
a walking route. Direct nearest-shore geometry distance remains a potential
later extension, and must not be confused with this endpoint's distance.

Ranking must minimize walking distances, not select the closest feature by
straight-line distance and then route only to that one. Specify the search
radius and validated candidate endpoints. Only claim the shortest within that
domain when the algorithm can prove it (e.g. a correct multi-target graph search
or exhaustive candidate enumeration with admissible lower-bound pruning).
If resource limits or a shortlist prevent proof, say "shortest of the tested
mapped approaches", disclose limits, and leave global shortest unclaimed.
Measure candidate extraction/index and multi-target routing on the Zero 2 W.

Every result must prominently state: **Drinking-water safety unknown. Do not
assume safe to drink.** Keep this in the initial reply, not only after `/bot next`.
Do not recommend boiling or an ordinary filter as a universal fix: chemical
contamination and cyanobacterial toxins can remain. Do not turn a recreational
swimming rating or the absence of a warning into drinking-water clearance.
Store quality/status as unknown by default; future warnings need their source,
issue/fetch time, expiry and explicit stale/unknown status when offline.

Safety source checks:
- https://www.lawa.org.nz/learn/factsheets/can-i-swim-here/toxic-algae
- https://www.waterservicesauthority.govt.nz/for-the-public/drinking-water-in-an-emergency/drinking-water-notices

Acceptance tests must include opposite river banks, inaccessible shores, bridges
without bank access, saline/tidal exclusions, unknown/dry seasonal features,
misleading centroids, disconnected paths, route-ranking versus straight-line
ranking, bounded searches/unknown minima and warning retention under paging.
No actual distance example is promised until a real endpoint is imported/tested.

### Daylight, practical guides, and calculators

Use deterministic astronomy calculations with a maintained dependency if needed.
Configure default location separately; Hamilton is the proposed daylight default,
with other locations available from a local catalogue or explicit coordinates.
Do not assume internet geocoding works offline. Use Pacific/Auckland timezone
and DST where appropriate, and distinguish calculated times from observations.

Practical guides should include source/version metadata so wiring instructions
can be matched to the correct board and firmware. Calculators should accept
only documented operations and validated numbers/units. Never execute user text
as Python or shell code. Battery/solar results must state their assumptions;
they are estimates, not measured capacity or current.

### Saved online snapshots

For weather, forecasts, warnings, news, and earthquakes, preserve provider issue
time, retrieval time, validity/expiry where supplied, resolved location, and the
last successful response. Keep failure distinct from a successful empty result.
Define per-provider age limits before implementation. Show stale information
only under an explicit policy and label its age; expired warnings must not be
rebroadcast as current. An outage must not trigger repeated cached announcements.

Fresh-data requirements remain: weather and forecast default to Hamilton and
accept other locations; emergency weather coverage is nationwide; tides require
an explicit coastal location. Surf forecasts need a coastal location and a new
forecast source or clearly labelled saved forecast; tide tables cannot predict surf.

## Integration requirements

Read `room_bot/client.py`, `worker.py`, `__main__.py`, `tests/test_room_bot.py`, and
the README before coding. Preserve the existing USB protocol and durable worker.

- Use explicit `/bot VERB ...` addressing. Ordinary conversation gets no reply.
- One process owns USB. Feature handlers must not independently open the port.
- Publish through the existing durable reply outbox and request IDs. Preserve
  cursor, identity, rollback, retention-gap, and retry/deduplication behaviour.
- Current handlers return None, one string, or at most four strings. Each reply
  including its bot prefix must fit 151 UTF-8 bytes. Paginate longer material
  through explicit requests; do not flood the room with a complete document.
- Preserve reply pacing and add bounded per-author admission limits where needed.
  Rate limits and new database writes must not silently lose incoming requests.
- A slow importer/search/provider must not block USB processing indefinitely.
  Scheduled output requires a deliberate worker/outbox integration; do not add
  background serial sends that bypass transactional delivery.
- Time-dependent functions need a trustworthy date/time. Do not assume the Pi
  retains correct wall-clock time after an offline power loss. Define behaviour
  for unknown or corrected time, and never invent timestamps or tide dates.
- Keep content imports bounded and offline runtime lightweight. Do not load a
  whole Wikipedia archive into RAM or require an LLM for these services.

## Suggested implementation checkpoints

1. IMPLEMENTED BASELINE: Versioned configuration and manual-session clock trust.
   Remaining operator choices: feature order, command syntax, authorization, retention,
   source reuse terms, time-validity rules, and local data directory layout.
2. IMPLEMENTED: Command dispatch and bounded pagination while preserving the
   current sample commands, worker state, and retry semantics.
3. IMPLEMENTED: Source metadata, local directory, and reviewed emergency text
   import/search. Verify that requests make no internet calls offline.
4. IMPLEMENTED: Noticeboard and voluntary check-ins, with bounded persistence,
   author/alias handling, expiry/withdrawal, and restart/idempotency checks.
5. IMPLEMENTED SUBSET: Raglan 2026 and 1-3 January 2027 imported; full 2027
   has an unresolved repeated DST timestamp. Supported annual tide CSVs and test NZ dates/DST, adjacent
   days, missing coverage, and offline/unknown-clock behaviour.
6. IMPLEMENTED: 4,903 LINZ settlements, labelled place/bearing/towns/walking-time
   tools, sample walking/road table and optional nationwide offline walking graph.
   National graph/resource tests pass on Pi; mapped/disconnected/ambiguous cases
   remain explicit. See [routing setup](nz_routing.md); road table remains a subset.
7. IMPLEMENTED: Daylight, selected practical guides, and safe calculators.
8. DEFERRED DEPENDENCY: Add explicit offline fallback for selected online providers after
   their online implementations exist; test expiry, stale results, and outages.
9. PARTIAL / HARDWARE PENDING: Validate on the actual Pi and room: disconnect host internet while
   keeping mesh active; check replies on a phone, restart persistence, paging,
   memory use, and latency. Record measured results separately from expectations.

Update each checkpoint and its validation evidence when work is performed.
The Pi has passed the existing 17 Python bot tests; that does not validate any
offline feature or USB/phone delivery on the Pi. A USB OTG adapter is still
needed for the planned direct room connection as of this document's date.

## Optional later work

Offline maps could be served to nearby phones over a local network, separately
from short mesh replies. DOC describes downloadable LINZ Topo50 maps and the
need to verify offline availability:
https://www.doc.govt.nz/parks-and-recreation/know-before-you-go/maps/
Do not plan to transmit map images or full PDFs through normal room messages.
This is a separate optional local-network service, not part of the first bot
implementation.

For exact software checks, data coverage, provisional retention policy and
remaining Pi USB work, use the execution TODO and offline_usage.md. Do not infer
complete national route/tide coverage from a completed software checkpoint.
