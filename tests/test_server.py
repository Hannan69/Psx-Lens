import sys, tempfile, threading, unittest, json, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from http.server import ThreadingHTTPServer

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=server.DB;server.DB=Path(self.temp.name)/'test.sqlite3';server.init(demo=True)
        with server.connect() as db:
            for r in db.execute('SELECT symbol FROM instruments').fetchall():
                f,_=server.instrument(db,r['symbol']);db.execute('INSERT INTO signals VALUES(?,?)',(r['symbol'],f['signal']))
        self.http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.http.server_port)
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();server.DB=self.old;self.temp.cleanup()
    def call(self,path,data=None,headers=None):
        req=urllib.request.Request(self.url+'/api/'+path,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json',**(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:return r.status,json.loads(r.read())
        except urllib.error.HTTPError as e:return e.code,json.loads(e.read())
    def test_open_close_cash_and_risk(self):
        status,s=self.call('state');self.assertEqual(status,200);self.assertEqual(s['equity'],100000)
        f=next(x for x in s['stocks'] if x['symbol']=='BOP')
        order={'symbol':'BOP','quantity':100,'stop':f['risk']['stop'],'target':f['risk']['target1']}
        self.assertEqual(self.call('paper/open',{**order,'quantity':1000000})[0],400)
        self.assertEqual(self.call('paper/open',{**order,'quantity':1.5})[0],400)
        self.assertEqual(self.call('paper/open',order)[0],200)
        s=self.call('state')[1];p=s['positions'][0]
        self.assertLess(s['settings']['cash'],100000);self.assertLess(s['equity'],100000)
        self.assertEqual(self.call('settings',{'risk_pct':1,'fee_bps':20,'slippage_bps':5})[0],400)
        self.assertEqual(self.call('paper/close',{'id':p['id']})[0],200)
        s=self.call('state')[1];self.assertAlmostEqual(s['equity'],100000+s['positions'][0]['pnl'])
        self.assertEqual(self.call('paper/close',{'id':p['id']})[0],400)
    def test_csv_import_and_backtest(self):
        import io,csv
        b=server.demo_bars('BOP',100)
        out=io.StringIO();w=csv.DictWriter(out,fieldnames=['symbol','time','open','high','low','close','volume']);w.writeheader()
        for x in b:w.writerow({'symbol':'NEW',**x})
        self.assertEqual(self.call('import',{'csv':out.getvalue()})[0],200)
        s=self.call('state')[1];f=next(x for x in s['stocks'] if x['symbol']=='NEW')
        self.assertEqual(f['source'],'CSV import');self.assertIsNone(f['book']);self.assertEqual(f['coverage'],85)
        status,r=self.call('backtest',{'symbol':'NEW'});self.assertEqual(status,200);self.assertEqual(r['bars'],100)
    def test_advance_and_transition_alerts(self):
        first=self.call('state')[1]['stocks'][0]['time']
        for _ in range(10):self.assertEqual(self.call('advance',{})[0],200)
        s=self.call('state')[1];self.assertNotEqual(first,s['stocks'][0]['time']);self.assertGreater(len(s['alerts']),0)
        self.call('alerts/read',{});self.assertTrue(all(a['read'] for a in self.call('state')[1]['alerts']))
    def test_same_origin_restriction(self):
        self.assertEqual(self.call('advance',{},headers={'Origin':'https://example.com'})[0],403)
        self.assertEqual(self.call('state',headers={'Host':'example.com'})[0],403)
    def test_provider_ingestion_and_duplicate_rejection(self):
        s=self.call('state')[1];f=next(x for x in s['stocks'] if x['symbol']=='BOP')
        b=server.demo_bars('BOP')[720]
        frame={'symbol':'BOP','provider':'Example licensed adapter','completed':True,'candle':b,'book':{'time':b['time'],'bids':[{'price':b['close']-.01,'size':2000}],'asks':[{'price':b['close']+.01,'size':1000}]}}
        self.assertEqual(self.call('feed',frame)[0],200)
        self.assertEqual(self.call('feed',frame)[0],400)
        f=next(x for x in self.call('state')[1]['stocks'] if x['symbol']=='BOP')
        self.assertEqual(f['coverage'],100);self.assertAlmostEqual(f['imbalance'],2/3)
        self.assertEqual(f['source'],'Example licensed adapter')
        frame['candle']=server.demo_bars('BOP')[721];frame['book']=None
        self.assertEqual(self.call('feed',frame)[0],200)
        f=next(x for x in self.call('state')[1]['stocks'] if x['symbol']=='BOP')
        self.assertIsNone(f['book']);self.assertEqual(f['coverage'],85)
    def test_paper_stop_on_new_bar(self):
        f=next(x for x in self.call('state')[1]['stocks'] if x['symbol']=='BOP')
        b=server.demo_bars('BOP')[720]
        # Next bar drops past a very tight modeled stop.
        self.assertLess(b['low'],f['price']-.001)
        self.call('paper/open',{'symbol':'BOP','quantity':10,'stop':f['price']-.001,'target':f['price']+10})
        self.call('advance',{})
        p=self.call('state')[1]['positions'][0]
        self.assertIsNotNone(p['closed']);self.assertIn(p['reason'],('Stop loss','Gap below stop'))
    def test_pypsx_sync_persists_interval(self):
        from unittest.mock import patch
        bars=server.demo_bars('BOP',100)
        meta={'interval':'1d','synced_at':'2026-10-17T12:00:00+05:00','omitted_incomplete':0,'environment':'paper'}
        with patch('server.pypsx_provider.fetch_history',return_value=(bars,meta)):
            code,result=self.call('pypsx/sync',{'symbol':'BOP','interval':'1d','days':180})
        self.assertEqual(code,200)
        f=next(x for x in self.call('state')[1]['stocks'] if x['symbol']=='BOP')
        self.assertEqual(f['source'],'pyPSX · 1d');self.assertEqual(f['interval'],'1d')
        self.assertIsNone(f['vwap']);self.assertIsNone(f['book']);self.assertEqual(f['coverage'],70)
    def test_pypsx_failed_sync_preserves_history(self):
        from unittest.mock import patch
        before=self.call('state')[1]['stocks']
        with patch('server.pypsx_provider.fetch_history',side_effect=ValueError('pyPSX returned HTTP 403')):
            code,_=self.call('pypsx/sync',{'symbol':'BOP'})
        self.assertEqual(code,400);self.assertEqual(before,self.call('state')[1]['stocks'])
    def test_pypsx_sync_blocks_open_position(self):
        from unittest.mock import patch
        f=next(x for x in self.call('state')[1]['stocks'] if x['symbol']=='BOP')
        self.call('paper/open',{'symbol':'BOP','quantity':10,'stop':f['risk']['stop'],'target':f['risk']['target1']})
        with patch('server.pypsx_provider.fetch_history') as fetch:
            self.assertEqual(self.call('pypsx/sync',{'symbol':'BOP'})[0],400)
            fetch.assert_not_called()
    def test_actual_mode_blocks_closed_market_paper_entry(self):
        from datetime import datetime,timedelta
        from engine import KARACHI
        now=datetime.now(KARACHI)
        bars=server.demo_bars('BOP',100)
        delta=now-timedelta(minutes=6)-datetime.fromisoformat(bars[-1]['time'])
        bars=[{**b,'time':(datetime.fromisoformat(b['time'])+delta).isoformat()} for b in bars]
        meta={'interval':'5m','synced_at':now.isoformat(),'environment':'paper'}
        with server.connect() as db:
            server.set_value(db,'demo_mode',0);server.apply_snapshot(db,'BOP',bars,meta)
            market={'is_open':False,'checked_at':now.isoformat(),'demo_mode':False,'override_mode':'AUTO','error':None}
            db.execute('INSERT OR REPLACE INTO provider_state VALUES(?,?)',('market',json.dumps(market)))
        f=next(x for x in self.call('state')[1]['stocks'] if x['symbol']=='BOP')
        self.assertEqual(f['guidance']['action'],'WAIT FOR MARKET OPEN')
        code,r=self.call('paper/open',{'symbol':'BOP','quantity':10,'stop':f['risk']['stop'],'target':f['risk']['target1']})
        self.assertEqual(code,400);self.assertIn('Paper entry blocked',r['error'])
        self.assertEqual(self.call('state')[1]['positions'],[])
    def test_actual_mode_disables_simulation(self):
        with server.connect() as db:server.set_value(db,'demo_mode',0)
        self.assertEqual(self.call('advance',{})[0],400)
        self.assertEqual(self.call('import',{})[0],400)
if __name__=='__main__':unittest.main()
