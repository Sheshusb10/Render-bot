"""
ΔLPHA PRO — Institutional Grade Crypto Options Bot
====================================================
Features:
- Multi-timeframe analysis (15m, 1h, 4h, 1d)
- RSI + MACD + Bollinger + Volume + Whale detection
- Market Regime Detection (STRONG_BULL/BULL/NEUTRAL/BEAR/STRONG_BEAR)
- Regime-based trade veto (never fight strong trends)
- Straddle strategy for sideways markets
- Execution engine (size/confidence adjustments)
- Trailing stop with progressive floors
- Profit buffer (profits absorb losses)
- Persistent state (survives restarts)
- IP monitoring with alerts
- Auto position management (expiry, TP, SL)
"""

import time, hmac, hashlib, json, threading, logging, traceback, re, os
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

BASE_URL   = "https://api.india.delta.exchange"
API_KEY    = None
API_SECRET = None

# ── Persistent state file ─────────────────────────────────────────
STATE_FILE = "alpha_state.json"

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except: pass
    return {}

def save_state(data):
    try:
        json.dump(data, open(STATE_FILE, "w"))
    except: pass

# ── Bot State ─────────────────────────────────────────────────────
_saved = load_state()

bot_state = {
    "running":  False,
    "strategy": "pro",
    "interval": 300,
    "log":      [],
    "cycle_lock": False,
    "startup_time": time.time(),
    "last_reset_date": "",  # tracks last daily reset date
    "pending_signal": {},  # signal waiting for confirmation
    "scalper": {
        "enabled": True,
        "active_trade": None,   # current scalper position
        "trades_today": 0,
        "wins": 0,
        "losses": 0,
        "profit": 0.0,
        "max_trades": 8,        # max scalper trades per day
        "target_pct": 8.0,      # take profit at +8%
        "stop_pct": -12.0,      # stop loss at -12%
        "min_confidence": 52,   # min confidence to scalp
    },
    "setup_memory": _saved.get("setup_memory", {
        "STRONG_BEAR_CALL": {"wins": 0, "losses": 0},
        "STRONG_BEAR_PUT":  {"wins": 0, "losses": 0},
        "BEAR_CALL":        {"wins": 0, "losses": 0},
        "BEAR_PUT":         {"wins": 0, "losses": 0},
        "NEUTRAL_STRADDLE": {"wins": 0, "losses": 0},
        "BULL_CALL":        {"wins": 0, "losses": 0},
        "STRONG_BULL_CALL": {"wins": 0, "losses": 0},
    }),
    "stats": {
        "trades_today":       0,
        "wins":               0,
        "losses":             0,
        "daily_pnl":          0.0,
        "starting_balance":   0.0,
        "peak_balance":       0.0,
        "profit_floor":       0.0,
        "current_balance":    0.0,
        "daily_loss_limit_hit": False,
        "consecutive_losses": 0,
        "total_exposure_pct": 0.0,
        "win_rate":           0.0,
        "profit_buffer":      _saved.get("profit_buffer", 0.0),
        "buffer_high":        _saved.get("buffer_high", 0.0),
        "losses_absorbed":    _saved.get("losses_absorbed", 0.0),
        "starting_balance":   _saved.get("starting_balance", 0.0),
        "peak_balance":       _saved.get("peak_balance", 0.0),
        "profit_floor":       _saved.get("profit_floor", 0.0),
        "wins":               _saved.get("wins", 0),
        "losses":             _saved.get("losses", 0),
        "win_rate":           _saved.get("win_rate", 0.0),
        "last_signal":        _saved.get("last_signal", {}),
    },
    "trail_state": _saved.get("trail_state", {}),
}

stop_event = threading.Event()
bot_thread = None
_lock      = threading.Lock()
_ip_state  = {"last": _saved.get("last_ip",""), "current": ""}
_cache     = {"products": [], "ts": 0}

# ── Auth ──────────────────────────────────────────────────────────
def _sig(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def _headers(method, path, body=""):
    ts = str(int(time.time()))
    return {
        "api-key":      API_KEY,
        "timestamp":    ts,
        "signature":    _sig(API_SECRET, method + ts + path + body),
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }

def dx_get(path):
    r = requests.get(BASE_URL + path,
        headers=_headers("GET", path), timeout=12)
    r.raise_for_status(); return r.json()

def dx_post(path, body):
    b = json.dumps(body)
    r = requests.post(BASE_URL + path,
        headers=_headers("POST", path, b), data=b, timeout=12)
    r.raise_for_status(); return r.json()

def dx_delete(path, body):
    b = json.dumps(body)
    r = requests.request("DELETE", BASE_URL + path,
        headers=_headers("DELETE", path, b), data=b, timeout=12)
    r.raise_for_status(); return r.json()

def pub_get(path):
    r = requests.get(BASE_URL + path,
        headers={"Accept": "application/json"}, timeout=12)
    r.raise_for_status(); return r.json()

# ── Logger ────────────────────────────────────────────────────────
def blog(msg, level="info"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with _lock:
        bot_state["log"] = bot_state["log"][-599:] + [
            {"ts": ts, "msg": msg, "type": level}]
    print(f"[{level.upper()}] {msg}")

# ── IP Monitor ────────────────────────────────────────────────────
def check_ip():
    try:
        ip = requests.get("https://ifconfig.me", timeout=5).text.strip()
        if _ip_state["last"] and _ip_state["last"] != ip:
            blog(f"🚨 IP CHANGED: {_ip_state['last']} → {ip} "
                 f"— UPDATE DELTA WHITELIST!", "error")
        _ip_state["last"] = _ip_state["current"] = ip
        s = bot_state["stats"]
        save_state({
            "last_ip":          ip,
            "profit_buffer":    s["profit_buffer"],
            "buffer_high":      s["buffer_high"],
            "losses_absorbed":  s["losses_absorbed"],
            "starting_balance": s["starting_balance"],
            "peak_balance":     s["peak_balance"],
            "profit_floor":     s["profit_floor"],
            "wins":             s["wins"],
            "losses":           s["losses"],
            "win_rate":         s["win_rate"],
            "last_signal":      s["last_signal"],
            "trail_state":      bot_state["trail_state"],
        })
    except: pass

# ── Price + Candles ───────────────────────────────────────────────
BINANCE_SYM = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"
}

# Price cache — avoid hammering Binance every cycle
_price_cache = {}
_price_cache_ttl = 30  # seconds

def get_price(asset):
    # Return cached price if fresh
    cached = _price_cache.get(asset.upper())
    if cached and time.time() - cached["ts"] < _price_cache_ttl:
        return cached["price"]

    sym = BINANCE_SYM.get(asset.upper())
    price = 0.0
    if sym:
        for base in ["https://api.binance.us", "https://api.binance.com"]:
            try:
                r = requests.get(f"{base}/api/v3/ticker/price?symbol={sym}", timeout=5)
                if r.status_code == 200:
                    price = float(r.json()["price"])
                    break
            except: continue
    if not price:
        try:
            gecko = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana"}.get(asset.upper())
            if gecko:
                r = requests.get(
                    f"https://api.coingecko.com/api/v3/simple/price"
                    f"?ids={gecko}&vs_currencies=usd", timeout=5)
                price = float(list(r.json().values())[0]["usd"])
        except: pass

    if price:
        _price_cache[asset.upper()] = {"price": price, "ts": time.time()}
    return price

def get_candles(asset, interval="1h", limit=50):
    sym = BINANCE_SYM.get(asset.upper())
    if not sym: return []
    # Binance US works from Render servers
    url = f"https://api.binance.us/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return [{"open": float(k[1]), "high": float(k[2]),
                         "low":  float(k[3]), "close": float(k[4]),
                         "volume": float(k[5])} for k in data]
    except Exception as e:
        blog(f"[{asset}] binance.us failed: {e}", "warning")

    # Fallback to global Binance
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}",
            timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return [{"open": float(k[1]), "high": float(k[2]),
                         "low":  float(k[3]), "close": float(k[4]),
                         "volume": float(k[5])} for k in data]
    except Exception as e:
        blog(f"[{asset}] binance.com failed: {e}", "warning")

    blog(f"[{asset}] All candle sources failed for {interval}", "error")
    return []

# ── Technical Indicators ──────────────────────────────────────────
def calc_rsi(candles, period=14):
    if len(candles) < period + 1: return 50.0
    closes = [c["close"] for c in candles]
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return round(100 - (100 / (1 + ag/al)), 2)

def calc_ema(values, period):
    if len(values) < period: return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]: ema = v * k + ema * (1 - k)
    return round(ema, 6)

def calc_macd(candles):
    if len(candles) < 26:
        return {"macd": 0, "signal": 0, "histogram": 0, "trend": "NEUTRAL"}
    closes = [c["close"] for c in candles]
    ema12  = calc_ema(closes, 12)
    ema26  = calc_ema(closes, 26)
    macd_line = ema12 - ema26
    macd_series = [calc_ema(closes[:i], 12) - calc_ema(closes[:i], 26)
                   for i in range(26, len(closes)+1)]
    signal_line = calc_ema(macd_series, 9) if len(macd_series) >= 9 else macd_line
    histogram   = macd_line - signal_line
    return {
        "macd":      round(macd_line, 4),
        "signal":    round(signal_line, 4),
        "histogram": round(histogram, 4),
        "trend":     "BULLISH" if histogram > 0 else "BEARISH",
    }

