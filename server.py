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
    "last_trade_time": 0,    # cooldown between entries
    "max_daily_trades": _saved.get("max_daily_trades", 12),
    "trade_cooldown":   _saved.get("trade_cooldown", 180),
    "monitor_manual": True,  # monitor manually placed trades too
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
    "setup_memory": _saved.get("setup_memory", {}),
    # Extended memory: regime_direction_striketype_vol_expiry → {wins, losses, avg_pnl}
    "deep_memory":  _saved.get("deep_memory", {}),
    # Signal source accuracy tracking
    "signal_weights": _saved.get("signal_weights", {
        "polymarket":   {"weight": 1.0, "correct": 0, "total": 0},
        "order_flow":   {"weight": 1.0, "correct": 0, "total": 0},
        "deribit_iv":   {"weight": 1.0, "correct": 0, "total": 0},
        "rsi":          {"weight": 1.0, "correct": 0, "total": 0},
        "macd":         {"weight": 1.0, "correct": 0, "total": 0},
        "news":         {"weight": 1.0, "correct": 0, "total": 0},
        "volume":       {"weight": 1.0, "correct": 0, "total": 0},
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
            "setup_memory":     bot_state["setup_memory"],
            "max_daily_trades": bot_state.get("max_daily_trades", 12),
            "trade_cooldown":   bot_state.get("trade_cooldown", 180),
            "deep_memory":      bot_state.get("deep_memory", {}),
            "signal_weights":   bot_state.get("signal_weights", {}),
        })
    except: pass

# ── Price + Candles ───────────────────────────────────────────────
BINANCE_SYM = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"
}

# Price cache — avoid hammering Binance every cycle
_price_cache = {}
_price_cache_ttl = 5   # seconds — fresh price for accurate strike selection

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


def get_polymarket_sentiment():
    """
    Polymarket BTC prediction markets — real money sentiment.
    "Will BTC be above $X?" YES price = crowd bull confidence.
    Weight: 15 points toward call/put decision.
    """
    try:
        r = requests.get(
            "https://clob.polymarket.com/markets?active=true&closed=false",
            timeout=8)
        if r.status_code != 200:
            return {"sentiment": "NEUTRAL", "bull_pct": 50, "markets": 0}

        markets = r.json().get("data", [])
        btc_markets = [m for m in markets
                       if any(x in m.get("question", "").lower()
                              for x in ["bitcoin", "btc"])
                       and any(x in m.get("question", "").lower()
                               for x in ["above", "below", "reach", "price",
                                         "higher", "lower", "exceed"])]

        if not btc_markets:
            return {"sentiment": "NEUTRAL", "bull_pct": 50, "markets": 0}

        bull_scores = []
        bear_scores = []

        for m in btc_markets[:8]:
            q = m.get("question", "").lower()
            tokens = m.get("tokens", [])
            for t in tokens:
                outcome = t.get("outcome", "").lower()
                price   = float(t.get("price", 0.5))
                # YES on "above" = bull
                if outcome == "yes" and any(x in q for x in ["above","exceed","higher","reach"]):
                    bull_scores.append(price)
                # YES on "below" = bear
                elif outcome == "yes" and any(x in q for x in ["below","lower","under","drop"]):
                    bear_scores.append(price)
                # NO on "above" = bear
                elif outcome == "no" and any(x in q for x in ["above","exceed"]):
                    bear_scores.append(price)
                # NO on "below" = bull
                elif outcome == "no" and any(x in q for x in ["below","lower"]):
                    bull_scores.append(price)

        total_scores = len(bull_scores) + len(bear_scores)
        if total_scores == 0:
            return {"sentiment": "NEUTRAL", "bull_pct": 50, "markets": len(btc_markets)}

        avg_bull = sum(bull_scores) / len(bull_scores) if bull_scores else 0
        avg_bear = sum(bear_scores) / len(bear_scores) if bear_scores else 0
        denom = avg_bull + avg_bear if (avg_bull + avg_bear) > 0 else 1
        bull_pct = round((avg_bull / denom) * 100, 1)

        sentiment = ("BULLISH" if bull_pct >= 62
                     else "BEARISH" if bull_pct <= 38
                     else "NEUTRAL")

        return {
            "sentiment": sentiment,
            "bull_pct":  bull_pct,
            "markets":   len(btc_markets),
        }
    except:
        return {"sentiment": "NEUTRAL", "bull_pct": 50, "markets": 0}


def get_deribit_iv():
    """
    Deribit BTC Volatility Index — real options market IV.
    Much more accurate than BB_width for volatility regime.
    IV < 45: Low vol — good for directional options (sniper mode)
    IV 45-65: Normal vol
    IV > 65: High fear — options expensive, tighter entries
    IV > 80: Extreme — avoid buying options (too expensive)
    """
    try:
        r = requests.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index"
            "?currency=BTC&resolution=60",
            timeout=6)
        data = r.json()
        if data.get("result") and data["result"].get("data"):
            iv = float(data["result"]["data"][-1][4])  # close value
            if iv < 45:   iv_regime = "LOW"
            elif iv > 65: iv_regime = "HIGH"
            else:         iv_regime = "NORMAL"
            return {"iv": round(iv, 1), "regime": iv_regime}
    except: pass
    # Fallback — try simpler endpoint
    try:
        r = requests.get(
            "https://www.deribit.com/api/v2/public/get_index_price"
            "?index_name=btc_usd",
            timeout=5)
        # If Deribit unreachable, return neutral
    except: pass
    return {"iv": 55, "regime": "NORMAL"}  # safe default

