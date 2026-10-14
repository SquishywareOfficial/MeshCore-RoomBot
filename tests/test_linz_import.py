from pathlib import Path
import unittest

from room_bot.data import DataError,read_bundle
from room_bot.geography import straight_distance
from tools.fetch_linz_places import feature_record


class LinzCoordinatesTests(unittest.TestCase):
    def test_authoritative_geometry_used_over_rounded_coordinate_attributes(self):
        # Source's Mosgiel attributes are -45,170, but its actual point geometry
        # is near -45.875435,170.347640 (NZTM-derived WGS84). Not a routing error.
        row={'attributes':{'name_id':3574,'name':'Mosgiel','feat_type':'Town',
                           'land_district':'Otago','crd_latitude':-45,'crd_longitude':170},
             'geometry':{'x':170.34763989698584,'y':-45.875435238659776}}
        result=feature_record(row)
        self.assertEqual(-45.875435238659776,result['latitude'])
        self.assertEqual(170.34763989698584,result['longitude'])
        del row['geometry']
        with self.assertRaises(DataError):
            feature_record(row)

    def test_geographic_bounds_use_geometry_not_attribute_fields(self):
        feature={'attributes':{'name_id':1,'name':'Synthetic','feat_type':'Town','crd_latitude':-40},
                 'geometry':{'x':170,'y':-70}}
        self.assertIsNone(feature_record(feature))
        feature['geometry']['y']=float('nan')
        with self.assertRaises(DataError):feature_record(feature)

    def test_supplied_mosgiel_point_is_near_dunedin_not_hundreds_of_km_away(self):
        _,rows=read_bundle(Path(__file__).resolve().parents[1]/'datasets/places/linz-settlements-2026-10-13.json')
        dunedin=next(r for r in rows if r['id']=='linz-nzgb:20040')
        mosgiel=next(r for r in rows if r['id']=='linz-nzgb:3574')
        self.assertLess(straight_distance(dunedin,mosgiel),15000)
