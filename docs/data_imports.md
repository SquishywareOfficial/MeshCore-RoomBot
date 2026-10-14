# Offline content imports

Implemented: separate content storage, strict JSON imports, offline lookup
handlers, geographic tools and tide conversion. See offline_usage.md for the
complete current command list and supplied dataset coverage. No production data is downloaded
automatically. The bot's delivery state DB is a separate schema and file.

Run these from the standalone project root:

```sh
python -m room_bot.data --config /path/to/config.json import /path/to/dataset.json
python -m room_bot.data --config /path/to/config.json list
```

Without `--config`, data defaults to `~/.meshcore-room-bot/data`. An explicit
`--data-dir PATH` overrides that directory for import/list operations only;
configure the same data directory for the running bot to see those datasets.
The content file is `offline.sqlite3`. Importing does not open USB or publish.

Enable desired command names in the config's `enabled_commands` array, retaining
ping/help/about as desired, then launch with that explicit `--config` in bot mode.
The monitor mode never replies. Available lookups at this checkpoint:

- `/bot directory TOPIC`
- `/bot emergency TOPIC`
- `/bot guide TOPIC`
- `/bot next` for additional saved pages

No match, no data and invalid data are distinct responses. Query results are
bounded and never synthesized. Directory entries do not establish live service
availability; reference text is labelled saved guidance, not a current warning.
Custom `--handler` callbacks keep their existing contract and do not automatically
receive this command registry.

## Bundled source coverage

Two small reviewed, adapted reference excerpts are included:

```sh
python -m room_bot.data import datasets/references/getready-earthquakes.json
python -m room_bot.data import datasets/references/getready-floods.json
```

Source: National Emergency Management Agency / Get Ready, reviewed 13 October
2026. These excerpts are not complete emergency guides and provide no live
warning or evacuation-centre status. The original pages and attribution are
recorded in each JSON dataset and sent with lookup results. Material is adapted,
not a verbatim reproduction of the full pages. Reuse terms were checked at
https://getready.govt.nz/about-this-site : CC BY 4.0 applies to eligible text;
logos/design and third-party materials are excluded. License:
https://creativecommons.org/licenses/by/4.0/

Power/internet-outage references, medical material and local contacts are not
included. Place/route/tide/practical-guide snapshots are now supplied as described
in offline_usage.md. Tests use
separately labelled synthetic fixtures; do not import them as real geography.

## Format version 1

Every import is a UTF-8 JSON object with these fields:

```json
{
  "format_version": 1,
  "dataset": {
    "id": "operator-directory",
    "kind": "directory",
    "title": "Operator reviewed local directory",
    "source_url": "https://example.invalid/replace-with-real-provenance",
    "license": "Replace with verified reuse terms",
    "revision": "Replace with source revision or review date",
    "retrieved_at": "2026-10-13T00:00:00+13:00",
    "coverage": "Describe actual locations and entries"
  },
  "records": [
    {
      "id": "synthetic-example",
      "title": "Example only; not a real service",
      "location": "Example locality",
      "text": "Replace with reviewed publishable directory content.",
      "aliases": ["example"]
    }
  ]
}
```

The timestamp above is a format example, not a claimed retrieval event. Source
metadata must reflect actual acquisition/review; do not invent source dates.
License declarations are not independently authenticated by the importer.

Unknown keys, unknown versions/kinds, duplicate JSON keys/record IDs, invalid
Unicode, controls, numeric values or missing fields are rejected. Supported
record schemas are defined by `validate_record` in `room_bot/data.py`:

| Kind | Required record fields | Optional fields |
| --- | --- | --- |
| directory | id, title, location, text | aliases |
| emergency / guide | id, title, topics (array), text | none |
| places | id, name, region, feature_type, latitude, longitude, aliases | none |
| routes | id, origin, destination, mode, distance_m, origin_label, destination_label, profile, summary | waypoints |
| tides | id, place_name, time (ISO timestamp with offset), event, height_m, datum | none |

Places use WGS84 degrees. Stored routes require explicit walk/road mode and
metres; straight-line values are calculated separately. Tide event is high/low;
the eventual LINZ importer must convert source time conventions deliberately.
Tide imports additionally support timezone, coverage_start and coverage_end
metadata supplied together, with validated timezone and ISO dates. Dated queries
require that coverage. See offline_usage.md for current command/importer status;
schema support alone does not guarantee dataset coverage.

Limits: 16 MiB and 20,000 records per import, 100,000 records per content DB,
and at most 1,000 returned rows/query (normally 100 by config). Large source
downloads require preprocessing outside normal command execution. An empty
import is rejected to prevent accidental replacement with no data.

All records validate before writes. A transaction replaces only the matching
dataset ID; database insertion failure rolls the replacement back. Other datasets
remain. Changing an existing ID's kind is rejected. Unknown database schemas
are not adopted or erased. SQL queries bind user values and escape LIKE wildcard
characters, and perform case/macron-insensitive matching.

No built-in data importer connects to the internet. Any acquisition/preprocessing
tools added later must be explicit operator operations, separate from lookups.
