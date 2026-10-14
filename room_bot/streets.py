"""Read-only national named-road components; no network or address inference."""
import json
import math
from pathlib import Path
import re
import sqlite3

from .data import DataError, normalize
from .geography import straight_distance

APP_ID = 0x53575244
SUFFIXES = {'st':'street','rd':'road','ave':'avenue','dr':'drive','pl':'place',
            'cres':'crescent','tce':'terrace','ln':'lane','ct':'court'}


def street_key(name):
    words = normalize(name).split()
    if words:
        last=words[-1].rstrip('.')
        words[-1]=SUFFIXES.get(last,last)
    return ' '.join(words)


def representative(paths):
    """Explicit representative point halfway through sorted source way lengths.

    A branching road has no unique midpoint; this is a labelled representative
    point on its actual geometry, never inferred to be the user's position.
    """
    sections=[]
    total=0.0
    for path in paths:
        for a,b in zip(path,path[1:]):
            distance=straight_distance({'latitude':a[1],'longitude':a[2]},
                                       {'latitude':b[1],'longitude':b[2]})
            sections.append((a,b,distance));total+=distance
    if not sections or total<=0:raise DataError('Street has no usable geometry')
    target=total/2
    for a,b,distance in sections:
        if target<=distance and distance:
            fraction=target/distance
            # Normalize across the dateline instead of interpolating through 0.
            delta=(b[2]-a[2]+180)%360-180
            return a[1]+fraction*(b[1]-a[1]),(a[2]+fraction*delta+180)%360-180,total
        target-=distance
    raise DataError('Cannot locate street representative point')


class StreetStore:
    def __init__(self,path):
        self.path=Path(path).resolve()
        if not self.path.is_file():raise DataError('No offline street index installed')
        self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True)
        self.db.row_factory=sqlite3.Row
        try:
            if self.db.execute('PRAGMA application_id').fetchone()[0]!=APP_ID or self.db.execute('PRAGMA user_version').fetchone()[0]!=1:
                raise DataError('Unsupported street index')
            self.metadata=json.loads(self.db.execute('SELECT value FROM metadata WHERE key=?',('source',)).fetchone()[0])
            if not isinstance(self.metadata,dict) or any(not isinstance(self.metadata.get(k),str) or not self.metadata[k] for k in ('title','source_url','revision')):
                raise DataError('Invalid street source metadata')
            self.db.execute('PRAGMA query_only=ON')
        except (ValueError,TypeError,KeyError) as exc:
            self.db.close();raise DataError('Invalid street index metadata') from exc
        except Exception:
            self.db.close();raise

    def close(self):self.db.close()

    def find(self,name,anchor=None,radius_m=20000):
        if re.fullmatch(r'street:\d+',name):
            identifier=int(name.split(':')[1])
            if not 1<=identifier<=9223372036854775807:raise DataError('Invalid street ID')
            rows=self.db.execute('SELECT * FROM streets WHERE id=?',(identifier,)).fetchall()
        else:
            rows=self.db.execute('SELECT * FROM streets WHERE name_key=? ORDER BY id LIMIT 2001',(street_key(name),))
        results=[]
        for count,row in enumerate(rows):
            if count>=2000:raise DataError('Street name too common; use a street ID')
            item=dict(row)
            if anchor is not None:
                # Geometry proximity, not distance to a long street's midpoint.
                if len(item['paths'])>1048576:raise DataError('Street geometry exceeds lookup bounds; use coordinates')
                try:paths=json.loads(item['paths'])
                except ValueError as exc:raise DataError('Invalid street geometry') from exc
                distance=min(point_segment_distance(anchor,a,b) for path in paths for a,b in zip(path,path[1:]))
                if distance>radius_m:continue
                item['hint_distance_m']=distance
            results.append(item)
        return results


def point_segment_distance(point,a,b):
    # Local projection for candidate filtering only (not route distance).
    lat=math.radians(point['latitude']);scale=111195.0802
    def xy(vertex):
        lon=(vertex[2]-point['longitude']+180)%360-180
        return lon*scale*math.cos(lat),(vertex[1]-point['latitude'])*scale
    ax,ay=xy(a);bx,by=xy(b);dx,dy=bx-ax,by-ay
    fraction=max(0,min(1,-(ax*dx+ay*dy)/(dx*dx+dy*dy))) if dx or dy else 0
    return math.hypot(ax+fraction*dx,ay+fraction*dy)
