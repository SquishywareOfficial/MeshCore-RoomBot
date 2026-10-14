# Nationwide tides: offline first

```text
/bot location tides Raglan
/bot location tides Tauranga
/bot location tides Riverton
/bot location tides Waitangi - Chatham Island
/bot location tides locations
/bot location tides locations Bay
```

The coastal location is required; there is no Hamilton/Raglan default. The old
`/bot tides LOCATION` alias also works when tides are enabled. Site search lists
up to 20 matches with normal reply paging; narrow the search for more specific
results. Case/macrons are normalized. Exact official names take precedence over
aliases (e.g. Waitangi in Northland versus Waitangi - Chatham Island).

The prepared dataset covers **all 322 sites in LINZ's official catalogue**:

- 88 sites have direct annual predictions for 2026, 2027, 2028 and 2029.
- 234 secondary sites use LINZ's published standard-port time differences and
  height conversion parameters, with results labelled **offset estimates**.
- There are 494,468 retained high/low-water events from 352 annual files.
- 19 secondary sites have missing time offsets for at least one event type;
  22 have missing height parameters. Missing values stay unavailable. A mapped
  tide site does not imply complete predictions for that point.

This is the official set of tide sites, not a tide grid for every beach or GPS
coordinate. The catalogue also includes Scott Base and offshore islands. Primary
predictions, secondary coefficients, original source CSVs, retrieval times and
hashes are bundled together, with LINZ attribution and CC BY 4.0 terms.

## Installation and upgrades

Both `setup_bot.py --profile basic` and `--profile nz` download and verify the
versioned national tide release automatically. The tide ZIP is approximately
15 MiB, separate from the optional 345 MiB routing/water ZIP. No tide preprocessing
is required on the Pi. The installer checks SQLite integrity and declared site/
year coverage, in addition to every archive/file checksum.

For offline transfer, use `--tide-bundle-file /path/to/nz-tides-20261014-v1.zip`.
The checksum-pinned manifest is `installer/nz-tides.json`. Raw files and notices
are installed alongside `tides.sqlite3`; private state/community data are absent.

An existing operator config is preserved, with a proposed new config written for
review. Stop the bot before upgrading. Merge the proposed `data_dir`/commands
deliberately, preserving community data if moving directories. Old manually
imported Raglan content remains supported when no national DB is installed.

## Clock, caching and refresh

Predictions require a verified host clock (`--clock-trusted`). Site listings
do not. Dates/times use the tide site's actual published local-time convention,
including Chatham's 45-minute timezone difference.

Annual tide predictions retain their utility across their published calendar
coverage; they are not expired after a few hours like weather estimates. Fresh
coverage is read entirely locally with **no internet request**. Refresh is tried
when coverage ends within 30 days, or a secondary-offset edition's next annual
review is within 30 days. Requests discover the currently published LINZ years
and latest secondary table, then gather missing annual files for the queried
standard port. Attempt times survive restart; each site retries at most weekly
by default. There are no automatic room announcements or daily whole-country
downloads.

Optional configuration:

```json
"tides": {
  "auto_refresh": true,
  "refresh_before_days": 30,
  "retry_hours": 168,
  "timeout_seconds": 5
}
```

The edition review date is a refresh policy, not a claim that tide physics expires
on that day. If a new table cannot be acquired, saved dated primary predictions
remain usable through their coverage. Secondary replies say `Older offsets:
2026-27` after their review date. Past/unsupported prediction dates are never
extrapolated, and missing offsets/heights are not inferred from a nearby port.

Each HTTP request has a subprocess-enforced timeout; one refresh has a 40-second
overall budget. Malformed/failed updates keep existing data. Base `tides.sqlite3`
stays immutable for installer checksum verification. Updates are staged and
activated atomically in a separate `tide-updates.sqlite3`; retry state is in
`tide-refresh.sqlite3`. Neither file changes USB delivery progress or room history.

## Source interpretation

Source: [LINZ tide predictions](https://www.linz.govt.nz/products-services/tides-and-tidal-streams/tide-predictions).
Secondary conversion follows the [official method](https://www.linz.govt.nz/guidance/marine-information/tide-prediction-guidance/how-calculate-tide-times-and-heights):
apply the signed mean high/low time difference, and calculate height as secondary
MSL + (standard-port predicted height - standard-port MSL) * range ratio. Mean
differences can vary from actual tidal differences. Missing coefficients stay
missing. Heights refer to chart datum, not mean sea level or water depth.

The CSV parser accepts five/six-event days, one-event diurnal days at Scott Base,
equal rounded heights, and an appended next-year boundary fragment. Only fully
covered annual dates are advertised. Explicit source header-name variants are
recorded for Kaituna/Rangitaiki River Entrance and Town Basin.

Repeated DST clock times retain both possible UTC instants, unless the source
event order itself eliminates one. Ambiguous responses show both timezone
possibilities and `DST uncertain`; they never silently choose a one-hour offset.
Chatham's local-time convention was checked against its official annual PDF.

Two source discrepancies are explicitly recorded rather than generalized:
Ocean Bay uses the current secondary table's Wellington reference, while the web
map says Lyttelton; Paparoa Point uses the table's 173°01'E longitude, while the
map point is around 174°01'E. Original catalogue values remain in site metadata.

These are predicted astronomical tides. They do not include storm surge,
tsunami effects, live sea-level observations or navigation access conditions.
Source details are opt-in through `/bot source tides`; ordinary replies omit
URLs and repeated metadata footers.

## Gather a new national snapshot

This is the reproducible data-gathering tool used to prepare the installer asset:

```sh
python tools/prepare_tides.py --output /path/to/new-tide-snapshot \
  --first-year 2026 --id nz-tides-NEW-DATE-vN
```

The last year defaults to LINZ's latest published year. Download receipts and
validated raw CSVs are reusable after an interrupted gathering step. An existing
completed DB is preserved: use `--package-only` to repackage it, or choose a new
output folder. No PDF library or raw map dependency is required for gathering.

Publish the prepared ZIP, set its pinned release URL in the generated manifest,
copy it to `installer/nz-tides.json`, then commit the small source/manifest/docs.
Never put the large derived DB or private runtime files into Git. Source name/
reference/coordinate changes outside the reviewed cases fail validation for
operator review rather than silently producing the wrong tide.
