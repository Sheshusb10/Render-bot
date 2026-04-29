"""
ALPHA BOT — Delta Exchange India
Single clean file. No patches. No legacy code.
"""
import os, time, hmac, hashlib, json, logging, math, threading, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alpha")

# ── CONFIG ────────────────────────────────────────────────────────────────────
class C:
    BASE        = "https://api.india.delta.exchange"
    KEY         = os.getenv("DELTA_API_KEY","").strip()
    SECRET      = os.getenv("DELTA_API_SECRET","").strip()
    PID         = 27          # BTCUSD perpetual product_id
    LOT_BTC     = 0.001       # 1 lot = 0.001 BTC
    LEVERAGE    = 5
    SCAN_SECS   = 300
    MIN_CONF    = 58
    STOP_PCT    = 0.025       # 2.5% hard stop (placed as real order on Delta)
    TP_PCT      = 0.030       # 3.0% take profit
    RISK_PCT    = 0.015       # 1.5% capital per trade
    MAX_HALT    = 0.08        # Halt bot if down 8% from start
    DAY_PAUSE   = 0.03        # Pause today if down 3%
    STATE_FILE  = "/tmp/alpha_state.json"

# ── DELTA API ─────────────────────────────────────────────────────────────────
class API:
    def __init__(self):
        self.key    = C.KEY
        self.secret = C.SECRET
        self.sess   = requests.Session()

    def set_creds(self, key, secret):
        self.key = key.strip()
        self.secret = secret.strip()

    def _sign(self, method, path, qs="", body=""):
        ts  = str(int(time.time()))
        sig = hmac.new(self.secret.encode(),
                       (method+ts+path+qs+body).encode(),
                       hashlib.sha256).hexdigest()
        return {"api-key":self.key,"timestamp":ts,"signature":sig,
                "Content-Type":"application/json"}

    def get(self, path, params=None):
        qs = ("?"+"&".join(f"{k}={v}" for k,v in params.items())) if params else ""
        try:
            r = self.sess.get(f"{C.BASE}{path}{qs}",
                              headers=self._sign("GET",path,qs), timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"GET {path}: {e}")
            return None

    def post(self, path, body):
        s = json.dumps(body)
        try:
            r = self.sess.post(f"{C.BASE}{path}",
                               headers=self._sign("POST",path,"",s),
                               data=s, timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"POST {path}: {e}")
            return {}

    def price(self):
        try:
            r = self.sess.get(f"{C.BASE}/v2/tickers/BTCUSD", timeout=6)
            d = r.json()
            return float(d.get("result",{}).get("mark_price",0) or 0)
        except Exception:
            return 0.0

    def balance(self):
        d = self.get("/v2/wallet/balances")
        if d and d.get("success"):
            for b in d.get("result",[]):
                if str(b.get("asset_symbol","")).upper() in ("USD","USDT"):
                    v = float(b.get("available_balance",0) or 0)
                    if v > 0: return v
            ne = float((d.get("meta") or {}).get("net_equity",0) or 0)
            if ne > 0: return ne
        return 0.0

    def candles(self, res="5m", n=100):
        end = int(time.time())
        mins = {"5m":5,"15m":15,"1h":60}.get(res,5)
        start = end - mins*60*n
        d = self.get("/v2/history/candles",
                     {"symbol":"BTCUSD","resolution":res,"start":start,"end":end})
        return d.get("result",[]) if d and d.get("success") else []

    def positions(self):
        d = self.get("/v2/positions/margined")
        if d and d.get("success"):
            return [p for p in d.get("result",[])
                    if abs(float(p.get("size",0) or 0))>0]
        return []

    def order(self, side, lots):
        return self.post("/v2/orders",{
            "product_id":C.PID,"size":lots,"side":side,
            "order_type":"market_order","time_in_force":"ioc"})

    def stop_order(self, side, lots, stop_price, tp_price):
        """Real stop+TP order on Delta — survives bot restarts."""
        return self.post("/v2/orders",{
            "product_id":C.PID,"size":lots,"side":side,
            "order_type":"stop_market_order",
            "stop_price":str(round(stop_price,1)),
            "bracket_stop_loss_price":str(round(stop_price,1)),
            "bracket_take_profit_price":str(round(tp_price,1)),
            "time_in_force":"gtc",
            "stop_trigger_method":"mark_price"})

    def close_all(self):
        closed = 0
        for p in self.positions():
            sz = float(p.get("size",0) or 0)
            q  = abs(int(sz))
            if q:
                self.post("/v2/orders",{
                    "product_id":p.get("product_id",C.PID),
                    "size":q,"side":"sell" if sz>0 else "buy",
                    "order_type":"market_order","time_in_force":"ioc"})
                closed += 1
        return closed

# ── INDICATORS ────────────────────────────────────────────────────────────────
def parse(raw):
    cl,hi,lo,vo=[],[],[],[]
    for c in raw:
        try:
            cv=float(c.get("close",0) or 0)
            if cv>0:
                cl.append(cv)
                hi.append(float(c.get("high",cv) or cv))
                lo.append(float(c.get("low",cv) or cv))
                vo.append(float(c.get("volume",0) or 0))
        except Exception:
            continue
    return cl,hi,lo,vo

def ema(p,n):
    if len(p)<n: return [p[-1]]*len(p) if p else []
    k=2/(n+1); v=[sum(p[:n])/n]
    for x in p[n:]: v.append(x*k+v[-1]*(1-k))
    return [v[0]]*(n-1)+v

def rsi(p,n=14):
    if len(p)<n+2: return 50.0
    d=[p[i]-p[i-1] for i in range(1,len(p))]
    g=sum(max(x,0) for x in d[-n:])/n
    l=sum(abs(min(x,0)) for x in d[-n:])/n
    return 100.0 if l<1e-10 else round(100-100/(1+g/l),2)

def adx_calc(hi,lo,cl,n=14):
    if len(cl)<n*2+1: return 0.0,0.0,0.0
    tr,pm,nm=[],[],[]
    for i in range(1,len(cl)):
        tr.append(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])))
        u=hi[i]-hi[i-1]; d=lo[i-1]-lo[i]
        pm.append(u if u>d and u>0 else 0.0)
        nm.append(d if d>u and d>0 else 0.0)
    def ws(a):
        s=sum(a[:n]); r=[s]
        for v in a[n:]: s=s-s/n+v; r.append(s)
        return r
    at=ws(tr); pd=ws(pm); nd=ws(nm)
    pi=[100*pd[i]/at[i] if at[i]>0 else 0 for i in range(len(at))]
    ni=[100*nd[i]/at[i] if at[i]>0 else 0 for i in range(len(at))]
    dx=[abs(pi[i]-ni[i])/(pi[i]+ni[i])*100 if pi[i]+ni[i]>0 else 0
        for i in range(len(pi))]
    return round(sum(dx[-n:])/n,2), round(pi[-1],2), round(ni[-1],2)

