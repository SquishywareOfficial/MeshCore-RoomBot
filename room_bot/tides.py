"""Strict LINZ local-time CSV conversion and offline next-high/low queries."""
import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
import io
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .clock import ClockUnavailable
from .data import DataError, normalize, validate_bundle


def local_instant(day, clock_text, zone):
    try:
        naive = datetime.combine(day,datetime.strptime(clock_text,'%H:%M').time())
    except ValueError as exc:
        raise DataError('Invalid tide time') from exc
    choices = {}
    for fold in (0,1):
        candidate=naive.replace(tzinfo=zone,fold=fold)
        utc=candidate.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None)==naive:
            choices[utc]=candidate
    if len(choices)!=1:
        raise DataError(f'Ambiguous/nonexistent local time {naive}; explicit source clarification required')
    return next(iter(choices.values()))


def convert_csv(raw, place, source_url, retrieved_at, start=None, end=None, zone_name='Pacific/Auckland'):
    if len(raw)>100000:
        raise DataError('Annual tide CSV exceeds supported size')
    rows=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    if len(rows)<4 or len(rows[0])<2 or rows[0][1].strip()!=place:
        raise DataError('CSV does not identify the requested place')
    if 'Local Std or Daylight Time' not in ','.join(rows[2]):
        raise DataError('Unsupported tide time convention')
    zone=ZoneInfo(zone_name)
    events,days=[],[]
    for row in rows[3:]:
        if not row:
            continue
        if len(row)!=12:
            raise DataError('Expected four date fields and four time/height pairs')
        try:
            day=date(int(row[3]),int(row[2]),int(row[0]))
        except ValueError as exc:
            raise DataError('Invalid tide date') from exc
        if days and day!=days[-1]+timedelta(days=1):
            raise DataError('CSV dates must be contiguous without duplicates')
        days.append(day)
        if start and day<start or end and day>end:
            continue
        count=0
        for column in range(4,12,2):
            clock_text,height=row[column:column+2]
            if not clock_text and not height:
                continue
            if not clock_text or not height:
                raise DataError('Incomplete tide time/height pair')
            instant=local_instant(day,clock_text,zone)
            try:
                height=float(height)
            except ValueError as exc:
                raise DataError('Invalid tide height') from exc
            events.append((instant,height))
            count+=1
        if not 2<=count<=4:
            raise DataError('Expected 2..4 tide extrema per included day')
    if not events:
        raise DataError('No tide events in selected coverage')
    events.sort(key=lambda row:row[0].astimezone(timezone.utc))
    records=[]
    for index,(instant,height) in enumerate(events):
        if index and instant<=events[index-1][0]:
            raise DataError('Duplicate or unordered tide instants')
        neighbours=[]
        if index: neighbours.append(events[index-1][1])
        if index+1<len(events): neighbours.append(events[index+1][1])
        if all(height>h for h in neighbours): event='high'
        elif all(height<h for h in neighbours): event='low'
        else: raise DataError('Heights do not establish alternating extrema; do not guess high/low')
        if records and records[-1]['event']==event:
            raise DataError('Tide event sequence must alternate')
        records.append({'id':place+'-'+instant.isoformat(),'place_name':place,'time':instant.isoformat(),
                        'event':event,'height_m':height,'datum':'Source CSV datum not specified'})
    first,last=events[0][0].date(),events[-1][0].date()
    result={'format_version':1,'dataset':{
        'id':f'linz-tides-{normalize(place)}-{first}-{last}','kind':'tides',
        'title':'Toitū Te Whenua Land Information New Zealand tide predictions',
        'source_url':source_url,'license':'Based on LINZ data licensed for reuse under CC BY 4.0; https://www.linz.govt.nz/copyright',
        'revision':f'{first.year} table; '+','.join(rows[1]),'retrieved_at':retrieved_at,
        'coverage':f'{place}, {first}..{last}; published local standard/daylight times; prediction heights only. Datum not stated in CSV.',
        'timezone':zone_name,'coverage_start':str(first),'coverage_end':str(last)},'records':records}
    validate_bundle(result)
    return result


def tide_reply(store, place, clock, display_zone):
    if not place:
        return 'Usage: /bot location tides COASTAL_LOCATION'
    try:
        now=clock.now()
    except ClockUnavailable:
        return 'Clock unconfirmed; operator must verify the host clock before dated tide lookups.'
    rows=[(record,source) for record,source in store.iter_kind('tides',term=place)
          if normalize(record['place_name'])==normalize(place)]
    if not rows:
        return 'No saved predictions for that exact coastal location.'
    sources={source['id']:source for _,source in rows}
    covered=[]
    for identifier,source in sources.items():
        if not all(key in source for key in ('timezone','coverage_start','coverage_end')):
            continue
        local_day=now.astimezone(ZoneInfo(source['timezone'])).date()
        if source['coverage_start']<=str(local_day)<=source['coverage_end']:
            covered.append(identifier)
    if not covered:
        return 'No saved tide coverage for the current local date; no extrapolation is available.'
    if len(covered)>1:
        return 'Overlapping tide datasets; operator must select one reviewed source.'
    forthcoming=sorted(((datetime.fromisoformat(record['time'].replace('Z','+00:00')),record,source) for record,source in rows
                         if datetime.fromisoformat(record['time'].replace('Z','+00:00'))>=now),key=lambda item:item[0])
    result=[]
    for kind in ('high','low'):
        found=next((row for row in forthcoming if row[1]['event']==kind),None)
        if found is None:
            result.append(f'Next {kind} unavailable within saved coverage.')
        else:
            instant,record,source=found
            result.append(f'{record["place_name"]}: predicted {kind} {instant.astimezone(ZoneInfo(display_zone)):%Y-%m-%d %H:%M %Z}, '
                          f'{record["height_m"]:.1f} m; {record["datum"]}.')
    result.append('Saved predictions, not observed sea level or a surge/tsunami warning.')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='Convert an explicitly downloaded LINZ daily prediction CSV')
    parser.add_argument('csv',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--place',required=True);parser.add_argument('--source-url',required=True)
    parser.add_argument('--retrieved-at',required=True)
    parser.add_argument('--start',type=date.fromisoformat);parser.add_argument('--end',type=date.fromisoformat)
    args=parser.parse_args(argv)
    try:
        if args.output.exists(): raise DataError('Output exists; choose a new snapshot filename')
        if args.start and args.end and args.start>args.end: raise DataError('Date range is reversed')
        with args.csv.open('rb') as stream: raw=stream.read(100001)
        value=convert_csv(raw,args.place,args.source_url,args.retrieved_at,args.start,args.end)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n')
        print(f'Converted {len(value["records"])} tide events: {value["dataset"]["coverage"]}')
        return 0
    except (DataError,OSError,UnicodeError) as exc:
        print(f'Tide conversion failed: {exc}')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
