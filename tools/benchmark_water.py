"""Real NZ water/route dispatcher checks, isolated from USB and the network."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import resource
import socket
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from room_bot.client import Post
from room_bot.commands import Dispatcher
from room_bot.config import Config,Limits,RoutingSettings
from room_bot.data import OfflineStore,read_bundle
from room_bot.offline import install_offline_commands
from room_bot.water import WARNING,WaterStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--water',type=Path,required=True)
    parser.add_argument('--streets',type=Path,required=True)
    parser.add_argument('--graph',type=Path,required=True)
    parser.add_argument('--executable',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    def no_network(*args,**kwargs):raise AssertionError('Network used')
    socket.create_connection=no_network
    report={'queries':[],'usb_verified':False,'network_used':False}
    water=WaterStore(args.water)
    try:report['source']=water.metadata
    finally:water.close()
    with tempfile.TemporaryDirectory(prefix='room-bot-water-') as temp:
        root=Path(temp)
        (root/'water.sqlite3').symlink_to(args.water.resolve())
        (root/'streets.sqlite3').symlink_to(args.streets.resolve())
        store=OfflineStore(root/'offline.sqlite3')
        store.import_validated(*read_bundle(Path(__file__).resolve().parents[1]/'datasets/places/linz-settlements-2026-10-13.json'))
        store.close()
        cfg=replace(Config(),enabled_commands=('water',),limits=Limits(request_cooldown_seconds=0),
                    routing=RoutingSettings(backend='routino',graph_dir=str(args.graph.resolve()),executable=args.executable))
        prefix='[SquishyBot] ';dispatch=Dispatcher(cfg,prefix);install_offline_commands(dispatch,root)
        commands=['/bot location water Hamilton','/bot location water -37.7313255,175.2215203',
                  '/bot location water Maui Street Hamilton','/bot location water street:32455893@point',
                  '/bot location water Christchurch','/bot location water Dunedin','/bot location water Wellington',
                  '/bot location water Raglan straight','/bot location water -43.9500,-176.5600 straight',
                  '/bot location water 0,0 straight']
        sequence=0; successes=0
        for command in commands:
            sequence+=1;started=time.monotonic()
            replies=dispatch(Post(sequence,0,0,'a'*64,0,command.encode()))
            first=list(replies);all_replies=[]
            assert replies[0].startswith(WARNING),(command,replies)
            for page in range(30):
                assert len(replies)<=4 and all(len((prefix+r).encode())<=151 for r in replies)
                more=bool(replies and replies[-1].startswith('More: /bot next'))
                all_replies.extend(replies[:-1] if more else replies)
                if not more:break
                sequence+=1
                replies=dispatch(Post(sequence,0,0,'a'*64,0,b'/bot next'))
            else:raise AssertionError('Water response page limit exceeded')
            text=''.join(all_replies)
            assert 'data unavailable' not in text,(command,text)
            if command.endswith('Maui Street Hamilton'):
                assert 'Ambiguous street' in text
            if command=='/bot location water 0,0 straight':
                assert 'No mapped approach' in text
            if 'walking; best tested approach' in text:
                successes+=1
                assert 'Water/access unverified' in text and 'Limited search' in text
                assert len(first)<=2
            assert 'https://' not in text and 'Source:' not in text
            row={'command':command,'seconds':time.monotonic()-started,'initial_replies':first,'full_reply_chunks':all_replies}
            report['queries'].append(row);print(json.dumps(row),flush=True)
        assert successes>=3,('Too few actual walking results',successes)
        report['walking_results']=successes
        report['python_peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report['router_peak_rss_kib']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    report['index_bytes']=args.water.stat().st_size
    digest=hashlib.sha256()
    with args.water.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):digest.update(block)
    report['index_sha256']=digest.hexdigest()
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('queries','source')}),flush=True)


if __name__=='__main__':main()
