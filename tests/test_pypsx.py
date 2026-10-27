import sys,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pypsx_provider as provider
from engine import KARACHI,features
NOW=datetime(2026,10,17,12,tzinfo=KARACHI)
def candles():return [[(NOW-timedelta(days=90-i)).replace(hour=10).isoformat(),10,11,9,10.5,10000] for i in range(90)]
def payload(rows):return {'results':[{'symbol':'BOP','interval':'5m','candles':rows,'gaps':[]}]}
class PyPSXTests(unittest.TestCase):
    def test_documented_intraday_shape(self):
        bars,omitted=provider.normalize_history(payload(candles()),'BOP','5m',NOW)
        self.assertEqual(len(bars),90);self.assertEqual(omitted,0)
    def test_forming_candle_omitted(self):
        rows=candles()+[[NOW.isoformat(),10,11,9,10,100]]
        bars,omitted=provider.normalize_history(payload(rows),'BOP','5m',NOW)
        self.assertEqual(len(bars),90);self.assertEqual(omitted,1)
    def test_naive_intraday_rejected(self):
        rows=candles();rows[0][0]=rows[0][0][:-6]
        with self.assertRaisesRegex(ValueError,'UTC offset'):provider.normalize_history(payload(rows),'BOP','5m',NOW)
    def test_daily_completed_dates_and_score(self):
        rows=[dict(zip(('date','open','high','low','close','volume'),[r[0][:10],*r[1:]])) for r in candles()]
        rows.append({'date':'2026-10-17','open':10,'high':11,'low':9,'close':10,'volume':100})
        bars,omitted=provider.normalize_history({'data':rows},'BOP','1d',NOW)
        self.assertEqual(omitted,1);self.assertTrue(bars[-1]['time'].endswith('+05:00'))
        f=features(bars,timeframe='1d');self.assertIsNone(f['vwap']);self.assertEqual(f['coverage'],70)
    def test_missing_high_low_rejected(self):
        rows=[{'time':r[0],'open':10,'close':10,'volume':100} for r in candles()]
        with self.assertRaisesRegex(ValueError,'high/low'):provider.normalize_history(rows,'BOP','5m',NOW)
    def test_reported_gaps_rejected(self):
        p=payload(candles());p['results'][0]['gaps']=[{'reason':'data_corruption'}]
        with self.assertRaisesRegex(ValueError,'gaps'):provider.normalize_history(p,'BOP','5m',NOW)
    def test_duplicate_candles_rejected(self):
        rows=candles();rows.append(rows[-1])
        with self.assertRaisesRegex(ValueError,'ascending'):provider.normalize_history(payload(rows),'BOP','5m',NOW)
    def test_wrong_symbol_rejected(self):
        with self.assertRaisesRegex(ValueError,'matching'):provider.normalize_history(payload(candles()),'HBL','5m',NOW)
    def test_missing_credentials(self):
        with patch.object(provider,'config',return_value=({},'paper')):
            with self.assertRaisesRegex(ValueError,'credentials are missing'):provider.get_json('/historical/BOP',{})
    def test_status_hides_credentials(self):
        with patch.object(provider,'config',return_value=({'PYPSX_API_KEY_ID':'hidden-key','PYPSX_API_SECRET_KEY':'hidden-secret'},'paper')):
            result=provider.status();self.assertTrue(result['configured']);self.assertNotIn('hidden',str(result))
    def test_auth_failure_sanitized(self):
        from urllib.error import HTTPError
        with patch.object(provider,'config',return_value=({'PYPSX_API_KEY_ID':'hidden','PYPSX_API_SECRET_KEY':'secret'},'paper')),patch.object(provider,'build_opener') as factory:
            factory.return_value.open.side_effect=HTTPError('https://paper-api.pypsx.com/test',403,'secret',{},None)
            with self.assertRaisesRegex(ValueError,'HTTP 403') as raised:provider.get_json('/historical/BOP',{})
            self.assertNotIn('secret',str(raised.exception))
