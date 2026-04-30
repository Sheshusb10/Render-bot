"""
ALPHA BOT — Delta Exchange India
Single-user, all bugs fixed, clean build.
"""
import os,time,hmac,hashlib,json,math,logging,threading,requests
from datetime import datetime,timezone,timedelta
from flask import Flask,jsonify,request,Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("bot")

class C:
    BASE="https://api.india.delta.exchange"
    KEY=os.getenv("DELTA_API_KEY","").strip()
    SECRET=os.getenv("DELTA_API_SECRET","").strip()
    PID=27; SYMBOL="BTCUSD"; LOT=0.001; LEV=5; SCAN=300
    STOP_PCT=0.025; TP_PCT=0.030; RISK_PCT=0.015
    OPT_TP=0.70          # +70% = full take profit
    OPT_STOP=0.15        # -15% = stop loss (tight)
    OPT_LOCK=0.64        # keep 64% of peak profit when trailing
    # "profit was 5 → floor at 3.2" = 5 * 0.64 = 3.2  ✓
    # Trail starts from FIRST profit tick (peak_pct > 0)
    OPT_FLOOR=0.00       # unused — trail starts immediately
    OPT_FLOOR_TRAIL=0.25 # unused — replaced by OPT_LOCK
    OPT_MAX_PREM=0.15; OPT_EXPIRY_BUF=180
    HALT_PCT=0.08; PAUSE_PCT=0.03; COOLDOWN=30
    CIRCUIT_N=3; CIRCUIT_MIN=120; MIN_HOLD=15; ADX_MIN=22
    STATE="/tmp/ab.json"

def pid_int(v):
    try: return int(v)
    except: return 0

class DeltaAPI:
    def __init__(self):
        self.key=C.KEY; self.sec=C.SECRET
        self.sess=requests.Session(); self._lock=threading.Lock()
    def set(self,k,s): self.key=k.strip(); self.sec=s.strip()
    def _sign(self,method,path,qs="",body=""):
        with self._lock: ts=str(int(time.time()))
        sig=hmac.new(self.sec.encode(),(method+ts+path+qs+body).encode(),hashlib.sha256).hexdigest()
        return {"api-key":self.key,"timestamp":ts,"signature":sig,"Content-Type":"application/json"}
    def get(self,path,p=None):
        qs=("?"+"&".join(f"{k}={v}" for k,v in p.items())) if p else ""
        try:
            r=self.sess.get(f"{C.BASE}{path}{qs}",headers=self._sign("GET",path,qs),timeout=10)
            return r.json()
        except Exception as e: log.warning(f"GET {path}: {e}"); return None
    def post(self,path,body):
        b=json.dumps(body)
        try:
            r=self.sess.post(f"{C.BASE}{path}",headers=self._sign("POST",path,"",b),data=b,timeout=10)
            return r.json()
        except Exception as e: log.warning(f"POST {path}: {e}"); return {}
    def price(self):
        try: return float(self.sess.get(f"{C.BASE}/v2/tickers/BTCUSD",timeout=6).json().get("result",{}).get("mark_price",0) or 0)
        except: return 0.0
    def balance(self):
        d=self.get("/v2/wallet/balances")
        if not d: return 0.0,None,"No response"
        if not d.get("success"):
            err=d.get("error",{}); code=err.get("code","") if isinstance(err,dict) else str(err)
            return 0.0,d,f"API error: {code} {d.get('message','')}"
        for b in d.get("result",[]):
            if str(b.get("asset_symbol","")).upper() in ("USD","USDT"):
                av=float(b.get("available_balance",0) or 0); bk=float(b.get("blocked_margin",0) or 0)
                if av+bk>0: return round(av+bk,2),d,"ok"
        ne=float((d.get("meta") or {}).get("net_equity",0) or 0)
        if ne>0: return round(ne,2),d,"ok"
        return 0.0,d,"Zero balance"
    def candles(self,res="5m",n=100):
        mins={"1m":1,"5m":5,"15m":15}.get(res,5); end=int(time.time())
        for rf in [res,mins]:
            d=self.get("/v2/history/candles",{"symbol":C.SYMBOL,"resolution":rf,"start":end-mins*60*n,"end":end})
            if d and d.get("success") and d.get("result"): return d["result"]
        return []
    def btcusd_pos(self):
        d=self.get("/v2/positions/margined")
        if not d or not d.get("success"): return []
        return [p for p in d.get("result",[]) if pid_int(p.get("product_id",0))==C.PID and abs(float(p.get("size",0) or 0))>0]
    def opt_pos(self):
        d=self.get("/v2/positions/margined")
        if not d or not d.get("success"): return []
        return [p for p in d.get("result",[]) if str(p.get("product_symbol","")).startswith(("C-BTC","P-BTC")) and float(p.get("size",0) or 0)>0]
    def order(self,side,lots,pid=None):
        return self.post("/v2/orders",{"product_id":pid or C.PID,"size":lots,"side":side,"order_type":"market_order","time_in_force":"ioc"})
    def bracket(self,side,lots,stop,tp):
        return self.post("/v2/orders",{"product_id":C.PID,"size":lots,"side":side,"order_type":"stop_market_order","stop_price":str(round(stop,1)),"bracket_stop_loss_price":str(round(stop,1)),"bracket_take_profit_price":str(round(tp,1)),"time_in_force":"gtc","stop_trigger_method":"mark_price"})
    def close(self,size,pid=None):
        return self.post("/v2/orders",{"product_id":pid or C.PID,"size":abs(int(size)),"side":"sell" if size>0 else "buy","order_type":"market_order","time_in_force":"ioc"})
    def get_opt_pid(self,symbol):
        prefix="call_options" if symbol.startswith("C-") else "put_options"
        d=self.get("/v2/products",{"contract_type":prefix,"state":"live"})
        if d and d.get("success"):
            for p in d.get("result",[]):
                if p.get("symbol")==symbol: return p.get("id")
        td=self.get(f"/v2/tickers/{symbol}")
        if td and td.get("success"): return td.get("result",{}).get("product_id")
        return None

def _parse(raw):
    out=[]
    for c in raw:
        try:
            v=float(c.get("close",0) or 0)
            if v>0: out.append({"close":v,"high":float(c.get("high",v) or v),"low":float(c.get("low",v) or v),"volume":float(c.get("volume",0) or 0)})
        except: pass
    return out

def ema(p,n):
    if len(p)<n: return [p[-1]]*len(p) if p else []
    k=2/(n+1); v=[sum(p[:n])/n]
    for x in p[n:]: v.append(x*k+v[-1]*(1-k))
    return [v[0]]*(n-1)+v

def rsi(p,n=14):
    if len(p)<n+2: return 50.0
    d=[p[i]-p[i-1] for i in range(1,len(p))]
    g=sum(max(x,0) for x in d[-n:])/n; l=sum(abs(min(x,0)) for x in d[-n:])/n
    return round(100 if l<1e-10 else 100-100/(1+g/l),1)

def adx_calc(hi,lo,cl,n=14):
    if len(cl)<n*2+1: return 0.0,0.0,0.0
    tr,pm,nm=[],[],[]
    for i in range(1,len(cl)):
        tr.append(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])))
        u=hi[i]-hi[i-1]; d=lo[i-1]-lo[i]
        pm.append(u if u>d and u>0 else 0.0); nm.append(d if d>u and d>0 else 0.0)
    def ws(a):
        s=sum(a[:n]); r=[s]
        for v in a[n:]: s=s-s/n+v; r.append(s)
        return r
    at=ws(tr); pd=ws(pm); nd=ws(nm)
    pi=[100*pd[i]/at[i] if at[i]>0 else 0 for i in range(len(at))]
    ni=[100*nd[i]/at[i] if at[i]>0 else 0 for i in range(len(at))]
    dx=[abs(pi[i]-ni[i])/(pi[i]+ni[i])*100 if pi[i]+ni[i]>0 else 0 for i in range(len(pi))]
    return round(sum(dx[-n:])/n,1),round(pi[-1],1),round(ni[-1],1)

def atr_val(hi,lo,cl,n=14):
    if len(cl)<n+1: return 0.0
    return sum(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])) for i in range(1,len(cl)))/(len(cl)-1)

def atr_tp_sl(atr_pct):
    if atr_pct<=0: return C.TP_PCT,C.STOP_PCT
    if atr_pct<0.30: return max(atr_pct*1.5/100,0.008),max(atr_pct*1.0/100,0.005)
    if atr_pct<0.80: return atr_pct*2.0/100,atr_pct*1.0/100
    return min(atr_pct*3.0/100,0.08),min(atr_pct*1.5/100,0.04)

def bollinger(cl,n=20):
    if len(cl)<n: m=cl[-1]; return m,m,m,0.0
    w=cl[-n:]; m=sum(w)/n; s=math.sqrt(sum((p-m)**2 for p in w)/n)
    return m+2*s,m,m-2*s,(4*s/m*100) if m>0 else 0.0

def macd(cl,fast=12,slow=26,sig=9):
    if len(cl)<slow+sig: return 0.0,0.0,0.0
    ef=ema(cl,fast); es=ema(cl,slow); line=[ef[i]-es[i] for i in range(len(es))]
    signal=ema(line,sig); return round(line[-1],2),round(signal[-1],2),round(line[-1]-signal[-1],4)

PCOLS={"Regime":"#3b82f6","MTF Align":"#00b386","RSI":"#f59e0b","MACD":"#8b5cf6","Volatility":"#ec4899","Volume":"#e74c3c","Session":"#14b8a6"}

def score_signal(candles,direction,hour):
    c5m=candles.get("5m",[]); c1m=candles.get("1m",[]); c15m=candles.get("15m",[])
    if len(c5m)<30: return {"total":0,"veto":f"need_30_have_{len(c5m)}","regime":"UNKNOWN","strategy":"WAIT","pillars":{},"vol_regime":"UNKNOWN","adx":0,"bw":0,"atr_pct":0}
    cl5=[c["close"] for c in c5m]; hi5=[c["high"] for c in c5m]; lo5=[c["low"] for c in c5m]; vo5=[c["volume"] for c in c5m]
    cl1=[c["close"] for c in c1m] if len(c1m)>=20 else cl5
    cl15=[c["close"] for c in c15m] if len(c15m)>=21 else cl5; hi15=[c["high"] for c in c15m] if len(c15m)>=21 else hi5; lo15=[c["low"] for c in c15m] if len(c15m)>=21 else lo5
    price=cl5[-1]; p={}
    # Regime
    adx_v,pdi,ndi=adx_calc(hi5,lo5,cl5)
    e8=ema(cl5,8)[-1]; e21=ema(cl5,21)[-1]; e55=ema(cl5,55)[-1] if len(cl5)>=55 else cl5[0]
    bull=price>e8>e21 and adx_v>20 and pdi>ndi; bear=price<e8<e21 and adx_v>20 and ndi>pdi
    if direction=="long" and bull: rs,rd=25,"Bull regime"
    elif direction=="short" and bear: rs,rd=25,"Bear regime"
    elif adx_v>15: rs,rd=12,"Weak trend"
    else: rs,rd=3,"No trend"
    p["Regime"]={"score":rs,"max":25,"detail":rd,"adx":round(adx_v,1)}
    # MTF
    ms=0; md=[]
    for tfc,lbl in [(cl1,"1m"),(cl15,"15m")]:
        if len(tfc)<21: continue
        e8t=ema(tfc,8)[-1]; e21t=ema(tfc,21)[-1]
        if direction=="long" and tfc[-1]>e8t>e21t: ms+=10; md.append(f"{lbl}↑")
        elif direction=="short" and tfc[-1]<e8t<e21t: ms+=10; md.append(f"{lbl}↓")
        else: md.append(f"{lbl}~")
    p["MTF Align"]={"score":min(ms,20),"max":20,"detail":" ".join(md) or "checking"}
    # RSI
    r5=rsi(cl5); r1=rsi(cl1) if len(cl1)>=16 else r5
    if direction=="long":
        if 35<=r5<=55 and r1>r5: rs2,rd2=15,"Pullback+rising"
        elif r5<35: rs2,rd2=12,"Oversold"
        elif r5<=65: rs2,rd2=7,"Mid-range"
        else: rs2,rd2=3,"Overbought"
    else:
        if 45<=r5<=65 and r1<r5: rs2,rd2=15,"Distribution"
        elif r5>65: rs2,rd2=12,"Overbought"
        elif r5>=35: rs2,rd2=7,"Mid-range"
        else: rs2,rd2=3,"Oversold"
    p["RSI"]={"score":rs2,"max":15,"detail":rd2,"rsi5":r5}
    # MACD
    ln,sg,hist=macd(cl5)
    if direction=="long":
        if hist>0 and ln>sg: ms3,md3=15,"Bullish"
        elif hist>0: ms3,md3=8,"Hist+"
        else: ms3,md3=2,"Bearish"
    else:
        if hist<0 and ln<sg: ms3,md3=15,"Bearish"
        elif hist<0: ms3,md3=8,"Hist-"
        else: ms3,md3=2,"Bullish"
    p["MACD"]={"score":ms3,"max":15,"detail":md3}
    # Vol
    _,_,_,bw=bollinger(cl5); atr_pct=atr_val(hi5,lo5,cl5)/price*100 if price>0 else 0
    if 0.5<bw<4.0 and 15<adx_v<50: vs,vd=10,"Ideal"
    elif bw<0.5: vs,vd=8,"Squeeze"
    elif bw>6.0: vs,vd=3,"Extreme"
    else: vs,vd=6,"Normal"
    p["Volatility"]={"score":vs,"max":10,"detail":vd,"bw":round(bw,2),"atr_pct":round(atr_pct,3)}
    # Volume
    if len(vo5)>=21:
        avg5=sum(vo5[-21:-1])/20; cur=vo5[-2]
        if cur<avg5*0.1: p["Volume"]={"score":0,"max":10,"detail":"trap"}
        elif cur>avg5*2: p["Volume"]={"score":10,"max":10,"detail":"Spike"}
        elif cur>avg5*1.3: p["Volume"]={"score":7,"max":10,"detail":"Above avg"}
        else: p["Volume"]={"score":5,"max":10,"detail":"Normal"}
    else: p["Volume"]={"score":5,"max":10,"detail":"no data"}
    # Session
    prime=[8,9,13,14,15,16,21,22,23,0]; dead=[2,3,4,5,6]
    if hour in dead: p["Session"]={"score":0,"max":5,"detail":"dead zone"}
    elif hour in prime: p["Session"]={"score":5,"max":5,"detail":"prime"}
    else: p["Session"]={"score":3,"max":5,"detail":"off-peak"}
    # Binance lead
    bnc_lead=candles.get("binance_lead","neutral"); lb=0
    if direction=="long" and bnc_lead=="binance_leading_bull": lb=8
    if direction=="short" and bnc_lead=="binance_leading_bear": lb=8
    total=min(sum(v["score"] for v in p.values())+lb,100)
    # Regime label
    if price>e8 and adx_v>25 and pdi>ndi and price>e21: regime="STRONG_BULL"
    elif price>e8>e21 and adx_v>18: regime="BULL"
    elif price<e8 and adx_v>25 and ndi>pdi and price<e21: regime="STRONG_BEAR"
    elif price<e8<e21 and adx_v>18: regime="BEAR"
    elif adx_v<15: regime="SIDEWAYS"
    else: regime="NEUTRAL"
    vol_regime="LOW" if bw<1.5 and adx_v<18 else "HIGH" if bw>5 or atr_pct>0.8 else "NORMAL"
    veto=""
    if hour in [2,3,4,5]: veto="dead_zone"
    if adx_v<12 and vol_regime=="NORMAL": veto="ADX<12"
    if veto: strategy="WAIT"
    elif regime=="SIDEWAYS" and vol_regime=="LOW" and bw<1.5: strategy="STRADDLE"
    elif vol_regime=="HIGH" and total>=62: strategy="SCALP"
    elif total>=62 and regime in ("STRONG_BULL","STRONG_BEAR"): strategy="SWING"
    elif total>=62: strategy="SCALP"
    else: strategy="WAIT"
    if strategy=="STRADDLE": fd="straddle"
    elif total<62 or veto: fd="wait"
    elif direction=="long" and regime in ("BULL","STRONG_BULL"): fd="long"
    elif direction=="short" and regime in ("BEAR","STRONG_BEAR"): fd="short"
    else: fd="wait"
    return {"total":total,"pillars":p,"veto":veto,"regime":regime,"volatility_regime":vol_regime,"strategy":strategy,"direction":fd,"adx":round(adx_v,1),"bw":round(bw,2),"atr_pct":round(atr_pct,3)}

