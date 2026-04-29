"""
ALPHA BOT — Delta Exchange India | BTCUSD Perpetual
"""
import os, time, hmac, hashlib, json, math, logging, threading, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

class C:
    BASE="https://api.india.delta.exchange"
    KEY=os.getenv("DELTA_API_KEY","").strip()
    SECRET=os.getenv("DELTA_API_SECRET","").strip()
    PID=27; LOT_BTC=0.001; LEVERAGE=5; SCAN_SECS=300
    MIN_CONF=58; STOP_PCT=0.025; TP_PCT=0.030
    RISK_PCT=0.015; HALT_PCT=0.08; PAUSE_PCT=0.03
    STATE="/tmp/ab.json"

class API:
    def __init__(self):
        self.key=C.KEY; self.secret=C.SECRET; self.base=C.BASE
        self.s=requests.Session()
    def set(self,k,s): self.key=k.strip(); self.secret=s.strip()
    def _sign(self,method,path,qs="",body=""):
        ts=str(int(time.time()))
        sig=hmac.new(self.secret.encode(),(method+ts+path+qs+body).encode(),hashlib.sha256).hexdigest()
        return {"api-key":self.key,"timestamp":ts,"signature":sig,"Content-Type":"application/json"}
    def get(self,path,p=None):
        qs=("?"+"&".join(f"{k}={v}" for k,v in p.items())) if p else ""
        try:
            r=self.s.get(f"{self.base}{path}{qs}",headers=self._sign("GET",path,qs),timeout=10)
            return r.json()
        except Exception as e: log.warning(f"GET {path}: {e}"); return None
    def post(self,path,body):
        b=json.dumps(body)
        try:
            r=self.s.post(f"{self.base}{path}",headers=self._sign("POST",path,"",b),data=b,timeout=10)
            return r.json()
        except Exception as e: log.warning(f"POST {path}: {e}"); return {}
    def price(self):
        try:
            r=self.s.get(f"{self.base}/v2/tickers/BTCUSD",timeout=6)
            return float(r.json().get("result",{}).get("mark_price",0) or 0)
        except: return 0.0
    def balance(self):
        d=self.get("/v2/wallet/balances")
        if not d: return 0.0,None,"No response"
        if not d.get("success"):
            err=d.get("error",{}); code=err.get("code","") if isinstance(err,dict) else str(err)
            return 0.0,d,f"API error: {code} {d.get('message','')}"
        for b in d.get("result",[]):
            if str(b.get("asset_symbol","")).upper() in ("USD","USDT"):
                av=float(b.get("available_balance",0) or 0)
                bk=float(b.get("blocked_margin",0) or 0)
                if av+bk>0: return round(av+bk,2),d,"ok"
                if av>0: return round(av,2),d,"ok"
        ne=float((d.get("meta") or {}).get("net_equity",0) or 0)
        if ne>0: return round(ne,2),d,"ok"
        return 0.0,d,f"Zero balance. Assets:{[b.get('asset_symbol') for b in d.get('result',[])]}"
    def candles(self,res="5m",n=100):
        mins={"5m":5,"15m":15}.get(res,5); end=int(time.time())
        d=self.get("/v2/history/candles",{"symbol":"BTCUSD","resolution":res,"start":end-mins*60*n,"end":end})
        return d.get("result",[]) if d and d.get("success") else []
    def positions(self):
        d=self.get("/v2/positions/margined")
        return [p for p in d.get("result",[]) if abs(float(p.get("size",0) or 0))>0] if d and d.get("success") else []
    def order(self,side,lots):
        return self.post("/v2/orders",{"product_id":C.PID,"size":lots,"side":side,"order_type":"market_order","time_in_force":"ioc"})
    def bracket(self,side,lots,stop,tp):
        return self.post("/v2/orders",{"product_id":C.PID,"size":lots,"side":side,"order_type":"stop_market_order",
            "stop_price":str(round(stop,1)),"bracket_stop_loss_price":str(round(stop,1)),
            "bracket_take_profit_price":str(round(tp,1)),"time_in_force":"gtc","stop_trigger_method":"mark_price"})
    def close_all(self):
        n=0
        for p in self.positions():
            sz=float(p.get("size",0) or 0); q=abs(int(sz))
            if q:
                self.post("/v2/orders",{"product_id":p.get("product_id",C.PID),"size":q,
                    "side":"sell" if sz>0 else "buy","order_type":"market_order","time_in_force":"ioc"})
                n+=1
        return n

def parse(raw):
    cl,hi,lo,vo=[],[],[],[]
    for c in raw:
        try:
            v=float(c.get("close",0) or 0)
            if v>0: cl.append(v);hi.append(float(c.get("high",v) or v));lo.append(float(c.get("low",v) or v));vo.append(float(c.get("volume",0) or 0))
        except: pass
    return cl,hi,lo,vo

def ema(p,n):
    if len(p)<n: return [p[-1]]*len(p) if p else []
    k=2/(n+1); v=[sum(p[:n])/n]
    for x in p[n:]: v.append(x*k+v[-1]*(1-k))
    return [v[0]]*(n-1)+v

def rsi(p,n=14):
    if len(p)<n+2: return 50.0
    d=[p[i]-p[i-1] for i in range(1,len(p))]
    g=sum(max(x,0) for x in d[-n:])/n; l=sum(abs(min(x,0)) for x in d[-n:])/n
    return round(100.0 if l<1e-10 else 100-100/(1+g/l),1)

def adx(hi,lo,cl,n=14):
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
    at=ws(tr);pd=ws(pm);nd=ws(nm)
    pi=[100*pd[i]/at[i] if at[i]>0 else 0 for i in range(len(at))]
    ni=[100*nd[i]/at[i] if at[i]>0 else 0 for i in range(len(at))]
    dx=[abs(pi[i]-ni[i])/(pi[i]+ni[i])*100 if pi[i]+ni[i]>0 else 0 for i in range(len(pi))]
    return round(sum(dx[-n:])/n,1),round(pi[-1],1),round(ni[-1],1)

