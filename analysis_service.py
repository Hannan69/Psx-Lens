"""Independent multi-timeframe research. Never replaces execution candles or positions."""
import math
import re
import threading
import time
from datetime import datetime,timedelta
from engine import KARACHI,features
from market_service import availability,guidance
import pypsx_provider as provider

WINDOWS={'3M':90,'1M':30,'5D':5}
INTERVALS={'auto','4h','15m'}

def profile(window,interval):
    if window not in WINDOWS or interval not in INTERVALS:raise ValueError('Choose 3M, 1M, or 5D history and Auto, 4h, or 15m candles.')
    actual=({'3M':'1d','1M':'1h','5D':'15m'}[window] if interval=='auto' else interval)
    return actual,('15m' if actual=='4h' else actual),(365 if actual=='1d' else max(65,WINDOWS[window]+10))

def fetch_research_history(symbol,interval,days):
    if interval=='1d':return provider.fetch_history(symbol,interval,days)
    now=datetime.now(KARACHI)
    payload=provider.get_json('/api/v1/market/historical-intraday',{'symbol':symbol,'from':(now-timedelta(days=days)).isoformat(),'to':now.isoformat(),'interval':interval})
    gaps=[]
    if isinstance(payload,dict) and isinstance(payload.get('results'),list):
        matches=[r for r in payload['results'] if r.get('symbol')==symbol and r.get('interval',interval)==interval]
        if len(matches)==1 and matches[0].get('gaps'):
            block=matches[0];gaps=block['gaps']
            last_end=max(datetime.fromisoformat(g['end']).date() for g in gaps)
            # Restart indicators after the last known outage. Never bridge a gap.
            kept=[b for b in block['candles'] if datetime.fromisoformat(str(b[0]).replace('Z','+00:00')).astimezone(KARACHI).date()>last_end]
            payload={'results':[{**block,'candles':kept,'gaps':[]}]}
    bars,omitted=provider.normalize_history(payload,symbol,interval,now)
    return bars,{'interval':interval,'synced_at':now.isoformat(),'omitted_incomplete':omitted,'environment':provider.config()[1],'excluded_gaps':gaps}

