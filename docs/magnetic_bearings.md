# Compass and true bearings

`/bot location bearing FROM to TO` defaults to **magnetic compass** bearing.
Use `compass`, `true` or `both` before the locations or after the destination:

```text
/bot location bearing Hamilton to Raglan
/bot location bearing compass Hamilton to Raglan
/bot location bearing true Hamilton to Raglan
/bot location bearing both Hamilton to Raglan
/bot location bearing Hamilton to Raglan true
```

`compass` means the heading on an unadjusted magnetic compass. If your compass
or phone already corrects for magnetic declination and shows true north, use
`true`. `both` explicitly labels both values. These are initial direct bearings,
not directions following roads or paths. Normal replies omit the long explanation;
use `/bot location bearing help` or `/bot location details` when needed.

Locations use the shared resolver: places, suburb plus city, street references
and decimal latitude,longitude. As with distance, a street bearing still needs
`@point` to select its approximate reference point; coordinates specify a more
precise origin. For example:

```text
/bot location bearing both Courtney Avenue Hamilton @point to -37.779203,175.250712
```

In the pinned street snapshot, on 14 October 2026 this gives approximately:

```text
Courtney Avenue near Hamilton to -37.779203,175.250712: Compass: 15° NNE; true: 36° NE.
```

Angles are rounded to whole degrees, wrap into 0–359, and use 16 compass
directions. Magnetic variation is calculated at the **origin**, not destination.
For east-positive declination, magnetic = (true - declination) modulo 360.

## Offline model and clock

Install the updated `requirements.txt` before using compass bearings:

```text
python -m pip install -r requirements.txt
```

The pinned dependency `pygeomag==1.1.0` is pure Python with no additional package
dependencies. It includes WMM2025 coefficients; requests never need the Internet.
The standard coefficient file is 4,554 bytes and is checked against SHA256
`dfa8597825af4e0b87ff4198a5b4fb661b3c49f4cd090cd0164e0259b075582f`.
Model calculations assume zero WGS84 ellipsoid height because the location
catalogue contains no elevation data. Model details and this approximation
are available on request, without automatic source footers in ordinary chat.

As with other dated features, verify the host's date/time and run the bot with
`--clock-trusted`. This uses the existing session-clock guard; no clock trust is
stored in the bot state. The Windows/WSL USB test adapter has its host clock
explicitly confirmed for the current test session. Production deployments
still require the normal CLI flag; this feature does not change that policy.

If the clock is unconfirmed, a clock jump invalidates it, the dependency/model
is missing or invalid, or the model date is outside 2025–2029, the bot says
**Compass unavailable** and supplies a clearly labelled true bearing. It never
calls a true bearing magnetic. Explicit `true` mode needs neither WMM nor a
trusted date. Magnetic caution/blackout zones are rejected rather than giving
an unreliable compass heading.

## Sources and verification

- [NOAA WMM model, software and validity](https://www.ncei.noaa.gov/products/world-magnetic-model).
- [pyGeoMag upstream implementation](https://github.com/boxpet/pygeomag), MIT.
- [Official NOAA reference values](https://www.ncei.noaa.gov/sites/default/files/2025-02/WMM2025_TEST_VALUES.txt).

The downloaded reference fixture is `tests/fixtures/wmm2025_test_values.txt`,
SHA256 `ae289c94e2200e4deeae2dfdedf543a8a0e93096512b30d6a754199731f570d0`.
NOAA WMM material is public domain. Dependency distributions retain their MIT
license. We use the dependency without modifying or vendoring its algorithm.
Tests compare all 12 official declination values (0.02° tolerance, respecting
their published rounding), east/west conversion, compass wrap/rounding,
date validity, weak-field rejection, parser modes and concise chat payloads.

At the end of 2029, update the model, coefficient hash, dependency/reference
values and validity guard together. Do not silently extrapolate WMM2025.