def atr(hi,lo,cl,n=14):
    if len(cl)<n+1: return 0.0
    return sum([max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])) for i in range(1,len(cl))][-n:])/n

def score(cl,hi,lo,vo,cl15,hour,dirn):
    if len(cl)<55: return 0,"need_55_candles"
    if hour in [2,3,4,5]: return 0,"dead_zone_UTC"
    if len(vo)>=21:
        avg=sum(vo[-21:-1])/20
        if vo[-2]<avg*0.10: return 0,"low_volume"
    adx_v,pdi,ndi=adx(hi,lo,cl); rsi_v=rsi(cl)
    e8=ema(cl,8)[-1];e21=ema(cl,21)[-1];e55=ema(cl,55)[-1];price=cl[-1]
    bull=price>e8>e21>e55 and adx_v>20 and pdi>ndi
    bear=price<e8<e21<e55 and adx_v>20 and ndi>pdi
    s=0
    if dirn=="long" and bull: s+=40
    elif dirn=="short" and bear: s+=40
    elif adx_v>15: s+=15
    else: s+=5
    if dirn=="long":
        if 35<=rsi_v<=55: s+=25
        elif rsi_v<35: s+=20
        elif rsi_v<=65: s+=10
    else:
        if 45<=rsi_v<=65: s+=25
        elif rsi_v>65: s+=20
        elif rsi_v>=35: s+=10
    if len(cl15)>=21:
        e8_=ema(cl15,8)[-1];e21_=ema(cl15,21)[-1]
        if dirn=="long" and cl15[-1]>e8_>e21_: s+=20
        elif dirn=="short" and cl15[-1]<e8_<e21_: s+=20
        else: s+=5
    else: s+=10
    if adx_v>30: s+=15
    elif adx_v>22: s+=10
    elif adx_v>15: s+=5
    return min(s,100),""

