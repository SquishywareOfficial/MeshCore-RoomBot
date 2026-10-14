"""Explicit, resumable national LINZ tide gathering and verified release packaging."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import quote
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from room_bot.tide_data import (CATALOGUE_URL, CSV_BASE, PREDICTIONS_URL, catalogue_sites,
                                secondary_rows, create_store, import_annual, check_store)
from room_bot.tide_service import fetch_bytes, publication


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def gather(root, first_year, last_year=None, jobs=4):
    root.mkdir(parents=True,exist_ok=True)
    source=root/'sources';source.mkdir(exist_ok=True)
    page=fetch_bytes(PREDICTIONS_URL,15,400000)
    published,secondary_url,revision,review=publication(page)
    last_year=published if last_year is None else last_year
    if not first_year<=last_year<=published or last_year-first_year>8:
        raise ValueError('Choose years within the current published range (maximum nine years)')
    secondary=fetch_bytes(secondary_url,15,200000)
    catalogue=fetch_bytes(CATALOGUE_URL,15,400000)
    (source/'secondary.csv').write_bytes(secondary)
    (source/'catalogue.json').write_bytes(catalogue)
    sites=catalogue_sites(catalogue,secondary_rows(secondary))
    tasks=[(s,y) for s in sites if s['type']=='daily' for y in range(first_year,last_year+1)]
    def get(task):
        s,y=task;path=source/f'{s["id"]}-{y}.csv'
        url=CSV_BASE+quote(s['file']+' '+str(y)+'.csv')
        receipt=path.with_suffix('.receipt.json')
        record=json.loads(receipt.read_text()) if receipt.exists() else None
        if not path.exists() or not record or record['sha256']!=sha(path):
            raw=fetch_bytes(url,15,100000)
            # Validate the entire annual table before caching/activating it.
            from room_bot.tide_data import parse_annual
            parse_annual(raw,s,y)
            path.write_bytes(raw)
            record={'url':url,'retrieved_at':datetime.now(timezone.utc).isoformat(),'bytes':len(raw),'sha256':sha(path)}
            receipt.write_text(json.dumps(record),encoding='utf-8')
        return s,y,path,record
    collected=[]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        pending={pool.submit(get,t):t for t in tasks}
        for i,f in enumerate(as_completed(pending),1):
            collected.append(f.result())
            if i%25==0:print(f'Gathered {i}/{len(tasks)} verified annual files',flush=True)
    path=root/'tides.sqlite3'
    db=create_store(path,sites,{'prepared_at':datetime.now(timezone.utc).isoformat(),'years':list(range(first_year,last_year+1)),
        'secondary_url':secondary_url,'secondary_revision':revision,'secondary_review_after':review,
        'source_url':PREDICTIONS_URL,'license':'LINZ data, CC BY 4.0; https://www.linz.govt.nz/copyright'})
    provenance=[]
    try:
        for s,y,p,record in sorted(collected,key=lambda x:(x[0]['id'],x[1])):
            imported=import_annual(db,p.read_bytes(),s,y,record['url'],record['retrieved_at'])
            provenance.append({'site':s['id'],'year':y,**imported})
    finally:db.close()
    (source/'provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')
    return check_store(path)


def package(root, identifier):
    report=check_store(root/'tides.sqlite3')
    notices=root/'TIDE-NOTICES.md'
    notices.write_text('''# National tide data

Toitū Te Whenua Land Information New Zealand tide predictions and secondary-port tables.
LINZ data reused under CC BY 4.0: https://www.linz.govt.nz/copyright
Source: https://www.linz.govt.nz/products-services/tides-and-tidal-streams/tide-predictions
Original annual CSVs, secondary offsets, catalogue and source hashes are included.
The SQLite index is derived from these sources. Heights are predictions, not sea-level observations.
Secondary results are mean-offset estimates; absent offsets/heights stay unavailable.
Repeated daylight-saving times retain both possible instants; no fold is silently guessed.
''',encoding='utf-8')
    files=[root/'tides.sqlite3',notices,*sorted((root/'sources').glob('*.csv')),
           root/'sources/catalogue.json',root/'sources/provenance.json']
    archive=root/(identifier+'.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:z.write(p,p.relative_to(root).as_posix())
    manifest={'version':1,'id':identifier,'coverage':report,
              'archive':{'url':None,'bytes':archive.stat().st_size,'sha256':sha(archive)},
              'files':{p.relative_to(root).as_posix():{'bytes':p.stat().st_size,'sha256':sha(p)} for p in files}}
    (root/'nz-tides.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return manifest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--first-year',type=int,default=datetime.now(timezone.utc).year)
    p.add_argument('--last-year',type=int)
    p.add_argument('--jobs',type=int,choices=range(1,5),default=4)
    p.add_argument('--id',required=True)
    p.add_argument('--package-only',action='store_true',help='Package an already gathered and verified tide database')
    a=p.parse_args(argv)
    if not __import__('re').fullmatch(r'nz-tides-[a-z0-9-]{1,64}',a.id):p.error('Use a versioned nz-tides-... ID')
    if not a.package_only:
        if (a.output/'tides.sqlite3').exists():p.error('Existing database preserved; use --package-only or a new output folder')
        print(json.dumps(gather(a.output,a.first_year,a.last_year,a.jobs),indent=2),flush=True)
    m=package(a.output,a.id)
    print('Prepared',m['id'],'ZIP bytes',m['archive']['bytes'],'SHA256',m['archive']['sha256'])


if __name__=='__main__':main()
