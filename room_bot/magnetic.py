"""Offline WMM2025 declination, with pinned coefficients and bounded validity."""
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import math
from pathlib import Path


COEFFICIENT_SHA256 = 'dfa8597825af4e0b87ff4198a5b4fb661b3c49f4cd090cd0164e0259b075582f'


class MagneticUnavailable(ValueError):
    pass


def decimal_year(day):
    if day.tzinfo is None:
        raise MagneticUnavailable('date has no timezone')
    day = day.astimezone(timezone.utc)
    start = datetime(day.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(day.year + 1, 1, 1, tzinfo=timezone.utc)
    return day.year + (day - start).total_seconds() / (end - start).total_seconds()


@lru_cache(maxsize=1)
def _model():
    try:
        import pygeomag
        from pygeomag import GeoMag
    except ImportError as exc:
        raise MagneticUnavailable('install pygeomag from requirements.txt') from exc
    path = Path(pygeomag.__file__).parent / 'wmm' / 'WMM_2025.COF'
    try:
        if path.stat().st_size > 8192:
            raise MagneticUnavailable('WMM2025 coefficients invalid')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != COEFFICIENT_SHA256:
            raise MagneticUnavailable('WMM2025 coefficients do not match')
        # pygeomag resolves relative paths against its package on both Windows/Linux.
        model = GeoMag(coefficients_file='wmm/WMM_2025.COF')
        if model.model != 'WMM-2025' or model.life_span != (2025.0, 2030.0):
            raise MagneticUnavailable('WMM2025 model invalid')
        return model
    except (OSError, ValueError) as exc:
        if isinstance(exc, MagneticUnavailable):
            raise
        raise MagneticUnavailable('WMM2025 data unavailable') from exc


def declination(latitude, longitude, year, altitude_km=0):
    """East-positive variation; altitude is WGS84 ellipsoid height in km.

    Normal chat queries use zero height: the catalogue has no elevation data.
    The date/model are never extrapolated, and unreliable polar zones are rejected.
    """
    values = (latitude, longitude, year, altitude_km)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise MagneticUnavailable('invalid model input')
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise MagneticUnavailable('invalid coordinates')
    if not 2025 <= year < 2030:
        raise MagneticUnavailable('date outside WMM2025 validity (2025-2029)')
    if not -1 <= altitude_km <= 850:
        raise MagneticUnavailable('altitude outside model bounds')
    model = _model()
    from pygeomag import BlackoutZoneException, CautionZoneException
    try:
        result = model.calculate(glat=latitude, glon=longitude, alt=altitude_km,
                                 time=year, raise_in_warning_zone=True)
    except (BlackoutZoneException, CautionZoneException) as exc:
        raise MagneticUnavailable('magnetic field too weak for a reliable compass') from exc
    except (OSError, ValueError, ArithmeticError) as exc:
        raise MagneticUnavailable('magnetic model calculation failed') from exc
    if not math.isfinite(result.d):
        raise MagneticUnavailable('magnetic model returned an invalid angle')
    return result.d


def compass_bearing(true_bearing, east_declination):
    if any(type(v) not in (int, float) or not math.isfinite(v)
           for v in (true_bearing, east_declination)):
        raise MagneticUnavailable('invalid bearing')
    return (true_bearing - east_declination) % 360
