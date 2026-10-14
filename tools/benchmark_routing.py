"""Actual offline national-graph checks; temporary content/cache, never USB.

On Linux, measure each cold-process child with /usr/bin/time, then test the
adapter's restart-persistent cache separately. Network is denied inside Python;
Routino is a local native executable with no network lookup interface.
"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import resource
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_bot.config import Config, RoutingSettings
from room_bot.data import OfflineStore, read_bundle
from room_bot.geography import distance_reply, resolve_place, straight_distance
from room_bot.routing import RoutinoRouter, RoutingError, parse_route


PAIRS = [('Hamilton','Raglan'), ('Auckland','Whangārei'), ('Wellington','Porirua'),
         ('Christchurch','Rangiora, Canterbury'), ('Dunedin','Mosgiel'), ('Invercargill','Bluff'),
         ('Hamilton','Wellington'), ('Christchurch','Invercargill'),
         ('Wellington','Christchurch')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph',type=Path,required=True)
    parser.add_argument('--executable',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--timeout',type=int,default=90)
    args=parser.parse_args()
    def offline(*args,**kwargs):
        raise AssertionError('Network attempted during offline routing benchmark')
    socket.create_connection=offline
    settings=RoutingSettings(backend='routino',graph_dir=str(args.graph.resolve()),executable=args.executable,
                             timeout_seconds=args.timeout)
    report={'graph':str(args.graph),'executable':args.executable,'queries':[], 'usb_verified':False}
    with tempfile.TemporaryDirectory(prefix='room-bot-national-') as temp:
        root=Path(temp)
        store=OfflineStore(root/'offline.sqlite3')
        store.import_validated(*read_bundle(Path(__file__).resolve().parents[1]/'datasets/places/linz-settlements-2026-10-13.json'))
        router=RoutinoRouter(settings,root)
        before=time.monotonic()
        manifest=router.verify()
        report['verify_seconds']=time.monotonic()-before
        report['revision']=manifest['revision']
        report['graph_bytes']=sum(manifest['files'][n]['bytes'] for n in ('nodes.mem','segments.mem','ways.mem','relations.mem'))
        try:
            for origin,destination in PAIRS:
                a,_=resolve_place(store,origin)
                b,_=resolve_place(store,destination)
                row={'origin':origin,'destination':destination,
                     'origin_point':a,'destination_point':b,
                     'straight_km':straight_distance(a,b)/1000}
                started=time.monotonic()
                try:
                    result=router.route(a,b)
                except RoutingError as exc:
                    row['error']=str(exc)
                    if origin=='Wellington' and destination=='Christchurch':
                        assert 'No connected' in str(exc),exc
                    else:
                        raise
                else:
                    assert (origin,destination)!=('Wellington','Christchurch'),'Ferry incorrectly included'
                    row['result']=asdict(result)
                row['uncached_seconds']=time.monotonic()-started
                if 'result' in row:
                    restarted=RoutinoRouter(settings,root)
                    restarted.verify()
                    started=time.monotonic()
                    assert restarted.route(a,b)==result
                    row['cached_seconds']=time.monotonic()-started
                report['queries'].append(row)
                args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
                print(json.dumps(row,ensure_ascii=False),flush=True)
        finally:
            store.close()
        report['python_peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report['child_peak_rss_kib']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        report['notes']='Cold subprocess per uncached query; OS page cache may already be warm. Not power/radio measurements.'
        args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k!='queries'},indent=2))


if __name__=='__main__':
    main()
