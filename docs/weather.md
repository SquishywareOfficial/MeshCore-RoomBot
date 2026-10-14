# Weather and forecasts

These commands use the bot host's internet connection. They require no extra
LoRa node, firmware update or API key for Open-Meteo's public noncommercial API.
Current conditions are weather-model estimates, rather than readings from a
local weather station. See [Open-Meteo's documentation](https://open-meteo.com/en/docs)
and [service terms](https://open-meteo.com/en/terms).

```text
/bot weather
/bot weather Raglan
/bot forecast
/bot forecast Mount Maunganui
/bot weather Springfield, Canterbury
```

Both commands default to `default_location` (Hamilton). A location override
applies only to that request. NZ populated places are supported; exact matching
avoids selecting similarly named mountains/airfields or Hamilton in another
country. For ambiguous names, the bot asks for a region. Multiword names and
case/macron differences are supported. This provider lookup is separate from
the offline street/coordinate location commands.

Weather includes temperature, condition, model time, feels-like temperature,
humidity, wind/gusts and precipitation over the stated model interval.
Forecast returns today and the following two days, with conditions, low/high
temperature, precipitation probability/total and maximum wind. Daily summaries
include the entire day, not just the hours remaining today. Temperature is C,
wind is km/h and precipitation is mm. Wind names give the direction it comes
from. Missing values are `n/a`, never silently zero. NZDT/NZST or the Chatham
offset is used as appropriate.

Normal responses identify Open-Meteo once, without URL/source footers or provider
metadata dumps. Each prefixed message fits 151 UTF-8 bytes and uses the existing
pacing/outbox/paging. Hamilton/Raglan current and forecast examples normally fit
two messages; unusually long place/condition names or stale labels may need more.
No room announcement is sent just because the weather plugin is enabled.

## Enable or disable

New installations enable weather and forecast. Existing configs are preserved
during setup; add these command names and settings explicitly, or merge the
installer's proposed config:

```json
{
  "version": 1,
  "enabled_commands": ["ping", "help", "about", "weather", "forecast"],
  "default_location": "Hamilton",
  "weather": {
    "enabled": true,
    "timeout_seconds": 5,
    "cache_seconds": 600,
    "stale_weather_seconds": 7200,
    "stale_forecast_seconds": 21600,
    "cache_entries": 128
  }
}
```

Merge into your existing config, preserving its location features and data_dir.
Configs without `weather.enabled` retain the old default of no weather network
access. Removing the command names or setting `weather.enabled` to false
disables it. The bare bot still defaults to its three original sample commands.

Check the host date, then launch with `--clock-trusted`. A missing/untrusted
clock or detected clock jump disables weather so cache age/dates aren't guessed.
On a Pi that starts without internet or an RTC, don't automatically assert clock
trust before setting its date.

## Cache, failures and limits

Successful results are saved in `weather.sqlite3` under the configured data_dir,
separately from the USB delivery state and offline/community databases. Current
and daily results share one provider response. Locations are cached for 30 days;
weather is reused for ten minutes by default. Cached entries and failure tracking
are bounded. Failed/invalid downloads never replace the last good result.

After a refresh failure, a saved weather result is usable for at most two hours
from both retrieval and model time, and a saved forecast for six hours from
retrieval. Fallback replies explicitly say `STALE cached Xm; refresh failed` and
retain their original time. Forecasts discard days already past. No sufficiently
recent saved result means an unavailable reply, not a made-up forecast.
Changing the clock into the past invalidates future-dated cached records.

Each HTTP request runs in a separate short-lived helper process with a hard
`timeout_seconds + 1` deadline, including DNS/connection/read. A new place takes
up to two requests (geocode then forecast); a cached place takes at most one.
Response size is capped at 128 KiB, with validated units, finite values, location,
timezone and forecast dates. A failed request has a 60-second per-location
backoff; per-author dispatcher limits still apply. Slow requests delay subsequent
commands within these finite bounds, without opening another USB connection.

Only these two on-demand services are implemented here. Official weather alerts,
automatic warnings, news, earthquakes and surf remain separate work.
`/bot weather emergency` states that alerts are not yet implemented; it does not
turn forecast values into invented official warnings.

Open-Meteo weather data: CC BY 4.0, attribution Open-Meteo; geocoding uses
GeoNames data. Source/terms links are kept here rather than sent with every reply.

## Verification

Tests cover NZ/default/override resolution, ambiguity, no network while disabled,
cache persistence/expiry, stale labels, midnight/Chatham dates, malformed data,
missing values, units, reply byte limits and hard request deadlines. Live provider
checks used Hamilton, Raglan and Mount Maunganui. These checks establish actual
provider responses and formatting; delivery to a phone requires the running bot
and a working room connection.
