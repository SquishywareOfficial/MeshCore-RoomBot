"""Software-only offline benchmark using temporary databases; never opens USB."""
from dataclasses import replace
from datetime import datetime,timezone
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from room_bot.clock import SessionClock
from room_bot.commands import Dispatcher
from room_bot.config import Config,Limits
from room_bot.data import OfflineStore,read_bundle
from room_bot.offline import install_offline_commands
from room_bot.community import install_community_commands
from types import SimpleNamespace

FILES=('references/getready-earthquakes.json','references/getready-floods.json',
       'places/linz-settlements-2026-10-13.json','routes/hamilton-raglan-2026-10-13.json',
       'tides/raglan-2026.json','tides/raglan-2027-jan-prefix.json','guides/xiao-enhanced-sq7.json')


def main():
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);store=OfflineStore(root/'offline.sqlite3')
        began=time.perf_counter()
        try:
            for name in FILES:store.import_validated(*read_bundle(ROOT/'datasets'/name))
        finally:store.close()
        import_seconds=time.perf_counter()-began
        config=replace(Config(),limits=Limits(request_cooldown_seconds=0),
                       enabled_commands=('ping','help','about','place','distance','bearing','towns',
                                         'walktime','tides','emergency','guide','sun','calc','notice','checkin','lastseen'))
        # A fixed known date is solely a benchmark fixture, not a host clock assertion.
        epoch=datetime(2026,6,3,0,tzinfo=timezone.utc).timestamp()
        clock=SessionClock(True,wall=lambda:epoch,monotonic=lambda:0)
        dispatch=Dispatcher(config,'[Bench] ',clock)
        dispatch.bind_room('01'+'00'*31)
        install_offline_commands(dispatch,root);install_community_commands(dispatch,root)
        commands=('/bot location place Hamilton','/bot location distance Hamilton to Raglan','/bot location towns Hamilton',
                  '/bot location tides Raglan','/bot emergency earthquake','/bot guide battery','/bot location sun',
                  '/bot calc runtime 1000 10','/bot checkin benchmark fixture')
        result={};sequence=0
        for command in commands:
            samples=[]
            for _ in range(20):
                sequence+=1
                request=SimpleNamespace(text=command,author='02'+'00'*31,sequence=sequence)
                start=time.perf_counter();replies=dispatch(request);samples.append(time.perf_counter()-start)
                if replies is None or len(replies)>4 or any(len(('[Bench] '+r).encode())>151 for r in replies):
                    raise RuntimeError('Benchmark response violated handler bounds')
            result[command]={'median_ms':round(statistics.median(samples)*1000,3),'max_ms':round(max(samples)*1000,3)}
        peak=None
        if sys.platform.startswith('linux'):
            import resource
            peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(json.dumps({'hostname':platform.node(),'architecture':platform.machine(),
                          'python':platform.python_version(),'import_seconds':round(import_seconds,3),
                          'content_database_bytes':(root/'offline.sqlite3').stat().st_size,
                          'peak_process_rss_kib_linux':peak,'lookup_measurements':result,
                          'scope':'20 software-only queries per command, temp DBs, fixed benchmark date, no USB or network calls'},indent=2))


if __name__=='__main__':main()