class OptionsEngine:
    def __init__(self,api):
        self.api=api
        self._peak={}   # symbol -> peak mark price
        self._opened={} # symbol -> datetime

    def next_friday(self):
        from datetime import date,timedelta
        today=date.today(); days=(4-today.weekday())%7
        if days==0: days=7
        return (today+timedelta(days=days)).strftime("%d%m%y")

    def atm(self,price,interval=500): return round(price/interval)*interval

    def find_option(self,opt_type,price,use_itm=False):
        prefix="C" if opt_type=="call" else "P"; expiry=self.next_friday(); atm=self.atm(price)
        candidates=[atm-500,atm] if use_itm and opt_type=="call" else [atm+500,atm] if use_itm else [atm,atm+500 if opt_type=="call" else atm-500]
        for strike in candidates:
            sym=f"{prefix}-BTC-{strike}-{expiry}"
            d=self.api.get(f"/v2/tickers/{sym}")
            if d and d.get("success"):
                res=d.get("result",{}); mark=float(res.get("mark_price",0) or 0)
                if mark<=0: continue
                bid=float(res.get("best_bid",0) or 0); ask=float(res.get("best_ask",0) or 0); iv=float(res.get("mark_iv",0) or 0)
                if iv>150 and iv>0: continue
                spread=(ask-bid)/mark*100 if mark>0 and ask>bid else 0
                if spread>20 and bid>0: continue
                return {"found":True,"symbol":sym,"strike":strike,"expiry":expiry,"type":opt_type,"mark":mark,"bid":bid,"ask":ask,"iv":round(iv,1),"moneyness":"ITM" if use_itm else "ATM","premium_usd":round(mark*C.LOT,3)}
        return {"found":False,"tried":candidates,"expiry":expiry}

    def should_exit(self,sym,cur_mark,entry_mark,opened_at):
        if entry_mark<=0: return {"exit":False,"reason":""}
        pct=(cur_mark-entry_mark)/entry_mark
        peak=self._peak.get(sym,entry_mark)
        if cur_mark>peak: self._peak[sym]=cur_mark; peak=cur_mark
        peak_pct=(peak-entry_mark)/entry_mark
        drop_from_peak=(peak-cur_mark)/peak if peak>0 else 0
        # Expiry check
        now=datetime.now(timezone.utc)
        expiry_str=sym[-6:] if len(sym)>=6 else ""
        if expiry_str:
            try:
                exp_dt=datetime.strptime(expiry_str,"%d%m%y").replace(hour=12,minute=0,tzinfo=timezone.utc)
                if now>=exp_dt-timedelta(minutes=C.OPT_EXPIRY_BUF):
                    return {"exit":True,"reason":f"expiry in {int((exp_dt-now).total_seconds()/60)}m","pct":pct}
            except: pass
        if pct>=C.OPT_TP: return {"exit":True,"reason":f"TP +{pct*100:.0f}%","pct":pct}
        if pct<=-C.OPT_STOP: return {"exit":True,"reason":f"SL {pct*100:.0f}%","pct":pct}
        # Floor trail: if peaked +15% and now dropped 25% from peak → exit
        if peak_pct>=C.OPT_FLOOR and drop_from_peak>=C.OPT_FLOOR_TRAIL:
            return {"exit":True,"reason":f"floor trail peaked+{peak_pct*100:.0f}% now+{pct*100:.1f}%","pct":pct}
        if opened_at and (now-opened_at).seconds<300: return {"exit":False,"reason":"min_hold_5m"}
        return {"exit":False,"reason":f"holding {pct*100:.1f}%","pct":pct}

    def straddle(self,price):
        c=self.find_option("call",price); p=self.find_option("put",price)
        if c.get("found") and p.get("found"):
            total=c["premium_usd"]+p["premium_usd"]
            return {"found":True,"call":c,"put":p,"total_premium_usd":round(total,3),"breakeven_up":c["strike"]+total/C.LOT,"breakeven_down":p["strike"]-total/C.LOT}
        return {"found":False}

    def record_open(self,sym): self._opened[sym]=datetime.now(timezone.utc); self._peak[sym]=0
    def record_close(self,sym): self._opened.pop(sym,None); self._peak.pop(sym,None)
    def opened_at(self,sym): return self._opened.get(sym)

class Bot:
    def __init__(self):
        self.api=DeltaAPI(); self.opts_eng=None
        self.running=False; self.connected=False; self.opts_mode=False
        self.capital=0.0; self.start_cap=0.0; self.day_start=0.0
        self.halted=False; self.halt_msg=""
        self.status="Not connected"; self.logs=[]; self.trades=[]
        self.scan_n=0; self.next_scan=None; self.price=0.0
        self.last_conf={}; self.total_tr=0; self.wins=0
        self._stops=set(); self._last_close=None; self._consec=0
        self._circuit=None; self._opened={}

    def emit(self,level,msg):
        e={"t":datetime.now(timezone.utc).strftime("%H:%M:%S"),"l":level,"m":msg}
        self.logs.append(e)
        if len(self.logs)>400: self.logs.pop(0)
        getattr(log,{"INFO":"info","WARN":"warning","ERROR":"error","TRADE":"info"}.get(level,"info"))(msg)

    @property
    def _state_path(self):
        return getattr(self, "_state_file", C.STATE)

    def save(self):
        try:
            peak={}
            if self.opts_eng: peak={str(k):v for k,v in self.opts_eng._peak.items()}
            json.dump({"start_cap":self.start_cap,"day_start":self.day_start,"halted":self.halted,"halt_msg":self.halt_msg,"total_tr":self.total_tr,"wins":self.wins,"trades":self.trades[-100:],"stops":[int(x) for x in self._stops],"consec":self._consec,"circuit":self._circuit.isoformat() if self._circuit else None,"last_close":self._last_close.isoformat() if self._last_close else None,"peak_premium":peak},open(self._state_path,"w"))
        except Exception as e: log.warning(f"save: {e}")

    def load(self):
        try:
            if not os.path.exists(self._state_path): return False
            s=json.load(open(self._state_path))
            self.start_cap=float(s.get("start_cap",0)); self.day_start=float(s.get("day_start",0))
            self.halted=bool(s.get("halted",False)); self.halt_msg=s.get("halt_msg","")
            self.total_tr=int(s.get("total_tr",0)); self.wins=int(s.get("wins",0))
            self.trades=s.get("trades",[]); self._stops=set(int(x) for x in s.get("stops",[]))
            self._consec=int(s.get("consec",0))
            cu=s.get("circuit"); self._circuit=datetime.fromisoformat(cu) if cu else None
            lc=s.get("last_close"); self._last_close=datetime.fromisoformat(lc) if lc else None
            return self.start_cap>0
        except: return False

    def connect(self,key,secret):
        self.api.set(key,secret)
        bal,raw,err=self.api.balance()
        if bal<=0:
            srv="unknown"
            try: srv=requests.get("https://api.ipify.org?format=json",timeout=4).json().get("ip","?")
            except: pass
            return {"success":False,"message":err,"server_ip":srv}
        self.capital=bal; self.connected=True
        self.opts_eng=OptionsEngine(self.api)
        if not self.load() or self.start_cap<=0:
            self.start_cap=bal; self.day_start=bal; self.save()
        # Restore peak premiums
        try:
            s=json.load(open(C.STATE))
            for sym,peak in s.get("peak_premium",{}).items():
                self.opts_eng._peak[sym]=float(peak)
        except: pass
        self.emit("INFO",f"Connected ${bal:.2f} | Start ${self.start_cap:.2f} | Halt <${self.start_cap*(1-C.HALT_PCT):.2f}")
        self._sync_pos()
        if not self.running: self.start()
        return {"success":True,"balance":bal}

    def _sync_wallet(self):
        bal,_,err=self.api.balance()
        if bal<=0: self.emit("WARN",f"Wallet: {err}"); return
        self.capital=bal
        if self.start_cap>0:
            loss=(self.start_cap-bal)/self.start_cap
            if loss>=C.HALT_PCT and not self.halted:
                self.halted=True; self.halt_msg=f"Down {loss*100:.1f}%"
                self.emit("ERROR",f"HALTED: {self.halt_msg}"); self.save()
        self.emit("INFO",f"Wallet ${bal:.2f} | {'HALTED' if self.halted else 'OK'}")

    def _sync_pos(self):
        for p in self.api.btcusd_pos():
            sz=float(p.get("size",0) or 0); entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            pid=pid_int(p.get("product_id",C.PID)); side="long" if sz>0 else "short"; lots=abs(int(sz))
            if not any(pid_int(t.get("pid",0))==pid and t.get("exit") is None for t in self.trades):
                now=datetime.now(timezone.utc)
                self.trades.append({"time":now.isoformat(),"side":side,"entry":round(entry,1),"exit":None,"lots":lots,"pnl":None,"pct":None,"reason":"synced","won":None,"pid":pid,"sym":C.SYMBOL})
                self._opened[pid]=now
            if pid not in self._stops and entry>0:
                sp=entry*(1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT)
                tp=entry*(1+C.TP_PCT if side=="long" else 1-C.TP_PCT)
                r=self.api.bracket("sell" if side=="long" else "buy",lots,sp,tp)
                if r.get("success"): self._stops.add(pid); self.save()
                else: self.emit("WARN",f"Stop failed — set manually ${sp:.0f}")

    def _check_perp_exits(self,positions):
        if not self.price: return
        for p in positions:
            sz=float(p.get("size",0) or 0); entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            side="long" if sz>0 else "short"
            pct=(self.price-entry)/entry if side=="long" else (entry-self.price)/entry
            lots=abs(int(sz)); pid=pid_int(p.get("product_id",C.PID))
            now=datetime.now(timezone.utc); opened=self._opened.get(pid)
            hold=(now-opened).seconds//60 if opened else C.MIN_HOLD+1
            if hold<C.MIN_HOLD: continue
            if pct<=-C.STOP_PCT: reason="stop"
            elif pct>=C.TP_PCT: reason="tp"
            else: continue
            r=self.api.close(sz,pid)
            if r.get("success"):
                pnl=round(entry*lots*C.LOT*pct,4); won=pct>0
                self.emit("TRADE",f"{'✅TP' if won else '❌SL'} {side.upper()} ${entry:.0f}→${self.price:.0f} P&L ${pnl:+.4f} held={hold}m")
                self._on_close(won,pnl,entry,self.price,lots,reason)

    def _check_opt_exits(self):
        if not self.opts_eng: return
        for p in self.api.opt_pos():
            sym=p.get("product_symbol",""); pid=p.get("product_id")
            size=float(p.get("size",0) or 0); entry=float(p.get("avg_entry_price") or p.get("entry_price") or 0)
            mark=float(p.get("mark_price") or 0)
            if size<=0 or entry<=0 or mark<=0 or not pid: continue
            check=self.opts_eng.should_exit(sym,mark,entry,self.opts_eng.opened_at(sym))
            if check["exit"]:
                r=self.api.close(size,pid)
                if r.get("success"):
                    pct=check.get("pct",0); pnl=round((mark-entry)*int(size)*C.LOT,4); won=pnl>0
                    self.emit("TRADE",f"{'✅' if won else '❌'} OPT {check['reason']} | {sym} | ${entry:.2f}→${mark:.2f} P&L ${pnl:+.4f}")
                    self.opts_eng.record_close(sym); self._on_close(won,pnl,entry,mark,int(size),check["reason"])

    def _on_close(self,won,pnl,entry,exit_p,lots,reason):
        now=datetime.now(timezone.utc); self._last_close=now
        if won: self._consec=0; self.wins+=1
        else:
            self._consec+=1
            if self._consec>=C.CIRCUIT_N:
                self._circuit=now+timedelta(minutes=C.CIRCUIT_MIN)
                self.emit("WARN",f"CIRCUIT BREAKER: {self._consec} losses — pause {C.CIRCUIT_MIN}min")
        for t in reversed(self.trades):
            if t.get("exit") is None and t.get("entry")==round(entry,1):
                t.update({"exit":round(exit_p,1),"pnl":pnl,"pct":round(pct/100 if abs(pct if (pct:=pnl/max(entry*lots*C.LOT,0.001))else 0)<2 else pct,2),"won":won,"reason":reason}); break
        self.save()

    def _pos_display(self,positions=None):
        if positions is None: positions=self.api.btcusd_pos()
        out=[]
        for p in positions:
            sz=float(p.get("size",0) or 0); entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            mark=float(p.get("mark_price") or self.price or entry); upnl=float(p.get("unrealized_pnl") or 0)
            side="long" if sz>0 else "short"; pct=((mark-entry)/entry if side=="long" else (entry-mark)/entry)*100
            out.append({"sym":C.SYMBOL,"side":side,"lots":abs(sz),"entry":round(entry,1),"mark":round(mark,1),"upnl":round(upnl,3),"pct":round(pct,2),"stop":round(entry*(1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT),1),"tp":round(entry*(1+C.TP_PCT if side=="long" else 1-C.TP_PCT),1)})
        return out

    def _opts_display(self):
        if not self.opts_eng: return []
        out=[]
        for p in self.api.opt_pos():
            sym=p.get("product_symbol",""); sz=float(p.get("size",0) or 0)
            entry=float(p.get("avg_entry_price") or p.get("entry_price") or 0); mark=float(p.get("mark_price") or 0)
            upnl=float(p.get("unrealized_pnl") or 0)
            if sz<=0: continue
            pct=(mark-entry)/entry*100 if entry>0 else 0; peak=self.opts_eng._peak.get(sym,entry)
            out.append({"sym":sym,"lots":int(sz),"entry":round(entry,4),"mark":round(mark,4),"upnl":round(upnl,3),"pct":round(pct,1),"peak":round(peak,4),"type":"CALL" if sym.startswith("C-") else "PUT"})
        return out

    def scan(self):
        self.scan_n+=1; self.next_scan=(datetime.now(timezone.utc)+timedelta(seconds=C.SCAN)).isoformat()
        p=self.api.price()
        if p>0: self.price=p
        if self.scan_n%5==0: self._sync_wallet()
        if self.halted: self.status=f"HALTED: {self.halt_msg}"; return
        # Candles
        sess=requests.Session()
        def binance(iv,n=100):
            try:
                r=sess.get("https://api.binance.com/api/v3/klines",params={"symbol":"BTCUSDT","interval":iv,"limit":n},timeout=8)
                if r.status_code!=200: return []
                return [{"close":float(c[4]),"high":float(c[2]),"low":float(c[3]),"volume":float(c[5])} for c in r.json()]
            except: return []
        d5m=_parse(self.api.candles("5m")); b5m=binance("5m")
        d1m=_parse(self.api.candles("1m")); b1m=binance("1m")
        d15m=_parse(self.api.candles("15m",60))
        c5m=d5m if len(d5m)>=55 else b5m; c1m=d1m if len(d1m)>=20 else b1m; c15m=d15m
        bnc_lead="neutral"
        if len(b1m)>=16 and len(d1m)>=16:
            diff=rsi([c["close"] for c in b1m])-rsi([c["close"] for c in d1m])
            if diff>8: bnc_lead="binance_leading_bull"
            elif diff<-8: bnc_lead="binance_leading_bear"
        candles={"5m":c5m,"1m":c1m,"15m":c15m,"binance_lead":bnc_lead}
        if len(c5m)<30: self.status=f"Fetching: {len(c5m)} candles"; return
        self.price=c5m[-1]["close"]
        real=self.api.btcusd_pos(); self._check_perp_exits(real); self._check_opt_exits(); self._sync_pos()
        hour=datetime.now(timezone.utc).hour
        rl=score_signal(candles,"long",hour); rs=score_signal(candles,"short",hour)
        best=rl if rl["total"]>=rs["total"] else rs
        self.last_conf=best; regime=best["regime"]; strat=best["strategy"]
        lv=rl.get("veto",""); sv=rs.get("veto","")
        self.emit("INFO",f"#{self.scan_n} ${self.price:,.0f}|{regime}|ADX={best['adx']} BW={best['bw']}|L={rl['total']}{'✗'+lv if lv else ''} S={rs['total']}{'✗'+sv if sv else ''}|→{strat}")
        now=datetime.now(timezone.utc)
        if self._circuit and now<self._circuit:
            left=int((self._circuit-now).seconds/60); self.status=f"Circuit breaker: {left}m"; return
        elif self._circuit and now>=self._circuit:
            self._circuit=None; self._consec=0; self.emit("INFO","Circuit lifted")
        if self._last_close and (now-self._last_close).seconds<C.COOLDOWN*60:
            gap=C.COOLDOWN-(now-self._last_close).seconds//60; self.status=f"Cooldown: {gap}m"; return
        if self.day_start>0 and (self.capital-self.day_start)/self.day_start<=-C.PAUSE_PCT:
            self.status="Paused — daily limit"; return
        if len(real)>=1:
            d=self._pos_display(real); x=d[0] if d else {}
            self.status=f"Holding {x.get('side','').upper()} {x.get('lots',0):.0f}L @ ${x.get('entry',0):,.0f} | UPL ${x.get('upnl',0):+.3f}"; return
        # Options mode
        if self.opts_mode and self.opts_eng:
            opt_pos=self.api.opt_pos()
            if opt_pos: self.status=f"Holding {len(opt_pos)} option(s)"; return
            if strat=="STRADDLE" and best["bw"]<1.5:
                st=self.opts_eng.straddle(self.price)
                if st.get("found") and st["total_premium_usd"]<=self.capital*C.OPT_MAX_PREM*2:
                    cc=self.api.get_opt_pid(st["call"]["symbol"]); pc=self.api.get_opt_pid(st["put"]["symbol"])
                    if cc: self.api.order("buy",1,cc)
                    if pc: self.api.order("buy",1,pc)
                    if cc and pc:
                        self.opts_eng.record_open(st["call"]["symbol"]); self.opts_eng.record_open(st["put"]["symbol"])
                        self.status=f"STRADDLE ${st['total_premium_usd']:.2f} BE±${abs(st['breakeven_up']-self.price):.0f}"
                        self.emit("TRADE",self.status); self.total_tr+=1
                        for opt,otype,pid in [(st["call"],"call",cc),(st["put"],"put",pc)]:
                            self.trades.append({"time":now.isoformat(),"side":otype,"entry":round(opt["mark"],4),"exit":None,"lots":1,"pnl":None,"pct":None,"reason":"straddle","won":None,"pid":str(pid),"sym":opt["symbol"]})
                        self.save()
                return
            # Directional option
            if rl["total"]>=C.CONF_TRADE and rl["total"]>=rs["total"]: opt_type="call"; conf=rl["total"]
            elif rs["total"]>=C.CONF_TRADE: opt_type="put"; conf=rs["total"]
            else: self.status=f"Options: waiting for signal (best={max(rl['total'],rs['total'])})"; return
            opt=self.opts_eng.find_option(opt_type,self.price,conf>=78)
            if not opt.get("found"): self.emit("WARN",f"No {opt_type} found"); return
            if opt["premium_usd"]>self.capital*C.OPT_MAX_PREM: self.emit("INFO","Premium too high"); return
            pid=self.api.get_opt_pid(opt["symbol"])
            if not pid: self.emit("WARN",f"No pid for {opt['symbol']}"); return
            r=self.api.order("buy",1,pid)
            if r.get("success"):
                self.opts_eng.record_open(opt["symbol"])
                self.status=f"OPT {opt_type.upper()} {opt['moneyness']} {opt['symbol']} ${opt['premium_usd']:.2f}"
                self.emit("TRADE",self.status); self.total_tr+=1
                self.trades.append({"time":now.isoformat(),"side":opt_type,"entry":round(opt["mark"],4),"exit":None,"lots":1,"pnl":None,"pct":None,"reason":strat.lower(),"won":None,"pid":str(pid),"sym":opt["symbol"]})
                self.save()
            return
        # Perps mode
        if strat=="WAIT" or best["total"]<C.CONF_TRADE:
            self.status=f"Watching | {regime} | score={best['total']}"; return
        direction=rl["direction"] if rl["total"]>rs["total"] else rs["direction"]
        if direction in ("wait","straddle"): self.status=f"Watching | {regime}"; return
        margin=self.price*C.LOT/C.LEV; lots=max(1,min(int(max(self.capital*C.RISK_PCT,margin)/margin),max(1,int(self.capital*.10/margin))))
        side="buy" if direction=="long" else "sell"
        r=self.api.order(side,lots)
        if not r.get("success"): self.emit("ERROR",f"Order failed: {r.get('error',r.get('message','?'))}"); return
        dyn_tp,dyn_sl=atr_tp_sl(best.get("atr_pct",0))
        sp=self.price*(1-dyn_sl if direction=="long" else 1+dyn_sl); tp=self.price*(1+dyn_tp if direction=="long" else 1-dyn_tp)
        self.api.bracket("sell" if direction=="long" else "buy",lots,sp,tp)
        self._opened[pid_int(C.PID)]=now
        self.status=f"{direction.upper()} {lots}L @ ${self.price:,.0f} conf={best['total']}"
        self.emit("TRADE",f"{self.status} | {strat}")
        self.total_tr+=1
        self.trades.append({"time":now.isoformat(),"side":direction,"entry":round(self.price,1),"exit":None,"lots":lots,"pnl":None,"pct":None,"reason":strat.lower(),"won":None,"pid":str(C.PID),"sym":C.SYMBOL})
        self.save()

    def start(self):
        if not self.running:
            self.running=True; threading.Thread(target=self._loop,daemon=True).start()
            self.emit("INFO","▶ Bot started")
    def stop(self): self.running=False; self.emit("INFO","■ Bot stopped")
    def _loop(self):
        while self.running:
            try: self.scan()
            except Exception as e: log.error(f"scan: {e}",exc_info=True); self.status=f"Error: {e}"
            time.sleep(C.SCAN)

    def state(self):
        sc=self.start_cap or self.capital; pnl=(self.capital-sc)/sc*100 if sc>0 else 0
        done=[t for t in self.trades if t.get("won") is not None]
        wr=sum(1 for t in done if t["won"])/len(done)*100 if done else 0
        cf=self.last_conf; pls=cf.get("pillars",{})
        return {"connected":self.connected,"running":self.running,"halted":self.halted,"halt_msg":self.halt_msg,"status":self.status,"price":round(self.price,1),"regime":cf.get("regime","—"),"strategy":cf.get("strategy","—"),"vol_regime":cf.get("volatility_regime","—"),"adx":cf.get("adx",0),"bw":cf.get("bw",0),"atr_pct":cf.get("atr_pct",0),"conf_long":sum(v["score"] for v in pls.values()) if pls else 0,"pillars":{k:{"s":v["score"],"m":v["max"],"d":v.get("detail","")} for k,v in pls.items()},"capital":round(self.capital,2),"start_cap":round(sc,2),"pnl_pct":round(pnl,2),"win_rate":round(wr,1),"total_trades":self.total_tr,"wins":self.wins,"next_scan":self.next_scan,"scan_n":self.scan_n,"opts_mode":self.opts_mode,"open_pos":self._pos_display(),"opts_pos":self._opts_display(),"trades":list(reversed(self.trades[-50:])),"logs":list(reversed(self.logs[-80:])),"guardrails":{"Stop loss":f"{C.STOP_PCT*100:.1f}%","Take profit":f"{C.TP_PCT*100:.1f}%","Opt TP":f"+{C.OPT_TP*100:.0f}%","Opt SL":f"-{C.OPT_STOP*100:.0f}%","Opt floor":f"+{C.OPT_FLOOR*100:.0f}% triggers trail","Opt trail":f"-{C.OPT_FLOOR_TRAIL*100:.0f}% from peak","Monthly halt":f"-{C.HALT_PCT*100:.0f}%","Daily pause":f"-{C.PAUSE_PCT*100:.0f}%","Cooldown":f"{C.COOLDOWN}min","Circuit":f"{C.CIRCUIT_N} losses","Min hold":f"{C.MIN_HOLD}min"}}



