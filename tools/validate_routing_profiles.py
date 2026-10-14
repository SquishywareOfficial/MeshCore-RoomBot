"""Build deterministic tiny OSM graphs and exercise the real Routino engine."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_bot.config import RoutingSettings
from room_bot.routing import RoutinoRouter, RoutingError


def fixture(path, middle_tags, barrier=False):
    osm = ET.Element('osm', {'version':'0.6','generator':'MeshCore-RoomBot synthetic routing test'})
    for i, lon in enumerate((175,175.001,175.019,175.020),1):
        node=ET.SubElement(osm,'node',{'id':str(i),'lat':'-37','lon':str(lon),'version':'1'})
        if barrier and i==2:
            ET.SubElement(node,'tag',{'k':'barrier','v':'wall'})
    for ident, refs, tags in ((10,(1,2),{'highway':'residential'}),
                              (11,(2,3),middle_tags),(12,(3,4),{'highway':'residential'})):
        way=ET.SubElement(osm,'way',{'id':str(ident),'version':'1'})
        for node in refs:
            ET.SubElement(way,'nd',{'ref':str(node)})
        for key,value in tags.items():
            ET.SubElement(way,'tag',{'k':key,'v':value})
    path.write_bytes(ET.tostring(osm,encoding='utf-8',xml_declaration=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path)
    args=parser.parse_args()
    origin={'id':'a','latitude':-37,'longitude':175}
    dest={'id':'b','latitude':-37,'longitude':175.020}
    scenarios=[('residential',{'highway':'residential'},False,True),
               ('footpath',{'highway':'footway','foot':'yes'},False,True),
               ('private',{'highway':'residential','access':'private'},False,False),
               ('foot-no',{'highway':'residential','foot':'no'},False,False),
               ('foot-private',{'highway':'residential','foot':'private'},False,False),
               ('motorroad',{'highway':'primary','motorroad':'yes'},False,False),
               ('motorway',{'highway':'motorway'},False,False),
               ('ferry',{'route':'ferry','foot':'yes'},False,False),
               ('conditional',{'highway':'residential','foot:conditional':'yes @ (Mo-Fr)'},False,False),
               ('access-conditional',{'highway':'residential','access:conditional':'no @ (Su)'},False,False),
               ('barrier',{'highway':'residential'},True,False),
               ('oneway-foot',{'highway':'residential','oneway':'yes'},False,True)]
    with tempfile.TemporaryDirectory(prefix='room-bot-profiles-') as temporary:
        root=Path(temporary)
        for name,tags,barrier,allowed in scenarios:
            input_path=root/(name+'.osm')
            fixture(input_path,tags,barrier)
            graph=root/name
            subprocess.run([sys.executable,str(Path(__file__).with_name('build_routing_graph.py')),
                '--source',str(args.source),'--input',str(input_path),'--output',str(graph),
                '--source-url','https://example.invalid/synthetic','--revision',name,
                '--coverage','synthetic fixture only','--sort-ram-mb','64'],check=True,stdout=subprocess.DEVNULL)
            router=RoutinoRouter(RoutingSettings(backend='routino',graph_dir=str(graph),
                executable=str(args.source/'src/router-slim'),max_snap_metres=10),root)
            try:
                result=router.route(dest,origin) if name=='oneway-foot' else router.route(origin,dest)
            except RoutingError as exc:
                if allowed:
                    raise AssertionError(f'{name} unexpectedly failed: {exc}') from exc
                if 'No connected' not in str(exc) and 'endpoint' not in str(exc):
                    raise
                print(f'{name}: correctly unavailable ({exc})')
            else:
                if not allowed:
                    raise AssertionError(f'{name} unexpectedly allowed: {result}')
                assert 1500 < result.distance_m < 2100,result
                print(f'{name}: PASS ({result.distance_m:g} m)')
    print(f'All {len(scenarios)} real engine/profile scenarios passed.')


if __name__=='__main__':
    main()
