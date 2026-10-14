# Offline bot setup and current coverage

Implemented locally on 13 October 2026. Enablement, content import, and a running
USB-connected bot are separate operations. Nothing here starts a service or
modifies the MCU's firmware/settings. Weather/news/quakes and Wikipedia/LLM
answers remain separate future work; no stale-provider fallback exists yet.

## Install and configure

For a repeatable new-machine installation, use [setup_bot.py](setup.md). The
following manual steps remain available for custom installations and data refresh.

From the standalone root, use your virtual environment:

```sh
python -m pip install -r requirements.txt
python -m room_bot --init-config ~/.meshcore-room-bot/config.json
```

For optional sunrise/sunset calculations:

```sh
python -m pip install -r requirements-astronomy.txt
```

Edit that config's `enabled_commands` to select the implemented commands below.
Ping/help/about remain the only defaults. A disabled command gives no supported
service; listing a future command name in config does not implement it.

```json
["ping", "help", "about", "directory", "emergency", "guide", "place", "distance",
 "bearing", "towns", "walktime", "water", "tides", "notice", "checkin", "lastseen", "sun", "calc"]
```

The list is an example to copy into the config field, not a complete config file.
Validate without USB:

```sh
python -m room_bot --config ~/.meshcore-room-bot/config.json --check-config
```

## Import selected data

Use the same `--config` for imports and runtime if you change `data_dir`.
These paths refer to the supplied initial snapshot; refresh is explicit.

```sh
python -m room_bot.data import datasets/references/getready-earthquakes.json
python -m room_bot.data import datasets/references/getready-floods.json
python -m room_bot.data import datasets/places/linz-settlements-2026-10-13.json
python -m room_bot.data import datasets/routes/hamilton-raglan-2026-10-13.json
python -m room_bot.data import datasets/tides/raglan-2026.json
python -m room_bot.data import datasets/tides/raglan-2027-jan-prefix.json
python -m room_bot.data import datasets/guides/xiao-enhanced-sq7.json
python -m room_bot.data list
```

Do not import raw CSV, route evidence, or synthetic test fixtures. See
[import schemas and source attribution](data_imports.md). The initial place
catalogue contains 4,903 LINZ settlement points, not all geographic features or
live services. Source area may be a historical land district, not a modern
council region. Distance calculations use representative points, not a user's
address or GPS location.

Only Hamilton-Raglan has supplied saved route estimates, in both directions for
walking and driving profiles. These are OpenStreetMap/Valhalla estimates and
include SH23; they do not verify present pedestrian access, shoulders, closures
or safety. Missing pairs are unavailable and any straight-line alternative is
explicitly labelled. Straight-line calculation is spherical/approximate.

For nationwide mapped walking distances, install the optional local pedestrian
graph/backend using [NZ offline routing setup](nz_routing.md). It is implemented
and verified on the Pi with a national dataset; it calculates arbitrary supported
pairs without a precomputed pair table. Without that backend, the saved routes
above remain the only walking coverage. Nationwide road routing is not supplied.

Distance, place, bearing, towns and water origins support coordinates and the optional
national street index; see [input forms, installation and limits](origins.md).
Water lookup uses a separate optional index; see [water approaches and limits](water.md).

Tide coverage is Raglan, 1 January-31 December 2026, plus 1-3 January 2027. Full
2027 conversion rejects its ambiguous local 4 April 02:09 timestamp rather than
choosing a daylight-saving fold without evidence. Converter rejects nonexistent
local times too. Source height datum is not stated in the CSV, so replies say so.
Do not interpret prediction heights as actual depth, observed levels or warnings.
Secondary-port offsets and other sites require separately validated imports.

Two short NEMA/Get Ready reference topics and three original-XIAO firmware
guides are supplied. These are not complete emergency manuals. Directory entries
and optional facility points require operator-reviewed publishable data.

## Run when the room is connected by USB

```sh
python -m room_bot --port /dev/ttyACM0 --config ~/.meshcore-room-bot/config.json --state ~/.meshcore-room-bot/state.sqlite3 --mode bot --name SquishyBot
```

Replace the actual serial path; use COM26 on Windows only if it is still the
correct device. One process owns USB. Reuse the existing state file when moving
an established installation. Do not delete it or use `--replay` as a routine upgrade.

Dates are unconfirmed by default. After independently checking the host OS date
and time, add `--clock-trusted` for this run. It is not persisted across restart.
See [compass and true bearings](magnetic_bearings.md) for the offline magnetic
model, switches and unavailable-clock behaviour. Tides and daylight fail
explicitly without a trusted current date; clock jumps
revoke trust. Notice/check-in writes can retain unknown timestamps and must not
invent ages. Config/clock checking sets neither the OS nor the MCU clock.

## Commands

Geographic commands live under `/bot location`. Main `/bot help` shows just
`/bot location help`; it does not list every geographic action. Use
`/bot location help` to list the enabled actions, or `/bot location towns help`
(or `/bot location help towns`) for an explanation and example.

`/bot location towns LOCATION` returns the closest three towns/localities in the
installed settlement catalogue, measured in a straight line from the supplied
coordinate or saved reference point. It does not require a category and does
not search rivers, facilities or services. For potential water approaches use
`/bot location water LOCATION`. Streets can use their labelled reference point
for `towns` and `water`; street water results label the approximation. Distance
and bearing still require explicit `@point` or coordinates for street endpoints.
Suburb-plus-city input such as `Dinsdale Hamilton` resolves the suburb point
using a 20 km city-proximity hint; coordinates provide the most precise origin.

