# MeshCore RoomBot

[![Tests](https://github.com/SquishywareOfficial/MeshCore-RoomBot/actions/workflows/tests.yml/badge.svg)](https://github.com/SquishywareOfficial/MeshCore-RoomBot/actions/workflows/tests.yml)

A Python bot for a USB-connected MeshCore Enhanced room server, designed for a Raspberry Pi Zero 2 W. Offline NZ towns, street/place lookups, walking distances, compass bearings and mapped-water searches are supported. Optional handlers include tide predictions, daylight, saved emergency references, noticeboard/check-ins and calculators.

On-demand **weather and three-day forecasts** default to Hamilton and accept other NZ locations, e.g. `/bot weather Raglan` and `/bot forecast Mount Maunganui`. Saved results can be used during an outage with an explicit stale label. [Weather setup and commands](docs/weather.md).

**Start here:** [repeatable installer](docs/setup.md), [command guide](docs/offline_usage.md), [releases and NZ map bundle](https://github.com/SquishywareOfficial/MeshCore-RoomBot/releases). Firmware remains in [MeshCore-Enhanced](https://github.com/SquishywareOfficial/MeshCore-Enhanced).

The installer downloads and verifies national map data; it preserves your settings and bot progress. Full NZ routing needs 64-bit Linux/Pi OS. Basic setup also supports Windows.

The Enhanced XIAO room firmware provides a USB interface for a Raspberry Pi or
other computer to read room posts and publish replies. **No extra companion
radio is required.** The room still handles normal encrypted radio delivery to
its members. This interface is for the direct USB connection, not remote radio
administration, Ethernet CLI, or companion firmware.

Use Enhanced room firmware with the USB bot API (SQ5 and later). The bot
code is separate from MCU firmware and does not alter room identity/settings,
journal formats or the phone/radio protocol. Update the same room role without erasing data; see the
[firmware installation guide](https://github.com/SquishywareOfficial/MeshCore-Enhanced/blob/main/docs/enhanced_builds.md).

## Run the supplied monitor or sample bot

The computer-side code is portable Python 3.9+ with `pyserial` and `tzdata`; SQLite is in the standard library. Optional
Astral supplies local daylight calculations. Optional Shapely/NumPy supplies
on-demand water geometry (see requirements-water.txt). A Raspberry Pi Zero
running a suitable Raspberry Pi OS should be able to run this lightweight USB
worker. Use its USB data/OTG port in host mode with an appropriate adapter and
provide adequate power for both boards; see the [Raspberry Pi setup guide](https://www.raspberrypi.com/documentation/computers/getting-started.html).
Windows USB/phone tests passed. Pi software-only validation is recorded
separately; Pi USB/phone delivery remains unverified. Heavy local AI inference is a separate
resource requirement; the bot can instead call a model hosted elsewhere.

Use a checkout containing this feature and Python 3.9 or later. On your Pi:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m room_bot --port /dev/ttyACM0 --state ~/room-monitor.sqlite3 --mode monitor
```

Run from the MeshCore-RoomBot folder. Replace the port with the room's actual USB port;
on Windows the equivalent example is `--port COM26`. Linux USB permissions may
require your account to belong to the serial-device group. Close web
configuration/serial terminals before starting: one process owns the USB port.

To run the sample bot instead:

```sh
python -m room_bot --port /dev/ttyACM0 --state ~/room-bot.sqlite3 --mode bot --name SquishyBot
```

From a normal logged-in room member, use:

| Message | Reply |
| --- | --- |
| `/bot ping` | `[SquishyBot] pong` |
| `/bot help` or `/bot` | Available commands |
| `/bot about` | Identifies the Python USB room bot |

Commands are case-insensitive and accept surrounding/repeated whitespace. Only
messages starting with a separate `/bot` token address this sample bot. Bare
`/ping`, `/help`, ordinary conversation and `/botping` get no reply. Unknown verbs
or extra arguments get a short help/usage response. `--mode ping` remains a CLI
alias for `--mode bot`; it uses the same new `/bot` message syntax. Replies
appear under the **room server's identity**; the prefix is a label, not a new
cryptographic identity. System/room-authored posts are ignored to prevent loops.

The default mode is a monitor that prints incoming user posts without replying.
First use of a new state file starts at the current archive end, so a newly
started bot does not unexpectedly answer old conversations. Existing state
resumes its saved position. Add `--replay` only if you intentionally want a
**new state file** to process retained history; it does not reset an existing
cursor. `--allow-gap` explicitly accepts missing posts before the retained
archive's oldest sequence. Otherwise a retention gap stops with an explanation.

Default polling is once per second in pages of four posts; maximum page size is
eight. Replies are spaced at least five seconds apart in a running process.
`--poll-interval` and `--reply-interval` can adjust these (minimum 0.2 and 1 second
respectively). USB polling does not itself transmit LoRa packets. Publishing a
reply creates a normal room post which the room sends to eligible members.

## Connect your own chatbot

Create an importable Python module, for example `my_chatbot.py` in the repository
root, containing:

```python
def reply(post):
    # post.sequence, post.timestamp, post.author (full public key), post.text,
    # post.sender_timestamp, post.kind and lossless post.raw_text are available.
    if post.text.strip().lower() == '/bot hello':
        return 'Hello from the connected computer!'
    return None
```

Start it with:

```sh
python -m room_bot --port /dev/ttyACM0 --state ~/my-chatbot.sqlite3 --handler my_chatbot:reply --name MyBot
```

The callback returns `None`, one string, or a list/tuple of up to four strings.
Every reply including its prefix must fit **151 UTF-8 bytes**, not characters.
Empty replies and NUL bytes are rejected. A failed callback or invalid response
does not advance that input's cursor. You can call your selected local model or
AI service inside this callback; no AI provider, credentials or network calls
are included here. Make external side effects idempotent using `post.sequence`:
a crash before the callback's result is saved can cause it to run again.

For direct programmatic use:

```python
from room_bot import RoomClient, SerialTransport, BotState, BotWorker

transport = SerialTransport('/dev/ttyACM0')
state = BotState('/home/pi/room-bot.sqlite3')
try:
    worker = BotWorker(RoomClient(transport), state, reply, prefix='[MyBot] ')
    worker.step()  # call repeatedly; the CLI supplies the polling/reconnect loop
finally:
    transport.close()
    state.close()
```

Keep one worker per state file and USB port. Multiple bot functions can be
combined in one callback; they cannot independently open the same physical port.

## Progress, retries and storage

The computer uses SQLite transactions to save input progress and pending replies
together, before attempting to send them. Pending replies drain before new
inputs. A lost USB receipt preserves the same request ID and text; retries
return the original committed post instead of publishing a duplicate, including
after a room or computer restart. Reply ordering is preserved within a callback.

Deduplication uses the room's existing retained journal, not an unbounded new
flash log. **It lasts only while the request's post remains in the retained
2,000-post archive.** If an ambiguous pending retry outlives that window, the
client stops for manual inspection instead of risking a duplicate. Reusing an
ID with different text reports `submission conflict`; state is not discarded.
USB I/O timeouts/disconnections reconnect with backoff. Protocol/storage/schema,
identity mismatch and rollback errors stop without clearing progress/outbox.

Use a separate state file for another room. If the room is reset or rolled back
behind your saved cursor, inspect it and choose a new state file intentionally;
the client never silently resets its cursor. Preserve the SQLite database and
any `-wal`/`-shm` files together while the process is running, or stop it first
for a backup. They can contain message replies in plaintext. SQLite and checked
room writes do not guarantee against every physical storage/power-loss fault.

## USB protocol v1

Commands end with CR or CRLF. Responses are ASCII JSON lines prefixed `@bot `;
ignore command echo, blank lines and ordinary diagnostic output. One request is
in flight at a time. All 64-bit sequences and request IDs are decimal strings.
Text is hex-encoded raw UTF-8 bytes, preserving quotes, newlines and non-ASCII
text without allowing it to become a console command.

| Command | Response |
| --- | --- |
| `bot.info` | `type=info`, `api=1`, full `room_key`, count, oldest/latest/high_water sequences, page/text limits |
| `bot.read <after> [limit]` | Zero to eight `type=post` rows, followed by one `type=end` page receipt |
| `bot.post <request-id> <text-hex>` | `type=posted`, matching request ID, committed sequence/timestamp and duplicate flag |

`bot.read` selects sequence **greater than** `after`, oldest first. The default
limit is four; accepted limits are 1..8. Each post includes full `author`,
`sequence`, server `timestamp`, `sender_timestamp`, `kind` and `text_hex`. The
end row includes `after`, `next`, `oldest`, `latest`, `high_water`, `count`, `gap`
and `more`. A page is valid only after its end receipt; discard a partial page
after timeout/error. Read-only polling never advances a room member's cursor.

`high_water` includes reserved sequence gaps; it may exceed the newest retained
post. A cursor above it is rejected. A gap indicates a cursor before the oldest
retained sequence minus one; sequence reservations can also create holes, so a
gap is conservative. Sequence IDs identify progress; timestamps are not relied
on for USB resume. They may be logical monotonic room timestamps when RTC time
has not been set.

Request IDs are nonzero unsigned 32-bit integers; text is 1..151 bytes without
NUL. The Python client allocates a random request ID and persists it before
sending. Bot submissions reuse the existing kind=0 room-author record, with
`sender_timestamp` holding the request ID. They remain readable by earlier
history firmware; there is no schema migration. Human `room.post` system records
still use kind=1. The worker ignores both by checking kind/full author.

Errors have `type=error` and an `error` string. Oversized/invalid input is discarded
through its line ending; it cannot execute a truncated command. Every page is
bounded and the radio loop runs between commands. USB output failure omits a
successful terminal response; the host retains pending work for retry.

Direct USB is an administrative connection and can read all retained posts.
Bot reads/posts are unavailable to RF/Ethernet CLI, including authenticated
radio admins. Ordinary command-looking guest posts remain plain room messages.
Existing history access/encryption and plaintext-at-rest limitations still apply.

## Verification

Firmware tests cover paging, retention gaps, maximum/control/Unicode text,
invalid inputs, storage faults, lost receipts, durable duplicates, and bounded
line parsing. Python tests cover default/replay resume, identity/rollback/gap
checks, lost receipts, transactional outbox, callback failures, reply ordering,
rate limiting and fragmented serial responses. Physical verification results
are recorded separately; software tests do not prove phone display or hardware
power interruption during writes.

Physical USB checks on the connected XIAO S3 room (`v1.17.1-sq5-bot`) preserved
identity/settings and all 204 existing posts, including the ordered 200-post
catch-up batch. A single labelled 151-byte bot post was read back byte-for-byte;
exact retries before and after soft reboot returned that same post (205 total).
The host's saved cursor resumed and ignored its own reply. Oversized input was
rejected without changing history. Hardware testing found the default 256-byte
ESP32-S3 USB RX buffer was too small for hex submissions; this build explicitly
allocates and checks a 1,024-byte buffer before starting serial.

These checks used the Python client on Windows. A subsequent real member's
`/ping` through the phone app produced `[SquishyBot] pong`; the companion
acknowledged it and the user confirmed it appeared in the room chat. Pi USB/phone delivery remains a separate, unverified hardware check.

The original phone acceptance test used the earlier `/ping` spelling. Current
sample commands require `/bot ping` or `/bot help`; this change is entirely on
the computer and requires no room firmware update.

## XIAO USB reply completion

The pinned Arduino 2.x HWCDC driver can leave a final full 64-byte USB packet
pending until another write. The room uses `UsbReplyStream.h` to drain each
bounded JSON line, then wait at most 100 ms for the hardware FIFO to become
writable and flush the terminating packet. It then yields for two milliseconds
to let the legacy driver finish handling the acknowledgement before another
write. This follows [Espressif's S3 HAL
requirement](https://github.com/espressif/esp-idf/blob/master/components/esp_hal_usb/esp32s3/include/hal/usb_serial_jtag_ll.h).
The low-level step is guarded to USB-Serial/JTAG mode; it does not run for UART
or USB OTG. Setup checks an 8,192-byte transmit buffer allocation alongside the
existing 1,024-byte receive buffer and enters recovery on allocation failure.
The generic bot JSON, cursor checks and short-write failure behaviour remain
unchanged. This is an application-level adapter, not a modified toolchain cache.

## Standalone project and tests

This folder contains the host-side Python bot, its tests and documentation.
It was extracted from MeshCore-Enhanced at commit
`ca22c37b96448d8626e1731ed476dd529bb08f26`. The firmware's USB interface
and C++ protocol tests remain in the firmware project. No firmware checkout
is required to run this bot; a compatible Enhanced room firmware is required.
The copied MIT license is retained in LICENSE.

From this folder, with dependencies installed:

```sh
python -m unittest discover -s tests -v
python -m room_bot --help
```

On Windows, a minimal setup is:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m room_bot --port COM26 --state "$env:USERPROFILE\.meshcore-room-bot\state.sqlite3" --mode bot --name SquishyBot
```

When migrating an existing installation, stop its old process and reuse the
same state database path. No state schema changes are needed. A different
state file starts fresh at the archive end unless --replay is deliberately used.
Imports and invocations change from `tools.room_bot` to `room_bot`; custom
handlers keep the same callback contract.

The original sample commands are /bot ping, /bot help and /bot about. Optional
offline lookups/tools are now implemented; see the current setup guide below.
Internet-backed weather/forecast/alerts, surf, news and quakes remain planned.

## Future offline data

The [offline data and services plan](docs/offline_data_plan.md) records proposed
community notices, emergency reference material, a local directory, tide tables,
voluntary check-ins, offline place/route distances, daylight calculations,
practical guides and saved online
snapshots. The plan covers implemented offline components and remaining work; current
commands and supplied dataset coverage are documented in the setup guide.

## Host configuration and clock validity

Install `requirements.txt` before using the new configuration options. The
`tzdata` dependency supplies NZ timezone/DST rules on Windows and as a fallback
on Linux; normal runtime needs no timezone download.

Create and validate a template without connecting to the room:

```sh
python -m room_bot --init-config ~/.meshcore-room-bot/config.json
python -m room_bot --config ~/.meshcore-room-bot/config.json --check-config
```

Existing config files are never overwritten by `--init-config`. A configuration
is loaded only when explicitly passed with `--config`; existing launch commands
and `--state` continue to work. Unknown keys/versions, duplicate JSON keys,
invalid timezones, commands and limits fail before USB is opened.

Version 1 selects enabled command names, default location (Hamilton), timezone
(Pacific/Auckland), a data directory and bounded query/paging/cooldown limits.
Only ping/help/about are enabled by default. No automatic announcements are
configured. New feature names in the schema do not imply those features exist.

Data defaults to `~/.meshcore-room-bot/data`, separate from the existing delivery
state database. A relative `data_dir` resolves relative to the config file, not
the current working directory. Configuration checking creates no data/state DB.

The offline clock guard treats date/time as unconfirmed by default. After
checking the operating system clock, the operator can launch with
`--clock-trusted` to confirm it for that process only. The assertion is never
saved; a restart requires confirmation again. A wall-clock discontinuity beyond
`clock_max_jump_seconds` (default 5) compared with monotonic elapsed time revokes
trust until a new session. This detects jumps, not all gradual clock drift.
Date-dependent features must use this guard and return an unavailable response
when it cannot supply a trusted time. Paging/cooldowns use monotonic time.

No OS clock is set, no NTP request is made, and no service is started by these
options. An RTC/automatic OS-sync validity policy remains a future extension.

### Command registry and paging

Built-in bot modes now use an explicit registry and honour configured enabled
commands. Existing monitor/custom handlers remain available. `/bot next` fetches
the requesting author's saved page; replies still appear in the shared room.
Pages expire after the configured monotonic TTL and are not retained through a
bot restart. Repeat the original query after expiry, eviction or restart.

The formatter accounts for the actual prefix and the 151-byte firmware limit;
answers use at most four messages per request. Lookups are subject to configured
per-author cooldowns before expensive work. Basic ping/help/about remain usable
without consuming those lookup cooldowns. Saved results and tracking maps are
bounded; requests too large to page return a request to narrow the query.

### Offline content available so far

Directory, saved emergency-reference and guide lookup handlers are implemented.
They require explicit command enablement and locally imported datasets; no data
is fetched during a lookup. Two short attributed Get Ready earthquake/flood
references are included for deliberate import. These are not full guides or
current emergency alerts. See [data import instructions](docs/data_imports.md).

The execution checkpoint file is the local `TODO_OfflineData.md`. It records
completed steps, exact checks and restart instructions; it is ignored by Git.
The broader feature plan includes future features and must not be treated as a
list of commands already available.

## Repeatable installation

See [the setup guide](docs/setup.md) for `python setup_bot.py --profile nz` on
Pi OS 64-bit/Linux, or `--profile basic` on Windows/Linux. Setup creates an
isolated environment, installs dependencies, imports supplied data and verifies
prepared map downloads. It preserves existing configuration/delivery state and
leaves bot/service startup explicit. Prepared NZ download hosting is pending;
the guide includes the supported local-bundle/mirror options.

## Current offline setup

Geographic commands are grouped under `/bot location`: `towns`, `water`,
`distance`, `bearing`, `place`, `walktime`, `tides`, `sun` and optional `details`.
Main `/bot help` lists the group once. `/bot location help` lists its enabled
commands; `/bot location towns help` explains a command with an example.
`/bot location towns Hamilton` shows the closest three towns/localities by
straight-line distance, without the old `nearby TYPE PLACE` syntax. Streets
and coordinates also work. Water also accepts streets without `@point` and
suburb-plus-city input, e.g. `/bot location water Dinsdale Hamilton`. Street
water results label the approximate starting point. Each action still requires
its data/setup.

[Bearing commands](docs/magnetic_bearings.md) default to magnetic compass, with
`true` and `both` options. Magnetic calculations run offline with WMM2025 and
require a confirmed host clock; unavailable magnetic results are labelled clearly.

Normal mesh replies omit source URLs/attribution footers to save airtime. Source
metadata and licenses remain in the local datasets and documentation. Optional
`/bot source [TOPIC|ID]` provides them only when explicitly requested; enable
`source` in the host configuration. Optional `/bot location details` returns the extra
precision/search information from your latest place, distance or water lookup;
enable `details` to use it. Ordinary location replies show the result without
internal dataset metadata. Access and safety qualifications remain.

See [offline setup, commands and coverage](docs/offline_usage.md). Optional
components now include saved directory/emergency/guides, source-backed place and
route distances, bearings/towns/walking-time tools, Raglan tide snapshots,
voluntary check-ins/community notices, daylight and deterministic calculators.
Only the three original sample commands are enabled by default. The valid
[example config](examples/offline_config.json) enables the implemented features
for deliberate use after imports; it neither starts the bot nor confirms its clock.

The optional [nationwide offline walking router](docs/nz_routing.md) uses a local
NZ OpenStreetMap graph, verified on the Pi Zero 2 W. It calculates mapped walking
distances for resolved settlements throughout NZ; disconnected/unmapped routes
remain unavailable. The 4,903-place LINZ catalogue supplies named endpoints.
Without this backend, the original saved walking/road table only covers
Hamilton-Raglan. See the [routing config example](examples/nz_routing_config.json)
for opt-in setup, graph installation, limits and measured Pi resources.

Geographic commands now share [coordinate and street origin support](docs/origins.md).
The optional [water approach lookup](docs/water.md) reuses those origins and
calculates nearby approaches on demand, then compares bounded walking routes
to mapped paths near water. It reports unknown
drinking-water safety and unverified bank access; it is not a potable-water map.
Use decimal latitude/longitude, or install the optional national street index.
Street-only routing requires an explicit `@point` choice for a labelled approximate
representative point; ambiguous streets are listed for selection. This resolver
also serves the implemented [on-demand water lookup](docs/water.md).

Sources and import schemas are in [data_imports.md](docs/data_imports.md).
Dataset contents, copyright notices and original source evidence are separate
from the MIT bot code. Date-dependent commands require an explicitly confirmed
clock; source snapshots cannot establish live warnings, service availability or
safe/open walking routes.
