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
    STOP_PCT     = 0.025   # 2.5% hard stop (base, scaled by ATR)
    TP_PCT       = 0.030   # 3.0% take profit (base, scaled by ATR)
    # Dynamic TP/SL: scaled by ATR regime
    # Low vol  (ATR<0.3%): TP = 1.5×ATR, SL = 1.0×ATR  (tight, realistic)
    # Normal   (0.3-0.8%): TP = 2.0×ATR, SL = 1.0×ATR
    # High vol (ATR>0.8%): TP = 3.0×ATR, SL = 1.5×ATR  (wider, room to breathe)
    RISK_PCT     = 0.015   # 1.5% capital per trade

    # ── Options guards ────────────────────────────────────────────
    OPT_TP_PCT   = 0.80    # +80% premium = take profit
    OPT_FLOOR    = 0.60    # trail from peak: if peak was +60%, hold
    OPT_STOP_PCT = 0.50    # -50% premium = stop loss
    OPT_MAX_PREM = 0.15    # max 15% of capital on one option trade
    OPT_EXPIRY_BUFFER = 180 # close options 3hr before Friday expiry (liquidity cliff)

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
def pid_int(v):
    """Normalise product_id to int — Delta returns int or str inconsistently."""
    try: return int(v)
    except (TypeError, ValueError): return 0