class Bot:
    def __init__(self):
        self.api=API();self.running=False;self.connected=False
        self.capital=0.0;self.start_cap=0.0;self.day_start=0.0
        self.halted=False;self.halt_msg=""
        self.status="Not connected"
        self.logs=[];self.trades=[]
        self.scan_n=0;self.next_scan=None
        self.price=0.0;self.regime="—"
        self.rsi_v=50.0;self.adx_v=0.0;self.atr_pct=0.0
        self.l_sc=0;self.s_sc=0;self.l_vt="";self.s_vt=""
        self.total_tr=0;self.wins=0;self._stops=set()
    def emit(self,level,msg):
        e={"t":datetime.now(timezone.utc).strftime("%H:%M:%S"),"l":level,"m":msg}
        self.logs.append(e)
        if len(self.logs)>500: self.logs.pop(0)
        getattr(log,{"INFO":"info","WARN":"warning","ERROR":"error","TRADE":"info"}.get(level,"info"))(msg)
    def save(self):
        try: json.dump({"sc":self.start_cap,"ds":self.day_start,"halted":self.halted,"hm":self.halt_msg,
            "tr":self.total_tr,"w":self.wins,"trades":self.trades[-100:],"stops":list(self._stops)},open(C.STATE,"w"))
        except: pass
    def load(self):
        try:
            if not os.path.exists(C.STATE): return False
            s=json.load(open(C.STATE))
            self.start_cap=float(s.get("sc",0));self.day_start=float(s.get("ds",0))
            self.halted=bool(s.get("halted",False));self.halt_msg=s.get("hm","")
            self.total_tr=int(s.get("tr",0));self.wins=int(s.get("w",0))
            self.trades=s.get("trades",[]);self._stops=set(s.get("stops",[]))
            if self.start_cap>0: self.emit("INFO",f"Restored: start=${self.start_cap:.2f} trades={self.total_tr}"); return True
        except: pass
        return False
    def connect(self,key,secret):
        self.api.set(key,secret)
        bal,raw,err=self.api.balance()
        if bal<=0:
            srv="unknown"
            try: srv=requests.get("https://api.ipify.org?format=json",timeout=4).json().get("ip","?")
            except: pass
            return {"success":False,"message":err,"server_ip":srv,"raw":raw}
        self.capital=bal;self.connected=True
        if not self.load() or self.start_cap<=0: self.start_cap=bal;self.day_start=bal;self.save()
        self.emit("INFO",f"Connected | ${bal:.2f} | Start ${self.start_cap:.2f} | Halt <${self.start_cap*(1-C.HALT_PCT):.2f}")
        self._sync();
        if not self.running: self.start()
        return {"success":True,"balance":bal}
    def _sync(self):
        for p in self.api.positions():
            sz=float(p.get("size",0) or 0); entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            pid=str(p.get("product_id",C.PID));sym=str(p.get("product_symbol","BTCUSD"))
            side="long" if sz>0 else "short"; lots=abs(int(sz))
            upnl=float(p.get("unrealized_pnl",0) or 0)
            if not any(str(t.get("pid",""))==pid and t.get("exit") is None for t in self.trades):
                self.trades.append({"time":datetime.now(timezone.utc).isoformat(),"side":side,
                    "entry":round(entry,1),"exit":None,"lots":lots,"pnl":None,"pct":None,
                    "reason":"synced","won":None,"pid":pid,"sym":sym,"upnl":round(upnl,3)})
                self.emit("INFO",f"Synced: {side.upper()} {lots}L {sym} @ ${entry:.0f}")
            if pid not in self._stops:
                sp=entry*(1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT)
                tp=entry*(1+C.TP_PCT if side=="long" else 1-C.TP_PCT)
                cs="sell" if side=="long" else "buy"
                r=self.api.bracket(cs,lots,sp,tp)
                if r.get("success"): self._stops.add(pid);self.emit("INFO",f"Stop placed: ${sp:.0f} TP=${tp:.0f}");self.save()
                else: self.emit("WARN",f"Stop FAILED — set manually ${sp:.0f} | {r.get('error','?')}")
    def _sw(self):
        bal,_,err=self.api.balance()
        if bal<=0: self.emit("WARN",f"Wallet: {err}"); return
        self.capital=bal
        if self.start_cap>0:
            loss=(self.start_cap-bal)/self.start_cap
            if loss>=C.HALT_PCT and not self.halted:
                self.halted=True;self.halt_msg=f"Down {loss*100:.1f}% (${self.start_cap:.2f}>${bal:.2f})"
                self.emit("ERROR",f"HALTED: {self.halt_msg}");self.save()
        self.emit("INFO",f"Wallet ${bal:.2f} | {'HALTED' if self.halted else 'OK'}")
    def _pd(self):
        out=[]
        for p in self.api.positions():
            sz=float(p.get("size",0) or 0); entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            mark=float(p.get("mark_price") or self.price or entry)
            upnl=float(p.get("unrealized_pnl") or 0); side="long" if sz>0 else "short"
            pct=((mark-entry)/entry if side=="long" else (entry-mark)/entry)*100
            out.append({"sym":p.get("product_symbol","BTCUSD"),"side":side,"lots":abs(sz),
                "entry":round(entry,1),"mark":round(mark,1),"upnl":round(upnl,3),"pct":round(pct,2),
                "stop":round(entry*(1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT),1),
                "tp":round(entry*(1+C.TP_PCT if side=="long" else 1-C.TP_PCT),1)})
        return out
    def _exits(self):
        if not self.price: return
        for p in self.api.positions():
            sz=float(p.get("size",0) or 0); entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            side="long" if sz>0 else "short"
            pct=(self.price-entry)/entry if side=="long" else (entry-self.price)/entry
            lots=abs(int(sz)); pid=p.get("product_id",C.PID)
            if pct<=-C.STOP_PCT or pct>=C.TP_PCT:
                cs="sell" if side=="long" else "buy"
                r=self.api.post("/v2/orders",{"product_id":pid,"size":lots,"side":cs,"order_type":"market_order","time_in_force":"ioc"})
                if r.get("success"):
                    pnl=round(entry*lots*C.LOT_BTC*pct,4); reason="stop" if pct<=-C.STOP_PCT else "tp"
                    self.emit("TRADE",f"{'❌' if pct<0 else '✅'} {reason.upper()} {side.upper()} {lots}L ${entry:.0f}>${self.price:.0f} P&L ${pnl:+.4f} ({pct*100:.2f}%)")
                    for t in reversed(self.trades):
                        if t.get("side")==side and t.get("entry")==round(entry,1) and t.get("exit") is None:
                            t.update({"exit":round(self.price,1),"pnl":pnl,"pct":round(pct*100,2),"won":pct>0,"reason":reason})
                            if pct>0: self.wins+=1
                            break
                    self.save()
    def scan(self):
        self.scan_n+=1; self.next_scan=(datetime.now(timezone.utc)+timedelta(seconds=C.SCAN_SECS)).isoformat()
        p=self.api.price()
        if p>0: self.price=p
        if self.scan_n%5==0: self._sw()
        if self.halted: self.status=f"HALTED: {self.halt_msg}"; return
        raw5=self.api.candles("5m",100); raw15=self.api.candles("15m",60)
        cl,hi,lo,vo=parse(raw5); cl15,*_=parse(raw15)
        if len(cl)<55: self.status=f"{len(cl)} candles need 55"; return
        self.price=cl[-1]; self.rsi_v=rsi(cl); self.adx_v,pdi,ndi=adx(hi,lo,cl)
        self.atr_pct=round(atr(hi,lo,cl)/self.price*100,3)
        e8=ema(cl,8)[-1];e21=ema(cl,21)[-1];e55=ema(cl,55)[-1]
        if   self.price>e8>e21>e55 and self.adx_v>25 and pdi>ndi: self.regime="STRONG BULL"
        elif self.price>e8>e21 and self.adx_v>18: self.regime="BULL"
        elif self.price<e8<e21<e55 and self.adx_v>25 and ndi>pdi: self.regime="STRONG BEAR"
        elif self.price<e8<e21 and self.adx_v>18: self.regime="BEAR"
        else: self.regime="NEUTRAL"
        real=self.api.positions(); self._exits(); self._sync()
        if len(real)>=1:
            d=self._pd(); x=d[0] if d else {}
            self.status=f"Holding {x.get('side','').upper()} {x.get('lots',0):.0f}L @ ${x.get('entry',0):,.0f} UPL ${x.get('upnl',0):+.3f} ({x.get('pct',0):+.2f}%)"
            self.emit("INFO",self.status); return
        if self.day_start>0 and (self.capital-self.day_start)/self.day_start<=-C.PAUSE_PCT:
            self.status="Paused — daily -3% limit"; return
        ls,lv=score(cl,hi,lo,vo,cl15,datetime.now(timezone.utc).hour,"long")
        ss,sv=score(cl,hi,lo,vo,cl15,datetime.now(timezone.utc).hour,"short")
        self.l_sc=ls;self.s_sc=ss;self.l_vt=lv;self.s_vt=sv
        self.emit("INFO",f"#{self.scan_n} ${self.price:,.0f} {self.regime} RSI={self.rsi_v} ADX={self.adx_v} L={ls}{'x'+lv if lv else ''} S={ss}{'x'+sv if sv else ''}")
        dirn=sc=None
        if not lv and ls>=C.MIN_CONF and ls>ss: dirn,sc="long",ls
        elif not sv and ss>=C.MIN_CONF and ss>ls: dirn,sc="short",ss
        if not dirn: self.status=f"Watching {lv or sv or f'score {max(ls,ss)}<{C.MIN_CONF}'} {self.regime}"; return
        m=self.price*C.LOT_BTC/C.LEVERAGE
        lots=max(1,min(int(max(self.capital*C.RISK_PCT,m)/m),max(1,int(self.capital*.10/m))))
        side="buy" if dirn=="long" else "sell"
        r=self.api.order(side,lots)
        if not r.get("success"): self.status=f"Order failed: {r.get('error','?')[:50]}"; self.emit("ERROR",self.status); return
        sp=self.price*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp=self.price*(1+C.TP_PCT if dirn=="long" else 1-C.TP_PCT)
        cs="sell" if dirn=="long" else "buy"
        sr=self.api.bracket(cs,lots,sp,tp)
        if sr.get("success"): self._stops.add(str(C.PID));self.emit("INFO",f"Stop ${sp:.0f} TP ${tp:.0f}")
        else: self.emit("WARN",f"BRACKET FAILED — SET STOP ${sp:.0f}")
        self.status=f"{dirn.upper()} {lots}L @ ${self.price:,.0f} score={sc}"
        self.emit("TRADE",self.status); self.total_tr+=1
        self.trades.append({"time":datetime.now(timezone.utc).isoformat(),"side":dirn,"entry":round(self.price,1),
            "exit":None,"lots":lots,"pnl":None,"pct":None,"reason":"bot","won":None,"pid":str(C.PID),"sym":"BTCUSD"})
        self.save()
    def start(self):
        if not self.running: self.running=True; threading.Thread(target=self._loop,daemon=True).start(); self.emit("INFO","Bot started")
    def stop(self): self.running=False; self.emit("INFO","Bot stopped")
    def _loop(self):
        while self.running:
            try: self.scan()
            except Exception as e: log.error(f"Error: {e}",exc_info=True); self.status=f"Error: {e}"
            time.sleep(C.SCAN_SECS)
    def state(self):
        sc=self.start_cap or self.capital; pnl=(self.capital-sc)/sc*100 if sc>0 else 0
        done=[t for t in self.trades if t.get("won") is not None]
        wr=sum(1 for t in done if t["won"])/len(done)*100 if done else 0
        return {"running":self.running,"connected":self.connected,"halted":self.halted,"halt_msg":self.halt_msg,
            "status":self.status,"price":round(self.price,1),"regime":self.regime,"rsi":self.rsi_v,"adx":self.adx_v,
            "atr_pct":self.atr_pct,"l_sc":self.l_sc,"s_sc":self.s_sc,"l_vt":self.l_vt,"s_vt":self.s_vt,
            "capital":round(self.capital,2),"start_cap":round(sc,2),"pnl_pct":round(pnl,2),
            "win_rate":round(wr,1),"total_trades":self.total_tr,"wins":self.wins,
            "next_scan":self.next_scan,"scan_n":self.scan_n,
            "open_pos":self._pd(),"trades":list(reversed(self.trades[-50:])),"logs":list(reversed(self.logs[-100:])),
            "guardrails":{"Hard stop":f"{C.STOP_PCT*100:.1f}% bracket on Delta","Take profit":f"{C.TP_PCT*100:.1f}%",
                "Monthly halt":f"Down {C.HALT_PCT*100:.0f}% from start","Daily pause":f"Down {C.PAUSE_PCT*100:.0f}% today",
                "Max positions":"1 (live Delta API check)"}}

