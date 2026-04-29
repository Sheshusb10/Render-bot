"""
ALPHA BOT v7 — Delta Exchange India
Quantum-grade BTC options + perpetuals bot.
Multi-source data: Delta + Binance public API
Pure price action. No news. No external sentiment.
"""
import os, time, hmac, hashlib, json, math, logging, threading, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("v7")

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
class C:
    BASE       = "https://api.india.delta.exchange"
    BINANCE    = "https://api.binance.com"
    KEY        = os.getenv("DELTA_API_KEY",    "").strip()
    SECRET     = os.getenv("DELTA_API_SECRET", "").strip()
    PID        = 27           # BTCUSD perpetual
    SYMBOL     = "BTCUSD"
    LOT_BTC    = 0.001
    LEVERAGE   = 5
    SCAN_SECS  = 300

    # ── Confidence thresholds ──────────────────────────────────────
    CONF_TRADE   = 62   # minimum to trade
    CONF_ITM     = 78   # above this → buy ITM option (higher delta)
    CONF_STRADDLE= 55   # moderate both ways → straddle

    # ── Perpetual guards ──────────────────────────────────────────
    STOP_PCT     = 0.025   # 2.5% hard stop
    TP_PCT       = 0.030   # 3.0% take profit
    RISK_PCT     = 0.015   # 1.5% capital per trade

    # ── Options guards ────────────────────────────────────────────
    OPT_TP_PCT   = 0.80    # +80% premium = take profit
    OPT_FLOOR    = 0.60    # trail from peak: if peak was +60%, hold
    OPT_STOP_PCT = 0.50    # -50% premium = stop loss
    OPT_MAX_PREM = 0.15    # max 15% of capital on one option trade
    OPT_EXPIRY_BUFFER = 60 # close options 60min before Friday expiry

    # ── Account guards ────────────────────────────────────────────
    HALT_PCT     = 0.08    # halt if down 8% from start
    PAUSE_PCT    = 0.03    # pause if down 3% today
    COOLDOWN_MIN = 30      # wait 30min after any close
    CIRCUIT_N    = 3       # 3 consecutive losses = pause
    CIRCUIT_MIN  = 120     # pause 2hr after circuit break
    MIN_HOLD_MIN = 15      # min 15min before software stop fires
    ADX_TREND    = 22      # ADX floor for trend trades
    STATE        = "/tmp/ab_v7.json"

# ═══════════════════════════════════════════════════════════════════
#  DELTA API
# ═══════════════════════════════════════════════════════════════════
class DeltaAPI:
    def __init__(self):
        self.key  = C.KEY
        self.sec  = C.SECRET
        self.sess = requests.Session()

    def set(self, k, s):
        self.key = k.strip()
        self.sec  = s.strip()

    def _sign(self, method, path, qs="", body=""):
        ts  = str(int(time.time()))
        sig = hmac.new(self.sec.encode(),
            (method+ts+path+qs+body).encode(), hashlib.sha256).hexdigest()
        return {"api-key":self.key,"timestamp":ts,"signature":sig,
                "Content-Type":"application/json"}

    def get(self, path, p=None):
        qs = ("?"+"&".join(f"{k}={v}" for k,v in p.items())) if p else ""
        try:
            r = self.sess.get(f"{C.BASE}{path}{qs}",
                headers=self._sign("GET",path,qs), timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"DeltaGET {path}: {e}")
            return None

    def post(self, path, body):
        b = json.dumps(body)
        try:
            r = self.sess.post(f"{C.BASE}{path}",
                headers=self._sign("POST",path,"",b), data=b, timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"DeltaPOST {path}: {e}")
            return {}

    def price(self):
        try:
            r = self.sess.get(f"{C.BASE}/v2/tickers/BTCUSD", timeout=6)
            return float(r.json().get("result",{}).get("mark_price",0) or 0)
        except: return 0.0

    def balance(self):
        d = self.get("/v2/wallet/balances")
        if not d: return 0.0, None, "No response"
        if not d.get("success"):
            err = d.get("error",{})
            code = err.get("code","") if isinstance(err,dict) else str(err)
            return 0.0, d, f"API error: {code}"
        for b in d.get("result",[]):
            if str(b.get("asset_symbol","")).upper() in ("USD","USDT"):
                av = float(b.get("available_balance",0) or 0)
                bk = float(b.get("blocked_margin",0) or 0)
                if av+bk > 0: return round(av+bk,2), d, "ok"
        ne = float((d.get("meta") or {}).get("net_equity",0) or 0)
        if ne > 0: return round(ne,2), d, "ok"
        return 0.0, d, f"Zero. Assets:{[b.get('asset_symbol') for b in d.get('result',[])]}"

    def candles(self, res="5m", n=100):
        mins = {"1m":1,"5m":5,"15m":15}.get(res,5)
        end  = int(time.time())
        d    = self.get("/v2/history/candles",{
            "symbol":C.SYMBOL,"resolution":res,
            "start":end-mins*60*n,"end":end})
        return d.get("result",[]) if d and d.get("success") else []

    def btcusd_positions(self):
        d = self.get("/v2/positions/margined")
        if not d or not d.get("success"): return []
        return [p for p in d.get("result",[])
                if int(p.get("product_id",0) or 0)==C.PID
                and abs(float(p.get("size",0) or 0))>0]

    def option_positions(self):
        d = self.get("/v2/positions/margined")
        if not d or not d.get("success"): return []
        out = []
        for p in d.get("result",[]):
            sym = str(p.get("product_symbol",""))
            sz  = float(p.get("size",0) or 0)
            if sz > 0 and (sym.startswith("C-BTC") or sym.startswith("P-BTC")):
                out.append(p)
        return out

    def order(self, side, lots, pid=None):
        return self.post("/v2/orders",{
            "product_id":pid or C.PID,"size":lots,"side":side,
            "order_type":"market_order","time_in_force":"ioc"})

    def bracket(self, side, lots, stop, tp):
        return self.post("/v2/orders",{
            "product_id":C.PID,"size":lots,"side":side,
            "order_type":"stop_market_order",
            "stop_price":str(round(stop,1)),
            "bracket_stop_loss_price":str(round(stop,1)),
            "bracket_take_profit_price":str(round(tp,1)),
            "time_in_force":"gtc","stop_trigger_method":"mark_price"})

    def close_position(self, size, pid=None):
        qty  = abs(int(size))
        side = "sell" if size > 0 else "buy"
        return self.post("/v2/orders",{
            "product_id":pid or C.PID,"size":qty,"side":side,
            "order_type":"market_order","time_in_force":"ioc"})

    def get_option_pid(self, symbol):
        prefix = "call_options" if symbol.startswith("C-") else "put_options"
        d = self.get("/v2/products",{"contract_type":prefix,"state":"live"})
        if d and d.get("success"):
            for p in d.get("result",[]):
                if p.get("symbol") == symbol:
                    return p.get("id")
        td = self.get(f"/v2/tickers/{symbol}")
        if td and td.get("success"):
            return td.get("result",{}).get("product_id")
        return None

# ═══════════════════════════════════════════════════════════════════
#  MULTI-SOURCE MARKET DATA
# ═══════════════════════════════════════════════════════════════════
class MarketData:
    """
    Fetches candles from multiple sources:
    - Delta Exchange India (authenticated, BTCUSD)
    - Binance public API (BTCUSDT, no auth needed)
    Merges and validates. Falls back gracefully.
    """
    def __init__(self, delta: DeltaAPI):
        self.delta = delta
        self.sess  = requests.Session()

    def _binance_candles(self, interval="1m", limit=100) -> list:
        """Binance public API — no auth. Returns [{close,high,low,vol}]"""
        try:
            r = self.sess.get(
                f"{C.BINANCE}/api/v3/klines",
                params={"symbol":"BTCUSDT","interval":interval,"limit":limit},
                timeout=8)
            raw = r.json()
            out = []
            for c in raw:
                out.append({
                    "close":  float(c[4]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "volume": float(c[5]),
                    "open":   float(c[1]),
                })
            return out
        except Exception as e:
            log.warning(f"Binance {interval}: {e}")
            return []

    def _parse_delta(self, raw: list) -> list:
        out = []
        for c in raw:
            try:
                v = float(c.get("close",0) or 0)
                if v > 0:
                    out.append({
                        "close":  v,
                        "high":   float(c.get("high",  v) or v),
                        "low":    float(c.get("low",   v) or v),
                        "volume": float(c.get("volume",0) or 0),
                        "open":   float(c.get("open",  v) or v),
                    })
            except: pass
        return out

    def get_all(self) -> dict:
        """
        Returns dict of candle arrays keyed by timeframe.
        Merges Delta + Binance, uses longest valid source.
        """
        # Fetch in parallel-ish (sequential but fast)
        d1m  = self._parse_delta(self.delta.candles("1m", 100))
        d5m  = self._parse_delta(self.delta.candles("5m", 100))
        d15m = self._parse_delta(self.delta.candles("15m", 60))
        b1m  = self._binance_candles("1m", 100)
        b5m  = self._binance_candles("5m", 100)

        # Use whichever source has more data for each timeframe
        c1m  = d1m  if len(d1m)  >= len(b1m)  else b1m
        c5m  = d5m  if len(d5m)  >= len(b5m)  else b5m
        c15m = d15m  # only Delta has 15m

        return {
            "1m":  c1m,
            "5m":  c5m,
            "15m": c15m,
            "source_1m":  "delta" if d1m else "binance",
            "source_5m":  "delta" if d5m else "binance",
        }

    def arrays(self, candles: list):
        """Unpack candle list to (closes, highs, lows, volumes)"""
        cl=[]; hi=[]; lo=[]; vo=[]
        for c in candles:
            cl.append(c["close"]); hi.append(c["high"])
            lo.append(c["low"]);   vo.append(c["volume"])
        return cl, hi, lo, vo

# ═══════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════
def ema(p, n):
    if len(p) < n: return [p[-1]]*len(p) if p else []
    k=2/(n+1); v=[sum(p[:n])/n]
    for x in p[n:]: v.append(x*k+v[-1]*(1-k))
    return [v[0]]*(n-1)+v

def rsi(p, n=14):
    if len(p) < n+2: return 50.0
    d=[p[i]-p[i-1] for i in range(1,len(p))]
    g=sum(max(x,0) for x in d[-n:])/n
    l=sum(abs(min(x,0)) for x in d[-n:])/n
    return round(100 if l<1e-10 else 100-100/(1+g/l), 1)

def adx_calc(hi, lo, cl, n=14):
    if len(cl) < n*2+1: return 0.0, 0.0, 0.0
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
    return round(sum(dx[-n:])/n,1), round(pi[-1],1), round(ni[-1],1)

def atr_val(hi, lo, cl, n=14):
    if len(cl) < n+1: return 0.0
    trs=[max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1]))
         for i in range(1,len(cl))]
    return sum(trs[-n:])/n

def bollinger(cl, n=20):
    if len(cl) < n: m=cl[-1]; return m,m,m,0.0
    w=cl[-n:]; m=sum(w)/n
    s=math.sqrt(sum((p-m)**2 for p in w)/n)
    bw=(4*s/m*100) if m>0 else 0.0
    return m+2*s, m, m-2*s, bw

def macd(cl, fast=12, slow=26, sig=9):
    if len(cl) < slow+sig: return 0.0, 0.0, 0.0
    ef=ema(cl,fast); es=ema(cl,slow)
    line=[ef[i]-es[i] for i in range(len(es))]
    signal=ema(line,sig)
    hist=line[-1]-signal[-1]
    return round(line[-1],2), round(signal[-1],2), round(hist,4)

