#!/usr/bin/env python3
"""Local PSX research workspace, standard-library only. Bind to loopback."""
import argparse
import csv
import io
import json
import math
import os
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from engine import features, position_size, parse_csv, backtest, KARACHI
from providers import normalize_frame, ingest_frame
import pypsx_provider
from market_service import MarketService, availability, guidance

ROOT=Path(__file__).resolve().parent
DB=Path(os.environ.get('PSX_DB',str(ROOT/'data'/'workspace.sqlite3')))
LOCK=threading.RLock()
SERVICE=None
from analysis_service import AnalysisService
ANALYSIS=AnalysisService()
COMPANIES={'BOP':('Bank of Punjab','Commercial banks',12.4),'MEBL':('Meezan Bank','Commercial banks',280),'HUBC':('Hub Power Company','Power generation',130),'OGDC':('Oil & Gas Development','Oil & gas exploration',210),'ENGRO':('Engro Corporation','Diversified',310),'LUCK':('Lucky Cement','Cement',920)}

@contextmanager
def connect():
    db=sqlite3.connect(DB)
    db.row_factory=sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()

def demo_bars(symbol,count=840):
    rng=random.Random(symbol)
    price=COMPANIES[symbol][2]*.96
    day=datetime(2026,9,14,9,30,tzinfo=KARACHI)
    bars=[]
    for i in range(count):
        session=i//60
        date=day+timedelta(days=session+2*(session//5))
        time=date+timedelta(minutes=5*(i%60))
        drift=.00022 if symbol in ('BOP','OGDC','LUCK') else -.00012 if symbol=='HUBC' else .00003
        move=drift+.0009*math.sin(i/16)+rng.gauss(0,.0022)
        op=price
        price=max(.1,op*(1+move))
        span=op*rng.uniform(.0008,.003)
        bars.append({'time':time.isoformat(),'open':round(op,4),'high':round(max(op,price)+span,4),'low':round(min(op,price)-span,4),'close':round(price,4),'volume':int(rng.uniform(50000,240000)*(1.8 if i%60>48 else 1))})
    return bars

def init(demo=False):
    DB.parent.mkdir(parents=True,exist_ok=True)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY,interval TEXT DEFAULT '5m',days INTEGER DEFAULT 20,active INTEGER DEFAULT 1,error TEXT,attempted_at TEXT);
        CREATE TABLE IF NOT EXISTS provider_state(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS instruments(symbol TEXT PRIMARY KEY,name TEXT,sector TEXT,source TEXT,bars TEXT,cursor INTEGER);
        CREATE TABLE IF NOT EXISTS instrument_meta(symbol TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS books(symbol TEXT PRIMARY KEY,time TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS signals(symbol TEXT PRIMARY KEY,signal TEXT);
        CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY,time TEXT,symbol TEXT,previous TEXT,signal TEXT,score INTEGER,source TEXT,read INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY,symbol TEXT,quantity INTEGER,entry REAL,stop REAL,target REAL,cost REAL,source TEXT,opened TEXT,closed TEXT,exit REAL,pnl REAL,reason TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        ''')
        if demo:
            for symbol,(name,sector,_) in COMPANIES.items():
                db.execute('INSERT OR IGNORE INTO instruments VALUES(?,?,?,?,?,?)',(symbol,name,sector,'Simulated',json.dumps(demo_bars(symbol)),720))
        for symbol in ('BOP','MEBL','HUBC','OGDC','LUCK','ENGROH'):
            db.execute('INSERT OR IGNORE INTO watchlist(symbol) VALUES(?)',(symbol,))
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('demo_mode',str(int(demo))))
        for key,value in {'cash':100000,'initial':100000,'risk_pct':1,'fee_bps':10,'slippage_bps':5}.items():
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(key,str(value)))

def settings(db):
    return {r['key']:float(r['value']) for r in db.execute('SELECT * FROM settings')}

def set_value(db,key,value):
    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,str(value)))

def instrument(db,symbol):
    row=db.execute('SELECT * FROM instruments WHERE symbol=?',(symbol,)).fetchone()
    if not row: raise ValueError('Unknown symbol.')
    bars=json.loads(row['bars'])[:row['cursor']]
    book=None
    if row['source']=='Simulated':
        rng=random.Random(symbol+bars[-1]['time'])
        p=bars[-1]['close']
        bias=1.5 if p>bars[-2]['close'] else .65
        tick=max(.01,round(p*.0002,2))
        book={'bids':[{'price':round(p-tick*(i+1),4),'size':int(rng.randint(10000,100000)*bias)} for i in range(10)],'asks':[{'price':round(p+tick*(i+1),4),'size':rng.randint(10000,100000)} for i in range(10)]}
    if row['source'] not in ('Simulated','CSV import'):
        saved=db.execute('SELECT * FROM books WHERE symbol=?',(symbol,)).fetchone()
        if saved and saved['time']==bars[-1]['time']: book=json.loads(saved['payload'])
    meta_row=db.execute('SELECT payload FROM instrument_meta WHERE symbol=?',(symbol,)).fetchone()
    meta=json.loads(meta_row['payload']) if meta_row else {}
    f=features(bars[-301:],book,timeframe=meta.get('interval','intraday'))
    f.update({'interval':meta.get('interval','5m' if row['source']=='Simulated' else 'imported'),'sync':meta})
    f.update({'symbol':symbol,'name':row['name'],'sector':row['sector'],'source':row['source'],'time':bars[-1]['time'],'bars':bars[-120:],'book':book,'history_count':len(bars),'change':(bars[-1]['close']/bars[-2]['close']-1)*100})
    return f,bars

def close_position(db,p,raw,time,reason):
    s=settings(db)
    exit_price=raw*(1-s['slippage_bps']/10000)
    proceeds=p['quantity']*exit_price*(1-s['fee_bps']/10000)
    set_value(db,'cash',s['cash']+proceeds)
    db.execute('UPDATE positions SET closed=?,exit=?,pnl=?,reason=? WHERE id=?',(time,exit_price,proceeds-p['cost'],reason,p['id']))

def process_bar(db,symbol,b):
    for p in db.execute('SELECT * FROM positions WHERE symbol=? AND closed IS NULL',(symbol,)).fetchall():
        if datetime.fromisoformat(b['time'])<=datetime.fromisoformat(p['opened']):continue
        if b['open']<=p['stop']: close_position(db,p,b['open'],b['time'],'Gap below stop')
        elif b['low']<=p['stop']: close_position(db,p,p['stop'],b['time'],'Stop loss')
        elif b['high']>=p['target']: close_position(db,p,max(p['target'],b['open']),b['time'],'Take profit')
    f,_=instrument(db,symbol)
    prev=db.execute('SELECT signal FROM signals WHERE symbol=?',(symbol,)).fetchone()
    if prev and prev['signal']!=f['signal']:
        db.execute('INSERT INTO alerts(time,symbol,previous,signal,score,source) VALUES(?,?,?,?,?,?)',(b['time'],symbol,prev['signal'],f['signal'],f['score'],f['source']))
    db.execute('INSERT OR REPLACE INTO signals VALUES(?,?)',(symbol,f['signal']))

def market_state(db):
    row=db.execute("SELECT value FROM provider_state WHERE key='market'").fetchone()
    return json.loads(row['value']) if row else {}

def real_instrument(db,symbol,now=None):
    row=db.execute('SELECT source FROM instruments WHERE symbol=?',(symbol,)).fetchone()
    watch=db.execute('SELECT error FROM watchlist WHERE symbol=?',(symbol,)).fetchone()
    if row and row['source'].startswith('pyPSX'):
        f,_=instrument(db,symbol)
    else:
        info=COMPANIES.get(symbol,(symbol,'PSX equity',0))
        if symbol=='ENGROH':info=('Engro Holdings','Investment holding',0)
        f={'symbol':symbol,'name':info[0],'sector':info[1],'source':'Unavailable','time':None,'price':None,'score':None,'signal':'UNAVAILABLE','components':[],'bars':[],'book':None,'risk':None,'sync':{},'history_count':0,'interval':'5m','change':None,'coverage':0,'vwap':None,'rsi':None,'atr':None,'volume_ratio':None}
    ready=availability(f['source'],f['time'],f['sync'],market_state(db),watch['error'] if watch else None,now)
    f['availability']=ready
    f['snapshot_signal']=f['signal']
    f['signal']=f['signal'] if ready['actionable'] else 'NO CURRENT SIGNAL'
    f['guidance']=guidance(f,ready)
    return f

def apply_snapshot(db,symbol,bars,meta):
    """Commit provider candles atomically. Process only unseen bars against open paper positions."""
    source='pyPSX · '+meta['interval']
    old=db.execute('SELECT * FROM instruments WHERE symbol=?',(symbol,)).fetchone()
    old_meta=db.execute('SELECT payload FROM instrument_meta WHERE symbol=?',(symbol,)).fetchone()
    previous=json.loads(old['bars'])[:old['cursor']] if old else []
    same=bool(old and old['source']==source and old_meta and json.loads(old_meta['payload']).get('environment')==meta.get('environment'))
    positions=db.execute('SELECT 1 FROM positions WHERE symbol=? AND closed IS NULL',(symbol,)).fetchone()
    if positions and not same:raise ValueError('Close this symbol’s paper position before changing data source or candle interval.')
    existing={b['time']:b for b in previous}
    revised=any(b['time'] in existing and b!=existing[b['time']] for b in bars)
    if positions and revised:raise ValueError('The provider revised earlier candles. Review and close the paper position before replacing its history.')
    if same and previous and bars[-1]['time']<previous[-1]['time']:raise ValueError('Provider history moved backwards. Existing data was preserved.')
    overlaps=not previous or previous[-1]['time'] in {b['time'] for b in bars}
    if positions and not overlaps:raise ValueError('History no longer overlaps the open paper position. Review it before syncing.')
    if same and not overlaps:
        same=False  # Long downtime with no position: establish a fresh warmup baseline.
    info=COMPANIES.get(symbol,(symbol,'PSX equity',0))
    name,sector=(old['name'],old['sector']) if old else info[:2]
    if symbol=='ENGROH':name,sector='Engro Holdings','Investment holding'
    meta={**meta,'history_revised':revised}
    db.execute('INSERT OR REPLACE INTO instrument_meta VALUES(?,?)',(symbol,json.dumps(meta)))
    db.execute('DELETE FROM books WHERE symbol=?',(symbol,))
    unseen=[i for i,b in enumerate(bars) if same and previous and b['time']>previous[-1]['time']]
    if not same or revised:db.execute('DELETE FROM signals WHERE symbol=?',(symbol,))
    if unseen and not revised:
        for i in unseen:
            prefix=bars[:i+1]
            db.execute('INSERT OR REPLACE INTO instruments VALUES(?,?,?,?,?,?)',(symbol,name,sector,source,json.dumps(prefix),len(prefix)))
            process_bar(db,symbol,prefix[-1])
    db.execute('INSERT OR REPLACE INTO instruments VALUES(?,?,?,?,?,?)',(symbol,name,sector,source,json.dumps(bars),len(bars)))
    if not unseen or revised:
        f,_=instrument(db,symbol)
        db.execute('INSERT OR REPLACE INTO signals VALUES(?,?)',(symbol,f['signal']))
    db.execute('UPDATE watchlist SET error=NULL,attempted_at=? WHERE symbol=?',(meta['synced_at'],symbol))
    return {'message':f'Updated {symbol}: {len(bars)} completed {meta["interval"]} candles.','symbol':symbol,'meta':meta}

def state(db):
    s=settings(db)
    if s.get('demo_mode'):
        stocks=[instrument(db,r['symbol'])[0] for r in db.execute('SELECT symbol FROM instruments ORDER BY symbol')]
    else:
        stocks=[real_instrument(db,r['symbol']) for r in db.execute('SELECT symbol FROM watchlist WHERE active=1 ORDER BY symbol')]
    positions=[dict(r) for r in db.execute('SELECT * FROM positions ORDER BY id DESC')]
    equity=s['cash']
    for p in positions:
        if not p['closed']:
            f,_=instrument(db,p['symbol'])
            price=f['price']
            value=p['quantity']*price*(1-s['slippage_bps']/10000)*(1-s['fee_bps']/10000)
            p.update({'mark':price,'pnl':value-p['cost'],'action':'STOP LOSS' if price<=p['stop'] else 'TAKE PROFIT' if price>=p['target'] else 'EXIT WATCH' if f['score']<45 else 'HOLD','data_note':'Stored candle valuation; confirm current price with your broker.'})
            equity+=value
    return {'stocks':stocks,'settings':s,'positions':positions,'equity':equity,'alerts':[dict(r) for r in db.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 100') if s.get('demo_mode') or r['source'].startswith('pyPSX')],'server_time':datetime.now(KARACHI).isoformat(),'market':market_state(db),'updates':SERVICE.status() if SERVICE else {'running':False,'busy':False},'watchlist':[dict(r) for r in db.execute('SELECT * FROM watchlist WHERE active=1 ORDER BY symbol')]}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/'static'),**kwargs)
    def log_message(self,*args): pass
    def end_headers(self):
        if not self.path.startswith("/api/"):self.send_header("Cache-Control","no-store")
        super().end_headers()
    def send_json(self,data,status=200):
        payload=json.dumps(data,allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def do_GET(self):
        parsed=urlparse(self.path)
        try:
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'):
                self.send_json({'error':'Local requests only.'},403); return
            if parsed.path=='/api/state':
                with LOCK,connect() as db: self.send_json(state(db))
            elif parsed.path=='/api/analysis':
                query=parse_qs(parsed.query);symbol=query.get('symbol',['BOP'])[0].strip().upper()
                with LOCK,connect() as db:
                    if not db.execute('SELECT 1 FROM watchlist WHERE symbol=? AND active=1',(symbol,)).fetchone():raise ValueError('Choose a watched stock.')
                    market=market_state(db)
                self.send_json(ANALYSIS.get(symbol,query.get('window',['3M'])[0],query.get('interval',['auto'])[0],market))
            elif parsed.path=='/api/pypsx/status':
                self.send_json(pypsx_provider.status())
            elif parsed.path=='/api/export':
                symbol=parse_qs(parsed.query).get('symbol',['BOP'])[0]
                with LOCK,connect() as db: _,bars=instrument(db,symbol)
                stream=io.StringIO()
                w=csv.DictWriter(stream,fieldnames=['symbol','time','open','high','low','close','volume'])
                w.writeheader()
                for b in bars: w.writerow({'symbol':symbol,**b})
                payload=stream.getvalue().encode()
                self.send_response(200)
                self.send_header('Content-Type','text/csv')
                self.send_header('Content-Disposition',f'attachment; filename="{symbol}-candles.csv"')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            elif parsed.path.startswith('/api/'): self.send_json({'error':'Unknown endpoint'},404)
            else: super().do_GET()
        except ValueError as e: self.send_json({'error':str(e)},400)
    def do_POST(self):
        try:
            host=self.headers.get('Host','')
            allowed=f'127.0.0.1:{self.server.server_port}'
            if host not in (allowed,f'localhost:{self.server.server_port}') or self.headers.get('Origin') not in (None,'http://'+host):
                self.send_json({'error':'Local same-origin requests only.'},403); return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json': raise ValueError('JSON required.')
            length=int(self.headers.get('Content-Length',0))
            if not 0<length<=5000000: raise ValueError('Request too large or empty.')
            data=json.loads(self.rfile.read(length))
            with LOCK,connect() as db:
                route=urlparse(self.path).path
                if route=='/api/refresh':
                    if SERVICE:SERVICE.request()
                    result={'message':'Refresh requested. The background updater will check every watched stock.'}
                elif route=='/api/watchlist':
                    import re
                    symbol=str(data['symbol']).strip().upper()
                    if not re.fullmatch('[A-Z0-9-]{1,12}',symbol):raise ValueError('Enter a valid PSX symbol, for example BOP.')
                    if db.execute('SELECT count(*) FROM watchlist WHERE active=1').fetchone()[0]>=20 and not db.execute('SELECT 1 FROM watchlist WHERE symbol=?',(symbol,)).fetchone():raise ValueError('Watch up to 20 stocks at a time.')
                    db.execute('INSERT INTO watchlist(symbol) VALUES(?) ON CONFLICT(symbol) DO UPDATE SET active=1',(symbol,))
                    if SERVICE:SERVICE.request()
                    result={'message':f'{symbol} added. Data will be fetched automatically.'}
                elif route=='/api/advance':
                    if not settings(db).get('demo_mode'):raise ValueError('Simulation is disabled. This workspace uses pyPSX data.')
                    advanced=0
                    for r in db.execute("SELECT * FROM instruments WHERE source='Simulated'").fetchall():
                        bars=json.loads(r['bars'])
                        if r['cursor']<len(bars):
                            db.execute('UPDATE instruments SET cursor=cursor+1 WHERE symbol=?',(r['symbol'],))
                            process_bar(db,r['symbol'],bars[r['cursor']]); advanced+=1
                    if not advanced: raise ValueError('Simulation finished. Imported data advances only through new imports.')
                    result={'message':f'Advanced {advanced} simulated instruments by one 5-minute candle.'}
                elif route=='/api/pypsx/sync':
                    symbol=str(data['symbol']).strip().upper()
                    # Manual history replacement remains available for research; the automatic updater uses watchlist settings.
                    old=db.execute('SELECT source FROM instruments WHERE symbol=?',(symbol,)).fetchone()
                    if db.execute('SELECT 1 FROM positions WHERE symbol=? AND closed IS NULL',(symbol,)).fetchone() and (not old or not old['source'].startswith('pyPSX')):raise ValueError('Close this symbol’s paper position before changing its data source.')
                    bars,meta=pypsx_provider.fetch_history(symbol,str(data.get('interval','5m')),data.get('days',20))
                    result=apply_snapshot(db,symbol,bars,meta)
                    db.execute('INSERT INTO watchlist(symbol,interval,days) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET interval=excluded.interval,days=excluded.days',(symbol,meta['interval'],int(data.get('days',20))))
                elif route=='/api/feed':
                    frame=normalize_frame(data)
                    ingest_frame(db,frame)
                    process_bar(db,frame.symbol,frame.candle)
                    result={'message':f'Processed completed provider candle for {frame.symbol}.'}
                elif route=='/api/import':
                    if not settings(db).get('demo_mode'):raise ValueError('CSV replacement is disabled in the actual-data workspace. Add the stock to the pyPSX watchlist instead.')
                    symbol,bars=parse_csv(data['csv'])
                    if db.execute('SELECT 1 FROM positions WHERE symbol=? AND closed IS NULL',(symbol,)).fetchone(): raise ValueError('Close this symbol’s paper position before replacing its dataset.')
                    old=db.execute('SELECT name,sector FROM instruments WHERE symbol=?',(symbol,)).fetchone()
                    name,sector=(old['name'],old['sector']) if old else (symbol,'Imported')
                    db.execute('INSERT OR REPLACE INTO instruments VALUES(?,?,?,?,?,?)',(symbol,name,sector,'CSV import',json.dumps(bars),len(bars)))
                    db.execute('DELETE FROM instrument_meta WHERE symbol=?',(symbol,))
                    db.execute('DELETE FROM books WHERE symbol=?',(symbol,))
                    db.execute('DELETE FROM signals WHERE symbol=?',(symbol,))
                    process_bar(db,symbol,bars[-1])
                    result={'message':f'Imported {len(bars)} completed candles for {symbol}.','symbol':symbol}
                elif route=='/api/risk':
                    result=position_size(float(data['capital']),float(data['risk_pct']),float(data['entry']),float(data['stop']),settings(db)['fee_bps'])
                elif route=='/api/paper/open':
                    f,_=instrument(db,data['symbol'])
                    s=settings(db)
                    if not s.get('demo_mode'):
                        ready=real_instrument(db,data['symbol'])['availability']
                        if not ready['actionable']:raise ValueError('Paper entry blocked: '+ready['message'])
                    if db.execute('SELECT 1 FROM positions WHERE symbol=? AND closed IS NULL',(f['symbol'],)).fetchone(): raise ValueError('A position for this symbol is already open.')
                    q=float(data['quantity'])
                    if not math.isfinite(q) or q!=int(q): raise ValueError('Quantity must be a whole number.')
                    quantity=int(q)
                    stop=float(data['stop']);target=float(data['target'])
                    entry=f['price']*(1+s['slippage_bps']/10000)
                    if not math.isfinite(stop) or not math.isfinite(target) or not 0<stop<entry<target or quantity<=0: raise ValueError('Quantity must be positive, with stop < entry < target.')
                    cost=entry*quantity*(1+s['fee_bps']/10000)
                    allowed=position_size(s['cash'],s['risk_pct'],entry,stop*(1-s['slippage_bps']/10000),s['fee_bps'])['quantity']
                    if quantity>allowed: raise ValueError(f'Quantity exceeds available cash or configured risk. Maximum: {allowed} shares.')
                    set_value(db,'cash',s['cash']-cost)
                    db.execute('INSERT INTO positions(symbol,quantity,entry,stop,target,cost,source,opened) VALUES(?,?,?,?,?,?,?,?)',(f['symbol'],quantity,entry,stop,target,cost,f['source'],f['time']))
                    result={'message':f'Paper position opened: {quantity} {f["symbol"]} shares.'}
                elif route=='/api/paper/close':
                    p=db.execute('SELECT * FROM positions WHERE id=? AND closed IS NULL',(int(data['id']),)).fetchone()
                    if not p: raise ValueError('Open position not found.')
                    f,_=instrument(db,p['symbol'])
                    close_position(db,p,f['price'],f['time'],'Manual exit')
                    result={'message':'Paper position closed; costs applied.'}
                elif route=='/api/settings':
                    s=settings(db)
                    risk=float(data['risk_pct']); fee=float(data['fee_bps']); slip=float(data['slippage_bps'])
                    position_size(100000,risk,100,99,fee)
                    if not math.isfinite(slip) or not 0<=slip<=100: raise ValueError('Slippage must be 0–100 bps.')
                    if db.execute('SELECT 1 FROM positions WHERE closed IS NULL').fetchone(): raise ValueError('Close open paper positions before changing risk or costs.')
                    for key,v in {'risk_pct':risk,'fee_bps':fee,'slippage_bps':slip}.items(): set_value(db,key,v)
                    result={'message':'Risk and cost settings saved.'}
                elif route=='/api/backtest':
                    f,bars=instrument(db,data['symbol'])
                    if not settings(db).get('demo_mode') and not f['source'].startswith('pyPSX'):raise ValueError('Choose a stock with actual pyPSX candles before backtesting.')
                    result=backtest(bars,capital=float(data.get('capital',100000)),risk_pct=float(data.get('risk_pct',1)),fee_bps=float(data.get('fee_bps',10)),slippage_bps=float(data.get('slippage_bps',5)),entry_score=int(data.get('entry_score',65)),exit_score=int(data.get('exit_score',45)),timeframe=f.get('interval','intraday'))
                    result['source']=f['source']
                elif route=='/api/alerts/read':
                    db.execute('UPDATE alerts SET read=1'); result={'message':'Alerts marked as read.'}
                else: self.send_json({'error':'Unknown endpoint'},404); return
                db.commit()
                self.send_json(result)
        except (ValueError,KeyError,TypeError,OverflowError) as e: self.send_json({'error':str(e)},400)
        except Exception:
            self.send_json({'error':'Unexpected server error. See terminal.'},500)
            raise

def main():
    global SERVICE
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    init()
    SERVICE=MarketService(connect,LOCK,apply_snapshot)
    SERVICE.start()
    with connect() as db:
        for r in db.execute('SELECT symbol FROM instruments').fetchall():
            f,_=instrument(db,r['symbol'])
            db.execute('INSERT OR IGNORE INTO signals VALUES(?,?)',(r['symbol'],f['signal']))
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'PSX Lens running at http://127.0.0.1:{args.port} · Ctrl+C to stop',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWorkspace stopped.",flush=True)
    finally:
        SERVICE.stop()
        server.server_close()
if __name__=='__main__': main()
