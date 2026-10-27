"""Automatic, read-only pyPSX ingestion and conservative signal availability."""
import json
import re
import threading
import time
from datetime import datetime, timedelta
from engine import KARACHI
import pypsx_provider

OPEN_POLL=60
CLOSED_POLL=300

def market_snapshot(payload,now=None):
    now=now or datetime.now(KARACHI)
    if not isinstance(payload,dict) or type(payload.get('is_open')) is not bool:
        raise ValueError('pyPSX returned an unsupported market-status response.')
    if payload.get('demo_mode') is not False or payload.get('override_mode')!='AUTO':
        raise ValueError('pyPSX reports demo or overridden market mode. Current signals are disabled.')
    return {k:payload.get(k) for k in ('is_open','status_text','session','next_market_open','holiday_name','demo_mode','override_mode')} | {'checked_at':now.isoformat(),'error':None}

def availability(source,last_time,meta,market,error=None,now=None):
    now=now or datetime.now(KARACHI)
    result={'actionable':False,'state':'unavailable','label':'Data unavailable','message':'Waiting for verified pyPSX candles. No current signal is available.'}
    if not source.startswith('pyPSX') or not last_time:return result
    try:
        last=datetime.fromisoformat(last_time)
        synced=datetime.fromisoformat(meta['synced_at'])
        age=(now-last).total_seconds()
        if last.tzinfo is None or synced.tzinfo is None or age < -60 or (synced-now).total_seconds()>60:raise ValueError()
    except (ValueError,KeyError,TypeError):
        return {**result,'message':'The dataset timestamp is invalid. Refresh before using a signal.'}
    if error or meta.get('quality_issue'):
        return {**result,'state':'error','label':'Update failed','message':error or meta['quality_issue']}
    try:
        checked=datetime.fromisoformat(market['checked_at'])
        ttl=180 if market.get('is_open') is True else 600
        verified=not market.get('error') and market.get('demo_mode') is False and market.get('override_mode')=='AUTO' and 0<=(now-checked).total_seconds()<=ttl
    except (ValueError,KeyError,TypeError):verified=False
    if not verified:
        return {**result,'state':'unknown','label':'Market status unverified','message':'The provider’s market status is unavailable or expired. Saved candles are for review only.'}
    interval=meta.get('interval','5m')
    if interval=='1d':
        return {**result,'state':'historical','label':'Daily research only','message':'Daily candles support historical research, not current intraday entry signals.'}
    if age>4*86400:
        return {**result,'state':'stale','label':'Outdated candles','message':'The newest candle is over four days old. Refresh or choose another stock.'}
    if market.get('is_open') is not True:
        return {**result,'state':'closed','label':'Market closed','message':'Review the last session’s setup. Wait for the market to open and fresh candles before a new entry.'}
    minutes=pypsx_provider.INTERVALS.get(interval,5)
    if age>max(15,minutes*3)*60 or (now-synced).total_seconds()>180:
        return {**result,'state':'stale','label':'Data delayed or stale','message':'The latest candle or successful refresh is too old for a current signal. Wait for an update.'}
    return {'actionable':True,'state':'fresh','label':'Recent provider candles','message':f'Updated automatically. Analysis uses completed {interval} candles, not a real-time executable quote.'}

def guidance(f,ready):
    score=f.get('score')
    if not ready['actionable']:
        return {'action':'WAIT FOR DATA' if ready['state']!='closed' else 'WAIT FOR MARKET OPEN','tone':'wait','new_buyer':ready['message'],'holder':'Compare your own entry and stop with your broker’s current quote. Saved model levels are not a live exit instruction.','steps':['Check the data timestamp and connection status.','Wait for a recent completed candle during market hours.','Use the last session only to prepare a plan; do not treat it as today’s entry.']}
    if score>=65:
        action='REVIEW ENTRY SETUP';tone='bull'
        buyer='A bullish setup is forming. Review the entry zone, confirm the current broker quote, and practise with a paper trade first. This is not an automatic buy.'
    elif score>=45:
        action='WAIT';tone='wait';buyer='The evidence is mixed. Keep this on your watchlist and wait for stronger trend and volume confirmation.'
    else:
        action='AVOID NEW ENTRY';tone='bear';buyer='The model sees weak conditions for a new long trade. Wait for improvement; this is not a short-selling signal.'
    return {'action':action,'tone':tone,'new_buyer':buyer,'holder':'Review your existing stop and position size. If the setup weakens, review an exit against your own plan. Do not move your stop lower just because this model recalculates it.','steps':['Read the reason for the setup, including weak factors.','Confirm the current price and liquidity with your broker.','Set a stop before entry and calculate shares using your saved risk limit.','Try the trade in the paper portfolio. Review the outcome before considering real money.']}