def calc_bollinger(candles, period=20):
    if len(candles) < period:
        return {"upper":0,"middle":0,"lower":0,"position":"MIDDLE","squeeze":False}
    closes = [c["close"] for c in candles[-period:]]
    mid    = sum(closes) / period
    std    = (sum((c-mid)**2 for c in closes) / period) ** 0.5
    upper  = mid + 2*std; lower = mid - 2*std
    price  = closes[-1]
    bw     = (upper - lower) / mid * 100
    pos    = ("UPPER" if price > upper*0.98
              else "LOWER" if price < lower*1.02 else "MIDDLE")
    return {"upper": round(upper,2), "middle": round(mid,2),
            "lower": round(lower,2), "position": pos, "squeeze": bw < 2.0}

def calc_volume_analysis(candles):
    if len(candles) < 10:
        return {"spike":False,"fake_pump":False,"fake_dump":False,
                "volume_trend":"NEUTRAL","whale_detected":False,"vol_ratio":1}
    vols    = [c["volume"] for c in candles]
    avg_vol = sum(vols[-20:]) / min(20, len(vols))
    last_vol = vols[-1]
    chg = ((candles[-1]["close"]-candles[-2]["close"])/candles[-2]["close"]*100)
    return {
        "spike":          last_vol > avg_vol * 3,
        "fake_pump":      chg > 1.5 and last_vol < avg_vol * 0.7,
        "fake_dump":      chg < -1.5 and last_vol < avg_vol * 0.7,
        "volume_trend":   ("RISING" if last_vol > avg_vol*1.2
                           else "FALLING" if last_vol < avg_vol*0.8 else "NORMAL"),
        "whale_detected": last_vol > avg_vol * 5,
        "vol_ratio":      round(last_vol / avg_vol, 2) if avg_vol else 0,
    }

def detect_pattern(candles):
    if len(candles) < 2: return "NONE"
    c = candles[-1]; pc = candles[-2]
    body = abs(c["close"] - c["open"])
    full = c["high"] - c["low"]
    if full == 0: return "NONE"
    uw = c["high"] - max(c["close"], c["open"])
    lw = min(c["close"], c["open"]) - c["low"]
    if body/full < 0.1:          return "DOJI"
    if lw > body*2 and uw < body*0.5: return "HAMMER_BULLISH"
    if uw > body*2 and lw < body*0.5: return "SHOOTING_STAR_BEARISH"
    if (c["close"] > c["open"] and pc["close"] < pc["open"]
            and c["open"] < pc["close"] and c["close"] > pc["open"]):
        return "BULLISH_ENGULFING"
    if (c["close"] < c["open"] and pc["close"] > pc["open"]
            and c["open"] > pc["close"] and c["close"] < pc["open"]):
        return "BEARISH_ENGULFING"
    return "NONE"

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        v = int(d["value"])
        return {"value": v, "label": d["value_classification"],
                "sentiment": "FEAR" if v<40 else "GREED" if v>60 else "NEUTRAL"}
    except:
        return {"value": 50, "label": "Neutral", "sentiment": "NEUTRAL"}

def get_crypto_sentiment():
    """
    Read crypto news sentiment from CryptoCompare free API.
    Returns: BULLISH / BEARISH / NEUTRAL + key headlines
    """
    try:
        r = requests.get(
            "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=latest",
            timeout=8)
        articles = r.json().get("Data", [])[:10]
        bull_words = ["rally","surge","rise","pump","bull","gains","up","high","record","buy"]
        bear_words = ["crash","dump","fall","bear","loss","down","low","sell","fear","drop"]
        bull_count = 0; bear_count = 0; headlines = []
        for a in articles:
            title = a.get("title","").lower()
            headlines.append(a.get("title","")[:60])
            bull_count += sum(1 for w in bull_words if w in title)
            bear_count += sum(1 for w in bear_words if w in title)
        total = bull_count + bear_count or 1
        bull_pct = bull_count / total * 100
        sentiment = "BULLISH" if bull_pct > 60 else "BEARISH" if bull_pct < 40 else "NEUTRAL"
        return {"sentiment": sentiment, "bull_pct": round(bull_pct),
                "headlines": headlines[:3]}
    except:
        return {"sentiment": "NEUTRAL", "bull_pct": 50, "headlines": []}

def check_delta_status():
    """Check if Delta Exchange API is healthy."""
    try:
        r = requests.get(
            "https://api.india.delta.exchange/v2/products?states=live&page_size=1",
            headers={"Accept": "application/json"}, timeout=8)
        if r.status_code == 200:
            return True, "OK"
        elif r.status_code == 503:
            return False, "Maintenance"
        else:
            return False, f"Error {r.status_code}"
    except Exception as e:
        return False, f"Unreachable: {str(e)[:30]}"

def get_funding_rate(asset):
    sym = BINANCE_SYM.get(asset.upper())
    if not sym: return 0.0
    try:
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/fundingRate"
            f"?symbol={sym}&limit=1", timeout=5)
        d = r.json()
        if d and isinstance(d, list):
            return float(d[0].get("fundingRate", 0)) * 100
    except: pass
    return 0.0

def get_oi_trend(asset):
    sym = BINANCE_SYM.get(asset.upper())
    if not sym: return "STABLE"
    try:
        r = requests.get(
            f"https://fapi.binance.com/futures/data/openInterestHist"
            f"?symbol={sym}&period=1h&limit=5", timeout=5)
        d = r.json()
        if isinstance(d, list) and len(d) >= 2:
            vals = [float(x["sumOpenInterest"]) for x in d]
            if vals[-1] > vals[0]*1.02: return "RISING"
            if vals[-1] < vals[0]*0.98: return "FALLING"
    except: pass
    return "STABLE"

# ── Regime Detection ──────────────────────────────────────────────
def detect_market_regime(chg_1h, chg_4h, chg_1d, rsi_4h):
    """
    Classify market regime based on multi-timeframe price action.
    STRONG_BULL / BULL / NEUTRAL / BEAR / STRONG_BEAR
    """
    bull_points = 0
    bear_points = 0

    # 4h momentum
    if chg_4h > 2:   bull_points += 30
    elif chg_4h > 1: bull_points += 15
    elif chg_4h < -2: bear_points += 30
    elif chg_4h < -1: bear_points += 15

    # Daily momentum
    if chg_1d > 3:   bull_points += 30
    elif chg_1d > 1: bull_points += 15
    elif chg_1d < -3: bear_points += 30
    elif chg_1d < -1: bear_points += 15

    # RSI regime
    if rsi_4h > 60:  bull_points += 20
    elif rsi_4h < 40: bear_points += 20

    total = bull_points + bear_points or 1
    bull_pct = bull_points / total * 100

    if bull_pct >= 75:   return "STRONG_BULL"
    elif bull_pct >= 55: return "BULL"
    elif bull_pct <= 25: return "STRONG_BEAR"
    elif bull_pct <= 45: return "BEAR"
    else:                return "NEUTRAL"