# ═══════════════════════════════════════════════════════════════════
#  CONFIDENCE ENGINE  (7 pillars, 0-100)
# ═══════════════════════════════════════════════════════════════════
class ConfidenceEngine:
    """
    Scores a trade direction 0-100 across 7 independent pillars.
    All inputs are price-derived. No news. No external sentiment.
    """

    def score(self, candles: dict, direction: str, hour: int) -> dict:
        """
        Returns {
          total: 0-100,
          pillars: {name: {score, max, detail}},
          veto: reason or "",
          regime: STRONG_BULL/BULL/NEUTRAL/BEAR/STRONG_BEAR/SIDEWAYS,
          volatility_regime: LOW/NORMAL/HIGH,
          strategy: SCALP/SWING/STRADDLE/WAIT,
          direction: long/short/straddle/wait
        }
        """
        c1m  = candles.get("1m", [])
        c5m  = candles.get("5m", [])
        c15m = candles.get("15m", [])

        if len(c5m) < 55:
            return {"total":0, "veto":"need_55_candles_5m",
                    "regime":"UNKNOWN", "strategy":"WAIT",
                    "direction":"wait", "pillars":{},
                    "volatility_regime":"UNKNOWN"}

        cl5,hi5,lo5,vo5 = self._unpack(c5m)
        cl1,hi1,lo1,vo1 = self._unpack(c1m) if len(c1m)>=20 else (cl5,hi5,lo5,vo5)
        cl15,hi15,lo15,_ = self._unpack(c15m) if len(c15m)>=21 else (cl5,hi5,lo5,vo5)

        price = cl5[-1]
        pillars = {}

        # ── PILLAR 1: Regime (25pts) ──────────────────────────────
        p1 = self._pillar_regime(cl5, hi5, lo5, direction)
        pillars["Regime"] = p1

        # ── PILLAR 2: Multi-TF Alignment (20pts) ─────────────────
        p2 = self._pillar_mtf(cl5, cl1, cl15, direction)
        pillars["MTF Align"] = p2

        # ── PILLAR 3: Momentum RSI (15pts) ───────────────────────
        p3 = self._pillar_rsi(cl5, cl1, direction)
        pillars["RSI"] = p3

        # ── PILLAR 4: MACD (15pts) ───────────────────────────────
        p4 = self._pillar_macd(cl5, direction)
        pillars["MACD"] = p4

        # ── PILLAR 5: Volatility / BB (10pts) ────────────────────
        p5 = self._pillar_vol(cl5, hi5, lo5)
        pillars["Volatility"] = p5

        # ── PILLAR 6: Volume (10pts) ──────────────────────────────
        p6 = self._pillar_volume(vo5, vo1)
        pillars["Volume"] = p6

        # ── PILLAR 7: Session Time (5pts) ────────────────────────
        p7 = self._pillar_session(hour)
        pillars["Session"] = p7

        total = sum(v["score"] for v in pillars.values())
        total = min(total, 100)

        # ── Detect volatility regime for options strategy ─────────
        _, _, _, bw = bollinger(cl5)
        adx_v,_,_ = adx_calc(hi5, lo5, cl5)
        atr_pct   = atr_val(hi5,lo5,cl5)/price*100 if price>0 else 0

        if bw < 1.5 and adx_v < 18:
            vol_regime = "LOW"       # → good for straddle (expansion coming)
        elif bw > 5.0 or atr_pct > 0.8:
            vol_regime = "HIGH"      # → scalp small, tight stops
        else:
            vol_regime = "NORMAL"

        # ── Detect regime ─────────────────────────────────────────
        e8 =ema(cl5,8)[-1]; e21=ema(cl5,21)[-1]; e55=ema(cl5,55)[-1]
        adx_v2,pdi,ndi = adx_calc(hi5,lo5,cl5)
        if   price>e8>e21>e55 and adx_v2>25 and pdi>ndi: regime="STRONG_BULL"
        elif price>e8>e21 and adx_v2>18:                  regime="BULL"
        elif price<e8<e21<e55 and adx_v2>25 and ndi>pdi: regime="STRONG_BEAR"
        elif price<e8<e21 and adx_v2>18:                  regime="BEAR"
        elif adx_v2 < 15:                                  regime="SIDEWAYS"
        else:                                               regime="NEUTRAL"

        # ── Determine strategy from total + regime + vol ─────────
        veto = ""
        if hour in [2,3,4,5]:
            veto = "dead_zone_UTC"
        if adx_v2 < 12 and vol_regime == "NORMAL":
            veto = "no_trend_ADX<12"

        if veto:
            strategy = "WAIT"
        elif regime == "SIDEWAYS" and vol_regime == "LOW":
            strategy = "STRADDLE"   # compression → expect expansion
        elif vol_regime == "HIGH" and total >= C.CONF_TRADE:
            strategy = "SCALP"      # volatile → quick in/out
        elif total >= C.CONF_TRADE and regime in ("STRONG_BULL","STRONG_BEAR"):
            strategy = "SWING"      # strong trend → hold
        elif total >= C.CONF_TRADE:
            strategy = "SCALP"
        else:
            strategy = "WAIT"

        # ── Final direction ───────────────────────────────────────
        if strategy == "STRADDLE":
            final_dir = "straddle"
        elif total < C.CONF_TRADE or veto:
            final_dir = "wait"
        elif direction == "long" and regime in ("BULL","STRONG_BULL"):
            final_dir = "long"
        elif direction == "short" and regime in ("BEAR","STRONG_BEAR"):
            final_dir = "short"
        else:
            final_dir = "wait"

        return {
            "total":             total,
            "pillars":           pillars,
            "veto":              veto,
            "regime":            regime,
            "volatility_regime": vol_regime,
            "strategy":          strategy,
            "direction":         final_dir,
            "adx":               round(adx_v2,1),
            "bw":                round(bw,2),
            "atr_pct":           round(atr_pct,3),
        }

    def _unpack(self, candles):
        cl=[c["close"] for c in candles]
        hi=[c["high"]  for c in candles]
        lo=[c["low"]   for c in candles]
        vo=[c["volume"] for c in candles]
        return cl,hi,lo,vo

    def _pillar_regime(self, cl, hi, lo, direction):
        if len(cl) < 55: return {"score":0,"max":25,"detail":"no data"}
        adx_v,pdi,ndi = adx_calc(hi,lo,cl)
        e8=ema(cl,8)[-1]; e21=ema(cl,21)[-1]; e55=ema(cl,55)[-1]
        price=cl[-1]
        bull = price>e8>e21>e55 and adx_v>20 and pdi>ndi
        bear = price<e8<e21<e55 and adx_v>20 and ndi>pdi
        if   direction=="long"  and bull: s,d = 25,"Strong bull regime"
        elif direction=="short" and bear: s,d = 25,"Strong bear regime"
        elif direction in ("long","short") and adx_v>15: s,d = 12,"Weak trend"
        else:                                             s,d = 3,"No trend"
        return {"score":s,"max":25,"detail":d,"adx":round(adx_v,1)}

    def _pillar_mtf(self, cl5, cl1, cl15, direction):
        score=0; details=[]
        for tf_cl, label in [(cl1,"1m"),(cl15,"15m")]:
            if len(tf_cl) < 21: continue
            e8=ema(tf_cl,8)[-1]; e21=ema(tf_cl,21)[-1]; p=tf_cl[-1]
            if direction=="long"  and p>e8>e21: score+=10; details.append(f"{label}↑")
            elif direction=="short" and p<e8<e21: score+=10; details.append(f"{label}↓")
            else:                                  details.append(f"{label}~")
        return {"score":min(score,20),"max":20,
                "detail":" ".join(details) or "checking"}

    def _pillar_rsi(self, cl5, cl1, direction):
        r5 = rsi(cl5)
        r1 = rsi(cl1) if len(cl1)>=16 else r5
        if direction=="long":
            if   35<=r5<=55 and r1>r5: s,d = 15,"Pullback+rising"
            elif r5<35:                 s,d = 12,"Oversold bounce"
            elif r5<=65:                s,d = 7, "Mid-range"
            else:                       s,d = 3, "Overbought"
        else:
            if   45<=r5<=65 and r1<r5: s,d = 15,"Distribution+falling"
            elif r5>65:                 s,d = 12,"Overbought rejection"
            elif r5>=35:                s,d = 7, "Mid-range"
            else:                       s,d = 3, "Oversold"
        return {"score":s,"max":15,"detail":d,"rsi5":r5,"rsi1":round(r1,1)}

    def _pillar_macd(self, cl, direction):
        line, sig, hist = macd(cl)
        if direction=="long":
            if hist > 0 and line > sig: s,d = 15,"MACD bullish"
            elif hist > 0:              s,d = 8, "Hist positive"
            else:                       s,d = 2, "MACD bearish"
        else:
            if hist < 0 and line < sig: s,d = 15,"MACD bearish"
            elif hist < 0:              s,d = 8, "Hist negative"
            else:                       s,d = 2, "MACD bullish"
        return {"score":s,"max":15,"detail":d,"hist":hist}

    def _pillar_vol(self, cl, hi, lo):
        _,_,_, bw = bollinger(cl)
        adx_v,_,_ = adx_calc(hi,lo,cl)
        atr_pct   = atr_val(hi,lo,cl)/cl[-1]*100 if cl[-1]>0 else 0
        if   0.5 < bw < 4.0 and 15<adx_v<50: s,d = 10,"Ideal vol"
        elif bw < 0.5:                          s,d = 8, "Squeeze-ready"
        elif bw > 6.0:                          s,d = 3, "Extreme vol"
        else:                                   s,d = 6, "Normal vol"
        return {"score":s,"max":10,"detail":d,"bw":round(bw,2),"atr_pct":round(atr_pct,3)}

    def _pillar_volume(self, vo5, vo1):
        if len(vo5) < 21: return {"score":5,"max":10,"detail":"no vol data"}
        avg5 = sum(vo5[-21:-1])/20
        cur  = vo5[-2]  # last completed candle
        if   cur > avg5*2.0: s,d = 10,"Volume spike"
        elif cur > avg5*1.3: s,d = 7, "Above average"
        elif cur > avg5*0.5: s,d = 5, "Normal"
        else:                 s,d = 2, "Low volume"
        if cur < avg5*0.1: return {"score":0,"max":10,"detail":"volume trap","veto":"low_volume"}
        return {"score":s,"max":10,"detail":d}

    def _pillar_session(self, hour):
        prime = [8,9,13,14,15,16,21,22,23,0]  # London + NY + Asia open
        dead  = [2,3,4,5,6]
        if   hour in dead:  return {"score":0,"max":5,"detail":"dead zone"}
        elif hour in prime: return {"score":5,"max":5,"detail":"prime session"}
        else:               return {"score":3,"max":5,"detail":"off-peak"}