# ═══════════════════════════════════════════════════════════════════
#  MULTI-USER LAYER — 5 users, isolated bots, login required
# ═══════════════════════════════════════════════════════════════════
import secrets as _sec, hashlib as _hl, functools

MAX_USERS  = 5
_BOT_KEY   = os.getenv("BOT_SECRET", _sec.token_hex(32))
_UFILE     = "/tmp/ab_users.json"

class UserManager:
    def __init__(self):
        self._lk = threading.Lock()
        self.db  = self._load()

    def _load(self):
        try:
            if os.path.exists(_UFILE):
                return json.load(open(_UFILE))
        except: pass
        return {"users": {}, "invites": []}

    def _save(self):
        try: json.dump(self.db, open(_UFILE, "w"), indent=2)
        except Exception as e: log.warning(f"UserManager save: {e}")

    def _hash(self, pw):
        return _hl.pbkdf2_hmac("sha256", pw.encode(), b"alphabot2025", 200000).hex()

    def create_admin(self, username, password):
        """First-time setup only — creates admin when no users exist."""
        with self._lk:
            if self.db["users"]:
                return False, "Setup already done"
            uid = _sec.token_hex(8)
            self.db["users"][uid] = {
                "username": username,
                "pw_hash":  self._hash(password),
                "created":  datetime.now(timezone.utc).isoformat(),
                "is_admin": True,
            }
            self._save()
            return True, uid

    def gen_invite(self):
        with self._lk:
            if not self.db["users"]:
                return None, "Create admin first"
            code = _sec.token_urlsafe(12)
            self.db["invites"].append(code)
            self._save()
            return code, "ok"

    def register(self, invite, username, password):
        with self._lk:
            if invite not in self.db["invites"]:
                return False, "Invalid invite code"
            if len(self.db["users"]) >= MAX_USERS:
                return False, f"Maximum {MAX_USERS} users reached"
            for u in self.db["users"].values():
                if u["username"].lower() == username.lower():
                    return False, "Username already taken"
            if len(password) < 6:
                return False, "Password must be at least 6 characters"
            uid = _sec.token_hex(8)
            self.db["users"][uid] = {
                "username": username,
                "pw_hash":  self._hash(password),
                "created":  datetime.now(timezone.utc).isoformat(),
                "is_admin": False,
            }
            self.db["invites"].remove(invite)
            self._save()
            return True, uid

    def login(self, username, password):
        with self._lk:
            for uid, u in self.db["users"].items():
                if u["username"].lower() == username.lower() \
                        and u["pw_hash"] == self._hash(password):
                    return True, uid
            return False, None

    def get(self, uid):    return self.db["users"].get(uid)
    def all(self):         return {uid: {k:v for k,v in u.items() if k!="pw_hash"} for uid,u in self.db["users"].items()}
    def is_admin(self, uid): u=self.get(uid); return u and u.get("is_admin", False)
    def invites(self):     return list(self.db.get("invites", []))

um   = UserManager()
bots = {}   # uid -> Bot instance

def get_bot(uid):
    if uid not in bots:
        b = Bot()
        b._state_file = f"/tmp/ab_{uid}.json"
        bots[uid] = b
    return bots[uid]



# ═══════════════════════════════════════════════════════════════════
#  FLASK APP — Multi-user routes
# ═══════════════════════════════════════════════════════════════════
from flask import session

app = Flask(__name__)
app.secret_key = _BOT_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
CORS(app, supports_credentials=True)

# Single-user auto-connect from env (optional, for Render/legacy use)
if C.KEY and C.SECRET:
    threading.Thread(
        target=lambda: get_bot("env").connect(C.KEY, C.SECRET),
        daemon=True).start()

@app.after_request
def _headers(r):
    r.headers.update({
        "Access-Control-Allow-Origin":  request.headers.get("Origin", "*"),
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Credentials": "true",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
    })
    return r

def login_required(f):
    @functools.wraps(f)
    def wrapped(*a, **kw):
        if "uid" not in session:
            return jsonify({"error": "unauthorized"}), 401
        return f(*a, **kw)
    return wrapped

def admin_required(f):
    @functools.wraps(f)
    def wrapped(*a, **kw):
        uid = session.get("uid")
        if not uid or not um.is_admin(uid):
            return jsonify({"error": "forbidden"}), 403
        return f(*a, **kw)
    return wrapped

# ── Auth ──────────────────────────────────────────────────────────
@app.route("/auth/me")
def auth_me():
    uid = session.get("uid")
    if not uid: return jsonify({"logged_in": False})
    u = um.get(uid)
    if not u:   return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "username": u["username"], "is_admin": u.get("is_admin", False)})

@app.route("/auth/login", methods=["POST", "OPTIONS"])
def auth_login():
    if request.method == "OPTIONS": return jsonify({})
    d = request.json or {}
    ok, uid = um.login(d.get("username",""), d.get("password",""))
    if not ok:
        return jsonify({"success": False, "message": "Wrong username or password"}), 401
    session["uid"] = uid; session.permanent = True
    u = um.get(uid)
    return jsonify({"success": True, "username": u["username"], "is_admin": u.get("is_admin", False)})

@app.route("/auth/register", methods=["POST", "OPTIONS"])
def auth_register():
    if request.method == "OPTIONS": return jsonify({})
    d = request.json or {}
    ok, result = um.register(d.get("invite",""), d.get("username","").strip(), d.get("password",""))
    if not ok: return jsonify({"success": False, "message": result}), 400
    session["uid"] = result; session.permanent = True
    return jsonify({"success": True, "username": d["username"]})

@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    uid = session.pop("uid", None)
    if uid and uid in bots: bots[uid].stop()
    return jsonify({"success": True})

@app.route("/auth/setup", methods=["POST"])
def auth_setup():
    """One-time admin creation. Disabled once any user exists."""
    if um.db["users"]:
        return jsonify({"error": "Setup already complete"}), 403
    d = request.json or {}
    if d.get("setup_key") != os.getenv("SETUP_KEY", "alphabotsetup"):
        return jsonify({"error": "Wrong setup key"}), 403
    ok, result = um.create_admin(d.get("username","admin").strip(), d.get("password",""))
    if ok:
        return jsonify({"success": True, "message": "Admin created! Share invite codes for other users."})
    return jsonify({"error": result}), 400

# ── Bot API (requires login) ──────────────────────────────────────
@app.route("/api/status")
@app.route("/api/bot/status")
@login_required
def api_status():
    return jsonify(get_bot(session["uid"]).state())

@app.route("/api/connect", methods=["POST", "OPTIONS"])
@login_required
def api_connect():
    if request.method == "OPTIONS": return jsonify({})
    d = request.json or {}
    k = d.get("api_key",""); s = d.get("api_secret","")
    if not k or not s: return jsonify({"success": False, "message": "Key and secret required"})
    return jsonify(get_bot(session["uid"]).connect(k.strip(), s.strip()))

@app.route("/api/bot/start",   methods=["POST"])
@login_required
def api_start(): get_bot(session["uid"]).start(); return jsonify({"success": True})

@app.route("/api/bot/stop",    methods=["POST"])
@login_required
def api_stop():  get_bot(session["uid"]).stop();  return jsonify({"success": True})