class MarketService:
    def __init__(self,connect,lock,apply_snapshot):
        self.connect=connect;self.lock=lock;self.apply=apply_snapshot
        self.wake=threading.Event();self.stop_event=threading.Event();self.busy=False
        self.thread=None;self.next_refresh=None;self.last_cycle=0;self.rate_limited_until=0
    def status(self):
        return {'running':bool(self.thread and self.thread.is_alive()),'busy':self.busy,'open_poll_seconds':OPEN_POLL,'closed_poll_seconds':CLOSED_POLL,'next_refresh':self.next_refresh}
    def start(self):
        self.thread=threading.Thread(target=self.run,name='pypsx-market-data',daemon=True);self.thread.start()
    def stop(self):self.stop_event.set();self.wake.set()
    def request(self):self.wake.set()
    def cycle(self):
        if not pypsx_provider.status()['configured']:return CLOSED_POLL
        now_tick=time.monotonic()
        remaining=max(self.rate_limited_until-now_tick,30-(now_tick-self.last_cycle))
        if remaining>0:return max(1,int(remaining)+1)
        self.last_cycle=now_tick
        self.busy=True
        try:
            try:market=market_snapshot(pypsx_provider.get_json('/market/status',{}))
            except ValueError as e:market={'is_open':None,'checked_at':datetime.now(KARACHI).isoformat(),'error':str(e),'status_text':'Status unavailable'}
            with self.lock,self.connect() as db:
                db.execute('INSERT OR REPLACE INTO provider_state VALUES(?,?)',('market',json.dumps(market)))
                rows=[dict(r) for r in db.execute('SELECT * FROM watchlist WHERE active=1 ORDER BY symbol')]
            for row in rows:
                if self.stop_event.is_set():break
                try:
                    bars,meta=pypsx_provider.fetch_history(row['symbol'],row['interval'],row['days'])
                    with self.lock,self.connect() as db:self.apply(db,row['symbol'],bars,meta)
                except (ValueError,KeyError,TypeError) as e:
                    with self.lock,self.connect() as db:
                        db.execute('UPDATE watchlist SET error=?,attempted_at=? WHERE symbol=?',(str(e)[:300],datetime.now(KARACHI).isoformat(),row['symbol']))
                    # Rate-limit errors end this cycle rather than retrying every symbol.
                    if 'rate limit' in str(e).lower():
                        self.rate_limited_until=time.monotonic()+CLOSED_POLL
                        return CLOSED_POLL
            return OPEN_POLL if market.get('is_open') is True else CLOSED_POLL
        finally:self.busy=False
    def run(self):
        while not self.stop_event.is_set():
            self.wake.clear()
            try:delay=self.cycle()
            except Exception:
                # Never print provider payloads or credentials. Surface failure in status.
                delay=CLOSED_POLL
                try:
                    with self.lock,self.connect() as db:
                        error={'is_open':None,'checked_at':datetime.now(KARACHI).isoformat(),'error':'Background update failed. Check connection settings and refresh.','status_text':'Update failed'}
                        db.execute('INSERT OR REPLACE INTO provider_state VALUES(?,?)',('market',json.dumps(error)))
                except Exception:pass
            self.next_refresh=(datetime.now(KARACHI)+timedelta(seconds=delay)).isoformat()
            self.wake.wait(delay)
