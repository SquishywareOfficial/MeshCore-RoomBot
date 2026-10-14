"""Offline reference/directory handlers; all replies go through the dispatcher."""
import logging
import sqlite3

from .data import DataError, OfflineStore
from .geography import (PlaceError, bearing_reply, distance_reply,
                        place_reply, towns_reply, walktime_reply)
from .tides import tide_reply
from .practical import calc_reply, sun_reply
from .routing import RoutingError


def install_offline_commands(dispatcher, data_path):
    from .tide_service import TideService
    national_tides = TideService(data_path, dispatcher.config.tides)
    router = None
    if dispatcher.config.routing.backend == 'routino':
        from .routing import RoutinoRouter
        router = RoutinoRouter(dispatcher.config.routing, data_path)
    def lookup(kind, ctx, args):
        if not args:
            return f'Usage: /bot {kind} TOPIC'
        term = ' '.join(args)
        if len(term) > 200:
            return 'Lookup too long; use a shorter topic.'
        store = None
        try:
            path = data_path / 'offline.sqlite3'
            if not path.exists():
                return 'No offline data installed. The operator must import a dataset first.'
            store = OfflineStore(path)
            rows = store.query(kind, term, ctx.config.limits.query_results)
        except (DataError, sqlite3.Error, OSError) as exc:
            logging.error('Offline lookup failed: %s', exc)
            return 'Offline data unavailable; ask the operator to check the dataset.'
        finally:
            if store is not None:
                store.close()
        if not rows:
            return 'No saved match for that topic. Try another keyword.'
        replies = []
        for record, source in rows:
            label = 'Saved reference' if kind in ('emergency', 'guide') else 'Saved directory; availability unconfirmed'
            location = f' ({record["location"]})' if kind == 'directory' else ''
            replies.append(f'{label}: {record["title"]}{location}\n{record["text"]}')
        if len(rows) == ctx.config.limits.query_results:
            replies.append('Query limit reached; narrow the topic for more specific results.')
        return replies

    for kind in ('directory', 'emergency', 'guide'):
        dispatcher.register(kind, f'/bot {kind} TOPIC',
                            lambda ctx, args, kind=kind: lookup(kind, ctx, args))

    def geographic(verb, ctx, args):
        details=[]
        dispatcher.remember_details(ctx.post.author,[])
        if verb == 'tides' and (data_path/'tides.sqlite3').exists():
            return national_tides.reply(args, ctx.clock)
        path = data_path / 'offline.sqlite3'
        if not path.exists():
            return ('Drinking-water safety unknown. Do not assume safe to drink. ' if verb=='water' else '')+'No offline place data installed. Ask the operator to import it.'
        store = None
        try:
            store = OfflineStore(path)
            if verb == 'place':
                result=place_reply(store, ' '.join(args),details) if args else 'Usage: /bot location place ORIGIN (place, street/locality or latitude,longitude)'
                dispatcher.remember_details(ctx.post.author,details)
                return result
            if verb == 'bearing':
                result=bearing_reply(store,args,ctx.clock,details)
                dispatcher.remember_details(ctx.post.author,details)
                return result
            if verb == 'towns':
                return towns_reply(store,args,min(3,ctx.config.limits.query_results))
            if verb == 'tides':
                return tide_reply(store,' '.join(args),ctx.clock,ctx.config.timezone)
            if verb == 'sun':
                return sun_reply(store,' '.join(args) or ctx.config.default_location,ctx.clock,ctx.config.timezone)
            if verb == 'water':
                from .water import water_reply
                result=water_reply(store,args,data_path,ctx.config.water,router,details=details)
                dispatcher.remember_details(ctx.post.author,details)
                return result
            result=distance_reply(store, args, router=router,details=details)
            dispatcher.remember_details(ctx.post.author,details)
            return result
        except PlaceError as exc:
            return ('Drinking-water safety unknown. Do not assume safe to drink. ' if verb=='water' else '')+str(exc)
        except RoutingError as exc:
            return 'Drinking-water safety unknown. Do not assume safe to drink. '+str(exc)
        except (DataError, sqlite3.Error, OSError) as exc:
            logging.error('Geographic lookup failed: %s', exc)
            return ('Drinking-water safety unknown. Do not assume safe to drink. ' if verb=='water' else '')+'Offline geographic data unavailable; ask the operator to check it.'
        finally:
            if store is not None:
                store.close()

    for verb, usage in [('place', '/bot location place NAME'), ('distance', '/bot location distance FROM to TO [walk|road|straight]'),
                        ('bearing','/bot location bearing [compass|true|both] FROM to TO'), ('towns','/bot location towns LOCATION'),
                        ('nearby','/bot location towns LOCATION'),
                        ('tides','/bot location tides COASTAL_LOCATION'),('sun','/bot location sun [LOCATION]'),
                        ('water','/bot location water ORIGIN [walk|straight]')]:
        if verb == 'nearby':
            dispatcher.register(verb, usage, lambda ctx,args: 'Nearby is now towns. Use /bot location towns LOCATION.', False)
        else:
            dispatcher.register(verb, usage, lambda ctx, args, verb=verb: geographic(verb, ctx, args))
    dispatcher.register('walktime','/bot location walktime KM [PACE_KM_PER_HOUR]', lambda ctx,args:walktime_reply(args))
    dispatcher.register('calc','/bot calc OPERATION VALUES',lambda ctx,args:calc_reply(args))
    from .sources import source_reply
    dispatcher.register('source','/bot source [TOPIC|ID]',lambda ctx,args:source_reply(data_path,args,router))
    dispatcher.register('details','/bot location details',lambda ctx,args:dispatcher.details(ctx.post.author) if not args else 'Usage: /bot location details')
