"""Optional deterministic daylight calculations and explicitly allowed arithmetic."""
import math
from zoneinfo import ZoneInfo

from .clock import ClockUnavailable
from .geography import resolve_place


def daylight(latitude,longitude,day,zone):
    from astral import Observer
    from astral.sun import sunrise,sunset
    observer=Observer(latitude,longitude,elevation=0)
    rise=sunrise(observer,date=day,tzinfo=ZoneInfo(zone))
    setting=sunset(observer,date=day,tzinfo=ZoneInfo(zone))
    return rise,setting


def sun_reply(store,name,clock,zone):
    try: day=clock.now().astimezone(ZoneInfo(zone)).date()
    except ClockUnavailable: return 'Clock unconfirmed; operator must verify the host clock before dated daylight lookups.'
    place,source=resolve_place(store,name)
    try: rise,setting=daylight(place['latitude'],place['longitude'],day,zone)
    except ImportError: return 'Sun calculations require requirements-astronomy.txt; ask the operator to install it.'
    except ValueError: return 'Sunrise/sunset unavailable for that location/date.'
    hours=(setting-rise).total_seconds()/3600
    return [f'{place["name"]}, calculated {day}: sunrise {rise:%H:%M %Z}, sunset {setting:%H:%M %Z}; daylight approx {hours:.1f} h.',
            f'Astral model, unobstructed horizon at representative point; weather/terrain not modelled. Coordinates: {source["title"]}.']


def calc_reply(args):
    operations={'km-miles':1,'miles-km':1,'c-f':1,'f-c':1,'energy':2,'runtime':2,'daily-load':1,'solar-input':2}
    if not args or args[0].casefold() not in operations:
        return 'Calc: km-miles N | miles-km N | c-f N | f-c N | energy MAH VOLTS | runtime MAH MA | daily-load MA | solar-input MA HOURS'
    operation=args[0].casefold()
    if len(args)!=operations[operation]+1: return 'Wrong argument count. Use /bot calc for supported forms.'
    try:
        values=[float(v) for v in args[1:]]
        if any(not math.isfinite(v) or abs(v)>1e9 for v in values): raise ValueError()
        if operation not in ('c-f','f-c') and any(v<0 for v in values): raise ValueError()
        if operation=='runtime' and values[1]<=0: raise ValueError()
        if operation=='solar-input' and values[1]>24: raise ValueError()
        if operation=='energy' and values[1]<=0: raise ValueError()
    except ValueError: return 'Use finite numbers in the documented units; capacities/current/durations must be nonnegative and divisors positive.'
    if operation=='km-miles': return f'{values[0]:g} km = {values[0]/1.609344:g} miles'
    if operation=='miles-km': return f'{values[0]:g} miles = {values[0]*1.609344:g} km'
    if operation=='c-f': return f'{values[0]:g} C = {values[0]*9/5+32:g} F'
    if operation=='f-c': return f'{values[0]:g} F = {(values[0]-32)*5/9:g} C'
    if operation=='energy': return f'Nominal energy {values[0]*values[1]/1000:g} Wh from {values[0]:g} mAh at {values[1]:g} V; usable energy/cutoff/losses excluded.'
    if operation=='runtime': return f'Ideal runtime {values[0]/values[1]:g} h at constant {values[1]:g} mA battery load; cutoff, conversion losses and capacity variation excluded.'
    if operation=='daily-load': return f'Estimated daily draw {values[0]*24:g} mAh at constant average battery load {values[0]:g} mA for 24 h.'
    return f'Ideal battery charge {values[0]*values[1]:g} mAh at {values[0]:g} mA into battery for {values[1]:g} h; taper/weather/losses excluded. Not a net solar-power budget.'