# ── Full Analysis Engine ──────────────────────────────────────────
def full_analysis(asset):
    blog(f"[{asset}] Analyzing...", "info")
    price = get_price(asset)
    if not price: return None

    c1h  = get_candles(asset, "1h",  50)
    c4h  = get_candles(asset, "4h",  50)
    c1d  = get_candles(asset, "1d",  30)
    c15m = get_candles(asset, "15m", 20)
    if not c1h or not c4h: return None

    rsi_1h  = calc_rsi(c1h)
    rsi_4h  = calc_rsi(c4h)
    rsi_1d  = calc_rsi(c1d) if c1d else 50
    macd_1h = calc_macd(c1h)
    macd_4h = calc_macd(c4h)
    bb_1h   = calc_bollinger(c1h)
    vol     = calc_volume_analysis(c1h)
    pattern = detect_pattern(c1h)
    fg      = get_fear_greed()
    funding = get_funding_rate(asset)
    oi      = get_oi_trend(asset)
    news    = get_crypto_sentiment()
    blog(f"[{asset}] News:{news['sentiment']} ({news['bull_pct']}% bull) | "
         f"Headline: {news['headlines'][0] if news['headlines'] else 'none'}", "info")

    chg_1h = ((c1h[-1]["close"]-c1h[-2]["close"])/c1h[-2]["close"]*100) if len(c1h)>=2 else 0
    chg_4h = ((c4h[-1]["close"]-c4h[-2]["close"])/c4h[-2]["close"]*100) if len(c4h)>=2 else 0
    chg_1d = ((c1d[-1]["close"]-c1d[-2]["close"])/c1d[-2]["close"]*100) if c1d and len(c1d)>=2 else 0

    # Market regime
    regime = detect_market_regime(chg_1h, chg_4h, chg_1d, rsi_4h)

    # ── Volatility Regime ─────────────────────────────────────────
    bb_width = ((bb_1h["upper"] - bb_1h["lower"]) / bb_1h["middle"] * 100
                if bb_1h["middle"] > 0 else 3.0)
    if bb_width < 2.0:
        vol_regime = "LOW"    # straddle zone
    elif bb_width > 5.0:
        vol_regime = "HIGH"   # trend trade zone
    else:
        vol_regime = "MID"    # normal directional

    blog(f"[{asset}] VolRegime:{vol_regime} BB_width:{bb_width:.2f}%", "info")

    highs = [c["high"] for c in c1h[-20:]]
    lows  = [c["low"]  for c in c1h[-20:]]
    support    = round(min(lows), 2)
    resistance = round(max(highs), 2)

    bull = 0; bear = 0; reasons = []

    # ── Volatility spike kill switch ─────────────────────────────
    # If price moved >5% in last hour = abnormal, skip trading
    if abs(chg_1h) > 8.0:
        blog(f"[{asset}] ⚠️ Volatility spike {chg_1h:.1f}% — skipping", "warning")
        return {
            "asset": asset, "price": price, "direction": "NO TRADE",
            "confidence": 0, "bull_score": 0, "bear_score": 0,
            "regime": regime, "candles_4h": c4h, "candles_15m": c15m,
            "indicators": {"rsi_1h": rsi_1h, "rsi_4h": rsi_4h, "rsi_1d": rsi_1d,
                          "macd_1h": macd_1h["trend"], "macd_4h": macd_4h["trend"],
                          "bb_1h": bb_1h["position"], "pattern": "NONE",
                          "volume": vol["volume_trend"], "whale": False,
                          "fake_pump": False, "fake_dump": False,
                          "fear_greed": 50, "funding": 0, "oi": "STABLE",
                          "chg_1h": round(chg_1h,2), "chg_4h": round(chg_4h,2),
                          "chg_1d": round(chg_1d,2)},
            "support": 0, "resistance": 0,
            "reasons": [f"Volatility spike {chg_1h:.1f}% — trading paused"],
        }

    # ── Extreme oversold bounce signal ────────────────────────────
    if rsi_1h < 25 and bb_1h["position"] == "LOWER":
        bull += 20
        reasons.append(f"🚨 Extreme oversold RSI {rsi_1h} — bounce signal")
    if rsi_1h < 20:
        bull += 15
        reasons.append(f"RSI {rsi_1h} — historic oversold level")

    # ── RSI — adjusted thresholds based on regime ─────────────────
    if regime in ("STRONG_BULL", "BULL"):
        # In bull market RSI 60-75 is healthy, not overbought
        if rsi_1h < 35:  bull += 15; reasons.append(f"RSI 1h oversold {rsi_1h}")
        elif rsi_1h > 78: bear += 10; reasons.append(f"RSI 1h extreme {rsi_1h}")
        if rsi_4h < 40:  bull += 20; reasons.append(f"RSI 4h oversold {rsi_4h}")
        elif rsi_4h > 78: bear += 10; reasons.append(f"RSI 4h extreme {rsi_4h}")
        if rsi_1d < 40:  bull += 25; reasons.append(f"RSI daily oversold {rsi_1d}")
        elif rsi_1d > 80: bear += 15; reasons.append(f"RSI daily extreme {rsi_1d}")
    else:
        # Normal thresholds for neutral/bear
        if rsi_1h < 35:  bull += 15; reasons.append(f"RSI 1h oversold {rsi_1h}")
        elif rsi_1h > 65: bear += 15; reasons.append(f"RSI 1h overbought {rsi_1h}")
        if rsi_4h < 40:  bull += 20; reasons.append(f"RSI 4h oversold {rsi_4h}")
        elif rsi_4h > 60: bear += 20; reasons.append(f"RSI 4h overbought {rsi_4h}")
        if rsi_1d < 40:  bull += 25; reasons.append(f"RSI daily oversold {rsi_1d}")
        elif rsi_1d > 60: bear += 25; reasons.append(f"RSI daily overbought {rsi_1d}")

    # ── MACD — halved weight in strong trend ──────────────────────
    macd_1h_weight = 8  if regime in ("STRONG_BULL","STRONG_BEAR") else 15
    macd_4h_weight = 10 if regime in ("STRONG_BULL","STRONG_BEAR") else 20

    if macd_1h["trend"] == "BULLISH":
        bull += macd_1h_weight; reasons.append("MACD 1h bullish")
    else:
        bear += macd_1h_weight; reasons.append("MACD 1h bearish")

    if macd_4h["trend"] == "BULLISH":
        bull += macd_4h_weight; reasons.append("MACD 4h bullish")
    else:
        bear += macd_4h_weight; reasons.append("MACD 4h bearish")

    # ── Bollinger — upper band is bullish in bull regime ──────────
    if regime in ("STRONG_BULL", "BULL"):
        if bb_1h["position"] == "UPPER":
            bull += 5;  reasons.append("Price walking upper BB — bull trend")
        elif bb_1h["position"] == "LOWER":
            bull += 15; reasons.append("At lower BB in bull — buy dip")
    else:
        if bb_1h["position"] == "LOWER":
            bull += 10; reasons.append("At lower BB — oversold")
        elif bb_1h["position"] == "UPPER":
            bear += 10; reasons.append("At upper BB — overbought")
    if bb_1h["squeeze"]: reasons.append("BB squeeze — big move incoming")

    # ── Volume / Whale ────────────────────────────────────────────
    if vol["fake_pump"]:  bear += 25; reasons.append("⚠️ FAKE PUMP detected")
    if vol["fake_dump"]:  bull += 25; reasons.append("⚠️ FAKE DUMP detected")
    if vol["whale_detected"]:
        reasons.append(f"🐋 WHALE {vol['vol_ratio']}x volume")
        if chg_1h > 0: bull += 15
        else:           bear += 15
    if vol["volume_trend"] == "RISING" and chg_1h > 0:
        bull += 10; reasons.append("Volume+price rising")
    if vol["volume_trend"] == "RISING" and chg_1h < 0:
        bear += 10; reasons.append("Volume rising on dump")

    # ── Patterns ──────────────────────────────────────────────────
    pm = {
        "HAMMER_BULLISH":       (15,"bull"),
        "BULLISH_ENGULFING":    (25,"bull"),
        "SHOOTING_STAR_BEARISH":(15,"bear"),
        "BEARISH_ENGULFING":    (25,"bear"),
    }
    if pattern in pm:
        pts, side = pm[pattern]
        if side == "bull": bull += pts
        else:              bear += pts
        reasons.append(f"Pattern: {pattern}")

    # ── Fear & Greed ──────────────────────────────────────────────
    if fg["sentiment"] == "FEAR" and chg_4h > 0:
        bull += 10; reasons.append("Fear+recovery = buy")
    elif fg["sentiment"] == "GREED" and chg_4h < 0:
        bear += 10; reasons.append("Greed+falling = sell")

    # ── News Sentiment ────────────────────────────────────────────
    if news["sentiment"] == "BULLISH":
        bull += 8; reasons.append(f"News bullish ({news['bull_pct']}%)")
    elif news["sentiment"] == "BEARISH":
        bear += 8; reasons.append(f"News bearish ({100-news['bull_pct']}%)")

    # ── Funding — ignored in strong bull regime ───────────────────
    if regime not in ("STRONG_BULL", "BULL"):
        if funding > 0.05:
            bear += 15; reasons.append(f"High funding {funding:.3f}%")
        elif funding < -0.02:
            bull += 15; reasons.append("Negative funding — squeeze possible")

    # ── OI ────────────────────────────────────────────────────────
    if oi == "RISING" and chg_1h > 0:   bull += 10
    elif oi == "RISING" and chg_1h < 0: bear += 10

    # ── Momentum ─────────────────────────────────────────────────
    if chg_1h > 1:   bull += 10
    if chg_1h < -1:  bear += 10
    if chg_4h > 2:   bull += 15
    if chg_4h < -2:  bear += 15
    if chg_1d > 3:   bull += 10
    if chg_1d < -3:  bear += 10

    # ── Support/Resistance ────────────────────────────────────────
    if (price - support) / price * 100 < 1.0:
        bull += 15; reasons.append(f"Near support ${support}")
    if (resistance - price) / price * 100 < 1.0:
        bear += 15; reasons.append(f"Near resistance ${resistance}")

    total    = bull + bear or 1
    bull_pct = round(bull / total * 100, 1)
    bear_pct = round(bear / total * 100, 1)

    # ── Direction with minimum confidence 75% ────────────────────
    if bull > bear and bull_pct >= 55:
        direction = "BUY_CALL"; conf = bull_pct
    elif bear > bull and bear_pct >= 55:
        direction = "BUY_PUT";  conf = bear_pct
    else:
        direction = "NO TRADE"; conf = max(bull_pct, bear_pct)

    # Straddle disabled — directional only for now
    # (MV products too risky near expiry)

    # High vol — only allow trend following trades
    if vol_regime == "HIGH" and direction != "NO TRADE":
        # In high vol only block weak counter-trend setups
        is_with_trend = (
            (direction == "BUY_CALL" and chg_4h > 0) or
            (direction == "BUY_PUT"  and chg_4h < 0)
        )
        if not is_with_trend and conf < 70:
            direction = "NO TRADE"
            reasons.append(f"High vol ({bb_width:.1f}%) + weak conf — blocked")

    # ── Trade Memory Adjustment ──────────────────────────────────
    # If this setup historically loses, reduce confidence
    setup_key = f"{regime}_{direction.replace('BUY_','')}" if direction != "NO TRADE" else None
    if setup_key:
        memory = bot_state.get("setup_memory", {}).get(setup_key, {})
        mem_wins   = memory.get("wins", 0)
        mem_losses = memory.get("losses", 0)
        mem_total  = mem_wins + mem_losses
        if mem_total >= 3:  # need at least 3 trades to learn
            mem_winrate = mem_wins / mem_total * 100
            if mem_winrate < 35:
                conf = max(0, conf - 10)
                reasons.append(f"Setup {setup_key} hist {mem_winrate:.0f}% WR — reduced conf")
            elif mem_winrate > 65:
                conf = min(100, conf + 5)
                reasons.append(f"Setup {setup_key} hist {mem_winrate:.0f}% WR — boosted conf")

    # ── Regime Hard Veto ─────────────────────────────────────────
    # Dynamic regime veto — tighter in high volatility
    vol_factor = 0
    if c1h:
        recent_range = max(c["high"] for c in c1h[-5:]) - min(c["low"] for c in c1h[-5:])
        avg_range = sum(c["high"]-c["low"] for c in c1h[-20:]) / 20 if len(c1h) >= 20 else recent_range
        if recent_range > avg_range * 1.5:
            vol_factor = 5  # tighten veto in high vol
        elif recent_range < avg_range * 0.7:
            vol_factor = -5  # loosen veto in low vol

    veto_thresholds = {
        "STRONG_BULL": {"PUT": 60 + vol_factor, "CALL": 0},
        "BULL":        {"PUT": 58 + vol_factor, "CALL": 0},
        "NEUTRAL":     {"PUT": 0,               "CALL": 0},
        "BEAR":        {"PUT": 0,  "CALL": 52 + vol_factor},
        "STRONG_BEAR": {"PUT": 0,  "CALL": 55 + vol_factor},
    }
    thresholds = veto_thresholds.get(regime, {"PUT": 0, "CALL": 0})

    if direction == "BUY_PUT" and bear_pct < thresholds["PUT"]:
        direction = "NO TRADE"
        reasons.append(f"PUT vetoed — {regime} requires {thresholds['PUT']}% bear (got {bear_pct}%)")

    if direction == "BUY_CALL" and bull_pct < thresholds["CALL"]:
        direction = "NO TRADE"
        reasons.append(f"CALL vetoed — {regime} requires {thresholds['CALL']}% bull (got {bull_pct}%)")

    # ── Fake pump/dump block ──────────────────────────────────────
    if direction == "BUY_CALL" and vol["fake_pump"]:
        direction = "NO TRADE"; reasons.append("CALL blocked — fake pump")
    if direction == "BUY_PUT" and vol["fake_dump"]:
        direction = "NO TRADE"; reasons.append("PUT blocked — fake dump")

    blog(f"[{asset}] Regime:{regime} | Bull:{bull_pct}% Bear:{bear_pct}% "
         f"→ {direction} ({conf:.1f}%)", "info")

    result = {
        "asset":      asset,
        "price":      price,
        "direction":  direction,
        "confidence": round(conf, 1),
        "bull_score": bull,
        "bear_score": bear,
        "regime":     regime,
        "candles_4h": c4h,
        "candles_15m":c15m,
        "indicators": {
            "rsi_1h":   rsi_1h, "rsi_4h": rsi_4h, "rsi_1d": rsi_1d,
            "macd_1h":  macd_1h["trend"], "macd_4h": macd_4h["trend"],
            "bb_1h":    bb_1h["position"], "pattern": pattern,
            "volume":   vol["volume_trend"], "whale": vol["whale_detected"],
            "fake_pump":vol["fake_pump"], "fake_dump": vol["fake_dump"],
            "fear_greed": fg["value"], "funding": funding, "oi": oi,
            "chg_1h":   round(chg_1h,2), "chg_4h": round(chg_4h,2),
            "chg_1d":   round(chg_1d,2),
        },
        "support":    support,
        "resistance": resistance,
        "reasons":    reasons[:8],
    }
    return result

