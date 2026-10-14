"""Dataset provenance is opt-in, without network calls or routine radio footers."""
import logging
from pathlib import Path
import sqlite3

from .data import DataError,OfflineStore
from .streets import StreetStore
from .water import WaterStore
from .routing import RoutingError

ALIASES={'place':('places','streets'),'distance':('places','routes','routing'),
         'bearing':('places','streets'),'nearby':('places','streets'),
         'towns':('places','streets'),
         'water':('water',),'street':('streets',),'guide':('guide',),
         'tide':('tides',)}


def source_reply(data_path,args,router=None):
    if len(args)>1:return 'Usage: /bot source [TOPIC|ID]'
    topic=args[0].casefold() if args else None
    wanted=ALIASES.get(topic,(topic,)) if topic else None
    sources=[]
    try:
        path=Path(data_path)/'offline.sqlite3'
        if path.exists():
            store=OfflineStore(path)
            try:
                # Bounded metadata only; source lookup never reads message state.
                import json
                rows=store.db.execute('SELECT id,kind,metadata FROM datasets ORDER BY id LIMIT 101')
                for index,row in enumerate(rows):
                    if index>=100:raise DataError('Too many source datasets; inspect host-side metadata')
                    if wanted is None or row['kind'] in wanted or row['id'].casefold()==topic:
                        sources.append((row['id'],dict(json.loads(row['metadata']))))
            finally:store.close()
        for name,cls in (('streets',StreetStore),('water',WaterStore)):
            path=Path(data_path)/(name+'.sqlite3')
            if path.exists() and (wanted is None or name in wanted):
                store=cls(path)
                try:sources.append((name,dict(store.metadata)))
                finally:store.close()
        if router is not None and (wanted is None or 'routing' in wanted):
            if topic is None:
                sources.append(('routing',{'title':'Offline pedestrian graph'}))
            else:
                meta=dict(router.verify(),title='OpenStreetMap pedestrian graph',
                          license='Map data: OpenStreetMap contributors, ODbL 1.0; engine licensing in host documentation')
                sources.append(('routing',meta))
        if not sources:return 'No installed source matches that topic or ID.'
        if topic is None:
            return ['Sources available only on request. Use /bot source TOPIC or a listed ID.']+[
                f'{identifier}: {meta["title"]}' for identifier,meta in sources]
        return [f'{identifier}: {meta["title"]}. Snapshot: {meta.get("revision","not recorded")}. '
                f'{meta.get("source_url","")} License: {meta.get("license","See host documentation")}'
                for identifier,meta in sources]
    except (DataError,RoutingError,sqlite3.Error,OSError,ValueError,TypeError,KeyError) as exc:
        logging.error('Source lookup failed: %s',exc)
        return 'Source metadata unavailable; ask the operator to inspect the installed data.'