# ═══════════════════════════════════════════════════════════════════
#  OPTIONS STRATEGY ENGINE
# ═══════════════════════════════════════════════════════════════════
class OptionsEngine:
    """
    Decides: ATM or ITM? Call or Put? Straddle?
    Manages profit floor (trailing) + stop ceiling.
    """
    LOT_BTC = 0.001

    def __init__(self, delta: DeltaAPI):
        self.delta = delta
        self._peak_premium = {}   # symbol -> peak mark price seen
        self._entry_time   = {}   # symbol -> datetime opened

    def next_friday(self) -> str:
        from datetime import date, timedelta
        today = date.today()
        days  = (4 - today.weekday()) % 7
        if days == 0: days = 7
        return (today + timedelta(days=days)).strftime("%d%m%y")

    def atm_strike(self, price, interval=500):
        return round(price / interval) * interval

    def itm_strike(self, price, direction, interval=500):
        """ITM = 1 strike in-the-money for higher delta."""
        atm = self.atm_strike(price, interval)
        if direction == "call": return atm - interval   # lower strike = ITM call
        else:                   return atm + interval   # higher strike = ITM put

    def find_option(self, opt_type, btc_price, use_itm=False) -> dict:
        """Find best option. Try ITM or ATM, fallback to other."""
        prefix = "C" if opt_type=="call" else "P"
        expiry = self.next_friday()
        atm    = self.atm_strike(btc_price)

        candidates = []
        if use_itm:
            itm = self.itm_strike(btc_price, opt_type)
            candidates = [itm, atm]
        else:
            candidates = [atm, atm+500 if opt_type=="call" else atm-500]

        for strike in candidates:
            sym = f"{prefix}-BTC-{strike}-{expiry}"
            d   = self.delta.get(f"/v2/tickers/{sym}")
            if d and d.get("success"):
                res  = d.get("result",{})
                mark = float(res.get("mark_price",0) or 0)
                bid  = float(res.get("best_bid",0)  or 0)
                ask  = float(res.get("best_ask",0)  or 0)
                if mark > 0:
                    return {
                        "found":    True,
                        "symbol":   sym,
                        "strike":   strike,
                        "expiry":   expiry,
                        "type":     opt_type,
                        "mark":     mark,
                        "bid":      bid,
                        "ask":      ask,
                        "moneyness":"ITM" if use_itm else "ATM",
                        "premium_usd": mark * self.LOT_BTC,
                    }
        return {"found":False,"tried":candidates,"expiry":expiry}

    def should_exit(self, sym, current_mark, entry_mark, opened_at) -> dict:
        """
        Profit floor + stop ceiling check.
        Returns {exit: bool, reason: str}
        """
        if entry_mark <= 0: return {"exit":False,"reason":""}
        pct = (current_mark - entry_mark) / entry_mark

        # Track peak for profit floor
        peak = self._peak_premium.get(sym, entry_mark)
        if current_mark > peak:
            self._peak_premium[sym] = current_mark
            peak = current_mark

        # Profit floor: if we've been up ≥60% and now drop 30% from peak
        peak_pct = (peak - entry_mark) / entry_mark
        drop_from_peak = (peak - current_mark) / peak if peak > 0 else 0

        # Expiry check: close 60min before Friday 12:00 UTC
        now = datetime.now(timezone.utc)
        expiry_str = sym[-6:] if len(sym) >= 6 else ""
        if expiry_str:
            try:
                exp_dt = datetime.strptime(expiry_str, "%d%m%y").replace(
                    hour=11, minute=0, tzinfo=timezone.utc)
                if now >= exp_dt:
                    return {"exit":True,"reason":f"expiry","pct":pct}
            except: pass

        if pct >= C.OPT_TP_PCT:
            return {"exit":True,"reason":f"TP +{pct*100:.0f}%","pct":pct}
        if pct <= -C.OPT_STOP_PCT:
            return {"exit":True,"reason":f"SL {pct*100:.0f}%","pct":pct}
        if peak_pct >= C.OPT_FLOOR and drop_from_peak >= 0.30:
            return {"exit":True,"reason":f"floor trail peak={peak_pct*100:.0f}%","pct":pct}

        # Minimum hold: don't exit in first 5min
        if opened_at:
            hold = (now - opened_at).seconds / 60
            if hold < 5:
                return {"exit":False,"reason":f"min_hold {hold:.0f}m"}

        return {"exit":False,"reason":f"holding pct={pct*100:.1f}%","pct":pct}

    def record_open(self, sym):
        self._entry_time[sym]   = datetime.now(timezone.utc)
        self._peak_premium[sym] = 0

    def record_close(self, sym):
        self._entry_time.pop(sym, None)
        self._peak_premium.pop(sym, None)

    def opened_at(self, sym):
        return self._entry_time.get(sym)

    def straddle_find(self, btc_price) -> dict:
        """Find matched call+put for straddle at ATM."""
        c = self.find_option("call", btc_price, use_itm=False)
        p = self.find_option("put",  btc_price, use_itm=False)
        if c.get("found") and p.get("found"):
            total_premium = c["premium_usd"] + p["premium_usd"]
            return {
                "found":True,"call":c,"put":p,
                "total_premium_usd":round(total_premium,3),
                "breakeven_up":   c["strike"] + total_premium/self.LOT_BTC,
                "breakeven_down": p["strike"] - total_premium/self.LOT_BTC,
            }
        return {"found":False}

