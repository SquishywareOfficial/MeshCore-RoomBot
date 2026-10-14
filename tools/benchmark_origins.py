"""Real national street/coordinate dispatcher smoke tests; never USB or network."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import resource
import socket
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from room_bot.commands import Dispatcher
from room_bot.client import Post
from room_bot.config import Config,Limits,RoutingSettings
from room_bot.data import OfflineStore,read_bundle
from room_bot.offline import install_offline_commands


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--streets',type=Path,required=True)
    parser.add_argument('--graph',type=Path,required=True)
    parser.add_argument('--executable',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    def no_network(*args,**kwargs):raise AssertionError('Network used')
    socket.create_connection=no_network
    report={'queries':[],'usb_verified':False}
    with tempfile.TemporaryDirectory(prefix='room-bot-origins-') as temp:
        root=Path(temp);(root/'streets.sqlite3').symlink_to(args.streets.resolve())
        store=OfflineStore(root/'offline.sqlite3')
        store.import_validated(*read_bundle(Path(__file__).resolve().parents[1]/'datasets/places/linz-settlements-2026-10-13.json'));store.close()
        cfg=replace(Config(),enabled_commands=('place','distance','bearing','towns'),limits=Limits(request_cooldown_seconds=0),
            routing=RoutingSettings(backend='routino',graph_dir=str(args.graph.resolve()),executable=args.executable))
        prefix='[SquishyBot] ';dispatch=Dispatcher(cfg,prefix);install_offline_commands(dispatch,root)
        cases=[('/bot location place Maui Street Hamilton','Ambiguous street'),
               ('/bot location distance street:32455893@point to Raglan','45.7 km'),
               ('/bot location distance -37.7313255, 175.2215203 to street:32455893@point','0.9 km'),
               ('/bot location distance Hamilton to street:32455893@point','mapped walking route'),
               ('/bot location place Queen Street Auckland','Ambiguous street'),
               ('/bot location bearing -37.7313255,175.2215203 to street:32455893@point','initial bearing'),
               ('/bot location towns -37.7313255,175.2215203','straight-line ranking')]
        for sequence,(command,expected) in enumerate(cases,1):
            started=time.monotonic();replies=dispatch(Post(sequence,0,0,'a'*64,0,command.encode()))
            assert expected in ''.join(replies),(command,replies)
            assert len(replies)<=4 and all(len((prefix+t).encode())<=151 for t in replies)
            row={'command':command,'seconds':time.monotonic()-started,'replies':replies}
            report['queries'].append(row);print(json.dumps(row),flush=True)
        report['python_peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report['router_peak_rss_kib']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        report['index_bytes']=args.streets.stat().st_size
    args.output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='queries'}))


if __name__=='__main__':main()