# ── Execution Engine ──────────────────────────────────────────────
def execution_engine(analysis):
    """
    Never blocks trades — adjusts size and confidence instead.
    Big edge → big size. Weak edge → small size.
    """
    direction  = analysis["direction"]
    confidence = analysis["confidence"]
    price      = analysis["price"]
    asset      = analysis["asset"]
    c4h        = analysis.get("candles_4h", [])
    c15m       = analysis.get("candles_15m", [])

    if direction in ("NO TRADE",): return analysis

    result = {
        "size_multiplier": 1.0,
        "confidence":      confidence,
        "aggression":      "NORMAL",
        "log":             [],
    }

    # 4h momentum
    move_4h = 0
    if len(c4h) >= 2:
        p4h = c4h[-2]["close"]
        move_4h = ((price - p4h) / p4h * 100) if p4h else 0

    # ATR threshold
    if len(c4h) >= 14:
        atr = sum(c["high"]-c["low"] for c in c4h[-14:]) / 14
        threshold = (atr / price) * 100 * 1.2
    else:
        threshold = 1.5

    # Classify momentum
    if   move_4h > threshold:  momentum = "STRONG_UP"
    elif move_4h < -threshold: momentum = "STRONG_DOWN"
    else:                      momentum = "NEUTRAL"

    # Acceleration
    acceleration = "DECREASING"
    if len(c4h) >= 4:
        recent = abs(c4h[-1]["close"] - c4h[-2]["close"])
        prev   = abs(c4h[-3]["close"] - c4h[-4]["close"])
        acceleration = "INCREASING" if recent > prev else "DECREASING"

    is_put  = "PUT"  in direction
    is_call = "CALL" in direction

    # Counter-trend logic
    if momentum == "STRONG_UP" and is_put:
        if acceleration == "INCREASING":
            result["size_multiplier"] = 0.15
            result["confidence"]     -= 15
            result["aggression"]      = "LOW"
            result["log"].append("Counter-trend high risk — tiny size 0.15x")
        else:
            result["size_multiplier"] = 0.4
            result["confidence"]     -= 5
            result["aggression"]      = "MEDIUM"
            result["log"].append("Counter-trend — momentum slowing 0.4x")

    elif momentum == "STRONG_DOWN" and is_call:
        if acceleration == "INCREASING":
            result["size_multiplier"] = 0.15
            result["confidence"]     -= 15
            result["aggression"]      = "LOW"
            result["log"].append("Counter-trend high risk — tiny size 0.15x")
        else:
            result["size_multiplier"] = 0.4
            result["confidence"]     -= 5
            result["aggression"]      = "MEDIUM"
            result["log"].append("Counter-trend — momentum slowing 0.4x")

    # Trend-following boost
    elif ((momentum == "STRONG_UP" and is_call) or
          (momentum == "STRONG_DOWN" and is_put)):
        result["size_multiplier"] *= 1.5
        result["confidence"]     += 5
        result["aggression"]      = "HIGH"
        result["log"].append("Trend-following boost 1.5x")

    # 15m micro-timing
    if len(c15m) >= 2:
        last = c15m[-1]; prev = c15m[-2]
        last_bull = last["close"] > last["open"]
        prev_bull = prev["close"] > prev["open"]
        if is_call and last_bull and prev_bull:
            result["confidence"] += 5
            result["log"].append("15m aligned bullish")
        elif is_put and not last_bull and not prev_bull:
            result["confidence"] += 5
            result["log"].append("15m aligned bearish")
        elif ((is_call and not last_bull) or (is_put and last_bull)):
            body = abs(last["close"]-last["open"])
            pbody = abs(prev["close"]-prev["open"])
            if body > pbody * 1.5:
                result["log"].append("Sharp opposite 15m candle — noted only")

    # Final confidence gate
    final_conf = max(0, min(100, result["confidence"]))
    if final_conf < 50:
        result["size_multiplier"] *= 0.3
        result["aggression"] = "LOW"
        result["log"].append(f"Low conf {final_conf}% — tiny size")

    result["confidence"] = final_conf

    blog(f"[{asset}] Engine: {result['aggression']} | "
         f"Size:{result['size_multiplier']:.2f}x | "
         f"Conf:{final_conf}% | "
         f"{' | '.join(result['log'])}", "bot")

    analysis["confidence"]      = final_conf
    analysis["size_multiplier"] = result["size_multiplier"]
    analysis["aggression"]      = result["aggression"]
    return analysis

# ── Balance + Risk ────────────────────────────────────────────────
def refresh_balance():
    try:
        data = dx_get("/v2/wallet/balances")
        best = 0.0
        for w in (data.get("result") or []):
            if w.get("asset_symbol") in ("USDT","INR","USD"):
                bal = float(w.get("available_balance") or 0)
                if bal < 1: continue
                if w.get("asset_symbol") == "INR": bal = round(bal/85.0, 4)
                if bal > best: best = bal
        if best <= 0: return 0.0
        s = bot_state["stats"]
        s["current_balance"] = best
        if s["starting_balance"] == 0:
            s["starting_balance"] = best
            s["peak_balance"]     = best
            s["profit_floor"]     = round(best * 0.90, 4)
            blog(f"Balance: {best:.2f} | Floor: {s['profit_floor']:.2f}", "success")
        if best > s["peak_balance"]:
            s["peak_balance"] = best
            s["profit_floor"] = max(s["profit_floor"], round(best*0.92, 4))
            blog(f"New peak {best:.2f} — floor {s['profit_floor']:.2f}", "success")
        return best
    except Exception as e:
        blog(f"Balance error: {e}", "error"); return 0.0

def risk_ok():
    s = bot_state["stats"]
    if s["daily_loss_limit_hit"]: return False, "Daily limit hit"
    if s["current_balance"] > 0 and s["profit_floor"] > 0:
        if s["current_balance"] < s["profit_floor"]:
            s["daily_loss_limit_hit"] = True
            return False, "Profit floor breached"
    # Auto-scale daily cap based on win rate
    total_trades = s["wins"] + s["losses"]
    if total_trades >= 5:
        win_rate = s["wins"] / total_trades * 100
        if win_rate > 50:
            daily_cap = 999  # winning streak — no cap
        elif win_rate >= 40:
            daily_cap = 12   # neutral
        else:
            daily_cap = 8    # losing — slow down
    else:
        daily_cap = 12  # default before enough data
    if s["trades_today"] >= daily_cap:
        return False, f"Max {daily_cap} trades/day (WR-based)"
    if s["consecutive_losses"] >= 4: return False, "4 consecutive losses — pause"
    return True, "OK"