app=Flask(__name__); CORS(app); bot=Bot()
if C.KEY and C.SECRET: threading.Thread(target=lambda: bot.connect(C.KEY,C.SECRET),daemon=True).start()

@app.after_request
def _c(r):
    r.headers.update({"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET,POST,OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
    return r
@app.route("/api/status")
@app.route("/api/bot/status")
def api_s(): return jsonify(bot.state())
@app.route("/api/connect",methods=["POST","OPTIONS"])
def api_conn():
    if request.method=="OPTIONS": return jsonify({})
    d=request.json or {}; k=d.get("api_key","").strip(); s=d.get("api_secret","").strip()
    if not k or not s: return jsonify({"success":False,"message":"Key and secret required"})
    return jsonify(bot.connect(k,s))
@app.route("/api/bot/start",methods=["POST"])
def api_start(): bot.start(); return jsonify({"success":True})
@app.route("/api/bot/stop",methods=["POST"])
def api_stop(): bot.stop(); return jsonify({"success":True})
@app.route("/api/bot/run_now",methods=["POST"])
def api_run(): threading.Thread(target=bot.scan,daemon=True).start(); return jsonify({"success":True})
@app.route("/api/trades")
def api_tr(): return jsonify(list(reversed(bot.trades[-50:])))
@app.route("/api/logs")
def api_lg(): return jsonify(bot.logs)
@app.route("/api/positions")
def api_pos(): return jsonify({"raw":bot.api.positions(),"display":bot._pd()})
@app.route("/api/ticker")
def api_tick():
    p=bot.api.price()
    if not p:
        try: p=requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",timeout=5).json()["bitcoin"]["usd"]
        except: p=0
    return jsonify({"price":p})
@app.route("/api/ip")
def api_ip():
    try: ip=requests.get("https://api.ipify.org?format=json",timeout=5).json().get("ip","?")
    except: ip="unknown"
    return jsonify({"ip":ip})
@app.route("/api/close_all",methods=["POST"])
def api_ca(): n=bot.api.close_all(); bot.emit("TRADE",f"Closed {n}"); return jsonify({"success":True,"closed":n})
@app.route("/api/manual_trade",methods=["POST"])
def api_mt():
    d=request.json or {}; dirn=d.get("direction","")
    if dirn not in ("long","short"): return jsonify({"success":False,"message":"direction: long or short"})
    p=bot.price or bot.api.price(); lots=max(1,int(d.get("lots",1))); side="buy" if dirn=="long" else "sell"
    r=bot.api.order(side,lots)
    if r.get("success"):
        sp=p*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT); tp=p*(1+C.TP_PCT if dirn=="long" else 1-C.TP_PCT)
        bot.api.bracket("sell" if dirn=="long" else "buy",lots,sp,tp)
        bot.emit("TRADE",f"MANUAL {dirn.upper()} {lots}L @${p:,.0f}")
        bot.trades.append({"time":datetime.now(timezone.utc).isoformat(),"side":dirn,"entry":round(p,1),"exit":None,"lots":lots,"pnl":None,"pct":None,"reason":"manual","won":None,"pid":str(C.PID),"sym":"BTCUSD"})
        bot.save(); return jsonify({"success":True,"entry":round(p,1),"stop":round(sp,1),"tp":round(tp,1)})
    return jsonify({"success":False,"message":r.get("error","failed")})
@app.route("/api/set_stop",methods=["POST"])
def api_ss():
    d=request.json or {}; dirn=d.get("direction","long"); entry=float(d.get("entry",bot.price or 77000)); lots=int(d.get("lots",1))
    sp=entry*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT); tp=entry*(1+C.TP_PCT if dirn=="long" else 1-C.TP_PCT)
    r=bot.api.bracket("sell" if dirn=="long" else "buy",lots,sp,tp)
    return jsonify({"success":r.get("success",False),"stop":round(sp,1),"tp":round(tp,1)})