class DeltaAPI:
    def __init__(self):
        self.key  = C.KEY
        self.sec  = C.SECRET
        self.sess = requests.Session()
        self._lock = threading.Lock()  # prevent signature race condition

    def set(self, k, s):
        self.key = k.strip()
        self.sec  = s.strip()

    def _sign(self, method, path, qs="", body=""):
        with self._lock:
            ts = str(int(time.time()))
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
                if pid_int(p.get("product_id",0)) == C.PID
                and abs(float(p.get("size",0) or 0)) > 0]

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
        Returns merged candle dict.
        Binance 1m used as LEAD INDICATOR: Binance often moves seconds
        before Delta India reacts. If Binance RSI diverges from Delta RSI,
        front-run the expected Delta move.
        """
        d1m  = self._parse_delta(self.delta.candles("1m", 100))
        d5m  = self._parse_delta(self.delta.candles("5m", 100))
        d15m = self._parse_delta(self.delta.candles("15m", 60))
        b1m  = self._binance_candles("1m", 100)
        b5m  = self._binance_candles("5m", 100)

        # Primary: use Delta (same exchange = exact prices for orders)
        # Fallback: Binance if Delta returns insufficient data
        c1m  = d1m if len(d1m)  >= 20 else b1m
        c5m  = d5m if len(d5m)  >= 55 else b5m
        c15m = d15m

        # ── Binance Lead Signal ──────────────────────────────────────
        # Compute RSI on Binance 1m vs Delta 1m.
        # Divergence = Binance already moved, Delta hasn't caught up yet.
        bnc_lead = "neutral"
        if len(b1m) >= 16 and len(d1m) >= 16:
            bc = [x["close"] for x in b1m]
            dc = [x["close"] for x in d1m]
            b_rsi = rsi(bc)
            d_rsi = rsi(dc)
            diff  = b_rsi - d_rsi
            # Binance RSI significantly higher → Delta likely to rise soon
            if   diff > 8:  bnc_lead = "binance_leading_bull"
            elif diff < -8: bnc_lead = "binance_leading_bear"

        return {
            "1m":  c1m,
            "5m":  c5m,
            "15m": c15m,
            "binance_lead": bnc_lead,
            "source_1m":   "delta" if d1m else "binance",
            "source_5m":   "delta" if d5m else "binance",
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

def atr_tp_sl(atr_pct: float) -> tuple:
    """
    Dynamic TP and SL percentages based on ATR regime.
    Returns (tp_pct, sl_pct).
    Low vol → tight targets (realistic). High vol → wider (avoid noise).
    """
    if atr_pct <= 0:
        return C.TP_PCT, C.STOP_PCT
    if atr_pct < 0.30:          # Low volatility
        tp = max(atr_pct * 1.5 / 100, 0.010)
        sl = max(atr_pct * 1.0 / 100, 0.008)
    elif atr_pct < 0.80:        # Normal volatility
        tp = atr_pct * 2.0 / 100
        sl = atr_pct * 1.0 / 100
    else:                        # High volatility
        tp = min(atr_pct * 3.0 / 100, 0.08)
        sl = min(atr_pct * 1.5 / 100, 0.04)
    return round(tp, 4), round(sl, 4)


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

        total = sum(v["score"] for v in pillars.values()) + lead_bonus
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
                iv   = float(res.get("mark_iv",0)   or 0)  # implied vol %
                if mark <= 0:
                    continue

                # ── IV FILTER ────────────────────────────────────────
                # If implied volatility > 150%, premium is too expensive.
                # "IV Crush" will destroy value even if price moves correctly.
                # 80-120% is normal for BTC weekly options. >150% = avoid.
                iv_too_high = iv > 150.0 and iv > 0
                if iv_too_high:
                    log.info(f"IV filter: {sym} IV={iv:.1f}% > 150% — skip")
                    continue

                # Spread check: bid/ask spread > 20% of mark = illiquid
                spread_pct = (ask - bid) / mark * 100 if (mark > 0 and ask > bid) else 0
                if spread_pct > 20 and bid > 0:
                    log.info(f"Spread filter: {sym} spread={spread_pct:.1f}% > 20% — skip")
                    continue

                return {
                    "found":       True,
                    "symbol":      sym,
                    "strike":      strike,
                    "expiry":      expiry,
                    "type":        opt_type,
                    "mark":        mark,
                    "bid":         bid,
                    "ask":         ask,
                    "iv":          round(iv, 1),
                    "spread_pct":  round(spread_pct, 1),
                    "moneyness":   "ITM" if use_itm else "ATM",
                    "premium_usd": round(mark * self.LOT_BTC, 3),
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
                # Delta settles at 12:00 UTC Friday. We exit OPT_EXPIRY_BUFFER
                # minutes early to avoid the liquidity cliff (empty order book).
                exp_dt = datetime.strptime(expiry_str, "%d%m%y").replace(
                    hour=12, minute=0, tzinfo=timezone.utc)
                buffer_dt = exp_dt - timedelta(minutes=C.OPT_EXPIRY_BUFFER)
                if now >= buffer_dt:
                    mins_left = int((exp_dt - now).total_seconds() / 60)
                    return {"exit":True,
                            "reason":f"expiry_buffer {mins_left}m to settle",
                            "pct":pct}
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

    def buy_option_limit(self, symbol, mark_price, pid=None, lots=1, max_tries=3):
        """
        Limit order with chase: avoids the 2-5% market order spread on options.
        Retries up to max_tries with price widening 0.5% each attempt.
        Falls back to market if all limit attempts timeout.
        """
        if not pid:
            pid = self.get_pid(symbol)
        if not pid:
            return {"success": False, "error": f"No pid for {symbol}"}
        for attempt in range(max_tries):
            limit = round(mark_price * (1 + attempt * 0.005), 2)
            r = self.delta.post("/v2/orders", {
                "product_id":    pid, "size": lots, "side": "buy",
                "order_type":    "limit_order", "limit_price": str(limit),
                "time_in_force": "gtc",
            })
            if r.get("success"):
                order_id = (r.get("result") or {}).get("id")
                if order_id:
                    for _ in range(5):
                        time.sleep(1)
                        od = self.delta.get(f"/v2/orders/{order_id}")
                        state = ((od or {}).get("result") or {}).get("state","")
                        if state in ("filled","closed"):
                            log.info(f"Limit fill: {symbol} @ ${limit:.2f} try={attempt+1}")
                            return {"success": True, "fill_price": limit}
                    self.delta.post(f"/v2/orders/{order_id}/cancel", {"product_id": pid})
            if attempt < max_tries - 1:
                time.sleep(2)
        # Fallback to market
        log.warning(f"Limit chase failed {symbol} — using market order")
        return self.delta.post("/v2/orders", {
            "product_id": pid, "size": lots, "side": "buy",
            "order_type": "market_order", "time_in_force": "ioc"})

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
            # Persist peak_premium so floor-trail survives restarts
            peak = {}
            if self.opts_eng:
                peak = {str(k):v for k,v in self.opts_eng._peak_premium.items()}
            json.dump({
                "start_cap":  self.start_cap,
                "day_start":  self.day_start,
                "halted":     self.halted,
                "halt_msg":   self.halt_msg,
                "total_tr":   self.total_tr,
                "wins":       self.wins,
                "trades":     self.trades[-100:],
                "stops":      [int(x) for x in self._stops],   # int list
                "consec":     self._consec_loss,
                "circuit":    self._circuit_until.isoformat() if self._circuit_until else None,
                "last_close": self._last_close.isoformat() if self._last_close else None,
                "peak_premium": peak,
            }, open(C.STATE,"w"))
        except Exception as e:
            log.warning(f"save failed: {e}")

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
            self._stops        = set(int(x) for x in s.get("stops",[]))  # int set
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
        self.mdata    = MarketData(self.delta)
        self.opts_eng = OptionsEngine(self.delta)
        loaded = self.load()
        if not loaded or self.start_cap <= 0:
            self.start_cap = bal; self.day_start = bal; self.save()
        # Restore peak_premium from saved state
        try:
            import json as _j
            if os.path.exists(C.STATE):
                _s = _j.load(open(C.STATE))
                for sym, peak in _s.get("peak_premium", {}).items():
                    self.opts_eng._peak_premium[sym] = float(peak)
                if _s.get("peak_premium"):
                    self.emit("INFO", f"Restored {len(_s['peak_premium'])} option peaks")
        except Exception: pass
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
            pid  = pid_int(p.get("product_id", C.PID))   # always int
            side = "long" if sz>0 else "short"
            lots = abs(int(sz))
            if not any(pid_int(t.get("pid",0))==pid and t.get("exit") is None for t in self.trades):
                now = datetime.now(timezone.utc)
                self.trades.append({"time":now.isoformat(),"side":side,
                    "entry":round(entry,1),"exit":None,"lots":lots,"pnl":None,
                    "pct":None,"reason":"synced","won":None,"pid":pid,"sym":C.SYMBOL})
                self._pos_opened[pid] = now   # int key
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
            lots = abs(int(sz))
            pid  = pid_int(p.get("product_id", C.PID))   # int key
            now  = datetime.now(timezone.utc)
            opened_at = self._pos_opened.get(pid)   # dict keyed by int
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
        # Dynamic TP/SL based on current ATR regime
        dyn_tp, dyn_sl = atr_tp_sl(self.last_conf.get("atr_pct", 0))
        sp = self.btc_price*(1-dyn_sl if direction=="long" else 1+dyn_sl)
        tp = self.btc_price*(1+dyn_tp if direction=="long" else 1-dyn_tp)
        self.emit("INFO",
            f"ATR={self.last_conf.get('atr_pct',0):.3f}% → "
            f"dynamic TP={dyn_tp*100:.2f}% SL={dyn_sl*100:.2f}%")
        self.delta.bracket("sell" if direction=="long" else "buy", lots, sp, tp)
        self._pos_opened[pid_int(C.PID)] = now
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
            # Only enter straddle on confirmed BB squeeze (multi-candle low BW)
            bw_now = self.last_conf.get("bw", 99)
            # Require BW below 1.5% (tight compression) to enter straddle
            if bw_now > 1.5:
                self.status = f"Straddle skipped: BW={bw_now:.2f}% not compressed enough (<1.5%)"
                return
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

        r = self.opts_eng.buy_option_limit(opt["symbol"], opt["mark"], pid=pid, lots=1)
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



import base64 as _b64
_DASHBOARD_HTML = _b64.b64decode(b"PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEsbWF4aW11bS1zY2FsZT0xIj4KPHRpdGxlPkFscGhhIEJvdDwvdGl0bGU+CjxzdHlsZT4KKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQo6cm9vdHstLWc6IzAwYjM4NjstLWdiOiNlOGY5ZjM7LS1nZDojYTdmM2QwOy0tcjojZTc0YzNjOy0tcmI6I2ZlZjJmMjstLXJkOiNmY2E1YTU7LS15OiNmNTllMGI7LS1iOiMzYjgyZjY7LS1iYjojZWZmNmZmOy0tdDojMGYxNzJhOy0tdDI6IzY0NzQ4YjstLXQzOiM5NGEzYjg7LS1iZzojZjBmMmY1Oy0tdzojZmZmOy0tYmRyOjFweCBzb2xpZCAjZTJlOGYwfQpib2R5e2JhY2tncm91bmQ6dmFyKC0tYmcpO2NvbG9yOnZhcigtLXQpO2ZvbnQtZmFtaWx5Oi1hcHBsZS1zeXN0ZW0sQmxpbmtNYWNTeXN0ZW1Gb250LCJTZWdvZSBVSSIsSGVsdmV0aWNhLEFyaWFsLHNhbnMtc2VyaWY7Zm9udC1zaXplOjE0cHh9Ci5oZHJ7YmFja2dyb3VuZDp2YXIoLS13KTtwYWRkaW5nOjAgMTZweDtoZWlnaHQ6NTRweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTtwb3NpdGlvbjpzdGlja3k7dG9wOjA7ei1pbmRleDoxMDB9Ci5sb2dve2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweH0KLmxpY3t3aWR0aDozMnB4O2hlaWdodDozMnB4O2JhY2tncm91bmQ6dmFyKC0tdCk7Ym9yZGVyLXJhZGl1czo5cHg7Y29sb3I6I2ZmZjtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwfQoubG57Zm9udC1zaXplOjE1cHg7Zm9udC13ZWlnaHQ6NzAwfS5sc3tmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10Myl9Ci5waWxse3BhZGRpbmc6NXB4IDEycHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjYwMDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo1cHh9Ci5wLW9re2JhY2tncm91bmQ6dmFyKC0tZ2IpO2NvbG9yOnZhcigtLWcpfS5wLW9mZntiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKX0KLndyYXB7cGFkZGluZzoxMnB4IDE0cHggOTBweDttYXgtd2lkdGg6NDgwcHg7bWFyZ2luOjAgYXV0b30KLnRhYntkaXNwbGF5Om5vbmV9LnRhYi5vbntkaXNwbGF5OmJsb2NrfQoubmF2e3Bvc2l0aW9uOmZpeGVkO2JvdHRvbTowO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXRvcDp2YXIoLS1iZHIpO2Rpc3BsYXk6ZmxleDtwYWRkaW5nOjhweCAwIG1heCg4cHgsZW52KHNhZmUtYXJlYS1pbnNldC1ib3R0b20pKTt6LWluZGV4Ojk5fQoubmJ7ZmxleDoxO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47YWxpZ24taXRlbXM6Y2VudGVyO2dhcDozcHg7cGFkZGluZzo0cHggMDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLm5pe2ZvbnQtc2l6ZToyMHB4O2NvbG9yOnZhcigtLXQzKX0ubmx7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDo2MDA7Y29sb3I6dmFyKC0tdDMpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNXB4fQoubmIub24gLm5pLC5uYi5vbiAubmx7Y29sb3I6dmFyKC0tdCl9Ci5jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTZweDttYXJnaW4tYm90dG9tOjEwcHg7Ym94LXNoYWRvdzowIDFweCAzcHggcmdiYSgwLDAsMCwuMDUpLDAgMnB4IDhweCByZ2JhKDAsMCwwLC4wNCl9Ci5jdHtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206MTJweH0KLyogU0VUVVAgQ0FSRCAqLwouc2V0dXB7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLCMwZjE3MmEsIzFlM2E1Zik7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MjBweDttYXJnaW4tYm90dG9tOjEwcHg7Y29sb3I6I2ZmZn0KLnNldHVwIGgye2ZvbnQtc2l6ZToxOHB4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tYm90dG9tOjZweH0KLnNldHVwIHB7Zm9udC1zaXplOjEycHg7b3BhY2l0eTouNzttYXJnaW4tYm90dG9tOjE2cHg7bGluZS1oZWlnaHQ6MS42fQouc2V0dXAtc3RlcHN7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6OHB4O21hcmdpbi1ib3R0b206MTZweH0KLnN0ZXB7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDtmb250LXNpemU6MTJweH0KLnN0ZXAtbnt3aWR0aDoyMnB4O2hlaWdodDoyMnB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTUpO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7ZmxleC1zaHJpbms6MH0KLmdvLWJ0bnt3aWR0aDoxMDAlO3BhZGRpbmc6MTJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLWcpO2NvbG9yOiNmZmY7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci8qIEhFUk8gKi8KLmhlcm97YmFja2dyb3VuZDp2YXIoLS10KTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMHB4O21hcmdpbi1ib3R0b206MTBweH0KLmhse2ZvbnQtc2l6ZToxMHB4O2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjQpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouOHB4O21hcmdpbi1ib3R0b206NXB4fQouaHB7Zm9udC1zaXplOjQwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOiNmZmY7bGluZS1oZWlnaHQ6MTtsZXR0ZXItc3BhY2luZzotMXB4fQouaHJ7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O21hcmdpbi10b3A6OHB4O2ZsZXgtd3JhcDp3cmFwfQouaGN7cGFkZGluZzozcHggMTBweDtib3JkZXItcmFkaXVzOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo2MDB9Ci5oY2d7YmFja2dyb3VuZDpyZ2JhKDAsMjAwLDE1MCwuMik7Y29sb3I6IzAwZThiMH0uaGNye2JhY2tncm91bmQ6cmdiYSgyMzEsNzYsNjAsLjIpO2NvbG9yOiNmZjgwODB9LmhjbntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjEpO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjUpfQoucmJhcntwYWRkaW5nOjlweCAxM3B4O2JvcmRlci1yYWRpdXM6OHB4O21hcmdpbi1ib3R0b206MTBweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo2MDB9Ci5yYi1ie2JhY2tncm91bmQ6dmFyKC0tZ2IpO2NvbG9yOiMwNTk2Njk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1nZCl9Ci5yYi1ze2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOiNkYzI2MjY7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1yZCl9Ci5yYi1ue2JhY2tncm91bmQ6I2Y4ZmFmYztjb2xvcjp2YXIoLS10Mik7Ym9yZGVyOnZhcigtLWJkcil9Ci5yYi13e2JhY2tncm91bmQ6I2ZlZjNjNztjb2xvcjojOTI0MDBlO2JvcmRlcjoxcHggc29saWQgI2ZkZTY4YX0KLyogQ09ORklERU5DRSAqLwouY3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTRweDtwYWRkaW5nOjRweCAwfQouY3J7cG9zaXRpb246cmVsYXRpdmU7d2lkdGg6NzJweDtoZWlnaHQ6NzJweDtmbGV4LXNocmluazowfQouY3Igc3Zne3RyYW5zZm9ybTpyb3RhdGUoLTkwZGVnKTtkaXNwbGF5OmJsb2NrfQouY2l7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQouY257Zm9udC1zaXplOjIycHg7Zm9udC13ZWlnaHQ6NzAwO2xpbmUtaGVpZ2h0OjF9LmNte2ZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjYwMH0KLmNke2ZsZXg6MX0uY2Rpcntmb250LXNpemU6MTZweDtmb250LXdlaWdodDo3MDA7bWFyZ2luLWJvdHRvbTozcHh9LmNkZXR7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDIpfQoucGlsbGFyc3ttYXJnaW4tdG9wOjEwcHh9Ci5wcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7cGFkZGluZzo3cHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcil9Ci5wcjpsYXN0LWNoaWxke2JvcmRlcjpub25lfQoucG57d2lkdGg6ODRweDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo2MDA7Y29sb3I6dmFyKC0tdDIpO2ZsZXgtc2hyaW5rOjB9Ci5wdHtmbGV4OjE7aGVpZ2h0OjVweDtiYWNrZ3JvdW5kOiNmMWY1Zjk7Ym9yZGVyLXJhZGl1czozcHg7b3ZlcmZsb3c6aGlkZGVufQoucGZ7aGVpZ2h0OjEwMCU7Ym9yZGVyLXJhZGl1czozcHg7dHJhbnNpdGlvbjp3aWR0aCAuNXN9Ci5wcHt3aWR0aDozNnB4O3RleHQtYWxpZ246cmlnaHQ7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2ZsZXgtc2hyaW5rOjB9Ci5pbmRze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmciAxZnI7Z2FwOjhweDttYXJnaW4tdG9wOjEwcHh9Ci5pbmR7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTBweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKX0KLmlse2ZvbnQtc2l6ZTo5cHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQzKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjVweDttYXJnaW4tYm90dG9tOjRweH0KLml2e2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnNie2hlaWdodDozcHg7YmFja2dyb3VuZDojZTJlOGYwO2JvcmRlci1yYWRpdXM6MnB4O292ZXJmbG93OmhpZGRlbjttYXJnaW4tdG9wOjhweH0KLnNme2hlaWdodDoxMDAlO2JhY2tncm91bmQ6dmFyKC0tYik7Ym9yZGVyLXJhZGl1czoycHg7dHJhbnNpdGlvbjp3aWR0aCAuNXN9Ci5zcntkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6NHB4fQovKiBQT1NJVElPTlMgKi8KLnBje2JvcmRlci1yYWRpdXM6MTJweDtwYWRkaW5nOjE0cHg7bWFyZ2luLWJvdHRvbToxMHB4fQoucGMtbHtiYWNrZ3JvdW5kOiNmMGZkZjQ7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1nZCl9LnBjLXN7YmFja2dyb3VuZDojZmZmNWY1O2JvcmRlcjoxcHggc29saWQgdmFyKC0tcmQpfS5wYy1ve2JhY2tncm91bmQ6dmFyKC0tYmIpO2JvcmRlcjoxcHggc29saWQgIzkzYzVmZH0KLnBoe2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxMHB4fQoucHN5bXtmb250LXNpemU6MTVweDtmb250LXdlaWdodDo3MDB9Ci5wYntwYWRkaW5nOjNweCAxMHB4O2JvcmRlci1yYWRpdXM6MjBweDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDB9Ci5wYmx7YmFja2dyb3VuZDp2YXIoLS1nKTtjb2xvcjojZmZmfS5wYnN7YmFja2dyb3VuZDp2YXIoLS1yKTtjb2xvcjojZmZmfS5wYmN7YmFja2dyb3VuZDp2YXIoLS1iKTtjb2xvcjojZmZmfS5wYnB7YmFja2dyb3VuZDojOGI1Y2Y2O2NvbG9yOiNmZmZ9Ci5wZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjZweH0KLnBpe2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuNzUpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6OHB4fQoucGlse2ZvbnQtc2l6ZTo5cHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQyKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjRweDttYXJnaW4tYm90dG9tOjJweH0KLnBpdntmb250LXNpemU6MTRweDtmb250LXdlaWdodDo3MDB9LnBpZ3tjb2xvcjp2YXIoLS1nKX0ucGlye2NvbG9yOnZhcigtLXIpfQovKiBXQUxMRVQgKi8KLnd0e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQoud2x7ZmxleDoxfS53bGJ7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQzKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjVweDttYXJnaW4tYm90dG9tOjRweH0KLndhe2ZvbnQtc2l6ZTozMnB4O2ZvbnQtd2VpZ2h0OjcwMDtsZXR0ZXItc3BhY2luZzotMXB4fS53c3tmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHh9Ci53cHtmb250LXNpemU6MThweDtmb250LXdlaWdodDo3MDA7dGV4dC1hbGlnbjpyaWdodH0ud257Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246cmlnaHQ7bWFyZ2luLXRvcDoycHh9Ci8qIFNUQVRTICovCi5zZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouc3RhdHtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKX0KLnN0bHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTo0cHh9Ci5zdHZ7Zm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6NzAwfQovKiBCVVRUT05TICovCi5iM3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbTo4cHh9Ci5idG57cGFkZGluZzoxM3B4IDZweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6bm9uZTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlcjtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Z2FwOjVweH0KLmJ0bjphY3RpdmV7b3BhY2l0eTouOH0KLmJke2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZn0uYnJ7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcik7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXJkKX0uYmJ7YmFja2dyb3VuZDp2YXIoLS1iYik7Y29sb3I6dmFyKC0tYik7Ym9yZGVyOjEuNXB4IHNvbGlkICNiZmRiZmV9Ci5iZnd7d2lkdGg6MTAwJTttYXJnaW4tYm90dG9tOjhweDtwYWRkaW5nOjE0cHh9Ci5iY2x7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcik7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXJkKTt3aWR0aDoxMDAlO3BhZGRpbmc6MTNweDtib3JkZXItcmFkaXVzOjhweDtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlcn0KLyogT1BUUyBUT0dHTEUgKi8KLnRvZ3Jvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MTBweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTttYXJnaW4tYm90dG9tOjEwcHh9Ci50bHtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo2MDB9LnRze2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjFweH0KLnRvZ3twb3NpdGlvbjpyZWxhdGl2ZTt3aWR0aDo0NnB4O2hlaWdodDoyNnB4O2ZsZXgtc2hyaW5rOjB9Ci50b2cgaW5wdXR7b3BhY2l0eTowO3dpZHRoOjA7aGVpZ2h0OjA7cG9zaXRpb246YWJzb2x1dGV9Ci50b2dzbHtwb3NpdGlvbjphYnNvbHV0ZTtpbnNldDowO2JhY2tncm91bmQ6I2UyZThmMDtib3JkZXItcmFkaXVzOjEzcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjouMnN9Ci50b2dzbDo6YmVmb3Jle2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7d2lkdGg6MjBweDtoZWlnaHQ6MjBweDtsZWZ0OjNweDtib3R0b206M3B4O2JhY2tncm91bmQ6I2ZmZjtib3JkZXItcmFkaXVzOjUwJTt0cmFuc2l0aW9uOi4ycztib3gtc2hhZG93OjAgMXB4IDNweCByZ2JhKDAsMCwwLC4yKX0KLnRvZyBpbnB1dDpjaGVja2VkICsgLnRvZ3Nse2JhY2tncm91bmQ6dmFyKC0tZyl9Ci50b2cgaW5wdXQ6Y2hlY2tlZCArIC50b2dzbDo6YmVmb3Jle3RyYW5zZm9ybTp0cmFuc2xhdGVYKDIwcHgpfQoub2luZm97ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjtnYXA6OHB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTBweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTttYXJnaW4tYm90dG9tOjEwcHg7Zm9udC1zaXplOjExcHh9Ci5vYntkaXNwbGF5OmZsZXg7Z2FwOjhweH0KLm9iYnRue2ZsZXg6MTtwYWRkaW5nOjEwcHg7Ym9yZGVyLXJhZGl1czo4cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXJ9Ci5vYmN7YmFja2dyb3VuZDp2YXIoLS1iYik7Y29sb3I6dmFyKC0tYik7Ym9yZGVyOjFweCBzb2xpZCAjYmZkYmZlfQoub2Jwe2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tcmQpfQoub2Jze2JhY2tncm91bmQ6I2ZlZjNjNztjb2xvcjp2YXIoLS15KTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci5vcmVze21hcmdpbi10b3A6MTBweDtwYWRkaW5nOjEwcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2ZvbnQtc2l6ZToxMXB4O2xpbmUtaGVpZ2h0OjEuNztib3JkZXI6dmFyKC0tYmRyKTtkaXNwbGF5Om5vbmV9Ci5tcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi10b3A6OHB4fQouYnRubHtmbGV4OjE7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1nKTtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlcn0KLmJ0bnN7ZmxleDoxO3BhZGRpbmc6MTNweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MS41cHggc29saWQgdmFyKC0tcik7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcik7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXJ9Ci5pbnB7d2lkdGg6MTAwJTtib3JkZXI6dmFyKC0tYmRyKTtib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjExcHggMTNweDtmb250LXNpemU6MTRweDtmb250LWZhbWlseTppbmhlcml0O291dGxpbmU6bm9uZTtiYWNrZ3JvdW5kOiNmOGZhZmM7bWFyZ2luLWJvdHRvbTo4cHh9Ci5pbnA6Zm9jdXN7Ym9yZGVyLWNvbG9yOnZhcigtLWcpO2JhY2tncm91bmQ6I2ZmZn0KLnRpe3BhZGRpbmc6MTFweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4fQoudGk6bGFzdC1jaGlsZHtib3JkZXI6bm9uZX0KLnRpY3t3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MTRweDtmbGV4LXNocmluazowO2ZvbnQtd2VpZ2h0OjcwMH0KLnRpbHtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0udGlze2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpfS50aWMye2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpfS50aXB7YmFja2dyb3VuZDojZjNlOGZmO2NvbG9yOiM3YzNhZWR9Ci50bXtmbGV4OjE7bWluLXdpZHRoOjB9LnRzeW17Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwfQoudG1ldGF7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4O3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KLnRye3RleHQtYWxpZ246cmlnaHQ7ZmxleC1zaHJpbms6MH0udHBubHtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo3MDB9Ci50cGd7Y29sb3I6dmFyKC0tZyl9LnRwcntjb2xvcjp2YXIoLS1yKX0udHBue2NvbG9yOnZhcigtLXQzKX0KLmxib3h7YmFja2dyb3VuZDojMGYxNzJhO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDttYXgtaGVpZ2h0OjQwMHB4O292ZXJmbG93LXk6YXV0b30KLmxye3BhZGRpbmc6NHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgIzFlMjkzYjtmb250LXNpemU6MTFweDtkaXNwbGF5OmZsZXg7Z2FwOjhweDtmb250LWZhbWlseTptb25vc3BhY2V9Ci5sdHtjb2xvcjojNDc1NTY5O3doaXRlLXNwYWNlOm5vd3JhcDtmbGV4LXNocmluazowfQoubEl7Y29sb3I6IzY0NzQ4Yn0ubFd7Y29sb3I6dmFyKC0teSl9LmxFe2NvbG9yOnZhcigtLXIpfS5sVHtjb2xvcjp2YXIoLS1nKTtmb250LXdlaWdodDo3MDB9Ci5sZntkaXNwbGF5OmZsZXg7Z2FwOjZweDttYXJnaW4tYm90dG9tOjhweH0KLmxmYntwYWRkaW5nOjRweCAxMnB4O2JvcmRlci1yYWRpdXM6MjBweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjYwMDtjdXJzb3I6cG9pbnRlcjtjb2xvcjp2YXIoLS10Myk7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmxmYi5vbntiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmY7Ym9yZGVyLWNvbG9yOnZhcigtLXQpfQouc3Jvd3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6ZmxleC1zdGFydDtwYWRkaW5nOjlweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKX0KLnNyb3c6bGFzdC1jaGlsZHtib3JkZXI6bm9uZX0KLnNre2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXQyKX0uc3Z7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLWcpO3RleHQtYWxpZ246cmlnaHQ7bWF4LXdpZHRoOjU1JX0KLmlwYm94e2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MThweDtmb250LXdlaWdodDo3MDA7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNHB4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtsZXR0ZXItc3BhY2luZzoycHg7bWFyZ2luLWJvdHRvbToxMHB4fQouZW1wdHl7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoyOHB4O2NvbG9yOnZhcigtLXQzKTtmb250LXNpemU6MTNweH0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGRpdiBjbGFzcz0iaGRyIj4KICA8ZGl2IGNsYXNzPSJsb2dvIj48ZGl2IGNsYXNzPSJsaWMiPkE8L2Rpdj48ZGl2PjxkaXYgY2xhc3M9ImxuIj5BbHBoYSBCb3Q8L2Rpdj48ZGl2IGNsYXNzPSJscyI+RGVsdGEgRXhjaGFuZ2UgSW5kaWE8L2Rpdj48L2Rpdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJwaWxsIHAtb2ZmIiBpZD0icGlsbCI+JiM5Njc5OyA8c3BhbiBpZD0icFR4dCI+U3RvcHBlZDwvc3Bhbj48L2Rpdj4KPC9kaXY+CjxkaXYgY2xhc3M9IndyYXAiPgoKPCEtLSBIT01FIC0tPgo8ZGl2IGNsYXNzPSJ0YWIgb24iIGlkPSJ0YWItaG9tZSI+CiAgPCEtLSBTZXR1cCBjYXJkIHNob3duIHdoZW4gbm90IGNvbm5lY3RlZCAtLT4KICA8ZGl2IGlkPSJzZXR1cENhcmQiIGNsYXNzPSJzZXR1cCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICA8aDI+Q29ubmVjdCB0byBTdGFydCBUcmFkaW5nPC9oMj4KICAgIDxwPkVudGVyIHlvdXIgRGVsdGEgRXhjaGFuZ2UgSW5kaWEgQVBJIGtleXMgaW4gU2V0dGluZ3MgdG8gY29ubmVjdCB0aGUgYm90LjwvcD4KICAgIDxkaXYgY2xhc3M9InNldHVwLXN0ZXBzIj4KICAgICAgPGRpdiBjbGFzcz0ic3RlcCI+PGRpdiBjbGFzcz0ic3RlcC1uIj4xPC9kaXY+PHNwYW4+T3BlbiA8Yj5TZXR0aW5nczwvYj4gdGFiIGJlbG93PC9zcGFuPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzdGVwIj48ZGl2IGNsYXNzPSJzdGVwLW4iPjI8L2Rpdj48c3Bhbj5FbnRlciB5b3VyIEFQSSBLZXkgJmFtcDsgU2VjcmV0PC9zcGFuPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzdGVwIj48ZGl2IGNsYXNzPSJzdGVwLW4iPjM8L2Rpdj48c3Bhbj5UYXAgPGI+Q29ubmVjdCB0byBEZWx0YSBFeGNoYW5nZTwvYj48L3NwYW4+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0ZXAiPjxkaXYgY2xhc3M9InN0ZXAtbiI+NDwvZGl2PjxzcGFuPldoaXRlbGlzdCBzZXJ2ZXIgSVAgc2hvd24gaW4gU2V0dGluZ3M8L3NwYW4+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImdvLWJ0biIgb25jbGljaz0ic3dpdGNoVGFiKCdzZXR0aW5ncycpIj5HbyB0byBTZXR0aW5ncyAmcmFycjs8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPGRpdiBjbGFzcz0iaGVybyI+CiAgICA8ZGl2IGNsYXNzPSJobCI+Qml0Y29pbiBMaXZlPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJocCIgaWQ9ImhQIj4kLS08L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImhyIj4KICAgICAgPHNwYW4gY2xhc3M9ImhjIGhjbiIgaWQ9ImhSIj4tLTwvc3Bhbj4KICAgICAgPHNwYW4gY2xhc3M9ImhjIGhjbiIgaWQ9ImhTIj4tLTwvc3Bhbj4KICAgICAgPHNwYW4gY2xhc3M9ImhjIGhjbiIgaWQ9ImhWIj4tLTwvc3Bhbj4KICAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9InJiYXIgcmItbiIgaWQ9InJCYXIiPlNjYW5uaW5nLi4uPC9kaXY+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJjdCI+Q29uZmlkZW5jZSBTY29yZTwvZGl2PgogICAgPGRpdiBjbGFzcz0iY3ciPgogICAgICA8ZGl2IGNsYXNzPSJjciI+CiAgICAgICAgPHN2ZyB2aWV3Qm94PSIwIDAgNzIgNzIiIHdpZHRoPSI3MiIgaGVpZ2h0PSI3MiI+CiAgICAgICAgICA8Y2lyY2xlIGN4PSIzNiIgY3k9IjM2IiByPSIyOCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZjFmNWY5IiBzdHJva2Utd2lkdGg9IjciLz4KICAgICAgICAgIDxjaXJjbGUgaWQ9ImNBcmMiIGN4PSIzNiIgY3k9IjM2IiByPSIyOCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDBiMzg2IiBzdHJva2Utd2lkdGg9IjciCiAgICAgICAgICAgIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWRhc2hhcnJheT0iMTc1LjkiIHN0cm9rZS1kYXNob2Zmc2V0PSIxNzUuOSIKICAgICAgICAgICAgc3R5bGU9InRyYW5zaXRpb246c3Ryb2tlLWRhc2hvZmZzZXQgLjZzLHN0cm9rZSAuM3MiLz4KICAgICAgICA8L3N2Zz4KICAgICAgICA8ZGl2IGNsYXNzPSJjaSI+PGRpdiBjbGFzcz0iY24iIGlkPSJjTnVtIj4tLTwvZGl2PjxkaXYgY2xhc3M9ImNtIj4vMTAwPC9kaXY+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJjZCI+PGRpdiBjbGFzcz0iY2RpciIgaWQ9ImNEaXIiPldBSVQ8L2Rpdj48ZGl2IGNsYXNzPSJjZGV0IiBpZD0iY0RldCI+SW5pdGlhbGl6aW5nLi4uPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBpbGxhcnMiIGlkPSJwaWxEaXYiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iaW5kcyI+CiAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkFEWDwvZGl2PjxkaXYgY2xhc3M9Iml2IiBpZD0iaUEiPi0tPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkJCIFdpZHRoPC9kaXY+PGRpdiBjbGFzcz0iaXYiIGlkPSJpQiI+LS08L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iaW5kIj48ZGl2IGNsYXNzPSJpbCI+QVRSICU8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlUIj4tLTwvZGl2PjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzYiI+PGRpdiBjbGFzcz0ic2YiIGlkPSJzRmlsbCIgc3R5bGU9IndpZHRoOjAlIj48L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNyIj48c3BhbiBpZD0ic1N0YXR1cyI+Tm90IHJ1bm5pbmc8L3NwYW4+PHNwYW4gaWQ9InNDZCIgc3R5bGU9ImZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1iKSI+LS08L3NwYW4+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBpZD0icGVycERpdiI+PC9kaXY+CiAgPGRpdiBpZD0ib3B0c0RpdiI+PC9kaXY+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJjdCI+V2FsbGV0PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJ3dCI+CiAgICAgIDxkaXYgY2xhc3M9IndsIj48ZGl2IGNsYXNzPSJ3bGIiPkJhbGFuY2U8L2Rpdj48ZGl2IGNsYXNzPSJ3YSIgaWQ9IndBbXQiPiQtLTwvZGl2PjxkaXYgY2xhc3M9IndzIiBpZD0id1N0Ij48L2Rpdj48L2Rpdj4KICAgICAgPGRpdj48ZGl2IGNsYXNzPSJ3cCIgaWQ9IndQY3QiPi0tJTwvZGl2PjxkaXYgY2xhc3M9InduIiBpZD0id1BubCI+UCZhbXA7TCAkLS08L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9InNnIj4KICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+V2luIFJhdGU8L2Rpdj48ZGl2IGNsYXNzPSJzdHYiIGlkPSJzV1IiPi0tPC9kaXY+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzdGF0Ij48ZGl2IGNsYXNzPSJzdGwiPlRyYWRlczwvZGl2PjxkaXYgY2xhc3M9InN0diIgaWQ9InNUUiI+MDwvZGl2PjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBjbGFzcz0ic3RsIj5TY2FuICM8L2Rpdj48ZGl2IGNsYXNzPSJzdHYiIHN0eWxlPSJjb2xvcjp2YXIoLS1iKSIgaWQ9InNTTiI+MDwvZGl2PjwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImIzIj4KICAgIDxidXR0b24gY2xhc3M9ImJ0biBiZCIgb25jbGljaz0iZG9TdGFydCgpIj4mIzk2NTQ7IFN0YXJ0PC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJidG4gYnIiIG9uY2xpY2s9ImRvU3RvcCgpIj4mIzk2MzI7IFN0b3A8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImJ0biBiYiIgb25jbGljaz0iZG9TY2FuKCkiPiYjOTg4OTsgUnVuPC9idXR0b24+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTBweCI+T3B0aW9ucyBNb2RlPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJ0b2dyb3ciPgogICAgICA8ZGl2PjxkaXYgY2xhc3M9InRsIj5FbmFibGUgT3B0aW9uczwvZGl2PjxkaXYgY2xhc3M9InRzIj5DYWxscywgUHV0cywgU3RyYWRkbGVzPC9kaXY+PC9kaXY+CiAgICAgIDxsYWJlbCBjbGFzcz0idG9nIj48aW5wdXQgdHlwZT0iY2hlY2tib3giIGlkPSJ0b2dPIiBvbmNoYW5nZT0idG9nZ2xlT3B0cyh0aGlzLmNoZWNrZWQpIj48c3BhbiBjbGFzcz0idG9nc2wiPjwvc3Bhbj48L2xhYmVsPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJvcHRzUGFuZWwiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgICA8ZGl2IGNsYXNzPSJvaW5mbyI+CiAgICAgICAgPGRpdj48ZGl2IHN0eWxlPSJmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tZykiPis4MCU8L2Rpdj48ZGl2IHN0eWxlPSJjb2xvcjp2YXIoLS10MykiPlRha2UgUHJvZml0PC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdj48ZGl2IHN0eWxlPSJmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tcikiPi01MCU8L2Rpdj48ZGl2IHN0eWxlPSJjb2xvcjp2YXIoLS10MykiPlN0b3AgTG9zczwvZGl2PjwvZGl2PgogICAgICAgIDxkaXY+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLWIpIj4tMzAlIHBrPC9kaXY+PGRpdiBzdHlsZT0iY29sb3I6dmFyKC0tdDMpIj5GbG9vciBUcmFpbDwvZGl2PjwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ib2IiPgogICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9iYyIgb25jbGljaz0iY2hrT3B0KCdjYWxsJykiPkNoZWNrIENBTEw8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJvYmJ0biBvYnAiIG9uY2xpY2s9ImNoa09wdCgncHV0JykiPkNoZWNrIFBVVDwvYnV0dG9uPgogICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9icyIgb25jbGljaz0iY2hrU3QoKSI+U3RyYWRkbGU8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgaWQ9Im9SZXMiIGNsYXNzPSJvcmVzIj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHgiPk1hbnVhbCBUcmFkZTwvZGl2PgogICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJtTG90cyIgdHlwZT0ibnVtYmVyIiBwbGFjZWhvbGRlcj0iTG90cyAoZGVmYXVsdCAxKSIgbWluPSIxIj4KICAgIDxkaXYgY2xhc3M9Im1yb3ciPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG5sIiBvbmNsaWNrPSJkb01hbnVhbCgnbG9uZycpIj4mIzg1OTM7IEJ1eSBMb25nPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImJ0bnMiIG9uY2xpY2s9ImRvTWFudWFsKCdzaG9ydCcpIj4mIzg1OTU7IFNlbGwgU2hvcnQ8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvZGl2PgogIDxidXR0b24gY2xhc3M9ImJjbCIgb25jbGljaz0iZG9DbG9zZUFsbCgpIj4mIzk4ODg7IENsb3NlIEFsbCBQb3NpdGlvbnM8L2J1dHRvbj4KPC9kaXY+Cgo8IS0tIFRSQURFUyAtLT4KPGRpdiBjbGFzcz0idGFiIiBpZD0idGFiLXRyYWRlcyI+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTJweCI+CiAgICAgIDxzcGFuIGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbjowIj5BbGwgVHJhZGVzPC9zcGFuPgogICAgICA8c3BhbiBpZD0idHJDbnRMYmwiIHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10MykiPjAgdHJhZGVzPC9zcGFuPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJ0ckxpc3QiPjxkaXYgY2xhc3M9ImVtcHR5Ij5ObyB0cmFkZXMgeWV0PC9kaXY+PC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPCEtLSBMT0dTIC0tPgo8ZGl2IGNsYXNzPSJ0YWIiIGlkPSJ0YWItbG9ncyI+CiAgPGRpdiBjbGFzcz0ibGYiPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIG9uIiBpZD0ibGZBIiBvbmNsaWNrPSJzZXRMRignJykiPkFsbDwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIiBpZD0ibGZUIiBvbmNsaWNrPSJzZXRMRignVFJBREUnKSI+VHJhZGVzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIiIGlkPSJsZlciIG9uY2xpY2s9InNldExGKCdXQVJOJykiPldhcm5pbmdzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIiIGlkPSJsZkUiIG9uY2xpY2s9InNldExGKCdFUlJPUicpIj5FcnJvcnM8L2J1dHRvbj4KICA8L2Rpdj4KICA8ZGl2IGlkPSJsQ250IiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi1ib3R0b206OHB4Ij4wIGVudHJpZXM8L2Rpdj4KICA8ZGl2IGNsYXNzPSJsYm94IiBpZD0ibEJveCI+PC9kaXY+CjwvZGl2PgoKPCEtLSBTRVRUSU5HUyAtLT4KPGRpdiBjbGFzcz0idGFiIiBpZD0idGFiLXNldHRpbmdzIj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5Db25uZWN0IHRvIERlbHRhIEV4Y2hhbmdlPC9kaXY+CiAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9ImFLZXkiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSJBUEkgS2V5Ij4KICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0iYVNlYyIgdHlwZT0icGFzc3dvcmQiIHBsYWNlaG9sZGVyPSJBUEkgU2VjcmV0Ij4KICAgIDxidXR0b24gY2xhc3M9ImJ0biBiZCBiZnciIG9uY2xpY2s9ImRvQ29ubmVjdCgpIj5Db25uZWN0IHRvIERlbHRhIEV4Y2hhbmdlPC9idXR0b24+CiAgICA8ZGl2IGlkPSJjTXNnIiBzdHlsZT0idGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjEycHg7bWFyZ2luLXRvcDo4cHg7bGluZS1oZWlnaHQ6MS44Ij48L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij5TZXJ2ZXIgSVAgJm1kYXNoOyBXaGl0ZWxpc3Qgb24gRGVsdGE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImlwYm94IiBpZD0ic0lQIj5Mb2FkaW5nLi4uPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bGluZS1oZWlnaHQ6MiI+CiAgICAgIDEuIENvcHkgdGhlIElQIGFib3ZlPGJyPgogICAgICAyLiBEZWx0YSBFeGNoYW5nZSBhcHAgJiM4NTk0OyBBY2NvdW50ICYjODU5NDsgQVBJIEtleXMgJiM4NTk0OyBFZGl0PGJyPgogICAgICAzLiBQYXN0ZSBpbnRvIElQIFdoaXRlbGlzdCBmaWVsZCAmIzg1OTQ7IFNhdmU8YnI+CiAgICAgIDQuIEFsc28gYWRkIGtleXMgdG8gPGI+UmVuZGVyICYjODU5NDsgRW52aXJvbm1lbnQ8L2I+IGZvciBhdXRvLWNvbm5lY3QKICAgIDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjRweCI+QWN0aXZlIEd1YXJkcmFpbHM8L2Rpdj4KICAgIDxkaXYgaWQ9ImdMaXN0Ij48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+CjwvZGl2PgoKPG5hdiBjbGFzcz0ibmF2Ij4KICA8YnV0dG9uIGNsYXNzPSJuYiBvbiIgaWQ9Im5iLWhvbWUiICAgICBvbmNsaWNrPSJzd2l0Y2hUYWIoJ2hvbWUnKSI+PHNwYW4gY2xhc3M9Im5pIj4mIzEyNzk2ODs8L3NwYW4+PHNwYW4gY2xhc3M9Im5sIj5Ib21lPC9zcGFuPjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9Im5iIiAgICBpZD0ibmItdHJhZGVzIiAgIG9uY2xpY2s9InN3aXRjaFRhYigndHJhZGVzJykiPjxzcGFuIGNsYXNzPSJuaSI+JiMxMjgyMDM7PC9zcGFuPjxzcGFuIGNsYXNzPSJubCI+VHJhZGVzPC9zcGFuPjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9Im5iIiAgICBpZD0ibmItbG9ncyIgICAgIG9uY2xpY2s9InN3aXRjaFRhYignbG9ncycpIj48c3BhbiBjbGFzcz0ibmkiPiYjMTI4MjIwOzwvc3Bhbj48c3BhbiBjbGFzcz0ibmwiPkxvZ3M8L3NwYW4+PC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibmIiICAgIGlkPSJuYi1zZXR0aW5ncyIgb25jbGljaz0ic3dpdGNoVGFiKCdzZXR0aW5ncycpIj48c3BhbiBjbGFzcz0ibmkiPiYjOTg4MTs8L3NwYW4+PHNwYW4gY2xhc3M9Im5sIj5TZXR0aW5nczwvc3Bhbj48L2J1dHRvbj4KPC9uYXY+Cgo8c2NyaXB0Pgp2YXIgR0wgPSB7bG9nczpbXSwgbG9nRjoiIiwgdHJhZGVzOltdLCBuZXh0QXQ6bnVsbCwgc3M6MzAwfTsKdmFyIFBDID0geyJSZWdpbWUiOiIjM2I4MmY2IiwiTVRGIEFsaWduIjoiIzAwYjM4NiIsIlJTSSI6IiNmNTllMGIiLCJNQUNEIjoiIzhiNWNmNiIsIlZvbGF0aWxpdHkiOiIjZWM0ODk5IiwiVm9sdW1lIjoiI2U3NGMzYyIsIlNlc3Npb24iOiIjMTRiOGE2In07CgpmdW5jdGlvbiBnZShpZCkgeyByZXR1cm4gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpOyB9CmZ1bmN0aW9uIHN0KGlkLHYpIHsgdmFyIGU9Z2UoaWQpOyBpZihlKSBlLnRleHRDb250ZW50PXY7IH0KZnVuY3Rpb24gc2goaWQsdikgeyB2YXIgZT1nZShpZCk7IGlmKGUpIGUuaW5uZXJIVE1MPXY7IH0KCmZ1bmN0aW9uIHN3aXRjaFRhYihuKSB7CiAgdmFyIHRhYnMgPSBbImhvbWUiLCJ0cmFkZXMiLCJsb2dzIiwic2V0dGluZ3MiXTsKICBmb3IgKHZhciBpPTA7IGk8dGFicy5sZW5ndGg7IGkrKykgewogICAgZ2UoInRhYi0iK3RhYnNbaV0pLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgdGFic1tpXT09PW4pOwogICAgZ2UoIm5iLSIrdGFic1tpXSkuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCB0YWJzW2ldPT09bik7CiAgfQogIGlmIChuPT09ImxvZ3MiKSByZW5kZXJMb2dzKCk7CiAgaWYgKG49PT0idHJhZGVzIikgcmVuZGVyVHJhZGVzKCk7Cn0KCmZ1bmN0aW9uIHhocih1cmwsIGJvZHksIGNiKSB7CiAgdmFyIHggPSBuZXcgWE1MSHR0cFJlcXVlc3QoKTsKICB2YXIgbWV0aG9kID0gYm9keSAhPT0gdW5kZWZpbmVkID8gIlBPU1QiIDogIkdFVCI7CiAgeC5vcGVuKG1ldGhvZCwgdXJsLCB0cnVlKTsKICBpZiAoYm9keSAhPT0gdW5kZWZpbmVkKSB4LnNldFJlcXVlc3RIZWFkZXIoIkNvbnRlbnQtVHlwZSIsImFwcGxpY2F0aW9uL2pzb24iKTsKICB4Lm9ucmVhZHlzdGF0ZWNoYW5nZSA9IGZ1bmN0aW9uKCkgewogICAgaWYgKHgucmVhZHlTdGF0ZSA9PT0gNCkgewogICAgICBpZiAoeC5zdGF0dXMgPT09IDIwMCkgewogICAgICAgIHRyeSB7IGNiKEpTT04ucGFyc2UoeC5yZXNwb25zZVRleHQpKTsgfQogICAgICAgIGNhdGNoKGUpIHsgY2IobnVsbCk7IH0KICAgICAgfSBlbHNlIHsgY2IobnVsbCk7IH0KICAgIH0KICB9OwogIHgub25lcnJvciA9IGZ1bmN0aW9uKCkgeyBjYihudWxsKTsgfTsKICB4LnNlbmQoYm9keSAhPT0gdW5kZWZpbmVkID8gSlNPTi5zdHJpbmdpZnkoYm9keSkgOiBudWxsKTsKfQoKZnVuY3Rpb24gZG9TdGFydCgpICAgIHsgeGhyKCIvYXBpL2JvdC9zdGFydCIsICAge30sIGZ1bmN0aW9uKCl7fSk7IH0KZnVuY3Rpb24gZG9TdG9wKCkgICAgIHsgeGhyKCIvYXBpL2JvdC9zdG9wIiwgICAge30sIGZ1bmN0aW9uKCl7fSk7IH0KZnVuY3Rpb24gZG9TY2FuKCkgICAgIHsgc3QoInNTdGF0dXMiLCJTY2FubmluZy4uLiIpOyB4aHIoIi9hcGkvYm90L3J1bl9ub3ciLHt9LGZ1bmN0aW9uKCl7fSk7IH0KZnVuY3Rpb24gZG9DbG9zZUFsbCgpIHsKICBpZiAoIWNvbmZpcm0oIkNsb3NlIEFMTCBvcGVuIHBvc2l0aW9ucz8iKSkgcmV0dXJuOwogIHhocigiL2FwaS9jbG9zZV9hbGwiLCB7fSwgZnVuY3Rpb24ocikgeyBhbGVydCgiQ2xvc2VkOiAiKyhyP3IuY2xvc2VkOjApKyIgcG9zaXRpb25zIik7IH0pOwp9CmZ1bmN0aW9uIGRvTWFudWFsKGRpcikgewogIHZhciBsb3RzID0gcGFyc2VJbnQoZ2UoIm1Mb3RzIikudmFsdWUpIHx8IDE7CiAgeGhyKCIvYXBpL21hbnVhbF90cmFkZSIsIHtkaXJlY3Rpb246ZGlyLGxvdHM6bG90c30sIGZ1bmN0aW9uKHIpIHsKICAgIGlmIChyICYmIHIuc3VjY2VzcykgYWxlcnQoZGlyLnRvVXBwZXJDYXNlKCkrIiAiK2xvdHMrIkxcbkVudHJ5ICQiK3IuZW50cnkrIlxuU3RvcCAkIityLnN0b3ArIlxuVFAgJCIrci50cCk7CiAgICBlbHNlIGFsZXJ0KCJGYWlsZWQ6ICIrKHI/ci5tZXNzYWdlOiJDaGVjayBMb2dzIHRhYiIpKTsKICB9KTsKfQpmdW5jdGlvbiBkb0Nvbm5lY3QoKSB7CiAgdmFyIGsgPSBnZSgiYUtleSIpLnZhbHVlLnRyaW0oKTsKICB2YXIgcyA9IGdlKCJhU2VjIikudmFsdWUudHJpbSgpOwogIGlmICghayB8fCAhcykgeyBzaCgiY01zZyIsIjxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS1yKSc+RW50ZXIgQVBJIGtleSBhbmQgc2VjcmV0PC9zcGFuPiIpOyByZXR1cm47IH0KICBzaCgiY01zZyIsIkNvbm5lY3RpbmcuLi4iKTsKICB4aHIoIi9hcGkvY29ubmVjdCIsIHthcGlfa2V5OmssYXBpX3NlY3JldDpzfSwgZnVuY3Rpb24ocikgewogICAgaWYgKHIgJiYgci5zdWNjZXNzKSB7CiAgICAgIHNoKCJjTXNnIiwiPHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLWcpJz5Db25uZWN0ZWQgJiMxMDAwMzsgQmFsYW5jZTogJCIrKHIuYmFsYW5jZXx8MCkudG9GaXhlZCgyKSsiPC9zcGFuPiIpOwogICAgfSBlbHNlIHsKICAgICAgdmFyIGlwID0gciYmci5zZXJ2ZXJfaXAgPyAiPGJyPjxzbWFsbD5TZXJ2ZXIgSVA6ICIrci5zZXJ2ZXJfaXArIjwvc21hbGw+IiA6ICIiOwogICAgICBzaCgiY01zZyIsIjxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS1yKSc+Iisocj9yLm1lc3NhZ2U6IkZhaWxlZCIpKyI8L3NwYW4+IitpcCsiPGJyPjxzbWFsbD48YSBocmVmPScvYXBpL2RlYnVnL2F1dGgnIHRhcmdldD0nX2JsYW5rJz5EZWJ1ZyBhdXRoIGRldGFpbHM8L2E+PC9zbWFsbD4iKTsKICAgIH0KICB9KTsKfQpmdW5jdGlvbiB0b2dnbGVPcHRzKG9uKSB7CiAgeGhyKCIvYXBpL29wdHMvdG9nZ2xlIiwge2VuYWJsZWQ6b259LCBmdW5jdGlvbihyKSB7CiAgICBnZSgib3B0c1BhbmVsIikuc3R5bGUuZGlzcGxheSA9IChyJiZyLm9wdHNfbW9kZSkgPyAiYmxvY2siIDogIm5vbmUiOwogIH0pOwp9CmZ1bmN0aW9uIGNoa09wdCh0KSB7CiAgdmFyIGVsID0gZ2UoIm9SZXMiKTsgZWwuc3R5bGUuZGlzcGxheT0iYmxvY2siOyBlbC50ZXh0Q29udGVudD0iQ2hlY2tpbmcuLi4iOwogIHhocigiL2FwaS9vcHRzL2ZpbmQiLCB7dHlwZTp0LGl0bTpmYWxzZX0sIGZ1bmN0aW9uKHIpIHsKICAgIGlmIChyICYmIHIuZm91bmQpIHsKICAgICAgZWwuaW5uZXJIVE1MID0gIjxiPiIrci5zeW1ib2wrIjwvYj48YnI+U3RyaWtlICQiKyhyLnN0cmlrZXx8MCkudG9Mb2NhbGVTdHJpbmcoKSsiIHwgTWFyayAkIisoci5tYXJrfHwwKS50b0ZpeGVkKDIpKyIgfCBQcmVtaXVtICQiKyhyLnByZW1pdW1fdXNkfHwwKS50b0ZpeGVkKDIpKyhyLml2PyIgfCBJViAiK3IuaXYrIiUiOiIiKSsiPGJyPiIrci5tb25leW5lc3MrIiB8IEV4cGlyeSAiK3IuZXhwaXJ5OwogICAgfSBlbHNlIHsKICAgICAgZWwudGV4dENvbnRlbnQgPSAiTm8gIit0KyIgb3B0aW9uIGZvdW5kLiBFeHBpcnk6ICIrKHImJnIuZXhwaXJ5fHwiPyIpOwogICAgfQogIH0pOwp9CmZ1bmN0aW9uIGNoa1N0KCkgewogIHZhciBlbCA9IGdlKCJvUmVzIik7IGVsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsgZWwudGV4dENvbnRlbnQ9IkNoZWNraW5nIHN0cmFkZGxlLi4uIjsKICB4aHIoIi9hcGkvb3B0cy9zdHJhZGRsZSIsIHt9LCBmdW5jdGlvbihyKSB7CiAgICBpZiAociAmJiByLmZvdW5kKSB7CiAgICAgIGVsLmlubmVySFRNTCA9ICI8Yj5TdHJhZGRsZSBmb3VuZDwvYj48YnI+VG90YWwgcHJlbWl1bTogJCIrKHIudG90YWxfcHJlbWl1bV91c2R8fDApLnRvRml4ZWQoMikrIjxicj5CcmVhay1ldmVuIFVQOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX3VwfHwwKS50b0xvY2FsZVN0cmluZygpKyIgfCBET1dOOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX2Rvd258fDApLnRvTG9jYWxlU3RyaW5nKCk7CiAgICB9IGVsc2UgewogICAgICBlbC50ZXh0Q29udGVudCA9ICJDYW5ub3QgYnVpbGQgc3RyYWRkbGUgcmlnaHQgbm93LiI7CiAgICB9CiAgfSk7Cn0KZnVuY3Rpb24gc2V0TEYoZikgewogIEdMLmxvZ0YgPSBmOwogIHZhciBtID0geyIiOiJsZkEiLCJUUkFERSI6ImxmVCIsIldBUk4iOiJsZlciLCJFUlJPUiI6ImxmRSJ9OwogIHZhciBrcyA9IE9iamVjdC5rZXlzKG0pOwogIGZvciAodmFyIGk9MDsgaTxrcy5sZW5ndGg7IGkrKykgeyB2YXIgZWw9Z2UobVtrc1tpXV0pOyBpZihlbCkgZWwuY2xhc3NMaXN0LnRvZ2dsZSgib24iLGtzW2ldPT09Zik7IH0KICByZW5kZXJMb2dzKCk7Cn0KCmZ1bmN0aW9uIHJlbmRlcihzKSB7CiAgaWYgKCFzKSByZXR1cm47CiAgdmFyIG9rID0gcy5jb25uZWN0ZWQgJiYgIXMuaGFsdGVkOwogIGdlKCJwaWxsIikuY2xhc3NOYW1lID0gInBpbGwgIiArIChvayA/ICJwLW9rIiA6ICJwLW9mZiIpOwogIHN0KCJwVHh0Iiwgcy5oYWx0ZWQgPyAiSEFMVEVEIiA6IHMuY29ubmVjdGVkID8gIkxpdmUiIDogIlN0b3BwZWQiKTsKICBnZSgic2V0dXBDYXJkIikuc3R5bGUuZGlzcGxheSA9IHMuY29ubmVjdGVkID8gIm5vbmUiIDogImJsb2NrIjsKICBzdCgiaFAiLCBzLnByaWNlID8gIiQiK3MucHJpY2UudG9Mb2NhbGVTdHJpbmcoKSA6ICIkLS0iKTsKICB2YXIgcmcgPSBzLnJlZ2ltZXx8IiI7CiAgdmFyIHJFbCA9IGdlKCJoUiIpOyByRWwudGV4dENvbnRlbnQ9cmd8fCItLSI7CiAgckVsLmNsYXNzTmFtZSA9ICJoYyAiICsgKHJnLmluZGV4T2YoIkJVTEwiKT49MD8iaGNnIjpyZy5pbmRleE9mKCJCRUFSIik+PTA/ImhjciI6ImhjbiIpOwogIHN0KCJoUyIsIHMuc3RyYXRlZ3l8fCItLSIpOwogIHN0KCJoViIsIHMudm9sX3JlZ2ltZXx8Ii0tIik7CiAgdmFyIHJiID0gZ2UoInJCYXIiKTsKICB2YXIgcmMgPSAicmItbiI7CiAgaWYgKHJnLmluZGV4T2YoIkJVTEwiKT49MCkgcmM9InJiLWIiOwogIGVsc2UgaWYgKHJnLmluZGV4T2YoIkJFQVIiKT49MCkgcmM9InJiLXMiOwogIGVsc2UgaWYgKHJnPT09IlNJREVXQVlTIikgcmM9InJiLXciOwogIHJiLmNsYXNzTmFtZSA9ICJyYmFyICIrcmM7CiAgcmIudGV4dENvbnRlbnQgPSByZyArICIgXHUyMDE0ICIgKyAocy5zdHJhdGVneXx8IkNhbGN1bGF0aW5nIik7CiAgdmFyIHNjID0gcy5jb25mX2xvbmd8fDA7CiAgc3QoImNOdW0iLCBzY3x8Ii0tIik7CiAgdmFyIGFyYyA9IGdlKCJjQXJjIik7CiAgYXJjLnN0eWxlLnN0cm9rZURhc2hvZmZzZXQgPSAxNzUuOS0oc2MvMTAwKjE3NS45KTsKICBhcmMuc3R5bGUuc3Ryb2tlID0gc2M+PTcwPyIjMDBiMzg2IjpzYz49NTA/IiNmNTllMGIiOiIjZTc0YzNjIjsKICBnZSgiY051bSIpLnN0eWxlLmNvbG9yID0gc2M+PTcwPyJ2YXIoLS1nKSI6c2M+PTUwPyJ2YXIoLS15KSI6InZhcigtLXIpIjsKICBzdCgiY0RpciIsIHMuc3RyYXRlZ3k9PT0iV0FJVCI/IldBSVQiOnJnKTsKICBzdCgiY0RldCIsICJTY29yZSAiK3NjKyIvMTAwIHwgQURYPSIrKHMuYWR4fHwwKSsiIHwgIisocy52b2xfcmVnaW1lfHwiIikpOwogIHZhciBwbHMgPSBzLnBpbGxhcnN8fHt9OyB2YXIgcGtzID0gT2JqZWN0LmtleXMocGxzKTsgdmFyIHBoPSIiOwogIGZvciAodmFyIGk9MDsgaTxwa3MubGVuZ3RoOyBpKyspIHsKICAgIHZhciBrPXBrc1tpXTsgdmFyIHY9cGxzW2tdOwogICAgdmFyIHBjdD12Lm0+MD9NYXRoLnJvdW5kKHYucy92Lm0qMTAwKTowOwogICAgdmFyIGNvbD1QQ1trXXx8InZhcigtLWcpIjsKICAgIHBoKz0iPGRpdiBjbGFzcz0ncHInPjxkaXYgY2xhc3M9J3BuJz4iK2srIjwvZGl2PjxkaXYgY2xhc3M9J3B0Jz48ZGl2IGNsYXNzPSdwZicgc3R5bGU9J3dpZHRoOiIrcGN0KyIlO2JhY2tncm91bmQ6Iitjb2wrIic+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncHAnIHN0eWxlPSdjb2xvcjoiK2NvbCsiJz4iK3YucysiLyIrdi5tKyI8L2Rpdj48L2Rpdj4iOwogIH0KICBzaCgicGlsRGl2IixwaCk7CiAgc3QoImlBIixzLmFkeHx8Ii0tIik7IHN0KCJpQiIscy5idz9zLmJ3KyIlIjoiLS0iKTsgc3QoImlUIixzLmF0cl9wY3Q/cy5hdHJfcGN0KyIlIjoiLS0iKTsKICBzdCgic1N0YXR1cyIscy5zdGF0dXN8fCItLSIpOyBzdCgic1NOIixzLnNjYW5fbnx8MCk7CiAgaWYgKHMubmV4dF9zY2FuKSBHTC5uZXh0QXQgPSBuZXcgRGF0ZShzLm5leHRfc2Nhbik7CiAgdmFyIHBwPXMub3Blbl9wb3N8fFtdOyB2YXIgcGgyPSIiOwogIGZvciAodmFyIGk9MDsgaTxwcC5sZW5ndGg7IGkrKykgewogICAgdmFyIHA9cHBbaV07IHZhciBuZWc9cC51cG5sPDA7CiAgICBwaDIrPSI8ZGl2IGNsYXNzPSdwYyBwYy0iKyhuZWc/InMiOiJsIikrIic+PGRpdiBjbGFzcz0ncGgnPjxzcGFuIGNsYXNzPSdwc3ltJz4iK3Auc3ltKyI8L3NwYW4+PHNwYW4gY2xhc3M9J3BiIHBiIisocC5zaWRlPT09ImxvbmciPyJsIjoicyIpKyInPiIrcC5zaWRlLnRvVXBwZXJDYXNlKCkrIjwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSdwZyc+IgogICAgICArIjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPkVudHJ5PC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIitwLmVudHJ5LnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5Mb3RzPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4iK3AubG90cysiPC9kaXY+PC9kaXY+IgogICAgICArIjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlVQTDwvZGl2PjxkaXYgY2xhc3M9J3BpdiAiKyhuZWc/InBpciI6InBpZyIpKyInPiIrKHAudXBubD49MD8iKyI6IiIpK3AudXBubCsiICgiKyhwLnBjdD49MD8iKyI6IiIpK3AucGN0KyIlKTwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5NYXJrPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIisocC5tYXJrfHxwLmVudHJ5KS50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+U3RvcDwvZGl2PjxkaXYgY2xhc3M9J3BpdiBwaXInPiQiK3Auc3RvcC50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+VFA8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgcGlnJz4kIitwLnRwLnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PiIKICAgICAgKyI8L2Rpdj48L2Rpdj4iOwogIH0KICBzaCgicGVycERpdiIscGgyKTsKICB2YXIgb3A9cy5vcHRzX3Bvc3x8W107IHZhciBvaD0iIjsKICBmb3IgKHZhciBpPTA7IGk8b3AubGVuZ3RoOyBpKyspIHsKICAgIHZhciBvPW9wW2ldOyB2YXIgaXNDPW8udHlwZT09PSJDQUxMIjsKICAgIG9oKz0iPGRpdiBjbGFzcz0ncGMgcGMtbyc+PGRpdiBjbGFzcz0ncGgnPjxzcGFuIGNsYXNzPSdwc3ltJyBzdHlsZT0nZm9udC1zaXplOjEycHgnPiIrby5zeW0rIjwvc3Bhbj48c3BhbiBjbGFzcz0ncGIgIisoaXNDPyJwYmMiOiJwYnAiKSsiJz4iK28udHlwZSsiPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9J3BnJz4iCiAgICAgICsiPGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+RW50cnk8L2Rpdj48ZGl2IGNsYXNzPSdwaXYnPiQiK28uZW50cnkrIjwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5NYXJrPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIitvLm1hcmsrIjwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5QJkw8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgIisoby5wY3Q8MD8icGlyIjoicGlnIikrIic+Iisoby5wY3Q+PTA/IisiOiIiKStvLnBjdCsiJTwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5QZWFrPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpZyc+JCIrby5wZWFrKyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPC9kaXY+PC9kaXY+IjsKICB9CiAgc2goIm9wdHNEaXYiLG9oKTsKICB2YXIgY2FwPXMuY2FwaXRhbHx8MDsgdmFyIHNjMj1zLnN0YXJ0X2NhcHx8MDsgdmFyIHBwMj1zLnBubF9wY3R8fDA7CiAgc3QoIndBbXQiLCBjYXA/IiQiK2NhcC50b0ZpeGVkKDIpOiIkLS0iKTsKICBzdCgid1N0IiwgIHNjMj8iU3RhcnRlZCAkIitzYzIudG9GaXhlZCgyKToiIik7CiAgdmFyIHdwPWdlKCJ3UGN0Iik7IHdwLnRleHRDb250ZW50PShwcDI+PTA/IisiOiIiKStwcDIudG9GaXhlZCgyKSsiJSI7IHdwLnN0eWxlLmNvbG9yPXBwMj49MD8idmFyKC0tZykiOiJ2YXIoLS1yKSI7CiAgc3QoIndQbmwiLCJQJkwgJCIrKHBwMj49MD8iKyI6IiIpKyhjYXAtc2MyKS50b0ZpeGVkKDIpKTsKICBzdCgic1dSIiwgIHMud2luX3JhdGUhPW51bGw/cy53aW5fcmF0ZSsiJSI6Ii0tIik7CiAgc3QoInNUUiIsICBzLnRvdGFsX3RyYWRlc3x8MCk7CiAgdmFyIG90PWdlKCJ0b2dPIik7IGlmKG90KSBvdC5jaGVja2VkPSEhcy5vcHRzX21vZGU7CiAgZ2UoIm9wdHNQYW5lbCIpLnN0eWxlLmRpc3BsYXkgPSBzLm9wdHNfbW9kZT8iYmxvY2siOiJub25lIjsKICBpZiAocy5ndWFyZHJhaWxzKSB7CiAgICB2YXIgZ2s9T2JqZWN0LmtleXMocy5ndWFyZHJhaWxzKTsgdmFyIGdoPSIiOwogICAgZm9yICh2YXIgaT0wOyBpPGdrLmxlbmd0aDsgaSsrKSBnaCs9IjxkaXYgY2xhc3M9J3Nyb3cnPjxzcGFuIGNsYXNzPSdzayc+Iitna1tpXSsiPC9zcGFuPjxzcGFuIGNsYXNzPSdzdic+IitzLmd1YXJkcmFpbHNbZ2tbaV1dKyI8L3NwYW4+PC9kaXY+IjsKICAgIHNoKCJnTGlzdCIsZ2gpOwogIH0KICBpZiAocy5sb2dzKSAgIEdMLmxvZ3MgICA9IHMubG9nczsKICBpZiAocy50cmFkZXMpIEdMLnRyYWRlcyA9IHMudHJhZGVzOwogIHN0KCJsQ250IiwgR0wubG9ncy5sZW5ndGgrIiBlbnRyaWVzIik7CiAgaWYgKGdlKCJ0YWItbG9ncyIpLmNsYXNzTGlzdC5jb250YWlucygib24iKSkgICByZW5kZXJMb2dzKCk7CiAgaWYgKGdlKCJ0YWItdHJhZGVzIikuY2xhc3NMaXN0LmNvbnRhaW5zKCJvbiIpKSByZW5kZXJUcmFkZXMoKTsKfQoKZnVuY3Rpb24gcmVuZGVyTG9ncygpIHsKICB2YXIgZiA9IEdMLmxvZ0YgPyBHTC5sb2dzLmZpbHRlcihmdW5jdGlvbihlKXtyZXR1cm4gZS5sPT09R0wubG9nRjt9KSA6IEdMLmxvZ3M7CiAgdmFyIGg9IiI7CiAgZm9yICh2YXIgaT0wOyBpPE1hdGgubWluKGYubGVuZ3RoLDE1MCk7IGkrKykgewogICAgdmFyIGU9ZltpXTsgdmFyIGNscz0ibEkiOwogICAgaWYoZS5sPT09IldBUk4iKWNscz0ibFciOyBlbHNlIGlmKGUubD09PSJFUlJPUiIpY2xzPSJsRSI7IGVsc2UgaWYoZS5sPT09IlRSQURFIiljbHM9ImxUIjsKICAgIGgrPSI8ZGl2IGNsYXNzPSdscic+PHNwYW4gY2xhc3M9J2x0Jz4iK2UudCsiPC9zcGFuPjxzcGFuIGNsYXNzPSciK2NscysiJz4iK2UubSsiPC9zcGFuPjwvZGl2PiI7CiAgfQogIHNoKCJsQm94IixoKTsKfQpmdW5jdGlvbiByZW5kZXJUcmFkZXMoKSB7CiAgc3QoInRyQ250TGJsIiwgR0wudHJhZGVzLmxlbmd0aCsiIHRyYWRlcyIpOwogIGlmICghR0wudHJhZGVzLmxlbmd0aCkgeyBzaCgidHJMaXN0IiwiPGRpdiBjbGFzcz0nZW1wdHknPk5vIHRyYWRlcyB5ZXQ8L2Rpdj4iKTsgcmV0dXJuOyB9CiAgdmFyIGg9IiI7CiAgZm9yICh2YXIgaT0wOyBpPEdMLnRyYWRlcy5sZW5ndGg7IGkrKykgewogICAgdmFyIHQ9R0wudHJhZGVzW2ldOyB2YXIgb3Blbj10LmV4aXQ9PW51bGw7CiAgICB2YXIgc2Q9dC5zaWRlfHwiIjsgdmFyIGljPXNkPT09ImxvbmciPyJ0aWwiOnNkPT09InNob3J0Ij8idGlzIjpzZD09PSJjYWxsIj8idGljMiI6InRpcCI7CiAgICB2YXIgaWNvPXNkPT09ImxvbmciPyImIzg1OTM7IjpzZD09PSJzaG9ydCI/IiYjODU5NTsiOnNkPT09ImNhbGwiPyJDIjoiUCI7CiAgICB2YXIgcGM9b3Blbj8idHBuIjoodC53b24/InRwZyI6InRwciIpOwogICAgdmFyIHB2PW9wZW4/Ik9wZW5cdTIwMjYiOih0Lndvbj8iKyI6IiIpKyh0LnBubHx8MCkudG9GaXhlZCg0KTsKICAgIHZhciB0bT10LnRpbWU/dC50aW1lLnN1YnN0cig1LDExKS5yZXBsYWNlKCJUIiwiICIpOiIiOwogICAgaCs9IjxkaXYgY2xhc3M9J3RpJz48ZGl2IGNsYXNzPSd0aWMgIitpYysiJz4iK2ljbysiPC9kaXY+PGRpdiBjbGFzcz0ndG0nPjxkaXYgY2xhc3M9J3RzeW0nPiIrKHQuc3ltfHwiQlRDVVNEIikrIjwvZGl2PjxkaXYgY2xhc3M9J3RtZXRhJz4iK3RtKyIgJm1pZGRvdDsgIisodC5yZWFzb258fCIiKSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ndHInPjxkaXYgY2xhc3M9J3RwbmwgIitwYysiJz4kIitwdisiPC9kaXY+PGRpdiBzdHlsZT0nZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpJz4iKyh0LmVudHJ5PyJAICQiK3QuZW50cnk6IiIpKyI8L2Rpdj48L2Rpdj48L2Rpdj4iOwogIH0KICBzaCgidHJMaXN0IixoKTsKfQoKc2V0SW50ZXJ2YWwoZnVuY3Rpb24oKSB7CiAgaWYgKCFHTC5uZXh0QXQpIHJldHVybjsKICB2YXIgZD1NYXRoLm1heCgwLE1hdGgucm91bmQoKEdMLm5leHRBdC1EYXRlLm5vdygpKS8xMDAwKSk7CiAgdmFyIG09TWF0aC5mbG9vcihkLzYwKTsgdmFyIHM9ZCU2MDsKICBzdCgic0NkIiwgZD4wPyhtKyJtICIrcysicyIpOiJTY2FubmluZyIpOwogIGdlKCJzRmlsbCIpLnN0eWxlLndpZHRoID0gTWF0aC5tYXgoMCwxMDAtZC9HTC5zcyoxMDApKyIlIjsKfSwxMDAwKTsKCmZ1bmN0aW9uIHBvbGwoKSB7IHhocigiL2FwaS9zdGF0dXMiLHVuZGVmaW5lZCxmdW5jdGlvbihzKXtpZihzKXJlbmRlcihzKTt9KTsgfQpwb2xsKCk7CnNldEludGVydmFsKHBvbGwsNDAwMCk7Cgp4aHIoIi9hcGkvaXAiLHVuZGVmaW5lZCxmdW5jdGlvbihyKXtzdCgic0lQIixyJiZyLmlwP3IuaXA6InVua25vd24iKTt9KTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPg==").decode("utf-8")

@app.route("/")
def index(): return Response(_DASHBOARD_HTML, mimetype="text/html")

if __name__ == "__main__":
    port=int(os.getenv("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)