def get_products_cached():
    now = time.time()
    if now - _cache["ts"] > 120 or not _cache["products"]:
        try:
            _cache["products"] = pub_get(
                "/v2/products?states=live&page_size=500").get("result",[])
            _cache["ts"] = now
        except: pass
    return _cache["products"]

# ── Position Manager ──────────────────────────────────────────────
def manage_positions():
    """Auto-manage all open positions."""
    try:
        positions = dx_get("/v2/positions/margined").get("result",[])
        open_pos  = [p for p in positions if abs(float(p.get("size",0))) > 0]
        if not open_pos: return

        products = get_products_cached()
        now      = datetime.now(timezone.utc)

        for pos in open_pos:
            sym   = pos.get("product_symbol","")
            size  = abs(float(pos.get("size",0)))
            entry = float(pos.get("entry_price",0))
            upnl  = float(pos.get("unrealized_pnl",0))
            side  = "buy" if float(pos.get("size",0)) > 0 else "sell"
            mark  = float(pos.get("mark_price",0) or 0)

            # PnL calculation
            entry_val = entry * size
            mark_val  = mark * size
            pnl_pct   = ((mark_val - entry_val) / entry_val * 100) if entry_val else 0

            blog(f"[{sym}] Entry:{entry} Mark:{mark:.2f} PnL:{pnl_pct:.1f}%","info")

            should_close = False
            close_reason = ""
            s = bot_state["stats"]  # needed for dynamic stop loss

            # Expiry check
            try:
                parts      = sym.split("-")
                expiry_str = parts[-1]
                day   = int(expiry_str[0:2])
                month = int(expiry_str[2:4])
                year  = int("20"+expiry_str[4:6])
                expiry_dt  = datetime(year, month, day, 8, 0, 0, tzinfo=timezone.utc)
                hours_left = (expiry_dt - now).total_seconds() / 3600
                blog(f"[{sym}] Expiry: {day}/{month}/{year} | Hours:{hours_left:.1f}","info")

                if hours_left < 24 and pnl_pct < -5:
                    should_close = True
                    close_reason = f"Expiry {hours_left:.1f}h + losing >5% — closing"
                elif hours_left < 6:
                    should_close = True
                    close_reason = f"Expiry in {hours_left:.1f}h — salvaging value"
            except: pass

            # Dynamic stop loss — tightens based on consecutive losses
            consec = s.get("consecutive_losses", 0)
            if consec >= 3:
                stop_loss = -10   # 3+ losses — cut quick
            elif consec >= 2:
                stop_loss = -15   # 2 losses — tighter
            else:
                stop_loss = -20   # normal

            if not should_close and pnl_pct <= stop_loss:
                should_close = True
                close_reason = f"Stop loss {stop_loss}% (consec:{consec}): {pnl_pct:.1f}%"

            # Take profit ceiling at 100% — let winners run!
            if not should_close and pnl_pct >= 100:
                should_close = True
                close_reason = f"🎯 Take profit 100%: {pnl_pct:.1f}%"

            # Trailing stop with precise floors
            trail = bot_state["trail_state"].get(sym, {"peak": 0, "floor": None})
            if pnl_pct > trail["peak"]:
                trail["peak"] = pnl_pct
                # Progressive floors — ceil at each level, floor locks in gains
                # Every level covered — floor = peak minus ~10-15% buffer
                # Formula: floor = peak * 0.88 (keep 88% of gains)
                # But use fixed steps for clean numbers
                floors = [
                    (100, 90), (95, 85), (90, 80), (85, 75), (80, 72),
                    (75, 67),  (70, 62), (65, 58), (60, 53), (55, 48),
                    (50, 45),  (48, 43), (45, 40), (42, 37), (40, 35),
                    (38, 33),  (35, 30), (33, 28), (30, 26), (28, 24),
                    (26, 22),  (25, 21), (24, 20), (23, 19), (22, 18),
                    (21, 17),  (20, 16), (19, 15), (18, 14), (17, 13),
                    (16, 12),  (15, 11), (14, 10), (13,  9), (12,  9),
                    (11,  8),  (10,  8), (9,   7), (8,   6), (7,   5),
                    (6,   4),  (5, 3.5), (4,   3), (3,   2),
                ]
                for threshold, floor in floors:
                    if pnl_pct >= threshold:
                        if trail["floor"] is None or floor > trail["floor"]:
                            trail["floor"] = floor
                            blog(f"[{sym}] 🔒 Trail floor +{floor}% (peak +{pnl_pct:.1f}%)", "success")
                        break
                bot_state["trail_state"][sym] = trail

            if (not should_close and trail["floor"] is not None
                    and pnl_pct < trail["floor"]):
                should_close = True
                close_reason = (f"🔒 Trail stop: peak +{trail['peak']:.1f}% "
                                f"floor +{trail['floor']}% now +{pnl_pct:.1f}%")

            if should_close:
                blog(f"[{sym}] Closing: {close_reason}", "warning")
                product = next((p for p in products
                    if p.get("symbol") == sym), None)
                if product:
                    try:
                        close_side = "sell" if side == "buy" else "buy"
                        is_mv = "MV-" in sym
                        order_body = {
                            "product_id":     product["id"],
                            "product_symbol": sym,
                            "size":           int(size),
                            "side":           close_side,
                            "order_type":     "market_order",
                        }
                        if not is_mv:
                            order_body["reduce_only"] = "true"
                        resp = dx_post("/v2/orders", order_body)
                        if resp.get("error"):
                            blog(f"[{sym}] Close FAILED: {resp['error']}", "error")
                        elif not resp.get("result"):
                            blog(f"[{sym}] Close no result: {str(resp)[:100]}", "error")
                        else:
                            blog(f"[{sym}] ✓ Closed automatically | PnL:{pnl_pct:.1f}%", "success")
                            # Use pnl_pct as source of truth (upnl can be stale)
                            realized = entry_val * (pnl_pct / 100)
                            s = bot_state["stats"]
                            if pnl_pct > 0:
                                s["wins"]              += 1
                                s["consecutive_losses"]  = 0  # reset on win
                                s["profit_buffer"]      += abs(realized)
                                s["buffer_high"]         = max(s["buffer_high"], s["profit_buffer"])
                                blog(f"✅ WIN +{pnl_pct:.1f}% | Buffer: +${abs(realized):.3f} = ${s['profit_buffer']:.3f}","success")
                            else:
                                s["losses"]            += 1
                                s["consecutive_losses"] += 1
                                loss_amt = abs(realized)
                                if s["profit_buffer"] >= loss_amt:
                                    s["profit_buffer"]   -= loss_amt
                                    s["losses_absorbed"] += loss_amt
                                    blog(f"❌ LOSS {pnl_pct:.1f}% | Absorbed by buffer | Buffer: ${s['profit_buffer']:.3f}","warning")
                                else:
                                    remaining = loss_amt - s["profit_buffer"]
                                    s["profit_buffer"] = 0
                                    blog(f"❌ LOSS {pnl_pct:.1f}% | Capital hit: -${remaining:.3f}","error")
                            s["daily_pnl"]          += realized
                            s["total_exposure_pct"]  = max(0,s["total_exposure_pct"]-3.0)
                            # Clear trail
                            bot_state["trail_state"].pop(sym, None)

                            # Update trade memory
                            try:
                                # Determine setup key from symbol
                                is_call = sym.startswith("C-")
                                direction_str = "CALL" if is_call else "PUT"
                                # Get regime from last signal
                                last_sig = s.get("last_signal", {})
                                mem_regime = last_sig.get("regime", "NEUTRAL")
                                setup_key = f"{mem_regime}_{direction_str}"
                                if setup_key not in bot_state["setup_memory"]:
                                    bot_state["setup_memory"][setup_key] = {"wins": 0, "losses": 0}
                                if pnl_pct > 0:
                                    bot_state["setup_memory"][setup_key]["wins"] += 1
                                else:
                                    bot_state["setup_memory"][setup_key]["losses"] += 1
                                mem = bot_state["setup_memory"][setup_key]
                                total = mem["wins"] + mem["losses"]
                                wr = mem["wins"]/total*100 if total else 0
                                blog(f"Setup memory: {setup_key} → {wr:.0f}% WR "
                                     f"({mem['wins']}W/{mem['losses']}L)", "info")
                            except Exception as me:
                                pass
                            # Save state
                            save_state({
                                "last_ip":         _ip_state["current"],
                                "profit_buffer":   s["profit_buffer"],
                                "buffer_high":     s["buffer_high"],
                                "losses_absorbed": s["losses_absorbed"],
                                "trail_state":     bot_state["trail_state"],
                            })
                    except Exception as e:
                        blog(f"[{sym}] Close error: {e}", "error")
    except Exception as e:
        blog(f"Position manager error: {e}", "error")

