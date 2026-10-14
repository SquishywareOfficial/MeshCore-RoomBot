"""Local-first national tide lookups and bounded near-expiry source refresh."""
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

from .clock import ClockUnavailable
from .data import DataError
from .tide_data import (CSV_BASE, CATALOGUE_URL, PREDICTIONS_URL, TideStore,
                        catalogue_sites, create_store, import_annual, norm, secondary_rows)


def fetch_bytes(url, timeout=5, limit=100000):
    try:
        result = subprocess.run([sys.executable,str(Path(__file__).with_name('tide_http.py'))],
            input=json.dumps({'url':url,'timeout':timeout,'limit':limit}).encode(),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=timeout+1,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
        if not result.stdout or len(result.stdout)>limit:raise DataError('Tide response too large')
        return result.stdout
    except (OSError,subprocess.SubprocessError) as exc:
        raise DataError('Official tide source unavailable') from exc


def publication(page):
    raw = page.decode('utf-8')
    match = re.search(r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>',raw,re.S)
    if not match:raise DataError('Tide publication settings unavailable')
    settings = json.loads(match.group(1))['linz_map_snapshot_tp']
    last = settings['end']
    if type(last) is not int or not 2020<=last<=2100:raise DataError('Unsupported tide publication year')
    links = re.findall(r'href="([^"]*Secondary[-_]ports[-_]([0-9]{4})[-_]([0-9]{2})\.csv)"',raw,re.I)
    if not links:raise DataError('Secondary-port publication unavailable')
    link,start,end = max(links,key=lambda x:int(x[1]))
    if int(end)!=(int(start)+1)%100:raise DataError('Unsupported secondary edition')
    return last,urljoin(PREDICTIONS_URL,link),f'{start}-{end}',f'{int(start)+1}-07-01'


def select_site(sites, query):
    key = norm(query)
    canonical = [s for s in sites if norm(s['name']) == key]
    if len(canonical)==1:return canonical[0]
    matches = [s for s in sites if key in {norm(a) for a in s['aliases']}]
    if len(matches)==1:return matches[0]
    if len(matches)>1:raise DataError('Which tide site? ' + ' or '.join(s['name'] for s in matches[:3]) + '.')
    options = [s['name'] for s in sites if any(key in norm(a) for a in s['aliases'])][:4]
    raise DataError(('No exact tide site. Try ' + ' or '.join(options) + '.') if options else
                    'No official tide site matches. Use /bot location tides locations NAME to search.')


def next_at(stores, identifier, kind, now):
    # A newer overlay supersedes the same site/year, even if the event was removed.
    years = {}
    for store in stores:
        for year,source in store.years(identifier).items():years[year]=(store,source)
    found = []
    for year,(store,source) in years.items():
        row = store.db.execute('SELECT earliest,latest,height FROM events WHERE site=? AND year=? AND event=? AND latest>=? ORDER BY earliest LIMIT 1',
                               (identifier,year,kind,now)).fetchone()
        if row:found.append(row)
    return min(found,key=lambda r:r[0]) if found else None


def format_event(kind, row, site, now):
    if row is None:return f'Next {kind}: unavailable in published data.'
    early,late,height = row
    zone = ZoneInfo(site['zone'])
    first,last = datetime.fromtimestamp(early,zone),datetime.fromtimestamp(late,zone)
    if early != late:
        # Same local wall time occurs twice. Keep both timezone choices visible.
        instant = first.strftime('%a %d %b %H:%M ') + first.tzname() + '/' + last.tzname() + ' (DST uncertain)'
        if early < now:instant += '; may have passed'
    else:instant = first.strftime('%a %d %b %H:%M %Z')
    return f'{kind.title()}: {instant}' + (f' {height:.1f}m' if height is not None else '; height unavailable')


class TideService:
    def __init__(self, data_path, settings, fetch=fetch_bytes):
        self.root,self.settings,self.fetch = Path(data_path),settings,fetch

    def open_stores(self):
        base = TideStore(self.root/'tides.sqlite3')
        try:
            stores=[base]
            if (self.root/'tide-updates.sqlite3').exists():stores.append(TideStore(self.root/'tide-updates.sqlite3'))
            return stores
        except Exception:
            base.close();raise

    def needs_refresh(self, stores, site, now):
        identifier = site.get('reference_id',site['id'])
        sources = {}
        for store in stores:sources.update(store.years(identifier))
        day = now.astimezone(ZoneInfo(site['zone'])).date()
        if not sources or max(date.fromisoformat(s['end']) for s in sources.values())-day <= timedelta(days=self.settings.refresh_before_days):return True
        return site['type']=='offset' and date.fromisoformat(stores[-1].metadata['secondary_review_after'])-day <= timedelta(days=self.settings.refresh_before_days)

    def can_attempt(self, identifier, now):
        path=self.root/'tide-refresh.sqlite3'
        db=sqlite3.connect(path,timeout=2)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (site TEXT PRIMARY KEY,time REAL)')
            row=db.execute('SELECT time FROM attempts WHERE site=?',(identifier,)).fetchone()
            if row and 0<=now-row[0]<self.settings.retry_hours*3600:return False
            with db:db.execute('INSERT OR REPLACE INTO attempts VALUES (?,?)',(identifier,now))
            return True
        finally:db.close()

    def refresh(self, identifier, now):
        """Preserve immutable bundled DB; stage source updates into a separate overlay."""
        deadline=time.monotonic()+40
        def get(url,limit):
            remaining=deadline-time.monotonic()-1
            if remaining<.5:raise DataError('Tide refresh deadline exceeded')
            return self.fetch(url,min(self.settings.timeout_seconds,remaining),limit)
        last,secondary_url,revision,review = publication(get(PREDICTIONS_URL,400000))
        raw_secondary=get(secondary_url,200000)
        sites=catalogue_sites(get(CATALOGUE_URL,400000),secondary_rows(raw_secondary))
        site=next((s for s in sites if s['id']==identifier),None)
        if site is None:raise DataError('Requested tide site is no longer published')
        base=TideStore(self.root/'tides.sqlite3')
        old=None;stage=None;db=None
        try:
            if (self.root/'tide-updates.sqlite3').exists():old=TideStore(self.root/'tide-updates.sqlite3')
            metadata=dict(base.metadata)
            metadata.update(secondary_url=secondary_url,secondary_revision=revision,secondary_review_after=review,
                            prepared_at=now.isoformat(),overlay=True)
            fd,name=tempfile.mkstemp(prefix='.tide-update-',suffix='.sqlite3',dir=self.root);os.close(fd)
            stage=Path(name);stage.unlink()
            if old:
                db=sqlite3.connect(stage);old.db.backup(db)
                with db:
                    for key,value in metadata.items():db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(key,json.dumps(value)))
                    for s in sites:db.execute('INSERT OR REPLACE INTO sites VALUES (?,?,?)',(s['id'],s['name'],json.dumps(s,ensure_ascii=False)))
            else:db=create_store(stage,sites,metadata)
            standard_id=site.get('reference_id',site['id'])
            standard=next(s for s in sites if s['id']==standard_id)
            years=base.years(standard_id)
            if old:years.update(old.years(standard_id))
            first=now.astimezone(ZoneInfo(site['zone'])).year
            if last<first or last-first>8:raise DataError('No supported current tide publication')
            for year in range(first,last+1):
                if year in years:continue
                url=CSV_BASE+quote(standard['file']+' '+str(year)+'.csv')
                import_annual(db,get(url,100000),standard,year,url,now.isoformat())
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise DataError('Updated tide DB check failed')
            db.close();db=None
            if old:old.close();old=None
            os.replace(stage,self.root/'tide-updates.sqlite3');stage=None
        finally:
            base.close()
            if old:old.close()
            if db:db.close()
            if stage:stage.unlink(missing_ok=True)

    def reply(self, args, clock):
        if not args:return 'Usage: /bot location tides COASTAL_LOCATION | tides locations [NAME]'
        stores=[]
        try:
            stores=self.open_stores()
            sites={s['id']:s for store in stores for s in store.sites()}
            if args[0].casefold() in ('locations','sites'):
                term=norm(' '.join(args[1:]))
                names=sorted(s['name'] for s in sites.values() if not term or any(term in norm(a) for a in s['aliases']))
                if not names:return 'No matching tide sites.'
                return [f'{len(names)} LINZ tide sites match.']+names[:20]+(['More sites: narrow the locations search.'] if len(names)>20 else [])
            try:now=clock.now()
            except ClockUnavailable:return 'Clock unconfirmed; operator must verify the host clock before dated tide lookups.'
            site=select_site(list(sites.values()),' '.join(args))
            refresh_needed=self.needs_refresh(stores,site,now)
            if self.settings.auto_refresh and refresh_needed and self.can_attempt(site['id'],now.timestamp()):
                for store in stores:store.close()
                stores=[]
                try:self.refresh(site['id'],now)
                except (DataError,OSError,ValueError,KeyError,sqlite3.Error):logging.info('Tide source refresh unavailable; retaining saved predictions')
                stores=self.open_stores()
                site=next((s for s in stores[-1].sites() if s['id']==site['id']),site)
            standard_id=site.get('reference_id',site['id'])
            years={y:s for store in stores for y,s in store.years(standard_id).items()}
            local_day=now.astimezone(ZoneInfo(site['zone'])).date().isoformat()
            if not any(s['start']<=local_day<=s['end'] for s in years.values()):
                return 'No saved tide coverage for the current date; source update unavailable. No extrapolation.'
            replies=[]
            for kind in ('high','low'):
                delta=0;height=None
                if site['type']=='offset':
                    offset=site['offset'];delta=offset[kind+'_minutes']
                    if delta is None:
                        replies.append((float('inf'),f'Next {kind}: no published time offset for this site.'));continue
                row=next_at(stores,standard_id,kind,now.timestamp()-delta*60)
                if row and site['type']=='offset':
                    if offset['msl'] is not None and offset['ratio'] is not None:
                        height=offset['msl']+(row[2]-offset['standard_msl'])*offset['ratio']
                    row=(row[0]+delta*60,row[1]+delta*60,height)
                replies.append((row[0] if row else float('inf'),format_event(kind,row,site,now.timestamp())))
            label='offset estimates' if site['type']=='offset' else 'predictions'
            suffix=''
            if site['type']=='offset' and local_day>=stores[-1].metadata['secondary_review_after']:
                suffix=f' Older offsets: {stores[-1].metadata["secondary_revision"]}.'
            return [f'{site["name"]} tides (LINZ {label}).{suffix}\n'+'\n'.join(text for _,text in sorted(replies,key=lambda r:r[0]))]
        except (DataError,OSError,sqlite3.Error,ValueError,KeyError,TypeError) as exc:
            if isinstance(exc,DataError):return str(exc)
            logging.error('National tide data unavailable')
            return 'Tide data unavailable; ask the operator to check installation.'
        finally:
            for store in stores:store.close()