# ═══════════════════════════════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════════════════════════════
class Bot:
    def __init__(self):
        self.delta     = DeltaAPI()
        self.mdata     = None   # MarketData, set on connect
        self.conf_eng  = ConfidenceEngine()
        self.opts_eng  = None   # OptionsEngine, set on connect
        self.running   = False
        self.connected = False
        self.opts_mode = False  # toggle from dashboard

        # Account state
        self.capital   = 0.0
        self.start_cap = 0.0
        self.day_start = 0.0
        self.halted    = False
        self.halt_msg  = ""

        # Dashboard state
        self.status    = "Not connected"
        self.logs      = []
        self.trades    = []
        self.scan_n    = 0
        self.next_scan = None
        self.btc_price = 0.0
        self.last_conf = {}   # last confidence result
        self.total_tr  = 0
        self.wins      = 0

        # Anti-overtrading
        self._stops       = set()
        self._last_close  = None
        self._consec_loss = 0
        self._circuit_until = None
        self._pos_opened  = {}

    def emit(self, level, msg):
        e = {"t":datetime.now(timezone.utc).strftime("%H:%M:%S"),
             "l":level,"m":msg}
        self.logs.append(e)
        if len(self.logs) > 500: self.logs.pop(0)
        getattr(log,{"INFO":"info","WARN":"warning",
                     "ERROR":"error","TRADE":"info"}.get(level,"info"))(msg)

    def save(self):
        try:
            json.dump({
                "start_cap":self.start_cap,"day_start":self.day_start,
                "halted":self.halted,"halt_msg":self.halt_msg,
                "total_tr":self.total_tr,"wins":self.wins,
                "trades":self.trades[-100:],"stops":list(self._stops),
                "consec":self._consec_loss,
                "circuit":self._circuit_until.isoformat() if self._circuit_until else None,
                "last_close":self._last_close.isoformat() if self._last_close else None,
            }, open(C.STATE,"w"))
        except: pass

    def load(self):
        try:
            if not os.path.exists(C.STATE): return False
            s = json.load(open(C.STATE))
            self.start_cap     = float(s.get("start_cap",0))
            self.day_start     = float(s.get("day_start",0))
            self.halted        = bool(s.get("halted",False))
            self.halt_msg      = s.get("halt_msg","")
            self.total_tr      = int(s.get("total_tr",0))
            self.wins          = int(s.get("wins",0))
            self.trades        = s.get("trades",[])
            self._stops        = set(s.get("stops",[]))
            self._consec_loss  = int(s.get("consec",0))
            cu = s.get("circuit"); self._circuit_until = datetime.fromisoformat(cu) if cu else None
            lc = s.get("last_close"); self._last_close = datetime.fromisoformat(lc) if lc else None
            if self.start_cap > 0:
                self.emit("INFO",f"Restored: start=${self.start_cap:.2f} trades={self.total_tr}")
                return True
        except: pass
        return False

    def connect(self, key, secret):
        self.delta.set(key, secret)
        bal, raw, err = self.delta.balance()
        if bal <= 0:
            srv = "unknown"
            try: srv = requests.get("https://api.ipify.org?format=json",timeout=4).json().get("ip","?")
            except: pass
            return {"success":False,"message":err,"server_ip":srv}
        self.capital   = bal
        self.connected = True
        self.mdata     = MarketData(self.delta)
        self.opts_eng  = OptionsEngine(self.delta)
        if not self.load() or self.start_cap <= 0:
            self.start_cap = bal; self.day_start = bal; self.save()
        self.emit("INFO",
            f"Connected | ${bal:.2f} | Start ${self.start_cap:.2f} | "
            f"Halt <${self.start_cap*(1-C.HALT_PCT):.2f}")
        self._sync_positions()
        if not self.running: self.start()
        return {"success":True,"balance":bal}

    def _sync_wallet(self):
        bal,_,err = self.delta.balance()
        if bal <= 0: self.emit("WARN",f"Wallet: {err}"); return
        self.capital = bal
        if self.start_cap > 0:
            loss = (self.start_cap-bal)/self.start_cap
            if loss >= C.HALT_PCT and not self.halted:
                self.halted=True
                self.halt_msg=f"Down {loss*100:.1f}% (${self.start_cap:.2f}→${bal:.2f})"
                self.emit("ERROR",f"HALTED: {self.halt_msg}"); self.save()
        self.emit("INFO",f"Wallet ${bal:.2f} | {'HALTED' if self.halted else 'OK'}")

    def _sync_positions(self):
        for p in self.delta.btcusd_positions():
            sz    = float(p.get("size",0) or 0)
            entry = float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            pid  = str(p.get("product_id",C.PID))
            side = "long" if sz>0 else "short"
            lots = abs(int(sz))
            if not any(str(t.get("pid",""))==pid and t.get("exit") is None for t in self.trades):
                now = datetime.now(timezone.utc)
                self.trades.append({"time":now.isoformat(),"side":side,
                    "entry":round(entry,1),"exit":None,"lots":lots,"pnl":None,
                    "pct":None,"reason":"synced","won":None,"pid":pid,"sym":C.SYMBOL})
                self._pos_opened[pid] = now
                self.emit("INFO",f"Synced: {side.upper()} {lots}L @ ${entry:.0f}")
            if pid not in self._stops and entry>0:
                sp=entry*(1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT)
                tp=entry*(1+C.TP_PCT   if side=="long" else 1-C.TP_PCT)
                cs="sell" if side=="long" else "buy"
                r=self.delta.bracket(cs,lots,sp,tp)
                if r.get("success"): self._stops.add(pid); self.save()
                else: self.emit("WARN",f"Stop FAILED — set manually ${sp:.0f}")

    def _check_perp_exits(self, positions):
        if not self.btc_price: return
        for p in positions:
            sz    = float(p.get("size",0) or 0)
            entry = float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            side = "long" if sz>0 else "short"
            pct  = (self.btc_price-entry)/entry if side=="long" else (entry-self.btc_price)/entry
            lots = abs(int(sz)); pid = p.get("product_id",C.PID)
            now  = datetime.now(timezone.utc)
            opened_at = self._pos_opened.get(str(pid))
            hold_min = (now-opened_at).seconds//60 if opened_at else C.MIN_HOLD_MIN+1
            if hold_min < C.MIN_HOLD_MIN: continue
            reason = None
            if pct <= -C.STOP_PCT: reason="stop"
            elif pct >= C.TP_PCT:  reason="tp"
            if reason:
                r = self.delta.close_position(sz, pid)
                if r.get("success"):
                    pnl = round(entry*lots*C.LOT_BTC*pct,4); won=pct>0
                    self.emit("TRADE",
                        f"{'✅TP' if won else '❌SL'} {side.upper()} {lots}L "
                        f"${entry:.0f}→${self.btc_price:.0f} "
                        f"P&L ${pnl:+.4f} ({pct*100:.2f}%) held={hold_min}m")
                    self._on_close(won, pnl, side, entry, self.btc_price, lots, reason)

    def _check_options_exits(self):
        if not self.opts_eng: return
        for p in self.delta.option_positions():
            sym    = p.get("product_symbol","")
            pid    = p.get("product_id")
            size   = float(p.get("size",0) or 0)
            entry  = float(p.get("avg_entry_price") or p.get("entry_price") or 0)
            mark   = float(p.get("mark_price") or 0)
            if size<=0 or entry<=0 or mark<=0 or not pid: continue
            lots  = int(size)
            check = self.opts_eng.should_exit(sym, mark, entry, self.opts_eng.opened_at(sym))
            if check["exit"]:
                r = self.delta.close_position(size, pid)
                if r.get("success"):
                    pct = check.get("pct",0)
                    pnl = round((mark-entry)*lots*self.opts_eng.LOT_BTC,4)
                    won = pnl > 0
                    self.emit("TRADE",
                        f"{'✅' if won else '❌'} OPT {check['reason']} | "
                        f"{sym} | entry ${entry:.2f}→${mark:.2f} | P&L ${pnl:+.4f}")
                    self.opts_eng.record_close(sym)
                    self._on_close(won, pnl, "option", entry, mark, lots, check["reason"])

    def _on_close(self, won, pnl, side, entry, exit_p, lots, reason):
        now = datetime.now(timezone.utc)
        self._last_close = now
        if won:
            self._consec_loss = 0; self.wins += 1
        else:
            self._consec_loss += 1
            if self._consec_loss >= C.CIRCUIT_N:
                self._circuit_until = now + timedelta(minutes=C.CIRCUIT_MIN)
                self.emit("WARN",
                    f"CIRCUIT BREAKER: {self._consec_loss} losses — "
                    f"pause {C.CIRCUIT_MIN}min")
        for t in reversed(self.trades):
            if t.get("exit") is None and t.get("entry")==round(entry,1):
                t.update({"exit":round(exit_p,1),"pnl":pnl,
                          "pct":round(pnl/max(entry*lots*C.LOT_BTC,0.001)*100,2),
                          "won":won,"reason":reason})
                break
        self.save()

    def _pos_display(self, positions=None):
        if positions is None: positions = self.delta.btcusd_positions()
        out = []
        for p in positions:
            sz    = float(p.get("size",0) or 0)
            entry = float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            mark = float(p.get("mark_price") or self.btc_price or entry)
            upnl = float(p.get("unrealized_pnl") or 0)
            side = "long" if sz>0 else "short"
            pct  = ((mark-entry)/entry if side=="long" else (entry-mark)/entry)*100
            out.append({"sym":C.SYMBOL,"side":side,"lots":abs(sz),
                "entry":round(entry,1),"mark":round(mark,1),
                "upnl":round(upnl,3),"pct":round(pct,2),
                "stop":round(entry*(1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT),1),
                "tp":  round(entry*(1+C.TP_PCT   if side=="long" else 1-C.TP_PCT),1)})
        return out

    def _opts_display(self):
        out = []
        for p in self.delta.option_positions():
            sym   = p.get("product_symbol","")
            sz    = float(p.get("size",0) or 0)
            entry = float(p.get("avg_entry_price") or p.get("entry_price") or 0)
            mark  = float(p.get("mark_price") or 0)
            upnl  = float(p.get("unrealized_pnl") or 0)
            if sz<=0: continue
            pct = (mark-entry)/entry*100 if entry>0 else 0
            peak = self.opts_eng._peak_premium.get(sym,entry) if self.opts_eng else entry
            out.append({
                "sym":sym,"lots":int(sz),"entry":round(entry,4),
                "mark":round(mark,4),"upnl":round(upnl,3),"pct":round(pct,1),
                "peak":round(peak,4),"type":"CALL" if sym.startswith("C-") else "PUT"
            })
        return out

    def scan(self):
        self.scan_n += 1
        self.next_scan = (datetime.now(timezone.utc)+timedelta(seconds=C.SCAN_SECS)).isoformat()

        p = self.delta.price()
        if p > 0: self.btc_price = p
        if self.scan_n % 5 == 0: self._sync_wallet()
        if self.halted: self.status=f"HALTED: {self.halt_msg}"; return

        # Fetch all candle data (Delta + Binance)
        if not self.mdata: return
        candles = self.mdata.get_all()
        c5m = candles.get("5m",[])
        if len(c5m) < 55:
            self.status=f"Need 55 candles, got {len(c5m)}"; return
        self.btc_price = c5m[-1]["close"]

        # Real positions from Delta (fetched once)
        real_pos = self.delta.btcusd_positions()
        self._check_perp_exits(real_pos)
        self._check_options_exits()
        self._sync_positions()

        # Score LONG and SHORT
        hour = datetime.now(timezone.utc).hour
        res_long  = self.conf_eng.score(candles, "long",  hour)
        res_short = self.conf_eng.score(candles, "short", hour)

        # Pick best direction
        if res_long["total"] >= res_short["total"]:
            best = res_long;  other = res_short; best_dir = "long"
        else:
            best = res_short; other = res_long;  best_dir = "short"

        self.last_conf = best
        regime = best["regime"]
        strat  = best["strategy"]

        # Log the scan
        lv = res_long.get("veto",""); sv = res_short.get("veto","")
        self.emit("INFO",
            f"#{self.scan_n} ${self.btc_price:,.0f} | {regime} | "
            f"ADX={best['adx']} BW={best['bw']} | "
            f"L={res_long['total']}{'✗'+lv if lv else ''} "
            f"S={res_short['total']}{'✗'+sv if sv else ''} | "
            f"→{strat}")

        # Guard checks
        now = datetime.now(timezone.utc)
        if self._circuit_until and now < self._circuit_until:
            left = int((self._circuit_until-now).seconds/60)
            self.status=f"Circuit breaker: pause {left}m more"; return
        elif self._circuit_until and now >= self._circuit_until:
            self._circuit_until=None; self._consec_loss=0
            self.emit("INFO","Circuit breaker lifted")
        if self._last_close:
            gap = (now-self._last_close).seconds//60
            if gap < C.COOLDOWN_MIN:
                self.status=f"Cooldown: {C.COOLDOWN_MIN-gap}m remaining"; return
        if self.day_start>0 and (self.capital-self.day_start)/self.day_start<=-C.PAUSE_PCT:
            self.status="Paused — daily -3% limit"; return

        # No open perpetual positions?
        if len(real_pos) >= 1:
            d=self._pos_display(real_pos); x=d[0] if d else {}
            self.status=(f"Holding {x.get('side','').upper()} "
                f"{x.get('lots',0):.0f}L @ ${x.get('entry',0):,.0f} | "
                f"UPL ${x.get('upnl',0):+.3f} ({x.get('pct',0):+.2f}%)")
            self.emit("INFO",self.status); return

        # OPTIONS mode
        if self.opts_mode and self.opts_eng:
            self._trade_options(best, res_long, res_short, now)
            return

        # PERPETUALS mode
        if strat == "WAIT" or best["total"] < C.CONF_TRADE:
            self.status=f"Watching | {regime} | {strat} score={best['total']}"; return

        direction = res_long["direction"] if res_long["total"]>res_short["total"] else res_short["direction"]
        if direction in ("wait","straddle"):
            self.status=f"Watching | {regime} | {direction}"; return

        # Size + order
        margin = self.btc_price * C.LOT_BTC / C.LEVERAGE
        lots   = max(1, min(int(max(self.capital*C.RISK_PCT,margin)/margin),
                            max(1,int(self.capital*.10/margin))))
        side   = "buy" if direction=="long" else "sell"
        r      = self.delta.order(side, lots)
        if not r.get("success"):
            self.emit("ERROR",f"Order failed: {r.get('error',r.get('message','?'))}")
            return
        sp = self.btc_price*(1-C.STOP_PCT if direction=="long" else 1+C.STOP_PCT)
        tp = self.btc_price*(1+C.TP_PCT   if direction=="long" else 1-C.TP_PCT)
        self.delta.bracket("sell" if direction=="long" else "buy", lots, sp, tp)
        self._pos_opened[str(C.PID)] = now
        self.status=f"{direction.upper()} {lots}L @ ${self.btc_price:,.0f} conf={best['total']}"
        self.emit("TRADE",f"{self.status} | {strat}")
        self.total_tr += 1
        self.trades.append({"time":now.isoformat(),"side":direction,
            "entry":round(self.btc_price,1),"exit":None,"lots":lots,"pnl":None,
            "pct":None,"reason":strat.lower(),"won":None,"pid":str(C.PID),"sym":C.SYMBOL})
        self.save()

    def _trade_options(self, best, res_long, res_short, now):
        """Options trading logic with ATM/ITM selection + straddle."""
        # Check existing options
        opt_pos = self.delta.option_positions()
        if opt_pos:
            self.status=f"Holding {len(opt_pos)} option(s)"
            self.emit("INFO",self.status); return

        strat   = best["strategy"]
        total_l = res_long["total"]
        total_s = res_short["total"]

        # STRADDLE: low volatility compression
        if strat == "STRADDLE" and (total_l >= C.CONF_STRADDLE or total_s >= C.CONF_STRADDLE):
            st = self.opts_eng.straddle_find(self.btc_price)
            if st.get("found"):
                total_prem = st["total_premium_usd"]
                if total_prem <= self.capital * C.OPT_MAX_PREM * 2:
                    # Buy call leg
                    c_opt = st["call"]
                    cp = self.delta.get_option_pid(c_opt["symbol"])
                    if cp: self.delta.order("buy",1,cp)
                    # Buy put leg
                    p_opt = st["put"]
                    pp = self.delta.get_option_pid(p_opt["symbol"])
                    if pp: self.delta.order("buy",1,pp)
                    if cp and pp:
                        self.opts_eng.record_open(c_opt["symbol"])
                        self.opts_eng.record_open(p_opt["symbol"])
                        self.status=f"STRADDLE: C+P ${total_prem:.2f} | BE up=${st['breakeven_up']:.0f} dn=${st['breakeven_down']:.0f}"
                        self.emit("TRADE",self.status)
                        self.total_tr+=1
                        for opt,otype in [(c_opt,"call"),(p_opt,"put")]:
                            self.trades.append({"time":now.isoformat(),"side":otype,
                                "entry":round(opt["mark"],4),"exit":None,"lots":1,
                                "pnl":None,"pct":None,"reason":"straddle","won":None,
                                "pid":str(cp if otype=="call" else pp),"sym":opt["symbol"]})
                        self.save()
            return

        # DIRECTIONAL: call or put
        if total_l >= C.CONF_TRADE and total_l >= total_s:
            opt_type = "call"
            conf     = total_l
        elif total_s >= C.CONF_TRADE:
            opt_type = "put"
            conf     = total_s
        else:
            self.status=f"Options: conf {max(total_l,total_s)}<{C.CONF_TRADE}"
            return

        use_itm = (conf >= C.CONF_ITM)  # high confidence → ITM for better delta
        opt = self.opts_eng.find_option(opt_type, self.btc_price, use_itm)

        if not opt.get("found"):
            self.emit("WARN",f"No {opt_type} option found | tried {opt.get('tried')}")
            return

        prem_usd = opt["premium_usd"]
        if prem_usd > self.capital * C.OPT_MAX_PREM:
            self.emit("INFO",f"Premium ${prem_usd:.2f} > max ${self.capital*C.OPT_MAX_PREM:.2f}")
            return
        if prem_usd <= 0:
            self.emit("WARN",f"Zero premium for {opt['symbol']}"); return

        pid = self.delta.get_option_pid(opt["symbol"])
        if not pid:
            self.emit("WARN",f"No pid for {opt['symbol']}"); return

        r = self.delta.order("buy",1,pid)
        if r.get("success"):
            self.opts_eng.record_open(opt["symbol"])
            mon = opt["moneyness"]
            self.status=(f"OPT {opt_type.upper()} {mon} | "
                f"{opt['symbol']} | premium ${prem_usd:.2f} | conf={conf}")
            self.emit("TRADE",self.status)
            self.total_tr+=1
            self.trades.append({"time":now.isoformat(),"side":opt_type,
                "entry":round(opt["mark"],4),"exit":None,"lots":1,"pnl":None,
                "pct":None,"reason":f"{mon.lower()}_{strat.lower()}",
                "won":None,"pid":str(pid),"sym":opt["symbol"]})
            self.save()
        else:
            self.emit("ERROR",f"OPT order failed: {r.get('error','?')}")

    def start(self):
        if not self.running:
            self.running=True
            threading.Thread(target=self._loop,daemon=True).start()
            self.emit("INFO","▶ Bot started")

    def stop(self):
        self.running=False; self.emit("INFO","■ Bot stopped")

    def _loop(self):
        while self.running:
            try: self.scan()
            except Exception as e:
                log.error(f"Error: {e}",exc_info=True)
                self.status=f"Error: {e}"
            time.sleep(C.SCAN_SECS)

    def state(self):
        sc   = self.start_cap or self.capital
        pnl  = (self.capital-sc)/sc*100 if sc>0 else 0
        done = [t for t in self.trades if t.get("won") is not None]
        wr   = sum(1 for t in done if t["won"])/len(done)*100 if done else 0
        cf   = self.last_conf
        pillars = cf.get("pillars",{})
        return {
            "connected":self.connected,"running":self.running,
            "halted":self.halted,"halt_msg":self.halt_msg,
            "status":self.status,"price":round(self.btc_price,1),
            "regime":cf.get("regime","—"),"strategy":cf.get("strategy","—"),
            "vol_regime":cf.get("volatility_regime","—"),
            "adx":cf.get("adx",0),"bw":cf.get("bw",0),
            "atr_pct":cf.get("atr_pct",0),
            "conf_long": sum(v["score"] for v in pillars.values()) if pillars else 0,
            "pillars":   {k:{"s":v["score"],"m":v["max"],"d":v.get("detail","")} for k,v in pillars.items()},
            "capital":round(self.capital,2),"start_cap":round(sc,2),
            "pnl_pct":round(pnl,2),"win_rate":round(wr,1),
            "total_trades":self.total_tr,"wins":self.wins,
            "next_scan":self.next_scan,"scan_n":self.scan_n,
            "opts_mode":self.opts_mode,
            "open_pos":  self._pos_display(),
            "opts_pos":  self._opts_display(),
            "trades":    list(reversed(self.trades[-50:])),
            "logs":      list(reversed(self.logs[-80:])),
            "circuit_active": self._circuit_until is not None,
            "consec_loss":    self._consec_loss,
            "cooldown_left":  max(0,(C.COOLDOWN_MIN*60-(datetime.now(timezone.utc)-self._last_close).seconds)//60) if self._last_close else 0,
            "guardrails":{
                "Perp stop":f"{C.STOP_PCT*100:.1f}%","Perp TP":f"{C.TP_PCT*100:.1f}%",
                "Opt TP":f"+{C.OPT_TP_PCT*100:.0f}% premium","Opt SL":f"-{C.OPT_STOP_PCT*100:.0f}% premium",
                "Opt floor trail":"30% drop from peak (if up 60%)",
                "Monthly halt":f"-{C.HALT_PCT*100:.0f}%","Daily pause":f"-{C.PAUSE_PCT*100:.0f}%",
                "Cooldown":f"{C.COOLDOWN_MIN}min","Circuit":f"{C.CIRCUIT_N} losses={C.CIRCUIT_MIN}min",
                "Min hold":f"{C.MIN_HOLD_MIN}min","ADX floor":str(C.ADX_TREND),
            },
        }


# ═══════════════════════════════════════════════════════════════════
#  FLASK
# ═══════════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app)
bot = Bot()

if C.KEY and C.SECRET:
    threading.Thread(target=lambda: bot.connect(C.KEY,C.SECRET),daemon=True).start()

@app.after_request
def _c(r):
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
    d=request.json or {}; k=d.get("api_key",""); s=d.get("api_secret","")
    if not k or not s: return jsonify({"success":False,"message":"Key+secret required"})
    return jsonify(bot.connect(k.strip(),s.strip()))

@app.route("/api/bot/start",   methods=["POST"])
def api_start(): bot.start(); return jsonify({"success":True})

@app.route("/api/bot/stop",    methods=["POST"])
def api_stop():  bot.stop();  return jsonify({"success":True})

@app.route("/api/bot/run_now", methods=["POST"])
def api_run():
    threading.Thread(target=bot.scan,daemon=True).start()
    return jsonify({"success":True})

@app.route("/api/trades")
def api_trades(): return jsonify(list(reversed(bot.trades[-50:])))

@app.route("/api/logs")
def api_logs(): return jsonify(bot.logs)

@app.route("/api/positions")
def api_positions():
    return jsonify({"perp":bot._pos_display(),"options":bot._opts_display()})

@app.route("/api/ticker")
def api_ticker():
    p=bot.delta.price()
    if not p:
        try: p=requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",timeout=5).json()["bitcoin"]["usd"]
        except: p=0
    return jsonify({"price":p})

@app.route("/api/ip")
def api_ip():
    try: ip=requests.get("https://api.ipify.org?format=json",timeout=5).json().get("ip","?")
    except: ip="unknown"
    return jsonify({"ip":ip})

@app.route("/api/close_all", methods=["POST"])
def api_close_all():
    n=0
    for p in bot.delta.btcusd_positions():
        sz=float(p.get("size",0) or 0)
        r=bot.delta.close_position(sz,p.get("product_id",C.PID))
        if r.get("success"): n+=1
    for p in bot.delta.option_positions():
        sz=float(p.get("size",0) or 0)
        pid=p.get("product_id")
        if pid: r=bot.delta.close_position(sz,pid)
        if r.get("success"): n+=1
    bot.emit("TRADE",f"Emergency close: {n} positions")
    return jsonify({"success":True,"closed":n})

@app.route("/api/manual_trade", methods=["POST"])
def api_manual():
    d=request.json or {}; dirn=d.get("direction","")
    if dirn not in ("long","short"): return jsonify({"success":False,"message":"direction: long/short"})
    p=bot.btc_price or bot.delta.price(); lots=max(1,int(d.get("lots",1)))
    r=bot.delta.order("buy" if dirn=="long" else "sell", lots)
    if r.get("success"):
        sp=p*(1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp=p*(1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
        bot.delta.bracket("sell" if dirn=="long" else "buy",lots,sp,tp)
        bot.emit("TRADE",f"MANUAL {dirn.upper()} {lots}L @ ${p:,.0f}")
        bot.trades.append({"time":datetime.now(timezone.utc).isoformat(),"side":dirn,
            "entry":round(p,1),"exit":None,"lots":lots,"pnl":None,"pct":None,
            "reason":"manual","won":None,"pid":str(C.PID),"sym":C.SYMBOL})
        bot.save()
        return jsonify({"success":True,"entry":round(p,1),"stop":round(sp,1),"tp":round(tp,1)})
    return jsonify({"success":False,"message":r.get("error","failed")})

@app.route("/api/opts/toggle", methods=["POST"])
def api_opts_toggle():
    d=request.json or {}
    bot.opts_mode = bool(d.get("enabled", not bot.opts_mode))
    msg = "Options mode ON" if bot.opts_mode else "Options mode OFF"
    bot.emit("INFO",msg)
    return jsonify({"success":True,"opts_mode":bot.opts_mode,"message":msg})

@app.route("/api/opts/find", methods=["POST"])
def api_opts_find():
    if not bot.opts_eng: return jsonify({"error":"Not connected"})
    d=request.json or {}; t=d.get("type","call")
    p=bot.btc_price or bot.delta.price()
    opt=bot.opts_eng.find_option(t,p,d.get("itm",False))
    if opt.get("found"): opt["premium_usd"]=round(opt["mark"]*0.001,3)
    return jsonify(opt)

@app.route("/api/opts/straddle", methods=["POST"])
def api_opts_straddle():
    if not bot.opts_eng: return jsonify({"error":"Not connected"})
    p=bot.btc_price or bot.delta.price()
    return jsonify(bot.opts_eng.straddle_find(p))

@app.route("/api/config", methods=["POST"])
def api_config():
    d=request.json or {}
    if "min_confidence" in d: C.CONF_TRADE=int(d["min_confidence"])
    if "opts_mode"      in d: bot.opts_mode=bool(d["opts_mode"])
    return jsonify({"success":True})

@app.route("/api/wallet/sync", methods=["POST"])
def api_wallet():
    bot._sync_wallet()
    return jsonify({"success":True,"balance":bot.capital})

@app.route("/api/debug/auth")
def api_debug():
    out={"key_len":len(bot.delta.key),"key_set":bool(bot.delta.key)}
    try:
        r=requests.get(f"{C.BASE}/v2/tickers/BTCUSD",timeout=6)
        out["ticker_ok"]=r.status_code==200
        out["btc_price"]=r.json().get("result",{}).get("mark_price","?")
    except Exception as e: out["ticker_err"]=str(e)
    bal,raw,err=bot.delta.balance(); out["balance"]=bal; out["err"]=err
    return jsonify(out)



DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Alpha Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --g:#00b386;--gb:#e8f9f3;--gd:#a7f3d0;
  --r:#e74c3c;--rb:#fef2f2;--rd:#fca5a5;
  --y:#f59e0b;--yb:#fef3c7;
  --b:#3b82f6;--bb:#eff6ff;
  --bg:#f0f2f5;--w:#fff;
  --t:#0f172a;--t2:#64748b;--t3:#94a3b8;
  --bdr:1px solid #e2e8f0;
  --r8:8px;--r12:12px;--r16:16px;
}
body{background:var(--bg);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px;min-height:100vh}
.hdr{background:var(--w);padding:0 16px;height:54px;display:flex;align-items:center;justify-content:space-between;border-bottom:var(--bdr);position:sticky;top:0;z-index:100;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.logo{display:flex;align-items:center;gap:9px}
.logo-ico{width:32px;height:32px;background:var(--t);border-radius:9px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:700}
.logo-name{font-size:16px;font-weight:700}
.logo-sub{font-size:10px;color:var(--t3)}
.pill{display:flex;align-items:center;gap:5px;padding:5px 12px;border-radius:20px;font-size:11px;font-weight:600}
.pill-ok{background:var(--gb);color:var(--g)}
.pill-off{background:var(--rb);color:var(--r)}
.pdot{width:6px;height:6px;border-radius:50%;background:currentColor}
.wrap{padding:12px 14px 90px;max-width:480px;margin:0 auto}
.tab-c{display:none}.tab-c.show{display:block}
.nav{position:fixed;bottom:0;left:0;right:0;background:var(--w);border-top:var(--bdr);display:flex;padding:8px 0 max(8px,env(safe-area-inset-bottom));z-index:99}
.nb{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:4px 0;border:none;background:none;cursor:pointer;font-family:inherit}
.nb-ico{font-size:20px;color:var(--t3)}
.nb-lbl{font-size:9px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.nb.on .nb-ico,.nb.on .nb-lbl{color:var(--t)}
.card{background:var(--w);border-radius:var(--r12);padding:16px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.05),0 2px 8px rgba(0,0,0,.04)}
.card-title{font-size:11px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.6px;margin-bottom:12px}
.hero{background:var(--t);border-radius:var(--r16);padding:20px;margin-bottom:10px}
.hero-lbl{font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px}
.hero-price{font-size:40px;font-weight:700;color:#fff;line-height:1;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.hero-row{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap}
.hchip{padding:3px 10px;border-radius:6px;font-size:12px;font-weight:600}
.hc-g{background:rgba(0,200,150,.2);color:#00e8b0}
.hc-r{background:rgba(231,76,60,.2);color:#ff8080}
.hc-n{background:rgba(255,255,255,.1);color:rgba(255,255,255,.5)}
.regime-bar{padding:9px 13px;border-radius:var(--r8);margin-bottom:10px;font-size:12px;font-weight:600;border:var(--bdr)}
.rb-bull{background:var(--gb);color:#059669;border-color:var(--gd)}
.rb-bear{background:var(--rb);color:#dc2626;border-color:var(--rd)}
.rb-neu{background:#f8fafc;color:var(--t2);border-color:#e2e8f0}
.rb-sw{background:var(--yb);color:#92400e;border-color:#fde68a}
.conf-wrap{display:flex;align-items:center;gap:14px;padding:4px 0}
.conf-circle{position:relative;width:72px;height:72px;flex-shrink:0}
.conf-circle svg{transform:rotate(-90deg);display:block}
.conf-overlay{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:0}
.conf-num{font-size:22px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.conf-max{font-size:9px;color:var(--t3);font-weight:600}
.conf-info{flex:1}
.conf-dir{font-size:16px;font-weight:700;margin-bottom:3px}
.conf-detail{font-size:11px;color:var(--t2)}
.pillars{display:flex;flex-direction:column;gap:0;margin-top:10px}
.pillar-row{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:var(--bdr)}
.pillar-row:last-child{border:none}
.pillar-name{width:84px;font-size:11px;font-weight:600;color:var(--t2);flex-shrink:0}
.pillar-track{flex:1;height:5px;background:#f1f5f9;border-radius:3px;overflow:hidden}
.pillar-fill{height:100%;border-radius:3px;transition:width .5s}
.pillar-pts{width:36px;text-align:right;font-size:10px;font-weight:700;flex-shrink:0;font-variant-numeric:tabular-nums}
.inds{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px}
.ind-box{background:#f8fafc;border-radius:var(--r8);padding:10px;text-align:center;border:var(--bdr)}
.ind-lbl{font-size:9px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.ind-val{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums}
.scan-bar{height:3px;background:#e2e8f0;border-radius:2px;overflow:hidden;margin-top:8px}
.scan-fill{height:100%;background:var(--b);border-radius:2px;transition:width .5s}
.scan-row{display:flex;justify-content:space-between;font-size:11px;color:var(--t3);margin-top:4px}
.pos-card{border-radius:var(--r12);padding:14px;margin-bottom:10px;border:var(--bdr)}
.pos-long{background:#f0fdf4;border-color:var(--gd)}
.pos-short{background:#fff5f5;border-color:var(--rd)}
.pos-opt{background:#eff6ff;border-color:#93c5fd}
.pos-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.pos-sym{font-size:15px;font-weight:700}
.pos-badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}
.pb-long{background:var(--g);color:#fff}
.pb-short{background:var(--r);color:#fff}
.pb-call{background:var(--b);color:#fff}
.pb-put{background:#8b5cf6;color:#fff}
.pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.pos-item{background:rgba(255,255,255,.75);border-radius:var(--r8);padding:8px}
.pos-item-lbl{font-size:9px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.4px;margin-bottom:2px}
.pos-item-val{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums}
.pos-item-val.g{color:var(--g)}.pos-item-val.r{color:var(--r)}
.wal-top{display:flex;justify-content:space-between;align-items:flex-start}
.wal-left{flex:1}
.wal-lbl{font-size:10px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.wal-amt{font-size:32px;font-weight:700;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.wal-start{font-size:11px;color:var(--t3);margin-top:2px}
.wal-pct{font-size:18px;font-weight:700;text-align:right;font-variant-numeric:tabular-nums}
.wal-pnl{font-size:11px;color:var(--t3);text-align:right;margin-top:2px}
.stats-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}
.stat{background:var(--w);border-radius:var(--r8);padding:12px;text-align:center;border:var(--bdr)}
.stat-lbl{font-size:9px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.stat-val{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.btn-row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px}
.btn{padding:13px 6px;border-radius:var(--r8);border:none;font-family:inherit;font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:5px;transition:opacity .15s}
.btn:active{opacity:.8}
.btn-dark{background:var(--t);color:#fff}
.btn-red{background:var(--rb);color:var(--r);border:1.5px solid var(--rd)}
.btn-blue{background:var(--bb);color:var(--b);border:1.5px solid #bfdbfe}
.btn-full{width:100%;margin-bottom:8px;padding:14px}
.btn-close{background:var(--rb);color:var(--r);border:1.5px solid var(--rd);width:100%;padding:13px;border-radius:var(--r8);font-family:inherit;font-size:13px;font-weight:700;cursor:pointer}
.opts-toggle-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:var(--bdr);margin-bottom:10px}
.toggle-lbl{font-size:14px;font-weight:600}
.toggle-sub{font-size:11px;color:var(--t3);margin-top:1px}
.tog{position:relative;width:46px;height:26px;flex-shrink:0}
.tog input{opacity:0;width:0;height:0;position:absolute}
.tog-sl{position:absolute;inset:0;background:#e2e8f0;border-radius:13px;cursor:pointer;transition:.2s}
.tog-sl:before{content:'';position:absolute;width:20px;height:20px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
.tog input:checked+.tog-sl{background:var(--g)}
.tog input:checked+.tog-sl:before{transform:translateX(20px)}
.opt-info{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;font-size:11px;text-align:center;padding:10px 0;border-bottom:var(--bdr);margin-bottom:10px}
.opt-info-val{font-weight:700;margin-bottom:2px}
.opt-btns{display:flex;gap:8px}
.opt-btn{flex:1;padding:10px;border-radius:var(--r8);font-family:inherit;font-size:11px;font-weight:700;cursor:pointer;border:var(--bdr)}
.ob-call{background:var(--bb);color:var(--b);border-color:#bfdbfe}
.ob-put{background:var(--rb);color:var(--r);border-color:var(--rd)}
.ob-st{background:var(--yb);color:var(--y);border-color:#fde68a}
.opt-result{margin-top:10px;padding:10px;background:#f8fafc;border-radius:var(--r8);font-size:11px;line-height:1.7;display:none;border:var(--bdr)}
.manual-row{display:flex;gap:8px;margin-top:8px}
.btn-long{flex:1;padding:13px;border-radius:var(--r8);border:1.5px solid var(--g);background:var(--gb);color:var(--g);font-family:inherit;font-size:12px;font-weight:700;cursor:pointer}
.btn-short{flex:1;padding:13px;border-radius:var(--r8);border:1.5px solid var(--r);background:var(--rb);color:var(--r);font-family:inherit;font-size:12px;font-weight:700;cursor:pointer}
.inp{width:100%;border:var(--bdr);border-radius:var(--r8);padding:11px 13px;font-size:14px;font-family:inherit;outline:none;background:#f8fafc;margin-bottom:8px}
.inp:focus{border-color:var(--g);background:#fff}
.halt-banner{background:var(--rb);border:1.5px solid var(--rd);border-radius:var(--r12);padding:13px;margin-bottom:10px;text-align:center;color:var(--r);font-weight:700;font-size:13px}
.trade-item{padding:11px 0;border-bottom:var(--bdr);display:flex;align-items:center;gap:10px}
.trade-item:last-child{border:none}
.trade-ico{width:32px;height:32px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;font-weight:700}
.ti-l{background:var(--gb);color:var(--g)}.ti-s{background:var(--rb);color:var(--r)}
.ti-c{background:var(--bb);color:var(--b)}.ti-p{background:#f3e8ff;color:#7c3aed}
.trade-mid{flex:1;min-width:0}
.trade-sym{font-size:12px;font-weight:700}
.trade-meta{font-size:10px;color:var(--t3);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trade-right{text-align:right;flex-shrink:0}
.trade-pnl{font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
.tpg{color:var(--g)}.tpr{color:var(--r)}.tpn{color:var(--t3)}
.log-box{background:#0f172a;border-radius:var(--r8);padding:12px;max-height:400px;overflow-y:auto}
.log-row{padding:4px 0;border-bottom:1px solid #1e293b;font-size:11px;display:flex;gap:8px;font-family:monospace}
.log-t{color:#475569;white-space:nowrap;flex-shrink:0}
.lI{color:#64748b}.lW{color:var(--y)}.lE{color:var(--r)}.lT{color:var(--g);font-weight:700}
.lf-row{display:flex;gap:6px;margin-bottom:8px}
.lf-btn{padding:4px 12px;border-radius:20px;border:var(--bdr);background:var(--w);font-size:11px;font-weight:600;cursor:pointer;color:var(--t3);font-family:inherit}
.lf-btn.on{background:var(--t);color:#fff;border-color:var(--t)}
.setting-row{display:flex;justify-content:space-between;align-items:flex-start;padding:11px 0;border-bottom:var(--bdr)}
.setting-row:last-child{border:none}
.setting-key{font-size:12px;font-weight:500;color:var(--t2)}
.setting-val{font-size:12px;font-weight:700;color:var(--g);text-align:right;max-width:55%}
.ip-box{font-family:monospace;font-size:18px;font-weight:700;text-align:center;padding:14px;background:#f8fafc;border-radius:var(--r8);border:var(--bdr);letter-spacing:2px;margin-bottom:10px}
.empty{text-align:center;padding:28px;color:var(--t3);font-size:13px}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo">
    <div class="logo-ico">A</div>
    <div><div class="logo-name">Alpha Bot</div><div class="logo-sub">Delta Exchange India</div></div>
  </div>
  <div class="pill pill-off" id="sPill"><span class="pdot"></span><span id="sTxt">Stopped</span></div>
</div>

<div class="wrap">
<!-- HOME -->
<div class="tab-c show" id="tab-home">
  <div id="haltDiv" style="display:none" class="halt-banner"></div>
  <div class="hero">
    <div class="hero-lbl">Bitcoin Live</div>
    <div class="hero-price" id="hPrice">$--</div>
    <div class="hero-row">
      <span class="hchip hc-n" id="hRegime">--</span>
      <span class="hchip hc-n" id="hStrat">--</span>
      <span class="hchip hc-n" id="hVol">--</span>
    </div>
  </div>

  <div class="regime-bar rb-neu" id="regBar">Scanning...</div>

  <div class="card">
    <div class="card-title">Confidence Score</div>
    <div class="conf-wrap">
      <div class="conf-circle">
        <svg viewBox="0 0 72 72" width="72" height="72">
          <circle cx="36" cy="36" r="28" fill="none" stroke="#f1f5f9" stroke-width="7"/>
          <circle id="confArc" cx="36" cy="36" r="28" fill="none"
            stroke="#00b386" stroke-width="7" stroke-linecap="round"
            stroke-dasharray="175.9" stroke-dashoffset="175.9"
            style="transition:stroke-dashoffset .6s,stroke .3s"/>
        </svg>
        <div class="conf-overlay">
          <div class="conf-num" id="confNum">--</div>
          <div class="conf-max">/100</div>
        </div>
      </div>
      <div class="conf-info">
        <div class="conf-dir" id="confDir">WAIT</div>
        <div class="conf-detail" id="confDetail">Initializing...</div>
      </div>
    </div>
    <div class="pillars" id="pillarsDiv"></div>
    <div class="inds">
      <div class="ind-box"><div class="ind-lbl">ADX</div><div class="ind-val" id="iAdx">--</div></div>
      <div class="ind-box"><div class="ind-lbl">BB Width</div><div class="ind-val" id="iBw">--</div></div>
      <div class="ind-box"><div class="ind-lbl">ATR %</div><div class="ind-val" id="iAtr">--</div></div>
    </div>
    <div class="scan-bar"><div class="scan-fill" id="scanFill" style="width:0%"></div></div>
    <div class="scan-row">
      <span id="scanStatus">Not running</span>
      <span id="scanCd" style="font-weight:700;color:var(--b)">--</span>
    </div>
  </div>

  <div id="perpPos"></div>
  <div id="optsPos"></div>

  <div class="card">
    <div class="card-title">Wallet</div>
    <div class="wal-top">
      <div class="wal-left">
        <div class="wal-lbl">Balance</div>
        <div class="wal-amt" id="walAmt">$--</div>
        <div class="wal-start" id="walStart"></div>
      </div>
      <div>
        <div class="wal-pct" id="walPct">--%</div>
        <div class="wal-pnl" id="walPnl">P&L $--</div>
      </div>
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat"><div class="stat-lbl">Win Rate</div><div class="stat-val" id="sWR">--</div></div>
    <div class="stat"><div class="stat-lbl">Trades</div><div class="stat-val" id="sTR">0</div></div>
    <div class="stat"><div class="stat-lbl">Scan #</div><div class="stat-val" style="color:var(--b)" id="sSN">0</div></div>
  </div>

  <div class="btn-row3">
    <button class="btn btn-dark" id="bStart">&#9654; Start</button>
    <button class="btn btn-red"  id="bStop">&#9632; Stop</button>
    <button class="btn btn-blue" id="bScan">&#9889; Run</button>
  </div>

  <div class="card">
    <div class="card-title" style="margin-bottom:10px">Options Mode</div>
    <div class="opts-toggle-row">
      <div><div class="toggle-lbl">Enable Options</div><div class="toggle-sub">Calls, Puts, Straddles</div></div>
      <label class="tog"><input type="checkbox" id="togOpts"><span class="tog-sl"></span></label>
    </div>
    <div id="optsPanel" style="display:none">
      <div class="opt-info">
        <div><div class="opt-info-val" style="color:var(--g)">+80%</div><div style="color:var(--t3)">Take Profit</div></div>
        <div><div class="opt-info-val" style="color:var(--r)">-50%</div><div style="color:var(--t3)">Stop Loss</div></div>
        <div><div class="opt-info-val" style="color:var(--b)">-30% peak</div><div style="color:var(--t3)">Floor Trail</div></div>
      </div>
      <div class="opt-btns">
        <button class="opt-btn ob-call" id="bCheckCall">Check CALL</button>
        <button class="opt-btn ob-put"  id="bCheckPut">Check PUT</button>
        <button class="opt-btn ob-st"   id="bCheckSt">Straddle</button>
      </div>
      <div id="optResult" class="opt-result"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title" style="margin-bottom:10px">Manual Trade</div>
    <input class="inp" id="mLots" type="number" placeholder="Lots (default: 1)" min="1">
    <div class="manual-row">
      <button class="btn-long"  id="bBuyLong">&#8593; Buy Long</button>
      <button class="btn-short" id="bSellShort">&#8595; Sell Short</button>
    </div>
  </div>

  <button class="btn-close" id="bCloseAll">&#9888; Close All Positions</button>
</div>

<!-- TRADES -->
<div class="tab-c" id="tab-trades">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <span class="card-title" style="margin:0">All Trades</span>
      <span id="trCountLbl" style="font-size:11px;color:var(--t3)">0 trades</span>
    </div>
    <div id="trList"><div class="empty">No trades yet</div></div>
  </div>
</div>

<!-- LOGS -->
<div class="tab-c" id="tab-logs">
  <div class="lf-row">
    <button class="lf-btn on" id="lfAll">All</button>
    <button class="lf-btn" id="lfTrade">Trades</button>
    <button class="lf-btn" id="lfWarn">Warnings</button>
    <button class="lf-btn" id="lfError">Errors</button>
  </div>
  <div id="logCount" style="font-size:11px;color:var(--t3);margin-bottom:8px">0 entries</div>
  <div class="log-box" id="logBox"></div>
</div>

<!-- SETTINGS -->
<div class="tab-c" id="tab-settings">
  <div class="card">
    <div class="card-title" style="margin-bottom:12px">Delta Exchange Login</div>
    <input class="inp" id="iKey"    type="text"     placeholder="API Key">
    <input class="inp" id="iSecret" type="password" placeholder="API Secret">
    <button class="btn btn-dark btn-full" id="bConnect">Connect to Delta Exchange</button>
    <div id="connMsg" style="text-align:center;font-size:12px;margin-top:8px;line-height:1.7"></div>
  </div>
  <div class="card">
    <div class="card-title" style="margin-bottom:10px">Server IP — Add to Delta Whitelist</div>
    <div class="ip-box" id="serverIp">Loading...</div>
    <div style="font-size:11px;color:var(--t3);line-height:1.9">
      1. Copy IP above<br>
      2. Delta Exchange app &#8594; Account &#8594; API Keys &#8594; Edit<br>
      3. Paste into IP Whitelist &#8594; Save
    </div>
  </div>
  <div class="card">
    <div class="card-title" style="margin-bottom:4px">Active Guardrails</div>
    <div id="guardsList"></div>
  </div>
</div>
</div>

<nav class="nav">
  <button class="nb on" id="nb-home"     onclick="T('home')"><span class="nb-ico">&#127968;</span><span class="nb-lbl">Home</span></button>
  <button class="nb"    id="nb-trades"   onclick="T('trades')"><span class="nb-ico">&#128203;</span><span class="nb-lbl">Trades</span></button>
  <button class="nb"    id="nb-logs"     onclick="T('logs')"><span class="nb-ico">&#128220;</span><span class="nb-lbl">Logs</span></button>
  <button class="nb"    id="nb-settings" onclick="T('settings')"><span class="nb-ico">&#9881;</span><span class="nb-lbl">Settings</span></button>
</nav>

<script>
var G={logs:[],logF:'',trades:[],nextAt:null,scanSecs:300};
var PCOLS={'Regime':'#3b82f6','MTF Align':'#00b386','RSI':'#f59e0b','MACD':'#8b5cf6','Volatility':'#ec4899','Volume':'#e74c3c','Session':'#14b8a6'};

function T(n){
  var tabs=['home','trades','logs','settings'];
  for(var i=0;i<tabs.length;i++){
    var t=tabs[i];
    document.getElementById('tab-'+t).classList.toggle('show',t===n);
    document.getElementById('nb-'+t).classList.toggle('on',t===n);
  }
  if(n==='logs') RL();
  if(n==='trades') RTr();
}

function call(url,body,cb){
  var opts={};
  if(body!==undefined){
    opts={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
  }
  fetch(url,opts).then(function(r){return r.json();}).then(cb||function(){}).catch(function(e){console.error(url,e);});
}

function $(id){return document.getElementById(id);}
function txt(id,v){var e=document.getElementById(id);if(e)e.textContent=v;}
function html(id,v){var e=document.getElementById(id);if(e)e.innerHTML=v;}

function render(s){
  if(!s) return;
  // Pill
  var ok=s.connected&&!s.halted;
  $('sPill').className='pill '+(ok?'pill-ok':'pill-off');
  txt('sTxt',s.halted?'HALTED':s.connected?'Live':'Stopped');
  // Halt
  $('haltDiv').style.display=s.halted?'block':'none';
  if(s.halted) txt('haltDiv','BOT HALTED: '+s.halt_msg);
  // Price
  txt('hPrice',s.price?'$'+s.price.toLocaleString():'$--');
  // Regime
  var rg=s.regime||'';
  $('hRegime').textContent=rg||'--';
  $('hRegime').className='hchip '+(rg.indexOf('BULL')>=0?'hc-g':rg.indexOf('BEAR')>=0?'hc-r':'hc-n');
  $('hStrat').textContent=s.strategy||'--';
  $('hVol').textContent=s.vol_regime||'--';
  // Regime bar
  var rb=$('regBar');
  var isBull=rg.indexOf('BULL')>=0; var isBear=rg.indexOf('BEAR')>=0; var isSide=rg==='SIDEWAYS';
  rb.className='regime-bar '+(isBull?'rb-bull':isBear?'rb-bear':isSide?'rb-sw':'rb-neu');
  rb.textContent=(isBull?'&#128308; ':isBear?'&#128309; ':'')+rg+' — '+(s.strategy||'');
  // Confidence ring
  var sc=s.conf_long||0;
  txt('confNum',sc||'--');
  var circ=175.9;
  var off=circ-(sc/100*circ);
  var arc=$('confArc');
  arc.style.strokeDashoffset=off;
  arc.style.stroke=sc>=70?'#00b386':sc>=50?'#f59e0b':'#e74c3c';
  $('confNum').style.color=sc>=70?'var(--g)':sc>=50?'var(--y)':'var(--r)';
  txt('confDir',s.strategy==='WAIT'?'WAIT':rg);
  txt('confDetail','Score '+sc+'/100 | ADX='+(s.adx||0)+' | Regime: '+rg);
  // Pillars
  var pDiv=$('pillarsDiv');
  var pillars=s.pillars||{};
  var pHtml='';
  var pKeys=Object.keys(pillars);
  for(var i=0;i<pKeys.length;i++){
    var k=pKeys[i]; var v=pillars[k];
    var pct=v.m>0?Math.round(v.s/v.m*100):0;
    var col=PCOLS[k]||'var(--g)';
    pHtml+='<div class="pillar-row">'+
      '<div class="pillar-name">'+k+'</div>'+
      '<div class="pillar-track"><div class="pillar-fill" style="width:'+pct+'%;background:'+col+'"></div></div>'+
      '<div class="pillar-pts" style="color:'+col+'">'+v.s+'/'+v.m+'</div>'+
      '</div>';
  }
  pDiv.innerHTML=pHtml;
  // Indicators
  txt('iAdx',s.adx||'--');
  txt('iBw',s.bw?s.bw+'%':'--');
  txt('iAtr',s.atr_pct?s.atr_pct+'%':'--');
  // Scan bar
  if(s.next_scan) G.nextAt=new Date(s.next_scan);
  txt('scanStatus',s.status||'--');
  txt('sSN',s.scan_n||0);
  // Open perp positions
  var pp=s.open_pos||[];
  var ppHtml='';
  for(var i=0;i<pp.length;i++){
    var p=pp[i];
    var neg=p.upnl<0;
    ppHtml+='<div class="pos-card pos-'+p.side+'">'+
      '<div class="pos-head"><span class="pos-sym">'+p.sym+'</span>'+
      '<span class="pos-badge pb-'+p.side+'">'+p.side.toUpperCase()+'</span></div>'+
      '<div class="pos-grid">'+
      '<div class="pos-item"><div class="pos-item-lbl">Entry</div><div class="pos-item-val">$'+p.entry.toLocaleString()+'</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">Lots</div><div class="pos-item-val">'+p.lots+'</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">UPL</div><div class="pos-item-val '+(neg?'r':'g')+'">'+(p.upnl>=0?'+':'')+p.upnl+' ('+(p.pct>=0?'+':'')+p.pct+'%)</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">Mark</div><div class="pos-item-val">$'+(p.mark||p.entry).toLocaleString()+'</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">Stop</div><div class="pos-item-val r">$'+p.stop.toLocaleString()+'</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">TP</div><div class="pos-item-val g">$'+p.tp.toLocaleString()+'</div></div>'+
      '</div></div>';
  }
  html('perpPos',ppHtml);
  // Open options
  var op=s.opts_pos||[];
  var opHtml='';
  for(var i=0;i<op.length;i++){
    var o=op[i];
    var isC=o.type==='CALL';
    opHtml+='<div class="pos-card pos-opt">'+
      '<div class="pos-head"><span class="pos-sym" style="font-size:12px">'+o.sym+'</span>'+
      '<span class="pos-badge '+(isC?'pb-call':'pb-put')+'">'+o.type+'</span></div>'+
      '<div class="pos-grid">'+
      '<div class="pos-item"><div class="pos-item-lbl">Entry</div><div class="pos-item-val">$'+o.entry+'</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">Mark</div><div class="pos-item-val">$'+o.mark+'</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">P&L</div><div class="pos-item-val '+(o.pct<0?'r':'g')+'">'+(o.pct>=0?'+':'')+o.pct+'%</div></div>'+
      '<div class="pos-item"><div class="pos-item-lbl">Peak</div><div class="pos-item-val g">$'+o.peak+'</div></div>'+
      '</div></div>';
  }
  html('optsPos',opHtml);
  // Wallet
  var cap=s.capital||0; var sc2=s.start_cap||0; var pp2=s.pnl_pct||0;
  txt('walAmt',cap?'$'+cap.toFixed(2):'$--');
  txt('walStart',sc2?'Started: $'+sc2.toFixed(2):'');
  var wpEl=$('walPct');
  wpEl.textContent=(pp2>=0?'+':'')+pp2.toFixed(2)+'%';
  wpEl.style.color=pp2>=0?'var(--g)':'var(--r)';
  txt('walPnl','P&L: $'+(pp2>=0?'+':'')+(cap-sc2).toFixed(2));
  // Stats
  txt('sWR',s.win_rate!=null?s.win_rate+'%':'--');
  txt('sTR',s.total_trades||0);
  // Options toggle
  var ot=$('togOpts'); if(ot) ot.checked=!!s.opts_mode;
  $('optsPanel').style.display=s.opts_mode?'block':'none';
  // Guards
  if(s.guardrails){
    var gk=Object.keys(s.guardrails);
    var gHtml='';
    for(var i=0;i<gk.length;i++){
      gHtml+='<div class="setting-row"><span class="setting-key">'+gk[i]+'</span><span class="setting-val">'+s.guardrails[gk[i]]+'</span></div>';
    }
    html('guardsList',gHtml);
  }
  // Data for other tabs
  if(s.logs) G.logs=s.logs;
  if(s.trades) G.trades=s.trades;
  txt('logCount',G.logs.length+' entries');
  if(document.getElementById('tab-logs').classList.contains('show')) RL();
  if(document.getElementById('tab-trades').classList.contains('show')) RTr();
}

function RL(){
  var f=G.logF?G.logs.filter(function(e){return e.l===G.logF;}):G.logs;
  var h='';
  for(var i=0;i<Math.min(f.length,150);i++){
    var e=f[i];
    var cls='lI';
    if(e.l==='WARN') cls='lW';
    else if(e.l==='ERROR') cls='lE';
    else if(e.l==='TRADE') cls='lT';
    h+='<div class="log-row"><span class="log-t">'+e.t+'</span><span class="'+cls+'">'+e.m+'</span></div>';
  }
  html('logBox',h);
}

function SLF(f){
  G.logF=f;
  var map={'':'lfAll','TRADE':'lfTrade','WARN':'lfWarn','ERROR':'lfError'};
  var keys=Object.keys(map);
  for(var i=0;i<keys.length;i++){
    var el=$(map[keys[i]]);
    if(el) el.classList.toggle('on',keys[i]===f);
  }
  RL();
}

function RTr(){
  var el=$('trList');
  txt('trCountLbl',G.trades.length+' trades');
  if(!G.trades.length){html('trList','<div class="empty">No trades yet</div>');return;}
  var h='';
  for(var i=0;i<G.trades.length;i++){
    var t=G.trades[i];
    var open=t.exit==null;
    var s=t.side||'';
    var ic=s==='long'?'ti-l':s==='short'?'ti-s':s==='call'?'ti-c':'ti-p';
    var ico=s==='long'?'&#8593;':s==='short'?'&#8595;':s==='call'?'C':'P';
    var pc=open?'tpn':(t.won?'tpg':'tpr');
    var pv=open?'Open…':(t.won?'+':'')+(t.pnl||0).toFixed(4);
    var tm=t.time?t.time.substr(5,11).replace('T',' '):'';
    h+='<div class="trade-item">'+
      '<div class="trade-ico '+ic+'">'+ico+'</div>'+
      '<div class="trade-mid">'+
        '<div class="trade-sym">'+(t.sym||'BTCUSD')+'</div>'+
        '<div class="trade-meta">'+tm+' &middot; '+(t.reason||'')+'</div>'+
      '</div>'+
      '<div class="trade-right">'+
        '<div class="trade-pnl '+pc+'">$'+pv+'</div>'+
        '<div style="font-size:10px;color:var(--t3)">'+(t.entry?'@ $'+t.entry:'')+'</div>'+
      '</div>'+
      '</div>';
  }
  html('trList',h);
}

// Countdown
setInterval(function(){
  if(!G.nextAt) return;
  var d=Math.max(0,Math.round((G.nextAt-Date.now())/1000));
  var m=Math.floor(d/60); var s=d%60;
  txt('scanCd',d>0?(m+'m '+s+'s'):'Scanning');
  $('scanFill').style.width=Math.max(0,100-d/G.scanSecs*100)+'%';
},1000);

// Poll
function poll(){
  call('/api/status',undefined,function(s){if(s) render(s);});
}
poll();
setInterval(poll,4000);

// Load IP
call('/api/ip',undefined,function(r){txt('serverIp',r&&r.ip?r.ip:'unknown');});

// Wire all buttons after page loads
window.onload=function(){
  $('bStart').onclick=function(){call('/api/bot/start',{});};
  $('bStop').onclick=function(){call('/api/bot/stop',{});};
  $('bScan').onclick=function(){call('/api/bot/run_now',{});txt('scanStatus','Triggering scan...');};
  $('bCloseAll').onclick=function(){
    if(!confirm('Close ALL positions?')) return;
    call('/api/close_all',{},function(r){alert('Closed: '+(r?r.closed:0)+' positions');});
  };
  $('bBuyLong').onclick=function(){
    var lots=parseInt($('mLots').value)||1;
    call('/api/manual_trade',{direction:'long',lots:lots},function(r){
      if(r&&r.success) alert('LONG '+lots+'L
Entry $'+r.entry+'
Stop $'+r.stop+'
TP $'+r.tp);
      else alert('Failed: '+(r?r.message:'check Logs tab'));
    });
  };
  $('bSellShort').onclick=function(){
    var lots=parseInt($('mLots').value)||1;
    call('/api/manual_trade',{direction:'short',lots:lots},function(r){
      if(r&&r.success) alert('SHORT '+lots+'L
Entry $'+r.entry+'
Stop $'+r.stop+'
TP $'+r.tp);
      else alert('Failed: '+(r?r.message:'check Logs tab'));
    });
  };
  $('bConnect').onclick=function(){
    var k=$('iKey').value.trim(); var s=$('iSecret').value.trim();
    if(!k||!s){html('connMsg','<span style="color:var(--r)">Enter API key and secret</span>');return;}
    html('connMsg','Connecting...');
    call('/api/connect',{api_key:k,api_secret:s},function(r){
      html('connMsg',r&&r.success
        ?'<span style="color:var(--g)">&#10003; Connected &mdash; $'+(r.balance||0).toFixed(2)+'</span>'
        :'<span style="color:var(--r)">&#10007; '+(r?r.message:'Failed')+'</span>'+(r&&r.server_ip?'<br><small style="color:var(--t3)">Server IP: '+r.server_ip+'</small>':'')+'<br><small style="color:var(--t3)">Debug: /api/debug/auth</small>');
    });
  };
  $('togOpts').onchange=function(){
    call('/api/opts/toggle',{enabled:this.checked},function(r){
      $('optsPanel').style.display=r&&r.opts_mode?'block':'none';
    });
  };
  $('bCheckCall').onclick=function(){
    $('optResult').style.display='block';
    txt('optResult','Checking...');
    call('/api/opts/find',{type:'call',itm:false},function(r){
      html('optResult',r&&r.found
        ?('<b>'+r.symbol+'</b><br>Strike $'+(r.strike||0).toLocaleString()+' | Mark $'+(r.mark||0).toFixed(2)+' | Premium $'+(r.premium_usd||0).toFixed(2)+'<br>Expiry Friday '+r.expiry)
        :'No CALL found. Expiry: '+(r&&r.expiry||'?'));
    });
  };
  $('bCheckPut').onclick=function(){
    $('optResult').style.display='block';
    txt('optResult','Checking...');
    call('/api/opts/find',{type:'put',itm:false},function(r){
      html('optResult',r&&r.found
        ?('<b>'+r.symbol+'</b><br>Strike $'+(r.strike||0).toLocaleString()+' | Mark $'+(r.mark||0).toFixed(2)+' | Premium $'+(r.premium_usd||0).toFixed(2))
        :'No PUT found. Expiry: '+(r&&r.expiry||'?'));
    });
  };
  $('bCheckSt').onclick=function(){
    $('optResult').style.display='block';
    txt('optResult','Checking straddle...');
    call('/api/opts/straddle',{},function(r){
      html('optResult',r&&r.found
        ?('<b>Straddle</b><br>Total Premium: $'+(r.total_premium_usd||0).toFixed(2)+'<br>Break-even UP: $'+Math.round(r.breakeven_up||0).toLocaleString()+' | DOWN: $'+Math.round(r.breakeven_down||0).toLocaleString())
        :'Could not build straddle.');
    });
  };
  $('lfAll').onclick=function(){SLF('');};
  $('lfTrade').onclick=function(){SLF('TRADE');};
  $('lfWarn').onclick=function(){SLF('WARN');};
  $('lfError').onclick=function(){SLF('ERROR');};
};
</script>
</body>
</html>
"""

@app.route("/")
def index(): return Response(DASHBOARD, mimetype="text/html")

if __name__ == "__main__":
    port=int(os.getenv("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)