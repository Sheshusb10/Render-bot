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
        start= end - mins*60*n
        # Try string resolution first, then integer (Delta India accepts both)
        for res_fmt in [res, mins]:
            d = self.get("/v2/history/candles",{
                "symbol":C.SYMBOL,"resolution":res_fmt,
                "start":start,"end":end})
            if d and d.get("success") and d.get("result"):
                return d.get("result",[])
            if d and not d.get("success"):
                log.warning(f"Delta candles {res_fmt}: {d.get('error',d.get('message','?'))}")
        log.warning(f"Delta candles {res} returned empty — will use Binance")
        return []

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
        """Binance public API — no auth needed. Best effort."""
        try:
            r = self.sess.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol":"BTCUSDT","interval":interval,"limit":limit},
                timeout=8)
            if r.status_code != 200:
                log.warning(f"Binance HTTP {r.status_code}")
                return []
            raw = r.json()
            if not isinstance(raw, list):
                return []
            out = []
            for c in raw:
                try:
                    out.append({
                        "close":  float(c[4]),
                        "high":   float(c[2]),
                        "low":    float(c[3]),
                        "volume": float(c[5]),
                        "open":   float(c[1]),
                    })
                except: pass
            log.info(f"Binance {interval}: {len(out)} candles OK")
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

        log.info(
            f"Candles: d1m={len(d1m)} d5m={len(d5m)} d15m={len(d15m)} "
            f"b1m={len(b1m)} b5m={len(b5m)} "
            f"using 5m={'delta('+str(len(c5m))+')' if d5m else 'binance('+str(len(c5m))+')'} "
            f"lead={bnc_lead}"
        )
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

        if len(c5m) < 30:
            return {"total":0, "veto":f"need_candles_have_{len(c5m)}",
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

        # ── Binance lead bonus (must be before total) ───────────────
        bnc_lead   = candles.get("binance_lead", "neutral")
        lead_bonus = 0
        if direction == "long"  and bnc_lead == "binance_leading_bull": lead_bonus = 8
        if direction == "short" and bnc_lead == "binance_leading_bear": lead_bonus = 8

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
        if len(cl) < 20: return {"score":0,"max":25,"detail":"no data"}
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
        if len(c5m) < 30:
            self.status=f"Fetching data: {len(c5m)} candles (need 30)"; return
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
_DASH = _b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEsbWF4aW11bS1zY2FsZT0xIj4KPHRpdGxlPkFscGhhIEJvdDwvdGl0bGU+CjxzdHlsZT4KKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowOy13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjp0cmFuc3BhcmVudH0KOnJvb3R7CiAgLS1nOiMwMGIzODY7LS1nYjojZThmOWYzOy0tZ2Q6I2E3ZjNkMDsKICAtLXI6I2U3NGMzYzstLXJiOiNmZWYyZjI7LS1yZDojZmNhNWE1OwogIC0teTojZjU5ZTBiOy0teWI6I2ZlZjNjNzsKICAtLWI6IzNiODJmNjstLWJiOiNlZmY2ZmY7CiAgLS10OiMwZjE3MmE7LS10MjojNjQ3NDhiOy0tdDM6Izk0YTNiODsKICAtLWJnOiNmMGYyZjU7LS13OiNmZmY7CiAgLS1iZHI6MXB4IHNvbGlkICNlMmU4ZjA7Cn0KYm9keXtiYWNrZ3JvdW5kOnZhcigtLWJnKTtjb2xvcjp2YXIoLS10KTtmb250LWZhbWlseTotYXBwbGUtc3lzdGVtLEJsaW5rTWFjU3lzdGVtRm9udCwiU2Vnb2UgVUkiLEhlbHZldGljYSxBcmlhbCxzYW5zLXNlcmlmO2ZvbnQtc2l6ZToxNHB4O21pbi1oZWlnaHQ6MTAwdmh9CgovKiBIRUFERVIgKi8KLmhkcntiYWNrZ3JvdW5kOnZhcigtLXcpO3BhZGRpbmc6MCAxNnB4O2hlaWdodDo1NHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47Ym9yZGVyLWJvdHRvbTp2YXIoLS1iZHIpO3Bvc2l0aW9uOnN0aWNreTt0b3A6MDt6LWluZGV4OjEwMDtib3gtc2hhZG93OjAgMXB4IDRweCByZ2JhKDAsMCwwLC4wNil9Ci5sb2dve2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHh9Ci5sb2dvLWljb3t3aWR0aDozNHB4O2hlaWdodDozNHB4O2JhY2tncm91bmQ6dmFyKC0tdCk7Ym9yZGVyLXJhZGl1czoxMHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtjb2xvcjojZmZmO2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzotMXB4fQoubG9nby10ZXh0IC5uYW1le2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjcwMDtsaW5lLWhlaWdodDoxLjJ9Ci5sb2dvLXRleHQgLnN1Yntmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10Myk7bGluZS1oZWlnaHQ6MS4yfQoucGlsbHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo1cHg7cGFkZGluZzo1cHggMTNweDtib3JkZXItcmFkaXVzOjIwcHg7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwfQoucGlsbC1saXZle2JhY2tncm91bmQ6dmFyKC0tZ2IpO2NvbG9yOnZhcigtLWcpfS5waWxsLW9mZntiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKX0ucGlsbC13YXJue2JhY2tncm91bmQ6dmFyKC0teWIpO2NvbG9yOnZhcigtLXkpfQouZG90e3dpZHRoOjZweDtoZWlnaHQ6NnB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6Y3VycmVudENvbG9yO2FuaW1hdGlvbjpibGluayAycyBpbmZpbml0ZX0KQGtleWZyYW1lcyBibGlua3swJSwxMDAle29wYWNpdHk6MX01MCV7b3BhY2l0eTouM319CgovKiBMQVlPVVQgKi8KLndyYXB7cGFkZGluZzoxMnB4IDE0cHggOTBweDttYXgtd2lkdGg6NDgwcHg7bWFyZ2luOjAgYXV0b30KLnBhZ2V7ZGlzcGxheTpub25lfS5wYWdlLnNob3d7ZGlzcGxheTpibG9ja30KLm5hdntwb3NpdGlvbjpmaXhlZDtib3R0b206MDtsZWZ0OjA7cmlnaHQ6MDtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci10b3A6dmFyKC0tYmRyKTtkaXNwbGF5OmZsZXg7cGFkZGluZzo4cHggMCBtYXgoOHB4LGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7ei1pbmRleDo5OTtib3gtc2hhZG93OjAgLTFweCA0cHggcmdiYSgwLDAsMCwuMDQpfQoubmJ7ZmxleDoxO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47YWxpZ24taXRlbXM6Y2VudGVyO2dhcDozcHg7cGFkZGluZzo0cHggMDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOm5vbmU7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLm5iIC5pY3tmb250LXNpemU6MjBweDtjb2xvcjp2YXIoLS10Myl9Lm5iIC5sYntmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi42cHh9Ci5uYi5vbiAuaWMsLm5iLm9uIC5sYntjb2xvcjp2YXIoLS10KX0KLmNhcmR7YmFja2dyb3VuZDp2YXIoLS13KTtib3JkZXItcmFkaXVzOjEycHg7cGFkZGluZzoxNnB4O21hcmdpbi1ib3R0b206MTBweDtib3gtc2hhZG93OjAgMXB4IDNweCByZ2JhKDAsMCwwLC4wNSksMCAycHggOHB4IHJnYmEoMCwwLDAsLjA0KX0KLmNhcmQtdGl0bGV7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQyKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjZweH0KCi8qIENPTk5FQ1QgU0NSRUVOICovCi5jb25uZWN0LXNjcmVlbntiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNjBkZWcsIzBmMTcyYSAwJSwjMWUzYTVmIDEwMCUpO2JvcmRlci1yYWRpdXM6MTZweDtwYWRkaW5nOjI0cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouY3MtdGl0bGV7Zm9udC1zaXplOjIycHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOiNmZmY7bWFyZ2luLWJvdHRvbTo2cHg7bGluZS1oZWlnaHQ6MS4zfQouY3Mtc3Vie2ZvbnQtc2l6ZToxM3B4O2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjUpO21hcmdpbi1ib3R0b206MjBweDtsaW5lLWhlaWdodDoxLjZ9Ci5jcy1pcC1yb3d7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTJweCAxNHB4O21hcmdpbi1ib3R0b206MTZweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVufQouY3MtaXAtbGJse2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC40KTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjZweDttYXJnaW4tYm90dG9tOjRweH0KLmNzLWlwLXZhbHtmb250LWZhbWlseTptb25vc3BhY2U7Zm9udC1zaXplOjE3cHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOiNmZmY7bGV0dGVyLXNwYWNpbmc6MXB4fQouY3MtaXAtY29weXtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjEyKTtib3JkZXI6bm9uZTtib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjhweCAxNHB4O2NvbG9yOiNmZmY7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5jcy1pcC1jb3B5OmFjdGl2ZXtvcGFjaXR5Oi43fQouY3Mtc3RlcHN7bWFyZ2luLWJvdHRvbToxOHB4fQouY3Mtc3RlcHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O3BhZGRpbmc6N3B4IDA7Zm9udC1zaXplOjEycHg7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNjUpfQouY3Mtc3RlcC1ue3dpZHRoOjIycHg7aGVpZ2h0OjIycHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4xMik7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC44KTtmbGV4LXNocmluazowfQouY3MtaW5wdXR7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA4KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7Zm9udC1zaXplOjE0cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtjb2xvcjojZmZmO21hcmdpbi1ib3R0b206MTBweDtvdXRsaW5lOm5vbmV9Ci5jcy1pbnB1dDpmb2N1c3tib3JkZXItY29sb3I6dmFyKC0tZyk7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4xMil9Ci5jcy1pbnB1dDo6cGxhY2Vob2xkZXJ7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMyl9Ci5jcy1idG57d2lkdGg6MTAwJTtwYWRkaW5nOjE0cHg7Ym9yZGVyLXJhZGl1czoxMHB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tZyk7Y29sb3I6I2ZmZjtmb250LXNpemU6MTVweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdDtsZXR0ZXItc3BhY2luZzouM3B4fQouY3MtYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmNzLW1zZ3t0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MTJweDttYXJnaW4tdG9wOjEwcHg7bWluLWhlaWdodDoyMHB4O2xpbmUtaGVpZ2h0OjEuN30KLmNzLW1zZy5va3tjb2xvcjojNGFkZTgwfS5jcy1tc2cuZXJye2NvbG9yOiNmODcxNzF9Ci5jcy1zYXZlZHtiYWNrZ3JvdW5kOnJnYmEoMCwyMDAsMTUwLC4xKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMCwyMDAsMTUwLC4yKTtib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjhweCAxMnB4O2ZvbnQtc2l6ZToxMXB4O2NvbG9yOiM0YWRlODA7bWFyZ2luLWJvdHRvbToxNHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KCi8qIEhFUk8gKi8KLmhlcm97YmFja2dyb3VuZDp2YXIoLS10KTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMHB4O21hcmdpbi1ib3R0b206MTBweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59Ci5oZXJvOjphZnRlcntjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO3RvcDotNDBweDtyaWdodDotNDBweDt3aWR0aDoxNjBweDtoZWlnaHQ6MTYwcHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5oZXJvLWxibHtmb250LXNpemU6MTBweDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC40KTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjhweDttYXJnaW4tYm90dG9tOjVweH0KLmhlcm8tcHJpY2V7Zm9udC1zaXplOjQwcHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOiNmZmY7bGluZS1oZWlnaHQ6MTtsZXR0ZXItc3BhY2luZzotMS41cHh9Ci5oZXJvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7bWFyZ2luLXRvcDo5cHg7ZmxleC13cmFwOndyYXB9Ci5jaGlwe3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czo2cHg7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwfQouY2hpcC1ne2JhY2tncm91bmQ6cmdiYSgwLDIwMCwxNTAsLjIpO2NvbG9yOiMwMGU4YjB9LmNoaXAtcntiYWNrZ3JvdW5kOnJnYmEoMjMxLDc2LDYwLC4yKTtjb2xvcjojZmY4MDgwfS5jaGlwLW57YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSl9CgovKiBSRUdJTUUgQkFSICovCi5yZWdpbWV7cGFkZGluZzo5cHggMTRweDtib3JkZXItcmFkaXVzOjhweDttYXJnaW4tYm90dG9tOjEwcHg7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwfQoucmVnLWJ1bGx7YmFja2dyb3VuZDp2YXIoLS1nYik7Y29sb3I6IzA1OTY2OTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWdkKX0KLnJlZy1iZWFye2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOiNkYzI2MjY7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1yZCl9Ci5yZWctbmV1e2JhY2tncm91bmQ6I2Y4ZmFmYztjb2xvcjp2YXIoLS10Mik7Ym9yZGVyOnZhcigtLWJkcil9Ci5yZWctc2lkZXtiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjojOTI0MDBlO2JvcmRlcjoxcHggc29saWQgI2ZkZTY4YX0KCi8qIENPTkZJREVOQ0UgKi8KLmNvbmYtd3JhcHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNHB4O3BhZGRpbmc6NHB4IDB9Ci5jb25mLXJpbmd7cG9zaXRpb246cmVsYXRpdmU7d2lkdGg6NzJweDtoZWlnaHQ6NzJweDtmbGV4LXNocmluazowfQouY29uZi1yaW5nIHN2Z3t0cmFuc2Zvcm06cm90YXRlKC05MGRlZyk7ZGlzcGxheTpibG9ja30KLmNvbmYtb3Zlcntwb3NpdGlvbjphYnNvbHV0ZTtpbnNldDowO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXJ9Ci5jb25mLW51bXtmb250LXNpemU6MjJweDtmb250LXdlaWdodDo4MDA7bGluZS1oZWlnaHQ6MX0KLmNvbmYtZGVub217Zm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS10Myk7Zm9udC13ZWlnaHQ6NzAwfQouY29uZi1tZXRhe2ZsZXg6MX0KLmNvbmYtZGlye2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tYm90dG9tOjNweH0KLmNvbmYtc3Vie2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQyKX0KLnBpbGxhcnN7bWFyZ2luLXRvcDoxMnB4O2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjB9Ci5wcm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDtwYWRkaW5nOjdweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKX0KLnByb3c6bGFzdC1jaGlsZHtib3JkZXI6bm9uZX0KLnBuYW1le3dpZHRoOjg2cHg7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQyKTtmbGV4LXNocmluazowfQoucHRyYWNre2ZsZXg6MTtoZWlnaHQ6NXB4O2JhY2tncm91bmQ6I2YxZjVmOTtib3JkZXItcmFkaXVzOjNweDtvdmVyZmxvdzpoaWRkZW59Ci5wZmlsbHtoZWlnaHQ6MTAwJTtib3JkZXItcmFkaXVzOjNweDt0cmFuc2l0aW9uOndpZHRoIC42cyBlYXNlfQoucHNjb3Jle3dpZHRoOjM2cHg7dGV4dC1hbGlnbjpyaWdodDtmb250LXNpemU6MTBweDtmb250LXdlaWdodDo4MDA7ZmxleC1zaHJpbms6MH0KLmluZC1ncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmciAxZnI7Z2FwOjhweDttYXJnaW4tdG9wOjEwcHh9Ci5pbmR7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTBweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKX0KLmluZC1sYmx7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDMpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNXB4O21hcmdpbi1ib3R0b206M3B4fQouaW5kLXZhbHtmb250LXNpemU6MTZweDtmb250LXdlaWdodDo4MDB9Ci5zY2FuLWJhcntoZWlnaHQ6M3B4O2JhY2tncm91bmQ6I2UyZThmMDtib3JkZXItcmFkaXVzOjJweDtvdmVyZmxvdzpoaWRkZW47bWFyZ2luLXRvcDo5cHh9Ci5zY2FuLWZpbGx7aGVpZ2h0OjEwMCU7YmFja2dyb3VuZDp2YXIoLS1iKTtib3JkZXItcmFkaXVzOjJweDt0cmFuc2l0aW9uOndpZHRoIC41c30KLnNjYW4tcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbjtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDo0cHh9CgovKiBQT1NJVElPTlMgKi8KLnBvc3tib3JkZXItcmFkaXVzOjEycHg7cGFkZGluZzoxNHB4O21hcmdpbi1ib3R0b206MTBweDtib3JkZXI6dmFyKC0tYmRyKX0KLnBvcy1sb25ne2JhY2tncm91bmQ6I2YwZmRmNDtib3JkZXItY29sb3I6dmFyKC0tZ2QpfS5wb3Mtc2hvcnR7YmFja2dyb3VuZDojZmZmNWY1O2JvcmRlci1jb2xvcjp2YXIoLS1yZCl9LnBvcy1vcHR7YmFja2dyb3VuZDp2YXIoLS1iYik7Ym9yZGVyLWNvbG9yOiM5M2M1ZmR9Ci5wb3MtaGR7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjEwcHh9Ci5wb3Mtc3lte2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJhZGdle3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJhZGdlLWx7YmFja2dyb3VuZDp2YXIoLS1nKTtjb2xvcjojZmZmfS5iYWRnZS1ze2JhY2tncm91bmQ6dmFyKC0tcik7Y29sb3I6I2ZmZn0uYmFkZ2UtY3tiYWNrZ3JvdW5kOnZhcigtLWIpO2NvbG9yOiNmZmZ9LmJhZGdlLXB7YmFja2dyb3VuZDojOGI1Y2Y2O2NvbG9yOiNmZmZ9Ci5wb3MtZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjZweH0KLnBvcy1jZWxse2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuNzUpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6OHB4fQoucG9zLWNlbGwtbGJse2ZvbnQtc2l6ZTo5cHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQyKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjRweDttYXJnaW4tYm90dG9tOjJweH0KLnBvcy1jZWxsLXZhbHtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo4MDB9LnBjZ3tjb2xvcjp2YXIoLS1nKX0ucGNye2NvbG9yOnZhcigtLXIpfQoKLyogV0FMTEVUICovCi53YWx7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmZsZXgtc3RhcnR9Ci53YWwtbHtmbGV4OjF9Ci53YWwtbGJse2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTo0cHh9Ci53YWwtYW10e2ZvbnQtc2l6ZTozMnB4O2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzotMXB4fQoud2FsLXN0YXJ0e2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweH0KLndhbC1wY3R7Zm9udC1zaXplOjE4cHg7Zm9udC13ZWlnaHQ6ODAwO3RleHQtYWxpZ246cmlnaHR9Ci53YWwtcG5se2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOnJpZ2h0O21hcmdpbi10b3A6MnB4fQoKLyogU1RBVFMgKi8KLnN0YXRze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmciAxZnI7Z2FwOjhweDttYXJnaW4tYm90dG9tOjEwcHh9Ci5zdGF0e2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzoxMnB4O3RleHQtYWxpZ246Y2VudGVyO2JvcmRlcjp2YXIoLS1iZHIpfQouc3RhdC1sYmx7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDMpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNXB4O21hcmdpbi1ib3R0b206NHB4fQouc3RhdC12YWx7Zm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwfQoKLyogQlVUVE9OUyAqLwouYnRuLXJvd3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouYnRue3BhZGRpbmc6MTNweCA2cHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2dhcDo1cHg7bGV0dGVyLXNwYWNpbmc6LjNweH0KLmJ0bjphY3RpdmV7b3BhY2l0eTouOH0KLmJ0bi1zdGFydHtiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmZ9Ci5idG4tc3RvcHtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MS41cHggc29saWQgdmFyKC0tcmQpfQouYnRuLXJ1bntiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtib3JkZXI6MS41cHggc29saWQgI2JmZGJmZX0KLmJ0bi1jbG9zZS1hbGx7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcik7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXJkKTt3aWR0aDoxMDAlO3BhZGRpbmc6MTRweDtib3JkZXItcmFkaXVzOjlweDtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMDtjdXJzb3I6cG9pbnRlcn0KCi8qIE9QVElPTlMgVE9HR0xFICovCi5vcHRzLXRvZ2dsZXtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MTBweCAwO21hcmdpbi1ib3R0b206MTJweDtib3JkZXItYm90dG9tOnZhcigtLWJkcil9Ci5vcHRzLXRvZ2dsZS10ZXh0IC5sYWJlbHtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo3MDB9Ci5vcHRzLXRvZ2dsZS10ZXh0IC5zdWJ7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4fQoudG9ne3Bvc2l0aW9uOnJlbGF0aXZlO3dpZHRoOjQ2cHg7aGVpZ2h0OjI2cHg7ZmxleC1zaHJpbms6MDtjdXJzb3I6cG9pbnRlcn0KLnRvZyBpbnB1dHtvcGFjaXR5OjA7d2lkdGg6MDtoZWlnaHQ6MDtwb3NpdGlvbjphYnNvbHV0ZX0KLnRvZy1zbHtwb3NpdGlvbjphYnNvbHV0ZTtpbnNldDowO2JhY2tncm91bmQ6I2UyZThmMDtib3JkZXItcmFkaXVzOjEzcHg7dHJhbnNpdGlvbjouMnN9Ci50b2ctc2w6OmJlZm9yZXtjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO3dpZHRoOjIwcHg7aGVpZ2h0OjIwcHg7bGVmdDozcHg7Ym90dG9tOjNweDtiYWNrZ3JvdW5kOiNmZmY7Ym9yZGVyLXJhZGl1czo1MCU7dHJhbnNpdGlvbjouMnM7Ym94LXNoYWRvdzowIDFweCAzcHggcmdiYSgwLDAsMCwuMil9Ci50b2cgaW5wdXQ6Y2hlY2tlZCsudG9nLXNse2JhY2tncm91bmQ6dmFyKC0tZyl9Ci50b2cgaW5wdXQ6Y2hlY2tlZCsudG9nLXNsOjpiZWZvcmV7dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMjBweCl9Ci5vcHRzLWluZm8tcm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmciAxZnI7Z2FwOjhweDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjEwcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7bWFyZ2luLWJvdHRvbToxMnB4O2ZvbnQtc2l6ZToxMXB4fQoub3B0cy1idG5ze2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi1ib3R0b206MH0KLm9wdC1idG57ZmxleDoxO3BhZGRpbmc6MTBweDtib3JkZXItcmFkaXVzOjhweDtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlcn0KLm9iLWNhbGx7YmFja2dyb3VuZDp2YXIoLS1iYik7Y29sb3I6dmFyKC0tYik7Ym9yZGVyOjFweCBzb2xpZCAjYmZkYmZlfQoub2ItcHV0e2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tcmQpfQoub2Itc3R7YmFja2dyb3VuZDp2YXIoLS15Yik7Y29sb3I6dmFyKC0teSk7Ym9yZGVyOjFweCBzb2xpZCAjZmRlNjhhfQoub3B0LXJlc3VsdHttYXJnaW4tdG9wOjEwcHg7cGFkZGluZzoxMXB4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtmb250LXNpemU6MTFweDtsaW5lLWhlaWdodDoxLjg7Ym9yZGVyOnZhcigtLWJkcik7ZGlzcGxheTpub25lfQoKLyogTUFOVUFMIFRSQURFICovCi5pbnB7d2lkdGg6MTAwJTtib3JkZXI6dmFyKC0tYmRyKTtib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjExcHggMTNweDtmb250LXNpemU6MTRweDtmb250LWZhbWlseTppbmhlcml0O291dGxpbmU6bm9uZTtiYWNrZ3JvdW5kOiNmOGZhZmM7bWFyZ2luLWJvdHRvbTo4cHh9Ci5pbnA6Zm9jdXN7Ym9yZGVyLWNvbG9yOnZhcigtLWcpO2JhY2tncm91bmQ6I2ZmZn0KLm1hbnVhbC1idG5ze2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi10b3A6MH0KLmJ0bi1sb25ne2ZsZXg6MTtwYWRkaW5nOjEzcHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLWcpO2JhY2tncm91bmQ6dmFyKC0tZ2IpO2NvbG9yOnZhcigtLWcpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyfQouYnRuLXNob3J0e2ZsZXg6MTtwYWRkaW5nOjEzcHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXIpO2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyfQoKLyogVFJBREVTICovCi50cmFkZS1yb3d7cGFkZGluZzoxMXB4IDA7Ym9yZGVyLWJvdHRvbTp2YXIoLS1iZHIpO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHh9Ci50cmFkZS1yb3c6bGFzdC1jaGlsZHtib3JkZXI6bm9uZX0KLnRyYWRlLWljb3t3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo4MDA7ZmxleC1zaHJpbms6MH0KLnQtbG9uZ3tiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0udC1zaG9ydHtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKX0udC1jYWxse2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpfS50LXB1dHtiYWNrZ3JvdW5kOiNmM2U4ZmY7Y29sb3I6IzdjM2FlZH0KLnRyYWRlLW1pZHtmbGV4OjE7bWluLXdpZHRoOjB9Ci50cmFkZS1zeW17Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwfQoudHJhZGUtbWV0YXtmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoxcHg7d2hpdGUtc3BhY2U6bm93cmFwO292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzfQoudHJhZGUtcmlnaHR7dGV4dC1hbGlnbjpyaWdodDtmbGV4LXNocmluazowfQoudHJhZGUtcG5se2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMH0udHBne2NvbG9yOnZhcigtLWcpfS50cHJ7Y29sb3I6dmFyKC0tcil9LnRwbntjb2xvcjp2YXIoLS10Myl9Ci5lbXB0eXt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjI4cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtc2l6ZToxM3B4fQoKLyogTE9HUyAqLwoubG9nLWZpbHRlcnN7ZGlzcGxheTpmbGV4O2dhcDo2cHg7bWFyZ2luLWJvdHRvbTo4cHh9Ci5sb2ctZmJ0bntwYWRkaW5nOjRweCAxMnB4O2JvcmRlci1yYWRpdXM6MjBweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlcjtjb2xvcjp2YXIoLS10Myk7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmxvZy1mYnRuLm9ue2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtib3JkZXItY29sb3I6dmFyKC0tdCl9Ci5sb2ctYm94e2JhY2tncm91bmQ6IzBmMTcyYTtib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjEycHg7bWF4LWhlaWdodDo0MjBweDtvdmVyZmxvdy15OmF1dG99Ci5sb2ctZW50cnl7cGFkZGluZzo0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMWUyOTNiO2ZvbnQtc2l6ZToxMXB4O2Rpc3BsYXk6ZmxleDtnYXA6OHB4O2ZvbnQtZmFtaWx5Om1vbm9zcGFjZX0KLmxvZy10aW1le2NvbG9yOiM0NzU1Njk7d2hpdGUtc3BhY2U6bm93cmFwO2ZsZXgtc2hyaW5rOjB9Ci5sb2ctSXtjb2xvcjojNjQ3NDhifS5sb2ctV3tjb2xvcjp2YXIoLS15KX0ubG9nLUV7Y29sb3I6dmFyKC0tcil9LmxvZy1Ue2NvbG9yOnZhcigtLWcpO2ZvbnQtd2VpZ2h0OjcwMH0KCi8qIFNFVFRJTkdTIChzbWFsbCwgZm9yIGd1YXJkcmFpbHMgb25seSkgKi8KLmd1YXJkLXJvd3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6ZmxleC1zdGFydDtwYWRkaW5nOjlweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKX0KLmd1YXJkLXJvdzpsYXN0LWNoaWxke2JvcmRlcjpub25lfQouZ3VhcmQta2V5e2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXQyKX0uZ3VhcmQtdmFse2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS1nKTt0ZXh0LWFsaWduOnJpZ2h0O21heC13aWR0aDo1NSV9Ci5kaXNjb25uZWN0LWJ0bnt3aWR0aDoxMDAlO3BhZGRpbmc6MTJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2NvbG9yOnZhcigtLXIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6OHB4fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5PgoKPCEtLSBIRUFERVIgLS0+CjxkaXYgY2xhc3M9ImhkciI+CiAgPGRpdiBjbGFzcz0ibG9nbyI+CiAgICA8ZGl2IGNsYXNzPSJsb2dvLWljbyI+JiM5MTY7PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJsb2dvLXRleHQiPgogICAgICA8ZGl2IGNsYXNzPSJuYW1lIj5BbHBoYSBCb3Q8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ic3ViIj5EZWx0YSBFeGNoYW5nZSBJbmRpYTwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0icGlsbCBwaWxsLW9mZiIgaWQ9InN0YXR1c1BpbGwiPgogICAgPHNwYW4gY2xhc3M9ImRvdCI+PC9zcGFuPgogICAgPHNwYW4gaWQ9InBpbGxMYWJlbCI+U3RvcHBlZDwvc3Bhbj4KICA8L2Rpdj4KPC9kaXY+Cgo8ZGl2IGNsYXNzPSJ3cmFwIj4KCjwhLS0g4pWQ4pWQ4pWQIEhPTUUgUEFHRSDilZDilZDilZAgLS0+CjxkaXYgY2xhc3M9InBhZ2Ugc2hvdyIgaWQ9InBhZ2UtaG9tZSI+CgogIDwhLS0gQ09OTkVDVCBTQ1JFRU4gKHNob3duIHdoZW4gbm90IGNvbm5lY3RlZCkgLS0+CiAgPGRpdiBpZD0iY29ubmVjdFNjcmVlbiIgY2xhc3M9ImNvbm5lY3Qtc2NyZWVuIj4KICAgIDxkaXYgY2xhc3M9ImNzLXRpdGxlIj5Db25uZWN0IEFscGhhIEJvdDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY3Mtc3ViIj5FbnRlciB5b3VyIERlbHRhIEV4Y2hhbmdlIEluZGlhIEFQSSBrZXlzLiBUaGV5IGFyZSBzYXZlZCBpbiB5b3VyIGJyb3dzZXIgYW5kIG5ldmVyIHNlbnQgYW55d2hlcmUgZXhjZXB0IGRpcmVjdGx5IHRvIERlbHRhLjwvZGl2PgoKICAgIDwhLS0gU2VydmVyIElQIHNob3duIGZpcnN0IHNvIHVzZXIgY2FuIHdoaXRlbGlzdCBiZWZvcmUgY29ubmVjdGluZyAtLT4KICAgIDxkaXYgY2xhc3M9ImNzLWlwLXJvdyI+CiAgICAgIDxkaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iY3MtaXAtbGJsIj5TZXJ2ZXIgSVAg4oCUIHdoaXRlbGlzdCB0aGlzIG9uIERlbHRhIGZpcnN0PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iY3MtaXAtdmFsIiBpZD0ic2VydmVySVAiPkxvYWRpbmcuLi48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9ImNzLWlwLWNvcHkiIG9uY2xpY2s9ImNvcHlJUCgpIj5Db3B5PC9idXR0b24+CiAgICA8L2Rpdj4KCiAgICA8ZGl2IGNsYXNzPSJjcy1zdGVwcyI+CiAgICAgIDxkaXYgY2xhc3M9ImNzLXN0ZXAiPjxkaXYgY2xhc3M9ImNzLXN0ZXAtbiI+MTwvZGl2PkNvcHkgSVAgYWJvdmUg4oaSIERlbHRhIEV4Y2hhbmdlIOKGkiBBY2NvdW50IOKGkiBBUEkgS2V5cyDihpIgRWRpdCDihpIgSVAgV2hpdGVsaXN0IOKGkiBTYXZlPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImNzLXN0ZXAiPjxkaXYgY2xhc3M9ImNzLXN0ZXAtbiI+MjwvZGl2PkVudGVyIHlvdXIgQVBJIEtleSBhbmQgU2VjcmV0IGJlbG93PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImNzLXN0ZXAiPjxkaXYgY2xhc3M9ImNzLXN0ZXAtbiI+MzwvZGl2PlRhcCBDb25uZWN0IOKAlCBib3Qgc3RhcnRzIGF1dG9tYXRpY2FsbHk8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gU2F2ZWQga2V5cyBub3RpY2UgLS0+CiAgICA8ZGl2IGlkPSJzYXZlZE5vdGljZSIgY2xhc3M9ImNzLXNhdmVkIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgJiMxMDAwMzsgS2V5cyBzYXZlZCDigJQgcmVjb25uZWN0aW5nLi4uCiAgICA8L2Rpdj4KCiAgICA8aW5wdXQgY2xhc3M9ImNzLWlucHV0IiBpZD0iaW5wdXRLZXkiICAgIHR5cGU9InRleHQiICAgICBwbGFjZWhvbGRlcj0iQVBJIEtleSIgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJub25lIj4KICAgIDxpbnB1dCBjbGFzcz0iY3MtaW5wdXQiIGlkPSJpbnB1dFNlY3JldCIgdHlwZT0icGFzc3dvcmQiIHBsYWNlaG9sZGVyPSJBUEkgU2VjcmV0Ij4KICAgIDxidXR0b24gY2xhc3M9ImNzLWJ0biIgb25jbGljaz0iZG9Db25uZWN0KCkiPkNvbm5lY3QgdG8gRGVsdGEgRXhjaGFuZ2U8L2J1dHRvbj4KICAgIDxkaXYgY2xhc3M9ImNzLW1zZyIgaWQ9ImNvbm5lY3RNc2ciPjwvZGl2PgogIDwvZGl2PgoKICA8IS0tIExJVkUgREFTSEJPQVJEIChoaWRkZW4gdW50aWwgY29ubmVjdGVkKSAtLT4KICA8ZGl2IGlkPSJsaXZlRGFzaCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CgogICAgPGRpdiBjbGFzcz0iaGVybyI+CiAgICAgIDxkaXYgY2xhc3M9Imhlcm8tbGJsIj5CaXRjb2luICZidWxsOyBMaXZlPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Imhlcm8tcHJpY2UiIGlkPSJidGNQcmljZSI+JC0tPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Imhlcm8tcm93Ij4KICAgICAgICA8c3BhbiBjbGFzcz0iY2hpcCBjaGlwLW4iIGlkPSJjaGlwUmVnaW1lIj4tLTwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0iY2hpcCBjaGlwLW4iIGlkPSJjaGlwU3RyYXQiPi0tPC9zcGFuPgogICAgICAgIDxzcGFuIGNsYXNzPSJjaGlwIGNoaXAtbiIgaWQ9ImNoaXBWb2wiPi0tPC9zcGFuPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDxkaXYgY2xhc3M9InJlZ2ltZSByZWctbmV1IiBpZD0icmVnaW1lQmFyIj5TY2FubmluZy4uLjwvZGl2PgoKICAgIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgICA8ZGl2IGNsYXNzPSJjYXJkLXRpdGxlIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5Db25maWRlbmNlIFNjb3JlPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImNvbmYtd3JhcCI+CiAgICAgICAgPGRpdiBjbGFzcz0iY29uZi1yaW5nIj4KICAgICAgICAgIDxzdmcgdmlld0JveD0iMCAwIDcyIDcyIiB3aWR0aD0iNzIiIGhlaWdodD0iNzIiPgogICAgICAgICAgICA8Y2lyY2xlIGN4PSIzNiIgY3k9IjM2IiByPSIyOCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZjFmNWY5IiBzdHJva2Utd2lkdGg9IjciLz4KICAgICAgICAgICAgPGNpcmNsZSBpZD0iY29uZkFyYyIgY3g9IjM2IiBjeT0iMzYiIHI9IjI4IiBmaWxsPSJub25lIiBzdHJva2U9IiMwMGIzODYiIHN0cm9rZS13aWR0aD0iNyIKICAgICAgICAgICAgICBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1kYXNoYXJyYXk9IjE3NS45IiBzdHJva2UtZGFzaG9mZnNldD0iMTc1LjkiCiAgICAgICAgICAgICAgc3R5bGU9InRyYW5zaXRpb246c3Ryb2tlLWRhc2hvZmZzZXQgLjdzLHN0cm9rZSAuM3MiLz4KICAgICAgICAgIDwvc3ZnPgogICAgICAgICAgPGRpdiBjbGFzcz0iY29uZi1vdmVyIj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iY29uZi1udW0iIGlkPSJjb25mTnVtIj4tLTwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJjb25mLWRlbm9tIj4vMTAwPC9kaXY+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJjb25mLW1ldGEiPgogICAgICAgICAgPGRpdiBjbGFzcz0iY29uZi1kaXIiIGlkPSJjb25mRGlyIj5XQUlUPC9kaXY+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJjb25mLXN1YiIgaWQ9ImNvbmZTdWIiPkdhdGhlcmluZyBkYXRhLi4uPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJwaWxsYXJzIiBpZD0icGlsbGFyc0VsIj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iaW5kLWdyaWQiPgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaW5kLWxibCI+QURYPC9kaXY+PGRpdiBjbGFzcz0iaW5kLXZhbCIgaWQ9ImluZEFEWCI+LS08L2Rpdj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJpbmQiPjxkaXYgY2xhc3M9ImluZC1sYmwiPkJCIFdpZHRoPC9kaXY+PGRpdiBjbGFzcz0iaW5kLXZhbCIgaWQ9ImluZEJXIj4tLTwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaW5kLWxibCI+QVRSICU8L2Rpdj48ZGl2IGNsYXNzPSJpbmQtdmFsIiBpZD0iaW5kQVRSIj4tLTwvZGl2PjwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ic2Nhbi1iYXIiPjxkaXYgY2xhc3M9InNjYW4tZmlsbCIgaWQ9InNjYW5GaWxsIiBzdHlsZT0id2lkdGg6MCUiPjwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzY2FuLXJvdyI+CiAgICAgICAgPHNwYW4gaWQ9InNjYW5TdGF0dXMiPk5vdCBydW5uaW5nPC9zcGFuPgogICAgICAgIDxzcGFuIGlkPSJzY2FuQ2QiIHN0eWxlPSJmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tYikiPi0tPC9zcGFuPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDxkaXYgaWQ9InBlcnBQb3NpdGlvbnMiPjwvZGl2PgogICAgPGRpdiBpZD0ib3B0c1Bvc2l0aW9ucyI+PC9kaXY+CgogICAgPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTBweCI+CiAgICAgIDxkaXYgY2xhc3M9ImNhcmQtdGl0bGUiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPldhbGxldDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ3YWwiPgogICAgICAgIDxkaXYgY2xhc3M9IndhbC1sIj4KICAgICAgICAgIDxkaXYgY2xhc3M9IndhbC1sYmwiPkJhbGFuY2U8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9IndhbC1hbXQiIGlkPSJ3YWxBbXQiPiQtLTwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0id2FsLXN0YXJ0IiBpZD0id2FsU3RhcnQiPjwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXY+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJ3YWwtcGN0IiBpZD0id2FsUGN0Ij4tLSU8L2Rpdj4KICAgICAgICAgIDxkaXYgY2xhc3M9IndhbC1wbmwiIGlkPSJ3YWxQbmwiPlAmYW1wO0wgJC0tPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CgogICAgPGRpdiBjbGFzcz0ic3RhdHMiPgogICAgICA8ZGl2IGNsYXNzPSJzdGF0Ij48ZGl2IGNsYXNzPSJzdGF0LWxibCI+V2luIFJhdGU8L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LXZhbCIgaWQ9InN0YXRXUiI+LS08L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ic3RhdCI+PGRpdiBjbGFzcz0ic3RhdC1sYmwiPlRyYWRlczwvZGl2PjxkaXYgY2xhc3M9InN0YXQtdmFsIiBpZD0ic3RhdFRSIj4wPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0YXQtbGJsIj5TY2FuICM8L2Rpdj48ZGl2IGNsYXNzPSJzdGF0LXZhbCIgc3R5bGU9ImNvbG9yOnZhcigtLWIpIiBpZD0ic3RhdFNOIj4wPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8ZGl2IGNsYXNzPSJidG4tcm93Ij4KICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRuIGJ0bi1zdGFydCIgb25jbGljaz0iYm90U3RhcnQoKSI+JiM5NjU0OyBTdGFydDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG4gYnRuLXN0b3AiICBvbmNsaWNrPSJib3RTdG9wKCkiPiYjOTYzMjsgU3RvcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG4gYnRuLXJ1biIgICBvbmNsaWNrPSJib3RSdW4oKSI+JiM5ODg5OyBSdW48L2J1dHRvbj4KICAgIDwvZGl2PgoKICAgIDwhLS0gT1BUSU9OUyBNT0RFIC0tPgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImNhcmQtdGl0bGUiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEycHgiPk9wdGlvbnMgTW9kZTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJvcHRzLXRvZ2dsZSI+CiAgICAgICAgPGRpdiBjbGFzcz0ib3B0cy10b2dnbGUtdGV4dCI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJsYWJlbCI+RW5hYmxlIE9wdGlvbnMgVHJhZGluZzwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0ic3ViIj5BVE0gLyBJVE0gY2FsbHMgJmFtcDsgcHV0cyArIHN0cmFkZGxlczwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxsYWJlbCBjbGFzcz0idG9nIj4KICAgICAgICAgIDxpbnB1dCB0eXBlPSJjaGVja2JveCIgaWQ9Im9wdHNUb2dnbGUiIG9uY2hhbmdlPSJ0b2dnbGVPcHRzKHRoaXMuY2hlY2tlZCkiPgogICAgICAgICAgPHNwYW4gY2xhc3M9InRvZy1zbCI+PC9zcGFuPgogICAgICAgIDwvbGFiZWw+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGlkPSJvcHRzUGFuZWwiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgICAgIDxkaXYgY2xhc3M9Im9wdHMtaW5mby1yb3ciPgogICAgICAgICAgPGRpdj48ZGl2IHN0eWxlPSJmb250LXdlaWdodDo4MDA7Y29sb3I6dmFyKC0tZykiPis4MCU8L2Rpdj48ZGl2IHN0eWxlPSJjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHgiPlRha2UgUHJvZml0PC9kaXY+PC9kaXY+CiAgICAgICAgICA8ZGl2PjxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1yKSI+LTUwJTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+U3RvcCBMb3NzPC9kaXY+PC9kaXY+CiAgICAgICAgICA8ZGl2PjxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1iKSI+LTMwJXBrPC9kaXY+PGRpdiBzdHlsZT0iY29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MnB4Ij5GbG9vciBUcmFpbDwvZGl2PjwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im9wdHMtYnRucyI+CiAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJvcHQtYnRuIG9iLWNhbGwiIG9uY2xpY2s9ImZpbmRPcHQoJ2NhbGwnKSI+Q2hlY2sgQ0FMTDwvYnV0dG9uPgogICAgICAgICAgPGJ1dHRvbiBjbGFzcz0ib3B0LWJ0biBvYi1wdXQiICBvbmNsaWNrPSJmaW5kT3B0KCdwdXQnKSI+Q2hlY2sgUFVUPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJvcHQtYnRuIG9iLXN0IiAgIG9uY2xpY2s9ImZpbmRTdHJhZGRsZSgpIj5TdHJhZGRsZTwvYnV0dG9uPgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgaWQ9Im9wdFJlc3VsdCIgY2xhc3M9Im9wdC1yZXN1bHQiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gTUFOVUFMIFRSQURFIC0tPgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImNhcmQtdGl0bGUiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEwcHgiPk1hbnVhbCBUcmFkZTwvZGl2PgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9Im1hbnVhbExvdHMiIHR5cGU9Im51bWJlciIgcGxhY2Vob2xkZXI9IkxvdHMgKGRlZmF1bHQ6IDEpIiBtaW49IjEiPgogICAgICA8ZGl2IGNsYXNzPSJtYW51YWwtYnRucyI+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRuLWxvbmciICBvbmNsaWNrPSJtYW51YWxUcmFkZSgnbG9uZycpIj4mIzg1OTM7IEJ1eSBMb25nPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRuLXNob3J0IiBvbmNsaWNrPSJtYW51YWxUcmFkZSgnc2hvcnQnKSI+JiM4NTk1OyBTZWxsIFNob3J0PC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CgogICAgPGJ1dHRvbiBjbGFzcz0iYnRuLWNsb3NlLWFsbCIgb25jbGljaz0iY2xvc2VBbGwoKSI+JiM5ODg4OyBDbG9zZSBBbGwgUG9zaXRpb25zPC9idXR0b24+CgogIDwvZGl2PjwhLS0gbGl2ZURhc2ggLS0+CjwvZGl2PjwhLS0gcGFnZS1ob21lIC0tPgoKPCEtLSDilZDilZDilZAgVFJBREVTIFBBR0Ug4pWQ4pWQ4pWQIC0tPgo8ZGl2IGNsYXNzPSJwYWdlIiBpZD0icGFnZS10cmFkZXMiPgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjEycHgiPgogICAgICA8c3BhbiBjbGFzcz0iY2FyZC10aXRsZSI+QWxsIFRyYWRlczwvc3Bhbj4KICAgICAgPHNwYW4gaWQ9InRyYWRlQ291bnQiIHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10MykiPjAgdHJhZGVzPC9zcGFuPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJ0cmFkZXNMaXN0Ij48ZGl2IGNsYXNzPSJlbXB0eSI+Tm8gdHJhZGVzIHlldDwvZGl2PjwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjwhLS0g4pWQ4pWQ4pWQIExPR1MgUEFHRSDilZDilZDilZAgLS0+CjxkaXYgY2xhc3M9InBhZ2UiIGlkPSJwYWdlLWxvZ3MiPgogIDxkaXYgY2xhc3M9ImxvZy1maWx0ZXJzIj4KICAgIDxidXR0b24gY2xhc3M9ImxvZy1mYnRuIG9uIiBpZD0ibGYtYWxsIiAgIG9uY2xpY2s9InNldExvZ0ZpbHRlcignJykiPkFsbDwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0ibG9nLWZidG4iICAgIGlkPSJsZi10cmFkZSIgIG9uY2xpY2s9InNldExvZ0ZpbHRlcignVFJBREUnKSI+VHJhZGVzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsb2ctZmJ0biIgICAgaWQ9ImxmLXdhcm4iICAgb25jbGljaz0ic2V0TG9nRmlsdGVyKCdXQVJOJykiPldhcm5pbmdzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsb2ctZmJ0biIgICAgaWQ9ImxmLWVycm9yIiAgb25jbGljaz0ic2V0TG9nRmlsdGVyKCdFUlJPUicpIj5FcnJvcnM8L2J1dHRvbj4KICA8L2Rpdj4KICA8ZGl2IGlkPSJsb2dDb3VudCIgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjhweCI+MCBlbnRyaWVzPC9kaXY+CiAgPGRpdiBjbGFzcz0ibG9nLWJveCIgaWQ9ImxvZ0JveCI+PC9kaXY+CjwvZGl2PgoKPCEtLSDilZDilZDilZAgU0VUVElOR1MgUEFHRSDilZDilZDilZAgLS0+CjxkaXYgY2xhc3M9InBhZ2UiIGlkPSJwYWdlLXNldHRpbmdzIj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxkaXYgY2xhc3M9ImNhcmQtdGl0bGUiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEycHgiPkNvbm5lY3Rpb248L2Rpdj4KICAgIDxkaXYgaWQ9InNldHRpbmdzQ29ubmVjdGVkIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjEzcHg7Y29sb3I6dmFyKC0tZyk7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1ib3R0b206NHB4Ij4mIzEwMDAzOyBDb25uZWN0ZWQgdG8gRGVsdGEgRXhjaGFuZ2U8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi1ib3R0b206MTJweCIgaWQ9InNldHRpbmdzQmFsYW5jZSI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQtdGl0bGUiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjhweDtmb250LXNpemU6MTBweCI+U2VydmVyIElQIOKAlCB3aGl0ZWxpc3Qgb24gRGVsdGEgRXhjaGFuZ2U8L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo3MDA7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtsZXR0ZXItc3BhY2luZzoycHg7bWFyZ2luLWJvdHRvbToxMHB4IiBpZD0ic2V0dGluZ3NJUCI+LS08L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImRpc2Nvbm5lY3QtYnRuIiBvbmNsaWNrPSJkb0Rpc2Nvbm5lY3QoKSI+JiMxMDAwNzsgRGlzY29ubmVjdCAmYW1wOyBjbGVhciBzYXZlZCBrZXlzPC9idXR0b24+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJjYXJkLXRpdGxlIiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo0cHgiPkFjdGl2ZSBHdWFyZHJhaWxzPC9kaXY+CiAgICA8ZGl2IGlkPSJndWFyZHJhaWxzTGlzdCI+PC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPC9kaXY+PCEtLSB3cmFwIC0tPgoKPCEtLSBCT1RUT00gTkFWIC0tPgo8bmF2IGNsYXNzPSJuYXYiPgogIDxidXR0b24gY2xhc3M9Im5iIG9uIiBpZD0ibmItaG9tZSIgICAgIG9uY2xpY2s9ImdvUGFnZSgnaG9tZScpIj48c3BhbiBjbGFzcz0iaWMiPiYjMTI3OTY4Ozwvc3Bhbj48c3BhbiBjbGFzcz0ibGIiPkhvbWU8L3NwYW4+PC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibmIiICAgIGlkPSJuYi10cmFkZXMiICAgb25jbGljaz0iZ29QYWdlKCd0cmFkZXMnKSI+PHNwYW4gY2xhc3M9ImljIj4mIzEyODIwMzs8L3NwYW4+PHNwYW4gY2xhc3M9ImxiIj5UcmFkZXM8L3NwYW4+PC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibmIiICAgIGlkPSJuYi1sb2dzIiAgICAgb25jbGljaz0iZ29QYWdlKCdsb2dzJykiPjxzcGFuIGNsYXNzPSJpYyI+JiMxMjgyMjA7PC9zcGFuPjxzcGFuIGNsYXNzPSJsYiI+TG9nczwvc3Bhbj48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJuYiIgICAgaWQ9Im5iLXNldHRpbmdzIiBvbmNsaWNrPSJnb1BhZ2UoJ3NldHRpbmdzJykiPjxzcGFuIGNsYXNzPSJpYyI+JiM5ODgxOzwvc3Bhbj48c3BhbiBjbGFzcz0ibGIiPlNldHRpbmdzPC9zcGFuPjwvYnV0dG9uPgo8L25hdj4KCjxzY3JpcHQ+Ci8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkAovLyBTVEFURQovLyDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZAKdmFyIFNUQVRFID0geyBsb2dzOltdLCBsb2dGaWx0ZXI6IiIsIHRyYWRlczpbXSwgbmV4dEF0Om51bGwsIHNjYW5TZWNzOjMwMCwgY29ubmVjdGVkOmZhbHNlIH07CnZhciBQQ09MUyA9IHsiUmVnaW1lIjoiIzNiODJmNiIsIk1URiBBbGlnbiI6IiMwMGIzODYiLCJSU0kiOiIjZjU5ZTBiIiwiTUFDRCI6IiM4YjVjZjYiLCJWb2xhdGlsaXR5IjoiI2VjNDg5OSIsIlZvbHVtZSI6IiNlNzRjM2MiLCJTZXNzaW9uIjoiIzE0YjhhNiJ9OwoKLy8g4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQCi8vIEhFTFBFUlMKLy8g4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQCmZ1bmN0aW9uIGdlKGlkKSB7IHJldHVybiBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCk7IH0KZnVuY3Rpb24gc2V0VGV4dChpZCwgdikgeyB2YXIgZT1nZShpZCk7IGlmKGUpIGUudGV4dENvbnRlbnQ9djsgfQpmdW5jdGlvbiBzZXRIVE1MKGlkLCB2KSB7IHZhciBlPWdlKGlkKTsgaWYoZSkgZS5pbm5lckhUTUw9djsgfQoKZnVuY3Rpb24geGhyKHVybCwgYm9keSwgY2FsbGJhY2spIHsKICB2YXIgcmVxID0gbmV3IFhNTEh0dHBSZXF1ZXN0KCk7CiAgdmFyIGlzUG9zdCA9IChib2R5ICE9PSB1bmRlZmluZWQgJiYgYm9keSAhPT0gbnVsbCk7CiAgcmVxLm9wZW4oaXNQb3N0ID8gIlBPU1QiIDogIkdFVCIsIHVybCwgdHJ1ZSk7CiAgaWYgKGlzUG9zdCkgcmVxLnNldFJlcXVlc3RIZWFkZXIoIkNvbnRlbnQtVHlwZSIsImFwcGxpY2F0aW9uL2pzb24iKTsKICByZXEub25yZWFkeXN0YXRlY2hhbmdlID0gZnVuY3Rpb24oKSB7CiAgICBpZiAocmVxLnJlYWR5U3RhdGUgIT09IDQpIHJldHVybjsKICAgIGlmICghY2FsbGJhY2spIHJldHVybjsKICAgIGlmIChyZXEuc3RhdHVzID09PSAyMDApIHsKICAgICAgdHJ5IHsgY2FsbGJhY2soSlNPTi5wYXJzZShyZXEucmVzcG9uc2VUZXh0KSk7IH0KICAgICAgY2F0Y2goZSkgeyBjYWxsYmFjayhudWxsKTsgfQogICAgfSBlbHNlIHsgY2FsbGJhY2sobnVsbCk7IH0KICB9OwogIHJlcS5vbmVycm9yID0gZnVuY3Rpb24oKSB7IGlmKGNhbGxiYWNrKSBjYWxsYmFjayhudWxsKTsgfTsKICByZXEuc2VuZChpc1Bvc3QgPyBKU09OLnN0cmluZ2lmeShib2R5KSA6IG51bGwpOwp9CgovLyDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZAKLy8gTkFWSUdBVElPTgovLyDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZAKZnVuY3Rpb24gZ29QYWdlKG5hbWUpIHsKICB2YXIgcGFnZXMgPSBbImhvbWUiLCJ0cmFkZXMiLCJsb2dzIiwic2V0dGluZ3MiXTsKICBmb3IgKHZhciBpPTA7IGk8cGFnZXMubGVuZ3RoOyBpKyspIHsKICAgIGdlKCJwYWdlLSIrcGFnZXNbaV0pLmNsYXNzTGlzdC50b2dnbGUoInNob3ciLCBwYWdlc1tpXT09PW5hbWUpOwogICAgZ2UoIm5iLSIrcGFnZXNbaV0pLmNsYXNzTGlzdC50b2dnbGUoIm9uIiwgcGFnZXNbaV09PT1uYW1lKTsKICB9CiAgaWYgKG5hbWU9PT0idHJhZGVzIikgICByZW5kZXJUcmFkZXMoKTsKICBpZiAobmFtZT09PSJsb2dzIikgICAgIHJlbmRlckxvZ3MoKTsKICBpZiAobmFtZT09PSJzZXR0aW5ncyIpIHJlbmRlclNldHRpbmdzKCk7Cn0KCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkAovLyBLRVkgU1RPUkFHRSAobG9jYWxTdG9yYWdlIOKAlCBicm93c2VyLXNpZGUgb25seSkKLy8g4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQCmZ1bmN0aW9uIHNhdmVLZXlzKGssIHMpIHsKICB0cnkgeyBsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgiYWJfa2V5Iiwgayk7IGxvY2FsU3RvcmFnZS5zZXRJdGVtKCJhYl9zZWMiLCBzKTsgfSBjYXRjaChlKXt9Cn0KZnVuY3Rpb24gbG9hZEtleXMoKSB7CiAgdHJ5IHsgcmV0dXJuIHsga2V5OiBsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgiYWJfa2V5Iil8fCIiLCBzZWM6IGxvY2FsU3RvcmFnZS5nZXRJdGVtKCJhYl9zZWMiKXx8IiIgfTsgfQogIGNhdGNoKGUpIHsgcmV0dXJuIHtrZXk6IiIsc2VjOiIifTsgfQp9CmZ1bmN0aW9uIGNsZWFyS2V5cygpIHsKICB0cnkgeyBsb2NhbFN0b3JhZ2UucmVtb3ZlSXRlbSgiYWJfa2V5Iik7IGxvY2FsU3RvcmFnZS5yZW1vdmVJdGVtKCJhYl9zZWMiKTsgfSBjYXRjaChlKXt9Cn0KCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkAovLyBDT05ORUNUIEZMT1cKLy8g4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQCmZ1bmN0aW9uIGRvQ29ubmVjdCgpIHsKICB2YXIga2V5ID0gZ2UoImlucHV0S2V5IikudmFsdWUudHJpbSgpIHx8IGxvYWRLZXlzKCkua2V5OwogIHZhciBzZWMgPSBnZSgiaW5wdXRTZWNyZXQiKS52YWx1ZS50cmltKCkgfHwgbG9hZEtleXMoKS5zZWM7CiAgaWYgKCFrZXkgfHwgIXNlYykgewogICAgc2hvd0Nvbm5lY3RNc2coIkVudGVyIHlvdXIgQVBJIEtleSBhbmQgU2VjcmV0IiwgImVyciIpOwogICAgcmV0dXJuOwogIH0KICBzaG93Q29ubmVjdE1zZygiQ29ubmVjdGluZy4uLiIsICIiKTsKICB4aHIoIi9hcGkvY29ubmVjdCIsIHthcGlfa2V5OmtleSwgYXBpX3NlY3JldDpzZWN9LCBmdW5jdGlvbihyKSB7CiAgICBpZiAociAmJiByLnN1Y2Nlc3MpIHsKICAgICAgc2F2ZUtleXMoa2V5LCBzZWMpOwogICAgICBzaG93Q29ubmVjdE1zZygiQ29ubmVjdGVkISBCYWxhbmNlOiAkIityLmJhbGFuY2UudG9GaXhlZCgyKSwgIm9rIik7CiAgICAgIGdlKCJjb25uZWN0U2NyZWVuIikuc3R5bGUuZGlzcGxheSA9ICJub25lIjsKICAgICAgZ2UoImxpdmVEYXNoIikuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgICAgIFNUQVRFLmNvbm5lY3RlZCA9IHRydWU7CiAgICAgIHBvbGwoKTsKICAgIH0gZWxzZSB7CiAgICAgIHZhciBtc2cgPSByID8gci5tZXNzYWdlIDogIkNvbm5lY3Rpb24gZmFpbGVkIjsKICAgICAgdmFyIGlwICA9IHIgJiYgci5zZXJ2ZXJfaXAgPyAiIHwgU2VydmVyIElQOiAiK3Iuc2VydmVyX2lwIDogIiI7CiAgICAgIHNob3dDb25uZWN0TXNnKG1zZyArIGlwLCAiZXJyIik7CiAgICB9CiAgfSk7Cn0KCmZ1bmN0aW9uIHNob3dDb25uZWN0TXNnKG1zZywgY2xzKSB7CiAgdmFyIGVsID0gZ2UoImNvbm5lY3RNc2ciKTsKICBlbC50ZXh0Q29udGVudCA9IG1zZzsKICBlbC5jbGFzc05hbWUgPSAiY3MtbXNnIiArIChjbHMgPyAiICIrY2xzIDogIiIpOwp9CgpmdW5jdGlvbiBkb0Rpc2Nvbm5lY3QoKSB7CiAgaWYgKCFjb25maXJtKCJEaXNjb25uZWN0IGFuZCBjbGVhciBzYXZlZCBBUEkga2V5cz8iKSkgcmV0dXJuOwogIGNsZWFyS2V5cygpOwogIFNUQVRFLmNvbm5lY3RlZCA9IGZhbHNlOwogIGdlKCJjb25uZWN0U2NyZWVuIikuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgZ2UoImxpdmVEYXNoIikuc3R5bGUuZGlzcGxheSA9ICJub25lIjsKICBnZSgic3RhdHVzUGlsbCIpLmNsYXNzTmFtZSA9ICJwaWxsIHBpbGwtb2ZmIjsKICBzZXRUZXh0KCJwaWxsTGFiZWwiLCAiU3RvcHBlZCIpOwogIGdlKCJpbnB1dEtleSIpLnZhbHVlID0gIiI7CiAgZ2UoImlucHV0U2VjcmV0IikudmFsdWUgPSAiIjsKICBnZSgic2F2ZWROb3RpY2UiKS5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOwp9CgpmdW5jdGlvbiBjb3B5SVAoKSB7CiAgdmFyIGlwID0gZ2UoInNlcnZlcklQIikudGV4dENvbnRlbnQ7CiAgdHJ5IHsgbmF2aWdhdG9yLmNsaXBib2FyZC53cml0ZVRleHQoaXApOyB9IGNhdGNoKGUpe30KICB2YXIgYnRuID0gZG9jdW1lbnQucXVlcnlTZWxlY3RvcigiLmNzLWlwLWNvcHkiKTsKICBidG4udGV4dENvbnRlbnQgPSAiQ29waWVkISI7CiAgc2V0VGltZW91dChmdW5jdGlvbigpeyBidG4udGV4dENvbnRlbnQgPSAiQ29weSI7IH0sIDIwMDApOwp9CgovLyDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZAKLy8gQk9UIENPTlRST0xTCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkApmdW5jdGlvbiBib3RTdGFydCgpIHsgeGhyKCIvYXBpL2JvdC9zdGFydCIsIHt9LCBudWxsKTsgfQpmdW5jdGlvbiBib3RTdG9wKCkgIHsgeGhyKCIvYXBpL2JvdC9zdG9wIiwgIHt9LCBudWxsKTsgfQpmdW5jdGlvbiBib3RSdW4oKSAgIHsgc2V0VGV4dCgic2NhblN0YXR1cyIsIlNjYW5uaW5nLi4uIik7IHhocigiL2FwaS9ib3QvcnVuX25vdyIse30sbnVsbCk7IH0KZnVuY3Rpb24gY2xvc2VBbGwoKSB7CiAgaWYgKCFjb25maXJtKCJDbG9zZSBBTEwgb3BlbiBwb3NpdGlvbnMgKHBlcnBzICsgb3B0aW9ucyk/IikpIHJldHVybjsKICB4aHIoIi9hcGkvY2xvc2VfYWxsIiwge30sIGZ1bmN0aW9uKHIpIHsgYWxlcnQoIkNsb3NlZCAiKygociYmci5jbG9zZWQpfHwwKSsiIHBvc2l0aW9ucyIpOyB9KTsKfQpmdW5jdGlvbiBtYW51YWxUcmFkZShkaXIpIHsKICB2YXIgbG90cyA9IHBhcnNlSW50KGdlKCJtYW51YWxMb3RzIikudmFsdWUpIHx8IDE7CiAgeGhyKCIvYXBpL21hbnVhbF90cmFkZSIsIHtkaXJlY3Rpb246ZGlyLCBsb3RzOmxvdHN9LCBmdW5jdGlvbihyKSB7CiAgICBpZiAociAmJiByLnN1Y2Nlc3MpCiAgICAgIGFsZXJ0KGRpci50b1VwcGVyQ2FzZSgpKyIgIitsb3RzKyJMXG5FbnRyeTogJCIrci5lbnRyeSsiXG5TdG9wOiAkIityLnN0b3ArIlxuVFA6ICQiK3IudHApOwogICAgZWxzZQogICAgICBhbGVydCgiRmFpbGVkOiAiKygociYmci5tZXNzYWdlKXx8IkNoZWNrIExvZ3MgdGFiIikpOwogIH0pOwp9CmZ1bmN0aW9uIHRvZ2dsZU9wdHMob24pIHsKICB4aHIoIi9hcGkvb3B0cy90b2dnbGUiLCB7ZW5hYmxlZDpvbn0sIGZ1bmN0aW9uKHIpIHsKICAgIGdlKCJvcHRzUGFuZWwiKS5zdHlsZS5kaXNwbGF5ID0gKHImJnIub3B0c19tb2RlKSA/ICJibG9jayIgOiAibm9uZSI7CiAgfSk7Cn0KZnVuY3Rpb24gZmluZE9wdCh0KSB7CiAgdmFyIGVsID0gZ2UoIm9wdFJlc3VsdCIpOyBlbC5zdHlsZS5kaXNwbGF5PSJibG9jayI7IGVsLnRleHRDb250ZW50PSJDaGVja2luZy4uLiI7CiAgeGhyKCIvYXBpL29wdHMvZmluZCIsIHt0eXBlOnQsaXRtOmZhbHNlfSwgZnVuY3Rpb24ocikgewogICAgaWYgKHIgJiYgci5mb3VuZCkgewogICAgICBlbC5pbm5lckhUTUwgPSAiPGI+IityLnN5bWJvbCsiPC9iPjxicj5TdHJpa2UgJCIrKHIuc3RyaWtlfHwwKS50b0xvY2FsZVN0cmluZygpCiAgICAgICAgKyIgfCBNYXJrICQiKyhyLm1hcmt8fDApLnRvRml4ZWQoMikrIiB8IFByZW1pdW0gJCIrKHIucHJlbWl1bV91c2R8fDApLnRvRml4ZWQoMikKICAgICAgICArKHIuaXY/IiB8IElWICIrci5pdisiJSI6IiIpKyIgfCAiK3IubW9uZXluZXNzCiAgICAgICAgKyI8YnI+RXhwaXJ5IEZyaWRheSAiK3IuZXhwaXJ5OwogICAgfSBlbHNlIHsKICAgICAgZWwudGV4dENvbnRlbnQgPSAiTm8gIit0KyIgb3B0aW9uIGF2YWlsYWJsZS4gRXhwaXJ5OiAiKygociYmci5leHBpcnkpfHwiPyIpOwogICAgfQogIH0pOwp9CmZ1bmN0aW9uIGZpbmRTdHJhZGRsZSgpIHsKICB2YXIgZWwgPSBnZSgib3B0UmVzdWx0Iik7IGVsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsgZWwudGV4dENvbnRlbnQ9IkNoZWNraW5nIHN0cmFkZGxlLi4uIjsKICB4aHIoIi9hcGkvb3B0cy9zdHJhZGRsZSIsIHt9LCBmdW5jdGlvbihyKSB7CiAgICBpZiAociAmJiByLmZvdW5kKSB7CiAgICAgIGVsLmlubmVySFRNTCA9ICI8Yj5TdHJhZGRsZTwvYj48YnI+VG90YWwgcHJlbWl1bTogJCIrKHIudG90YWxfcHJlbWl1bV91c2R8fDApLnRvRml4ZWQoMikKICAgICAgICArIjxicj5CcmVhay1ldmVuIFVQOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX3VwfHwwKS50b0xvY2FsZVN0cmluZygpCiAgICAgICAgKyIgfCBET1dOOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX2Rvd258fDApLnRvTG9jYWxlU3RyaW5nKCk7CiAgICB9IGVsc2UgewogICAgICBlbC50ZXh0Q29udGVudCA9ICJDYW5ub3QgYnVpbGQgc3RyYWRkbGUgcmlnaHQgbm93LiI7CiAgICB9CiAgfSk7Cn0KCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkAovLyBMT0cgRklMVEVSCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkApmdW5jdGlvbiBzZXRMb2dGaWx0ZXIoZikgewogIFNUQVRFLmxvZ0ZpbHRlciA9IGY7CiAgdmFyIG1hcCA9IHsiIjoibGYtYWxsIiwiVFJBREUiOiJsZi10cmFkZSIsIldBUk4iOiJsZi13YXJuIiwiRVJST1IiOiJsZi1lcnJvciJ9OwogIHZhciBrZXlzID0gT2JqZWN0LmtleXMobWFwKTsKICBmb3IgKHZhciBpPTA7IGk8a2V5cy5sZW5ndGg7IGkrKykgewogICAgdmFyIGVsID0gZ2UobWFwW2tleXNbaV1dKTsKICAgIGlmIChlbCkgZWwuY2xhc3NMaXN0LnRvZ2dsZSgib24iLCBrZXlzW2ldPT09Zik7CiAgfQogIHJlbmRlckxvZ3MoKTsKfQoKLy8g4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQCi8vIFJFTkRFUiBGVU5DVElPTlMKLy8g4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQ4pWQCmZ1bmN0aW9uIHJlbmRlcihzKSB7CiAgaWYgKCFzKSByZXR1cm47CgogIC8vIOKUgCBDb25uZWN0aW9uIHN0YXRlIOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgAogIGlmIChzLmNvbm5lY3RlZCkgewogICAgZ2UoImNvbm5lY3RTY3JlZW4iKS5zdHlsZS5kaXNwbGF5ID0gIm5vbmUiOwogICAgZ2UoImxpdmVEYXNoIikuc3R5bGUuZGlzcGxheSA9ICJibG9jayI7CiAgICBTVEFURS5jb25uZWN0ZWQgPSB0cnVlOwogIH0KCiAgLy8g4pSAIEhlYWRlciBwaWxsIOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgAogIHZhciBydW5uaW5nID0gcy5jb25uZWN0ZWQgJiYgcy5ydW5uaW5nICYmICFzLmhhbHRlZDsKICBnZSgic3RhdHVzUGlsbCIpLmNsYXNzTmFtZSA9ICJwaWxsICIgKyAocy5oYWx0ZWQ/InBpbGwtd2FybiI6cnVubmluZz8icGlsbC1saXZlIjoicGlsbC1vZmYiKTsKICBzZXRUZXh0KCJwaWxsTGFiZWwiLCBzLmhhbHRlZD8iSEFMVEVEIjpydW5uaW5nPyJMaXZlIjoiU3RvcHBlZCIpOwoKICAvLyDilIAgQlRDIHByaWNlICsgY2hpcHMg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACiAgc2V0VGV4dCgiYnRjUHJpY2UiLCBzLnByaWNlID8gIiQiK3MucHJpY2UudG9Mb2NhbGVTdHJpbmcoKSA6ICIkLS0iKTsKICB2YXIgcmcgPSBzLnJlZ2ltZXx8IiI7CiAgdmFyIHJjID0gZ2UoImNoaXBSZWdpbWUiKTsgcmMudGV4dENvbnRlbnQ9cmd8fCItLSI7CiAgcmMuY2xhc3NOYW1lID0gImNoaXAgIisocmcuaW5kZXhPZigiQlVMTCIpPj0wPyJjaGlwLWciOnJnLmluZGV4T2YoIkJFQVIiKT49MD8iY2hpcC1yIjoiY2hpcC1uIik7CiAgc2V0VGV4dCgiY2hpcFN0cmF0Iiwgcy5zdHJhdGVneXx8Ii0tIik7CiAgc2V0VGV4dCgiY2hpcFZvbCIsICAgcy52b2xfcmVnaW1lfHwiLS0iKTsKCiAgLy8g4pSAIFJlZ2ltZSBiYXIg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACiAgdmFyIHJiID0gZ2UoInJlZ2ltZUJhciIpOwogIHJiLmNsYXNzTmFtZSA9ICJyZWdpbWUgIiArCiAgICAocmcuaW5kZXhPZigiQlVMTCIpPj0wPyJyZWctYnVsbCI6cmcuaW5kZXhPZigiQkVBUiIpPj0wPyJyZWctYmVhciI6cmc9PT0iU0lERVdBWVMiPyJyZWctc2lkZSI6InJlZy1uZXUiKTsKICByYi50ZXh0Q29udGVudCA9IHJnICsgIiBcdTIwMTQgIiArIChzLnN0cmF0ZWd5fHwiQ2FsY3VsYXRpbmciKTsKCiAgLy8g4pSAIENvbmZpZGVuY2UgcmluZyDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICB2YXIgc2MgPSBzLmNvbmZfbG9uZ3x8MDsKICBzZXRUZXh0KCJjb25mTnVtIiwgc2N8fCItLSIpOwogIHZhciBhcmMgPSBnZSgiY29uZkFyYyIpOwogIGFyYy5zdHlsZS5zdHJva2VEYXNob2Zmc2V0ID0gMTc1LjkgLSAoc2MvMTAwKjE3NS45KTsKICBhcmMuc3R5bGUuc3Ryb2tlID0gc2M+PTcwPyIjMDBiMzg2IjpzYz49NTA/IiNmNTllMGIiOiIjZTc0YzNjIjsKICBnZSgiY29uZk51bSIpLnN0eWxlLmNvbG9yID0gc2M+PTcwPyJ2YXIoLS1nKSI6c2M+PTUwPyJ2YXIoLS15KSI6InZhcigtLXIpIjsKICBzZXRUZXh0KCJjb25mRGlyIiwgcy5zdHJhdGVneT09PSJXQUlUIj8iV0FJVCI6cmd8fCJXQUlUIik7CiAgc2V0VGV4dCgiY29uZlN1YiIsICJTY29yZSAiK3NjKyIvMTAwIHwgQURYPSIrKHMuYWR4fHwwKSsiIHwgIisocy52b2xfcmVnaW1lfHwiIikpOwoKICAvLyDilIAgUGlsbGFycyDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICB2YXIgcGxzID0gcy5waWxsYXJzfHx7fTsgdmFyIHBrZXlzID0gT2JqZWN0LmtleXMocGxzKTsgdmFyIHBoPSIiOwogIGZvciAodmFyIGk9MDsgaTxwa2V5cy5sZW5ndGg7IGkrKykgewogICAgdmFyIGs9cGtleXNbaV07IHZhciB2PXBsc1trXTsKICAgIHZhciBwY3QgPSB2Lm0+MCA/IE1hdGgucm91bmQodi5zL3YubSoxMDApIDogMDsKICAgIHZhciBjb2wgPSBQQ09MU1trXXx8InZhcigtLWcpIjsKICAgIHBoICs9ICI8ZGl2IGNsYXNzPSdwcm93Jz4iCiAgICAgICsiPGRpdiBjbGFzcz0ncG5hbWUnPiIraysiPC9kaXY+IgogICAgICArIjxkaXYgY2xhc3M9J3B0cmFjayc+PGRpdiBjbGFzcz0ncGZpbGwnIHN0eWxlPSd3aWR0aDoiK3BjdCsiJTtiYWNrZ3JvdW5kOiIrY29sKyInPjwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwc2NvcmUnIHN0eWxlPSdjb2xvcjoiK2NvbCsiJz4iK3YucysiLyIrdi5tKyI8L2Rpdj4iCiAgICAgICsiPC9kaXY+IjsKICB9CiAgc2V0SFRNTCgicGlsbGFyc0VsIiwgcGgpOwoKICAvLyDilIAgSW5kaWNhdG9ycyDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICBzZXRUZXh0KCJpbmRBRFgiLCBzLmFkeHx8Ii0tIik7CiAgc2V0VGV4dCgiaW5kQlciLCAgcy5idyAgPyBzLmJ3KyIlIiAgICAgOiAiLS0iKTsKICBzZXRUZXh0KCJpbmRBVFIiLCBzLmF0cl9wY3QgPyBzLmF0cl9wY3QrIiUiIDogIi0tIik7CgogIC8vIOKUgCBTY2FuIGJhciArIHN0YXR1cyDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICBzZXRUZXh0KCJzY2FuU3RhdHVzIiwgcy5zdGF0dXN8fCItLSIpOwogIHNldFRleHQoInN0YXRTTiIsIHMuc2Nhbl9ufHwwKTsKICBpZiAocy5uZXh0X3NjYW4pIFNUQVRFLm5leHRBdCA9IG5ldyBEYXRlKHMubmV4dF9zY2FuKTsKCiAgLy8g4pSAIE9wZW4gcGVycCBwb3NpdGlvbnMg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACiAgdmFyIHBlcnBzID0gcy5vcGVuX3Bvc3x8W107IHZhciBwZXJwSHRtbCA9ICIiOwogIGZvciAodmFyIGk9MDsgaTxwZXJwcy5sZW5ndGg7IGkrKykgewogICAgdmFyIHA9cGVycHNbaV07IHZhciBuZWc9cC51cG5sPDA7CiAgICBwZXJwSHRtbCArPSAiPGRpdiBjbGFzcz0ncG9zIHBvcy0iKyhuZWc/InNob3J0IjoibG9uZyIpKyInPiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwb3MtaGQnPjxzcGFuIGNsYXNzPSdwb3Mtc3ltJz4iK3Auc3ltKyI8L3NwYW4+IgogICAgICArIjxzcGFuIGNsYXNzPSdiYWRnZSBiYWRnZS0iKyhwLnNpZGU9PT0ibG9uZyI/ImwiOiJzIikrIic+IitwLnNpZGUudG9VcHBlckNhc2UoKSsiPC9zcGFuPjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwb3MtZ3JpZCc+IgogICAgICArIjxkaXYgY2xhc3M9J3Bvcy1jZWxsJz48ZGl2IGNsYXNzPSdwb3MtY2VsbC1sYmwnPkVudHJ5PC9kaXY+PGRpdiBjbGFzcz0ncG9zLWNlbGwtdmFsJz4kIitwLmVudHJ5LnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwb3MtY2VsbCc+PGRpdiBjbGFzcz0ncG9zLWNlbGwtbGJsJz5Mb3RzPC9kaXY+PGRpdiBjbGFzcz0ncG9zLWNlbGwtdmFsJz4iK3AubG90cysiPC9kaXY+PC9kaXY+IgogICAgICArIjxkaXYgY2xhc3M9J3Bvcy1jZWxsJz48ZGl2IGNsYXNzPSdwb3MtY2VsbC1sYmwnPlVQTDwvZGl2PjxkaXYgY2xhc3M9J3Bvcy1jZWxsLXZhbCAiKyhuZWc/InBjciI6InBjZyIpKyInPiIrKHAudXBubD49MD8iKyI6IiIpK3AudXBubCsiICgiKyhwLnBjdD49MD8iKyI6IiIpK3AucGN0KyIlKTwvZGl2PjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwb3MtY2VsbCc+PGRpdiBjbGFzcz0ncG9zLWNlbGwtbGJsJz5NYXJrPC9kaXY+PGRpdiBjbGFzcz0ncG9zLWNlbGwtdmFsJz4kIisocC5tYXJrfHxwLmVudHJ5KS50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ncG9zLWNlbGwnPjxkaXYgY2xhc3M9J3Bvcy1jZWxsLWxibCc+U3RvcDwvZGl2PjxkaXYgY2xhc3M9J3Bvcy1jZWxsLXZhbCBwY3InPiQiK3Auc3RvcC50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ncG9zLWNlbGwnPjxkaXYgY2xhc3M9J3Bvcy1jZWxsLWxibCc+VFA8L2Rpdj48ZGl2IGNsYXNzPSdwb3MtY2VsbC12YWwgcGNnJz4kIitwLnRwLnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PiIKICAgICAgKyI8L2Rpdj48L2Rpdj4iOwogIH0KICBzZXRIVE1MKCJwZXJwUG9zaXRpb25zIiwgcGVycEh0bWwpOwoKICAvLyDilIAgT3BlbiBvcHRpb25zIHBvc2l0aW9ucyDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICB2YXIgb3B0cyA9IHMub3B0c19wb3N8fFtdOyB2YXIgb3B0c0h0bWwgPSAiIjsKICBmb3IgKHZhciBpPTA7IGk8b3B0cy5sZW5ndGg7IGkrKykgewogICAgdmFyIG89b3B0c1tpXTsgdmFyIGlzQz1vLnR5cGU9PT0iQ0FMTCI7CiAgICBvcHRzSHRtbCArPSAiPGRpdiBjbGFzcz0ncG9zIHBvcy1vcHQnPiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwb3MtaGQnPjxzcGFuIGNsYXNzPSdwb3Mtc3ltJyBzdHlsZT0nZm9udC1zaXplOjEycHgnPiIrby5zeW0rIjwvc3Bhbj4iCiAgICAgICsiPHNwYW4gY2xhc3M9J2JhZGdlIGJhZGdlLSIrKGlzQz8iYyI6InAiKSsiJz4iK28udHlwZSsiPC9zcGFuPjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSdwb3MtZ3JpZCc+IgogICAgICArIjxkaXYgY2xhc3M9J3Bvcy1jZWxsJz48ZGl2IGNsYXNzPSdwb3MtY2VsbC1sYmwnPkVudHJ5PC9kaXY+PGRpdiBjbGFzcz0ncG9zLWNlbGwtdmFsJz4kIitvLmVudHJ5KyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ncG9zLWNlbGwnPjxkaXYgY2xhc3M9J3Bvcy1jZWxsLWxibCc+TWFyazwvZGl2PjxkaXYgY2xhc3M9J3Bvcy1jZWxsLXZhbCc+JCIrby5tYXJrKyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ncG9zLWNlbGwnPjxkaXYgY2xhc3M9J3Bvcy1jZWxsLWxibCc+UCZMPC9kaXY+PGRpdiBjbGFzcz0ncG9zLWNlbGwtdmFsICIrKG8ucGN0PDA/InBjciI6InBjZyIpKyInPiIrKG8ucGN0Pj0wPyIrIjoiIikrby5wY3QrIiU8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ncG9zLWNlbGwnPjxkaXYgY2xhc3M9J3Bvcy1jZWxsLWxibCc+UGVhazwvZGl2PjxkaXYgY2xhc3M9J3Bvcy1jZWxsLXZhbCBwY2cnPiQiK28ucGVhaysiPC9kaXY+PC9kaXY+IgogICAgICArIjwvZGl2PjwvZGl2PiI7CiAgfQogIHNldEhUTUwoIm9wdHNQb3NpdGlvbnMiLCBvcHRzSHRtbCk7CgogIC8vIOKUgCBXYWxsZXQg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACiAgdmFyIGNhcD1zLmNhcGl0YWx8fDA7IHZhciBzYzI9cy5zdGFydF9jYXB8fDA7IHZhciBwcD1zLnBubF9wY3R8fDA7CiAgc2V0VGV4dCgid2FsQW10IiwgIGNhcD8iJCIrY2FwLnRvRml4ZWQoMik6IiQtLSIpOwogIHNldFRleHQoIndhbFN0YXJ0IixzYzI/IlN0YXJ0ZWQgJCIrc2MyLnRvRml4ZWQoMik6IiIpOwogIHZhciB3cEVsPWdlKCJ3YWxQY3QiKTsgd3BFbC50ZXh0Q29udGVudD0ocHA+PTA/IisiOiIiKStwcC50b0ZpeGVkKDIpKyIlIjsgd3BFbC5zdHlsZS5jb2xvcj1wcD49MD8idmFyKC0tZykiOiJ2YXIoLS1yKSI7CiAgc2V0VGV4dCgid2FsUG5sIiwiUCZMICQiKyhwcD49MD8iKyI6IiIpKyhjYXAtc2MyKS50b0ZpeGVkKDIpKTsKCiAgLy8g4pSAIFN0YXRzIOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgAogIHNldFRleHQoInN0YXRXUiIsIHMud2luX3JhdGUhPW51bGwgPyBzLndpbl9yYXRlKyIlIiA6ICItLSIpOwogIHNldFRleHQoInN0YXRUUiIsIHMudG90YWxfdHJhZGVzfHwwKTsKCiAgLy8g4pSAIE9wdGlvbnMgdG9nZ2xlIOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgAogIHZhciBvdD1nZSgib3B0c1RvZ2dsZSIpOyBpZihvdCkgb3QuY2hlY2tlZD0hIXMub3B0c19tb2RlOwogIGdlKCJvcHRzUGFuZWwiKS5zdHlsZS5kaXNwbGF5ID0gcy5vcHRzX21vZGU/ImJsb2NrIjoibm9uZSI7CgogIC8vIOKUgCBEYXRhIGZvciBvdGhlciBwYWdlcyDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKICBpZiAocy5sb2dzKSAgIFNUQVRFLmxvZ3MgICA9IHMubG9nczsKICBpZiAocy50cmFkZXMpIFNUQVRFLnRyYWRlcyA9IHMudHJhZGVzOwogIHNldFRleHQoImxvZ0NvdW50IiwgU1RBVEUubG9ncy5sZW5ndGgrIiBlbnRyaWVzIik7CiAgaWYgKGdlKCJwYWdlLWxvZ3MiKS5jbGFzc0xpc3QuY29udGFpbnMoInNob3ciKSkgICByZW5kZXJMb2dzKCk7CiAgaWYgKGdlKCJwYWdlLXRyYWRlcyIpLmNsYXNzTGlzdC5jb250YWlucygic2hvdyIpKSByZW5kZXJUcmFkZXMoKTsKICBpZiAoZ2UoInBhZ2Utc2V0dGluZ3MiKS5jbGFzc0xpc3QuY29udGFpbnMoInNob3ciKSkgcmVuZGVyU2V0dGluZ3Mocy5ndWFyZHJhaWxzKTsKfQoKZnVuY3Rpb24gcmVuZGVyVHJhZGVzKCkgewogIHNldFRleHQoInRyYWRlQ291bnQiLCBTVEFURS50cmFkZXMubGVuZ3RoKyIgdHJhZGVzIik7CiAgaWYgKCFTVEFURS50cmFkZXMubGVuZ3RoKSB7IHNldEhUTUwoInRyYWRlc0xpc3QiLCI8ZGl2IGNsYXNzPSdlbXB0eSc+Tm8gdHJhZGVzIHlldDwvZGl2PiIpOyByZXR1cm47IH0KICB2YXIgaD0iIjsKICBmb3IgKHZhciBpPTA7IGk8U1RBVEUudHJhZGVzLmxlbmd0aDsgaSsrKSB7CiAgICB2YXIgdD1TVEFURS50cmFkZXNbaV07IHZhciBvcGVuPXQuZXhpdD09bnVsbDsKICAgIHZhciBzZD10LnNpZGV8fCIiOwogICAgdmFyIGljPXNkPT09ImxvbmciPyJ0LWxvbmciOnNkPT09InNob3J0Ij8idC1zaG9ydCI6c2Q9PT0iY2FsbCI/InQtY2FsbCI6InQtcHV0IjsKICAgIHZhciBpY289c2Q9PT0ibG9uZyI/IiYjODU5MzsiOnNkPT09InNob3J0Ij8iJiM4NTk1OyI6c2Q9PT0iY2FsbCI/IkMiOiJQIjsKICAgIHZhciBwYz1vcGVuPyJ0cG4iOih0Lndvbj8idHBnIjoidHByIik7CiAgICB2YXIgcHY9b3Blbj8iT3Blblx1MjAyNiI6KHQud29uPyIrIjoiIikrKHQucG5sfHwwKS50b0ZpeGVkKDQpOwogICAgdmFyIHRtPXQudGltZT90LnRpbWUuc3Vic3RyKDUsMTEpLnJlcGxhY2UoIlQiLCIgIik6IiI7CiAgICBoKz0iPGRpdiBjbGFzcz0ndHJhZGUtcm93Jz4iCiAgICAgICsiPGRpdiBjbGFzcz0ndHJhZGUtaWNvICIraWMrIic+IitpY28rIjwvZGl2PiIKICAgICAgKyI8ZGl2IGNsYXNzPSd0cmFkZS1taWQnPjxkaXYgY2xhc3M9J3RyYWRlLXN5bSc+IisodC5zeW18fCJCVENVU0QiKSsiPC9kaXY+IgogICAgICArIjxkaXYgY2xhc3M9J3RyYWRlLW1ldGEnPiIrdG0rIiAmbWlkZG90OyAiKyh0LnJlYXNvbnx8IiIpKyI8L2Rpdj48L2Rpdj4iCiAgICAgICsiPGRpdiBjbGFzcz0ndHJhZGUtcmlnaHQnPjxkaXYgY2xhc3M9J3RyYWRlLXBubCAiK3BjKyInPiQiK3B2KyI8L2Rpdj4iCiAgICAgICsiPGRpdiBzdHlsZT0nZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpJz4iKyh0LmVudHJ5PyJAJCIrdC5lbnRyeToiIikrIjwvZGl2PjwvZGl2PiIKICAgICAgKyI8L2Rpdj4iOwogIH0KICBzZXRIVE1MKCJ0cmFkZXNMaXN0IiwgaCk7Cn0KCmZ1bmN0aW9uIHJlbmRlckxvZ3MoKSB7CiAgdmFyIGYgPSBTVEFURS5sb2dGaWx0ZXIgPyBTVEFURS5sb2dzLmZpbHRlcihmdW5jdGlvbihlKXtyZXR1cm4gZS5sPT09U1RBVEUubG9nRmlsdGVyO30pIDogU1RBVEUubG9nczsKICB2YXIgaD0iIjsKICBmb3IgKHZhciBpPTA7IGk8TWF0aC5taW4oZi5sZW5ndGgsMTUwKTsgaSsrKSB7CiAgICB2YXIgZT1mW2ldOwogICAgdmFyIGNscz0ibG9nLUkiOwogICAgaWYoZS5sPT09IldBUk4iKWNscz0ibG9nLVciOyBlbHNlIGlmKGUubD09PSJFUlJPUiIpY2xzPSJsb2ctRSI7IGVsc2UgaWYoZS5sPT09IlRSQURFIiljbHM9ImxvZy1UIjsKICAgIGgrPSI8ZGl2IGNsYXNzPSdsb2ctZW50cnknPjxzcGFuIGNsYXNzPSdsb2ctdGltZSc+IitlLnQrIjwvc3Bhbj48c3BhbiBjbGFzcz0nIitjbHMrIic+IitlLm0rIjwvc3Bhbj48L2Rpdj4iOwogIH0KICBzZXRIVE1MKCJsb2dCb3giLGgpOwp9CgpmdW5jdGlvbiByZW5kZXJTZXR0aW5ncyhndWFyZHMpIHsKICB2YXIgaXAgPSBnZSgic2VydmVySVAiKS50ZXh0Q29udGVudDsKICBzZXRUZXh0KCJzZXR0aW5nc0lQIiwgaXApOwogIGlmIChTVEFURS5jb25uZWN0ZWQpIHsKICAgIGdlKCJzZXR0aW5nc0Nvbm5lY3RlZCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsKICAgIHNldFRleHQoInNldHRpbmdzQmFsYW5jZSIsIkJhbGFuY2U6ICQiKyhnZSgid2FsQW10IikudGV4dENvbnRlbnR8fCItLSIpKTsKICB9CiAgaWYgKCFndWFyZHMpIHJldHVybjsKICB2YXIgZ2s9T2JqZWN0LmtleXMoZ3VhcmRzKTsgdmFyIGdoPSIiOwogIGZvciAodmFyIGk9MDsgaTxnay5sZW5ndGg7IGkrKykgewogICAgZ2grPSI8ZGl2IGNsYXNzPSdndWFyZC1yb3cnPjxzcGFuIGNsYXNzPSdndWFyZC1rZXknPiIrZ2tbaV0rIjwvc3Bhbj48c3BhbiBjbGFzcz0nZ3VhcmQtdmFsJz4iK2d1YXJkc1tna1tpXV0rIjwvc3Bhbj48L2Rpdj4iOwogIH0KICBzZXRIVE1MKCJndWFyZHJhaWxzTGlzdCIsZ2gpOwp9CgovLyDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZAKLy8gQ09VTlRET1dOCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkApzZXRJbnRlcnZhbChmdW5jdGlvbigpIHsKICBpZiAoIVNUQVRFLm5leHRBdCkgcmV0dXJuOwogIHZhciBkID0gTWF0aC5tYXgoMCwgTWF0aC5yb3VuZCgoU1RBVEUubmV4dEF0IC0gRGF0ZS5ub3coKSkvMTAwMCkpOwogIHZhciBtID0gTWF0aC5mbG9vcihkLzYwKTsgdmFyIHMgPSBkJTYwOwogIHNldFRleHQoInNjYW5DZCIsIGQ+MCA/IChtKyJtICIrcysicyIpIDogIlNjYW5uaW5nLi4uIik7CiAgZ2UoInNjYW5GaWxsIikuc3R5bGUud2lkdGggPSBNYXRoLm1heCgwLCAxMDAtZC9TVEFURS5zY2FuU2VjcyoxMDApKyIlIjsKfSwxMDAwKTsKCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkAovLyBTVEFSVFVQCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkAovLyBMb2FkIHNlcnZlciBJUCBpbW1lZGlhdGVseSAobm8gYXV0aCBuZWVkZWQpCnhocigiL2FwaS9pcCIsIG51bGwsIGZ1bmN0aW9uKHIpIHsKICB2YXIgaXAgPSAociYmci5pcCkgPyByLmlwIDogInVua25vd24iOwogIHNldFRleHQoInNlcnZlcklQIiwgaXApOwogIHNldFRleHQoInNldHRpbmdzSVAiLCBpcCk7Cn0pOwoKLy8gQ2hlY2sgaWYgd2UgaGF2ZSBzYXZlZCBrZXlzCihmdW5jdGlvbigpIHsKICB2YXIgc2F2ZWQgPSBsb2FkS2V5cygpOwogIGlmIChzYXZlZC5rZXkgJiYgc2F2ZWQuc2VjKSB7CiAgICAvLyBQcmUtZmlsbCBpbnB1dHMKICAgIGdlKCJpbnB1dEtleSIpLnZhbHVlICAgID0gc2F2ZWQua2V5OwogICAgZ2UoImlucHV0U2VjcmV0IikudmFsdWUgPSBzYXZlZC5zZWM7CiAgICAvLyBTaG93IHNhdmVkIG5vdGljZQogICAgZ2UoInNhdmVkTm90aWNlIikuc3R5bGUuZGlzcGxheSA9ICJmbGV4IjsKICAgIC8vIEF1dG8tY29ubmVjdAogICAgc2hvd0Nvbm5lY3RNc2coIlJlY29ubmVjdGluZyB3aXRoIHNhdmVkIGtleXMuLi4iLCAiIik7CiAgICB4aHIoIi9hcGkvY29ubmVjdCIsIHthcGlfa2V5OnNhdmVkLmtleSwgYXBpX3NlY3JldDpzYXZlZC5zZWN9LCBmdW5jdGlvbihyKSB7CiAgICAgIGlmIChyICYmIHIuc3VjY2VzcykgewogICAgICAgIGdlKCJjb25uZWN0U2NyZWVuIikuc3R5bGUuZGlzcGxheSA9ICJub25lIjsKICAgICAgICBnZSgibGl2ZURhc2giKS5zdHlsZS5kaXNwbGF5ID0gImJsb2NrIjsKICAgICAgICBTVEFURS5jb25uZWN0ZWQgPSB0cnVlOwogICAgICAgIHNob3dDb25uZWN0TXNnKCIiLCAiIik7CiAgICAgICAgcG9sbCgpOwogICAgICB9IGVsc2UgewogICAgICAgIGdlKCJzYXZlZE5vdGljZSIpLnN0eWxlLmRpc3BsYXkgPSAibm9uZSI7CiAgICAgICAgc2hvd0Nvbm5lY3RNc2coIkF1dG8tY29ubmVjdCBmYWlsZWQ6ICIrKHI/ci5tZXNzYWdlOiJjaGVjayBrZXlzIiksICJlcnIiKTsKICAgICAgfQogICAgfSk7CiAgfQp9KSgpOwoKLy8gUG9sbCAvYXBpL3N0YXR1cyBldmVyeSA0IHNlY29uZHMKZnVuY3Rpb24gcG9sbCgpIHsKICB4aHIoIi9hcGkvc3RhdHVzIiwgbnVsbCwgZnVuY3Rpb24ocykgeyBpZihzKSByZW5kZXIocyk7IH0pOwp9CmlmIChTVEFURS5jb25uZWN0ZWQpIHBvbGwoKTsKc2V0SW50ZXJ2YWwoZnVuY3Rpb24oKSB7IGlmKFNUQVRFLmNvbm5lY3RlZCkgcG9sbCgpOyB9LCA0MDAwKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo=").decode("utf-8")

@app.route("/")
def index():
    return Response(_DASH, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)