@app.route("/api/debug/auth")
def api_da():
    out={"key_len":len(bot.api.key),"key_set":bool(bot.api.key),"secret_len":len(bot.api.secret)}
    try:
        r=requests.get(f"{bot.api.base}/v2/tickers/BTCUSD",timeout=6)
        out["ticker_ok"]=r.status_code==200; out["btc_price"]=r.json().get("result",{}).get("mark_price","?")
    except Exception as e: out["ticker_error"]=str(e)
    bal,raw,err=bot.api.balance(); out["balance"]=bal; out["err"]=err; out["raw"]=raw
    return jsonify(out)
@app.route("/api/debug/candles")
def api_dc():
    for res in ["5m","1m","15m"]:
        d=bot.api.get("/v2/history/candles",{"symbol":"BTCUSD","resolution":res,"start":int(time.time())-3600,"end":int(time.time())})
        if d and d.get("success") and d.get("result"): return jsonify({"ok":True,"res":res,"count":len(d["result"]),"sample":d["result"][0]})
    return jsonify({"ok":False})
@app.route("/api/debug/positions")
def api_dp(): return jsonify(bot.api.get("/v2/positions/margined") or {"error":"no_response"})


DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Alpha Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f5f5f5;color:#1a1a2e;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}
.hdr{background:#fff;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 4px rgba(0,0,0,.08);position:sticky;top:0;z-index:100}
.logo{font-size:16px;font-weight:700;color:#00b386;display:flex;align-items:center;gap:8px}
.logo span{background:#00b386;color:#fff;width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:900}
.pill{font-size:11px;font-weight:600;padding:5px 12px;border-radius:20px;display:flex;align-items:center;gap:5px}
.pill-ok{background:#e6f9f3;color:#00b386}
.pill-off{background:#fff0f0;color:#e74c3c}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.tabs{background:#fff;display:flex;border-bottom:1px solid #eee;position:sticky;top:57px;z-index:99}
.tab{flex:1;padding:12px;text-align:center;font-size:12px;font-weight:600;color:#999;border-bottom:2px solid transparent;cursor:pointer;text-transform:uppercase;letter-spacing:.5px}
.tab.on{color:#00b386;border-bottom-color:#00b386}
.pnl{display:none;padding:12px 14px}
.pnl.on{display:block}
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,.05)}
.ct{font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
.price{font-size:38px;font-weight:700;letter-spacing:-1px}
.chip{display:inline-block;margin-top:8px;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700}
.c-bull{background:#e6f9f3;color:#00b386}
.c-bear{background:#fff0f0;color:#e74c3c}
.c-neu{background:#f5f5f5;color:#999}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.sbox{background:#f8f9fa;border-radius:10px;padding:12px 8px;text-align:center}
.slbl{font-size:10px;color:#999;margin-bottom:4px;font-weight:600;text-transform:uppercase}
.snum{font-size:22px;font-weight:700}
.g{color:#00b386}.r{color:#e74c3c}.y{color:#f39c12}
.ssub{font-size:9px;color:#bbb;margin-top:2px;min-height:12px}
.ibox{background:#f8f9fa;border-radius:10px;padding:10px;text-align:center}
.ilbl{font-size:10px;color:#999;margin-bottom:3px;font-weight:600}
.ival{font-size:17px;font-weight:700}
.sb{background:#f0f8ff;border:1px solid #d0e8f8;border-radius:10px;padding:10px 12px;font-size:12px;color:#2980b9;margin-bottom:10px;min-height:34px;line-height:1.5}
.sb-h{background:#e8f5e9;border-color:#c8e6c9;color:#2e7d32}
.sb-w{background:#fffde7;border-color:#f9e4a0;color:#e67e22}
.prog{height:3px;background:#eee;border-radius:2px;overflow:hidden;margin:8px 0 4px}
.progf{height:100%;background:#00b386;border-radius:2px;transition:width .4s}
.cd{font-size:11px;color:#bbb}
.pc{border-radius:12px;padding:14px;margin-bottom:10px}
.pc-l{background:#e8f9f1;border:1px solid #b2dfdb}
.pc-s{background:#fff0f0;border:1px solid #ffcdd2}
.ph{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.psym{font-size:16px;font-weight:700}
.pbadge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}
.pb-l{background:#00b386;color:#fff}
.pb-s{background:#e74c3c;color:#fff}
.pg{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.pi{background:rgba(255,255,255,.7);border-radius:8px;padding:8px}
.pil{font-size:10px;color:#777;margin-bottom:2px}
.piv{font-size:14px;font-weight:700}
.wrow{display:flex;justify-content:space-between;align-items:baseline}
.wamt{font-size:30px;font-weight:700}
.wpct{font-size:16px;font-weight:700}
.wst{font-size:11px;color:#bbb;margin-top:2px}
.stat{background:#f8f9fa;border-radius:10px;padding:12px;text-align:center}
.stlbl{font-size:10px;color:#999;margin-bottom:4px;font-weight:600}
.stval{font-size:20px;font-weight:700}
.btn{padding:14px;border-radius:10px;border:none;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;width:100%}
.b-go{background:#00b386;color:#fff}
.b-st{background:#e74c3c;color:#fff}
.b-sc{background:#3498db;color:#fff;margin-bottom:8px}
.b-cl{background:#e74c3c;color:#fff;opacity:.8}
.b-bl{background:#e8f9f1;color:#00b386;border:1.5px solid #00b386;flex:1}
.b-bs{background:#fff0f0;color:#e74c3c;border:1.5px solid #e74c3c;flex:1}
.mrow{display:flex;gap:8px;margin-top:8px}
.inp{width:100%;border:1.5px solid #e0e0e0;border-radius:10px;padding:12px;font-size:14px;font-family:inherit;margin-bottom:8px;outline:none}
.inp:focus{border-color:#00b386}
.ti{background:#f8f9fa;border-radius:10px;padding:12px;margin-bottom:8px}
.tt{display:flex;justify-content:space-between;margin-bottom:6px}
.tl{font-weight:700}
.tm{font-size:11px;color:#bbb}
.tp{display:flex;justify-content:space-between;font-size:12px;color:#777}
.tpnl{font-size:13px;font-weight:700;margin-top:4px}
.topen{font-size:11px;color:#f39c12;font-style:italic;font-weight:600}
.tag{display:inline-block;font-size:9px;padding:1px 6px;border-radius:8px;margin-left:4px;font-weight:600}
.tag-s{background:#d4edda;color:#155724}
.tag-m{background:#d1ecf1;color:#0c5460}
.tag-b{background:#e2d9f3;color:#6f42c1}
.tag-y{background:#fff3cd;color:#856404}
.lb{background:#1a1a2e;border-radius:10px;padding:12px;max-height:350px;overflow-y:auto}
.le{padding:4px 0;border-bottom:1px solid #252540;font-size:11px;display:flex;gap:8px;font-family:monospace}
.lt{color:#555;white-space:nowrap}
.INFO{color:#7f8c8d}.WARN{color:#f39c12}.ERROR{color:#e74c3c}.TRADE{color:#00b386;font-weight:700}
.fp{padding:5px 12px;border-radius:20px;border:1px solid #e0e0e0;background:#fff;font-size:11px;font-weight:600;cursor:pointer;color:#999}
.fp.on{background:#1a1a2e;color:#fff;border-color:#1a1a2e}
.frow{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.gi{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f0f0f0;font-size:13px}
.gi:last-child{border:none}
.gk{color:#555}
.gv{color:#00b386;font-weight:600;text-align:right;max-width:60%}
.hbanner{background:#fff0f0;border:1.5px solid #e74c3c;border-radius:12px;padding:14px;margin-bottom:10px;text-align:center;color:#e74c3c;font-weight:700}
.ipd{font-size:22px;font-weight:700;text-align:center;padding:12px;background:#f8f9fa;border-radius:10px;font-family:monospace;letter-spacing:1px;margin-bottom:8px}
.b2r{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo"><span>&#916;</span> ALPHA BOT</div>
  <div class="pill pill-off" id="cPill"><div class="dot"></div><span id="cLbl">Not connected</span></div>
</div>
<div class="tabs">
  <div class="tab on" onclick="T('home')">Home</div>
  <div class="tab" onclick="T('trades')">Trades</div>
  <div class="tab" onclick="T('logs')">Logs</div>
  <div class="tab" onclick="T('settings')">Settings</div>
</div>
<div id="pnl-home" class="pnl on">
  <div id="hb" class="hbanner" style="display:none"></div>
  <div class="card">
    <div class="ct">Bitcoin &middot; Live</div>
    <div class="price" id="px">$&#8212;</div>
    <span class="chip c-neu" id="rc">Loading</span>
  </div>
  <div class="card">
    <div class="sb" id="sb">Initializing...</div>
    <div class="g3" style="margin-bottom:10px">
      <div class="sbox"><div class="slbl">&#8593; Long</div><div class="snum g" id="ls">&#8212;</div><div class="ssub" id="lv"></div></div>
      <div class="sbox"><div class="slbl">&#8595; Short</div><div class="snum r" id="ss">&#8212;</div><div class="ssub" id="sv"></div></div>
      <div class="sbox"><div class="slbl">&#9889; Signal</div><div class="snum y" id="dec">WAIT</div><div class="ssub" id="ds">No signal</div></div>
    </div>
    <div class="g3"><div class="ibox"><div class="ilbl">RSI 14</div><div class="ival" id="rv">&#8212;</div></div><div class="ibox"><div class="ilbl">ADX 14</div><div class="ival" id="av">&#8212;</div></div><div class="ibox"><div class="ilbl">ATR %</div><div class="ival" id="at">&#8212;</div></div></div>
    <div class="prog"><div class="progf" id="pb" style="width:0"></div></div>
    <div class="cd" id="cd">Next scan in &#8212;</div>
  </div>
  <div id="opa"></div>
  <div class="card">
    <div class="ct">Wallet Balance</div>
    <div class="wrow"><div class="wamt" id="wa">$&#8212;</div><div class="wpct" id="wp">&#8212;</div></div>
    <div class="wst" id="ws"></div>
  </div>
  <div class="card">
    <div class="g3"><div class="stat"><div class="stlbl">Win Rate</div><div class="stval g" id="wr">&#8212;</div></div><div class="stat"><div class="stlbl">Trades</div><div class="stval" id="tc">0</div></div><div class="stat"><div class="stlbl">Scan #</div><div class="stval" style="color:#3498db" id="sn">0</div></div></div>
  </div>
  <div class="b2r"><button class="btn b-go" onclick="A('/api/bot/start',{})">&#9654; Start</button><button class="btn b-st" onclick="A('/api/bot/stop',{})">&#9632; Stop</button></div>
  <button class="btn b-sc" onclick="A('/api/bot/run_now',{})">&#9889; Scan Now</button>
  <button class="btn b-cl" onclick="closeAll()" style="margin-bottom:10px">&#9888; Close All Positions</button>
  <div class="card">
    <div class="ct">Manual Trade</div>
    <input class="inp" id="ml" type="number" placeholder="Lots (default 1)" min="1">
    <div class="mrow"><button class="btn b-bl" onclick="MT('long')">&#8593; BUY LONG</button><button class="btn b-bs" onclick="MT('short')">&#8595; SELL SHORT</button></div>
  </div>
</div>
<div id="pnl-trades" class="pnl">
  <div id="tl"><div style="text-align:center;padding:40px;color:#bbb">No trades yet</div></div>
</div>
<div id="pnl-logs" class="pnl">
  <div class="frow">
    <div class="fp on" onclick="FL('')" id="fa">All</div>
    <div class="fp" onclick="FL('TRADE')" id="ft">Trades</div>
    <div class="fp" onclick="FL('WARN')" id="fw">Warnings</div>
    <div class="fp" onclick="FL('ERROR')" id="fe">Errors</div>
  </div>
  <div style="font-size:11px;color:#bbb;margin-bottom:6px" id="lc">0 entries</div>
  <div class="lb" id="lb"></div>
</div>
<div id="pnl-settings" class="pnl">
  <div class="card">
    <div class="ct">Delta Exchange Login</div>
    <input class="inp" id="ak" type="text" placeholder="API Key">
    <input class="inp" id="as" type="password" placeholder="API Secret">
    <button class="btn" style="background:#1a1a2e;color:#fff;margin-bottom:8px" onclick="">India &#10003;</button>
    <button class="btn b-go" onclick="CON()">Connect to Delta Exchange</button>
    <div id="cm" style="margin-top:8px;font-size:12px;text-align:center"></div>
  </div>
  <div class="card">
    <div class="ct">Server IP &#8212; Whitelist on Delta</div>
    <div class="ipd" id="ip">Loading...</div>
    <div style="font-size:11px;color:#999;line-height:1.8">1. Copy IP above<br>2. Delta &#8594; Account &#8594; API Keys &#8594; Edit<br>3. Paste into IP Whitelist &#8594; Save</div>
  </div>
  <div class="card">
    <div class="ct">Active Guardrails</div>
    <div id="gl"></div>
  </div>
</div>
<script>
var LG=[],LF='',CT=[];
function T(n){['home','trades','logs','settings'].forEach(function(t,i){document.querySelectorAll('.tab')[i].classList.toggle('on',t===n);document.getElementById('pnl-'+t).classList.toggle('on',t===n);});if(n==='logs')RL();if(n==='trades')RT();}
function A(u,b){return fetch(u,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{}).then(function(r){return r.json();}).catch(function(){return null;});}
function R(s){
  if(!s)return;
  var ok=s.connected&&!s.halted;
  document.getElementById('cPill').className='pill '+(ok?'pill-ok':'pill-off');
  document.getElementById('cLbl').textContent=s.halted?'HALTED':s.connected?'Connected ✓':'Not connected';
  var hb=document.getElementById('hb');hb.style.display=s.halted?'block':'none';if(s.halted)hb.textContent='BOT HALTED: '+s.halt_msg;
  document.getElementById('px').textContent=s.price?'$'+s.price.toLocaleString():'$—';
  var rc=document.getElementById('rc');rc.textContent=s.regime||'—';
  var r=(s.regime||'').toLowerCase();rc.className='chip '+(r.includes('bull')?'c-bull':r.includes('bear')?'c-bear':'c-neu');
  var sb=document.getElementById('sb');sb.textContent=s.status||'—';
  sb.className='sb'+(s.status&&s.status.includes('Holding')?' sb-h':s.status&&s.status.includes('HALT')?' sb-w':'');
  var ls=s.l_sc||0,ss=s.s_sc||0,mc=58;
  document.getElementById('ls').textContent=ls||'—';document.getElementById('ss').textContent=ss||'—';
  document.getElementById('lv').textContent=s.l_vt?'✗ '+s.l_vt:'';document.getElementById('sv').textContent=s.s_vt?'✗ '+s.s_vt:'';
  var dec='WAIT',ds='No signal',dc='y';
  if(!s.l_vt&&ls>=mc&&ls>ss){dec='LONG';ds='score='+ls;dc='g';}
  if(!s.s_vt&&ss>=mc&&ss>ls){dec='SHORT';ds='score='+ss;dc='r';}
  var de=document.getElementById('dec');de.textContent=dec;de.className='snum '+dc;
  document.getElementById('ds').textContent=ds;
  document.getElementById('rv').textContent=s.rsi||'—';document.getElementById('av').textContent=s.adx||'—';document.getElementById('at').textContent=s.atr_pct?s.atr_pct+'%':'—';
  if(s.next_scan){var sec=Math.max(0,Math.round((new Date(s.next_scan)-Date.now())/1000));document.getElementById('pb').style.width=Math.max(0,100-sec/300*100)+'%';document.getElementById('cd').textContent=sec>0?'Next scan in '+Math.floor(sec/60)+'m '+sec%60+'s':'Scanning...';}
  var ops=s.open_pos||[];document.getElementById('opa').innerHTML=ops.map(function(p){return '<div class="pc pc-'+p.side+'"><div class="ph"><span class="psym">'+p.sym+'</span><span class="pbadge pb-'+p.side+'">'+p.side.toUpperCase()+'</span></div><div class="pg"><div class="pi"><div class="pil">Entry</div><div class="piv">$'+p.entry.toLocaleString()+'</div></div><div class="pi"><div class="pil">Lots</div><div class="piv">'+p.lots+'</div></div><div class="pi"><div class="pil">UPL</div><div class="piv '+(p.upnl>=0?'g':'r')+'">$'+(p.upnl>=0?'+':'')+p.upnl+' ('+(p.pct>=0?'+':'')+p.pct+'%)</div></div><div class="pi"><div class="pil">Mark</div><div class="piv">$'+p.mark.toLocaleString()+'</div></div><div class="pi"><div class="pil" style="color:#e74c3c">Stop Loss</div><div class="piv r">$'+p.stop.toLocaleString()+'</div></div><div class="pi"><div class="pil" style="color:#00b386">Take Profit</div><div class="piv g">$'+p.tp.toLocaleString()+'</div></div></div></div>';}).join('');
  document.getElementById('wa').textContent=s.capital?'$'+s.capital.toFixed(2):'$—';
  var pp=s.pnl_pct||0;var we=document.getElementById('wp');we.textContent=(pp>=0?'+':'')+pp.toFixed(2)+'%';we.className='wpct '+(pp>=0?'g':'r');
  document.getElementById('ws').textContent=s.start_cap?'Started: $'+s.start_cap.toFixed(2):'';
  document.getElementById('wr').textContent=s.win_rate!=null?s.win_rate+'%':'—';document.getElementById('tc').textContent=s.total_trades||0;document.getElementById('sn').textContent=s.scan_n||0;
  if(s.logs)LG=s.logs;document.getElementById('lc').textContent=LG.length+' entries';
  if(document.getElementById('pnl-logs').classList.contains('on'))RL();
  if(s.trades)CT=s.trades;if(document.getElementById('pnl-trades').classList.contains('on'))RT();
  if(s.guardrails)document.getElementById('gl').innerHTML=Object.entries(s.guardrails).map(function(e){return '<div class="gi"><span class="gk">'+e[0]+'</span><span class="gv">'+e[1]+'</span></div>';}).join('');
}
function RL(){var f=LF?LG.filter(function(e){return e.l===LF;}):LG;document.getElementById('lb').innerHTML=f.slice(0,100).map(function(e){return '<div class="le"><span class="lt">'+e.t+'</span><span class="'+e.l+'">'+e.m+'</span></div>';}).join('');}
function FL(f){LF=f;['','TRADE','WARN','ERROR'].forEach(function(x){var el=document.getElementById(x?'f'+x[0].toLowerCase():'fa');if(el)el.classList.toggle('on',f===x);});RL();}
function RT(){var el=document.getElementById('tl');if(!CT.length){el.innerHTML='<div style="text-align:center;padding:40px;color:#bbb">No trades yet</div>';return;}el.innerHTML=CT.map(function(t){var open=t.exit==null;var sc=t.side==='long'?'tl g':'tl r';var tag=t.reason==='synced'?'<span class="tag tag-y">synced</span>':t.reason==='manual'?'<span class="tag tag-m">manual</span>':'<span class="tag tag-b">bot</span>';return '<div class="ti"><div class="tt"><span class="'+sc+'">'+t.side.toUpperCase()+' '+t.lots+'L '+t.sym+tag+'</span><span class="tm">'+new Date(t.time).toLocaleTimeString()+'</span></div><div class="tp"><span>Entry $'+(t.entry||0).toLocaleString()+'</span>'+(open?'<span class="topen">Open...</span>':'<span>Exit $'+(t.exit||0).toLocaleString()+'</span>')+'</div>'+(open?'':'<div class="tpnl '+(t.won?'g':'r')+'">'+(t.won?'✅ Profit':'❌ Loss')+' $'+(t.pnl>0?'+':'')+(t.pnl||0).toFixed(4)+' ('+(t.pct>0?'+':'')+(t.pct||0).toFixed(2)+'%) <span style="font-size:10px;color:#bbb">'+t.reason+'</span></div>')+'</div>';}).join('');}
function closeAll(){if(!confirm('Close ALL open positions?'))return;A('/api/close_all',{}).then(function(r){alert('Closed: '+(r?r.closed:0));});}
function MT(d){var lots=parseInt(document.getElementById('ml').value)||1;A('/api/manual_trade',{direction:d,lots:lots}).then(function(r){if(r&&r.success)alert(d.toUpperCase()+' '+lots+'L placed
Entry: $'+r.entry+'
Stop: $'+r.stop+'
TP: $'+r.tp);else alert('Failed: '+(r?r.message:'check logs'));});}
function CON(){var k=document.getElementById('ak').value.trim();var s=document.getElementById('as').value.trim();if(!k||!s){document.getElementById('cm').innerHTML='<span style="color:#e74c3c">Enter key and secret</span>';return;}document.getElementById('cm').textContent='Connecting...';A('/api/connect',{api_key:k,api_secret:s}).then(function(r){document.getElementById('cm').innerHTML=r&&r.success?'<span style="color:#00b386">✓ Connected — $'+(r.balance||0).toFixed(2)+'</span>':'<span style="color:#e74c3c">✗ '+(r?r.message:'Failed')+'<br><small style="color:#999">Debug: /api/debug/auth</small></span>';});}
function LIP(){A('/api/ip').then(function(r){document.getElementById('ip').textContent=r&&r.ip?r.ip:'unknown';});}
function poll(){A('/api/status').then(function(s){if(s)R(s);}).catch(function(){});}
LIP();poll();setInterval(poll,4000);setInterval(LIP,60000);
</script>
</body>
</html>"""

@app.route("/")
def index(): return Response(DASHBOARD, mimetype="text/html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=False)