"""Water geometry policies shared by desktop indexing and local queries."""
from .geography import straight_distance

PATH_TYPES={'residential','living_street','unclassified','tertiary','secondary','primary',
            'service','pedestrian','footway','path','cycleway','track','steps'}
YES={'yes','true','1'}


def restricted(tags):
    return (any(k in tags for k in ('access:conditional','foot:conditional')) or
            tags.get('foot') in ('no','private','customers','permit','agricultural','forestry','use_sidepath','destination') or
            tags.get('access') not in (None,'yes','permissive','designated','public'))


def water_kind(tags,area=False):
    if (restricted(tags) or tags.get('tidal') in YES or tags.get('salt') in YES or
            tags.get('salt_water') in YES or tags.get('salinity') not in (None,'fresh','freshwater','no','0') or
            tags.get('landuse') in ('industrial','reservoir','basin') or
            tags.get('usage') in ('industrial','wastewater','irrigation','stormwater') or
            tags.get('water') in ('wastewater','salt','saltwater','brackish','pond','reservoir','basin','canal') or
            tags.get('waterway') in ('drain','ditch','canal','tidal_channel')):return None
    if area:
        if tags.get('natural')=='water' and tags.get('water') in ('lake','river'):return tags['water']
        if tags.get('waterway')=='riverbank':return 'river'
    elif tags.get('waterway')=='stream':return 'stream'
    return None


def path_allowed(tags):
    return (tags.get('highway') in PATH_TYPES and not restricted(tags) and
            tags.get('bridge') in (None,'no') and tags.get('tunnel') in (None,'no') and
            tags.get('ford') in (None,'no') and tags.get('motorroad') not in YES and
            tags.get('footway')!='crossing' and tags.get('indoor') not in YES)


def blocked(tags):
    return (tags.get('natural')=='cliff' or tags.get('barrier') not in (None,'no') or
            tags.get('bridge') not in (None,'no') or tags.get('tunnel') not in (None,'no') or
            tags.get('ford') not in (None,'no'))


def segment_approach(segment,water,blockers=()):
    """A path point near real boundary/line; no claimed bank/collection access."""
    from shapely.ops import nearest_points
    if segment.intersects(water):return None
    boundary=water if water.geom_type=='LineString' else water.boundary
    pathpoint,waterpoint=nearest_points(segment,boundary)
    a={'latitude':pathpoint.y,'longitude':pathpoint.x};b={'latitude':waterpoint.y,'longitude':waterpoint.x}
    gap=straight_distance(a,b)
    if not 2<=gap<=40:return None
    for obstacle in blockers:
        closest=nearest_points(pathpoint,obstacle)[1]
        if straight_distance(a,{'latitude':closest.y,'longitude':closest.x})<=40:return None
    return a,b,gap
