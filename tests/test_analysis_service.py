import sys,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import KARACHI
from analysis_service import profile,four_hour_bars,volume_context,build_analysis,AnalysisService,fetch_research_history
NOW=datetime(2026,10,23,17,tzinfo=KARACHI)
def bar(t,close=10,volume=100):return {'time':t.isoformat(),'open':10,'high':12,'low':8,'close':close,'volume':volume}
class AnalysisTests(unittest.TestCase):
 def test_profiles_separate_window_and_candle_interval(self):
  self.assertEqual(profile('3M','auto')[:2],('1d','1d'))
  self.assertEqual(profile('1M','auto')[0],'1h')
  self.assertEqual(profile('5D','auto')[0],'15m')
  self.assertEqual(profile('3M','4h')[:2],('4h','15m'))
  with self.assertRaises(ValueError):profile('3M','1M')
 def test_friday_sessions_are_separate_and_not_four_hour_lunch_bars(self):
  bars=[bar(NOW.replace(hour=9,minute=15)),bar(NOW.replace(hour=11,minute=45),11,200),bar(NOW.replace(hour=14,minute=30),10,300),bar(NOW.replace(hour=16,minute=15),9,400)]
  out=four_hour_bars(bars,NOW)
  self.assertEqual(len(out),2);self.assertEqual(out[0]['volume'],300);self.assertEqual(out[1]['volume'],700)
  self.assertIn('14:30',out[1]['time']);self.assertEqual(out[1]['close'],9)
 def test_unfinished_four_hour_bucket_excluded(self):
  t=NOW.replace(day=22,hour=10,minute=0)
  self.assertEqual(four_hour_bars([bar(t)],t+timedelta(minutes=30)),[])
 def test_weekend_and_out_of_session_excluded(self):
  self.assertEqual(four_hour_bars([bar(NOW.replace(day=24)),bar(NOW.replace(hour=13))],NOW+timedelta(days=2)),[])
 def test_volume_balance_is_a_bounded_proxy_and_zero_volume_unknown(self):
  ctx=volume_context([bar(NOW,12),bar(NOW,8)],[])
  self.assertEqual(ctx['close_location_balance_pct'],0)
  self.assertIsNone(volume_context([bar(NOW,12,0)],[])['close_location_balance_pct'])
  self.assertEqual(volume_context([bar(NOW,12)],[])['close_location_balance_pct'],100)
 def test_warmup_history_is_not_plotted_as_window_and_daily_is_research_only(self):
  bars=[bar(NOW-timedelta(days=i),10+i*.01) for i in range(150,0,-1)]
  original=[dict(b) for b in bars]
  out=build_analysis('BOP','1M','auto',bars,bars,{'synced_at':NOW.isoformat()},{},NOW)
  self.assertEqual(len(out['stock']['bars']),30);self.assertEqual(out['warmup_candles'],120)
  self.assertFalse(out['stock']['availability']['actionable']);self.assertFalse(out['order_flow']['available']);self.assertEqual(bars,original)
 def test_request_validation_precedes_fetch(self):
  service=AnalysisService()
  with patch('analysis_service.provider.fetch_history') as fetch:
   with self.assertRaises(ValueError):service.get('../bad','3M','auto',{})
   with self.assertRaises(ValueError):service.get('BOP','1Y','auto',{})
   fetch.assert_not_called()
 def test_failed_refresh_preserves_cached_history_and_blocks_guidance(self):
  s=AnalysisService();key=('BOP','5D','15m');bars=[bar(NOW-timedelta(minutes=15*i)) for i in range(100,0,-1)]
  s.cache[key]={'attempted':1e20,'bars':bars,'daily':bars,'meta':{'synced_at':NOW.isoformat()},'error':'Provider unavailable','ttl':300}
  result=s.get(*key,{})
  self.assertFalse(result['analysis']['stock']['availability']['actionable'])
  self.assertEqual(result['analysis']['stock']['guidance']['action'],'WAIT FOR DATA')

 def test_reported_gap_restarts_history_and_is_disclosed(self):
  start=datetime(2026,10,1,9,30,tzinfo=KARACHI)
  rows=[[start.isoformat(),10,12,8,10,100]]
  rows += [[(start+timedelta(days=2,minutes=15*i)).isoformat(),10,12,8,10,100] for i in range(40)]
  gap={'start':'2026-10-02','end':'2026-10-02','reason':'data_corruption'}
  payload={'results':[{'symbol':'BOP','interval':'15m','candles':rows,'gaps':[gap]}]}
  with patch('analysis_service.provider.get_json',return_value=payload):
   bars,meta=fetch_research_history('BOP','15m',65)
  self.assertEqual(len(bars),40);self.assertEqual(meta['excluded_gaps'],[gap])
  self.assertTrue(all(b['time'].startswith('2026-10-03') for b in bars))
  self.assertEqual(payload['results'][0]['gaps'],[gap])

 def test_sparse_four_hour_history_shows_chart_without_inventing_levels(self):
  start=NOW.replace(day=22,hour=9,minute=30)
  bars=[bar(start+timedelta(minutes=15*i)) for i in range(24)]
  out=build_analysis('BOP','1M','4h',bars,bars,{'synced_at':NOW.isoformat()},{},NOW)
  self.assertFalse(out['history_sufficient']);self.assertIsNone(out['stock']['risk'])
  self.assertFalse(out['stock']['availability']['actionable']);self.assertTrue(out['stock']['bars'])