@app.route("/api/bot/run_now", methods=["POST"])
@login_required
def api_run():
    threading.Thread(target=get_bot(session["uid"]).scan, daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/trades")
@login_required
def api_trades(): return jsonify(get_bot(session["uid"]).trades[-50:])

@app.route("/api/logs")
@login_required
def api_logs(): return jsonify(get_bot(session["uid"]).logs)

@app.route("/api/positions")
@login_required
def api_positions():
    b = get_bot(session["uid"])
    return jsonify({"perp": b._pos_display(), "options": b._opts_display()})

@app.route("/api/close_all", methods=["POST"])
@login_required
def api_close_all():
    b = get_bot(session["uid"]); n = 0
    for p in b.api.btcusd_pos():
        if b.api.close(float(p.get("size",0)), p.get("product_id", C.PID)).get("success"): n+=1
    for p in b.api.opt_pos():
        if b.api.close(float(p.get("size",0)), p.get("product_id")).get("success"): n+=1
    b.emit("TRADE", f"Emergency close: {n} positions")
    return jsonify({"success": True, "closed": n})

@app.route("/api/manual_trade", methods=["POST"])
@login_required
def api_manual():
    d = request.json or {}; dirn = d.get("direction","")
    if dirn not in ("long","short"): return jsonify({"success": False, "message": "long or short"})
    b = get_bot(session["uid"]); p = b.price or b.api.price(); lots = max(1, int(d.get("lots",1)))
    r = b.api.order("buy" if dirn=="long" else "sell", lots)
    if r.get("success"):
        sp = p*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp = p*(1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
        b.api.bracket("sell" if dirn=="long" else "buy", lots, sp, tp)
        b.emit("TRADE", f"MANUAL {dirn.upper()} {lots}L @ ${p:,.0f}")
        b.trades.append({"time": datetime.now(timezone.utc).isoformat(), "side": dirn,
            "entry": round(p,1), "exit": None, "lots": lots, "pnl": None, "pct": None,
            "reason": "manual", "won": None, "pid": str(C.PID), "sym": C.SYMBOL})
        b.save()
        return jsonify({"success": True, "entry": round(p,1), "stop": round(sp,1), "tp": round(tp,1)})
    return jsonify({"success": False, "message": r.get("error","failed")})

@app.route("/api/opts/toggle", methods=["POST"])
@login_required
def api_opts_toggle():
    d = request.json or {}; b = get_bot(session["uid"])
    b.opts_mode = bool(d.get("enabled", not b.opts_mode))
    b.emit("INFO", "Options ON" if b.opts_mode else "Options OFF")
    return jsonify({"success": True, "opts_mode": b.opts_mode})

@app.route("/api/opts/find", methods=["POST"])
@login_required
def api_opts_find():
    b = get_bot(session["uid"])
    if not b.opts_eng: return jsonify({"error": "Not connected"})
    d = request.json or {}
    opt = b.opts_eng.find_option(d.get("type","call"), b.price or b.api.price(), d.get("itm",False))
    return jsonify(opt)

@app.route("/api/opts/straddle", methods=["POST"])
@login_required
def api_opts_straddle():
    b = get_bot(session["uid"])
    if not b.opts_eng: return jsonify({"error": "Not connected"})
    return jsonify(b.opts_eng.straddle(b.price or b.api.price()))

@app.route("/api/ip")
def api_ip():
    try: ip = requests.get("https://api.ipify.org?format=json", timeout=5).json().get("ip","?")
    except: ip = "unknown"
    return jsonify({"ip": ip})

@app.route("/api/debug/auth")
@login_required
def api_debug():
    b = get_bot(session["uid"])
    out = {"key_len": len(b.api.key), "key_set": bool(b.api.key)}
    try:
        r = requests.get(f"{C.BASE}/v2/tickers/BTCUSD", timeout=6)
        out["ticker_ok"] = r.status_code == 200
        out["btc_price"] = r.json().get("result",{}).get("mark_price","?")
    except Exception as e: out["ticker_err"] = str(e)
    bal, _, err = b.api.balance(); out["balance"] = bal; out["err"] = err
    return jsonify(out)

# ── Admin routes ──────────────────────────────────────────────────
@app.route("/api/admin/users")
@admin_required
def admin_users():
    users = um.all()
    for uid, u in users.items():
        b = bots.get(uid)
        u["bot_running"] = b.running if b else False
        u["balance"]     = b.capital if b else 0
        u["trades"]      = b.total_tr if b else 0
    return jsonify({"users": users, "invites": um.invites(), "max_users": MAX_USERS})

@app.route("/api/admin/invite", methods=["POST"])
@admin_required
def admin_invite():
    code, msg = um.gen_invite()
    if code: return jsonify({"success": True, "code": code})
    return jsonify({"success": False, "message": msg}), 400



import base64 as _b64
_DASH = _b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEsbWF4aW11bS1zY2FsZT0xIj4KPHRpdGxlPkFscGhhIEJvdDwvdGl0bGU+CjxzdHlsZT4KKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowOy13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjp0cmFuc3BhcmVudH0KOnJvb3R7LS1nOiMwMGIzODY7LS1nYjojZThmOWYzOy0tZ2Q6I2E3ZjNkMDstLXI6I2U3NGMzYzstLXJiOiNmZWYyZjI7LS1yZDojZmNhNWE1Oy0teTojZjU5ZTBiOy0teWI6I2ZlZjNjNzstLWI6IzNiODJmNjstLWJiOiNlZmY2ZmY7LS10OiMwZjE3MmE7LS10MjojNjQ3NDhiOy0tdDM6Izk0YTNiODstLWJnOiNmMGYyZjU7LS13OiNmZmY7LS1iZHI6MXB4IHNvbGlkICNlMmU4ZjB9CmJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0tdCk7Zm9udC1mYW1pbHk6LWFwcGxlLXN5c3RlbSxCbGlua01hY1N5c3RlbUZvbnQsIlNlZ29lIFVJIixIZWx2ZXRpY2EsQXJpYWwsc2Fucy1zZXJpZjtmb250LXNpemU6MTRweDttaW4taGVpZ2h0OjEwMHZofQovKiBBVVRIICovCi5hdXRoLXdyYXB7bWluLWhlaWdodDoxMDB2aDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQouYXV0aC1jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxNnB4O3BhZGRpbmc6MjhweDt3aWR0aDoxMDAlO21heC13aWR0aDozODBweDtib3gtc2hhZG93OjAgNHB4IDI0cHggcmdiYSgwLDAsMCwuMDgpfQouYXV0aC1sb2dve2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToyMHB4fQouYXV0aC1pY297d2lkdGg6NDBweDtoZWlnaHQ6NDBweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Y29sb3I6I2ZmZjtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDB9Ci5hdXRoLXRpdGxle2ZvbnQtc2l6ZToyMnB4O2ZvbnQtd2VpZ2h0OjgwMH0uYXV0aC1zdWIye2ZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKX0KLmF1dGgtZGVzY3tmb250LXNpemU6MTNweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbToxOHB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlucHt3aWR0aDoxMDAlO2JvcmRlcjp2YXIoLS1iZHIpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTFweCAxM3B4O2ZvbnQtc2l6ZToxNHB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7b3V0bGluZTpub25lO2JhY2tncm91bmQ6I2Y4ZmFmYzttYXJnaW4tYm90dG9tOjEwcHh9Ci5pbnA6Zm9jdXN7Ym9yZGVyLWNvbG9yOnZhcigtLWcpO2JhY2tncm91bmQ6I2ZmZn0KLmF1dGgtYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmF1dGgtYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmF1dGgtbXNne3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O21hcmdpbi10b3A6MTBweDttaW4taGVpZ2h0OjIwcHg7bGluZS1oZWlnaHQ6MS43fQouYXV0aC1tc2cub2t7Y29sb3I6dmFyKC0tZyl9LmF1dGgtbXNnLmVycntjb2xvcjp2YXIoLS1yKX0KLmF1dGgtc3dpdGNoe3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjE0cHh9Ci5hdXRoLXN3aXRjaCBhe2NvbG9yOnZhcigtLWIpO2N1cnNvcjpwb2ludGVyO2ZvbnQtd2VpZ2h0OjYwMH0KLyogTUFJTiBBUFAgKi8KI2FwcHtkaXNwbGF5Om5vbmV9Ci5oZHJ7YmFja2dyb3VuZDp2YXIoLS13KTtwYWRkaW5nOjAgMTZweDtoZWlnaHQ6NTRweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTtwb3NpdGlvbjpzdGlja3k7dG9wOjA7ei1pbmRleDoxMDA7Ym94LXNoYWRvdzowIDFweCA0cHggcmdiYSgwLDAsMCwuMDYpfQoubG9nb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHh9Ci5saWN7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6OXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtjb2xvcjojZmZmO2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMH0KLmxue2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjcwMH0ubHN7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpfQouaHJpZ2h0e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KLnViYWRnZXtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3BhZGRpbmc6NHB4IDEwcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6MjBweDtib3JkZXI6dmFyKC0tYmRyKX0KLnBpbGx7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4O3BhZGRpbmc6NXB4IDEycHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnAtbGl2ZXtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0ucC1vZmZ7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcil9LnAtd2FybntiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KX0KLndyYXB7cGFkZGluZzoxMnB4IDE0cHggOTBweDttYXgtd2lkdGg6NDgwcHg7bWFyZ2luOjAgYXV0b30KLnBhZ2V7ZGlzcGxheTpub25lfS5wYWdlLnNob3d7ZGlzcGxheTpibG9ja30KLm5hdntwb3NpdGlvbjpmaXhlZDtib3R0b206MDtsZWZ0OjA7cmlnaHQ6MDtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci10b3A6dmFyKC0tYmRyKTtkaXNwbGF5OmZsZXg7cGFkZGluZzo4cHggMCBtYXgoOHB4LGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7ei1pbmRleDo5OX0KLm5ie2ZsZXg6MTtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6M3B4O3BhZGRpbmc6NHB4IDA7Ym9yZGVyOm5vbmU7YmFja2dyb3VuZDpub25lO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5uYiAuaWN7Zm9udC1zaXplOjIwcHg7Y29sb3I6dmFyKC0tdDMpfS5uYiAubGJ7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDMpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4fQoubmIub24gLmljLC5uYi5vbiAubGJ7Y29sb3I6dmFyKC0tdCl9Ci5jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTZweDttYXJnaW4tYm90dG9tOjEwcHg7Ym94LXNoYWRvdzowIDFweCAzcHggcmdiYSgwLDAsMCwuMDUpLDAgMnB4IDhweCByZ2JhKDAsMCwwLC4wNCl9Ci5jdHtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206MTJweH0KLyogQ09OTkVDVCBDQVJEICovCi5jY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNjBkZWcsIzBmMTcyYSwjMWUzYTVmKTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMnB4O21hcmdpbi1ib3R0b206MTBweH0KLmN0aXRsZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo4MDA7Y29sb3I6I2ZmZjttYXJnaW4tYm90dG9tOjZweH0KLmNzdWJ7Zm9udC1zaXplOjEycHg7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSk7bWFyZ2luLWJvdHRvbToxNnB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlwLXJvd3tiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7bWFyZ2luLWJvdHRvbToxNHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW59Ci5pcC1sYmx7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjQpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206NHB4fQouaXAtdmFse2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTZweDtmb250LXdlaWdodDo3MDA7Y29sb3I6I2ZmZjtsZXR0ZXItc3BhY2luZzoxcHh9Ci5pcC1jb3B5e2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTIpO2JvcmRlcjpub25lO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6OHB4IDE0cHg7Y29sb3I6I2ZmZjtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmNpbnB7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA4KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7Zm9udC1zaXplOjE0cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtjb2xvcjojZmZmO21hcmdpbi1ib3R0b206MTBweDtvdXRsaW5lOm5vbmV9Ci5jaW5wOmZvY3Vze2JvcmRlci1jb2xvcjp2YXIoLS1nKX0uY2lucDo6cGxhY2Vob2xkZXJ7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMyl9Ci5jYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6MTBweDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLWcpO2NvbG9yOiNmZmY7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5jYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmNtc2d7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjEycHg7bWFyZ2luLXRvcDoxMHB4O21pbi1oZWlnaHQ6MjBweDtsaW5lLWhlaWdodDoxLjd9Ci8qIEhFUk8gKi8KLmhlcm97YmFja2dyb3VuZDp2YXIoLS10KTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMHB4O21hcmdpbi1ib3R0b206MTBweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59Ci5oZXJvOjphZnRlcntjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO3RvcDotNDBweDtyaWdodDotNDBweDt3aWR0aDoxNjBweDtoZWlnaHQ6MTYwcHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5obHtmb250LXNpemU6MTBweDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC40KTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjhweDttYXJnaW4tYm90dG9tOjVweH0KLmhwe2ZvbnQtc2l6ZTo0MHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjojZmZmO2xpbmUtaGVpZ2h0OjE7bGV0dGVyLXNwYWNpbmc6LTEuNXB4fQouaHIye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDttYXJnaW4tdG9wOjlweDtmbGV4LXdyYXA6d3JhcH0KLmNoaXB7cGFkZGluZzozcHggMTBweDtib3JkZXItcmFkaXVzOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDB9Ci5jZ3tiYWNrZ3JvdW5kOnJnYmEoMCwyMDAsMTUwLC4yKTtjb2xvcjojMDBlOGIwfS5jcjJ7YmFja2dyb3VuZDpyZ2JhKDIzMSw3Niw2MCwuMik7Y29sb3I6I2ZmODA4MH0uY257YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSl9Ci5yYmFye3BhZGRpbmc6OXB4IDE0cHg7Ym9yZGVyLXJhZGl1czo4cHg7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnJiLWJ7YmFja2dyb3VuZDp2YXIoLS1nYik7Y29sb3I6IzA1OTY2OTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWdkKX0ucmItcntiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjojZGMyNjI2O2JvcmRlcjoxcHggc29saWQgdmFyKC0tcmQpfS5yYi1ue2JhY2tncm91bmQ6I2Y4ZmFmYztjb2xvcjp2YXIoLS10Mik7Ym9yZGVyOnZhcigtLWJkcil9LnJiLXd7YmFja2dyb3VuZDp2YXIoLS15Yik7Y29sb3I6IzkyNDAwZTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci8qIENPTkZJREVOQ0UgKi8KLmN3e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjE0cHg7cGFkZGluZzo0cHggMH0KLmNybmd7cG9zaXRpb246cmVsYXRpdmU7d2lkdGg6NzJweDtoZWlnaHQ6NzJweDtmbGV4LXNocmluazowfQouY3JuZyBzdmd7dHJhbnNmb3JtOnJvdGF0ZSgtOTBkZWcpO2Rpc3BsYXk6YmxvY2t9Ci5jb3Z7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQouY251bXtmb250LXNpemU6MjJweDtmb250LXdlaWdodDo4MDA7bGluZS1oZWlnaHQ6MX0uY2Rlbntmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTtmb250LXdlaWdodDo3MDB9Ci5jbXR7ZmxleDoxfS5jZGlye2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tYm90dG9tOjNweH0uY2RldHtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Mil9Ci5waWxsYXJze21hcmdpbi10b3A6MTJweH0KLnByb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O3BhZGRpbmc6N3B4IDA7Ym9yZGVyLWJvdHRvbTp2YXIoLS1iZHIpfS5wcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5wbnt3aWR0aDo4NnB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7ZmxleC1zaHJpbms6MH0KLnB0e2ZsZXg6MTtoZWlnaHQ6NXB4O2JhY2tncm91bmQ6I2YxZjVmOTtib3JkZXItcmFkaXVzOjNweDtvdmVyZmxvdzpoaWRkZW59LnBme2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6M3B4O3RyYW5zaXRpb246d2lkdGggLjVzfQoucHN7d2lkdGg6MzZweDt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQouaW5kc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLXRvcDoxMHB4fQouaW5ke2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjEwcHg7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOnZhcigtLWJkcil9Ci5pbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTozcHh9Lml2e2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMH0KLnNiYXJ7aGVpZ2h0OjNweDtiYWNrZ3JvdW5kOiNlMmU4ZjA7Ym9yZGVyLXJhZGl1czoycHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbi10b3A6OXB4fS5zZmlse2hlaWdodDoxMDAlO2JhY2tncm91bmQ6dmFyKC0tYik7Ym9yZGVyLXJhZGl1czoycHg7dHJhbnNpdGlvbjp3aWR0aCAuNXN9Ci5zcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbjtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDo0cHh9Ci8qIFBPU0lUSU9OUyAqLwoucG9ze2JvcmRlci1yYWRpdXM6MTJweDtwYWRkaW5nOjE0cHg7bWFyZ2luLWJvdHRvbToxMHB4fQoucG9zLWx7YmFja2dyb3VuZDojZjBmZGY0O2JvcmRlcjoxcHggc29saWQgdmFyKC0tZ2QpfS5wb3Mtc3tiYWNrZ3JvdW5kOiNmZmY1ZjU7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1yZCl9LnBvcy1ve2JhY2tncm91bmQ6dmFyKC0tYmIpO2JvcmRlcjoxcHggc29saWQgIzkzYzVmZH0KLnBoe2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxMHB4fS5wc3lte2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJhZGdle3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJse2JhY2tncm91bmQ6dmFyKC0tZyk7Y29sb3I6I2ZmZn0uYnNoe2JhY2tncm91bmQ6dmFyKC0tcik7Y29sb3I6I2ZmZn0uYmN7YmFja2dyb3VuZDp2YXIoLS1iKTtjb2xvcjojZmZmfS5icHtiYWNrZ3JvdW5kOiM4YjVjZjY7Y29sb3I6I2ZmZn0KLnBne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6NnB4fQoucGl7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC43NSk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo4cHh9LnBpbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi40cHg7bWFyZ2luLWJvdHRvbToycHh9LnBpdntmb250LXNpemU6MTRweDtmb250LXdlaWdodDo4MDB9LnBpZ3tjb2xvcjp2YXIoLS1nKX0ucGlye2NvbG9yOnZhcigtLXIpfQovKiBXQUxMRVQgKi8KLnd0e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQoud2x7ZmxleDoxfS53bGJ7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQzKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjVweDttYXJnaW4tYm90dG9tOjRweH0KLndhe2ZvbnQtc2l6ZTozMnB4O2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzotMXB4fS53c3tmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHh9Ci53cHtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDA7dGV4dC1hbGlnbjpyaWdodH0ud257Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246cmlnaHQ7bWFyZ2luLXRvcDoycHh9Ci8qIFNUQVRTICovCi5zZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouc3RhdHtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKX0KLnN0bHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTo0cHh9LnN0dntmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDB9Ci5iM3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouYnRue3BhZGRpbmc6MTNweCA2cHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2dhcDo1cHh9LmJ0bjphY3RpdmV7b3BhY2l0eTouOH0KLmJke2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZn0uYnIze2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1yZCl9LmJiM3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtib3JkZXI6MS41cHggc29saWQgI2JmZGJmZX0KLmJjYXtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MS41cHggc29saWQgdmFyKC0tcmQpO3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6OHB4fQovKiBPUFRJT05TICovCi50b2dyb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjEwcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7bWFyZ2luLWJvdHRvbToxMnB4fQoudGx7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6NzAwfS50czN7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4fQoudG9ne3Bvc2l0aW9uOnJlbGF0aXZlO3dpZHRoOjQ2cHg7aGVpZ2h0OjI2cHg7ZmxleC1zaHJpbms6MDtjdXJzb3I6cG9pbnRlcn0KLnRvZyBpbnB1dHtvcGFjaXR5OjA7d2lkdGg6MDtoZWlnaHQ6MDtwb3NpdGlvbjphYnNvbHV0ZX0KLnRvZ3Nse3Bvc2l0aW9uOmFic29sdXRlO2luc2V0OjA7YmFja2dyb3VuZDojZTJlOGYwO2JvcmRlci1yYWRpdXM6MTNweDt0cmFuc2l0aW9uOi4yc30KLnRvZ3NsOjpiZWZvcmV7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoyMHB4O2hlaWdodDoyMHB4O2xlZnQ6M3B4O2JvdHRvbTozcHg7YmFja2dyb3VuZDojZmZmO2JvcmRlci1yYWRpdXM6NTAlO3RyYW5zaXRpb246LjJzO2JveC1zaGFkb3c6MCAxcHggM3B4IHJnYmEoMCwwLDAsLjIpfQoudG9nIGlucHV0OmNoZWNrZWQrLnRvZ3Nse2JhY2tncm91bmQ6dmFyKC0tZyl9LnRvZyBpbnB1dDpjaGVja2VkKy50b2dzbDo6YmVmb3Jle3RyYW5zZm9ybTp0cmFuc2xhdGVYKDIwcHgpfQoub2luZm97ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjtnYXA6OHB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTBweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTttYXJnaW4tYm90dG9tOjEycHg7Zm9udC1zaXplOjExcHh9Ci5vYntkaXNwbGF5OmZsZXg7Z2FwOjhweH0KLm9iYnRue2ZsZXg6MTtwYWRkaW5nOjEwcHg7Ym9yZGVyLXJhZGl1czo4cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXJ9Ci5vYi1je2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpO2JvcmRlcjoxcHggc29saWQgI2JmZGJmZX0ub2ItcHtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXJkKX0ub2Itc3tiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci5vcmVze21hcmdpbi10b3A6MTBweDtwYWRkaW5nOjExcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2ZvbnQtc2l6ZToxMXB4O2xpbmUtaGVpZ2h0OjEuODtib3JkZXI6dmFyKC0tYmRyKTtkaXNwbGF5Om5vbmV9Ci5tcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi10b3A6OHB4fQouYnRubHtmbGV4OjE7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1nKTtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjgwMDtjdXJzb3I6cG9pbnRlcn0KLmJ0bnMye2ZsZXg6MTtwYWRkaW5nOjEzcHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXIpO2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyfQovKiBUUkFERVMgKi8KLnRyLXJvd3twYWRkaW5nOjExcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweH0udHItcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci50aWNve3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo5cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQoudGktbHtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0udGktc3tiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKX0udGktY3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKX0udGktcHtiYWNrZ3JvdW5kOiNmM2U4ZmY7Y29sb3I6IzdjM2FlZH0KLnRtaWR7ZmxleDoxO21pbi13aWR0aDowfS50c3lte2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0udG1ldGF7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4O3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KLnRyaWdodHt0ZXh0LWFsaWduOnJpZ2h0O2ZsZXgtc2hyaW5rOjB9LnRwbmx7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwfS50cGd7Y29sb3I6dmFyKC0tZyl9LnRwcntjb2xvcjp2YXIoLS1yKX0udHBue2NvbG9yOnZhcigtLXQzKX0KLyogTE9HUyAqLwoubGZ7ZGlzcGxheTpmbGV4O2dhcDo2cHg7bWFyZ2luLWJvdHRvbTo4cHh9Ci5sZmJ7cGFkZGluZzo0cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDp2YXIoLS13KTtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtZmFtaWx5OmluaGVyaXR9LmxmYi5vbntiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmY7Ym9yZGVyLWNvbG9yOnZhcigtLXQpfQoubGJveHtiYWNrZ3JvdW5kOiMwZjE3MmE7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzoxMnB4O21heC1oZWlnaHQ6NDAwcHg7b3ZlcmZsb3cteTphdXRvfQoubHJ7cGFkZGluZzo0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMWUyOTNiO2ZvbnQtc2l6ZToxMXB4O2Rpc3BsYXk6ZmxleDtnYXA6OHB4O2ZvbnQtZmFtaWx5Om1vbm9zcGFjZX0KLmx0e2NvbG9yOiM0NzU1Njk7d2hpdGUtc3BhY2U6bm93cmFwO2ZsZXgtc2hyaW5rOjB9LmxJe2NvbG9yOiM2NDc0OGJ9LmxXe2NvbG9yOnZhcigtLXkpfS5sRXtjb2xvcjp2YXIoLS1yKX0ubFR7Y29sb3I6dmFyKC0tZyk7Zm9udC13ZWlnaHQ6NzAwfQovKiBTRVRUSU5HUyAqLwouZ3JhaWwtcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjlweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKX0uZ3JhaWwtcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5ncmt7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tdDIpfS5ncnZ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWcpO3RleHQtYWxpZ246cmlnaHQ7bWF4LXdpZHRoOjYwJX0KLmRjLWJ0bnt3aWR0aDoxMDAlO3BhZGRpbmc6MTJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6NnB4fQovKiBBRE1JTiAqLwouYXV7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDttYXJnaW4tYm90dG9tOjhweDtib3JkZXI6dmFyKC0tYmRyKX0KLmF1LW5hbWV7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1ib3R0b206NHB4fQouYXUtc3RhdHN7ZGlzcGxheTpmbGV4O2dhcDoxMnB4O2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKX0KLmljb2Rle2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTVweDtmb250LXdlaWdodDo3MDA7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxMnB4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtsZXR0ZXItc3BhY2luZzoycHg7bWFyZ2luOjhweCAwfQouaXBib3h7Zm9udC1mYW1pbHk6bW9ub3NwYWNlO2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjcwMDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjEzcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2xldHRlci1zcGFjaW5nOjJweDttYXJnaW4tYm90dG9tOjEwcHh9Ci5lbXB0eXt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjI4cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtc2l6ZToxM3B4fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5PgoKPCEtLSDilZDilZDilZAgQVVUSCBTQ1JFRU4g4pWQ4pWQ4pWQIC0tPgo8ZGl2IGlkPSJhdXRoU2NyZWVuIiBjbGFzcz0iYXV0aC13cmFwIj4KICA8ZGl2IGNsYXNzPSJhdXRoLWNhcmQiPgogICAgPGRpdiBjbGFzcz0iYXV0aC1sb2dvIj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1pY28iPiYjOTE2OzwvZGl2PgogICAgICA8ZGl2PjxkaXYgY2xhc3M9ImF1dGgtdGl0bGUiPkFscGhhIEJvdDwvZGl2PjxkaXYgY2xhc3M9ImF1dGgtc3ViMiI+RGVsdGEgRXhjaGFuZ2UgSW5kaWE8L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gTG9naW4gZm9ybSAtLT4KICAgIDxkaXYgaWQ9ImxvZ2luRm9ybSI+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtZGVzYyI+U2lnbiBpbiB0byB5b3VyIHRyYWRpbmcgYWNjb3VudDwvZGl2PgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9ImxVc2VyIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0iVXNlcm5hbWUiIGF1dG9jb21wbGV0ZT0idXNlcm5hbWUiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJub25lIj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJsUGFzcyIgdHlwZT0icGFzc3dvcmQiIHBsYWNlaG9sZGVyPSJQYXNzd29yZCIgYXV0b2NvbXBsZXRlPSJjdXJyZW50LXBhc3N3b3JkIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iYXV0aC1idG4iIG9uY2xpY2s9ImRvTG9naW4oKSI+U2lnbiBJbjwvYnV0dG9uPgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLW1zZyIgaWQ9ImxNc2ciPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLXN3aXRjaCI+SGF2ZSBhbiBpbnZpdGUgY29kZT8gPGEgb25jbGljaz0ic2hvd1JlZygpIj5SZWdpc3RlciBoZXJlPC9hPjwvZGl2PgogICAgPC9kaXY+CgogICAgPCEtLSBSZWdpc3RlciBmb3JtIC0tPgogICAgPGRpdiBpZD0icmVnRm9ybSIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtZGVzYyI+RW50ZXIgeW91ciBpbnZpdGUgY29kZSB0byBjcmVhdGUgYW4gYWNjb3VudDwvZGl2PgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9InJJbnYiICB0eXBlPSJ0ZXh0IiAgICAgcGxhY2Vob2xkZXI9Ikludml0ZSBjb2RlIiBhdXRvY29ycmVjdD0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ibm9uZSI+CiAgICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0iclVzZXIiIHR5cGU9InRleHQiICAgICBwbGFjZWhvbGRlcj0iQ2hvb3NlIGEgdXNlcm5hbWUiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJub25lIj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJyUGFzcyIgdHlwZT0icGFzc3dvcmQiIHBsYWNlaG9sZGVyPSJDaG9vc2UgYSBwYXNzd29yZCAobWluIDYgY2hhcnMpIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iYXV0aC1idG4iIG9uY2xpY2s9ImRvUmVnaXN0ZXIoKSI+Q3JlYXRlIEFjY291bnQ8L2J1dHRvbj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1tc2ciIGlkPSJyTXNnIj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1zd2l0Y2giPkFscmVhZHkgcmVnaXN0ZXJlZD8gPGEgb25jbGljaz0ic2hvd0xvZ2luKCkiPlNpZ24gaW48L2E+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8IS0tIOKVkOKVkOKVkCBNQUlOIEFQUCDilZDilZDilZAgLS0+CjxkaXYgaWQ9ImFwcCI+CjxkaXYgY2xhc3M9ImhkciI+CiAgPGRpdiBjbGFzcz0ibG9nbyI+PGRpdiBjbGFzcz0ibGljIj4mIzkxNjs8L2Rpdj48ZGl2PjxkaXYgY2xhc3M9ImxuIj5BbHBoYSBCb3Q8L2Rpdj48ZGl2IGNsYXNzPSJscyI+RGVsdGEgRXhjaGFuZ2UgSW5kaWE8L2Rpdj48L2Rpdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJocmlnaHQiPgogICAgPHNwYW4gY2xhc3M9InViYWRnZSIgaWQ9InVCYWRnZSI+LS08L3NwYW4+CiAgICA8ZGl2IGNsYXNzPSJwaWxsIHAtb2ZmIiBpZD0ic1BpbGwiPiYjOTY3OTsgPHNwYW4gaWQ9InNUeHQiPlN0b3BwZWQ8L3NwYW4+PC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPGRpdiBjbGFzcz0id3JhcCI+Cgo8IS0tIEhPTUUgLS0+CjxkaXYgY2xhc3M9InBhZ2Ugc2hvdyIgaWQ9InAtaG9tZSI+CgogIDwhLS0gQ29ubmVjdCBjYXJkIC0tPgogIDxkaXYgaWQ9ImNvbm5lY3RDYXJkIiBjbGFzcz0iY2NhcmQiPgogICAgPGRpdiBjbGFzcz0iY3RpdGxlIj5Db25uZWN0IHRvIERlbHRhIEV4Y2hhbmdlPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjc3ViIj5Zb3VyIEFQSSBrZXlzIGFyZSBzdG9yZWQgb25seSBpbiB5b3VyIGJyb3dzZXIgc2Vzc2lvbiDigJQgbmV2ZXIgc2F2ZWQgb24gdGhlIHNlcnZlci48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImlwLXJvdyI+CiAgICAgIDxkaXY+PGRpdiBjbGFzcz0iaXAtbGJsIj5TZXJ2ZXIgSVAg4oCUIHdoaXRlbGlzdCBvbiBEZWx0YSBmaXJzdDwvZGl2PjxkaXYgY2xhc3M9ImlwLXZhbCIgaWQ9InNJUCI+TG9hZGluZy4uLjwvZGl2PjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJpcC1jb3B5IiBvbmNsaWNrPSJjb3B5SVAoKSI+Q29weTwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8aW5wdXQgY2xhc3M9ImNpbnAiIGlkPSJjS2V5IiB0eXBlPSJ0ZXh0IiAgICAgcGxhY2Vob2xkZXI9IkFQSSBLZXkiICAgIGF1dG9jb21wbGV0ZT0ib2ZmIiBhdXRvY29ycmVjdD0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ibm9uZSI+CiAgICA8aW5wdXQgY2xhc3M9ImNpbnAiIGlkPSJjU2VjIiB0eXBlPSJwYXNzd29yZCIgcGxhY2Vob2xkZXI9IkFQSSBTZWNyZXQiPgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgb25jbGljaz0iZG9Db25uZWN0KCkiPkNvbm5lY3Q8L2J1dHRvbj4KICAgIDxkaXYgY2xhc3M9ImNtc2ciIGlkPSJjTXNnIj48L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBMaXZlIGRhc2hib2FyZCAtLT4KICA8ZGl2IGlkPSJsaXZlRGFzaCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICA8ZGl2IGNsYXNzPSJoZXJvIj4KICAgICAgPGRpdiBjbGFzcz0iaGwiPkJpdGNvaW4gJmJ1bGw7IExpdmU8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iaHAiIGlkPSJoUCI+JC0tPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImhyMiI+CiAgICAgICAgPHNwYW4gY2xhc3M9ImNoaXAgY24iIGlkPSJoUiI+LS08L3NwYW4+CiAgICAgICAgPHNwYW4gY2xhc3M9ImNoaXAgY24iIGlkPSJoUyI+LS08L3NwYW4+CiAgICAgICAgPHNwYW4gY2xhc3M9ImNoaXAgY24iIGlkPSJoViI+LS08L3NwYW4+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyYmFyIHJiLW4iIGlkPSJyQmFyIj5TY2FubmluZy4uLjwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5Db25maWRlbmNlIFNjb3JlPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImN3Ij4KICAgICAgICA8ZGl2IGNsYXNzPSJjcm5nIj4KICAgICAgICAgIDxzdmcgdmlld0JveD0iMCAwIDcyIDcyIiB3aWR0aD0iNzIiIGhlaWdodD0iNzIiPgogICAgICAgICAgICA8Y2lyY2xlIGN4PSIzNiIgY3k9IjM2IiByPSIyOCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZjFmNWY5IiBzdHJva2Utd2lkdGg9IjciLz4KICAgICAgICAgICAgPGNpcmNsZSBpZD0iY0FyYyIgY3g9IjM2IiBjeT0iMzYiIHI9IjI4IiBmaWxsPSJub25lIiBzdHJva2U9IiMwMGIzODYiIHN0cm9rZS13aWR0aD0iNyIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtZGFzaGFycmF5PSIxNzUuOSIgc3Ryb2tlLWRhc2hvZmZzZXQ9IjE3NS45IiBzdHlsZT0idHJhbnNpdGlvbjpzdHJva2UtZGFzaG9mZnNldCAuNnMsc3Ryb2tlIC4zcyIvPgogICAgICAgICAgPC9zdmc+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJjb3YiPjxkaXYgY2xhc3M9ImNudW0iIGlkPSJjTiI+LS08L2Rpdj48ZGl2IGNsYXNzPSJjZGVuIj4vMTAwPC9kaXY+PC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iY210Ij48ZGl2IGNsYXNzPSJjZGlyIiBpZD0iY0QiPldBSVQ8L2Rpdj48ZGl2IGNsYXNzPSJjZGV0IiBpZD0iY0R0Ij5HYXRoZXJpbmcgZGF0YS4uLjwvZGl2PjwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0icGlsbGFycyIgaWQ9InBpbERpdiI+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImluZHMiPgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkFEWDwvZGl2PjxkaXYgY2xhc3M9Iml2IiBpZD0iaUEiPi0tPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iaW5kIj48ZGl2IGNsYXNzPSJpbCI+QkIgV2lkdGg8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlCIj4tLTwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkFUUiAlPC9kaXY+PGRpdiBjbGFzcz0iaXYiIGlkPSJpVCI+LS08L2Rpdj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InNiYXIiPjxkaXYgY2xhc3M9InNmaWwiIGlkPSJzRmlsIiBzdHlsZT0id2lkdGg6MCUiPjwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzcm93Ij48c3BhbiBpZD0ic1N0YXR1cyI+Tm90IHJ1bm5pbmc8L3NwYW4+PHNwYW4gaWQ9InNjZCIgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1iKSI+LS08L3NwYW4+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9InBlcnBEaXYiPjwvZGl2PgogICAgPGRpdiBpZD0ib3B0c0RpdiI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPldhbGxldDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ3dCI+CiAgICAgICAgPGRpdiBjbGFzcz0id2wiPjxkaXYgY2xhc3M9IndsYiI+QmFsYW5jZTwvZGl2PjxkaXYgY2xhc3M9IndhIiBpZD0id0EiPiQtLTwvZGl2PjxkaXYgY2xhc3M9IndzIiBpZD0id1N0Ij48L2Rpdj48L2Rpdj4KICAgICAgICA8ZGl2PjxkaXYgY2xhc3M9IndwIiBpZD0id1AiPi0tJTwvZGl2PjxkaXYgY2xhc3M9InduIiBpZD0id04iPlAmYW1wO0wgJC0tPC9kaXY+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZyI+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+V2luIFJhdGU8L2Rpdj48ZGl2IGNsYXNzPSJzdHYiIGlkPSJzV1IiPi0tPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+VHJhZGVzPC9kaXY+PGRpdiBjbGFzcz0ic3R2IiBpZD0ic1RSIj4wPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+U2NhbiAjPC9kaXY+PGRpdiBjbGFzcz0ic3R2IiBzdHlsZT0iY29sb3I6dmFyKC0tYikiIGlkPSJzU04iPjA8L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iYjMiPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG4gYmQiICBvbmNsaWNrPSJib3RTdGFydCgpIj4mIzk2NTQ7IFN0YXJ0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImJ0biBicjMiIG9uY2xpY2s9ImJvdFN0b3AoKSI+JiM5NjMyOyBTdG9wPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImJ0biBiYjMiIG9uY2xpY2s9ImJvdFJ1bigpIj4mIzk4ODk7IFJ1bjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEycHgiPk9wdGlvbnMgTW9kZTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ0b2dyb3ciPgogICAgICAgIDxkaXY+PGRpdiBjbGFzcz0idGwiPkVuYWJsZSBPcHRpb25zIFRyYWRpbmc8L2Rpdj48ZGl2IGNsYXNzPSJ0czMiPkFUTS9JVE0gY2FsbHMgJmFtcDsgcHV0cyArIHN0cmFkZGxlczwvZGl2PjwvZGl2PgogICAgICAgIDxsYWJlbCBjbGFzcz0idG9nIj48aW5wdXQgdHlwZT0iY2hlY2tib3giIGlkPSJ0b2dPIiBvbmNoYW5nZT0idG9nZ2xlT3B0cyh0aGlzLmNoZWNrZWQpIj48c3BhbiBjbGFzcz0idG9nc2wiPjwvc3Bhbj48L2xhYmVsPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBpZD0ib3B0c1BhbmVsIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgICA8ZGl2IGNsYXNzPSJvaW5mbyI+CiAgICAgICAgICA8ZGl2PjxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1nKSI+KzcwJTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+VGFrZSBQcm9maXQ8L2Rpdj48L2Rpdj4KICAgICAgICAgIDxkaXY+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLXIpIj4tMTUlPC9kaXY+PGRpdiBzdHlsZT0iY29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MnB4Ij5TdG9wIExvc3M8L2Rpdj48L2Rpdj4KICAgICAgICAgIDxkaXY+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWIpIj5Mb2NrIDY0JTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+b2YgcGVhazwvZGl2PjwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im9iIj4KICAgICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9iLWMiIG9uY2xpY2s9ImNoa09wdCgnY2FsbCcpIj5DaGVjayBDQUxMPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJvYmJ0biBvYi1wIiBvbmNsaWNrPSJjaGtPcHQoJ3B1dCcpIj5DaGVjayBQVVQ8L2J1dHRvbj4KICAgICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9iLXMiIG9uY2xpY2s9ImNoa1N0KCkiPlN0cmFkZGxlPC9idXR0b24+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBpZD0ib1JlcyIgY2xhc3M9Im9yZXMiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij5NYW51YWwgVHJhZGU8L2Rpdj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJtTG90cyIgdHlwZT0ibnVtYmVyIiBwbGFjZWhvbGRlcj0iTG90cyAoZGVmYXVsdDogMSkiIG1pbj0iMSI+CiAgICAgIDxkaXYgY2xhc3M9Im1yb3ciPgogICAgICAgIDxidXR0b24gY2xhc3M9ImJ0bmwiICBvbmNsaWNrPSJtYW5UcmFkZSgnbG9uZycpIj4mIzg1OTM7IEJ1eSBMb25nPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRuczIiIG9uY2xpY2s9Im1hblRyYWRlKCdzaG9ydCcpIj4mIzg1OTU7IFNlbGwgU2hvcnQ8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImJjYSIgb25jbGljaz0iY2xvc2VBbGwoKSI+JiM5ODg4OyBDbG9zZSBBbGwgUG9zaXRpb25zPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2PgoKPCEtLSBUUkFERVMgLS0+CjxkaXYgY2xhc3M9InBhZ2UiIGlkPSJwLXRyYWRlcyI+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTJweCI+CiAgICAgIDxzcGFuIGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbjowIj5BbGwgVHJhZGVzPC9zcGFuPgogICAgICA8c3BhbiBpZD0idENudCIgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKSI+MCB0cmFkZXM8L3NwYW4+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9InRMaXN0Ij48ZGl2IGNsYXNzPSJlbXB0eSI+Tm8gdHJhZGVzIHlldDwvZGl2PjwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjwhLS0gTE9HUyAtLT4KPGRpdiBjbGFzcz0icGFnZSIgaWQ9InAtbG9ncyI+CiAgPGRpdiBjbGFzcz0ibGYiPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIG9uIiBpZD0ibGZhIiBvbmNsaWNrPSJzZXRMRignJykiPkFsbDwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIiBpZD0ibGZ0IiBvbmNsaWNrPSJzZXRMRignVFJBREUnKSI+VHJhZGVzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIiIGlkPSJsZnciIG9uY2xpY2s9InNldExGKCdXQVJOJykiPldhcm5pbmdzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIiIGlkPSJsZmUiIG9uY2xpY2s9InNldExGKCdFUlJPUicpIj5FcnJvcnM8L2J1dHRvbj4KICA8L2Rpdj4KICA8ZGl2IGlkPSJsQ250IiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi1ib3R0b206OHB4Ij4wIGVudHJpZXM8L2Rpdj4KICA8ZGl2IGNsYXNzPSJsYm94IiBpZD0ibEJveCI+PC9kaXY+CjwvZGl2PgoKPCEtLSBTRVRUSU5HUyAtLT4KPGRpdiBjbGFzcz0icGFnZSIgaWQ9InAtc2V0dGluZ3MiPgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjhweCI+U2VydmVyIElQIOKAlCBXaGl0ZWxpc3Qgb24gRGVsdGE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImlwYm94IiBpZD0ic2lwQm94Ij4tLTwvZGl2PgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO2xpbmUtaGVpZ2h0OjEuOSI+RGVsdGEgRXhjaGFuZ2UgJnJhcnI7IEFjY291bnQgJnJhcnI7IEFQSSBLZXlzICZyYXJyOyBFZGl0ICZyYXJyOyBJUCBXaGl0ZWxpc3Q8L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo0cHgiPkFjdGl2ZSBHdWFyZHJhaWxzPC9kaXY+CiAgICA8ZGl2IGlkPSJnckxpc3QiPjwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGJ1dHRvbiBjbGFzcz0iZGMtYnRuIiBzdHlsZT0iY29sb3I6dmFyKC0tcikiIG9uY2xpY2s9ImRvRGlzY29ubmVjdCgpIj4mIzEwMDA3OyBEaXNjb25uZWN0IERlbHRhIEV4Y2hhbmdlPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJkYy1idG4iIHN0eWxlPSJjb2xvcjp2YXIoLS10MikiIG9uY2xpY2s9ImRvTG9nb3V0KCkiPiYjODU5NDsgU2lnbiBPdXQ8L2J1dHRvbj4KICA8L2Rpdj4KICA8IS0tIEFkbWluIHBhbmVsIC0tPgogIDxkaXYgaWQ9ImFkbWluUGFuZWwiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9ImJvcmRlcjoycHggc29saWQgdmFyKC0teSkiPgogICAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTJweDtjb2xvcjp2YXIoLS15KSI+JiM5ODgxOyBBZG1pbiBQYW5lbDwvZGl2PgogICAgICA8ZGl2IGlkPSJhdUxpc3QiPjwvZGl2PgogICAgICA8YnV0dG9uIG9uY2xpY2s9Imdlbkludml0ZSgpIiBzdHlsZT0id2lkdGg6MTAwJTttYXJnaW4tdG9wOjEwcHg7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1iKTtiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlciI+KyBHZW5lcmF0ZSBJbnZpdGUgQ29kZTwvYnV0dG9uPgogICAgICA8ZGl2IGlkPSJuZXdJbnZpdGUiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgICAgIDxkaXYgY2xhc3M9Imljb2RlIiBpZD0iaW52Q29kZSI+PC9kaXY+CiAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246Y2VudGVyIj5TaGFyZSB0aGlzLiBPbmUtdGltZSB1c2Ugb25seS48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8L2Rpdj48IS0tIHdyYXAgLS0+CjxuYXYgY2xhc3M9Im5hdiI+CiAgPGJ1dHRvbiBjbGFzcz0ibmIgb24iIGlkPSJuYi1ob21lIiAgICAgb25jbGljaz0iZ29QYWdlKCdob21lJykiPjxzcGFuIGNsYXNzPSJpYyI+JiMxMjc5Njg7PC9zcGFuPjxzcGFuIGNsYXNzPSJsYiI+SG9tZTwvc3Bhbj48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJuYiIgICAgaWQ9Im5iLXRyYWRlcyIgICBvbmNsaWNrPSJnb1BhZ2UoJ3RyYWRlcycpIj48c3BhbiBjbGFzcz0iaWMiPiYjMTI4MjAzOzwvc3Bhbj48c3BhbiBjbGFzcz0ibGIiPlRyYWRlczwvc3Bhbj48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJuYiIgICAgaWQ9Im5iLWxvZ3MiICAgICBvbmNsaWNrPSJnb1BhZ2UoJ2xvZ3MnKSI+PHNwYW4gY2xhc3M9ImljIj4mIzEyODIyMDs8L3NwYW4+PHNwYW4gY2xhc3M9ImxiIj5Mb2dzPC9zcGFuPjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9Im5iIiAgICBpZD0ibmItc2V0dGluZ3MiIG9uY2xpY2s9ImdvUGFnZSgnc2V0dGluZ3MnKSI+PHNwYW4gY2xhc3M9ImljIj4mIzk4ODE7PC9zcGFuPjxzcGFuIGNsYXNzPSJsYiI+U2V0dGluZ3M8L3NwYW4+PC9idXR0b24+CjwvbmF2Pgo8L2Rpdj48IS0tIGFwcCAtLT4KCjxzY3JpcHQ+CnZhciBTVD17bG9nczpbXSxsZjoiIix0cmFkZXM6W10sbmV4dEF0Om51bGwsc3M6MzAwLGlzQWRtaW46ZmFsc2V9Owp2YXIgUEM9eyJSZWdpbWUiOiIjM2I4MmY2IiwiTVRGIEFsaWduIjoiIzAwYjM4NiIsIlJTSSI6IiNmNTllMGIiLCJNQUNEIjoiIzhiNWNmNiIsIlZvbGF0aWxpdHkiOiIjZWM0ODk5IiwiVm9sdW1lIjoiI2U3NGMzYyIsIlNlc3Npb24iOiIjMTRiOGE2In07CgpmdW5jdGlvbiBnZShpZCl7cmV0dXJuIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKTt9CmZ1bmN0aW9uIHN0KGlkLHYpe3ZhciBlPWdlKGlkKTtpZihlKWUudGV4dENvbnRlbnQ9djt9CmZ1bmN0aW9uIHNoKGlkLHYpe3ZhciBlPWdlKGlkKTtpZihlKWUuaW5uZXJIVE1MPXY7fQoKZnVuY3Rpb24geGhyKHVybCxib2R5LGNiKXsKICB2YXIgcmVxPW5ldyBYTUxIdHRwUmVxdWVzdCgpLGlzUD1ib2R5IT09dW5kZWZpbmVkJiZib2R5IT09bnVsbDsKICByZXEub3Blbihpc1A/IlBPU1QiOiJHRVQiLHVybCx0cnVlKTtyZXEud2l0aENyZWRlbnRpYWxzPXRydWU7CiAgaWYoaXNQKXJlcS5zZXRSZXF1ZXN0SGVhZGVyKCJDb250ZW50LVR5cGUiLCJhcHBsaWNhdGlvbi9qc29uIik7CiAgcmVxLm9ucmVhZHlzdGF0ZWNoYW5nZT1mdW5jdGlvbigpewogICAgaWYocmVxLnJlYWR5U3RhdGUhPT00KXJldHVybjsKICAgIGlmKCFjYilyZXR1cm47CiAgICBpZihyZXEuc3RhdHVzPT09MjAwKXt0cnl7Y2IoSlNPTi5wYXJzZShyZXEucmVzcG9uc2VUZXh0KSk7fWNhdGNoKGUpe2NiKG51bGwpO319CiAgICBlbHNlIGlmKHJlcS5zdGF0dXM9PT00MDEpe3Nob3dBdXRoKCk7fQogICAgZWxzZXtjYihudWxsKTt9CiAgfTsKICByZXEub25lcnJvcj1mdW5jdGlvbigpe2lmKGNiKWNiKG51bGwpO307CiAgcmVxLnNlbmQoaXNQP0pTT04uc3RyaW5naWZ5KGJvZHkpOm51bGwpOwp9CgpmdW5jdGlvbiBzaG93QXV0aCgpe2dlKCJhdXRoU2NyZWVuIikuc3R5bGUuZGlzcGxheT0iZmxleCI7Z2UoImFwcCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO30KZnVuY3Rpb24gc2hvd0FwcCgpe2dlKCJhdXRoU2NyZWVuIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7Z2UoImFwcCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjt9CmZ1bmN0aW9uIHNob3dMb2dpbigpe2dlKCJsb2dpbkZvcm0iKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7Z2UoInJlZ0Zvcm0iKS5zdHlsZS5kaXNwbGF5PSJub25lIjt9CmZ1bmN0aW9uIHNob3dSZWcoKXtnZSgibG9naW5Gb3JtIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7Z2UoInJlZ0Zvcm0iKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7fQoKZnVuY3Rpb24gZ29QYWdlKG4pewogIFsiaG9tZSIsInRyYWRlcyIsImxvZ3MiLCJzZXR0aW5ncyJdLmZvckVhY2goZnVuY3Rpb24odCl7CiAgICBnZSgicC0iK3QpLmNsYXNzTGlzdC50b2dnbGUoInNob3ciLHQ9PT1uKTsKICAgIGdlKCJuYi0iK3QpLmNsYXNzTGlzdC50b2dnbGUoIm9uIix0PT09bik7CiAgfSk7CiAgaWYobj09PSJ0cmFkZXMiKXJlbmRlclRyYWRlcygpOwogIGlmKG49PT0ibG9ncyIpcmVuZGVyTG9ncygpOwogIGlmKG49PT0ic2V0dGluZ3MiKWxvYWRBZG1pbigpOwp9CgpmdW5jdGlvbiBkb0xvZ2luKCl7CiAgdmFyIHU9Z2UoImxVc2VyIikudmFsdWUudHJpbSgpLHA9Z2UoImxQYXNzIikudmFsdWU7CiAgaWYoIXV8fCFwKXtzaG93TXNnKCJsTXNnIiwiRW50ZXIgdXNlcm5hbWUgYW5kIHBhc3N3b3JkIiwiZXJyIik7cmV0dXJuO30KICBzaG93TXNnKCJsTXNnIiwiU2lnbmluZyBpbi4uLiIsIiIpOwogIHhocigiL2F1dGgvbG9naW4iLHt1c2VybmFtZTp1LHBhc3N3b3JkOnB9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgU1QuaXNBZG1pbj1yLmlzX2FkbWluO3N0KCJ1QmFkZ2UiLHIudXNlcm5hbWUpO3Nob3dBcHAoKTtsb2FkSVAoKTtwb2xsKCk7CiAgICB9ZWxzZXtzaG93TXNnKCJsTXNnIixyP3IubWVzc2FnZToiTG9naW4gZmFpbGVkIiwiZXJyIik7fQogIH0pOwp9CmZ1bmN0aW9uIGRvUmVnaXN0ZXIoKXsKICB2YXIgaT1nZSgickludiIpLnZhbHVlLnRyaW0oKSx1PWdlKCJyVXNlciIpLnZhbHVlLnRyaW0oKSxwPWdlKCJyUGFzcyIpLnZhbHVlOwogIGlmKCFpfHwhdXx8IXApe3Nob3dNc2coInJNc2ciLCJBbGwgZmllbGRzIHJlcXVpcmVkIiwiZXJyIik7cmV0dXJuO30KICBzaG93TXNnKCJyTXNnIiwiQ3JlYXRpbmcgYWNjb3VudC4uLiIsIiIpOwogIHhocigiL2F1dGgvcmVnaXN0ZXIiLHtpbnZpdGU6aSx1c2VybmFtZTp1LHBhc3N3b3JkOnB9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgU1QuaXNBZG1pbj1mYWxzZTtzdCgidUJhZGdlIix1KTtzaG93QXBwKCk7bG9hZElQKCk7cG9sbCgpOwogICAgfWVsc2V7c2hvd01zZygick1zZyIscj9yLm1lc3NhZ2U6IlJlZ2lzdHJhdGlvbiBmYWlsZWQiLCJlcnIiKTt9CiAgfSk7Cn0KZnVuY3Rpb24gc2hvd01zZyhpZCxtc2csY2xzKXt2YXIgZT1nZShpZCk7ZS50ZXh0Q29udGVudD1tc2c7ZS5jbGFzc05hbWU9ImF1dGgtbXNnIisoY2xzPyIgIitjbHM6IiIpO30KZnVuY3Rpb24gZG9Mb2dvdXQoKXsKICBpZighY29uZmlybSgiU2lnbiBvdXQ/IikpcmV0dXJuOwogIHhocigiL2F1dGgvbG9nb3V0Iix7fSxmdW5jdGlvbigpe3Nob3dBdXRoKCk7Z2UoImxVc2VyIikudmFsdWU9IiI7Z2UoImxQYXNzIikudmFsdWU9IiI7fSk7Cn0KZnVuY3Rpb24gZG9EaXNjb25uZWN0KCl7CiAgaWYoIWNvbmZpcm0oIkRpc2Nvbm5lY3QgRGVsdGEgRXhjaGFuZ2U/IikpcmV0dXJuOwogIGdlKCJjb25uZWN0Q2FyZCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtnZSgibGl2ZURhc2giKS5zdHlsZS5kaXNwbGF5PSJub25lIjsKfQpmdW5jdGlvbiBjb3B5SVAoKXsKICB2YXIgaXA9Z2UoInNJUCIpLnRleHRDb250ZW50OwogIHRyeXtuYXZpZ2F0b3IuY2xpcGJvYXJkLndyaXRlVGV4dChpcCk7fWNhdGNoKGUpe30KICB2YXIgYj1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCIuaXAtY29weSIpO2IudGV4dENvbnRlbnQ9IkNvcGllZCEiOwogIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtiLnRleHRDb250ZW50PSJDb3B5Ijt9LDIwMDApOwp9CmZ1bmN0aW9uIGRvQ29ubmVjdCgpewogIHZhciBrPWdlKCJjS2V5IikudmFsdWUudHJpbSgpLHM9Z2UoImNTZWMiKS52YWx1ZS50cmltKCk7CiAgaWYoIWt8fCFzKXtnZSgiY01zZyIpLmlubmVySFRNTD0iPHNwYW4gc3R5bGU9J2NvbG9yOiNmODcxNzEnPkVudGVyIEFQSSBrZXkgYW5kIHNlY3JldDwvc3Bhbj4iO3JldHVybjt9CiAgZ2UoImNNc2ciKS50ZXh0Q29udGVudD0iQ29ubmVjdGluZy4uLiI7CiAgeGhyKCIvYXBpL2Nvbm5lY3QiLHthcGlfa2V5OmssYXBpX3NlY3JldDpzfSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuc3VjY2Vzcyl7CiAgICAgIGdlKCJjTXNnIikuaW5uZXJIVE1MPSI8c3BhbiBzdHlsZT0nY29sb3I6IzRhZGU4MCc+Q29ubmVjdGVkISAkIityLmJhbGFuY2UudG9GaXhlZCgyKSsiPC9zcGFuPiI7CiAgICAgIGdlKCJjb25uZWN0Q2FyZCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO2dlKCJsaXZlRGFzaCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsKICAgIH1lbHNlewogICAgICB2YXIgaXA9ciYmci5zZXJ2ZXJfaXA/IiB8IElQOiAiK3Iuc2VydmVyX2lwOiIiOwogICAgICBnZSgiY01zZyIpLmlubmVySFRNTD0iPHNwYW4gc3R5bGU9J2NvbG9yOiNmODcxNzEnPiIrKHI/ci5tZXNzYWdlOiJGYWlsZWQiKStpcCsiPC9zcGFuPiI7CiAgICB9CiAgfSk7Cn0KZnVuY3Rpb24gYm90U3RhcnQoKXt4aHIoIi9hcGkvYm90L3N0YXJ0Iix7fSxudWxsKTt9CmZ1bmN0aW9uIGJvdFN0b3AoKXt4aHIoIi9hcGkvYm90L3N0b3AiLHt9LG51bGwpO30KZnVuY3Rpb24gYm90UnVuKCl7c3QoInNTdGF0dXMiLCJTY2FubmluZy4uLiIpO3hocigiL2FwaS9ib3QvcnVuX25vdyIse30sbnVsbCk7fQpmdW5jdGlvbiBjbG9zZUFsbCgpewogIGlmKCFjb25maXJtKCJDbG9zZSBBTEwgb3BlbiBwb3NpdGlvbnM/IikpcmV0dXJuOwogIHhocigiL2FwaS9jbG9zZV9hbGwiLHt9LGZ1bmN0aW9uKHIpe2FsZXJ0KCJDbG9zZWQ6ICIrKChyJiZyLmNsb3NlZCl8fDApKyIgcG9zaXRpb25zIik7fSk7Cn0KZnVuY3Rpb24gbWFuVHJhZGUoZGlyKXsKICB2YXIgbG90cz1wYXJzZUludChnZSgibUxvdHMiKS52YWx1ZSl8fDE7CiAgeGhyKCIvYXBpL21hbnVhbF90cmFkZSIse2RpcmVjdGlvbjpkaXIsbG90czpsb3RzfSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuc3VjY2VzcylhbGVydChkaXIudG9VcHBlckNhc2UoKSsiICIrbG90cysiTFxuRW50cnkgJCIrci5lbnRyeSsiXG5TdG9wICQiK3Iuc3RvcCsiXG5UUCAkIityLnRwKTsKICAgIGVsc2UgYWxlcnQoIkZhaWxlZDogIisoKHImJnIubWVzc2FnZSl8fCJDaGVjayBMb2dzIikpOwogIH0pOwp9CmZ1bmN0aW9uIHRvZ2dsZU9wdHMob24pewogIHhocigiL2FwaS9vcHRzL3RvZ2dsZSIse2VuYWJsZWQ6b259LGZ1bmN0aW9uKHIpewogICAgZ2UoIm9wdHNQYW5lbCIpLnN0eWxlLmRpc3BsYXk9KHImJnIub3B0c19tb2RlKT8iYmxvY2siOiJub25lIjsKICB9KTsKfQpmdW5jdGlvbiBjaGtPcHQodCl7CiAgdmFyIGVsPWdlKCJvUmVzIik7ZWwuc3R5bGUuZGlzcGxheT0iYmxvY2siO2VsLnRleHRDb250ZW50PSJDaGVja2luZy4uLiI7CiAgeGhyKCIvYXBpL29wdHMvZmluZCIse3R5cGU6dCxpdG06ZmFsc2V9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5mb3VuZCllbC5pbm5lckhUTUw9IjxiPiIrci5zeW1ib2wrIjwvYj48YnI+U3RyaWtlICQiKyhyLnN0cmlrZXx8MCkudG9Mb2NhbGVTdHJpbmcoKSsiIHwgTWFyayAkIisoci5tYXJrfHwwKS50b0ZpeGVkKDIpKyIgfCBQcmVtaXVtICQiKyhyLnByZW1pdW1fdXNkfHwwKS50b0ZpeGVkKDIpKyhyLml2PyIgfCBJViAiK3IuaXYrIiUiOiIiKSsiPGJyPiIrci5tb25leW5lc3MrIiB8IEV4cGlyeSAiK3IuZXhwaXJ5OwogICAgZWxzZSBlbC50ZXh0Q29udGVudD0iTm8gIit0KyIgZm91bmQuIEV4cGlyeTogIisoKHImJnIuZXhwaXJ5KXx8Ij8iKTsKICB9KTsKfQpmdW5jdGlvbiBjaGtTdCgpewogIHZhciBlbD1nZSgib1JlcyIpO2VsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtlbC50ZXh0Q29udGVudD0iQ2hlY2tpbmcuLi4iOwogIHhocigiL2FwaS9vcHRzL3N0cmFkZGxlIix7fSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuZm91bmQpZWwuaW5uZXJIVE1MPSI8Yj5TdHJhZGRsZTwvYj48YnI+VG90YWw6ICQiKyhyLnRvdGFsX3ByZW1pdW1fdXNkfHwwKS50b0ZpeGVkKDIpKyI8YnI+QkUgdXA6ICQiK01hdGgucm91bmQoci5icmVha2V2ZW5fdXB8fDApLnRvTG9jYWxlU3RyaW5nKCkrIiB8IGRvd246ICQiK01hdGgucm91bmQoci5icmVha2V2ZW5fZG93bnx8MCkudG9Mb2NhbGVTdHJpbmcoKTsKICAgIGVsc2UgZWwudGV4dENvbnRlbnQ9IkNhbm5vdCBidWlsZCBzdHJhZGRsZSByaWdodCBub3cuIjsKICB9KTsKfQpmdW5jdGlvbiBzZXRMRihmKXsKICBTVC5sZj1mOwogIHZhciBtPXsiIjoibGZhIiwiVFJBREUiOiJsZnQiLCJXQVJOIjoibGZ3IiwiRVJST1IiOiJsZmUifTsKICBPYmplY3Qua2V5cyhtKS5mb3JFYWNoKGZ1bmN0aW9uKGspe3ZhciBlbD1nZShtW2tdKTtpZihlbCllbC5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsaz09PWYpO30pOwogIHJlbmRlckxvZ3MoKTsKfQpmdW5jdGlvbiByZW5kZXIocyl7CiAgaWYoIXMpcmV0dXJuOwogIGlmKHMuY29ubmVjdGVkKXtnZSgiY29ubmVjdENhcmQiKS5zdHlsZS5kaXNwbGF5PSJub25lIjtnZSgibGl2ZURhc2giKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7fQogIHZhciBydW49cy5jb25uZWN0ZWQmJnMucnVubmluZyYmIXMuaGFsdGVkOwogIGdlKCJzUGlsbCIpLmNsYXNzTmFtZT0icGlsbCAiKyhzLmhhbHRlZD8icC13YXJuIjpydW4/InAtbGl2ZSI6InAtb2ZmIik7CiAgc3QoInNUeHQiLHMuaGFsdGVkPyJIQUxURUQiOnJ1bj8iTGl2ZSI6IlN0b3BwZWQiKTsKICBzdCgiaFAiLHMucHJpY2U/IiQiK3MucHJpY2UudG9Mb2NhbGVTdHJpbmcoKToiJC0tIik7CiAgdmFyIHJnPXMucmVnaW1lfHwiIjsKICB2YXIgcmM9Z2UoImhSIik7cmMudGV4dENvbnRlbnQ9cmd8fCItLSI7cmMuY2xhc3NOYW1lPSJjaGlwICIrKHJnLmluZGV4T2YoIkJVTEwiKT49MD8iY2ciOnJnLmluZGV4T2YoIkJFQVIiKT49MD8iY3IyIjoiY24iKTsKICBzdCgiaFMiLHMuc3RyYXRlZ3l8fCItLSIpO3N0KCJoViIscy52b2xfcmVnaW1lfHwiLS0iKTsKICB2YXIgcmI9Z2UoInJCYXIiKTtyYi5jbGFzc05hbWU9InJiYXIgIisocmcuaW5kZXhPZigiQlVMTCIpPj0wPyJyYi1iIjpyZy5pbmRleE9mKCJCRUFSIik+PTA/InJiLXIiOnJnPT09IlNJREVXQVlTIj8icmItdyI6InJiLW4iKTsKICByYi50ZXh0Q29udGVudD1yZysiIFx1MjAxNCAiKyhzLnN0cmF0ZWd5fHwiQ2FsY3VsYXRpbmciKTsKICB2YXIgc2M9cy5jb25mX2xvbmd8fDA7c3QoImNOIixzY3x8Ii0tIik7CiAgdmFyIGFyYz1nZSgiY0FyYyIpO2FyYy5zdHlsZS5zdHJva2VEYXNob2Zmc2V0PTE3NS45LShzYy8xMDAqMTc1LjkpO2FyYy5zdHlsZS5zdHJva2U9c2M+PTcwPyIjMDBiMzg2IjpzYz49NTA/IiNmNTllMGIiOiIjZTc0YzNjIjsKICBnZSgiY04iKS5zdHlsZS5jb2xvcj1zYz49NzA/InZhcigtLWcpIjpzYz49NTA/InZhcigtLXkpIjoidmFyKC0tcikiOwogIHN0KCJjRCIscy5zdHJhdGVneT09PSJXQUlUIj8iV0FJVCI6cmd8fCJXQUlUIik7c3QoImNEdCIsIlNjb3JlICIrc2MrIi8xMDAgfCBBRFg9Iisocy5hZHh8fDApKyIgfCAiKyhzLnZvbF9yZWdpbWV8fCIiKSk7CiAgdmFyIHBscz1zLnBpbGxhcnN8fHt9O3ZhciBwaD0iIjsKICBPYmplY3Qua2V5cyhwbHMpLmZvckVhY2goZnVuY3Rpb24oayl7dmFyIHY9cGxzW2tdO3ZhciBwY3Q9di5tPjA/TWF0aC5yb3VuZCh2LnMvdi5tKjEwMCk6MDt2YXIgY29sPVBDW2tdfHwidmFyKC0tZykiO3BoKz0iPGRpdiBjbGFzcz0ncHJvdyc+PGRpdiBjbGFzcz0ncG4nPiIraysiPC9kaXY+PGRpdiBjbGFzcz0ncHQnPjxkaXYgY2xhc3M9J3BmJyBzdHlsZT0nd2lkdGg6IitwY3QrIiU7YmFja2dyb3VuZDoiK2NvbCsiJz48L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwcycgc3R5bGU9J2NvbG9yOiIrY29sKyInPiIrdi5zKyIvIit2Lm0rIjwvZGl2PjwvZGl2PiI7fSk7CiAgc2goInBpbERpdiIscGgpO3N0KCJpQSIscy5hZHh8fCItLSIpO3N0KCJpQiIscy5idz9zLmJ3KyIlIjoiLS0iKTtzdCgiaVQiLHMuYXRyX3BjdD9zLmF0cl9wY3QrIiUiOiItLSIpOwogIHN0KCJzU3RhdHVzIixzLnN0YXR1c3x8Ii0tIik7c3QoInNTTiIscy5zY2FuX258fDApOwogIGlmKHMubmV4dF9zY2FuKVNULm5leHRBdD1uZXcgRGF0ZShzLm5leHRfc2Nhbik7CiAgdmFyIHBwPXMub3Blbl9wb3N8fFtdO3ZhciBwaDI9IiI7CiAgcHAuZm9yRWFjaChmdW5jdGlvbihwKXt2YXIgbmVnPXAudXBubDwwO3BoMis9IjxkaXYgY2xhc3M9J3BvcyBwb3MtIisobmVnPyJzIjoibCIpKyInPjxkaXYgY2xhc3M9J3BoJz48c3BhbiBjbGFzcz0ncHN5bSc+IitwLnN5bSsiPC9zcGFuPjxzcGFuIGNsYXNzPSdiYWRnZSBiIisocC5zaWRlPT09ImxvbmciPyJsIjoic2giKSsiJz4iK3Auc2lkZS50b1VwcGVyQ2FzZSgpKyI8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ncGcnPjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPkVudHJ5PC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIitwLmVudHJ5LnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPkxvdHM8L2Rpdj48ZGl2IGNsYXNzPSdwaXYnPiIrcC5sb3RzKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5VUEw8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgIisobmVnPyJwaXIiOiJwaWciKSsiJz4iKyhwLnVwbmw+PTA/IisiOiIiKStwLnVwbmwrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPk1hcms8L2Rpdj48ZGl2IGNsYXNzPSdwaXYnPiQiKyhwLm1hcmt8fHAuZW50cnkpLnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlN0b3A8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgcGlyJz4kIitwLnN0b3AudG9Mb2NhbGVTdHJpbmcoKSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+VFA8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgcGlnJz4kIitwLnRwLnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PjwvZGl2PjwvZGl2PiI7fSk7CiAgc2goInBlcnBEaXYiLHBoMik7CiAgdmFyIG9wPXMub3B0c19wb3N8fFtdO3ZhciBvaD0iIjsKICBvcC5mb3JFYWNoKGZ1bmN0aW9uKG8pe3ZhciBpc0M9by50eXBlPT09IkNBTEwiO29oKz0iPGRpdiBjbGFzcz0ncG9zIHBvcy1vJz48ZGl2IGNsYXNzPSdwaCc+PHNwYW4gY2xhc3M9J3BzeW0nIHN0eWxlPSdmb250LXNpemU6MTJweCc+IitvLnN5bSsiPC9zcGFuPjxzcGFuIGNsYXNzPSdiYWRnZSBiIisoaXNDPyJjIjoicCIpKyInPiIrby50eXBlKyI8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ncGcnPjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPkVudHJ5PC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIitvLmVudHJ5KyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5NYXJrPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIitvLm1hcmsrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlAmTDwvZGl2PjxkaXYgY2xhc3M9J3BpdiAiKyhvLnBjdDwwPyJwaXIiOiJwaWciKSsiJz4iKyhvLnBjdD49MD8iKyI6IiIpK28ucGN0KyIlPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+UGVhazwvZGl2PjxkaXYgY2xhc3M9J3BpdiBwaWcnPiQiK28ucGVhaysiPC9kaXY+PC9kaXY+PC9kaXY+PC9kaXY+Ijt9KTsKICBzaCgib3B0c0RpdiIsb2gpOwogIHZhciBjYXA9cy5jYXBpdGFsfHwwLHNjMj1zLnN0YXJ0X2NhcHx8MCxwcDI9cy5wbmxfcGN0fHwwOwogIHN0KCJ3QSIsY2FwPyIkIitjYXAudG9GaXhlZCgyKToiJC0tIik7c3QoIndTdCIsc2MyPyJTdGFydGVkICQiK3NjMi50b0ZpeGVkKDIpOiIiKTsKICB2YXIgd3BFbD1nZSgid1AiKTt3cEVsLnRleHRDb250ZW50PShwcDI+PTA/IisiOiIiKStwcDIudG9GaXhlZCgyKSsiJSI7d3BFbC5zdHlsZS5jb2xvcj1wcDI+PTA/InZhcigtLWcpIjoidmFyKC0tcikiOwogIHN0KCJ3TiIsIlAmTCAkIisocHAyPj0wPyIrIjoiIikrKGNhcC1zYzIpLnRvRml4ZWQoMikpOwogIHN0KCJzV1IiLHMud2luX3JhdGUhPW51bGw/cy53aW5fcmF0ZSsiJSI6Ii0tIik7c3QoInNUUiIscy50b3RhbF90cmFkZXN8fDApOwogIHZhciBvdD1nZSgidG9nTyIpO2lmKG90KW90LmNoZWNrZWQ9ISFzLm9wdHNfbW9kZTsKICBnZSgib3B0c1BhbmVsIikuc3R5bGUuZGlzcGxheT1zLm9wdHNfbW9kZT8iYmxvY2siOiJub25lIjsKICBpZihzLmd1YXJkcmFpbHMpe3ZhciBnaz1PYmplY3Qua2V5cyhzLmd1YXJkcmFpbHMpO3ZhciBnaD0iIjtnay5mb3JFYWNoKGZ1bmN0aW9uKGspe2doKz0iPGRpdiBjbGFzcz0nZ3JhaWwtcm93Jz48c3BhbiBjbGFzcz0nZ3JrJz4iK2srIjwvc3Bhbj48c3BhbiBjbGFzcz0nZ3J2Jz4iK3MuZ3VhcmRyYWlsc1trXSsiPC9zcGFuPjwvZGl2PiI7fSk7c2goImdyTGlzdCIsZ2gpO30KICBpZihzLmxvZ3MpU1QubG9ncz1zLmxvZ3M7aWYocy50cmFkZXMpU1QudHJhZGVzPXMudHJhZGVzOwogIHN0KCJsQ250IixTVC5sb2dzLmxlbmd0aCsiIGVudHJpZXMiKTsKICBpZihnZSgicC1sb2dzIikuY2xhc3NMaXN0LmNvbnRhaW5zKCJzaG93IikpcmVuZGVyTG9ncygpOwogIGlmKGdlKCJwLXRyYWRlcyIpLmNsYXNzTGlzdC5jb250YWlucygic2hvdyIpKXJlbmRlclRyYWRlcygpOwp9CmZ1bmN0aW9uIHJlbmRlclRyYWRlcygpewogIHN0KCJ0Q250IixTVC50cmFkZXMubGVuZ3RoKyIgdHJhZGVzIik7CiAgaWYoIVNULnRyYWRlcy5sZW5ndGgpe3NoKCJ0TGlzdCIsIjxkaXYgY2xhc3M9J2VtcHR5Jz5ObyB0cmFkZXMgeWV0PC9kaXY+Iik7cmV0dXJuO30KICB2YXIgaD0iIjsKICBTVC50cmFkZXMuZm9yRWFjaChmdW5jdGlvbih0KXsKICAgIHZhciBvcGVuPXQuZXhpdD09bnVsbCxzZD10LnNpZGV8fCIiOwogICAgdmFyIGljPXNkPT09ImxvbmciPyJ0aS1sIjpzZD09PSJzaG9ydCI/InRpLXMiOnNkPT09ImNhbGwiPyJ0aS1jIjoidGktcCI7CiAgICB2YXIgaWNvPXNkPT09ImxvbmciPyImIzg1OTM7IjpzZD09PSJzaG9ydCI/IiYjODU5NTsiOnNkPT09ImNhbGwiPyJDIjoiUCI7CiAgICB2YXIgcGM9b3Blbj8idHBuIjoodC53b24/InRwZyI6InRwciIpLHB2PW9wZW4/Ik9wZW5cdTIwMjYiOih0Lndvbj8iKyI6IiIpKyh0LnBubHx8MCkudG9GaXhlZCg0KTsKICAgIHZhciB0bT10LnRpbWU/dC50aW1lLnN1YnN0cig1LDExKS5yZXBsYWNlKCJUIiwiICIpOiIiOwogICAgaCs9IjxkaXYgY2xhc3M9J3RyLXJvdyc+PGRpdiBjbGFzcz0ndGljbyAiK2ljKyInPiIraWNvKyI8L2Rpdj48ZGl2IGNsYXNzPSd0bWlkJz48ZGl2IGNsYXNzPSd0c3ltJz4iKyh0LnN5bXx8IkJUQ1VTRCIpKyI8L2Rpdj48ZGl2IGNsYXNzPSd0bWV0YSc+Iit0bSsiICZtaWRkb3Q7ICIrKHQucmVhc29ufHwiIikrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3RyaWdodCc+PGRpdiBjbGFzcz0ndHBubCAiK3BjKyInPiQiK3B2KyI8L2Rpdj48ZGl2IHN0eWxlPSdmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10MyknPiIrKHQuZW50cnk/IkAkIit0LmVudHJ5OiIiKSsiPC9kaXY+PC9kaXY+PC9kaXY+IjsKICB9KTtzaCgidExpc3QiLGgpOwp9CmZ1bmN0aW9uIHJlbmRlckxvZ3MoKXsKICB2YXIgZj1TVC5sZj9TVC5sb2dzLmZpbHRlcihmdW5jdGlvbihlKXtyZXR1cm4gZS5sPT09U1QubGY7fSk6U1QubG9nczsKICB2YXIgaD0iIjtmLnNsaWNlKDAsMTUwKS5mb3JFYWNoKGZ1bmN0aW9uKGUpe3ZhciBjbHM9ImxJIjtpZihlLmw9PT0iV0FSTiIpY2xzPSJsVyI7ZWxzZSBpZihlLmw9PT0iRVJST1IiKWNscz0ibEUiO2Vsc2UgaWYoZS5sPT09IlRSQURFIiljbHM9ImxUIjtoKz0iPGRpdiBjbGFzcz0nbHInPjxzcGFuIGNsYXNzPSdsdCc+IitlLnQrIjwvc3Bhbj48c3BhbiBjbGFzcz0nIitjbHMrIic+IitlLm0rIjwvc3Bhbj48L2Rpdj4iO30pO3NoKCJsQm94IixoKTsKfQpmdW5jdGlvbiBsb2FkQWRtaW4oKXsKICBpZighU1QuaXNBZG1pbil7Z2UoImFkbWluUGFuZWwiKS5zdHlsZS5kaXNwbGF5PSJub25lIjtyZXR1cm47fQogIGdlKCJhZG1pblBhbmVsIikuc3R5bGUuZGlzcGxheT0iYmxvY2siOwogIHhocigiL2FwaS9hZG1pbi91c2VycyIsbnVsbCxmdW5jdGlvbihyKXsKICAgIGlmKCFyKXJldHVybjsKICAgIHZhciBoPSIiOwogICAgT2JqZWN0LmtleXMoci51c2Vyc3x8e30pLmZvckVhY2goZnVuY3Rpb24odWlkKXsKICAgICAgdmFyIHU9ci51c2Vyc1t1aWRdOwogICAgICBoKz0iPGRpdiBjbGFzcz0nYXUnPjxkaXYgY2xhc3M9J2F1LW5hbWUnPiIrKHUuaXNfYWRtaW4/IiYjOTczMzsgIjoiIikrdS51c2VybmFtZSsodS5ib3RfcnVubmluZz8iIDxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS1nKTtmb250LXNpemU6MTBweCc+JiM5Njc5OyBMaXZlPC9zcGFuPiI6IiA8c3BhbiBzdHlsZT0nY29sb3I6dmFyKC0tdDMpO2ZvbnQtc2l6ZToxMHB4Jz5PZmZsaW5lPC9zcGFuPiIpKyI8L2Rpdj48ZGl2IGNsYXNzPSdhdS1zdGF0cyc+PHNwYW4+JCIrdS5iYWxhbmNlLnRvRml4ZWQoMikrIjwvc3Bhbj48c3Bhbj4iK3UudHJhZGVzKyIgdHJhZGVzPC9zcGFuPjwvZGl2PjwvZGl2PiI7CiAgICB9KTsKICAgIHNoKCJhdUxpc3QiLGh8fCI8ZGl2IGNsYXNzPSdlbXB0eSc+Tm8gdXNlcnMgeWV0PC9kaXY+Iik7CiAgICBpZihyLmludml0ZXMmJnIuaW52aXRlcy5sZW5ndGgpe3ZhciBpaD0iPGRpdiBzdHlsZT0nZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi1ib3R0b206NHB4Jz5QZW5kaW5nIGludml0ZSBjb2Rlczo8L2Rpdj4iO3IuaW52aXRlcy5mb3JFYWNoKGZ1bmN0aW9uKGMpe2loKz0iPGRpdiBjbGFzcz0naWNvZGUnPiIrYysiPC9kaXY+Ijt9KTtzaCgibmV3SW52aXRlIixpaCk7Z2UoIm5ld0ludml0ZSIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjt9CiAgfSk7Cn0KZnVuY3Rpb24gZ2VuSW52aXRlKCl7CiAgeGhyKCIvYXBpL2FkbWluL2ludml0ZSIse30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3Mpe3NoKCJpbnZDb2RlIixyLmNvZGUpO2dlKCJpbnZDb2RlIikuY2xhc3NOYW1lPSJpY29kZSI7Z2UoIm5ld0ludml0ZSIpLmlubmVySFRNTD0iPGRpdiBzdHlsZT0nZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi1ib3R0b206NHB4Jz5OZXcgaW52aXRlIGNvZGU6PC9kaXY+PGRpdiBjbGFzcz0naWNvZGUnPiIrci5jb2RlKyI8L2Rpdj48ZGl2IHN0eWxlPSdmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7dGV4dC1hbGlnbjpjZW50ZXInPk9uZS10aW1lIHVzZSBvbmx5PC9kaXY+IjtnZSgibmV3SW52aXRlIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO2xvYWRBZG1pbigpO30KICB9KTsKfQpmdW5jdGlvbiBsb2FkSVAoKXsKICB4aHIoIi9hcGkvaXAiLG51bGwsZnVuY3Rpb24ocil7dmFyIGlwPXImJnIuaXA/ci5pcDoidW5rbm93biI7c3QoInNJUCIsaXApO3N0KCJzaXBCb3giLGlwKTt9KTsKfQpzZXRJbnRlcnZhbChmdW5jdGlvbigpewogIGlmKCFTVC5uZXh0QXQpcmV0dXJuOwogIHZhciBkPU1hdGgubWF4KDAsTWF0aC5yb3VuZCgoU1QubmV4dEF0LURhdGUubm93KCkpLzEwMDApKTsKICB2YXIgbT1NYXRoLmZsb29yKGQvNjApLHM9ZCU2MDtzdCgic2NkIixkPjA/KG0rIm0gIitzKyJzIik6IlNjYW5uaW5nLi4uIik7CiAgZ2UoInNGaWwiKS5zdHlsZS53aWR0aD1NYXRoLm1heCgwLDEwMC1kL1NULnNzKjEwMCkrIiUiOwp9LDEwMDApOwpmdW5jdGlvbiBwb2xsKCl7eGhyKCIvYXBpL3N0YXR1cyIsbnVsbCxmdW5jdGlvbihzKXtpZihzKXJlbmRlcihzKTt9KTt9Ci8vIE9uIGxvYWQ6IGNoZWNrIGlmIGFscmVhZHkgbG9nZ2VkIGluCnhocigiL2F1dGgvbWUiLG51bGwsZnVuY3Rpb24ocil7CiAgaWYociYmci5sb2dnZWRfaW4pe1NULmlzQWRtaW49ci5pc19hZG1pbjtzdCgidUJhZGdlIixyLnVzZXJuYW1lKTtzaG93QXBwKCk7bG9hZElQKCk7cG9sbCgpO30KICBlbHNle3Nob3dBdXRoKCk7fQp9KTsKc2V0SW50ZXJ2YWwoZnVuY3Rpb24oKXtpZihnZSgiYXBwIikuc3R5bGUuZGlzcGxheSE9PSJub25lIilwb2xsKCk7fSw0MDAwKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPg==").decode("utf-8")

@app.route("/")
@app.route("/login")
def index(): return Response(_DASH, mimetype="text/html")

if __name__ == "__main__":
    import sys
    if "--gen-invites" in sys.argv:
        n = int(sys.argv[sys.argv.index("--gen-invites")+1]) if len(sys.argv)>sys.argv.index("--gen-invites")+1 else 4
        codes = um.gen_invite
        for _ in range(n):
            code, msg = um.gen_invite()
            if code: print(f"  Invite: {code}")
            else: print(f"  Error: {msg}")
        sys.exit()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)