def get_order_flow():
    """
    Binance futures taker buy/sell ratio.
    > 1.2: Buyers dominating — institutional buying → BUY_CALL signal
    < 0.8: Sellers dominating — institutional selling → BUY_PUT signal
    1.2 ≥ x ≥ 0.8: Neutral flow
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/takerlongshortRatio"
            "?symbol=BTCUSDT&period=5m&limit=3",
            timeout=6)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            # Average last 3 periods for stability
            ratios = [float(d.get("buySellRatio", 1.0)) for d in data]
            avg_ratio = sum(ratios) / len(ratios)
            if avg_ratio > 1.2:   flow = "BUY"
            elif avg_ratio < 0.8: flow = "SELL"
            else:                  flow = "NEUTRAL"
            return {"flow": flow, "ratio": round(avg_ratio, 3)}
    except: pass
    return {"flow": "NEUTRAL", "ratio": 1.0}

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
    dv_iv   = get_deribit_iv()
    flow    = get_order_flow()
    news    = get_crypto_sentiment()
    poly    = get_polymarket_sentiment()
    blog(f"[{asset}] News:{news['sentiment']} ({news['bull_pct']}% bull) | "
         f"Headline: {news['headlines'][0] if news['headlines'] else 'none'}", "info")
    if poly["markets"] > 0:
        blog(f"[{asset}] Polymarket:{poly['sentiment']} ({poly['bull_pct']}% bull) | "
             f"{poly['markets']} BTC markets", "info")

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

    blog(f"[{asset}] VolRegime:{vol_regime} BB_width:{bb_width:.2f}% | "
         f"DeribitIV:{dv_iv['iv']} ({dv_iv['regime']}) | "
         f"OrderFlow:{flow['flow']} ({flow['ratio']}x)", "info")
    # Use Deribit IV to refine vol regime if available
    if dv_iv["regime"] == "HIGH" and vol_regime != "HIGH":
        vol_regime = "HIGH"  # Deribit says high vol — trust it
    elif dv_iv["regime"] == "LOW" and vol_regime == "HIGH":
        vol_regime = "MID"   # Deribit disagrees — use middle ground
    bot_state["last_vol_regime"] = vol_regime  # save for stop loss

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

    # ── Polymarket (real money prediction markets) ────────────────
    # Strong signal — people bet real $ on these outcomes
    if poly["markets"] > 0:
        if poly["sentiment"] == "BULLISH":
            pts = 20 if poly["bull_pct"] >= 70 else 12
            bull += pts
            reasons.append(f"Polymarket bullish ({poly['bull_pct']}% bull money)")
        elif poly["sentiment"] == "BEARISH":
            pts = 20 if poly["bull_pct"] <= 30 else 12
            bear += pts
            reasons.append(f"Polymarket bearish ({100-poly['bull_pct']}% bear money)")

    # ── Funding — ignored in strong bull regime ───────────────────
    if regime not in ("STRONG_BULL", "BULL"):
        if funding > 0.05:
            bear += 15; reasons.append(f"High funding {funding:.3f}%")
        elif funding < -0.02:
            bull += 15; reasons.append("Negative funding — squeeze possible")

    # ── OI ────────────────────────────────────────────────────────
    if oi == "RISING" and chg_1h > 0:   bull += 10
    elif oi == "RISING" and chg_1h < 0: bear += 10

    # ── Order Flow (institutional taker activity) ─────────────────
    if flow["flow"] == "BUY":
        bull += 15; reasons.append(f"Order flow: buyers {flow['ratio']}x dominating")
    elif flow["flow"] == "SELL":
        bear += 15; reasons.append(f"Order flow: sellers {flow['ratio']}x dominating")

    # ── Deribit IV adjustment ─────────────────────────────────────
    if dv_iv["regime"] == "HIGH":
        # High IV = options expensive = reduce confidence on buys
        conf_penalty = 8 if dv_iv["iv"] > 80 else 4
        reasons.append(f"High IV {dv_iv['iv']} — options expensive, reduced conf")
    elif dv_iv["regime"] == "LOW":
        # Low IV = vol compression = breakout likely, boost confidence
        bull += 5; bear += 5  # boost both — breakout coming, direction TBD
        reasons.append(f"Low IV {dv_iv['iv']} — vol squeeze, breakout incoming")

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

    # ── Direction with minimum confidence ───────────────────────
    if bull > bear and bull_pct >= 55:
        direction = "BUY_CALL"; conf = bull_pct
    elif bear > bull and bear_pct >= 55:
        direction = "BUY_PUT";  conf = bear_pct
    else:
        direction = "NO TRADE"; conf = max(bull_pct, bear_pct)

    # Apply IV penalty — high IV = expensive options = reduce conf
    if dv_iv["regime"] == "HIGH" and direction != "NO TRADE":
        iv_penalty = 8 if dv_iv["iv"] > 80 else 4
        conf = max(0, conf - iv_penalty)

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
        "STRONG_BULL": {"PUT": 65 + vol_factor, "CALL": 0},
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
    # User-configurable daily cap (from dashboard)
    user_cap = bot_state.get("max_daily_trades", 12)
    # Auto-scale within user cap based on win rate
    total_trades = s["wins"] + s["losses"]
    if user_cap == 0:
        daily_cap = 999  # 0 = unlimited (user chose no cap)
    elif total_trades >= 5:
        win_rate = s["wins"] / total_trades * 100
        if win_rate > 50:
            daily_cap = user_cap  # winning — use full user cap
        elif win_rate >= 40:
            daily_cap = max(6, int(user_cap * 0.75))  # neutral — 75% of cap
        else:
            daily_cap = max(4, int(user_cap * 0.5))   # losing — 50% of cap
    else:
        daily_cap = user_cap  # default to user cap before enough data
    if s["trades_today"] >= daily_cap:
        return False, f"Max {daily_cap} trades/day (cap:{user_cap} WR-scaled)"
    if s["consecutive_losses"] >= 4: return False, "4 consecutive losses — pause"

    # Daily P&L circuit breakers — use daily_pnl tracked from closed trades
    cur_bal = s.get("current_balance", 0)
    daily_pnl = s.get("daily_pnl", 0.0)
    if cur_bal > 0 and daily_pnl != 0:
        daily_pct = (daily_pnl / cur_bal) * 100
        target = bot_state.get("daily_target", 10)
        if daily_pct >= target:
            return False, f"🎯 Daily target +{target}% hit ({daily_pct:.1f}%) — locking profits"
        if daily_pct <= -8:
            s["daily_loss_limit_hit"] = True
            return False, f"🛑 Daily loss -8% hit ({daily_pct:.1f}%) — stopping"

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

def check_signal_reversal(open_pos, current_direction, current_conf):
    """
    If we have an open CALL and signal is now strong PUT (or vice versa),
    close the losing position to stop the bleeding.
    Only acts when: signal is opposite direction AND conf >= 65%
    """
    if current_direction == "NO TRADE" or current_conf < 65:
        return
    
    products = get_products_cached()
    now = datetime.now(timezone.utc)
    
    for pos in open_pos:
        sym  = pos.get("product_symbol", "")
        size = abs(float(pos.get("size", 0)))
        if size == 0: continue
        
        entry = float(pos.get("entry_price", 0))
        mark  = float(pos.get("mark_price", 0) or 0)
        if entry <= 0: continue
        
        pnl_pct = ((mark - entry) / entry * 100)
        is_call = sym.startswith("C-")
        is_put  = sym.startswith("P-")
        
        # Check for signal reversal
        reversal = (
            (is_call and current_direction == "BUY_PUT" and pnl_pct < -5) or
            (is_put  and current_direction == "BUY_CALL" and pnl_pct < -5)
        )
        
        if reversal:
            blog(f"[{sym}] 🔄 Signal reversal: position {'CALL' if is_call else 'PUT'} "
                 f"but signal is {current_direction} at {current_conf}% | "
                 f"PnL:{pnl_pct:.1f}% — closing to stop loss", "warning")
            
            product = next((p for p in products if p.get("symbol") == sym), None)
            if product:
                try:
                    side = "buy" if float(pos.get("size", 0)) < 0 else "sell"
                    resp = dx_post("/v2/orders", {
                        "product_id":     product["id"],
                        "product_symbol": sym,
                        "size":           int(size),
                        "side":           side,
                        "order_type":     "market_order",
                        "reduce_only":    "true",
                    })
                    if not resp.get("error"):
                        realized = entry * size * (pnl_pct / 100)
                        s = bot_state["stats"]
                        s["losses"] += 1
                        s["consecutive_losses"] += 1
                        loss_amt = abs(realized)
                        if s["profit_buffer"] >= loss_amt:
                            s["profit_buffer"] -= loss_amt
                            s["losses_absorbed"] += loss_amt
                            blog(f"[{sym}] ✓ Reversal exit {pnl_pct:.1f}% | "
                                 f"Buffer: ${s['profit_buffer']:.3f}", "warning")
                        else:
                            remaining = loss_amt - s["profit_buffer"]
                            s["profit_buffer"] = 0
                            blog(f"[{sym}] ✓ Reversal exit {pnl_pct:.1f}% | "
                                 f"Capital hit: -${remaining:.3f}", "error")
                        s["daily_pnl"]         += realized
                        s["total_exposure_pct"] = max(0, s["total_exposure_pct"] - 3.0)
                        bot_state["trail_state"].pop(sym, None)
                        # Clean pyramid state
                        bot_state.get("pyramid_state", {}).pop(sym, None)
                except Exception as e:
                    blog(f"[{sym}] Reversal exit error: {e}", "error")


def learn_from_trade(sym, pnl_pct, entry_price, mark_price,
                     regime, vol_regime, conf, hours_left,
                     strike_type, direction, signals_at_entry):
    """
    Called every time a position closes.
    Updates 3 learning systems:
      1. deep_memory  — tracks win rate per exact setup combo
      2. signal_weights — tracks which signals were right
      3. ITM target adjustments — learns best ITM per regime/vol
    """
    won = pnl_pct > 0
    s   = bot_state

    # ── 1. Deep memory — 4-dimensional key ───────────────────────
    expiry_bucket = ("short" if hours_left < 48
                     else "medium" if hours_left < 120
                     else "long")
    deep_key = f"{regime}_{direction}_{strike_type}_{vol_regime}_{expiry_bucket}"
    dm = s.get("deep_memory", {})
    if deep_key not in dm:
        dm[deep_key] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "count": 0}
    dm[deep_key]["wins"   if won else "losses"] += 1
    dm[deep_key]["total_pnl"] += pnl_pct
    dm[deep_key]["count"]     += 1
    s["deep_memory"] = dm

    # Win rate and avg PnL for this key
    d    = dm[deep_key]
    tot  = d["wins"] + d["losses"]
    wr   = d["wins"] / tot * 100 if tot else 0
    avg  = d["total_pnl"] / d["count"] if d["count"] else 0
    blog(f"[LEARN] DeepMemory: {deep_key} → "
         f"{wr:.0f}% WR ({d['wins']}W/{d['losses']}L) avg:{avg:.1f}%", "info")

    # ── 2. Signal source accuracy ─────────────────────────────────
    sw = s.get("signal_weights", {})
    for source, was_bullish in (signals_at_entry or {}).items():
        if source not in sw:
            sw[source] = {"weight": 1.0, "correct": 0, "total": 0}
        sw[source]["total"] += 1
        # Signal was correct if: bullish+won OR bearish+lost (for the direction traded)
        is_call = "CALL" in direction
        signal_correct = (
            (was_bullish and is_call and won) or
            (was_bullish and is_call and not won) is False or
            (not was_bullish and not is_call and won) or
            (not was_bullish and not is_call and not won) is False
        )
        # Simplified: signal correct = it agreed with winning direction
        agreed_with_trade = (was_bullish and is_call) or (not was_bullish and not is_call)
        if (agreed_with_trade and won) or (not agreed_with_trade and not won):
            sw[source]["correct"] += 1
            # Boost signal weight slightly
            sw[source]["weight"] = min(1.5, sw[source]["weight"] * 1.05)
        else:
            # Signal was wrong — reduce weight slightly
            sw[source]["weight"] = max(0.5, sw[source]["weight"] * 0.95)

        acc = sw[source]["correct"] / sw[source]["total"] * 100 if sw[source]["total"] else 50
        if sw[source]["total"] >= 5:  # only log after enough data
            blog(f"[LEARN] Signal '{source}': {acc:.0f}% accuracy "
                 f"weight:{sw[source]['weight']:.2f}", "info")
    s["signal_weights"] = sw

    # ── 3. ITM target self-adjustment ────────────────────────────
    # If this strike_type has bad win rate → note for future adjustment
    # (actual parameter adjustment happens in get_optimal_itm_target via deep_memory)
    if tot >= 5 and wr < 35 and strike_type == "OTM":
        blog(f"[LEARN] ⚠️ OTM calls losing in {regime}/{vol_regime} — "
             f"will prefer ITM next time", "info")
    elif tot >= 5 and wr > 65:
        blog(f"[LEARN] ✅ {strike_type} working well in {regime}/{vol_regime} "
             f"({wr:.0f}% WR) — strategy confirmed", "info")

    # Save immediately
    save_state({
        "last_ip":         _ip_state.get("current", ""),
        "profit_buffer":   s["stats"]["profit_buffer"],
        "buffer_high":     s["stats"]["buffer_high"],
        "losses_absorbed": s["stats"]["losses_absorbed"],
        "starting_balance": s["stats"]["starting_balance"],
        "peak_balance":    s["stats"]["peak_balance"],
        "profit_floor":    s["stats"]["profit_floor"],
        "wins":            s["stats"]["wins"],
        "losses":          s["stats"]["losses"],
        "win_rate":        s["stats"]["win_rate"],
        "last_signal":     s["stats"]["last_signal"],
        "trail_state":     bot_state["trail_state"],
        "setup_memory":    bot_state["setup_memory"],
        "deep_memory":     bot_state.get("deep_memory", {}),
        "signal_weights":  bot_state.get("signal_weights", {}),
        "max_daily_trades": bot_state.get("max_daily_trades", 12),
        "trade_cooldown":  bot_state.get("trade_cooldown", 180),
    })

def manage_positions():
    """Auto-manage all open positions including manually placed ones."""
    try:
        positions = dx_get("/v2/positions/margined").get("result",[])
        open_pos  = [p for p in positions if abs(float(p.get("size",0))) > 0]
        if not open_pos: return

        products = get_products_cached()
        now      = datetime.now(timezone.utc)

        # Detect manually placed trades (not in last_signal)
        last_opt = bot_state["stats"].get("last_signal", {}).get("option", "")
        for pos in open_pos:
            sym = pos.get("product_symbol","")
            if sym != last_opt and sym not in bot_state.get("trail_state", {}):
                if bot_state.get("monitor_manual", True):
                    blog(f"[MANUAL] Detected external position: {sym} — applying trail stops", "warning")
                    # Initialize trail state so it gets monitored like auto trades

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

            # Dynamic stop loss — tightens based on vol regime AND consecutive losses
            consec = s.get("consecutive_losses", 0)
            # Vol regime lookup from trail_state context
            # Use BB_width via position symbol to determine vol (approximation)
            # PRIMARY: vol regime based stop
            try:
                # Get current vol regime from cached analysis if available
                cached_vol = bot_state.get("last_vol_regime", "MID")
            except:
                cached_vol = "MID"
            
            if cached_vol == "HIGH":
                base_stop = -12   # HIGH vol — cut quick
            elif cached_vol == "LOW":
                base_stop = -15   # LOW vol BB squeeze = breakout risk, NOT safe!
            else:
                base_stop = -15   # MID vol — normal
            
            # Tighten further on consecutive losses
            if consec >= 3:
                stop_loss = max(base_stop, -10)
            elif consec >= 2:
                stop_loss = max(base_stop, -12)
            else:
                stop_loss = base_stop

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
                            # Use actual upnl for buffer (real cash), pnl_pct for win/loss decision
                            realized = upnl  # actual cash P&L from exchange
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

                            # ── Deep learning from this trade ──────────
                            try:
                                is_call_sym  = sym.startswith("C-")
                                stype        = bot_state.get("_last_strike_type", "OTM")
                                vr           = bot_state.get("last_vol_regime", "MID")
                                last_s       = bot_state["stats"].get("last_signal", {})
                                sig_at_entry = last_s.get("signals_snapshot", {})
                                hrs_at_entry = last_s.get("hours_to_expiry", 72)
                                regime_used  = last_s.get("regime", "NEUTRAL")
                                conf_used    = last_s.get("confidence", 0)  # from entry signal
                                dir_used     = "CALL" if is_call_sym else "PUT"
                                learn_from_trade(
                                    sym, pnl_pct, entry, mark,
                                    regime_used, vr, conf_used,
                                    hrs_at_entry, stype, dir_used, sig_at_entry
                                )
                            except Exception as le:
                                blog(f"[LEARN] Error: {le}", "warning")

                            # Save state
                            save_state({
                                "last_ip":         _ip_state["current"],
                                "profit_buffer":   s["profit_buffer"],
                                "buffer_high":     s["buffer_high"],
                                "losses_absorbed": s["losses_absorbed"],
                                "trail_state":     bot_state["trail_state"],
                                "wins":            s["wins"],
                                "losses":          s["losses"],
                                "win_rate":        s["win_rate"],
                                "last_signal":     s["last_signal"],
                                "setup_memory":    bot_state["setup_memory"],
                                "starting_balance": s["starting_balance"],
                                "peak_balance":    s["peak_balance"],
                                "profit_floor":    s["profit_floor"],
                                "max_daily_trades": bot_state.get("max_daily_trades", 12),
                                "trade_cooldown":   bot_state.get("trade_cooldown", 180),
                                "deep_memory":      bot_state.get("deep_memory", {}),
                                "signal_weights":   bot_state.get("signal_weights", {}),
                            })
                    except Exception as e:
                        blog(f"[{sym}] Close error: {e}", "error")
    except Exception as e:
        blog(f"Position manager error: {e}", "error")

# ── Execute Trade ─────────────────────────────────────────────────

def get_optimal_itm_target(regime, vol_regime, conf, hours_to_expiry, is_call):
    """
    Returns the ideal ITM depth as a % of BTC price.
    Positive = ITM for calls (strike below price)
    Positive = ITM for puts  (strike above price)

    Matrix: regime × vol_regime × expiry
    """
    # ── Base ITM target from regime × vol ──────────────────────
    # Table: (regime, vol) → (min_itm_pct, max_itm_pct)
    itm_table = {
        ("STRONG_BULL", "LOW"):  (2.0, 3.0),
        ("STRONG_BULL", "MID"):  (1.5, 2.5),
        ("STRONG_BULL", "HIGH"): (0.5, 1.0),
        ("BULL",        "LOW"):  (1.0, 2.0),
        ("BULL",        "MID"):  (0.5, 1.5),
        ("BULL",        "HIGH"): (0.0, 0.5),   # ATM in high vol
        ("NEUTRAL",     "LOW"):  (-0.3, 0.3),  # ATM
        ("NEUTRAL",     "MID"):  (-0.3, 0.3),
        ("NEUTRAL",     "HIGH"): (-1.0, 0.0),  # slight OTM
        ("BEAR",        "LOW"):  (1.0, 2.0),
        ("BEAR",        "MID"):  (0.5, 1.5),
        ("BEAR",        "HIGH"): (0.0, 0.5),
        ("STRONG_BEAR", "LOW"):  (2.0, 3.0),
        ("STRONG_BEAR", "MID"):  (1.5, 2.5),
        ("STRONG_BEAR", "HIGH"): (0.5, 1.0),
    }

    key = (regime, vol_regime)
    if key not in itm_table:
        key = ("NEUTRAL", "MID")  # safe fallback

    min_itm, max_itm = itm_table[key]

    # ── Scale within range based on confidence ──────────────────
    # conf 55 = min target, conf 90+ = max target
    conf_scale = max(0, min(1, (conf - 55) / 35))  # 0 at 55%, 1 at 90%
    target_itm = min_itm + (max_itm - min_itm) * conf_scale

    # ── Adjust for expiry ───────────────────────────────────────
    if hours_to_expiry < 24:
        target_itm *= 0.3   # near expiry: theta risk, go ATM
    elif hours_to_expiry < 48:
        target_itm *= 0.6   # short: reduce ITM depth
    elif hours_to_expiry > 120:
        target_itm *= 1.2   # long dated: can go deeper safely

    # Clamp to reasonable bounds
    target_itm = max(-1.5, min(3.5, target_itm))

    return round(target_itm, 2)


def score_strike(strike, price, target_itm_pct, is_call, hours_to_expiry):
    """
    Score a strike option for selection.
    Lower score = better match for strategy.

    target_itm_pct: how deep ITM we want (% of price)
      Positive = ITM (call: strike below; put: strike above)
      Zero = ATM
      Negative = OTM
    """
    if is_call:
        # For calls: ITM means strike BELOW price
        # actual_itm = (price - strike) / price * 100
        actual_itm = (price - strike) / price * 100
    else:
        # For puts: ITM means strike ABOVE price
        # actual_itm = (strike - price) / price * 100
        actual_itm = (strike - price) / price * 100

    # Distance from ideal ITM depth
    diff = abs(actual_itm - target_itm_pct)

    # Penalty for being on wrong side (OTM when we want ITM)
    wrong_side_penalty = 0
    if target_itm_pct > 0.3 and actual_itm < 0:
        # Wanted ITM but got OTM — heavy penalty
        wrong_side_penalty = 3.0 + abs(actual_itm)
    elif target_itm_pct < -0.3 and actual_itm > 0.5:
        # Wanted OTM but got deep ITM
        wrong_side_penalty = 1.0

    return diff + wrong_side_penalty

def get_orderbook(product_id):
    """
    Fetch best bid and ask for an option from Delta Exchange India.
    Uses /v2/l2orderbook/{symbol} — public endpoint, no auth needed.
    Returns: {"bid": float, "ask": float, "spread": float, "spread_pct": float}
    or None if orderbook unavailable.
    """
    try:
        # Resolve product_id to symbol from cached products
        products = get_products_cached()
        product  = next((p for p in products if p.get("id") == product_id), None)
        if not product:
            return None
        sym = product.get("symbol", "")
        if not sym:
            return None
        r    = pub_get(f"/v2/l2orderbook/{sym}?depth=5")
        ob   = r.get("result", {})
        bids = ob.get("buy", [])
        asks = ob.get("sell", [])
        if not bids or not asks:
            return None
        best_bid = float(bids[0][0]) if isinstance(bids[0], list) else float(bids[0].get("price", 0))
        best_ask = float(asks[0][0]) if isinstance(asks[0], list) else float(asks[0].get("price", 0))
        if best_bid <= 0 or best_ask <= 0:
            return None
        spread     = best_ask - best_bid
        spread_pct = (spread / best_ask * 100) if best_ask > 0 else 999
        return {
            "bid":        best_bid,
            "ask":        best_ask,
            "spread":     round(spread, 4),
            "spread_pct": round(spread_pct, 2),
        }
    except Exception as e:
        blog(f"Orderbook error product_id={product_id}: {e}", "warning")
        return None

def place_smart_limit_order(product, size, asset, direction):
    """
    Smart limit order execution:
    1. Fetch bid/ask from orderbook
    2. Skip if spread > 3% or no liquidity
    3. Place limit at bid + 40% of spread (queue-jump price)
    4. Wait up to 4 seconds for fill
    5. Cancel and fall back to market order if unfilled

    Returns: (success: bool, fill_type: str)
    """
    sym        = product["symbol"]
    product_id = product["id"]

    # ── 1. Fetch orderbook ─────────────────────────────────────
    ob = get_orderbook(product_id)

    if ob is None:
        blog(f"[{asset}] Orderbook unavailable for {sym} — falling back to market", "warning")
        return _place_market_order(product, size, asset, direction), "market_fallback"

    bid        = ob["bid"]
    ask        = ob["ask"]
    spread     = ob["spread"]
    spread_pct = ob["spread_pct"]

    blog(f"[{asset}] Orderbook: bid=${bid:.2f} ask=${ask:.2f} "
         f"spread=${spread:.2f} ({spread_pct:.2f}%)", "info")

    # ── 2. Skip if spread too wide ─────────────────────────────
    if spread_pct > 3.0:
        blog(f"[{asset}] ⚠️ Spread {spread_pct:.2f}% > 3% threshold — skipping trade", "warning")
        return False, "skipped_wide_spread"

    # ── 3. Compute optimal entry price ─────────────────────────
    # bid + 40% of spread = queue-jump above passive bid
    # pays less than ask but likely fills faster than resting at bid
    entry_price = round(bid + spread * 0.4, 2)
    blog(f"[{asset}] Limit entry: ${entry_price:.2f} "
         f"(bid + 40% spread | ask save: ${ask - entry_price:.2f})", "info")

    # ── 4. Place limit order ───────────────────────────────────
    try:
        resp = dx_post("/v2/orders", {
            "product_id":     product_id,
            "product_symbol": sym,
            "size":           size,
            "side":           "buy",
            "order_type":     "limit_order",
            "limit_price":    str(entry_price),
        })
        if resp.get("error"):
            err = resp["error"]
            blog(f"[{asset}] Limit order failed: {err.get('code','err')} "
                 f"— falling back to market", "warning")
            return _place_market_order(product, size, asset, direction), "market_fallback"

        order_result = resp.get("result", {})
        order_id     = order_result.get("id")
        if not order_id:
            blog(f"[{asset}] No order ID returned — market fallback", "warning")
            return _place_market_order(product, size, asset, direction), "market_fallback"

        blog(f"[{asset}] Limit order #{order_id} placed @ ${entry_price:.2f}", "info")

    except Exception as e:
        blog(f"[{asset}] Limit order exception: {e} — market fallback", "warning")
        return _place_market_order(product, size, asset, direction), "market_fallback"

    # ── 5. Wait up to 4s for fill ──────────────────────────────
    fill_deadline = time.time() + 4.0
    while time.time() < fill_deadline:
        time.sleep(0.8)
        try:
            status_r = dx_get(f"/v2/orders/{order_id}")
            order    = status_r.get("result", {})
            state    = order.get("state", "")
            filled   = float(order.get("size_filled", 0) or 0)

            if state == "closed" and filled >= size:
                avg_fill = float(order.get("average_fill_price", entry_price) or entry_price)
                saving   = round((ask - avg_fill) * size, 4)
                blog(f"[{asset}] ✅ Limit filled @ ${avg_fill:.2f} | "
                     f"Saved ${saving:.4f} vs market ask", "success")
                return True, "limit_filled"

            if state in ("cancelled", "rejected"):
                blog(f"[{asset}] Order {state} — market fallback", "warning")
                return _place_market_order(product, size, asset, direction), "market_fallback"

        except Exception as e:
            blog(f"[{asset}] Fill check error: {e}", "warning")
            break

    # ── 6. Not filled — cancel and use market ─────────────────
    blog(f"[{asset}] Limit unfilled after 4s — cancelling, switching to market", "warning")
    try:
        dx_delete("/v2/orders", {"id": order_id, "product_id": product_id})
    except:
        pass
    return _place_market_order(product, size, asset, direction), "market_fallback"


def _place_market_order(product, size, asset, direction):
    """Fallback market order — used when limit fails or spread too wide."""
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
            blog(f"[{asset}] Market fallback failed: {err.get('code','err')}", "error")
            return False
        if not resp.get("result"):
            blog(f"[{asset}] Market fallback no result", "error")
            return False
        blog(f"[{asset}] ✓ Market order filled: {product['symbol']}", "success")
        return True
    except Exception as e:
        blog(f"[{asset}] Market fallback exception: {e}", "error")
        return False


def execute_trade(analysis, is_pyramid=False):
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

    # Trade cooldown — prevent rapid re-entries
    if not is_pyramid:
        elapsed = time.time() - bot_state.get("last_trade_time", 0)
        cooldown = bot_state.get("trade_cooldown", 180)
        if elapsed < cooldown:
            blog(f"[{asset}] Cooldown: {int(cooldown-elapsed)}s remaining", "info")
            return False

    try:
        open_pos   = dx_get("/v2/positions/margined").get("result",[])
        open_syms  = [p["product_symbol"] for p in open_pos
                      if abs(float(p.get("size",0))) > 0]

        # Never open same asset twice — UNLESS pyramiding into winning position
        if not is_pyramid and any(asset.upper() in s for s in open_syms):
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

    # ── SMART STRIKE SELECTION ──────────────────────────────────────
    # The right strike depends on regime and confidence:
    #
    # STRONG signal (conf>=75) in trending regime:
    #   CALL → ITM: strike 0.5-1.5% BELOW price → delta 0.6-0.8 → moves more with BTC
    #   PUT  → ITM: strike 0.5-1.5% ABOVE price → delta 0.6-0.8 → moves more as BTC falls
    #
    # NORMAL signal (conf 55-75):
    #   → ATM: strike within 0.5% of price → delta ~0.5 → balanced
    #
    # WEAK signal or high vol:
    #   → Slightly OTM → cheaper premium, defined loss
    #
    # ITM options cost more but earn MORE per $ BTC moves (higher delta)
    # OTM options are cheaper but need big BTC move to profit (low delta)

    regime   = analysis.get("regime", "NEUTRAL")
    vol_reg  = bot_state.get("last_vol_regime", "MID")
    is_call  = direction == "BUY_CALL"
    is_put   = direction == "BUY_PUT"

    # ── Get optimal ITM target for this exact market condition ──
    # Uses: regime × vol_regime × confidence × expiry
    avg_expiry = 72  # default, will recalc per option
    target_itm = get_optimal_itm_target(regime, vol_reg, conf, avg_expiry, is_call)

    # Check deep_memory — if OTM has been losing for this regime/vol, go more ITM
    dm = bot_state.get("deep_memory", {})
    for stype in ["OTM", "ATM"]:
        key = f"{regime}_{'CALL' if is_call else 'PUT'}_{stype}_{vol_reg}_medium"
        if key in dm:
            d = dm[key]
            tot = d["wins"] + d["losses"]
            if tot >= 4:
                wr = d["wins"] / tot * 100
                if wr < 35 and stype == "OTM":
                    target_itm = max(target_itm, target_itm + 0.5)  # push more ITM
                    blog(f"[LEARN] Nudging ITM target +0.5% — {key} losing ({wr:.0f}% WR)", "info")
                elif wr < 35 and stype == "ATM":
                    target_itm = max(target_itm, target_itm + 0.3)
                    blog(f"[LEARN] Nudging ITM target +0.3% — ATM losing in {regime}", "info")

    blog(f"[{asset}] Strike target: {target_itm:+.1f}% ITM | "
         f"Regime:{regime} Vol:{vol_reg} Conf:{conf}%", "info")

    # Sort all options using the score_strike engine
    def smart_score(p):
        try:
            strike = float(p["symbol"].split("-")[2])
            hrs    = get_hours(p)
            # Recalculate target with actual expiry for precision
            itm_for_this = get_optimal_itm_target(regime, vol_reg, conf, hrs, is_call)
            return score_strike(strike, price, itm_for_this, is_call, hrs)
        except:
            return 999.0

    valid.sort(key=smart_score)

    # Prefer liquid, re-sort by same strategy
    liquid_options = [p for p in valid
                      if float(p.get("volume", 0) or 0) > 0
                      or float(p.get("open_interest", 0) or 0) > 0]
    if not liquid_options:
        blog(f"[{asset}] No liquid options — using best strike regardless", "warning")
        liquid_options = valid
    else:
        liquid_options.sort(key=smart_score)

    # Block re-buying same option that just lost
    last_sig = bot_state["stats"].get("last_signal", {})
    last_opt = last_sig.get("option", "")
    consec   = bot_state["stats"].get("consecutive_losses", 0)
    if consec > 0 and last_opt:
        elapsed_since_last = time.time() - bot_state.get("last_trade_time", 0)
        if elapsed_since_last < 700:
            before = len(liquid_options)
            liquid_options = [p for p in liquid_options if p["symbol"] != last_opt]
            if len(liquid_options) < before:
                blog(f"[{asset}] Skipping recently lost {last_opt} — next best", "info")

    product = liquid_options[0] if liquid_options else (valid[0] if valid else None)
    if not product:
        blog(f"[{asset}] No options available", "warning")
        return False

    # Log selected strike and how it compares to target
    chosen_strike = float(product["symbol"].split("-")[2]) if "-" in product["symbol"] else 0
    actual_itm    = ((price - chosen_strike) / price * 100 if is_call
                     else (chosen_strike - price) / price * 100)
    strike_type   = ("ITM" if actual_itm > 0.3
                     else "ATM" if actual_itm >= -0.3
                     else "OTM")
    blog(f"[{asset}] ✅ Strike selected: {product['symbol']} | "
         f"Type:{strike_type} | Actual ITM:{actual_itm:+.2f}% | "
         f"Target:{target_itm:+.1f}% | BTC:${price:.0f}", "info")
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
        # ── Smart limit order execution ─────────────────────────────
        # Tries limit at bid+40% spread first.
        # Skips if spread >3% of price (wide/illiquid market).
        # Falls back to market if unfilled within 4 seconds.
        filled, fill_type = place_smart_limit_order(product, size, asset, direction)

        if not filled:
            return False  # skip_wide_spread or market fallback also failed

        blog(f"[{asset}] ✓ {direction} {size}x → {product['symbol']} ({fill_type})", "success")
        bot_state["last_trade_time"] = time.time()  # start cooldown
        s = bot_state["stats"]
        s["trades_today"]       += 1
        s["total_exposure_pct"] += 3.0 * size
        # consecutive_losses ONLY resets in manage_positions on WIN close
        # Snapshot signals and strike info for learning when trade closes
        actual_itm_snap = ((price - float(product["symbol"].split("-")[2])) / price * 100
                           if is_call else
                           (float(product["symbol"].split("-")[2]) - price) / price * 100
                           ) if "-" in product["symbol"] else 0
        stype_snap = ("ITM" if actual_itm_snap > 0.3
                      else "ATM" if actual_itm_snap >= -0.3 else "OTM")
        bot_state["_last_strike_type"] = stype_snap

        hrs_snap = get_hours(product)
        s["last_signal"] = {
            "asset": asset, "direction": direction,
            "option": product["symbol"], "price": price,
            "confidence": conf, "size": size,
            "regime": analysis.get("regime", "NEUTRAL"),
            "hours_to_expiry": hrs_snap,
            "strike_type": stype_snap,
            "time": datetime.now(timezone.utc).isoformat(),
            # Snapshot signals for learning on close
            "signals_snapshot": {
                "polymarket": analysis.get("poly_bull", False),
                "order_flow": analysis.get("flow_buy", False),
                "rsi":        analysis["indicators"].get("rsi_1h", 50) < 50,
                "macd":       analysis["indicators"].get("macd_1h") == "BULLISH",
            },
        }
        return True
    except Exception as e:
        blog(f"[{asset}] Exception: {e}", "error")
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
                # Note: consecutive_losses only resets on WIN CLOSE, not on trade entry
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
                            # Use pnl_pct not upnl (upnl can be stale at close time)
                            scalper_pnl = ((mark - entry) / entry * 100) if entry else 0
                            if scalper_pnl > 0: sc["wins"]   += 1
                            else:               sc["losses"] += 1
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
        _cap = bot_state.get("max_daily_trades", 12)
        _cap_lbl = "∞" if _cap == 0 else str(_cap)
        blog(f"━━ PRO CYCLE | Trades:{s['trades_today']}/{_cap_lbl} | "
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
            bot_state["pyramid_state"] = {}  # clear daily
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
        manage_positions()

        # ── SIGNAL REVERSAL CHECK ────────────────────────────────
        # If open position and signal flips hard → exit to stop bleeding
        try:
            _open = dx_get("/v2/positions/margined").get("result", [])
            _open = [p for p in _open if abs(float(p.get("size", 0))) > 0]
            if _open and analyses.get("BTC"):
                _a = analyses["BTC"]
                check_signal_reversal(_open, _a["direction"], _a["confidence"])
        except Exception as _re:
            blog(f"Reversal check error: {_re}", "error")

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

            # After losses, raise the bar — prevent revenge trading
            consec = bot_state["stats"].get("consecutive_losses", 0)
            imm_threshold  = min(88, 75 + consec * 7)  # 75→82→89 per loss, cap 88
            conf_threshold = min(72, 55 + consec * 5)  # 55→60→65 per loss, cap 72

            if best["confidence"] >= imm_threshold:
                blog(f"[SWING] High conviction {best['confidence']}% "
                     f"(threshold:{imm_threshold}%) — trading", "bot")
                bot_state["pending_signal"] = {}
                execute_trade(best)
            elif same_asset and same_dir and pending_age < 900 and best["confidence"] >= conf_threshold:
                blog(f"[SWING] Signal confirmed (need {conf_threshold}%) — executing", "bot")
                bot_state["pending_signal"] = {}
                execute_trade(best)
            elif best["confidence"] >= conf_threshold:
                bot_state["pending_signal"] = {
                    "asset": best["asset"], "direction": best["direction"],
                    "confidence": best["confidence"], "ts": time.time(),
                }
                blog(f"[SWING] Pending confirmation: {best['asset']} "
                     f"{best['direction']} {best['confidence']}% "
                     f"(need {imm_threshold}% to fire immediately)", "info")
            else:
                blog(f"[SWING] Confidence {best['confidence']}% below {conf_threshold}% — skipping", "info")
                bot_state["pending_signal"] = {}
        else:
            blog("[SWING] No valid setup this cycle", "info")

        # ── PYRAMIDING STRATEGY ───────────────────────────────────
        # Add to winning position when profit > 15% + signal still strong
        # Only 1 pyramid per original position, always 1 lot
        try:
            positions = dx_get("/v2/positions/margined").get("result", [])
            open_pos  = [p for p in positions if abs(float(p.get("size", 0))) > 0]
            pyramid_state = bot_state.get("pyramid_state", {})

            for pos in open_pos:
                sym   = pos.get("product_symbol", "")
                if "BTC" not in sym: continue
                entry = float(pos.get("entry_price", 0))
                mark  = float(pos.get("mark_price", 0) or 0)
                size  = abs(float(pos.get("size", 0)))
                if entry <= 0 or mark <= 0: continue

                pnl_pct  = ((mark - entry) / entry * 100)
                is_call  = sym.startswith("C-")
                is_put   = sym.startswith("P-")

                # Detect manual vs bot position — both eligible for pyramid
                last_bot_opt = s.get("last_signal", {}).get("option", "")
                is_manual = (sym != last_bot_opt and
                             sym not in bot_state.get("trail_state", {}))
                if is_manual and pnl_pct > 0:
                    blog(f"[PYRAMID] Manual position {sym} +{pnl_pct:.1f}% "
                         f"— eligible for add", "info")

                # Get best current signal
                best_sig = best if best else None

                # Signal must match position direction
                signal_matches = (
                    best_sig and (
                        (is_call and best_sig["direction"] == "BUY_CALL") or
                        (is_put  and best_sig["direction"] == "BUY_PUT")
                    )
                )

                already_pyramided = pyramid_state.get(sym, False)

                # ── Dynamic pyramid threshold ────────────────────────
                # Higher confidence = add earlier (don't wait for +15%)
                # conf >= 90%: add at +3%  — extreme conviction, catch the run early
                # conf >= 82%: add at +8%  — high conviction
                # conf >= 75%: add at +15% — standard pyramid
                sig_conf = best_sig["confidence"] if best_sig else 0
                if sig_conf >= 90:
                    pyramid_threshold = 3.0
                elif sig_conf >= 82:
                    pyramid_threshold = 8.0
                else:
                    pyramid_threshold = 15.0

                pyramid_ok = (
                    pnl_pct >= pyramid_threshold         and  # dynamic threshold
                    signal_matches                       and  # signal confirms direction
                    sig_conf >= 75                       and  # min confidence
                    not already_pyramided                and  # one pyramid per original
                    s["trades_today"] < 11               and  # leave room in cap
                    s["consecutive_losses"] == 0         and  # no loss streak
                    s.get("current_balance", 0) > 50         # enough balance
                )

                if pyramid_ok:
                    pos_type = "MANUAL" if is_manual else "BOT"
                    blog(f"[PYRAMID] 🔺 {pos_type} {sym} +{pnl_pct:.1f}% "
                         f">= {pyramid_threshold}% threshold | "
                         f"Conf:{sig_conf}% — opening P2", "success")
                    success = execute_trade(best_sig, is_pyramid=True)
                    if success:
                        pyramid_state[sym] = True
                        bot_state["pyramid_state"] = pyramid_state
                        blog(f"[PYRAMID] ✅ P2 opened — compounding {sym}", "success")
                    else:
                        blog(f"[PYRAMID] P2 blocked by risk check", "warning")
                elif pnl_pct > 0 and not already_pyramided and best_sig and signal_matches:
                    blog(f"[PYRAMID] {sym} +{pnl_pct:.1f}% — "
                         f"need +{pyramid_threshold:.0f}% to add "
                         f"(conf:{sig_conf:.0f}%)", "info")

            # Clean pyramid state when positions close
            open_syms = {p.get("product_symbol") for p in open_pos}
            for sym in list(pyramid_state.keys()):
                if sym not in open_syms:
                    del pyramid_state[sym]
            bot_state["pyramid_state"] = pyramid_state

        except Exception as pe:
            blog(f"[PYRAMID] Error: {pe}", "error")

        # ── MODE 2: SCALPER STRATEGY ─────────────────────────────
        # Independent — quick trades, 8% target, doesn't wait for swing
        run_scalper(analyses)

        # ── FORCE TRADE: Last resort if nothing triggered ─────────
        # If swing found no setup and scalper has no active trade,
        # force best available signal if confidence >= 50%
        sc = bot_state["scalper"]
        _btc = analyses.get("BTC")
        if not sc["active_trade"] and _btc and _btc.get("direction") == "NO TRADE":
            # Only force if no open positions at all
            try:
                open_pos = dx_get("/v2/positions/margined").get("result",[])
                has_open = any(abs(float(p.get("size",0))) > 0 for p in open_pos)
            except:
                has_open = True
            if not has_open and _btc.get("confidence",0) >= 52:
                # Reuse already-computed analysis — don't waste 8 API calls
                if _btc.get("direction") != "NO TRADE":
                    blog(f"⚡ Force trade: BTC {_btc['direction']} {_btc['confidence']}%", "warning")
                    execute_trade(_btc)

        total = s["wins"] + s["losses"]
        if total > 0: s["win_rate"] = round(s["wins"]/total*100, 1)

    except Exception as e:
        blog(f"Cycle error: {traceback.format_exc()}","error")
    finally:
        bot_state["cycle_lock"] = False

def position_monitor_loop():
    """
    Lightweight position monitor — runs every 90s independently.
    Catches stop losses faster than the 5-min main cycle.
    Only checks stops/trails, doesn't open new trades.
    """
    while not stop_event.is_set():
        try:
            if bot_state["running"] and API_KEY:
                manage_positions()
        except: pass
        stop_event.wait(90)

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
    # Start fast position monitor (90s cycle for stop loss protection)
    monitor_thread = threading.Thread(target=position_monitor_loop, daemon=True)
    monitor_thread.start()
    blog(f"PRO Bot started | {bot_state['interval']}s | position monitor: 90s","success")
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
    # Summarize learning state for dashboard
    sw = bot_state.get("signal_weights", {})
    sw_summary = {k: {"weight": round(v["weight"],2), "accuracy": round(v["correct"]/v["total"]*100) if v["total"]>0 else 50, "total": v["total"]} for k,v in sw.items()}
    dm = bot_state.get("deep_memory", {})
    dm_summary = {k: {"wr": round(v["wins"]/(v["wins"]+v["losses"])*100) if v["wins"]+v["losses"]>0 else 0, "count": v["wins"]+v["losses"]} for k,v in dm.items() if v["wins"]+v["losses"]>=2}
    return jsonify({
        "running":        bot_state["running"],
        "strategy":       bot_state["strategy"],
        "interval":       bot_state["interval"],
        "stats":          bot_state["stats"],
        "log":            bot_state["log"][-200:],
        "ip":             _ip_state["current"],
        "signal_weights": sw_summary,
        "deep_memory":    dm_summary,
        "trade_cap":      {"cap": bot_state.get("max_daily_trades",12), "cooldown": bot_state.get("trade_cooldown",180)},
    })

@app.route("/api/bot/set_cap", methods=["POST"])
def api_set_cap():
    d = request.json or {}
    cap = int(d.get("cap", 12))
    cap = max(0, min(50, cap))  # clamp 0-50
    bot_state["max_daily_trades"] = cap
    label = "unlimited" if cap == 0 else str(cap)
    blog(f"Daily trade cap set to {label} trades", "info")
    s = bot_state["stats"]
    save_state({
        "last_ip": _ip_state.get("current",""),
        "profit_buffer": s["profit_buffer"], "buffer_high": s["buffer_high"],
        "losses_absorbed": s["losses_absorbed"], "starting_balance": s["starting_balance"],
        "peak_balance": s["peak_balance"], "profit_floor": s["profit_floor"],
        "wins": s["wins"], "losses": s["losses"], "win_rate": s["win_rate"],
        "last_signal": s["last_signal"], "trail_state": bot_state["trail_state"],
        "setup_memory": bot_state["setup_memory"],
        "max_daily_trades": bot_state["max_daily_trades"],
        "trade_cooldown": bot_state["trade_cooldown"],
        "deep_memory":    bot_state.get("deep_memory", {}),
        "signal_weights": bot_state.get("signal_weights", {}),
    })
    return jsonify({"ok": True, "cap": cap})

@app.route("/api/bot/set_cooldown", methods=["POST"])
def api_set_cooldown():
    d = request.json or {}
    seconds = int(d.get("seconds", 180))
    seconds = max(60, min(600, seconds))  # clamp 60s-10min
    bot_state["trade_cooldown"] = seconds
    blog(f"Trade cooldown set to {seconds}s", "info")
    s = bot_state["stats"]
    save_state({
        "last_ip": _ip_state.get("current",""),
        "profit_buffer": s["profit_buffer"], "buffer_high": s["buffer_high"],
        "losses_absorbed": s["losses_absorbed"], "starting_balance": s["starting_balance"],
        "peak_balance": s["peak_balance"], "profit_floor": s["profit_floor"],
        "wins": s["wins"], "losses": s["losses"], "win_rate": s["win_rate"],
        "last_signal": s["last_signal"], "trail_state": bot_state["trail_state"],
        "setup_memory": bot_state["setup_memory"],
        "max_daily_trades": bot_state["max_daily_trades"],
        "trade_cooldown": bot_state["trade_cooldown"],
        "deep_memory":    bot_state.get("deep_memory", {}),
        "signal_weights": bot_state.get("signal_weights", {}),
    })
    return jsonify({"ok": True, "seconds": seconds})

@app.route("/api/bot/set_target", methods=["POST"])
def api_set_target():
    d = request.json or {}
    target = float(d.get("target", 10))
    target = max(1, min(100, target))
    bot_state["daily_target"] = target
    blog(f"Daily target set to {target}%", "info")
    return jsonify({"ok": True, "target": target})

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


@app.route("/api/bot/add_lot", methods=["POST"])
def api_add_lot():
    """
    Manual ADD LOT — user taps button on dashboard.
    Immediately places 1 more lot on same symbol or best current signal.
    Bypasses swing cooldown and same-asset check (is_pyramid=True).
    """
    d      = request.json or {}
    sym    = d.get("symbol", "")
    blog(f"[MANUAL ADD] User requested add lot for {sym}", "warning")

    try:
        # Get current analysis
        a = full_analysis("BTC")
        if not a:
            return jsonify({"ok": False, "error": "Analysis failed"})

        a = execution_engine(a)

        # Force direction from existing position type
        if sym.startswith("C-"):
            a["direction"] = "BUY_CALL"
        elif sym.startswith("P-"):
            a["direction"] = "BUY_PUT"

        # Override: must be a valid direction
        if a["direction"] not in ("BUY_CALL", "BUY_PUT"):
            return jsonify({"ok": False, "error": "No valid signal for add"})

        # Execute as pyramid (bypasses same-asset check)
        success = execute_trade(a, is_pyramid=True)

        if success:
            blog(f"[MANUAL ADD] ✅ Lot added for {sym}", "success")
            return jsonify({"ok": True, "symbol": sym})
        else:
            return jsonify({"ok": False, "error": "Trade blocked by risk check"})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

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