def four_hour_bars(bars,now=None):
    """Session-anchored 4h maximum buckets; Friday sessions never bridge lunch.

    15m exchange buckets are anchored 09:30 Mon-Thu, 09:15/14:30 Fri.
    Session-closing buckets are shorter; timestamps label bucket starts.
    """
    now=now or datetime.now(KARACHI);groups={}
    for b in bars:
        t=datetime.fromisoformat(b['time']).astimezone(KARACHI)
        if t.weekday()>4:continue
        minute=t.hour*60+t.minute
        if t.weekday()==4:
            if 555<=minute<720:anchor,close=555,720
            elif 870<=minute<990:anchor,close=870,990
            else:continue
        elif 570<=minute<930:anchor,close=570,930
        else:continue
        start=anchor+((minute-anchor)//240)*240
        end=min(start+240,close)
        midnight=t.replace(hour=0,minute=0,second=0,microsecond=0)
        start_time=midnight+timedelta(minutes=start);end_time=midnight+timedelta(minutes=end)
        if end_time+timedelta(minutes=1)>now:continue
        groups.setdefault((start_time.isoformat(),end_time.isoformat()),[]).append(b)
    result=[]
    for (start,end),items in sorted(groups.items()):
        items.sort(key=lambda b:b['time'])
        result.append({'time':start,'open':items[0]['open'],'high':max(b['high'] for b in items),'low':min(b['low'] for b in items),'close':items[-1]['close'],'volume':sum(b['volume'] for b in items)})
    return result

def volume_context(bars,daily):
    volume=sum(b['volume'] for b in bars)
    balance=sum((2*b['close']-b['high']-b['low'])/(b['high']-b['low'])*b['volume'] for b in bars if b['high']>b['low'])
    recent=daily[-20:]
    return {'close_location_balance_pct':round(100*balance/volume,2) if volume else None,
            'volume_shares':volume,'zero_volume_bars_pct':round(100*sum(b['volume']==0 for b in bars)/len(bars),1) if bars else None,
            'average_daily_shares':sum(b['volume'] for b in recent)/len(recent) if recent else None,
            'approx_daily_value':sum(b['close']*b['volume'] for b in recent)/len(recent) if recent else None,
            'daily_sessions':len(recent),
            'method':'Volume-weighted close location within each candle, not actual buyer/seller volume. Daily traded value is approximated as close × volume. Spread and available execution size are unknown.'}

def build_analysis(symbol,window,interval,bars,daily,meta,market,now=None):
    now=now or datetime.now(KARACHI);actual,_,_=profile(window,interval)
    if actual=='4h':bars=four_hour_bars(bars,now)
    if not bars:raise ValueError('No completed candles are available at this interval.')
    sufficient=len(bars)>=35
    f=features(bars[-301:],timeframe='1d' if actual=='1d' else actual) if sufficient else {'score':None,'signal':'INSUFFICIENT HISTORY','price':bars[-1]['close'],'ema9':None,'ema21':None,'rsi':None,'volume_ratio':None,'support':None,'resistance':None,'risk':None,'components':[]}

    cutoff=now-timedelta(days=WINDOWS[window]);shown=[b for b in bars if datetime.fromisoformat(b['time'])>=cutoff]
    if not shown:raise ValueError('No completed candles in this history window.')
    interval_meta={**meta,'interval':actual}
    ready=availability('pyPSX · '+actual,bars[-1]['time'],interval_meta,market,now=now)
    # These are research scenarios; slower candles do not establish scalp readiness.
    if actual=='4h' and sufficient:
        ready={**ready,'actionable':False,'state':'historical','label':'4h research context','message':'Session-anchored 4-hour research bars, including shorter closing bars. Use a fresh 15-minute setup and broker quote for entry timing.'}
    if not sufficient:ready={'actionable':False,'state':'unavailable','label':'Insufficient indicator warmup','message':f'Only {len(bars)} completed {actual} candles after the last known gap; at least 35 are required for model levels. The chart remains available for review.'}
    f.update({'symbol':symbol,'name':symbol,'source':'pyPSX · '+actual,'interval':actual,'bars':shown,'time':bars[-1]['time'],'sync':interval_meta,'availability':ready,'analysis_window':window,'history_count':len(bars),'snapshot_signal':f['signal']})
    f['guidance']=guidance(f,ready)
    if not ready['actionable']:
        f['signal']='NO CURRENT SIGNAL'
        f['guidance']={**f['guidance'],'action':'RESEARCH CONTEXT','new_buyer':ready['message']}
    return {'stock':f,'history_sufficient':sufficient,'window':window,'requested_interval':interval,'warmup_candles':len(bars)-len(shown),'volume':volume_context(shown,daily),
            'purpose':{'3M':'Short-to-mid-term trend context','1M':'Swing context and trading activity','5D':'Recent short-swing structure'}[window],
            'order_flow':{'available':False,'message':'Not connected. Current ingestion has no verified order book or trade-side tape. Documented broker routes returned HTTP 404 here; standard SDK depth access is still unverified.'},
            'method':'Historical context and conditional model levels, not a forecast over the chosen window. The existing heuristic strategy has not been validated separately for these horizons.',
            'coverage_notice':('Provider reports history gaps. This analysis restarts after the last gap; the requested window may be shorter. Excluded gaps: '+', '.join(g['start']+'–'+g['end'] for g in meta.get('excluded_gaps',[]))) if meta.get('excluded_gaps') else None,
            'aggregation':'4h maximum buckets anchored at 09:30 Mon–Thu and 09:15 / 14:30 Fri (PKT). Closing buckets are shorter; unfinished buckets are excluded.' if actual=='4h' else 'Provider OHLCV; unfinished candles excluded.'}

class AnalysisService:
    def __init__(self):self.lock=threading.Lock();self.cache={};self.daily={};self.busy=False
    def get(self,symbol,window,interval,market):
        symbol=symbol.strip().upper()
        if not re.fullmatch(r'[A-Z0-9-]{1,12}',symbol):raise ValueError('Invalid PSX symbol.')
        profile(window,interval);key=(symbol,window,interval);tick=time.monotonic()
        with self.lock:
            entry=self.cache.get(key)
            if (not entry or tick-entry['attempted']>entry.get('ttl',60)) and not self.busy:
                self.busy=True
                self.cache[key]={**(entry or {}),'attempted':tick,'refreshing':True}
                threading.Thread(target=self._fetch,args=(key,),daemon=True,name='pypsx-analysis').start()
                entry=self.cache[key]
            if not entry:return {'status':'loading','message':'Another analysis request is finishing. This view will retry automatically.'}
            data={k:v for k,v in entry.items() if k not in ('bars','daily','meta')}
            if 'bars' in entry:
                data['analysis']=build_analysis(symbol,window,interval,entry['bars'],entry['daily'],entry['meta'],market)
                if entry.get('error'):
                    f=data['analysis']['stock'];f['availability']={'actionable':False,'state':'error','label':'Analysis update failed','message':entry['error']};f['guidance']=guidance(f,f['availability'])
                data['status']='ready'
            else:data['status']='error' if entry.get('error') else 'loading'
            return data
    def _fetch(self,key):
        symbol,window,interval=key
        try:
            actual,base,days=profile(window,interval)
            bars,meta=fetch_research_history(symbol,base,days)
            cached=self.daily.get(symbol)
            if base=='1d':daily=bars
            elif cached and time.monotonic()-cached[0]<3600:daily=cached[1]
            else:daily,_=provider.fetch_history(symbol,'1d',365)
            # Validate before publishing; no SQLite/execution state is touched.
            build_analysis(symbol,window,interval,bars,daily,meta,{})
            with self.lock:
                self.daily[symbol]=(time.monotonic(),daily)
                self.cache[key]={'bars':bars,'daily':daily,'meta':meta,'attempted':time.monotonic(),'refreshing':False,'error':None,'ttl':300 if actual=='1d' else 60}
        except (ValueError,TypeError,KeyError,OverflowError) as e:
            with self.lock:self.cache[key]={**self.cache[key],'error':str(e)[:300],'refreshing':False,'ttl':300}
        except Exception:
            with self.lock:self.cache[key]={**self.cache[key],'error':'Analysis could not update. Retry after the connection recovers.','refreshing':False,'ttl':300}
        finally:
            with self.lock:
                self.busy=False
                if len(self.cache)>32:
                    oldest=min(self.cache,key=lambda k:self.cache[k]['attempted'])
                    del self.cache[oldest]