def atr_val(hi,lo,cl,n=14):
    if len(cl)<n+1: return 0.0
    t=[max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1]))
       for i in range(1,len(cl))]
    return sum(t[-n:])/n

def score(cl,hi,lo,vo,cl15,hour,dirn):
    """Score a trade signal 0-100. Returns (score, veto_reason)."""
    if len(cl)<55: return 0,"need_55_candles"
    if hour in [2,3,4,5]: return 0,"dead_zone"
    # Volume trap (use completed candle, not forming)
    if len(vo)>=21:
        avg=sum(vo[-21:-1])/20
        if vo[-2]<avg*0.10: return 0,"low_volume"
    
    adx_v,pdi,ndi = adx_calc(hi,lo,cl)
    rsi_v = rsi(cl)
    e8  = ema(cl,8)[-1]; e21=ema(cl,21)[-1]; e55=ema(cl,55)[-1]
    p   = cl[-1]
    
    bull = p>e8>e21>e55 and adx_v>20 and pdi>ndi
    bear = p<e8<e21<e55 and adx_v>20 and ndi>pdi
    
    s = 0
    # Regime 40pts
    if dirn=="long"  and bull: s+=40
    elif dirn=="short" and bear: s+=40
    elif adx_v>15: s+=15
    else: s+=5
    
    # RSI 25pts
    if dirn=="long":
        if 35<=rsi_v<=55: s+=25
        elif rsi_v<35:    s+=20
        elif rsi_v<=65:   s+=10
    else:
        if 45<=rsi_v<=65: s+=25
        elif rsi_v>65:    s+=20
        elif rsi_v>=35:   s+=10
    
    # 15m alignment 20pts
    if len(cl15)>=21:
        e8_=ema(cl15,8)[-1]; e21_=ema(cl15,21)[-1]
        if dirn=="long"  and cl15[-1]>e8_>e21_: s+=20
        elif dirn=="short" and cl15[-1]<e8_<e21_: s+=20
        else: s+=5
    else: s+=10
    
    # ADX strength 15pts
    if adx_v>30: s+=15
    elif adx_v>22: s+=10
    elif adx_v>15: s+=5
    
    return min(s,100), ""