# ── Execute Trade ─────────────────────────────────────────────────
def execute_trade(analysis):
    asset     = analysis["asset"]
    direction = analysis["direction"]
    price     = analysis["price"]
    conf      = analysis["confidence"]
    aggression= analysis.get("aggression","NORMAL")
    size_mult = analysis.get("size_multiplier", 1.0)

    if direction not in ("BUY_CALL","BUY_PUT","STRADDLE"):
        return False


    ok, reason = risk_ok()
    if not ok:
        blog(f"[{asset}] Risk blocked: {reason}", "warning")
        return False

    try:
        open_pos   = dx_get("/v2/positions/margined").get("result",[])
        open_syms  = [p["product_symbol"] for p in open_pos
                      if abs(float(p.get("size",0))) > 0]

        # Never open same asset twice
        if any(asset.upper() in s for s in open_syms):
            blog(f"[{asset}] Already have open position — skip","info")
            return False

        # Correlation filter — BTC and ETH move together
        # Don't open ETH call if BTC call already open (double risk)
        if len(open_syms) >= 1:
            # Check direction of existing positions
            existing_calls = any("C-" in s for s in open_syms)
            existing_puts  = any("P-" in s for s in open_syms)
            is_call = "CALL" in direction
            is_put  = "PUT"  in direction
            if (is_call and existing_calls) or (is_put and existing_puts):
                if conf < 72:
                    blog(f"[{asset}] Correlation block — same direction already open "
                         f"(need 72%+ got {conf}%) — skip","info")
                    return False
                else:
                    blog(f"[{asset}] High conviction {conf}% — allowing despite correlation","info")

        # Max 3 simultaneous positions
        if len(open_syms) >= 3:
            blog(f"Max 3 positions — skip","info")
            return False
    except: pass

    products = get_products_cached()

    # ── Straddle execution ────────────────────────────────────────
    if direction == "STRADDLE":
        return execute_straddle(asset, price, products)

    # ── Single option execution ───────────────────────────────────
    opt_type = "call_options" if direction=="BUY_CALL" else "put_options"
    options  = [p for p in products
                if p.get("contract_type") == opt_type
                and f"-{asset.upper()}-" in p.get("symbol","")
                and p.get("state") == "live"]

    if not options:
        blog(f"[{asset}] No {opt_type} available","warning")
        return False

    # Filter by expiry — prefer 2-7 days
    def get_hours(p):
        try:
            e = p["symbol"].split("-")[-1]
            d=int(e[0:2]); m=int(e[2:4]); y=int("20"+e[4:6])
            dt = datetime(y,m,d,8,0,0,tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds()/3600
        except: return 999

    valid = [p for p in options if 24 < get_hours(p) < 240]
    if not valid: valid = [p for p in options if get_hours(p) > 24]
    if not valid: valid = options

    def strike_dist(p):
        try: return abs(float(p["symbol"].split("-")[2]) - price)
        except: return 999999

    # Sort by strike distance first
    valid.sort(key=strike_dist)

    # Liquidity check — skip options with no volume
    liquid_options = []
    for p in valid:
        vol_24h = float(p.get("volume", 0) or p.get("turnover_24h", 0) or 0)
        oi = float(p.get("open_interest", 0) or 0)
        if vol_24h > 0 or oi > 0:
            liquid_options.append(p)

    if not liquid_options:
        blog(f"[{asset}] No liquid options — using best available", "warning")
        liquid_options = valid  # fallback to any option

    # Smart strike selection:
    # PUT: pick strike 0.5-2% BELOW current price (OTM put)
    # CALL: pick strike 0.5-2% ABOVE current price (OTM call)
    def otm_score(p):
        try:
            strike = float(p["symbol"].split("-")[2])
            if direction == "BUY_PUT":
                # Prefer strike slightly below price
                diff = price - strike
                if 0 < diff <= price * 0.03:  # 0-3% OTM
                    return (0, diff)  # best range
                elif diff > price * 0.03:
                    return (1, diff)  # too far OTM
                else:
                    return (2, abs(diff))  # ITM put (avoid)
            else:  # BUY_CALL
                diff = strike - price
                if 0 < diff <= price * 0.03:  # 0-3% OTM
                    return (0, diff)
                elif diff > price * 0.03:
                    return (1, diff)
                else:
                    return (2, abs(diff))
        except:
            return (3, 999999)

    liquid_options.sort(key=otm_score)
    product = liquid_options[0]  # Best OTM option with liquidity
    strike  = product["symbol"].split("-")[2] if "-" in product["symbol"] else "?"
    hrs     = get_hours(product)
    vol_24h = float(product.get("volume", 0) or 0)
    oi_val  = float(product.get("open_interest", 0) or 0)
    blog(f"[{asset}] Selected: {product['symbol']} | Vol={vol_24h:.0f} OI={oi_val:.0f}", "info")

    # Risk-based position sizing
    # risk per trade = 2% of balance * confidence factor
    s = bot_state["stats"]
    balance = s.get("current_balance", 70)
    risk_pct = 0.03  # 3% max risk per trade
    conf_factor = conf / 100.0
    risk_amount = balance * risk_pct * conf_factor

    # Estimate option premium (rough: 1-3% of underlying)
    est_premium = price * 0.015  # 1.5% of spot as rough premium estimate
    if est_premium > 0:
        raw_size = risk_amount / est_premium
        size = max(1, min(2, round(raw_size)))
    else:
        size = 1

    # Override: LOW aggression = always 1
    if aggression == "LOW":
        size = 1

    blog(f"[{asset}] Risk sizing: bal={balance:.2f} risk={risk_amount:.2f} "
         f"est_prem={est_premium:.2f} size={size}", "info")

    blog(f"[{asset}] {direction} | Strike ${strike} | "
         f"Price ${price:.2f} | Conf {conf}% | "
         f"Size {size}x | Expiry {hrs:.0f}h", "bot")
    blog(f"[{asset}] Reasons: {' | '.join(analysis['reasons'][:3])}", "info")

    try:
        resp = dx_post("/v2/orders", {
            "product_id":     product["id"],
            "product_symbol": product["symbol"],
            "size":           size,
            "side":           "buy",
            "order_type":     "market_order",
        })
        if resp.get("error"):
            err = resp["error"]
            blog(f"[{asset}] Failed: {err.get('code','err')} — {err.get('context','')}", "error")
            return False
        if not resp.get("result"):
            blog(f"[{asset}] No result in response: {str(resp)[:100]}", "error")
            return False

        blog(f"[{asset}] ✓ {direction} {size}x filled: {product['symbol']}", "success")
        s = bot_state["stats"]
        s["trades_today"]       += 1
        s["total_exposure_pct"] += 3.0 * size
        s["consecutive_losses"]  = 0
        s["last_signal"] = {
            "asset": asset, "direction": direction,
            "option": product["symbol"], "price": price,
            "confidence": conf, "size": size,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        return True
    except Exception as e:
        blog(f"[{asset}] Exception: {e}", "error")
        # Don't count order errors as losses — only real closed positions count
        return False

def execute_straddle(asset, price, products):
    """
    Execute straddle using Delta Exchange MV-BTC/MV-ETH products.
    These are pre-packaged call+put bundles with daily expiry.
    50% lower fees, single order execution.
    """
    blog(f"[{asset}] STRADDLE — using Delta MV products", "bot")

    # Try Delta native straddle products first (MV-BTC, MV-ETH)
    def mv_hours_left(p):
        try:
            e = p["symbol"].split("-")[-1]
            d=int(e[0:2]); m=int(e[2:4]); y=int("20"+e[4:6])
            dt = datetime(y,m,d,8,0,0,tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds()/3600
        except: return 999

    mv_products = [p for p in products
                   if p.get("contract_type") in ("move_options", "straddle", "move")
                   and asset.upper() in p.get("symbol","").upper()
                   and p.get("state") == "live"
                   and mv_hours_left(p) >= 20]  # min 20h left

    # Also try symbol pattern MV-BTC or MOVE
    if not mv_products:
        mv_products = [p for p in products
                       if ("MV-" + asset.upper() in p.get("symbol","")
                           or "MOVE" in p.get("symbol","").upper())
                       and p.get("state") == "live"
                       and mv_hours_left(p) >= 20]

    if mv_products:
        # Pick ATM move product
        def strike_dist(p):
            try:
                parts = p["symbol"].split("-")
                for part in parts:
                    try:
                        val = float(part)
                        if val > 1000:  # likely a strike price
                            return abs(val - price)
                    except: pass
                return 999999
            except: return 999999

        product = min(mv_products, key=strike_dist)
        blog(f"[{asset}] Using MV product: {product['symbol']}", "bot")

        try:
            resp = dx_post("/v2/orders", {
                "product_id":     product["id"],
                "product_symbol": product["symbol"],
                "size":           1,
                "side":           "buy",
                "order_type":     "market_order",
            })
            if not resp.get("error"):
                blog(f"[{asset}] ✓ MV Straddle filled: {product['symbol']}","success")
                s = bot_state["stats"]
                s["trades_today"]       += 1
                s["total_exposure_pct"] += 4.0
                s["consecutive_losses"]  = 0
                return True
            else:
                blog(f"[{asset}] MV order failed: {resp['error']}","error")
        except Exception as e:
            blog(f"[{asset}] MV error: {e}","error")

    # Fallback — buy separate call and put
    blog(f"[{asset}] No MV products — using separate call+put", "warning")

    def get_atm(opt_type):
        opts = [p for p in products
                if p.get("contract_type") == opt_type
                and f"-{asset.upper()}-" in p.get("symbol","")
                and p.get("state") == "live"]
        if not opts: return None
        def get_hours(p):
            try:
                e = p["symbol"].split("-")[-1]
                d=int(e[0:2]);m=int(e[2:4]);y=int("20"+e[4:6])
                return (datetime(y,m,d,8,0,0,tzinfo=timezone.utc) -
                        datetime.now(timezone.utc)).total_seconds()/3600
            except: return 999
        valid = [p for p in opts if 24 < get_hours(p) < 168]
        if not valid: valid = opts
        def sd(p):
            try: return abs(float(p["symbol"].split("-")[2]) - price)
            except: return 999999
        return min(valid, key=sd)

    call = get_atm("call_options")
    put  = get_atm("put_options")
    if not call or not put:
        blog(f"[{asset}] Straddle legs unavailable","warning")
        return False

    success = 0
    for product, name in [(call,"CALL"),(put,"PUT")]:
        try:
            resp = dx_post("/v2/orders", {
                "product_id":     product["id"],
                "product_symbol": product["symbol"],
                "size":           1,
                "side":           "buy",
                "order_type":     "market_order",
            })
            if resp.get("error"):
                blog(f"[{asset}] {name} leg failed","error")
            else:
                blog(f"[{asset}] ✓ {name} filled: {product['symbol']}","success")
                success += 1
        except Exception as e:
            blog(f"[{asset}] {name} error: {e}","error")

    if success == 2:
        s = bot_state["stats"]
        s["trades_today"]       += 1
        s["total_exposure_pct"] += 6.0
        s["consecutive_losses"]  = 0
        blog(f"[{asset}] ✓ Full straddle placed","success")
        return True
    return False

# ── Scalper Strategy ─────────────────────────────────────────────
def run_scalper(analyses):
    """
    Independent scalper — runs alongside swing strategy.
    Quick entries, 8% target, 12% stop.
    Never waits for swing — places trades independently.
    Max 6 scalp trades per day, always 1 lot.
    """
    sc = bot_state["scalper"]
    if not sc["enabled"]: return

    products = get_products_cached()

    # ── Manage existing scalper position ──────────────────────────
    if sc["active_trade"]:
        sym = sc["active_trade"]["symbol"]
        try:
            positions = dx_get("/v2/positions/margined").get("result", [])
            pos = next((p for p in positions
                        if p.get("product_symbol") == sym
                        and abs(float(p.get("size",0))) > 0), None)

            if not pos:
                blog(f"[SCALPER] {sym} gone — resetting", "warning")
                sc["active_trade"] = None
            else:
                entry   = float(pos.get("entry_price", 0))
                mark    = float(pos.get("mark_price", 0) or 0)
                size    = abs(float(pos.get("size", 0)))
                upnl    = float(pos.get("unrealized_pnl", 0))
                side    = "buy" if float(pos.get("size", 0)) > 0 else "sell"
                pnl_pct = ((mark - entry) / entry * 100) if entry else 0

                if pnl_pct > sc["active_trade"].get("peak_pnl", 0):
                    sc["active_trade"]["peak_pnl"] = pnl_pct

                peak = sc["active_trade"].get("peak_pnl", 0)
                blog(f"[SCALPER] {sym} PnL:{pnl_pct:.1f}% Peak:{peak:.1f}% "
                     f"TP:+{sc['target_pct']}% SL:{sc['stop_pct']}%", "bot")

                should_close = False; reason = ""
                if pnl_pct >= sc["target_pct"]:
                    should_close = True; reason = f"✅ TP +{pnl_pct:.1f}%"
                elif pnl_pct <= sc["stop_pct"]:
                    should_close = True; reason = f"❌ SL {pnl_pct:.1f}%"
                elif peak >= sc["target_pct"] * 0.4 and pnl_pct < peak * 0.5:
                    should_close = True
                    reason = f"🔒 Trail: peak +{peak:.1f}% now +{pnl_pct:.1f}%"

                if should_close:
                    blog(f"[SCALPER] Closing: {reason}", "bot")
                    product = next((p for p in products
                                    if p.get("symbol") == sym), None)
                    if product:
                        close_side = "sell" if side == "buy" else "buy"
                        is_mv = "MV-" in sym
                        order_body = {
                            "product_id":     product["id"],
                            "product_symbol": sym,
                            "size":           int(size),
                            "side":           close_side,
                            "order_type":     "market_order",
                        }
                        if not is_mv:
                            order_body["reduce_only"] = "true"
                        resp = dx_post("/v2/orders", order_body)
                        if not resp.get("error"):
                            sc["trades_today"] += 1
                            sc["profit"]       += upnl
                            if upnl > 0: sc["wins"]   += 1
                            else:        sc["losses"] += 1
                            sc["active_trade"] = None
                            wr = (sc["wins"]/(sc["wins"]+sc["losses"])*100
                                  if sc["wins"]+sc["losses"] > 0 else 0)
                            blog(f"[SCALPER] ✓ Closed | P&L:${upnl:.3f} | "
                                 f"WR:{wr:.0f}% ({sc['wins']}W/{sc['losses']}L)", "success")
                return  # done managing existing position
        except Exception as e:
            blog(f"[SCALPER] Manage error: {e}", "error")
            return

    # ── Look for new scalper entry ────────────────────────────────
    if sc["trades_today"] >= sc["max_trades"]:
        blog(f"[SCALPER] Max {sc['max_trades']} trades reached today", "info")
        return

    best_scalp = None; best_score = 0

    for asset, a in analyses.items():
        direction = a.get("direction", "NO TRADE")
        if direction in ("NO TRADE", "STRADDLE"): continue

        conf = a.get("confidence", 0)
        ind  = a.get("indicators", {})
        c15m = a.get("candles_15m", [])
        rsi  = ind.get("rsi_1h", 50)

        score = 0

        # RSI extreme = bounce opportunity
        if direction == "BUY_CALL" and rsi < 25: score += 35
        elif direction == "BUY_CALL" and rsi < 35: score += 20
        elif direction == "BUY_PUT" and rsi > 75: score += 35
        elif direction == "BUY_PUT" and rsi > 65: score += 20

        # 15m candle momentum
        if len(c15m) >= 3:
            last3 = c15m[-3:]
            if direction == "BUY_CALL":
                score += sum(8 for c in last3 if c["close"] > c["open"])
            else:
                score += sum(8 for c in last3 if c["close"] < c["open"])

        # Volume
        if ind.get("volume") == "RISING": score += 15
        if ind.get("whale"): score += 20

        # Base confidence
        score += conf * 0.25

        if score > best_score and score >= sc["min_confidence"]:
            best_score = score
            best_scalp = a

    if not best_scalp:
        blog("[SCALPER] No entry signal", "info")
        return

    asset     = best_scalp["asset"]
    direction = best_scalp["direction"]
    price     = best_scalp["price"]

    blog(f"[SCALPER] 🎯 Entry: {asset} {direction} Score:{best_score:.0f}", "bot")

    opt_type = "call_options" if "CALL" in direction else "put_options"
    options  = [p for p in products
                if p.get("contract_type") == opt_type
                and f"-{asset.upper()}-" in p.get("symbol","")
                and p.get("state") == "live"]

    def get_hrs(p):
        try:
            e = p["symbol"].split("-")[-1]
            d=int(e[0:2]); m=int(e[2:4]); y=int("20"+e[4:6])
            dt = datetime(y,m,d,8,0,0,tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds()/3600
        except: return 999

    valid = [p for p in options if 12 < get_hrs(p) < 120]
    if not valid: valid = options
    if not valid:
        blog(f"[SCALPER] No options available for {asset}", "warning")
        return

    def strike_dist(p):
        try: return abs(float(p["symbol"].split("-")[2]) - price)
        except: return 999999

    valid.sort(key=strike_dist)
    liquid = [p for p in valid
              if float(p.get("volume",0) or 0) > 0
              or float(p.get("open_interest",0) or 0) > 0]
    product = liquid[0] if liquid else valid[0]

    try:
        resp = dx_post("/v2/orders", {
            "product_id":     product["id"],
            "product_symbol": product["symbol"],
            "size":           1,
            "side":           "buy",
            "order_type":     "market_order",
        })
        if resp.get("error"):
            blog(f"[SCALPER] Order failed: {resp['error']}", "error")
            return

        blog(f"[SCALPER] ✓ {product['symbol']} | "
             f"TP:+{sc['target_pct']}% SL:{sc['stop_pct']}%", "success")
        sc["active_trade"] = {
            "symbol":    product["symbol"],
            "asset":     asset,
            "direction": direction,
            "score":     best_score,
            "peak_pnl":  0.0,
            "ts":        time.time(),
        }
    except Exception as e:
        blog(f"[SCALPER] Entry error: {e}", "error")

# ── Main Bot Cycle ────────────────────────────────────────────────
def run_cycle():
    lock_time = bot_state.get("cycle_lock_time", 0)
    if bot_state["cycle_lock"]:
        if time.time() - lock_time > 60:
            blog("Cycle lock auto-released", "warning")
            bot_state["cycle_lock"] = False
        else:
            return
    bot_state["cycle_lock"] = True
    bot_state["cycle_lock_time"] = time.time()

    try:
        s = bot_state["stats"]
        blog(f"━━ PRO CYCLE | Trades:{s['trades_today']}/12 | "
             f"Bal:{s['current_balance']:.2f} | "
             f"Buffer:${s['profit_buffer']:.3f} ━━", "bot")

        # ── Auto daily reset at midnight UTC ─────────────────────
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if bot_state.get("last_reset_date") != today:
            s = bot_state["stats"]
            s["trades_today"] = 0
            s["daily_pnl"] = 0.0
            s["total_exposure_pct"] = 0.0
            s["daily_loss_limit_hit"] = False
            s["consecutive_losses"] = 0
            bot_state["scalper"]["trades_today"] = 0
            bot_state["last_reset_date"] = today
            blog(f"🔄 Daily reset — new day {today}", "success")

        # Check Delta Exchange status first
        delta_ok, delta_status = check_delta_status()
        if not delta_ok:
            blog(f"⚠️ Delta Exchange: {delta_status} — pausing trading", "error")
            return

        check_ip()
        balance = refresh_balance()
        if balance <= 0:
            blog("No balance","error"); return

        ok, reason = risk_ok()
        if not ok:
            blog(f"Risk: {reason}","warning"); return

        # ── DUAL MODE: Analyze all assets ───────────────────────
        analyses = {}
        for asset in ["BTC"]:
            try:
                a = full_analysis(asset)
                if not a: continue
                ind = a["indicators"]
                blog(f"[{asset}] RSI:{ind['rsi_1h']}/{ind['rsi_4h']} "
                     f"MACD:{ind['macd_1h']} BB:{ind['bb_1h']} "
                     f"Regime:{a['regime']} "
                     f"→ {a['direction']} ({a['confidence']}%)", "info")
                if a["direction"] != "NO TRADE":
                    a = execution_engine(a)
                analyses[asset] = a
            except Exception as e:
                blog(f"[{asset}] Error: {traceback.format_exc()}","error")

        # ── MODE 1: SWING STRATEGY ───────────────────────────────
        # High confidence, holds hours/days, signal confirmation
        manage_positions()

        best = None; best_conf = 0
        for asset, a in analyses.items():
            if a["direction"] == "NO TRADE": continue
            if a["confidence"] > best_conf:
                best_conf = a["confidence"]
                best = a

        if best:
            blog(f"[SWING] Best: {best['asset']} {best['direction']} "
                 f"{best['confidence']}%", "bot")
            pending = bot_state.get("pending_signal", {})
            same_asset  = pending.get("asset") == best["asset"]
            same_dir    = pending.get("direction") == best["direction"]
            pending_age = time.time() - pending.get("ts", 0)

            if best["confidence"] >= 75:
                blog(f"[SWING] High conviction — trading immediately", "bot")
                bot_state["pending_signal"] = {}
                execute_trade(best)
            elif same_asset and same_dir and pending_age < 900:
                blog(f"[SWING] Signal confirmed — executing", "bot")
                bot_state["pending_signal"] = {}
                execute_trade(best)
            elif best["confidence"] >= 55:
                bot_state["pending_signal"] = {
                    "asset": best["asset"], "direction": best["direction"],
                    "confidence": best["confidence"], "ts": time.time(),
                }
                blog(f"[SWING] Pending confirmation: {best['asset']} "
                     f"{best['direction']} {best['confidence']}%", "info")
            else:
                blog(f"[SWING] Confidence too low — skipping", "info")
                bot_state["pending_signal"] = {}
        else:
            blog("[SWING] No valid setup this cycle", "info")

        # ── MODE 2: SCALPER STRATEGY ─────────────────────────────
        # Independent — quick trades, 8% target, doesn't wait for swing
        run_scalper(analyses)

        # ── FORCE TRADE: Last resort if nothing triggered ─────────
        # If swing found no setup and scalper has no active trade,
        # force best available signal if confidence >= 50%
        sc = bot_state["scalper"]
        if not sc["active_trade"] and a and a.get("direction") == "NO TRADE":
            # Only force if no open positions at all
            try:
                open_pos = dx_get("/v2/positions/margined").get("result",[])
                has_open = any(abs(float(p.get("size",0))) > 0 for p in open_pos)
            except:
                has_open = True
            if not has_open and a.get("confidence",0) >= 52:
                alt = full_analysis("BTC")
                if alt and alt.get("confidence",0) >= 52 and alt.get("direction") != "NO TRADE":
                    alt = execution_engine(alt)
                    blog(f"⚡ Force trade: BTC {alt['direction']} {alt['confidence']}%", "warning")
                    execute_trade(alt)

        total = s["wins"] + s["losses"]
        if total > 0: s["win_rate"] = round(s["wins"]/total*100, 1)

    except Exception as e:
        blog(f"Cycle error: {traceback.format_exc()}","error")
    finally:
        bot_state["cycle_lock"] = False

def bot_loop():
    while not stop_event.is_set():
        run_cycle()
        stop_event.wait(bot_state["interval"])

# ══════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/")
def index(): return send_from_directory("static","index.html")

@app.route("/ping")
def ping(): return "ok", 200

@app.route("/api/connect", methods=["POST"])
def api_connect():
    global API_KEY, API_SECRET, BASE_URL
    d          = request.json or {}
    API_KEY    = (d.get("key") or "").strip()
    API_SECRET = (d.get("secret") or "").strip()
    region     = d.get("region","india")
    BASE_URL   = ("https://api.india.delta.exchange"
                  if region=="india" else "https://api.delta.exchange")
    if not API_KEY or not API_SECRET:
        return jsonify({"ok":False,"error":"Key and secret required"}), 400
    try:
        result = dx_get("/v2/wallet/balances")
        if result.get("error"):
            return jsonify({"ok":False,"error":result["error"].get("code","Auth failed")}),401
        s = bot_state["stats"]
        for w in (result.get("result") or []):
            if w.get("asset_symbol") in ("USDT","INR","USD"):
                bal = float(w.get("available_balance") or 0)
                if bal < 1: continue
                if w.get("asset_symbol") == "INR": bal = round(bal/85.0,4)
                if bal > s["starting_balance"]:
                    s["starting_balance"] = s["current_balance"] = s["peak_balance"] = bal
                    s["profit_floor"] = round(bal*0.90,4)
        check_ip()
        blog(f"Connected {region} | Balance:{s['current_balance']:.2f} | IP:{_ip_state['current']}","success")
        return jsonify({"ok":True,"wallet":result.get("result",[]),"ip":_ip_state["current"]})
    except requests.exceptions.ConnectionError:
        return jsonify({"ok":False,"error":"Cannot reach Delta Exchange"}), 502
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500

@app.route("/api/wallet")
def api_wallet():
    try: return jsonify(dx_get("/v2/wallet/balances"))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/positions")
def api_positions():
    try: return jsonify(dx_get("/v2/positions/margined"))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/orders")
def api_orders():
    try: return jsonify(dx_get("/v2/orders?state=open"))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/orders", methods=["POST"])
def api_place_order():
    try: return jsonify(dx_post("/v2/orders", request.json))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/orders/<int:oid>", methods=["DELETE"])
def api_cancel_order(oid):
    try:
        pid = int(request.args.get("product_id",0))
        return jsonify(dx_delete("/v2/orders",{"id":oid,"product_id":pid}))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/tickers")
def api_tickers():
    try: return jsonify(pub_get("/v2/tickers"))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/products")
def api_products():
    try: return jsonify(pub_get("/v2/products?states=live&page_size=500"))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/analysis")
def api_analysis():
    try:
        results = []
        for asset in ["BTC"]:
            a = full_analysis(asset)
            if a:
                a = execution_engine(a)
                results.append(a)
        return jsonify({"ok":True,"result":results,
                        "timestamp":datetime.now(timezone.utc).isoformat()})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    global bot_thread
    d = request.json or {}
    if bot_state["running"]:
        return jsonify({"ok":False,"error":"Already running"})
    bot_state["interval"]     = max(180, int(d.get("interval",300)))
    bot_state["running"]      = True
    bot_state["startup_time"] = time.time()
    stop_event.clear()
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    blog(f"PRO Bot started | {bot_state['interval']}s | max 12 trades/day","success")
    return jsonify({"ok":True})

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    bot_state["running"] = False
    stop_event.set()
    blog("Bot stopped.","warning")
    return jsonify({"ok":True})

@app.route("/api/bot/run_now", methods=["POST"])
def api_bot_run_now():
    threading.Thread(target=run_cycle, daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/bot/status")
def api_bot_status():
    return jsonify({
        "running":  bot_state["running"],
        "strategy": bot_state["strategy"],
        "interval": bot_state["interval"],
        "stats":    bot_state["stats"],
        "log":      bot_state["log"][-200:],
        "ip":       _ip_state["current"],
    })

@app.route("/api/bot/reset", methods=["POST"])
def api_bot_reset():
    s = bot_state["stats"]
    s["trades_today"] = s["daily_pnl"] = s["total_exposure_pct"] = 0
    s["daily_loss_limit_hit"] = False
    s["consecutive_losses"]   = 0
    blog("Reset.","info")
    return jsonify({"ok":True})

@app.route("/api/straddles")
def api_straddles():
    """Debug route to see available straddle/move products"""
    try:
        products = get_products_cached()
        mv = [p["symbol"] for p in products
              if any(x in p.get("symbol","").upper()
                     for x in ["MV-","MOVE","STRADDLE"])
              and p.get("state") == "live"]
        types = list(set(p.get("contract_type","") for p in products))
        btc_types = list(set(p.get("contract_type","")
                             for p in products if "BTC" in p.get("symbol","")))
        return jsonify({"mv_products": mv[:20],
                        "all_types": types,
                        "btc_types": btc_types})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/ip")
def api_ip():
    check_ip()
    return jsonify({"ip": _ip_state["current"]})

@app.route("/api/health")
def api_health():
    return jsonify({
        "ok":   True,
        "time": datetime.now(timezone.utc).isoformat(),
        "base": BASE_URL,
        "ip":   _ip_state.get("current",""),
    })

if __name__ == "__main__":
    os.makedirs("static", exist_ok=True)
    port = int(os.environ.get("PORT", 8080))
    print(f"\n ΔLPHA PRO | http://0.0.0.0:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