Host configuration can enable the entire geographic group with `location`, or
select individual feature names (`towns`, `water`, `distance`, etc.). Existing
per-feature configurations still work. Old flat commands remain hidden aliases;
`nearby` responds with the new `towns` command instead of consuming a category.
No location features are enabled in the default sample-only configuration.

Normal replies omit source URLs and attribution footers to conserve LoRa airtime.
Provenance, licenses and snapshot revisions remain in the installed datasets and
documentation. Enable `source` alongside your chosen commands to request metadata
explicitly: `/bot source` lists installed sources; `/bot source place`,
`/bot source water`, or `/bot source DATASET_ID` returns details. Source requests
are offline too; they never fetch the listed URLs. No source hint is appended
to each ordinary reply. Safety, availability and expiry qualifications remain.

| Command | Behaviour |
| --- | --- |
| `/bot help` | Lists enabled top-level commands and groups |
| `/bot location help` | Lists enabled geographic commands |
| `/bot location towns help` | Explains the towns lookup and gives an example |
| `/bot source [TOPIC\|ID]` | Explicit source details only; bare command lists installed sources |
| `/bot location details` | Optional detail from your latest place/distance/bearing/water lookup; enable `details` |
| `/bot directory TOPIC` | Saved operator-reviewed directory, availability unconfirmed |
| `/bot emergency earthquake` | Saved source-labelled reference; floods is also supplied |
| `/bot guide battery` | Board/version-specific saved instructions; does not execute settings |
| `/bot location place Raglan` | Place name and reference coordinates; street IDs where needed |
| `/bot location distance Hamilton to Raglan` | Walking-profile saved estimate by default |
| `/bot location distance Hamilton to Raglan road` | Explicit road estimate |
| `/bot location distance Hamilton to Raglan straight` | Approximate straight-line value, explicitly not a route |
| `/bot location bearing Hamilton to Raglan` | Initial magnetic compass bearing by default; requires a confirmed date |
| `/bot location bearing true Hamilton to Raglan` | True-north bearing without a dated magnetic calculation |
| `/bot location bearing both Hamilton to Raglan` | Both values, clearly labelled; modes also work at the end |
| `/bot location towns Hamilton` | Closest three towns/localities by straight-line distance |
| `/bot location walktime 45 4` | Rough hours at stated 4 km/h, without stops/terrain/weather |
| `/bot location water Hamilton` | Shortest tested mapped-path approach near water; safety/access unknown |
| `/bot location water -37.7313255,175.2215203 straight` | Explicit straight-line ranking of indexed path approaches, not water access |
| `/bot location tides Raglan` | Next retained predicted high/low at trusted date; no default coast |
| `/bot location sun` / `/bot location sun Raglan` | Calculated sunrise/sunset; configurable Hamilton default |
| `/bot calc` | Explicit supported arithmetic operations, never arbitrary expressions |
| `/bot notice add TEXT` | Save an attributed unverified community report |
| `/bot notice list` / `/bot notice show ID` | Newest 20 summaries or detail; bounded paging |
| `/bot notice withdraw ID` | Author can remove their own notice |
| `/bot checkin [TEXT]` | Voluntary statement; never interpreted as proof of safety |
| `/bot lastseen KEY_PREFIX` | Latest retained voluntary check-in; minimum eight hex characters |
| `/bot next` | Requesting user's next saved page, delivered in the shared room |

Ambiguous place names can use `NAME, SOURCE_AREA` or a listed stable place ID.
Full names support macrons/case-insensitive matching. Phone display names are not
in the USB input; to use names with lastseen, configure explicit public-key aliases:

```json
{"aliases": {"ExampleAlias": "replace-with-the-full-64-character-lowercase-hex-public-key"}}
```

This is a format illustration, not a valid key/config until replaced. Names do
not grant administrator privileges. All replies/check-ins are shared room posts.

## Community retention and operator maintenance

Default `community` config: notice_days=7, checkin_days=30, notice_limit=1000,
checkin_limit=512. These are provisional configurable choices; days=0 disables
age-based expiry while hard count limits remain. Known timestamps expire only
when the current session clock is confirmed. Unknown timestamps cannot be aged
and remain until explicitly removed or purged; storage limits still apply.

Community data is a separate room-bound `community.sqlite3`. It is never adopted
for a different room. Domain mutations save their input sequence, author, request
and response atomically; a worker retry after a crash does not duplicate a notice.
Action outcomes survive content expiry/purge. The retry ledger is bounded at
4,096 rows; a saved floor refuses mutations from already-pruned old sequences.

Trusted local operator maintenance (does not publish or clear delivery state):

```sh
python -m room_bot.community list
python -m room_bot.community withdraw 123
python -m room_bot.community --clock-trusted purge --records notices --older-than 7
python -m room_bot.community purge --records checkins --all
```

The last command deliberately removes all retained check-ins; it is an example
for intentional maintenance, not an installation/upgrade step. Unknown-time rows
are excluded from age-based purge. Preserve the DB plus WAL/SHM together while
running, or stop the bot before backing up. These local databases contain plain
text content; no new at-rest encryption is claimed.

## Verification scope

Automated tests cover config/schema checks, transaction failure/restart/retries,
offline execution, published tide/astronomy examples, Unicode limits and paging,
route mode separation, unknown clocks and storage/admission bounds. They do not
prove current walking access or actual Pi-to-room/phone radio delivery. The local
ignored TODO records exact completion status and measured Pi checks.