# ── BOT ───────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self):
        self.api       = API()
        self.running   = False
        self.connected = False
        self.capital   = 0.0
        self.start_cap = 0.0
        self.day_start = 0.0
        self.halted    = False
        self.halt_msg  = ""
        self.status    = "Not connected"
        self.logs      = []
        self.trades    = []
        self.scan_n    = 0
        self.next_scan = None
        # Live data
        self.btc_price = 0.0
        self.regime    = "UNKNOWN"
        self.rsi_now   = 50.0
        self.adx_now   = 0.0
        self.atr_pct   = 0.0
        self.long_sc   = 0
        self.short_sc  = 0
        self.long_veto = ""
        self.short_veto= ""
        self.total_tr  = 0
        self.wins      = 0

    def log(self, level, msg):
        e = {"t":datetime.now(timezone.utc).strftime("%H:%M:%S"),
             "l":level,"m":msg}
        self.logs.append(e)
        if len(self.logs)>300: self.logs.pop(0)
        getattr(log,{"INFO":"info","WARN":"warning",
                     "ERROR":"error","TRADE":"info"}.get(level,"info"))(msg)

    def _save(self):
        try:
            json.dump({"start_cap":self.start_cap,"day_start":self.day_start,
                       "halted":self.halted,"halt_msg":self.halt_msg,
                       "total_tr":self.total_tr,"wins":self.wins,
                       "trades":self.trades[-50:]},
                      open(C.STATE_FILE,"w"))
        except Exception: pass

    def _load(self):
        try:
            if not os.path.exists(C.STATE_FILE): return False
            s=json.load(open(C.STATE_FILE))
            self.start_cap = float(s.get("start_cap",0))
            self.day_start = float(s.get("day_start",0))
            self.halted    = bool(s.get("halted",False))
            self.halt_msg  = s.get("halt_msg","")
            self.total_tr  = int(s.get("total_tr",0))
            self.wins      = int(s.get("wins",0))
            self.trades    = s.get("trades",[])
            if self.start_cap>0:
                self.log("INFO",f"Restored: start=${self.start_cap:.2f} "
                         f"trades={self.total_tr} halted={self.halted}")
                return True
        except Exception: pass
        return False

    def connect(self, key, secret):
        self.api.set_creds(key, secret)
        bal = self.api.balance()
        if bal<=0:
            return {"success":False,
                    "message":"Failed — check API key, secret and IP whitelist"}
        self.capital   = bal
        self.connected = True
        if not self._load() or self.start_cap<=0:
            self.start_cap = bal
            self.day_start = bal
            self._save()
        self.log("INFO",f"Connected ✓ | Balance ${bal:.2f} | "
                 f"Start ${self.start_cap:.2f} | "
                 f"Loss ceiling ${self.start_cap*(1-C.MAX_HALT):.2f}")
        if not self.running: self.start()
        return {"success":True,"balance":round(bal,2)}

    def _sync_wallet(self):
        bal = self.api.balance()
        if bal<=0: return
        self.capital = bal
        if self.start_cap>0:
            loss = (self.start_cap-bal)/self.start_cap
            if loss >= C.MAX_HALT and not self.halted:
                self.halted   = True
                self.halt_msg = (f"Down {loss*100:.1f}% — floor hit "
                                 f"${self.start_cap:.2f}→${bal:.2f}")
                self.log("ERROR",f"🛑 HALTED: {self.halt_msg}")
                self._save()
        self.log("INFO",f"Wallet ${bal:.2f} | "
                 f"{'🛑 HALTED' if self.halted else '✅ OK'}")

    def _real_positions(self):
        """Always read from Delta. Never trust in-memory state."""
        return self.api.positions()

    def _pos_display(self, raw):
        out=[]
        for p in raw:
            sz    = float(p.get("size",0) or 0)
            entry = float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            mark  = float(p.get("mark_price") or self.btc_price or entry)
            upnl  = float(p.get("unrealized_pnl") or 0)
            if sz==0 or entry==0: continue
            side  = "long" if sz>0 else "short"
            pct   = ((mark-entry)/entry if side=="long" else (entry-mark)/entry)*100
            stop  = round(entry*(1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT),1)
            tp    = round(entry*(1+C.TP_PCT   if side=="long" else 1-C.TP_PCT),1)
            out.append({"symbol":p.get("product_symbol","BTCUSD"),
                        "side":side,"lots":abs(sz),"entry":round(entry,1),
                        "mark":round(mark,1),"upnl":round(upnl,3),
                        "pct":round(pct,2),"stop":stop,"tp":tp})
        return out

    def _check_exits(self, positions):
        """Software stop+TP as backup to bracket orders."""
        if not self.btc_price: return
        for p in positions:
            sz    = float(p.get("size",0) or 0)
            entry = float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            side = "long" if sz>0 else "short"
            pct  = (self.btc_price-entry)/entry if side=="long" else (entry-self.btc_price)/entry
            lots = abs(int(sz))
            pid  = p.get("product_id",C.PID)

            if pct <= -C.STOP_PCT:
                r = self.api.post("/v2/orders",{
                    "product_id":pid,"size":lots,
                    "side":"sell" if side=="long" else "buy",
                    "order_type":"market_order","time_in_force":"ioc"})
                if r.get("success"):
                    pnl = entry*lots*C.LOT_BTC*pct
                    self.log("TRADE",
                        f"❌ STOP | {side.upper()} {lots}L "
                        f"${entry:.0f}→${self.btc_price:.0f} "
                        f"P&L ${pnl:+.3f} ({pct*100:.2f}%)")
                    self._close_trade(side,entry,self.btc_price,lots,pct,"stop")
            elif pct >= C.TP_PCT:
                r = self.api.post("/v2/orders",{
                    "product_id":pid,"size":lots,
                    "side":"sell" if side=="long" else "buy",
                    "order_type":"market_order","time_in_force":"ioc"})
                if r.get("success"):
                    pnl = entry*lots*C.LOT_BTC*pct
                    self.log("TRADE",
                        f"✅ TP HIT | {side.upper()} {lots}L "
                        f"${entry:.0f}→${self.btc_price:.0f} "
                        f"P&L ${pnl:+.3f} ({pct*100:.2f}%)")
                    self._close_trade(side,entry,self.btc_price,lots,pct,"tp")

    def _close_trade(self, side, entry, exit_p, lots, pct, reason):
        won = pct>0
        if won: self.wins+=1
        self.trades.append({
            "time":datetime.now(timezone.utc).isoformat(),
            "side":side,"entry":round(entry,1),"exit":round(exit_p,1),
            "lots":lots,"pnl":round(entry*lots*C.LOT_BTC*pct,4),
            "pct":round(pct*100,2),"reason":reason,"won":won})
        self._save()

    def scan(self):
        self.scan_n += 1
        now = datetime.now(timezone.utc)
        self.next_scan=(now+timedelta(seconds=C.SCAN_SECS)).isoformat()

        # Price
        p = self.api.price()
        if p>0: self.btc_price = p

        # Wallet sync every 5 scans
        if self.scan_n%5==0: self._sync_wallet()

        if self.halted:
            self.status=f"🛑 HALTED: {self.halt_msg}"
            return

        # Candles
        raw5  = self.api.candles("5m", 100)
        raw15 = self.api.candles("15m", 60)
        cl,hi,lo,vo = parse(raw5)
        cl15,*_ = parse(raw15)

        if len(cl)<55:
            self.status=f"⚠ {len(cl)} candles (need 55)"
            self.log("WARN",f"Only {len(cl)} 5m candles returned")
            return

        price = cl[-1]
        self.btc_price = price
        self.rsi_now   = rsi(cl)
        self.adx_now, pdi, ndi = adx_calc(hi,lo,cl)
        atr_v          = atr_val(hi,lo,cl)
        self.atr_pct   = round(atr_v/price*100,3) if price>0 else 0

        # Regime
        e8=ema(cl,8)[-1]; e21=ema(cl,21)[-1]; e55=ema(cl,55)[-1]
        if   price>e8>e21>e55 and self.adx_now>25 and pdi>ndi: self.regime="STRONG_BULL"
        elif price>e8>e21 and self.adx_now>18:                  self.regime="BULL"
        elif price<e8<e21<e55 and self.adx_now>25 and ndi>pdi: self.regime="STRONG_BEAR"
        elif price<e8<e21 and self.adx_now>18:                  self.regime="BEAR"
        else:                                                    self.regime="NEUTRAL"

        # Real positions from Delta
        real = self._real_positions()
        self._check_exits(real)

        # Max 1 position — check real count
        if len(real)>=1:
            d = self._pos_display(real)
            x = d[0] if d else {}
            self.status=(f"Holding {x.get('side','?').upper()} "
                         f"{x.get('lots',0)}L @ ${x.get('entry',0):.0f} | "
                         f"UPL ${x.get('upnl',0):+.3f} ({x.get('pct',0):+.2f}%) | "
                         f"Stop ${x.get('stop',0):.0f} TP ${x.get('tp',0):.0f}")
            self.log("INFO",self.status)
            return

        # Daily pause check
        if self.day_start>0 and (self.capital-self.day_start)/self.day_start<=-C.DAY_PAUSE:
            self.status="⏸ Daily -3% limit reached — paused today"
            return

        # Score both directions
        ls,lv = score(cl,hi,lo,vo,cl15,now.hour,"long")
        ss,sv = score(cl,hi,lo,vo,cl15,now.hour,"short")
        self.long_sc=ls; self.short_sc=ss
        self.long_veto=lv; self.short_veto=sv

        self.log("INFO",
            f"#{self.scan_n} ${price:,.0f} | {self.regime} | "
            f"RSI={self.rsi_now:.1f} ADX={self.adx_now:.1f} | "
            f"L={ls}{'✗'+lv if lv else '✓'} "
            f"S={ss}{'✗'+sv if sv else '✓'}")

        dirn = sc = None
        if not lv and ls>=C.MIN_CONF and ls>ss: dirn,sc="long",ls
        elif not sv and ss>=C.MIN_CONF and ss>ls: dirn,sc="short",ss

        if not dirn:
            why=(lv or sv or f"score {max(ls,ss)}<{C.MIN_CONF}")
            self.status=f"Watching — {why} | {self.regime}"
            return

        # Sizing
        m_per_lot = price*C.LOT_BTC/C.LEVERAGE
        risk_usd  = max(self.capital*C.RISK_PCT, m_per_lot)
        lots      = max(1, int(risk_usd/m_per_lot))
        lots      = min(lots, max(1,int(self.capital*0.10/m_per_lot)))

        side = "buy" if dirn=="long" else "sell"
        self.log("INFO",
            f"→ Placing {side.upper()} {lots}L @ ${price:,.0f} | "
            f"score={sc} margin=${m_per_lot*lots:.2f}")

        r = self.api.order(side, lots)
        if not r.get("success"):
            err=r.get("error",r.get("message",str(r)[:60]))
            self.status=f"❌ Order failed: {err}"
            self.log("ERROR",f"Order FAILED: {err}")
            return

        # Record trade
        self.status=f"✅ {dirn.upper()} {lots}L @ ${price:,.0f} (score={sc})"
        self.log("TRADE",self.status)
        self.total_tr+=1
        self.trades.append({
            "time":now.isoformat(),"side":dirn,
            "entry":round(price,1),"exit":None,
            "lots":lots,"pnl":None,"pct":None,
            "reason":"open","won":None,"score":sc})
        if len(self.trades)>100: self.trades.pop(0)

        # Place bracket stop on Delta — survives restarts
        stop_p = price*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp_p   = price*(1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
        cs     = "sell" if dirn=="long" else "buy"
        sr = self.api.stop_order(cs, lots, stop_p, tp_p)
        if sr.get("success"):
            self.log("INFO",
                f"🛑 Bracket placed | stop ${stop_p:.1f} | TP ${tp_p:.1f}")
        else:
            self.log("WARN",
                f"⚠ Bracket FAILED — SET MANUAL STOP ${stop_p:.1f} NOW | "
                f"err={sr.get('error','?')}")
        self._save()

    def start(self):
        if not self.running:
            self.running=True
            threading.Thread(target=self._loop,daemon=True).start()
            self.log("INFO","▶ Bot started")

    def stop(self):
        self.running=False
        self.log("INFO","■ Bot stopped")

    def _loop(self):
        while self.running:
            try: self.scan()
            except Exception as e:
                log.error(f"Loop error: {e}",exc_info=True)
                self.status=f"Error: {e}"
            time.sleep(C.SCAN_SECS)

    def state(self):
        sc    = self.start_cap or self.capital
        real  = self._real_positions()
        pnl_p = (self.capital-sc)/sc*100 if sc>0 else 0
        done  = [t for t in self.trades if t.get("won") is not None]
        wr    = sum(1 for t in done if t["won"])/len(done)*100 if done else 0
        return {
            "running":    self.running,
            "connected":  self.connected,
            "halted":     self.halted,
            "halt_msg":   self.halt_msg,
            "status":     self.status,
            "price":      round(self.btc_price,1),
            "regime":     self.regime,
            "rsi":        self.rsi_now,
            "adx":        self.adx_now,
            "atr_pct":    self.atr_pct,
            "long_score": self.long_sc,
            "short_score":self.short_sc,
            "long_veto":  self.long_veto,
            "short_veto": self.short_veto,
            "capital":    round(self.capital,2),
            "start_cap":  round(sc,2),
            "pnl_pct":    round(pnl_p,2),
            "win_rate":   round(wr,1),
            "total_trades":self.total_tr,
            "wins":       self.wins,
            "next_scan":  self.next_scan,
            "scan_count": self.scan_n,
            "logs":       self.logs[-60:],
            "trades":     self.trades[-30:],
            "open_positions": self._pos_display(real),
            "guardrails": {
                "stop": f"{C.STOP_PCT*100:.1f}% per trade (bracket order on Delta)",
                "tp":   f"{C.TP_PCT*100:.1f}% take profit",
                "monthly_halt": f"Halt if down {C.MAX_HALT*100:.0f}% (equity check)",
                "daily_pause":  f"Pause if down {C.DAY_PAUSE*100:.0f}% today",
                "max_positions":"1 (checked from Delta API, not memory)",
                "position_check":"Real API query before every trade",
            },
        }

# ── FLASK ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
bot = Bot()

if C.KEY and C.SECRET:
    threading.Thread(target=lambda: bot.connect(C.KEY,C.SECRET),daemon=True).start()

@app.after_request
def cors_h(r):
    r.headers.update({"Access-Control-Allow-Origin":"*",
                      "Access-Control-Allow-Methods":"GET,POST,OPTIONS",
                      "Access-Control-Allow-Headers":"Content-Type"})
    return r

@app.route("/api/status")
@app.route("/api/bot/status")
def api_status(): return jsonify(bot.state())

@app.route("/api/connect", methods=["POST","OPTIONS"])
def api_connect():
    if request.method=="OPTIONS": return jsonify({})
    d=request.json or {}
    k=d.get("api_key",""); s=d.get("api_secret","")
    if not k or not s:
        return jsonify({"success":False,"message":"api_key and api_secret required"})
    return jsonify(bot.connect(k,s))

@app.route("/api/bot/start",  methods=["POST"]) 
def api_start():  bot.start(); return jsonify({"success":True})

@app.route("/api/bot/stop",   methods=["POST"]) 
def api_stop():   bot.stop();  return jsonify({"success":True})

@app.route("/api/bot/run_now",methods=["POST"])
def api_run_now():
    threading.Thread(target=bot.scan,daemon=True).start()
    return jsonify({"success":True})

@app.route("/api/trades")
def api_trades(): return jsonify(bot.trades[-50:])

@app.route("/api/logs")
def api_logs():   return jsonify(bot.logs)

@app.route("/api/positions")
def api_positions():
    raw=bot.api.positions()
    return jsonify({"raw":raw,"display":bot._pos_display(raw)})

@app.route("/api/ticker")
def api_ticker():
    p=bot.api.price()
    if not p:
        try:
            r=requests.get("https://api.coingecko.com/api/v3/simple/price"
                           "?ids=bitcoin&vs_currencies=usd",timeout=5)
            p=r.json().get("bitcoin",{}).get("usd",0)
        except Exception: pass
    return jsonify({"mark_price":str(p),"price":p})

@app.route("/api/ip")
def api_ip():
    try:
        ip=requests.get("https://api.ipify.org?format=json",timeout=5).json().get("ip","?")
    except Exception: ip="unknown"
    return jsonify({"ip":ip,"whitelist_this_on_delta":ip})

@app.route("/api/close_all", methods=["POST"])
def api_close_all():
    n=bot.api.close_all()
    bot.log("TRADE",f"🔴 Emergency close: {n} position(s)")
    return jsonify({"success":True,"closed":n})

@app.route("/api/manual_trade", methods=["POST"])
def api_manual():
    d=request.json or {}
    dirn=d.get("direction","")
    if dirn not in ("long","short"):
        return jsonify({"success":False,"message":"direction: long or short"})
    p=bot.btc_price or bot.api.price()
    lots=max(1,int(d.get("lots",1)))
    side="buy" if dirn=="long" else "sell"
    r=bot.api.order(side,lots)
    if r.get("success"):
        stop=p*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp  =p*(1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
        cs  ="sell" if dirn=="long" else "buy"
        bot.api.stop_order(cs,lots,stop,tp)
        bot.log("TRADE",f"MANUAL {dirn.upper()} {lots}L @ ${p:,.0f} "
                f"stop=${stop:.0f} tp=${tp:.0f}")
        bot.trades.append({"time":datetime.now(timezone.utc).isoformat(),
                           "side":dirn,"entry":round(p,1),"exit":None,
                           "lots":lots,"pnl":None,"pct":None,
                           "reason":"manual","won":None})
        return jsonify({"success":True,"entry":round(p,1),
                        "stop":round(stop,1),"tp":round(tp,1)})
    return jsonify({"success":False,"message":r.get("error","failed")})

@app.route("/api/set_stop", methods=["POST"])
def api_set_stop():
    d=request.json or {}
    dirn =d.get("direction","long")
    entry=float(d.get("entry",bot.btc_price or 77000))
    lots =int(d.get("lots",1))
    stop =entry*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
    tp   =entry*(1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
    cs   ="sell" if dirn=="long" else "buy"
    r=bot.api.stop_order(cs,lots,stop,tp)
    ok=r.get("success",False)
    bot.log("INFO" if ok else "WARN",
        f"{'✅' if ok else '❌'} Stop set: {dirn.upper()} "
        f"entry=${entry:.0f} stop=${stop:.0f} tp=${tp:.0f}")
    return jsonify({"success":ok,"stop":round(stop,1),"tp":round(tp,1)})

@app.route("/api/debug/candles")
def api_debug_candles():
    for res in ["5m","1m","15m"]:
        d=bot.api.get("/v2/history/candles",
                      {"symbol":"BTCUSD","resolution":res,
                       "start":int(time.time())-3600,
                       "end":int(time.time())})
        if d and d.get("success") and d.get("result"):
            return jsonify({"ok":True,"res":res,
                            "count":len(d["result"]),
                            "sample":d["result"][0]})
    return jsonify({"ok":False,"error":"all resolutions failed"})

@app.route("/api/debug/positions")
def api_debug_pos():
    return jsonify(bot.api.get("/v2/positions/margined") or {"error":"no response"})



DASHBOARD = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>ALPHA BOT</title>\n<style>\n*{box-sizing:border-box;margin:0;padding:0}\n:root{--bg:#0a0a0f;--c1:#111118;--c2:#16161f;--acc:#00e5ff;--g:#00e676;--r:#ff1744;--y:#ffea00;--t:#e0e0e0;--t2:#888;--br:1px solid #222}\nbody{background:var(--bg);color:var(--t);font-family:\'Courier New\',monospace;font-size:13px;min-height:100vh}\n.header{background:var(--c1);border-bottom:var(--br);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:99}\n.logo{color:var(--acc);font-size:18px;font-weight:700;letter-spacing:2px}\n.live-dot{width:8px;height:8px;border-radius:50%;background:var(--g);display:inline-block;margin-right:6px;animation:pulse 2s infinite}\n@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}\n.tabs{display:flex;gap:0;border-bottom:var(--br);background:var(--c1)}\n.tab{flex:1;padding:10px;text-align:center;cursor:pointer;color:var(--t2);border-bottom:2px solid transparent;font-size:11px;text-transform:uppercase;letter-spacing:1px}\n.tab.active{color:var(--acc);border-bottom-color:var(--acc)}\n.panel{display:none;padding:12px}\n.panel.active{display:block}\n.card{background:var(--c1);border:var(--br);border-radius:8px;padding:12px;margin-bottom:10px}\n.card-title{color:var(--t2);font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}\n.big-price{font-size:36px;font-weight:700;color:var(--t);letter-spacing:-1px}\n.regime-badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;margin-top:4px}\n.regime-BULL,.regime-STRONG_BULL{background:#00e6763d;color:var(--g)}\n.regime-BEAR,.regime-STRONG_BEAR{background:#ff17443d;color:var(--r)}\n.regime-NEUTRAL,.regime-UNKNOWN{background:#ffffff15;color:var(--t2)}\n.scores{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}\n.score-box{background:var(--c2);border-radius:6px;padding:10px;text-align:center}\n.score-label{font-size:9px;color:var(--t2);margin-bottom:4px;text-transform:uppercase}\n.score-val{font-size:22px;font-weight:700}\n.score-val.green{color:var(--g)}\n.score-val.red{color:var(--r)}\n.score-val.wait{color:var(--y)}\n.inds{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}\n.ind{background:var(--c2);border-radius:6px;padding:8px;text-align:center}\n.ind-label{font-size:9px;color:var(--t2);margin-bottom:2px}\n.ind-val{font-size:16px;font-weight:700;color:var(--acc)}\n.wallet-row{display:flex;justify-content:space-between;align-items:baseline}\n.wallet-big{font-size:28px;font-weight:700}\n.pnl-pos{color:var(--g)}\n.pnl-neg{color:var(--r)}\n.status-bar{background:var(--c2);border-radius:6px;padding:8px 10px;font-size:11px;color:var(--t2);margin-bottom:10px;min-height:28px}\n.timer{color:var(--acc);font-size:11px}\n.pos-card{background:#00e67614;border:1px solid #00e67640;border-radius:8px;padding:10px;margin-bottom:8px}\n.pos-card.short{background:#ff174414;border-color:#ff174440}\n.pos-row{display:flex;justify-content:space-between;margin-bottom:4px;font-size:12px}\n.pos-tag{font-size:10px;padding:2px 8px;border-radius:4px;font-weight:700}\n.pos-tag.long{background:var(--g);color:#000}\n.pos-tag.short{background:var(--r);color:#fff}\n.guard-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:var(--br);font-size:11px}\n.guard-row:last-child{border-bottom:none}\n.guard-label{color:var(--t2)}\n.guard-val{color:var(--acc);text-align:right;max-width:60%}\n.log-box{background:#000;border-radius:6px;padding:8px;max-height:300px;overflow-y:auto;font-size:11px}\n.log-entry{padding:2px 0;border-bottom:1px solid #111;display:flex;gap:8px}\n.log-t{color:var(--t2);white-space:nowrap}\n.log-INFO{color:#888}\n.log-WARN{color:var(--y)}\n.log-ERROR{color:var(--r)}\n.log-TRADE{color:var(--g);font-weight:700}\n.trade-row{background:var(--c2);border-radius:6px;padding:8px;margin-bottom:6px;font-size:11px}\n.trade-header{display:flex;justify-content:space-between;margin-bottom:4px}\n.trade-open{color:var(--y);font-style:italic}\n.btn{padding:10px 16px;border-radius:6px;border:none;cursor:pointer;font-family:inherit;font-size:12px;font-weight:700;letter-spacing:1px}\n.btn-start{background:var(--g);color:#000;width:100%}\n.btn-stop{background:var(--r);color:#fff;width:100%}\n.btn-scan{background:var(--acc);color:#000;width:100%}\n.btn-close{background:#ff5722;color:#fff;width:100%;margin-top:8px}\n.btn-manual-l{background:#00e67630;color:var(--g);border:1px solid var(--g);flex:1}\n.btn-manual-s{background:#ff174430;color:var(--r);border:1px solid var(--r);flex:1}\n.input-field{width:100%;background:var(--c2);border:var(--br);border-radius:6px;padding:10px;color:var(--t);font-family:inherit;font-size:13px;margin-bottom:8px}\n.input-field:focus{outline:none;border-color:var(--acc)}\n.conn-status{font-size:11px;text-align:center;margin-top:6px}\n.conn-ok{color:var(--g)}\n.conn-err{color:var(--r)}\n.halted-banner{background:#ff174430;border:1px solid var(--r);border-radius:8px;padding:12px;margin-bottom:10px;text-align:center;color:var(--r);font-weight:700}\n.progress-bar{height:4px;background:#222;border-radius:2px;overflow:hidden;margin-top:6px}\n.progress-fill{height:100%;background:var(--acc);border-radius:2px;transition:width .3s}\n.countdown{font-size:11px;color:var(--t2);margin-top:4px}\n.manual-row{display:flex;gap:8px;margin-top:8px}\n</style>\n</head>\n<body>\n<div class="header">\n  <div class="logo">Δ ALPHA BOT</div>\n  <div>\n    <span class="live-dot" id="liveDot"></span>\n    <span id="connLabel" style="font-size:11px;color:var(--t2)">Not connected</span>\n  </div>\n</div>\n\n<div class="tabs">\n  <div class="tab active" onclick="tab(\'home\')">Home</div>\n  <div class="tab" onclick="tab(\'trades\')">Trades</div>\n  <div class="tab" onclick="tab(\'logs\')">Logs</div>\n  <div class="tab" onclick="tab(\'settings\')">Settings</div>\n</div>\n\n<!-- HOME -->\n<div id="p-home" class="panel active">\n  <div id="haltBanner" class="halted-banner" style="display:none"></div>\n\n  <div class="card">\n    <div class="card-title">Bitcoin · Live</div>\n    <div class="big-price" id="btcPrice">—</div>\n    <span class="regime-badge" id="regimeBadge">LOADING</span>\n  </div>\n\n  <div class="card">\n    <div class="status-bar" id="statusBar">Initializing...</div>\n    <div class="scores">\n      <div class="score-box">\n        <div class="score-label">↑ Long</div>\n        <div class="score-val green" id="longScore">—</div>\n        <div style="font-size:9px;color:var(--t2)" id="longVeto"></div>\n      </div>\n      <div class="score-box">\n        <div class="score-label">↓ Short</div>\n        <div class="score-val red" id="shortScore">—</div>\n        <div style="font-size:9px;color:var(--t2)" id="shortVeto"></div>\n      </div>\n      <div class="score-box">\n        <div class="score-label">⚡ Decision</div>\n        <div class="score-val wait" id="decision">WAIT</div>\n        <div style="font-size:9px;color:var(--t2)" id="decReason"></div>\n      </div>\n    </div>\n    <div class="inds">\n      <div class="ind"><div class="ind-label">RSI (14)</div><div class="ind-val" id="rsiVal">—</div></div>\n      <div class="ind"><div class="ind-label">ADX (14)</div><div class="ind-val" id="adxVal">—</div></div>\n      <div class="ind"><div class="ind-label">ATR %</div><div class="ind-val" id="atrVal">—</div></div>\n    </div>\n    <div class="progress-bar"><div class="progress-fill" id="scanBar" style="width:0%"></div></div>\n    <div class="countdown" id="scanCountdown">Next scan in —</div>\n  </div>\n\n  <!-- Open Positions -->\n  <div id="openPosSection"></div>\n\n  <div class="card">\n    <div class="card-title">Wallet Balance</div>\n    <div class="wallet-row">\n      <div class="wallet-big" id="walletBig">$—</div>\n      <div style="text-align:right">\n        <div id="pnlPct" style="font-size:16px;font-weight:700">+0.00%</div>\n        <div style="font-size:10px;color:var(--t2)">from start</div>\n      </div>\n    </div>\n    <div style="font-size:10px;color:var(--t2);margin-top:4px" id="startCapLine"></div>\n  </div>\n\n  <div class="card">\n    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;text-align:center">\n      <div><div style="font-size:10px;color:var(--t2)">Win Rate</div>\n           <div id="winRate" style="font-size:18px;font-weight:700;color:var(--g)">—</div></div>\n      <div><div style="font-size:10px;color:var(--t2)">Trades</div>\n           <div id="tradeCount" style="font-size:18px;font-weight:700">0</div></div>\n      <div><div style="font-size:10px;color:var(--t2)">Scan #</div>\n           <div id="scanCount" style="font-size:18px;font-weight:700;color:var(--acc)">0</div></div>\n    </div>\n  </div>\n\n  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">\n    <button class="btn btn-start" onclick="startBot()">▶ Start</button>\n    <button class="btn btn-stop" onclick="stopBot()">■ Stop</button>\n  </div>\n  <button class="btn btn-scan" onclick="scanNow()" style="margin-bottom:8px">⚡ Scan Now</button>\n  <button class="btn btn-close" onclick="closeAll()">⚠ Close All Positions</button>\n\n  <div class="card" style="margin-top:10px">\n    <div class="card-title">Manual Trade</div>\n    <input type="number" id="manualLots" class="input-field" placeholder="Lots (default: 1)" min="1">\n    <div class="manual-row">\n      <button class="btn btn-manual-l" onclick="manual(\'long\')">↑ BUY LONG</button>\n      <button class="btn btn-manual-s" onclick="manual(\'short\')">↓ SELL SHORT</button>\n    </div>\n  </div>\n</div>\n\n<!-- TRADES -->\n<div id="p-trades" class="panel">\n  <div class="card">\n    <div class="card-title">All Trades</div>\n    <div id="tradesList"><div style="color:var(--t2);text-align:center;padding:20px">No trades yet</div></div>\n  </div>\n</div>\n\n<!-- LOGS -->\n<div id="p-logs" class="panel">\n  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">\n    <div style="font-size:11px;color:var(--t2)" id="logCount">0 entries</div>\n    <div style="display:flex;gap:6px">\n      <button onclick="filterLogs(\'\')" style="padding:4px 8px;background:var(--c2);border:var(--br);border-radius:4px;color:var(--t);cursor:pointer;font-size:10px">All</button>\n      <button onclick="filterLogs(\'TRADE\')" style="padding:4px 8px;background:var(--c2);border:var(--br);border-radius:4px;color:var(--g);cursor:pointer;font-size:10px">Trades</button>\n      <button onclick="filterLogs(\'WARN\')" style="padding:4px 8px;background:var(--c2);border:var(--br);border-radius:4px;color:var(--y);cursor:pointer;font-size:10px">Warnings</button>\n      <button onclick="filterLogs(\'ERROR\')" style="padding:4px 8px;background:var(--c2);border:var(--br);border-radius:4px;color:var(--r);cursor:pointer;font-size:10px">Errors</button>\n    </div>\n  </div>\n  <div class="log-box" id="logBox"></div>\n</div>\n\n<!-- SETTINGS -->\n<div id="p-settings" class="panel">\n  <div class="card">\n    <div class="card-title">Delta Exchange Login</div>\n    <input type="text"     id="apiKey"    class="input-field" placeholder="API Key">\n    <input type="password" id="apiSecret" class="input-field" placeholder="API Secret">\n    <button class="btn btn-start" onclick="doConnect()">Connect to Delta Exchange</button>\n    <div class="conn-status" id="connMsg"></div>\n  </div>\n\n  <div class="card">\n    <div class="card-title">Server IP — Whitelist on Delta</div>\n    <div style="font-size:24px;font-weight:700;text-align:center;color:var(--acc);\n                font-family:\'Courier New\';padding:10px;background:var(--c2);\n                border-radius:6px;margin-bottom:8px" id="serverIp">Loading...</div>\n    <div style="font-size:10px;color:var(--t2);line-height:1.7">\n      1. Copy IP above<br>\n      2. Delta Exchange → Account → API Keys → Edit<br>\n      3. Paste into IP Whitelist → Save\n    </div>\n  </div>\n\n  <div class="card">\n    <div class="card-title">Guardrails — Active</div>\n    <div id="guardrailsList"></div>\n  </div>\n</div>\n\n<script>\nlet allLogs=[], logFilter=\'\';\n\nfunction tab(name){\n  document.querySelectorAll(\'.tab\').forEach((t,i)=>t.classList.toggle(\'active\',[\'home\',\'trades\',\'logs\',\'settings\'][i]===name));\n  document.querySelectorAll(\'.panel\').forEach(p=>p.classList.toggle(\'active\',p.id===\'p-\'+name));\n  if(name===\'logs\') renderLogs();\n}\n\nasync function api(path, body){\n  try{\n    const opt = body ? {method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(body)} : {};\n    const r = await fetch(path, opt);\n    return await r.json();\n  }catch(e){ return null; }\n}\n\nfunction fmt(n){ return typeof n===\'number\' ? n.toLocaleString(undefined,{maximumFractionDigits:2}) : n||\'—\'; }\n\nfunction refresh(s){\n  if(!s) return;\n\n  // Header\n  const conn = s.connected && !s.halted;\n  document.getElementById(\'liveDot\').style.background = conn ? \'var(--g)\' : \'var(--r)\';\n  document.getElementById(\'connLabel\').textContent = s.halted ? \'HALTED\' : s.connected ? \'Connected ✓\' : \'Not connected\';\n\n  // Halt banner\n  const hb = document.getElementById(\'haltBanner\');\n  hb.style.display = s.halted ? \'block\' : \'none\';\n  if(s.halted) hb.textContent = \'🛑 BOT HALTED: \'+s.halt_msg;\n\n  // Price + regime\n  document.getElementById(\'btcPrice\').textContent = s.price ? \'$\'+s.price.toLocaleString() : \'—\';\n  const rb = document.getElementById(\'regimeBadge\');\n  rb.textContent = s.regime||\'—\';\n  rb.className = \'regime-badge regime-\'+(s.regime||\'NEUTRAL\');\n\n  // Status\n  document.getElementById(\'statusBar\').textContent = s.status||\'—\';\n\n  // Scores\n  const ls = s.long_score||0, ss2 = s.short_score||0;\n  document.getElementById(\'longScore\').textContent  = ls||\'—\';\n  document.getElementById(\'shortScore\').textContent = ss2||\'—\';\n  document.getElementById(\'longVeto\').textContent   = s.long_veto  ? \'✗ \'+s.long_veto  : \'\';\n  document.getElementById(\'shortVeto\').textContent  = s.short_veto ? \'✗ \'+s.short_veto : \'\';\n\n  let dec=\'WAIT\', dr=\'No signal\', dc=\'wait\';\n  if(!s.long_veto  && ls>=(s.min_conf||58) && ls>ss2){ dec=\'LONG\';  dr=\'score=\'+ls; dc=\'green\'; }\n  if(!s.short_veto && ss2>=(s.min_conf||58) && ss2>ls){ dec=\'SHORT\'; dr=\'score=\'+ss2; dc=\'red\'; }\n  document.getElementById(\'decision\').textContent = dec;\n  document.getElementById(\'decision\').className   = \'score-val \'+dc;\n  document.getElementById(\'decReason\').textContent = dr;\n\n  // Indicators\n  document.getElementById(\'rsiVal\').textContent = s.rsi||\'—\';\n  document.getElementById(\'adxVal\').textContent = s.adx||\'—\';\n  document.getElementById(\'atrVal\').textContent = s.atr_pct ? s.atr_pct+\'%\' : \'—\';\n\n  // Countdown\n  if(s.next_scan){\n    const secs = Math.max(0,Math.round((new Date(s.next_scan)-Date.now())/1000));\n    const pct  = Math.max(0,Math.min(100,100-(secs/300*100)));\n    document.getElementById(\'scanBar\').style.width = pct+\'%\';\n    document.getElementById(\'scanCountdown\').textContent =\n      secs>0 ? `Next scan in ${Math.floor(secs/60)}m ${secs%60}s` : \'Scanning...\';\n  }\n\n  // Open positions\n  const ops = s.open_positions||[];\n  const opsEl = document.getElementById(\'openPosSection\');\n  if(ops.length){\n    opsEl.innerHTML = ops.map(p=>`\n      <div class="pos-card ${p.side}" style="margin-bottom:10px">\n        <div class="pos-row">\n          <span style="font-weight:700">${p.symbol}</span>\n          <span class="pos-tag ${p.side}">${p.side.toUpperCase()}</span>\n        </div>\n        <div class="pos-row">\n          <span style="color:var(--t2)">Entry</span>\n          <span style="font-family:\'Courier New\'">\\$${p.entry.toLocaleString()}</span>\n        </div>\n        <div class="pos-row">\n          <span style="color:var(--t2)">Lots</span>\n          <span>${p.lots}</span>\n        </div>\n        <div class="pos-row">\n          <span style="color:var(--t2)">UPL</span>\n          <span class="${p.upnl>=0?\'pnl-pos\':\'pnl-neg\'}">\\$${p.upnl>0?\'+\':\'\'}${p.upnl} (${p.pct>0?\'+\':\'\'}${p.pct}%)</span>\n        </div>\n        <div class="pos-row">\n          <span style="color:var(--r)">Stop</span>\n          <span>\\$${p.stop.toLocaleString()}</span>\n        </div>\n        <div class="pos-row">\n          <span style="color:var(--g)">TP</span>\n          <span>\\$${p.tp.toLocaleString()}</span>\n        </div>\n      </div>`).join(\'\');\n  } else {\n    opsEl.innerHTML = \'\';\n  }\n\n  // Wallet\n  document.getElementById(\'walletBig\').textContent = s.capital ? \'\\$\'+s.capital.toFixed(2) : \'\\$—\';\n  const pp = s.pnl_pct||0;\n  const ppEl = document.getElementById(\'pnlPct\');\n  ppEl.textContent = (pp>=0?\'+\':\'\')+pp.toFixed(2)+\'%\';\n  ppEl.className = pp>=0?\'pnl-pos\':\'pnl-neg\';\n  document.getElementById(\'startCapLine\').textContent =\n    s.start_cap ? \'Started: \\$\'+s.start_cap.toFixed(2) : \'\';\n\n  // Stats\n  document.getElementById(\'winRate\').textContent    = s.win_rate!=null ? s.win_rate+\'%\' : \'—\';\n  document.getElementById(\'tradeCount\').textContent = s.total_trades||0;\n  document.getElementById(\'scanCount\').textContent  = s.scan_count||0;\n\n  // Logs\n  if(s.logs) allLogs = s.logs;\n  document.getElementById(\'logCount\').textContent = allLogs.length+\' entries\';\n  if(document.getElementById(\'p-logs\').classList.contains(\'active\')) renderLogs();\n\n  // Trades\n  renderTrades(s.trades||[]);\n\n  // Guardrails\n  if(s.guardrails){\n    document.getElementById(\'guardrailsList\').innerHTML =\n      Object.entries(s.guardrails).map(([k,v])=>\n        `<div class="guard-row"><span class="guard-label">${k.replace(/_/g,\' \')}</span>\n         <span class="guard-val">${v}</span></div>`).join(\'\');\n  }\n}\n\nfunction renderLogs(){\n  const filtered = logFilter ? allLogs.filter(l=>l.l===logFilter) : allLogs;\n  const box = document.getElementById(\'logBox\');\n  box.innerHTML = filtered.slice(-100).reverse().map(l=>\n    `<div class="log-entry"><span class="log-t">${l.t}</span>\n     <span class="log-${l.l}">${l.m}</span></div>`).join(\'\');\n}\n\nfunction filterLogs(f){ logFilter=f; renderLogs(); }\n\nfunction renderTrades(trades){\n  const el = document.getElementById(\'tradesList\');\n  const done = [...trades].reverse();\n  if(!done.length){\n    el.innerHTML=\'<div style="color:var(--t2);text-align:center;padding:20px">No trades yet</div>\';\n    return;\n  }\n  el.innerHTML = done.map(t=>{\n    const open = t.reason===\'open\'||t.won==null;\n    return `<div class="trade-row">\n      <div class="trade-header">\n        <span class="${t.side===\'long\'?\'pnl-pos\':\'pnl-neg\'}" style="font-weight:700">\n          ${t.side.toUpperCase()} ${t.lots}L\n        </span>\n        <span style="color:var(--t2)">${new Date(t.time).toLocaleTimeString()}</span>\n      </div>\n      <div style="display:flex;justify-content:space-between">\n        <span>Entry \\$${(t.entry||0).toLocaleString()}</span>\n        ${open ? \'<span class="trade-open">Open...</span>\' :\n                 `<span>Exit \\$${(t.exit||0).toLocaleString()}</span>`}\n      </div>\n      ${!open ? `<div style="margin-top:4px">\n        <span class="${t.won?\'pnl-pos\':\'pnl-neg\'}" style="font-weight:700">\n          ${t.won?\'✅ WIN\':\'❌ LOSS\'} \\$${(t.pnl||0)>0?\'+\':\'\'}${(t.pnl||0).toFixed(4)} \n          (${(t.pct||0)>0?\'+\':\'\'}${(t.pct||0).toFixed(2)}%)\n        </span>\n        <span style="color:var(--t2);margin-left:8px;font-size:10px">${t.reason||\'\'}</span>\n      </div>` : \'\'}\n    </div>`;\n  }).join(\'\');\n}\n\nasync function startBot(){ const r=await api(\'/api/bot/start\',{}); if(r?.success) document.getElementById(\'statusBar\').textContent=\'Starting...\'; }\nasync function stopBot(){  const r=await api(\'/api/bot/stop\', {}); if(r?.success) document.getElementById(\'statusBar\').textContent=\'Stopped\'; }\nasync function scanNow(){  await api(\'/api/bot/run_now\',{}); document.getElementById(\'statusBar\').textContent=\'Scanning...\'; }\nasync function closeAll(){ if(!confirm(\'Close ALL open positions?\')) return; const r=await api(\'/api/close_all\',{}); alert(\'Closed: \'+(r?.closed||0)); }\nasync function manual(dirn){\n  const lots = parseInt(document.getElementById(\'manualLots\').value)||1;\n  const r = await api(\'/api/manual_trade\',{direction:dirn,lots});\n  if(r?.success) alert(`${dirn.toUpperCase()} ${lots}L @ \\$${r.entry}\\nStop: \\$${r.stop}\\nTP: \\$${r.tp}`);\n  else alert(\'Failed: \'+(r?.message||\'unknown\'));\n}\nasync function doConnect(){\n  const key=document.getElementById(\'apiKey\').value.trim();\n  const sec=document.getElementById(\'apiSecret\').value.trim();\n  if(!key||!sec){ document.getElementById(\'connMsg\').innerHTML=\'<span class="conn-err">Enter API key and secret</span>\'; return; }\n  document.getElementById(\'connMsg\').textContent=\'Connecting...\';\n  const r=await api(\'/api/connect\',{api_key:key,api_secret:sec});\n  document.getElementById(\'connMsg\').innerHTML = r?.success\n    ? `<span class="conn-ok">✓ Connected — Balance \\$${(r.balance||0).toFixed(2)}</span>`\n    : `<span class="conn-err">✗ ${r?.message||\'Failed\'}</span>`;\n}\nasync function loadIp(){\n  const r=await api(\'/api/ip\');\n  document.getElementById(\'serverIp\').textContent = r?.ip||\'unknown\';\n}\n\nasync function poll(){\n  try{\n    const s=await api(\'/api/status\');\n    if(s) refresh(s);\n  }catch(e){}\n}\n\nloadIp();\npoll();\nsetInterval(poll, 4000);\nsetInterval(loadIp, 60000);\n</script>\n</body>\n</html>'



@app.route("/")
def index():
    return Response(DASHBOARD, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)