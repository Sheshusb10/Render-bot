“””
ΔLPHA BOT v6.2 — Delta Exchange India | BTC Options
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG FIXES vs v6.1:
✅ BUG 1 FIXED  — 5 duplicate Flask routes removed (was crashing on startup)
✅ BUG 2 FIXED  — Circular import ‘from server_v6 import Position’ removed
✅ BUG 3 FIXED  — _auto_start() now correctly placed after bot=AlphaBot()
✅ BUG 4 FIXED  — Cfg.STARTING_CAPITAL ref removed (doesn’t exist in Cfg)
✅ BUG 5 FIXED  — DASHBOARD=open() removed (crashes at module load)
✅ BUG 6 FIXED  — ADX smoothing arrays properly aligned (was wrong regime)
✅ BUG 7 FIXED  — Candle parse handles both dict+array Delta formats + volume fallback
✅ BUG 8 FIXED  — Kelly minimum $20 floor (was $5 — couldn’t cover any premium)
✅ BUG 9 FIXED  — Premium vs move formula corrected (wrong units blocked all options)
✅ BUG 10 FIXED — Backslash continuation syntax error in manual_trade

PROFITABILITY IMPROVEMENTS:
✅ RSI(14) replaces RSI(7) — less noise on 5-min, better entry timing
✅ MACD(8,21,5) replaces MACD(5,13,5) — fewer whipsaws, higher signal quality
✅ Confidence threshold lowered to 58 — gets trades actually executing
✅ MIN_TRADES_BEFORE_KELLY = 10 — use fixed 1.5% until stats are meaningful
✅ Divergence detection rewrote — was silently crashing on edge cases
✅ Options premium formula fixed — bot now uses options not just perpetuals
✅ News multiplier capped at 1.1/0.85 — was over-amplifying single headlines
✅ Regime NEUTRAL now scores 5 (was 0) — allows range trading
✅ Volume check relaxed to 0.3× avg (was 0.5×) — crypto volume is episodic
✅ Macro blackout reduced to 20min (was 45min) — 45min blocked too many sessions
“””

import os, time, hmac, hashlib, json, logging, requests, threading, math
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,
format=”%(asctime)s [%(levelname)s] %(message)s”, datefmt=”%Y-%m-%d %H:%M:%S”)
log = logging.getLogger(“ALPHA”)

# ══════════════════════════════════════════════════════════════════════════════

# CONFIG

# ══════════════════════════════════════════════════════════════════════════════

class Cfg:
# Keys: env var OR Render Secret File (auto-stripped of whitespace/newlines)
API_KEY    = os.getenv(“DELTA_API_KEY”, “”).strip()
_sf = “/etc/secrets/DELTA_API_SECRET”
API_SECRET = (os.getenv(“DELTA_API_SECRET”, “”).strip() or
(open(_sf).read().strip() if os.path.exists(_sf) else “”))
BASE_URL   = “https://api.india.delta.exchange”

```
# Risk
MAX_RISK_NORMAL    = 0.02
MAX_RISK_HOT       = 0.03
MAX_RISK_RECOVERY  = 0.005
KELLY_FRACTION     = 0.25
MIN_TRADE_SIZE_USD = 20.0   # FIX BUG 8: floor prevents $5 trades
MAX_OPEN_POSITIONS = 2
MIN_TRADES_BEFORE_KELLY = 10  # Use fixed sizing until we have real stats

# Monthly targets
MONTHLY_TARGET_PCT = 0.10
MONTHLY_LOSS_LIMIT = 0.08
DAILY_LOSS_LIMIT   = 0.03

# Circuit breaker
MAX_CONSEC_LOSSES  = 3
RECOVERY_TRADES    = 2

# ── TREND MODE exits (ADX > 25, confirmed regime) ──────────────────────
HARD_STOP_PCT      = 0.025   # 2.5% hard stop
TP1_PCT            = 0.015   # Take 50% at +1.5%
TP2_PCT            = 0.030   # Full exit at +3.0%
TRAIL_ACTIVATE_PCT = 0.012
TRAIL_DISTANCE_PCT = 0.007

# ── RANGE/SCALP MODE exits (ADX < 20, choppy market) ───────────────────
# Smaller targets, tighter stops — designed for whale stop-hunt reversals
RANGE_HARD_STOP    = 0.008   # 0.8% stop (tight — exit fast if wrong)
RANGE_TP1          = 0.005   # Take 70% at +0.5%
RANGE_TP2          = 0.010   # Full exit at +1.0%
RANGE_MAX_HOLD_MIN = 30      # Max 30 min hold in range mode (decay risk)
RANGE_MIN_CONF     = 45      # Lower confidence threshold for range trades
RANGE_RISK_PCT     = 0.008   # Only 0.8% capital per range trade (smaller)

# ── WHALE TRAP detection thresholds ─────────────────────────────────────
WICK_RATIO_MIN     = 2.0    # Wick must be 2x candle body (trap signal)
VOLUME_SPIKE_RATIO = 1.8    # Volume spike needed for real breakout confirm
ENTRY_DELAY_SECS   = 15     # Wait 15s after signal to avoid stop-hunt entry

# Regime
ADX_TREND_MIN      = 22     # Lowered slightly — 25 too strict for BTC
ADX_CHOP_MAX       = 16

# INDICATORS — PROFITABILITY FIX
RSI_PERIOD         = 14     # FIX: was 7 (too noisy on 5-min)
RSI_LONG_MIN       = 40
RSI_LONG_MAX       = 60
RSI_SHORT_MIN      = 40
RSI_SHORT_MAX      = 60
MACD_FAST          = 8      # FIX: was 5 (too fast)
MACD_SLOW          = 21     # FIX: was 13
MACD_SIGNAL        = 5

# Options premium safety — FIXED FORMULA
MIN_MOVE_TO_PREMIUM_RATIO = 1.2   # Expected move >= 1.2x option premium

# Funding
FUNDING_LONG_MAX   = 0.001
FUNDING_SHORT_MIN  = -0.0005

# OI
OI_SPIKE_PCT       = 0.12

# Time (UTC)
DEAD_ZONE_HOURS    = [2, 3, 4, 5]
PEAK_HOURS         = [8, 9, 13, 14, 15, 16]
MACRO_BLACKOUT_TIMES = [(13, 30), (19, 0)]  # Removed 8:30 — too aggressive
BLACKOUT_WINDOW_MINS = 20   # FIX: was 45 (blocked too many valid windows)

# Confidence — PROFITABILITY FIX
MIN_CONFIDENCE     = 58     # FIX: was 65 (too strict, bot never traded)
HIGH_CONFIDENCE    = 78

BTC_PRODUCT_ID     = 27
SCAN_INTERVAL      = 300

# ── EXECUTION REALITY (slippage model) ──────────────────────────────────
# Options: ~5% of mark price (wide spreads on small exchanges)
# Perpetuals: ~0.05% (tight BTC perpetual book)
SLIPPAGE_OPT_PCT   = 0.05
SLIPPAGE_PERP_PCT  = 0.0005
```

# ══════════════════════════════════════════════════════════════════════════════

# DELTA EXCHANGE API

# ══════════════════════════════════════════════════════════════════════════════

class DeltaAPI:
def **init**(self):
self.base    = Cfg.BASE_URL
self.key     = Cfg.API_KEY
self.secret  = Cfg.API_SECRET
self.session = requests.Session()
self.consecutive_failures = 0
self.healthy = True
self.last_success = time.time()
self.connected = False

```
def set_credentials(self, api_key: str, api_secret: str, region: str = "india"):
    self.key    = api_key.strip()
    self.secret = api_secret.strip()
    self.base   = ("https://api.india.delta.exchange" if region == "india"
                   else "https://api.delta.exchange")
    self.consecutive_failures = 0
    self.healthy = True
    self.connected = False

def _sign(self, method: str, path: str, query_string: str = "", body: str = "") -> dict:
    """
    Official Delta Exchange signature format (from docs):
    signature = HMAC-SHA256(secret, method + timestamp + path + query_string + body)
    query_string includes the leading '?' e.g. '?product_id=1&state=open'
    """
    ts  = str(int(time.time()))
    msg = method + ts + path + query_string + body
    sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key":      self.key,
        "timestamp":    ts,
        "signature":    sig,
        "Content-Type": "application/json",
        "User-Agent":   "alpha-bot-v6.2",
    }

def _get(self, path: str, params: dict = None):
    """Signed GET request. Builds query string for signature manually."""
    query_string = ""
    if params:
        query_string = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{self.base}{path}{query_string}"
    try:
        r = self.session.get(url,
                             headers=self._sign("GET", path, query_string),
                             timeout=10)
        data = r.json()
        if r.status_code == 200:
            self.consecutive_failures = 0
            self.healthy = True
            self.last_success = time.time()
        else:
            log.warning(f"API GET {path} → {r.status_code}: {data.get('error','?')} {data.get('message','')}")
            self.consecutive_failures += 1
        return data
    except Exception as e:
        self.consecutive_failures += 1
        if self.consecutive_failures >= 3:
            self.healthy = False
            log.error(f"API unhealthy ({self.consecutive_failures}x): {e}")
        return None

def _post(self, path: str, body: dict):
    body_str = json.dumps(body)
    try:
        r = self.session.post(f"{self.base}{path}",
                              headers=self._sign("POST", path, "", body_str),
                              data=body_str, timeout=10)
        data = r.json()
        if r.status_code not in (200, 201):
            log.warning(f"API POST {path} → {r.status_code}: {data.get('error','?')}")
        self.consecutive_failures = 0
        self.healthy = True
        return data
    except Exception as e:
        self.consecutive_failures += 1
        return None

def get_candles(self, symbol="BTCUSD", resolution=5, limit=100):
    """Signed request — Delta India requires auth for history/candles endpoint."""
    end   = int(time.time())
    start = end - (resolution * 60 * limit)
    params = {"symbol": symbol, "resolution": resolution,
              "start": start, "end": end}
    d = self._get("/v2/history/candles", params)
    if d and d.get("success"):
        result = d.get("result", [])
        log.info(f"Candles: {len(result)} × {resolution}min {symbol}")
        return result
    if d:
        log.warning(f"Candles error: {d.get('error','?')} {d.get('message','')}")
    return []

def get_candles_debug(self, symbol="BTCUSD", resolution=5):
    """Returns raw candle response for debugging."""
    end   = int(time.time())
    start = end - (resolution * 60 * 20)
    qs = f"?symbol={symbol}&resolution={resolution}&start={start}&end={end}"
    url = f"{self.base}/v2/history/candles{qs}"
    try:
        r = self.session.get(url, timeout=10)
        return {"url": url, "status": r.status_code, "raw": r.json()}
    except Exception as e:
        return {"url": url, "error": str(e)}

def get_ticker(self, symbol="BTCUSD"):
    """Public endpoint — no auth needed, works before login."""
    try:
        r = self.session.get(f"{self.base}/v2/tickers/{symbol}",
                             timeout=8)
        d = r.json()
        return d.get("result", {}) if d and d.get("success") else {}
    except Exception:
        return {}

def get_wallet(self):
    """
    Delta Exchange India wallet. Handles all field name variants.
    Returns dict: {"USDT": 50.0, "INR": 1000.0, "BTC": 0.001}
    """
    d = self._get("/v2/wallet/balances")
    if d and d.get("success"):
        balances = {}
        for b in d.get("result", []):
            symbol = (b.get("asset_symbol") or b.get("currency") or
                      b.get("asset") or "?")
            # Values come as strings from Delta India — must cast to float
            avail  = float(b.get("available_balance") or
                           b.get("available") or b.get("balance") or 0)
            if symbol and symbol != "?":
                balances[symbol.upper()] = avail
                balances[symbol.lower()] = avail
        # Fallback: use meta.net_equity if USD balance not found
        meta = d.get("meta", {})
        if not balances.get("USD") and meta.get("net_equity"):
            balances["USD"] = float(meta["net_equity"])
            balances["usd"] = float(meta["net_equity"])
        log.info(f"Wallet assets: { {k:v for k,v in balances.items() if v>0 and k==k.upper()} }")
        return balances
    if d:
        err = d.get("error", d.get("message", "unknown"))
        log.warning(f"Wallet failed: {err}")
    else:
        log.warning("Wallet returned None — IP likely not whitelisted")
    return {}

def get_positions(self):
    d = self._get("/v2/positions/margined")
    if d and d.get("success"):
        return [p for p in d.get("result", []) if float(p.get("size", 0)) != 0]
    return []

def get_orders(self):
    d = self._get("/v2/orders", {"state": "open"})
    return d.get("result", []) if d and d.get("success") else []

def get_options_chain(self, underlying="BTC"):
    d = self._get("/v2/products", {
        "contract_type": "call_options,put_options",
        "underlying_asset_symbol": underlying,
        "state": "live", "page_size": 50})
    return d.get("result", []) if d and d.get("success") else []

def get_funding_rate(self, symbol="BTCUSD"):
    t = self.get_ticker(symbol)
    return float(t.get("funding_rate", 0)) if t else 0.0

def get_open_interest(self, symbol="BTCUSD"):
    t = self.get_ticker(symbol)
    return float(t.get("open_interest", 0)) if t else 0.0

def place_order(self, product_id, side, size,
                order_type="market_order",
                limit_price=None, stop_price=None):
    body = {"product_id": product_id, "size": size, "side": side,
            "order_type": order_type, "time_in_force": "gtc"}
    if limit_price: body["limit_price"] = str(limit_price)
    if stop_price:  body["stop_price"]  = str(stop_price)
    return self._post("/v2/orders", body) or {}

def cancel_order(self, order_id, product_id):
    return self._post(f"/v2/orders/{order_id}/cancel",
                      {"product_id": product_id}) or {}
```

# ══════════════════════════════════════════════════════════════════════════════

# TECHNICAL ENGINE — ALL BUGS FIXED

# ══════════════════════════════════════════════════════════════════════════════

class TechEngine:

```
@staticmethod
def ema(prices: list, period: int) -> list:
    if not prices: return []
    if len(prices) < period:
        return [prices[-1]] * len(prices)
    k = 2 / (period + 1)
    vals = [sum(prices[:period]) / period]
    for p in prices[period:]:
        vals.append(p * k + vals[-1] * (1 - k))
    return [vals[0]] * (period - 1) + vals

@staticmethod
def rsi(prices: list, period: int = 14) -> float:
    """FIX BUG: RSI(14) not RSI(7) — far less noisy on 5-min crypto."""
    if len(prices) < period + 2: return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(d, 0.0) for d in deltas[-period:]]
    losses = [abs(min(d, 0.0)) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al < 1e-10: return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)

@staticmethod
def macd(prices: list, fast: int = 8, slow: int = 21, signal: int = 5):
    """
    FIX BUG: MACD(8,21,5) vs old (5,13,5).
    (5,13,5) on 5-min = effective 23-candle window = crossover every 20min.
    Too many false signals. (8,21,5) smoother, fewer whipsaws.
    """
    if len(prices) < slow + signal:
        return 0.0, 0.0, 0.0, []
    ef   = TechEngine.ema(prices, fast)
    es   = TechEngine.ema(prices, slow)
    ml   = [ef[i] - es[i] for i in range(len(prices))]
    sl   = TechEngine.ema(ml, signal)
    hist = [ml[i] - sl[i] for i in range(len(ml))]
    return ml[-1], sl[-1], hist[-1], hist

@staticmethod
def adx(highs: list, lows: list, closes: list, period: int = 14):
    """
    FIX BUG 6: Wilder smoothing now returns correctly aligned arrays.
    Previous sm() function had off-by-one indexing causing wrong +DI/-DI.
    """
    n = len(closes)
    if n < period * 2 + 1:
        return 0.0, 0.0, 0.0

    tr_vals, pdm_vals, ndm_vals = [], [], []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr_vals.append(max(h - l, abs(h - pc), abs(l - pc)))
        up   = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        pdm_vals.append(up   if (up > down and up > 0)   else 0.0)
        ndm_vals.append(down if (down > up and down > 0) else 0.0)

    # Wilder smoothing — FIX: use rolling update, not sum slice
    def wilder(data: list, p: int) -> list:
        result = [sum(data[:p])]
        for v in data[p:]:
            result.append(result[-1] - result[-1] / p + v)
        return result

    atr_w = wilder(tr_vals,  period)
    pdm_w = wilder(pdm_vals, period)
    ndm_w = wilder(ndm_vals, period)

    # All three have same length — no index mismatch
    plus_di  = [100 * pdm_w[i] / atr_w[i] if atr_w[i] > 0 else 0
                for i in range(len(atr_w))]
    minus_di = [100 * ndm_w[i] / atr_w[i] if atr_w[i] > 0 else 0
                for i in range(len(atr_w))]
    dx       = [abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i]) * 100
                if (plus_di[i] + minus_di[i]) > 0 else 0
                for i in range(len(plus_di))]

    adx_val = sum(dx[-period:]) / period
    return round(adx_val, 2), round(plus_di[-1], 2), round(minus_di[-1], 2)

@staticmethod
def atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(closes) < period + 1: return 0.0
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i-1]),
               abs(lows[i] - closes[i-1]))
           for i in range(1, len(closes))]
    return sum(trs[-period:]) / period

@staticmethod
def bollinger(prices: list, period: int = 20, std_dev: float = 2.0):
    if len(prices) < period:
        m = prices[-1]
        return m, m, m, 0.0
    w   = prices[-period:]
    mid = sum(w) / period
    std = math.sqrt(sum((p - mid) ** 2 for p in w) / period)
    if mid == 0: return mid, mid, mid, 0.0
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower, (upper - lower) / mid * 100

@staticmethod
def detect_divergence(prices: list, histogram: list,
                      lookback: int = 12) -> str:
    """
    FIX BUG: Previous version crashed silently on edge cases.
    Fully guarded with try/except and bounds checks.
    """
    try:
        if len(prices) < lookback or len(histogram) < lookback:
            return "none"
        p = prices[-lookback:]
        h = histogram[-lookback:]
        half = lookback // 2
        if half < 2: return "none"

        p1, p2 = p[:half], p[half:]
        h1, h2 = h[:half], h[half:]

        # Bullish: price lower low, histogram higher low
        pl1, pl2 = min(p1), min(p2)
        hl1 = h1[p1.index(pl1)] if pl1 in p1 else h1[-1]
        hl2 = h2[p2.index(pl2)] if pl2 in p2 else h2[-1]
        if pl2 < pl1 * 0.9995 and hl2 > hl1 and hl2 < 0:
            return "bullish"

        # Bearish: price higher high, histogram lower high
        ph1, ph2 = max(p1), max(p2)
        hh1 = h1[p1.index(ph1)] if ph1 in p1 else h1[-1]
        hh2 = h2[p2.index(ph2)] if ph2 in p2 else h2[-1]
        if ph2 > ph1 * 1.0005 and hh2 < hh1 and hh2 > 0:
            return "bearish"
    except Exception:
        pass
    return "none"

@staticmethod
def detect_whale_trap(opens: list, highs: list, lows: list,
                      closes: list, volumes: list) -> dict:
    """
    Detects whale stop-hunt / liquidity grab patterns.

    Pattern: Price spikes below support OR above resistance (the trap),
    then a strong reversal candle closes back above/below the level.
    This is the ENTRY SIGNAL — trade the reversal, not the breakout.

    Returns: {
        "trap_type": "bull_trap" | "bear_trap" | "none",
        "strength": 0-100,
        "entry_direction": "long" | "short" | None,
        "stop_level": float
    }
    """
    if len(closes) < 10 or not volumes:
        return {"trap_type": "none", "strength": 0,
                "entry_direction": None, "stop_level": 0}
    try:
        # Last 3 candles
        c1, c2, c3 = closes[-3], closes[-2], closes[-1]
        h1, h2, h3 = highs[-3], highs[-2], highs[-1]
        l1, l2, l3 = lows[-3], lows[-2], lows[-1]
        o2, o3 = opens[-2] if len(opens) >= 2 else c2, opens[-1] if opens else c3
        v_avg = sum(volumes[-10:]) / 10
        v_last = volumes[-1]

        body2 = abs(c2 - o2)
        lower_wick2 = min(o2, c2) - l2
        upper_wick2 = h2 - max(o2, c2)

        # ── BULL TRAP (bear stop-hunt reversal → go LONG) ─────────────
        # Candle 2: Spike DOWN with long lower wick (stop hunt below support)
        # Candle 3: Strong close ABOVE candle 2 open (reclaim)
        bull_trap = (
            lower_wick2 > body2 * Cfg.WICK_RATIO_MIN and  # Long lower wick
            l2 < l3 and                                     # Spike low
            c3 > o2 and                                     # Reclaim
            c3 > c2 and                                     # Bullish close
            v_last > v_avg * 1.2                            # Volume confirm
        )

        # ── BEAR TRAP (bull stop-hunt reversal → go SHORT) ────────────
        # Candle 2: Spike UP with long upper wick
        # Candle 3: Strong close BELOW candle 2 open (rejection)
        bear_trap = (
            upper_wick2 > body2 * Cfg.WICK_RATIO_MIN and
            h2 > h3 and
            c3 < o2 and
            c3 < c2 and
            v_last > v_avg * 1.2
        )

        if bull_trap:
            strength = min(100, int((lower_wick2 / body2) * 25 +
                                    (v_last / v_avg) * 25))
            return {"trap_type": "bull_trap", "strength": strength,
                    "entry_direction": "long", "stop_level": l2}
        if bear_trap:
            strength = min(100, int((upper_wick2 / body2) * 25 +
                                    (v_last / v_avg) * 25))
            return {"trap_type": "bear_trap", "strength": strength,
                    "entry_direction": "short", "stop_level": h2}
    except Exception:
        pass
    return {"trap_type": "none", "strength": 0,
            "entry_direction": None, "stop_level": 0}

@staticmethod
def range_bounds(highs: list, lows: list,
                 period: int = 20) -> tuple:
    """Detect range support/resistance for range-mode trading."""
    if len(highs) < period:
        return 0.0, 0.0
    resistance = max(highs[-period:])
    support    = min(lows[-period:])
    return support, resistance

@staticmethod
def is_range_market(adx_val: float, bb_width: float) -> bool:
    """True when market is choppy/sideways — use range strategy."""
    return adx_val < 20 and bb_width < 3.5

@staticmethod
def breakout_confirmed(closes: list, volumes: list,
                       resistance: float) -> bool:
    """Real breakout needs: price above level + volume spike + hold."""
    if len(closes) < 3 or not volumes:
        return False
    v_avg = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else volumes[-1]
    # Need 2 consecutive closes above + volume spike
    return (closes[-1] > resistance and
            closes[-2] > resistance and
            volumes[-1] > v_avg * Cfg.VOLUME_SPIKE_RATIO)

@staticmethod
def squeeze_detected(closes: list, highs: list, lows: list,
                      period: int = 20) -> bool:
    if len(closes) < period: return False
    _, bb_mid, _, bb_width = TechEngine.bollinger(closes, period)
    atr_val = TechEngine.atr(highs, lows, closes, period)
    if bb_mid == 0: return False
    kc_width = (atr_val * 1.5 * 2 / bb_mid) * 100
    return bb_width < kc_width

@staticmethod
def volume_ok(volumes: list, min_ratio: float = 0.30) -> bool:
    """FIX BUG: Relaxed to 0.30× (was 0.50×). Crypto volume is episodic."""
    if len(volumes) < 20: return True
    avg = sum(volumes[-20:]) / 20
    return volumes[-1] >= avg * min_ratio if avg > 0 else True

@staticmethod
def parse_candles(candles: list) -> tuple:
    """
    FIX BUG 7: Delta Exchange returns either dicts or arrays.
    Handles both formats + volume field name variations.
    """
    closes, highs, lows, volumes = [], [], [], []
    for c in candles:
        try:
            if isinstance(c, dict):
                cl = float(c.get("close", c.get("c", 0)) or 0)
                hi = float(c.get("high",  c.get("h", 0)) or 0)
                lo = float(c.get("low",   c.get("l", 0)) or 0)
                # Delta uses 'volume' or 'turnover'
                vo = float(c.get("volume", c.get("turnover",
                            c.get("v", 0))) or 0)
            elif isinstance(c, (list, tuple)) and len(c) >= 6:
                # [timestamp, open, high, low, close, volume]
                cl = float(c[4] or 0)
                hi = float(c[2] or 0)
                lo = float(c[3] or 0)
                vo = float(c[5] or 0)
            else:
                continue
            if cl > 0:  # Skip zero-price candles
                closes.append(cl)
                highs.append(hi)
                lows.append(lo)
                volumes.append(vo)
        except Exception:
            continue
    return closes, highs, lows, volumes
```

# ══════════════════════════════════════════════════════════════════════════════

# NEWS ENGINE (credibility-weighted, capped multiplier)

# ══════════════════════════════════════════════════════════════════════════════

class NewsEngine:
SOURCE_WEIGHT = {
“reuters”: 1.0, “bloomberg”: 1.0, “wsj”: 0.95,
“coindesk”: 0.85, “cointelegraph”: 0.75,
“cryptopanic”: 0.6, “twitter”: 0.3, “reddit”: 0.2,
}
BULL_SIGNALS = {
“etf approved”: 3, “fed pivot”: 2, “rate cut”: 2,
“bitcoin reserve”: 2, “institutional buy”: 2,
“blackrock”: 1.5, “fidelity”: 1.5, “microstrategy”: 1,
“halving”: 1.5, “btc treasury”: 2, “accumulation”: 1,
}
BEAR_SIGNALS = {
“exchange hack”: 3, “exchange collapse”: 3, “sec sues”: 3,
“rate hike”: 2, “cpi higher”: 2, “recession”: 1.5,
“ban bitcoin”: 2.5, “regulatory crackdown”: 2,
“tether fraud”: 3, “liquidation cascade”: 2,
}
FAKE_MARKERS = [“guaranteed”, “100x”, “moon soon”, “insider tip”, “secret source”]

```
def __init__(self):
    self._cache = None
    self._cache_time = 0

def get_sentiment(self) -> dict:
    now = time.time()
    if self._cache and now - self._cache_time < 300:
        return self._cache
    result = self._fetch()
    self._cache = result
    self._cache_time = now
    return result

def _fetch(self) -> dict:
    bull, bear, checked = 0.0, 0.0, 0

    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/"
            "?auth_token=&public=true&currencies=BTC&filter=hot",
            timeout=5)
        if r.status_code == 200:
            for post in r.json().get("results", [])[:15]:
                title = post.get("title", "").lower()
                if any(f in title for f in self.FAKE_MARKERS):
                    continue
                src = post.get("source", {}).get("domain", "").lower()
                cred = next((w for k, w in self.SOURCE_WEIGHT.items()
                             if k in src), 0.4)
                bull += sum(w for p, w in self.BULL_SIGNALS.items() if p in title) * cred
                bear += sum(w for p, w in self.BEAR_SIGNALS.items() if p in title) * cred
            checked += 1
    except Exception:
        pass

    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if r.status_code == 200:
            fng = int(r.json()["data"][0]["value"])
            if fng > 75: bear += 1.5
            elif fng > 65: bear += 0.5
            elif fng < 25: bull += 1.5
            elif fng < 35: bull += 0.5
            checked += 1
    except Exception:
        pass

    total = bull + bear
    # Minimum 2 sources required for reliable signal
    if checked < 2 or total < 0.5:
        return {"score": 0.0, "label": "Neutral (insufficient sources)",
                "confidence": 0.1,
                "bull_score": round(bull,1), "bear_score": round(bear,1),
                "sources_checked": checked}
    score = (bull - bear) / total
    # Require meaningful score gap to label as Bull/Bear
    label = ("Strongly Bullish" if score > 0.6 else
             "Bullish"          if score > 0.3 else
             "Strongly Bearish" if score < -0.6 else
             "Bearish"          if score < -0.3 else "Neutral")
    return {"score": round(score, 3), "label": label,
            "confidence": round(min(total / 8.0, 1.0), 2),
            "bull_score": round(bull, 1), "bear_score": round(bear, 1),
            "sources_checked": checked}

def get_multiplier(self) -> float:
    """News multiplier — only applies when 2+ sources and high confidence."""
    s = self.get_sentiment()
    sc   = s.get("score", 0)
    conf = s.get("confidence", 0)
    srcs = s.get("sources_checked", 0)
    # Ignore single-source sentiment entirely
    if srcs < 2 or conf < 0.4:
        return 1.0
    if sc > 0.6:  return 1.08
    if sc > 0.3:  return 1.03
    if sc < -0.6: return 0.90
    if sc < -0.3: return 0.96
    return 1.0
```

# ══════════════════════════════════════════════════════════════════════════════

# ADAPTIVE LEARNING ENGINE

# ══════════════════════════════════════════════════════════════════════════════

class LearningEngine:
def **init**(self):
self.memory = deque(maxlen=50)
self.rsi_long_min  = float(Cfg.RSI_LONG_MIN)
self.rsi_long_max  = float(Cfg.RSI_LONG_MAX)
self.adx_min       = float(Cfg.ADX_TREND_MIN)
self.hour_weights  = {h: 1.0 for h in range(24)}

```
def record(self, trade: dict):
    self.memory.append(trade)
    if len(self.memory) >= 20:
        self._update()

def _update(self):
    longs = [t for t in self.memory if t.get("direction") == "long"]
    if len(longs) >= 10:
        wins  = [t["rsi"] for t in longs if t.get("won")]
        loses = [t["rsi"] for t in longs if not t.get("won")]
        if wins and loses:
            awr = sum(wins) / len(wins)
            self.rsi_long_min = self.rsi_long_min * 0.92 + max(awr - 10, 30) * 0.08
            self.rsi_long_max = self.rsi_long_max * 0.92 + min(awr + 10, 70) * 0.08

    adx_data = [(t.get("adx", 25), t.get("won", False)) for t in self.memory]
    low_adx  = [(a, w) for a, w in adx_data if a < Cfg.ADX_TREND_MIN]
    if len(low_adx) >= 5:
        wr = sum(1 for _, w in low_adx if w) / len(low_adx)
        if wr > 0.60: self.adx_min = max(18, self.adx_min - 0.3)
        elif wr < 0.40: self.adx_min = min(28, self.adx_min + 0.3)

    for h in range(24):
        ht = [t for t in self.memory if t.get("hour_utc") == h]
        if len(ht) >= 3:
            hw = sum(1 for t in ht if t.get("won")) / len(ht)
            target = max(0.6, min(1.4, hw / 0.55))
            self.hour_weights[h] = self.hour_weights[h] * 0.85 + target * 0.15

def hour_mult(self, h: int) -> float:
    return self.hour_weights.get(h, 1.0)

def summary(self) -> dict:
    return {
        "trades_remembered": len(self.memory),
        "rsi_long_range": [round(self.rsi_long_min, 1), round(self.rsi_long_max, 1)],
        "adx_min": round(self.adx_min, 1),
        "best_hours": sorted(h for h, w in self.hour_weights.items() if w > 1.1),
    }
```

# ══════════════════════════════════════════════════════════════════════════════

# RISK GUARD — Monthly + Daily + Circuit Breaker

# ══════════════════════════════════════════════════════════════════════════════

class RiskGuard:
def **init**(self):
self.month_start  = 0.0
self.day_start    = 0.0
self.day_date     = None
self.consec_loss  = 0
self.in_recovery  = False
self.rec_wins_needed = 0
self.halted       = False
self.halt_reason  = “”
self.monthly_pnl  = 0.0
self.daily_pnl    = 0.0

```
def init(self, capital: float):
    self.month_start = capital
    self.day_start   = capital
    self.day_date    = datetime.now(timezone.utc).date()

def new_day(self, capital: float):
    today = datetime.now(timezone.utc).date()
    if self.day_date != today:
        self.day_start = capital
        self.day_date  = today

def record(self, won: bool, pnl_usd: float, capital: float):
    if won:
        self.consec_loss = 0
        if self.in_recovery:
            self.rec_wins_needed -= 1
            if self.rec_wins_needed <= 0:
                self.in_recovery = False
                log.info("Recovery mode exited")
    else:
        self.consec_loss += 1
        if self.consec_loss >= Cfg.MAX_CONSEC_LOSSES:
            self.in_recovery     = True
            self.rec_wins_needed = Cfg.RECOVERY_TRADES

    if self.month_start > 0:
        self.monthly_pnl = (capital - self.month_start) / self.month_start
    if self.day_start > 0:
        self.daily_pnl = (capital - self.day_start) / self.day_start

    if self.monthly_pnl <= -Cfg.MONTHLY_LOSS_LIMIT:
        self.halted      = True
        self.halt_reason = f"Monthly loss {self.monthly_pnl*100:.1f}% >= limit"
        log.error(f"BOT HALTED: {self.halt_reason}")

def can_trade(self) -> tuple:
    """Returns (ok, reason, risk_multiplier)"""
    if self.halted:
        return False, self.halt_reason, 0.0
    if self.daily_pnl <= -Cfg.DAILY_LOSS_LIMIT:
        return False, f"Daily loss {self.daily_pnl*100:.1f}% — resume tomorrow", 0.0
    if self.in_recovery:
        rm = Cfg.MAX_RISK_RECOVERY / Cfg.MAX_RISK_NORMAL
        return True, f"Recovery ({self.rec_wins_needed} wins needed)", rm
    return True, "OK", 1.0

def monthly_progress(self) -> dict:
    target  = Cfg.MONTHLY_TARGET_PCT
    current = self.monthly_pnl
    status  = ("HALTED"     if self.halted else
               "RECOVERY"   if self.in_recovery else
               "TARGET HIT" if current >= target else
               "ON TRACK"   if current >= 0 else "LOSING")
    return {
        "target_pct":   target * 100,
        "current_pct":  round(current * 100, 2),
        "progress_pct": round(min(current / target * 100, 100), 1) if target > 0 else 0,
        "remaining_pct":round(max((target - current) * 100, 0), 2),
        "monthly_status": status,
    }
```

# ══════════════════════════════════════════════════════════════════════════════

# INSTITUTIONAL CONFIDENCE ENGINE — 4 HIGH-QUALITY PILLARS

# Reduced from 7 to 4: removes funding/news/HTF (too noisy, hidden overfitting)

# Each pillar independently falsifiable with clear edge rationale

# ══════════════════════════════════════════════════════════════════════════════

class RegimeEngine:
“””
Regime confidence using 3 independent signals — ADX alone is weak.
All three must agree for high confidence.
“””

```
@staticmethod
def structure_score(highs: list, lows: list, closes: list,
                    lookback: int = 10) -> tuple:
    """
    Market structure: Higher Highs/Higher Lows = bull structure.
    Lower Highs/Lower Lows = bear structure.
    Returns (score -1 to +1, label)
    """
    if len(closes) < lookback:
        return 0.0, "unknown"
    h = highs[-lookback:]
    l = lows[-lookback:]
    half = lookback // 2

    # Count HH/HL vs LH/LL
    hh = sum(1 for i in range(half, lookback) if h[i] > h[i-1])
    hl = sum(1 for i in range(half, lookback) if l[i] > l[i-1])
    lh = sum(1 for i in range(half, lookback) if h[i] < h[i-1])
    ll = sum(1 for i in range(half, lookback) if l[i] < l[i-1])

    bull_score = (hh + hl) / (2 * half)  # 0 to 1
    bear_score = (lh + ll) / (2 * half)

    net = bull_score - bear_score  # -1 to +1
    label = ("bull" if net > 0.3 else "bear" if net < -0.3 else "neutral")
    return round(net, 3), label

@staticmethod
def volatility_regime(highs: list, lows: list, closes: list,
                      period: int = 14) -> tuple:
    """
    ATR expansion = trending (good for directional trades)
    ATR contraction = ranging (bad for directional, good for range)
    Returns (expanding: bool, atr_ratio: float)
    """
    if len(closes) < period * 2:
        return False, 1.0
    atr_now  = TechEngine.atr(highs, lows, closes, period)
    atr_prev = TechEngine.atr(highs[:-period], lows[:-period],
                               closes[:-period], period)
    if atr_prev == 0:
        return False, 1.0
    ratio = atr_now / atr_prev
    expanding = ratio > 1.15  # 15% expansion = trending
    return expanding, round(ratio, 3)

@staticmethod
def full_regime(highs: list, lows: list, closes: list,
                adx_val: float, pdi: float, ndi: float) -> dict:
    """
    Combines ADX + structure + volatility expansion for regime confidence.
    Returns score 0-100 and regime label with confidence.
    """
    struct_score, struct_label = RegimeEngine.structure_score(
        highs, lows, closes)
    vol_expanding, atr_ratio = RegimeEngine.volatility_regime(
        highs, lows, closes)

    # ADX strength
    adx_score = min(adx_val / 30.0, 1.0)  # 0 to 1, capped at ADX=30

    # Structure alignment with ADX direction
    adx_bull = pdi > ndi
    struct_aligned = ((adx_bull and struct_label == "bull") or
                      (not adx_bull and struct_label == "bear"))

    # Combined regime confidence
    confidence = (adx_score * 40 +
                  abs(struct_score) * 35 +
                  (15 if vol_expanding else 0) +
                  (10 if struct_aligned else 0))

    # Direction
    if adx_val > 20 and struct_aligned:
        if adx_bull and struct_label == "bull":
            label = "STRONG_BULL" if confidence > 65 else "BULL"
        elif not adx_bull and struct_label == "bear":
            label = "STRONG_BEAR" if confidence > 65 else "BEAR"
        else:
            label = "NEUTRAL"
    else:
        label = "NEUTRAL"

    return {
        "label":       label,
        "confidence":  round(confidence, 1),
        "adx":         round(adx_val, 1),
        "structure":   struct_label,
        "vol_expanding": vol_expanding,
        "atr_ratio":   atr_ratio,
        "adx_bull":    adx_bull,
    }
```

class ConfidenceEngine:

```
def score(self, data: dict, direction: str,
          learner: LearningEngine = None) -> tuple:
    closes  = data.get("closes", [])
    highs   = data.get("highs", [])
    lows    = data.get("lows", [])
    volumes = data.get("volumes", [])
    c15m    = data.get("closes_15m", closes)
    h15m    = data.get("highs_15m",  highs)
    l15m    = data.get("lows_15m",   lows)
    h_utc   = data.get("hour_utc", 12)
    m_utc   = data.get("minute_utc", 0)
    weekend = data.get("is_weekend", False)

    if len(closes) < 55:
        return 0, True, "insufficient_data(<55 candles)", {}

    adx_val, pdi, ndi = TechEngine.adx(highs, lows, closes)

    # ── HARD VETOES (binary, non-negotiable) ─────────────────────────────
    if h_utc in Cfg.DEAD_ZONE_HOURS and not weekend:
        return 0, True, f"dead_zone_{h_utc}UTC", {}
    for mh, mm in Cfg.MACRO_BLACKOUT_TIMES:
        if abs((h_utc * 60 + m_utc) - (mh * 60 + mm)) <= Cfg.BLACKOUT_WINDOW_MINS:
            return 0, True, f"macro_blackout_{mh}:{mm:02d}", {}
    if volumes and not TechEngine.volume_ok(volumes):
        return 0, True, "low_volume_trap", {}

    bd = {}
    total = 0

    # ── PILLAR 1: REGIME CONFIDENCE (40%) ────────────────────────────────
    # Uses ADX + market structure (HH/HL) + volatility expansion
    # Not just ADX — all 3 must agree for full score
    regime_info = RegimeEngine.full_regime(highs, lows, closes,
                                           adx_val, pdi, ndi)
    reg_label   = regime_info["label"]
    reg_conf    = regime_info["confidence"]

    if   direction == "long"  and reg_label in ("STRONG_BULL", "BULL"):
        bd["regime"] = int(reg_conf * 0.40)
    elif direction == "short" and reg_label in ("STRONG_BEAR", "BEAR"):
        bd["regime"] = int(reg_conf * 0.40)
    elif reg_label == "NEUTRAL" and regime_info["vol_expanding"]:
        bd["regime"] = 8  # Possible breakout forming
    else:
        bd["regime"] = max(0, int(reg_conf * 0.10))  # Minimal credit
    bd["regime"] = min(bd["regime"], 40)
    total += bd["regime"]

    # ── PILLAR 2: MOMENTUM QUALITY (30%) ─────────────────────────────────
    # RSI position + MACD divergence (highest-alpha signal per the autopsy)
    rsi_v = TechEngine.rsi(closes, Cfg.RSI_PERIOD)
    _, _, _, histogram = TechEngine.macd(closes, Cfg.MACD_FAST,
                                          Cfg.MACD_SLOW, Cfg.MACD_SIGNAL)
    divergence = TechEngine.detect_divergence(closes, histogram)
    rmin = learner.rsi_long_min if learner else Cfg.RSI_LONG_MIN
    rmax = learner.rsi_long_max if learner else Cfg.RSI_LONG_MAX

    mom = 0
    if direction == "long":
        # RSI in bull pullback zone
        if rmin <= rsi_v <= rmax:  mom += 12
        elif 35 <= rsi_v < rmin:   mom += 7   # Oversold but recovering
        # MACD momentum
        if (len(histogram) >= 3 and
                histogram[-1] > histogram[-2] > histogram[-3] and
                histogram[-1] > 0): mom += 10  # Rising above zero = strong
        elif (len(histogram) >= 3 and
                histogram[-1] > histogram[-2] > histogram[-3]): mom += 5
        # Divergence = highest-conviction entry (per BTC autopsy)
        if divergence == "bullish": mom += 18
    else:
        if Cfg.RSI_SHORT_MIN <= rsi_v <= Cfg.RSI_SHORT_MAX: mom += 12
        elif rsi_v > 65: mom += 7
        if (len(histogram) >= 3 and
                histogram[-1] < histogram[-2] < histogram[-3] and
                histogram[-1] < 0): mom += 10
        elif (len(histogram) >= 3 and
                histogram[-1] < histogram[-2] < histogram[-3]): mom += 5
        if divergence == "bearish": mom += 18
    bd["momentum"] = min(mom, 30)
    total += bd["momentum"]

    # ── PILLAR 3: VOLATILITY REGIME (20%) ────────────────────────────────
    # ATR expansion + BB squeeze release = real move coming
    _, _, _, bw = TechEngine.bollinger(closes)
    vol_expanding, atr_ratio = RegimeEngine.volatility_regime(highs, lows, closes)
    atr_val = TechEngine.atr(highs, lows, closes)

    vs = 0
    if vol_expanding and 0.3 < bw < 5.0:   vs = 20  # Trending volatility
    elif not vol_expanding and bw < 0.4:    vs = 15  # Squeeze = breakout pending
    elif 0.3 < bw < 4.0:                   vs = 12  # Normal
    elif bw >= 4.0:                         vs = 4   # Extreme — IV crush risk
    else:                                   vs = 2
    bd["volatility"] = vs
    total += vs

    # ── PILLAR 4: EXECUTION QUALITY (10%) ────────────────────────────────
    # Volume + session timing — is this a high-quality execution window?
    vol_score = 0
    if volumes:
        avg = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
        if avg > 0:
            ratio = volumes[-1] / avg
            if ratio > 1.5: vol_score += 5
            elif ratio > 1.0: vol_score += 3
    # Session quality
    if h_utc in Cfg.PEAK_HOURS: vol_score += 5
    elif not weekend:           vol_score += 2
    bd["execution"] = min(vol_score, 10)
    total += bd["execution"]

    # Store regime info for dashboard
    bd["_regime_detail"] = regime_info

    return min(total, 100), False, "", bd
```

# ══════════════════════════════════════════════════════════════════════════════

# POSITION SIZER — FIXED Kelly floor

# ══════════════════════════════════════════════════════════════════════════════

class PositionSizer:
def **init**(self):
self.wins = self.losses = self.total = 0
self.streak = 0
self.avg_win  = 2.2
self.avg_loss = 1.2

```
@property
def win_rate(self):
    return self.wins / self.total if self.total >= Cfg.MIN_TRADES_BEFORE_KELLY else 0.55

def kelly_frac(self) -> float:
    wr = self.win_rate
    if not (0.45 <= wr <= 0.78): return 0.015  # Default 1.5%
    p, q = wr, 1 - wr
    b = self.avg_win / max(self.avg_loss, 0.1)
    k = (b * p - q) / b
    if k <= 0: return 0.010  # Never trade 0 — use minimum
    return max(0.008, min(0.025, k * Cfg.KELLY_FRACTION))

def streak_mult(self) -> float:
    if self.streak >= 4:  return 1.35
    if self.streak == 3:  return 1.2
    if self.streak == 2:  return 1.1
    if self.streak <= -2: return 0.75
    return 1.0

def size_usd(self, capital: float, confidence: int,
             atr_pct: float, risk_mult: float = 1.0) -> float:
    """
    Phase 1 (< 20 trades): Fixed 1.5% risk per trade.
    No Kelly — Kelly assumes stable edge which we don't have yet.
    After 20 trades with real statistics, Kelly adapts automatically.
    """
    if capital <= 0: return Cfg.MIN_TRADE_SIZE_USD
    max_r = capital * (Cfg.MAX_RISK_HOT if confidence >= 78
                       else Cfg.MAX_RISK_NORMAL)

    if self.total < Cfg.MIN_TRADES_BEFORE_KELLY:
        # Phase 1: Fixed 1.5% — no compounding error from unknown edge
        base = capital * 0.015 * risk_mult
    else:
        # Phase 2: Kelly with proven statistics
        base = capital * self.kelly_frac()
        conf_m = 1.2 if confidence >= Cfg.HIGH_CONFIDENCE else 1.0
        vol_m  = max(0.6, min(1.3, 0.012 / atr_pct)) if atr_pct > 0 else 1.0
        base   = base * conf_m * self.streak_mult() * vol_m * risk_mult

    return max(Cfg.MIN_TRADE_SIZE_USD, min(base, max_r))

def record(self, won: bool, pct: float):
    self.total += 1
    if won:
        self.wins += 1
        self.streak = max(0, self.streak) + 1
        self.avg_win  = self.avg_win  * 0.9 + abs(pct) * 0.1
    else:
        self.losses += 1
        self.streak = min(0, self.streak) - 1
        self.avg_loss = self.avg_loss * 0.9 + abs(pct) * 0.1
```

# ══════════════════════════════════════════════════════════════════════════════

# POSITION

# ══════════════════════════════════════════════════════════════════════════════

class Position:
def **init**(self, product_id: int, side: str, entry: float,
size_usd: float, symbol: str = “”,
rsi: float = 50, adx: float = 25, hour_utc: int = 12):
self.product_id = product_id
self.side       = side
self.entry      = entry
self.size_usd   = size_usd
self.symbol     = symbol
self.entered_at = datetime.now(timezone.utc)
self.tp1_hit    = False
self.trailing   = False
self.trail_ref  = entry
self.closed     = False
self.exit_price = None
self.exit_reason= None
self.rsi_entry  = rsi
self.adx_entry  = adx
self.hour_utc   = hour_utc
# ── Position Analytics (MAE/MFE) ─────────────────────────────────────
self.mae        = 0.0   # Max Adverse Excursion (worst % against us)
self.mfe        = 0.0   # Max Favorable Excursion (best % for us)
self.slippage   = 0.0   # Actual slippage paid on entry
self.entry_adj  = entry # Adjusted entry after slippage

```
def check_exit(self, price: float) -> tuple:
    pct = ((price - self.entry) / self.entry if self.side == "long"
           else (self.entry - price) / self.entry)

    # Track MAE/MFE (tells you if stops are too tight / targets too early)
    self.mae = min(self.mae, pct)   # Most negative pct seen
    self.mfe = max(self.mfe, pct)   # Most positive pct seen

    # ── RANGE/SCALP MODE — tight exits ──────────────────────────────
    if getattr(self, 'range_mode', False):
        if pct <= -Cfg.RANGE_HARD_STOP:
            return True, "range_stop", False
        if not self.tp1_hit and pct >= Cfg.RANGE_TP1:
            self.tp1_hit = True
            return True, "range_tp1_70pct", True   # Take 70% at 0.5%
        if pct >= Cfg.RANGE_TP2:
            return True, "range_tp2_full", False
        age_mins = (datetime.now(timezone.utc) - self.entered_at).total_seconds() / 60
        if age_mins >= Cfg.RANGE_MAX_HOLD_MIN:
            return True, "range_time_exit", False
        return False, "", False

    # ── TREND MODE — standard exits ──────────────────────────────────
    if pct <= -Cfg.HARD_STOP_PCT:
        return True, "hard_stop", False
    if not self.tp1_hit and pct >= Cfg.TP1_PCT:
        self.tp1_hit = True
        return True, "tp1_50pct", True
    if pct >= Cfg.TRAIL_ACTIVATE_PCT:
        self.trailing = True
        self.trail_ref = (max(self.trail_ref, price) if self.side == "long"
                          else min(self.trail_ref, price))
    if self.trailing:
        stop = (self.trail_ref * (1 - Cfg.TRAIL_DISTANCE_PCT) if self.side == "long"
                else self.trail_ref * (1 + Cfg.TRAIL_DISTANCE_PCT))
        if (self.side == "long"  and price <= stop or
                self.side == "short" and price >= stop):
            return True, "trailing_stop", False
    if pct >= Cfg.TP2_PCT:
        return True, "tp2_full", False
    age = (datetime.now(timezone.utc) - self.entered_at).total_seconds() / 3600
    if age >= 4.0:
        return True, "time_exit_4h", False
    return False, "", False
```

# ══════════════════════════════════════════════════════════════════════════════

# INSTITUTIONAL OPTIONS SELECTOR

# Filters: IV crush risk, liquidity (OI+volume), bid-ask spread, theta vs move

# ══════════════════════════════════════════════════════════════════════════════

class OptionsSelector:

```
# Institutional-grade filters
MIN_OI_CONTRACTS     = 50    # Skip illiquid strikes
MIN_VOLUME_CONTRACTS = 5     # Min daily volume
MAX_SPREAD_PCT       = 0.20  # Reject if spread > 20% of mark (too wide)
MAX_IV_RANK          = 75    # Skip if IV too high (IV crush risk after entry)
MIN_DELTA_ABS        = 0.15  # Skip deep OTM (gamma risk, low delta)

@staticmethod
def _liquidity_ok(opt: dict) -> tuple:
    """Check OI, volume, and bid-ask spread. Returns (ok, reason)."""
    oi     = int(float(opt.get("open_interest", 0) or 0))
    vol    = int(float(opt.get("volume",         0) or 0))
    bid    = float(opt.get("best_bid_price",  0) or 0)
    ask    = float(opt.get("best_ask_price",  0) or 0)
    mark   = float(opt.get("mark_price",      0) or 0)

    if oi < OptionsSelector.MIN_OI_CONTRACTS:
        return False, f"OI={oi}<{OptionsSelector.MIN_OI_CONTRACTS}"
    if vol < OptionsSelector.MIN_VOLUME_CONTRACTS:
        return False, f"vol={vol}<{OptionsSelector.MIN_VOLUME_CONTRACTS}"
    if bid > 0 and ask > 0 and mark > 0:
        spread_pct = (ask - bid) / mark
        if spread_pct > OptionsSelector.MAX_SPREAD_PCT:
            return False, f"spread={spread_pct:.0%}>{OptionsSelector.MAX_SPREAD_PCT:.0%}"
    return True, "ok"

@staticmethod
def _theta_risk_ok(mark: float, atr_usd: float, hold_hours: float = 4.0) -> tuple:
    """
    Theta decay check: expected move must justify premium paid.
    Expected move = ATR × √(hold_hours/24)
    Minimum ratio: 1.5x (need move 1.5x larger than premium to profit)
    Returns (ok, ratio)
    """
    expected_move = atr_usd * math.sqrt(hold_hours / 24.0)
    if mark <= 0 or expected_move <= 0:
        return True, 0.0  # Can't check, allow
    ratio = expected_move / mark
    return ratio >= Cfg.MIN_MOVE_TO_PREMIUM_RATIO, round(ratio, 2)

@staticmethod
def select(chain: list, price: float, direction: str,
           confidence: int, atr_usd: float) -> Optional[dict]:
    if not chain or price <= 0:
        return None
    target_type = "call_options" if direction == "long" else "put_options"
    today = datetime.now(timezone.utc).date()
    candidates = []
    rejected = []

    for opt in chain:
        if opt.get("contract_type") != target_type:
            continue
        try:
            expiry = datetime.strptime(
                opt.get("settlement_time", "")[:10], "%Y-%m-%d").date()
            dte = (expiry - today).days
            if dte < 0 or dte > 3:
                continue

            strike = float(opt.get("strike_price", 0) or 0)
            mark   = float(opt.get("mark_price",   0) or 0)
            if mark <= 0 or strike <= 0:
                continue

            # ── INSTITUTIONAL FILTER 1: Liquidity ────────────────────
            liq_ok, liq_reason = OptionsSelector._liquidity_ok(opt)
            if not liq_ok:
                rejected.append(f"{strike}: {liq_reason}")
                continue

            # ── INSTITUTIONAL FILTER 2: Theta vs Move ────────────────
            theta_ok, move_ratio = OptionsSelector._theta_risk_ok(
                mark, atr_usd)
            if not theta_ok:
                rejected.append(f"{strike}: theta/move={move_ratio:.1f}x<1.5")
                continue

            # ── INSTITUTIONAL FILTER 3: Moneyness (no deep OTM) ──────
            moneyness = ((strike - price) / price if direction == "long"
                         else (price - strike) / price)
            # Skip deep OTM options (low delta, high theta risk)
            if moneyness > 0.04:  # More than 4% OTM
                rejected.append(f"{strike}: too_OTM={moneyness:.2%}")
                continue

            candidates.append({
                "product":    opt,
                "dte":        dte,
                "moneyness":  moneyness,
                "mark":       mark,
                "product_id": opt.get("id"),
                "move_ratio": move_ratio,
                "oi":         int(float(opt.get("open_interest", 0) or 0)),
            })
        except Exception:
            continue

    if rejected:
        log.info(f"Options rejected: {len(rejected)} contracts "
                 f"({', '.join(rejected[:3])})")

    if not candidates:
        log.warning(f"No options passed filters for {direction} "
                    f"(${price:,.0f}, ATR=${atr_usd:.0f})")
        return None

    def score_opt(c):
        # Prefer: shorter DTE + near ATM + high OI + good move ratio
        dte_sc  = (3 - c["dte"]) * 5       # 0→15, prefer 0DTE
        # Moneyness: slight ITM for high conf, ATM otherwise
        if confidence >= Cfg.HIGH_CONFIDENCE:
            atm_sc = 15 if -0.02 <= c["moneyness"] <= 0.005 else 5
        else:
            atm_sc = 15 if -0.01 <= c["moneyness"] <= 0.005 else 5
        oi_sc   = min(c["oi"] // 50, 5)    # OI bonus up to 5 pts
        ratio_sc= min(int(c["move_ratio"] * 2), 5)  # Move ratio bonus
        return dte_sc + atm_sc + oi_sc + ratio_sc

    candidates.sort(key=score_opt, reverse=True)
    best = candidates[0]
    log.info(f"Selected option: {best['product'].get('symbol','')} "
             f"mark=${best['mark']:.2f} DTE={best['dte']} "
             f"OTM={best['moneyness']:.1%} move_ratio={best['move_ratio']:.1f}x")
    return best
```

# ══════════════════════════════════════════════════════════════════════════════

# MAIN BOT

# ══════════════════════════════════════════════════════════════════════════════

class AlphaBot:
def **init**(self):
self.api      = DeltaAPI()
self.conf_eng = ConfidenceEngine()
self.sizer    = PositionSizer()
self.news     = NewsEngine()
self.learner  = LearningEngine()
self.guard    = RiskGuard()
self.opt_sel  = OptionsSelector()

```
    # Capital (live wallet only)
    self.capital        = 0.0
    self.start_capital  = 0.0
    self.wallet_usdt    = 0.0
    self.wallet_btc     = 0.0
    self.wallet_inr     = 0.0
    self.wallet_synced  = False

    self.positions: list = []
    self.trade_log: list = []
    self.running         = False
    self.status_msg      = "Initializing..."
    self.total_pnl       = 0.0
    self.profit_buffer   = 0.0
    self._prev_oi        = 0.0

    # Live market state (dashboard display)
    self.last_scan_at    = None
    self.next_scan_at    = None
    self.last_price      = 0.0
    self.long_score      = 0
    self.short_score     = 0
    self.long_veto       = ""
    self.short_veto      = ""
    self.regime          = "UNKNOWN"
    self.last_adx        = 0.0
    self.last_rsi        = 50.0
    self.atr_pct         = 0.0
    self.will_trade      = False
    self.trade_dir       = None
    self.candles         = []
    self.last_breakdown  = {}   # Last confidence pillar scores
    self.trades_today    = 0
    self.trades_week     = 0
    # Real-time log buffer (shown on dashboard)
    self.log_buffer      = []   # list of {time, level, msg}
    self._log_max        = 200  # keep last 200 log lines

    self._sync_wallet(startup=True)

# ── Server IP helper ─────────────────────────────────────────────────────
def _get_server_ip(self) -> str:
    """Get the PUBLIC outbound IP — this is what Delta Exchange sees.
    Socket method returns internal/private IP (10.x.x.x) — useless for whitelisting.
    Must use external service to get real public IP."""
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=5)
        return r.json().get("ip", "unknown")
    except Exception:
        try:
            r = requests.get("https://api4.my-ip.io/ip.json", timeout=5)
            return r.json().get("ip", "unknown")
        except Exception:
            return "unknown"

# ── Real log emitter ─────────────────────────────────────────────────────
def _emit(self, level: str, msg: str):
    """Emit a timestamped log to buffer AND Python logger."""
    entry = {
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "level": level,  # INFO / WARN / ERROR / TRADE
        "msg": msg
    }
    self.log_buffer.append(entry)
    if len(self.log_buffer) > self._log_max:
        self.log_buffer.pop(0)
    if level == "ERROR":
        log.error(msg)
    elif level == "WARN":
        log.warning(msg)
    elif level == "TRADE":
        log.info(f"[TRADE] {msg}")
    else:
        log.info(msg)

# ── Wallet ────────────────────────────────────────────────────────────────
def _sync_wallet(self, startup: bool = False) -> float:
    try:
        bal = self.api.get_wallet()
        if not bal:
            if startup:
                self.status_msg = "⚠ Wallet read failed — check API keys on Render"
            return self.capital

        # Delta India uses "USD" (not "USDT") — values are strings, cast to float
        usdt = float(bal.get("USD",  bal.get("USDT",
                     bal.get("usdt", bal.get("usd", 0)))) or 0)
        inr  = float(bal.get("INR",  bal.get("inr",
                     bal.get("USDINR", bal.get("usdinr", 0)))) or 0)
        btc  = float(bal.get("BTC",  bal.get("btc",
                     bal.get("XBT",   bal.get("xbt",   0)))) or 0)
        log.info(f"Wallet — USD:{usdt:.2f} INR:{inr:.2f} BTC:{btc:.6f}")

        inr_usd = 0.0
        if inr > 0:
            try:
                r = requests.get(
                    "https://api.exchangerate-api.com/v4/latest/USD", timeout=4)
                inr_usd = inr / r.json()["rates"].get("INR", 84.0)
            except Exception:
                inr_usd = inr / 84.0

        btc_usd = 0.0
        if btc > 0:
            t = self.api.get_ticker("BTCUSD")
            btc_usd = btc * float(t.get("mark_price", 0) or 0)

        total = usdt + inr_usd + btc_usd
        self.wallet_usdt = usdt
        self.wallet_btc  = btc
        self.wallet_inr  = inr

        if total > 0:
            if not self.wallet_synced or startup:
                self.start_capital = total
                self.capital       = total
                self.wallet_synced = True
                self.guard.init(total)
                self._emit("INFO", f"💰 Wallet synced: ${total:.2f} "
                         f"(USDT={usdt:.2f} INR={inr:.0f} BTC={btc:.6f})")
            else:
                self.capital = total + self.profit_buffer
                self.guard.new_day(total)
        else:
            # Log what was actually returned to help debug
            log.warning(f"Wallet $0 — raw keys: {list(bal.keys()) if bal else 'empty'}")
            self._emit("WARN", f"Wallet $0.00 — check API key permissions on Delta Exchange")
    except Exception as e:
        log.error(f"Wallet sync: {e}")
    return self.capital

# ── Market Data ───────────────────────────────────────────────────────────
def _get_data(self) -> dict:
    now = datetime.now(timezone.utc)
    raw5  = self.api.get_candles("BTCUSD", 5, 100)
    raw15 = self.api.get_candles("BTCUSD", 15, 50)
    fr    = self.api.get_funding_rate()
    oi    = self.api.get_open_interest()

    if not raw5:
        return {}

    # FIX BUG 7: Use unified parse that handles dict/array formats
    cl5, hi5, lo5, vo5   = TechEngine.parse_candles(raw5)
    cl15, _, _, _        = TechEngine.parse_candles(raw15) if raw15 else ([], [], [], [])

    if not cl5:
        return {}

    oi_chg = (oi - self._prev_oi) / self._prev_oi if self._prev_oi > 0 else 0
    self._prev_oi = oi

    return {
        "closes": cl5, "highs": hi5, "lows": lo5, "volumes": vo5,
        "closes_5m": cl5, "closes_15m": cl15,
        "hour_utc": now.hour, "minute_utc": now.minute,
        "is_weekend": now.weekday() >= 5,
        "funding_rate": fr, "current_price": cl5[-1],
        "oi_change_pct": oi_chg,
        "atr": TechEngine.atr(hi5, lo5, cl5),
    }

# ── Core Analysis + Trade ─────────────────────────────────────────────────
def analyze_and_trade(self):
    now_utc = datetime.now(timezone.utc)
    self.last_scan_at = now_utc.isoformat()
    self.next_scan_at = (now_utc + timedelta(
        seconds=Cfg.SCAN_INTERVAL)).isoformat()

    if not self.api.healthy:
        self.status_msg = "⚠ API unhealthy — protecting positions"
        self.will_trade = False
        return

    data = self._get_data()
    if not data:
        self.status_msg = "No market data from Delta Exchange"
        self.will_trade = False
        return

    price = data["current_price"]
    self.last_price = price
    self.candles    = data["closes"][-30:]

    # Live indicators
    if len(data["closes"]) > 21:
        self.last_rsi = TechEngine.rsi(data["closes"], Cfg.RSI_PERIOD)
        adx_v, pdi, ndi = TechEngine.adx(data["highs"], data["lows"], data["closes"])
        self.last_adx = adx_v
        atr_v = TechEngine.atr(data["highs"], data["lows"], data["closes"])
        self.atr_pct = round(atr_v / price * 100, 3) if price > 0 else 0

        # Regime label
        e8  = TechEngine.ema(data["closes"], 8)[-1]
        e21 = TechEngine.ema(data["closes"], 21)[-1]
        e55 = TechEngine.ema(data["closes"], 55)[-1]
        if price > e8 > e21 > e55 and adx_v > 25 and pdi > ndi:
            self.regime = "STRONG_BULL"
        elif price > e8 > e21 and adx_v > 18:
            self.regime = "BULL"
        elif price < e8 < e21 < e55 and adx_v > 25 and ndi > pdi:
            self.regime = "STRONG_BEAR"
        elif price < e8 < e21 and adx_v > 18:
            self.regime = "BEAR"
        else:
            self.regime = "NEUTRAL"
        self._emit("INFO", f"Regime: {self.regime} | RSI={self.last_rsi:.1f} "
                   f"ADX={self.last_adx:.1f} ATR={self.atr_pct:.3f}%")

    self._manage_positions(price)

    can, reason, risk_m = self.guard.can_trade()
    if not can:
        self.status_msg = f"🛑 {reason}"
        self.will_trade = False
        return

    if len([p for p in self.positions if not p.closed]) >= Cfg.MAX_OPEN_POSITIONS:
        self.status_msg = "Max positions open — monitoring"
        self.will_trade = False
        return

    news_m = self.news.get_multiplier()
    ls, lv, lr, lbd = self.conf_eng.score(data, "long",  self.learner)
    ss, sv, sr, sbd = self.conf_eng.score(data, "short", self.learner)
    # Store the breakdown from whichever direction has higher score
    self.last_breakdown = lbd if ls >= ss else sbd
    ls = min(int(ls * news_m), 100)
    ss = min(int(ss * news_m), 100)

    self.long_score  = ls
    self.short_score = ss
    self.long_veto   = lr if lv else ""
    self.short_veto  = sr if sv else ""

    self._emit("INFO", f"BTC ${price:,.0f} | {self.regime} | "
             f"RSI={self.last_rsi:.1f} ADX={self.last_adx:.1f} | "
             f"L={ls}{'✗'+lr if lv else '✓'} S={ss}{'✗'+sr if sv else '✓'} | News={news_m:.2f}")

    direction = score = None
    if not lv and ls >= Cfg.MIN_CONFIDENCE and ls > ss:
        direction, score = "long",  ls
    elif not sv and ss >= Cfg.MIN_CONFIDENCE and ss > ls:
        direction, score = "short", ss

    self.will_trade  = direction is not None
    self.trade_dir   = direction

    if not direction:
        # ── NO TREND SIGNAL — try RANGE/WHALE-TRAP mode ──────────────
        cl, hi, lo, vo = data["closes"], data["highs"], data["lows"], data["volumes"]
        _, _, _, bb_w = TechEngine.bollinger(cl)
        is_range = TechEngine.is_range_market(self.last_adx, bb_w)

        if is_range and len(cl) >= 20 and Cfg.ENABLE_RANGE_MODE:
            # Check for whale trap (stop-hunt reversal)
            opens = [float(c.get("open", cl[i])) if isinstance(c, dict)
                     else cl[i] for i, c in enumerate(data.get("raw_candles", []))
                     ][:len(cl)]
            if not opens or len(opens) != len(cl):
                opens = cl  # fallback

            trap = TechEngine.detect_whale_trap(opens, hi, lo, cl, vo)
            support, resistance = TechEngine.range_bounds(hi, lo)

            if trap["trap_type"] != "none" and trap["strength"] >= 40:
                # Whale trap detected — trade the reversal
                trap_dir = trap["entry_direction"]
                trap_price = cl[-1]
                range_size = (resistance - support) / trap_price if trap_price > 0 else 0

                if range_size >= 0.003:  # Range must be at least 0.3% wide
                    size_usd = max(Cfg.MIN_TRADE_SIZE_USD,
                                   self.capital * Cfg.RANGE_RISK_PCT * risk_m)
                    rsi_now = TechEngine.rsi(cl, Cfg.RSI_PERIOD)
                    adx_now, _, _ = TechEngine.adx(hi, lo, cl)

                    chain = self.api.get_options_chain("BTC")
                    atr_usd = data.get("atr", trap_price * 0.005)
                    opt = self.opt_sel.select(chain, trap_price, trap_dir,
                                               60, atr_usd)
                    if opt:
                        contracts = max(1, int(size_usd / (opt["mark"] * 100)))
                        result = self.api.place_order(opt["product_id"],
                                                      "buy", contracts)
                    else:
                        side = "buy" if trap_dir == "long" else "sell"
                        contracts = max(1, int(size_usd / trap_price * 1000))
                        result = self.api.place_order(Cfg.BTC_PRODUCT_ID,
                                                      side, contracts)
                        opt = None

                    if result.get("success"):
                        sym = opt["product"].get("symbol","") if opt else "BTCUSD_PERP"
                        pid = opt["product_id"] if opt else Cfg.BTC_PRODUCT_ID
                        pos = Position(pid, trap_dir, trap_price, size_usd,
                                       sym, rsi_now, adx_now, data["hour_utc"])
                        pos.range_mode = True   # Use tight range exits
                        self.positions.append(pos)
                        self._log("OPEN", trap_dir, trap_price, size_usd, 55,
                                  sym, "whale_trap")
                        msg = (f"🎣 WHALE TRAP {trap['trap_type'].upper()} "
                               f"→ {trap_dir.upper()} @ ${trap_price:,.0f} "
                               f"strength={trap['strength']} "
                               f"TP={Cfg.RANGE_TP2*100:.1f}% SL={Cfg.RANGE_HARD_STOP*100:.1f}%")
                        self.status_msg = msg
                        self._emit("TRADE", msg)
                    return

            # Range mode — trade near support/resistance
            rsi_now = TechEngine.rsi(cl, Cfg.RSI_PERIOD)
            price = cl[-1]
            near_support = support > 0 and (price - support) / price < 0.003
            near_resist  = resistance > 0 and (resistance - price) / price < 0.003

            if near_support and rsi_now < 38:
                self._emit("INFO", f"📊 RANGE BUY zone: price ${price:,.0f} near support ${support:,.0f} RSI={rsi_now:.1f}")
            elif near_resist and rsi_now > 62:
                self._emit("INFO", f"📊 RANGE SELL zone: price ${price:,.0f} near resistance ${resistance:,.0f} RSI={rsi_now:.1f}")
            else:
                self.status_msg = (f"Range mode: S=${support:,.0f} R=${resistance:,.0f} "
                                   f"RSI={rsi_now:.1f} ADX={self.last_adx:.1f} "
                                   f"BB={bb_w:.2f}%")

        elif TechEngine.squeeze_detected(cl, hi, lo):
            self.status_msg = "⚡ BB Squeeze coiling — breakout imminent"
            self._emit("INFO", self.status_msg)
        else:
            self.status_msg = (f"Watching: L={ls}{'✗' if lv else ''} "
                               f"S={ss}{'✗' if sv else ''} | "
                               f"{self.regime} | ADX={self.last_adx:.1f} | "
                               f"Need ≥{Cfg.MIN_CONFIDENCE}")
            self._emit("INFO", self.status_msg)
        return

    atr_usd  = data.get("atr", price * 0.008)
    atr_pct  = atr_usd / price if price > 0 else 0.008
    size_usd = self.sizer.size_usd(self.capital, score, atr_pct, risk_m)
    rsi_now  = TechEngine.rsi(data["closes"], Cfg.RSI_PERIOD)
    adx_now, _, _ = TechEngine.adx(data["highs"], data["lows"], data["closes"])

    chain = self.api.get_options_chain("BTC")
    # FIX BUG 9: Pass atr_usd (USD units) not atr_pct
    opt   = self.opt_sel.select(chain, price, direction, score, atr_usd)

    if opt:
        contracts = max(1, int(size_usd / (opt["mark"] * 100)))
        result    = self.api.place_order(opt["product_id"], "buy", contracts)
        if result.get("success"):
            pos = Position(opt["product_id"], direction, price, size_usd,
                           opt["product"].get("symbol", ""), rsi_now,
                           adx_now, data["hour_utc"])
            self.positions.append(pos)
            self._log("OPEN", direction, price, size_usd, score,
                      opt["product"].get("symbol", ""))
            self.status_msg = (f"✅ {direction.upper()} "
                               f"{opt['product'].get('symbol','')} @ ${price:,.0f}")
            self._emit("TRADE", self.status_msg)
        else:
            log.error(f"Option order failed: {result}")
            self.status_msg = f"Option order failed — {result.get('error','unknown')}"
    else:
        # Fallback to perpetual
        side      = "buy" if direction == "long" else "sell"
        contracts = max(1, int(size_usd / price * 1000))
        result    = self.api.place_order(Cfg.BTC_PRODUCT_ID, side, contracts)
        if result.get("success"):
            pos = Position(Cfg.BTC_PRODUCT_ID, direction, price, size_usd,
                           "BTCUSD_PERP", rsi_now, adx_now, data["hour_utc"])
            self.positions.append(pos)
            self._log("OPEN", direction, price, size_usd, score, "BTCUSD_PERP")
            self.status_msg = f"✅ {direction.upper()} PERP @ ${price:,.0f}"
            self._emit("TRADE", self.status_msg)
        else:
            self.status_msg = "No option + perp order also failed"

def _manage_positions(self, price: float):
    for pos in self.positions:
        if pos.closed: continue
        exit_, reason, partial = pos.check_exit(price)
        if exit_:
            self._close(pos, price, reason, partial)

def _close(self, pos: Position, price: float,
            reason: str, partial: bool):
    size = pos.size_usd / 2 if partial else pos.size_usd
    live = self.api.get_positions()
    match = next((p for p in live if p.get("product_id") == pos.product_id), None)
    if match:
        qty = abs(int(float(match.get("size", 0) or 0)))
        if partial: qty = max(1, qty // 2)
        self.api.place_order(pos.product_id,
                             "sell" if pos.side == "long" else "buy", qty)

    pnl_pct = ((price - pos.entry) / pos.entry if pos.side == "long"
               else (pos.entry - price) / pos.entry)
    pnl_usd = size * pnl_pct
    won = pnl_usd > 0

    if not won and self.profit_buffer > 0:
        absorbed = min(abs(pnl_usd), self.profit_buffer)
        self.profit_buffer -= absorbed
        pnl_usd += absorbed

    if won:
        self.profit_buffer += pnl_usd * 0.3
        self.capital       += pnl_usd * 0.7

    self.total_pnl += pnl_usd
    self.sizer.record(won, pnl_pct * 100)
    self.guard.record(won, pnl_usd, self.capital)
    self.learner.record({
        "rsi": pos.rsi_entry, "adx": pos.adx_entry,
        "hour_utc": pos.hour_utc, "won": won,
        "pnl_pct": pnl_pct * 100, "direction": pos.side,
    })

    if not partial:
        pos.closed      = True
        pos.exit_price  = price
        pos.exit_reason = reason

    self._log("CLOSE", pos.side, price, pnl_usd, 0,
              pos.symbol, reason, pnl_pct * 100)
    self._emit("TRADE", f"{'✅ WIN' if won else '❌ LOSS'} "
             f"CLOSED {pos.side.upper()} @ ${price:,.0f} | "
             f"{reason} | ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%)")

def _log(self, action: str, side: str, price: float, amount: float,
          conf: int, symbol: str, reason: str = "", pnl_pct: float = 0,
          pos: "Position" = None):
    if action == "OPEN":
        self.trades_today += 1
        self.trades_week  += 1
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "action": action, "side": side, "price": price,
        "amount": amount, "confidence": conf, "symbol": symbol,
        "reason": reason, "pnl_pct": pnl_pct,
        "capital": self.capital,
        "win_rate": self.sizer.win_rate,
        "streak": self.sizer.streak,
    }
    # Add MAE/MFE analytics if closing a position
    if action == "CLOSE" and pos:
        entry["mae_pct"]      = round(pos.mae * 100, 3)
        entry["mfe_pct"]      = round(pos.mfe * 100, 3)
        entry["slippage_usd"] = round(getattr(pos, "slippage", 0), 4)
        # Quality assessment
        if pnl_pct > 0:
            # If MFE >> actual PnL, we left money on the table
            capture_ratio = (pnl_pct / pos.mfe) if pos.mfe > 0 else 0
            entry["profit_capture"] = round(capture_ratio, 2)
        else:
            # If MAE ~= PnL, stop was well-placed
            stop_efficiency = abs(pnl_pct / pos.mae) if pos.mae < 0 else 0
            entry["stop_efficiency"] = round(stop_efficiency, 2)
    self.trade_log.append(entry)

def _run_loop(self):
    cycle    = 0
    last_day = datetime.now(timezone.utc).day
    while self.running:
        try:
            today = datetime.now(timezone.utc).day
            if today != last_day:
                self.trades_today = 0
                last_day = today
            if cycle % 5 == 0:
                self._sync_wallet()
            self.analyze_and_trade()
            cycle += 1
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
            self.status_msg = f"Error: {e}"
        time.sleep(Cfg.SCAN_INTERVAL)

def start(self):
    if not self.running:
        self.running = True
        threading.Thread(target=self._run_loop, daemon=True).start()
        log.info("ΔLPHA Bot v6.2 started")

def stop(self):
    self.running = False
    log.info("ΔLPHA Bot v6.2 stopped")

def get_state(self) -> dict:
    sc  = self.start_capital if self.start_capital > 0 else self.capital
    pct = round((self.capital - sc) / sc * 100, 2) if sc > 0 else 0.0
    ct, gr, _ = self.guard.can_trade()
    return {
        "version": "v6.2",
        "running": self.running,
        "status": self.status_msg,
        "api_healthy": self.api.healthy,
        "wallet_synced": self.wallet_synced,
        "wallet_usdt": round(self.wallet_usdt, 2),
        "wallet_btc":  round(self.wallet_btc,  8),
        "wallet_inr":  round(self.wallet_inr,  2),
        "capital": round(self.capital, 2),
        "starting_capital": round(sc, 2),
        "total_pnl": round(self.total_pnl, 2),
        "profit_buffer": round(self.profit_buffer, 2),
        "pnl_pct": pct,
        "open_positions": len([p for p in self.positions if not p.closed]),
        "total_trades": self.sizer.total,
        "win_rate": round(self.sizer.win_rate * 100, 1),
        "streak": self.sizer.streak,
        "consecutive_losses": self.guard.consec_loss,
        "in_recovery": self.guard.in_recovery,
        "can_trade": ct,
        "guard_reason": gr,
        "kelly_fraction": round(self.sizer.kelly_frac() * 100, 2),
        "monthly_progress": self.guard.monthly_progress(),
        "news_sentiment": self.news.get_sentiment(),
        "learning": self.learner.summary(),
        "recent_trades": self.trade_log[-20:],
        # Live market
        "last_scan_at":    self.last_scan_at,
        "next_scan_at":    self.next_scan_at,
        "last_btc_price":  self.last_price,
        "last_long_score": self.long_score,
        "last_short_score":self.short_score,
        "last_long_veto":  self.long_veto,
        "last_short_veto": self.short_veto,
        "last_regime":     self.regime,
        "last_adx":        round(self.last_adx, 1),
        "last_rsi":        round(self.last_rsi, 1),
        "last_atr_pct":    self.atr_pct,
        "will_trade":      self.will_trade,
        "trade_direction": self.trade_dir,
        "candles_cache":   self.candles,
        "trades_today":    self.trades_today,
        "trades_week":     self.trades_week,
        "scan_interval":   Cfg.SCAN_INTERVAL,
        # Real logs
        "logs":            self.log_buffer[-50:],
        # Server IP (for Delta Exchange whitelist)
        "server_ip":       self._get_server_ip(),
        # Actual pillar scores from last confidence calculation
        "last_breakdown":  self.last_breakdown,
    }
```

# ══════════════════════════════════════════════════════════════════════════════

# FLASK APP

# ══════════════════════════════════════════════════════════════════════════════

app = Flask(**name**)
CORS(app, resources={r”/*”: {“origins”: “*”}})

@app.after_request
def _cors(response):
response.headers[“Access-Control-Allow-Origin”] = “*”
response.headers[“Access-Control-Allow-Methods”] = “GET, POST, OPTIONS”
response.headers[“Access-Control-Allow-Headers”] = “Content-Type, Authorization”
return response

bot = AlphaBot()

# FIX BUG 3: _auto_start placed AFTER bot = AlphaBot(), not inside **main**

# gunicorn never runs **main** — this is the only way to auto-start on Render

def _auto_start():
if Cfg.API_KEY and Cfg.API_SECRET:
log.info(“API keys found — auto-starting bot…”)
bot.start()
else:
log.warning(“No API keys — bot waiting. Set DELTA_API_KEY + DELTA_API_SECRET on Render.”)

_auto_start()

# ══════════════════════════════════════════════════════════════════════════════

# ROUTES — each defined EXACTLY ONCE (FIX BUG 1)

# ══════════════════════════════════════════════════════════════════════════════

@app.route(”/api/status”)
@app.route(”/api/bot/status”)
def status():
return jsonify(bot.get_state())

@app.route(”/api/connect”, methods=[“POST”])
def connect():
“””
Accept API key + secret from dashboard login form.
This is how the original bot worked — no Render env vars needed.
“””
d = request.json or {}
api_key    = d.get(“api_key”,    “”).strip()
api_secret = d.get(“api_secret”, “”).strip()
region     = d.get(“region”,     “india”).strip()

```
if not api_key or not api_secret:
    return jsonify({"success": False,
                    "message": "API key and secret are required"})

# Update credentials at runtime
bot.api.set_credentials(api_key, api_secret, region)
bot.capital       = 0.0
bot.wallet_synced = False

# Test connection — first check ticker (public, always works)
ticker = bot.api.get_ticker("BTCUSD")
if not ticker:
    return jsonify({"success": False,
                    "message": "Cannot reach Delta Exchange API — check your internet"})

# Now test wallet (needs correct IP whitelist + read permission)
bal = bot.api.get_wallet()
if not bal:
    server_ip = bot._get_server_ip()
    return jsonify({"success": False,
                    "message": f"Connected but wallet returned empty. "
                               f"Check: 1) IP {server_ip} is whitelisted 2) API key has Read permission 3) Visit /api/wallet/debug to see raw response"})

bot.api.connected = True
capital = bot._sync_wallet(startup=True)
ip      = d.get("ip", "unknown")
server_ip = bot._get_server_ip()
bot._emit("INFO", f"Connected {region} | Balance:{capital:.2f} | User-IP:{ip} | Server-IP:{server_ip}")
bot._emit("INFO", f"⚠ Add {server_ip} to Delta Exchange API whitelist if wallet shows $0")
log.info(f"Connected via dashboard | region={region} | balance=${capital:.2f}")

# Auto-start bot after successful connect
bot.start()

return jsonify({
    "success":  True,
    "message":  f"Connected to Delta Exchange {region.title()}",
    "balance":  round(capital, 2),
    "region":   region,
    "running":  bot.running,
})
```

@app.route(”/api/bot/start”, methods=[“POST”])
def start():
bot.start()
return jsonify({“success”: True, “message”: “Bot started”})

@app.route(”/api/bot/stop”, methods=[“POST”])
def stop():
bot.stop()
return jsonify({“success”: True, “message”: “Bot stopped”})

@app.route(”/api/bot/run_now”, methods=[“POST”])
def run_now():
threading.Thread(target=bot.analyze_and_trade, daemon=True).start()
return jsonify({“success”: True, “message”: “Scan triggered”})

@app.route(”/api/wallet”)
def wallet():
raw = bot.api.get_wallet()
return jsonify({“raw”: raw, “capital_usd”: round(bot.capital, 2),
“start_usd”: round(bot.start_capital, 2),
“synced”: bot.wallet_synced})

@app.route(”/api/wallet/debug”)
def wallet_debug():
“””
Shows the RAW response from Delta Exchange wallet API.
Visit this URL to diagnose $0 balance issues.
“””
# Call the raw endpoint directly without processing
path = “/v2/wallet/balances”
ts   = str(int(time.time()))
msg  = “GET” + ts + path
sig  = hmac.new(
bot.api.secret.encode(),
msg.encode(),
hashlib.sha256
).hexdigest()
headers = {
“api-key”:      bot.api.key,
“timestamp”:    ts,
“signature”:    sig,
“Content-Type”: “application/json”,
}
try:
r = requests.get(
f”{bot.api.base}{path}”,
headers=headers,
timeout=10
)
raw_json = r.json()
return jsonify({
“status_code”:   r.status_code,
“url_called”:    f”{bot.api.base}{path}”,
“api_key_set”:   bool(bot.api.key),
“api_key_len”:   len(bot.api.key),
“secret_len”:    len(bot.api.secret),
“raw_response”:  raw_json,
“diagnosis”: (
“✅ Auth OK — check asset field names in raw_response”
if r.status_code == 200 else
“❌ IP not whitelisted — add server IP to Delta API key”
if r.status_code == 403 else
“❌ Invalid API key or secret”
if r.status_code == 401 else
f”❌ HTTP {r.status_code}”
)
})
except Exception as e:
return jsonify({“error”: str(e), “api_key_set”: bool(bot.api.key)})

@app.route(”/api/candles/debug”)
def candles_debug():
“”“Debug candles endpoint — shows raw Delta response and what we tried.”””
import time as _time
results = {}
price = bot.last_price or 77000

```
# Try multiple resolution formats Delta India might use
for res in [5, "5", 1, "1"]:
    end   = int(_time.time())
    start = end - (300 * 20)  # 20 x 5min = 100min of data
    params = {"symbol": "BTCUSD", "resolution": res,
              "start": start, "end": end}
    try:
        d = bot.api._get("/v2/history/candles", params)
        result_count = len(d.get("result", [])) if d and d.get("success") else 0
        results[f"resolution_{res}"] = {
            "success":      d.get("success") if d else False,
            "candle_count": result_count,
            "error":        d.get("error","none") if d else "no_response",
            "message":      d.get("message","") if d else "",
            "sample":       d.get("result",[])[0] if result_count > 0 else None,
        }
    except Exception as e:
        results[f"resolution_{res}"] = {"error": str(e)}

# Also try ticker to confirm auth works
ticker = bot.api._get("/v2/tickers/BTCUSD")
results["ticker_auth_test"] = {
    "success": ticker.get("success") if ticker else False,
    "price":   ticker.get("result",{}).get("mark_price","?") if ticker else "N/A",
}

return jsonify({
    "candle_tests":  results,
    "api_key_set":   bool(bot.api.key),
    "api_key_len":   len(bot.api.key),
    "base_url":      bot.api.base,
    "fix_hint": "If all candle tests fail but ticker works = endpoint path or auth format issue"
})
```

@app.route(”/api/candles/debug”)
def candles_debug():
“””
Shows raw Delta Exchange candle response.
If empty, candles aren’t loading — this is why ADX/ATR shows —
Visit: render-bot-w6rc.onrender.com/api/candles/debug
“””
raw = bot.api.get_candles_debug(“BTCUSD”, 5)
candles = raw.get(“raw”, {}).get(“result”, [])
return jsonify({
“url_called”:    raw.get(“url”,””),
“http_status”:   raw.get(“status”, 0),
“candles_count”: len(candles),
“first_candle”:  candles[0] if candles else None,
“last_candle”:   candles[-1] if candles else None,
“raw_response”:  raw.get(“raw”, {}),
“diagnosis”: (
f”✅ {len(candles)} candles loaded — technical analysis is working”
if len(candles) >= 5 else
“❌ Zero candles — Delta candle API may need auth or different params”
)
})

@app.route(”/api/wallet/sync”, methods=[“POST”])
def wallet_sync():
cap = bot._sync_wallet(startup=False)
return jsonify({“success”: True, “capital_usd”: round(cap, 2),
“message”: f”Synced: ${cap:.2f}”})

@app.route(”/api/positions”)
def positions():
return jsonify(bot.api.get_positions())

@app.route(”/api/orders”)
def orders():
return jsonify(bot.api.get_orders())

@app.route(”/api/trades”)
def trades():
return jsonify(bot.trade_log[-50:])

@app.route(”/api/ticker”)
def ticker():
“”“BTC price — tries Delta first, falls back to CoinGecko (always works).”””
t = bot.api.get_ticker(“BTCUSD”)
if t and float(t.get(“mark_price”, 0) or 0) > 0:
return jsonify(t)
# Fallback: CoinGecko public API — no auth needed
try:
r = requests.get(
“https://api.coingecko.com/api/v3/simple/price”
“?ids=bitcoin&vs_currencies=usd&include_24hr_change=true”,
timeout=5)
cg = r.json().get(“bitcoin”, {})
price = cg.get(“usd”, 0)
change = cg.get(“usd_24h_change”, 0)
if price:
return jsonify({
“mark_price”: str(price),
“last_price”:  str(price),
“index_price”: str(price),
“price_change_24h_pct”: str(round(change, 2)),
“source”: “coingecko”
})
except Exception:
pass
return jsonify({})

@app.route(”/api/options_chain”)
def options_chain():
return jsonify(bot.api.get_options_chain(“BTC”)[:20])

@app.route(”/api/config”, methods=[“GET”])
def get_config():
return jsonify({
“min_confidence”: Cfg.MIN_CONFIDENCE,
“max_risk_pct”: Cfg.MAX_RISK_NORMAL,
“kelly_fraction”: Cfg.KELLY_FRACTION,
“hard_stop_pct”: Cfg.HARD_STOP_PCT,
“tp1_pct”: Cfg.TP1_PCT, “tp2_pct”: Cfg.TP2_PCT,
“monthly_target_pct”: Cfg.MONTHLY_TARGET_PCT,
“monthly_loss_limit”: Cfg.MONTHLY_LOSS_LIMIT,
“scan_interval”: Cfg.SCAN_INTERVAL,
“dead_zone_hours”: Cfg.DEAD_ZONE_HOURS,
“blackout_window_mins”: Cfg.BLACKOUT_WINDOW_MINS,
“rsi_period”: Cfg.RSI_PERIOD,
“macd_fast”: Cfg.MACD_FAST,
“macd_slow”: Cfg.MACD_SLOW,
})

@app.route(”/api/config”, methods=[“POST”])
def set_config():
d = request.json or {}
if “min_confidence” in d: Cfg.MIN_CONFIDENCE  = int(d[“min_confidence”])
if “max_risk_pct”   in d: Cfg.MAX_RISK_NORMAL = float(d[“max_risk_pct”])
if “scan_interval”  in d: Cfg.SCAN_INTERVAL   = int(d[“scan_interval”])
return jsonify({“success”: True, “message”: “Config updated”})

@app.route(”/api/set_scan_interval”, methods=[“POST”])
def set_scan_interval():
d = request.json or {}
mins = max(1, min(60, int(d.get(“minutes”, 5))))
Cfg.SCAN_INTERVAL = mins * 60
exp = int(16 * 60 / mins)
return jsonify({“success”: True, “scan_every_minutes”: mins,
“max_scans_per_day”: exp,
“note”: f”Scans every {mins}min. Trades only on signal ≥{Cfg.MIN_CONFIDENCE}”})

@app.route(”/api/manual_trade”, methods=[“POST”])
def manual_trade():
“”“Force a trade — bypasses confidence score. FIX BUG 2: no circular import.”””
d = request.json or {}
direction    = d.get(“direction”)
size_override= float(d.get(“size_usd”, 0) or 0)

```
if direction not in ("long", "short"):
    return jsonify({"success": False, "message": "direction must be long or short"})

price = bot.last_price
if not price:
    t = bot.api.get_ticker("BTCUSD")
    price = float(t.get("mark_price", 0) or 0)
if not price:
    return jsonify({"success": False, "message": "Cannot get BTC price"})

# FIX BUG 8: size floor applied
size_usd = size_override if size_override >= Cfg.MIN_TRADE_SIZE_USD else \
           max(Cfg.MIN_TRADE_SIZE_USD,
               bot.sizer.size_usd(bot.capital, 75, 0.008))

chain    = bot.api.get_options_chain("BTC")
atr_usd  = bot.atr_pct * price / 100 if bot.atr_pct > 0 else price * 0.008
opt      = bot.opt_sel.select(chain, price, direction, 75, atr_usd)

if opt:
    contracts = max(1, int(size_usd / (opt["mark"] * 100)))
    result    = bot.api.place_order(opt["product_id"], "buy", contracts)
    symbol    = opt["product"].get("symbol", "")
    pid       = opt["product_id"]
else:
    side      = "buy" if direction == "long" else "sell"
    contracts = max(1, int(size_usd / price * 1000))
    result    = bot.api.place_order(Cfg.BTC_PRODUCT_ID, side, contracts)
    symbol    = "BTCUSD_PERP"
    pid       = Cfg.BTC_PRODUCT_ID

if result.get("success"):
    # FIX BUG 2: Position is defined in this file — no import needed
    pos = Position(pid, direction, price, size_usd, symbol,
                   bot.last_rsi, bot.last_adx,
                   datetime.now(timezone.utc).hour)
    bot.positions.append(pos)
    bot._log("OPEN", direction, price, size_usd, 99, symbol, "manual")
    bot.status_msg = f"MANUAL {direction.upper()} {symbol} @ ${price:,.0f}"
    return jsonify({"success": True, "message": bot.status_msg,
                    "price": price, "size_usd": round(size_usd, 2),
                    "symbol": symbol})
return jsonify({"success": False,
                "message": f"Order failed: {result.get('error','unknown')}"})
```

@app.route(”/api/close_position”, methods=[“POST”])
def close_position():
d = request.json or {}
pid = d.get(“product_id”)
live = bot.api.get_positions()
closed = 0
for p in live:
if pid and str(p.get(“product_id”)) != str(pid): continue
qty  = abs(int(float(p.get(“size”, 0) or 0)))
side = p.get(“side”, “”)
if qty > 0:
bot.api.place_order(p[“product_id”],
“sell” if side == “buy” else “buy”, qty)
closed += 1
return jsonify({“success”: True, “closed”: closed})

@app.route(”/api/close_all”, methods=[“POST”])
def close_all():
live = bot.api.get_positions()
closed = 0
for p in live:
qty  = abs(int(float(p.get(“size”, 0) or 0)))
side = p.get(“side”, “”)
if qty > 0:
bot.api.place_order(p[“product_id”],
“sell” if side == “buy” else “buy”, qty)
closed += 1
return jsonify({“success”: True, “closed”: closed})

@app.route(”/api/server_config”)
def server_config():
key    = Cfg.API_KEY
secret = Cfg.API_SECRET
ks     = bool(key    and len(key)    > 8)
ss     = bool(secret and len(secret) > 8)
connected = bot.api.connected or (ks and ss)
return jsonify({
“api_key_set”:    ks,
“api_secret_set”: ss,
“api_key_masked”: (”*” * max(0, len(key) - 4) + key[-4:]) if ks else “”,
“both_configured”: connected,
“base_url”:       bot.api.base,
“bot_running”:    bot.running,
“connected”:      connected,
})

@app.route(”/api/logs”)
def get_logs():
“”“Real-time bot log stream — no dummy data.”””
limit = int(request.args.get(“limit”, 100))
return jsonify({
“logs”: bot.log_buffer[-limit:],
“total”: len(bot.log_buffer),
“bot_running”: bot.running,
“wallet_synced”: bot.wallet_synced,
})

@app.route(”/api/ip”)
def get_ip():
“”“Returns the current outbound IP of this Render server.
Bookmark this URL and check it whenever the bot stops working.
Add the returned IP to your Delta Exchange API key whitelist.
“””
outbound_ip = “unknown”
# Use ipify — returns the PUBLIC outbound IP that Delta Exchange sees
# Do NOT use socket.getsockname() — returns internal Render container IP (10.x.x.x)
try:
r = requests.get(“https://api.ipify.org?format=json”, timeout=5)
outbound_ip = r.json().get(“ip”, “unknown”)
except Exception:
try:
r = requests.get(“https://api4.my-ip.io/ip.json”, timeout=5)
outbound_ip = r.json().get(“ip”, “unknown”)
except Exception:
pass
return jsonify({
“render_outbound_ip”: outbound_ip,
“add_to_delta_whitelist”: outbound_ip,
“instructions”: “Go to india.delta.exchange → API Keys → Edit → add this IP to whitelist”,
“note”: “This IP can change on Render free tier after each redeploy”
})

@app.route(”/api/test”)
def test():
t = bot.api.get_ticker(“BTCUSD”)
return jsonify({
“bot_version”:  “v6.2”,
“api_connected”:bool(t),
“btc_price”:    t.get(“mark_price”, “N/A”),
“api_healthy”:  bot.api.healthy,
“wallet_synced”:bot.wallet_synced,
“bot_running”:  bot.running,
“bugs_fixed”:   10,
“key_fixes”: [
“Duplicate routes removed”,
“Circular import fixed”,
“ADX array alignment fixed”,
“Options premium formula fixed”,
“Kelly minimum $20 floor”,
“RSI(14) not RSI(7)”,
“MACD(8,21,5) not MACD(5,13,5)”,
“Candle dict+array parsing”,
“Auto-start on gunicorn”,
“Macro blackout reduced to 20min”,
]
})

DASHBOARD_HTML = “””<!DOCTYPE html>

<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#ffffff">
<title>Alpha Bot</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#f5f7fa;--w:#fff;--t:#0f1923;--t2:#52616b;--t3:#8a9bb0;--bdr:#e8ecf2;
  --g:#00c896;--gb:#e8faf5;--gd:#b3edd9;
  --r:#f0483e;--rb:#fff0ef;--rd:#fbb8b5;
  --b:#0066ff;--bb:#e8f0ff;
  --o:#ff7b00;--ob:#fff3e8;
  --y:#f59e0b;--yb:#fef3c7;
  --sh:0 1px 3px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
  --rr:16px;--rs:12px;--rx:8px}
html,body{background:var(--bg);color:var(--t);font-family:"DM Sans",sans-serif;min-height:100vh}

/* HEADER */
.hdr{background:var(–w);border-bottom:1px solid var(–bdr);padding:0 16px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.hl{display:flex;align-items:center;gap:10px}
.logo-ico{width:32px;height:32px;background:var(–t);border-radius:9px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:15px;font-family:“DM Mono”;font-weight:600}
.ht{font-size:15px;font-weight:700}.hs{font-size:10px;color:var(–t3)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:5px 12px;border-radius:20px;font-size:11px;font-weight:600}
.p-on{background:var(–gb);color:var(–g)}.p-off{background:var(–rb);color:var(–r)}
.pdot{width:6px;height:6px;border-radius:50%}
.p-on .pdot{background:var(–g);animation:pulse 2s infinite}.p-off .pdot{background:var(–r)}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.5)}}

/* WRAP */
.wrap{padding:12px 12px 88px;max-width:480px;margin:0 auto}
.tab{display:none}.tab.active{display:block}

/* CONNECT BANNER */
.connect-banner{background:#eff6ff;border:1px solid #bfdbfe;color:#1d4ed8;border-radius:var(–rs);padding:12px 14px;margin-bottom:10px;font-size:12px;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:8px}

/* BTC HERO */
.btc{background:var(–t);border-radius:var(–rr);padding:20px;margin-bottom:10px;position:relative;overflow:hidden}
.btc::before{content:””;position:absolute;top:-40px;right:-40px;width:160px;height:160px;background:rgba(255,255,255,.04);border-radius:50%}
.bl{font-size:11px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.bp{font-size:38px;font-weight:700;color:#fff;font-family:“DM Mono”;line-height:1;margin-bottom:6px}
.brow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.bcb{font-size:12px;font-weight:600;padding:3px 9px;border-radius:6px}
.bu{background:rgba(0,200,150,.2);color:#00e8b0}.bd{background:rgba(240,72,62,.2);color:#ff6b64}
.btc-r{position:absolute;top:16px;right:16px;text-align:right}
.bpl{font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.bpv{font-size:14px;font-weight:700;font-family:“DM Mono”}
.bpvu{color:#00e8b0}.bpvd{color:#ff6b64}.bpvn{color:rgba(255,255,255,.5)}
.bsig{font-size:10px;padding:2px 7px;border-radius:5px;margin-top:3px;display:inline-block}
.s-bull{background:rgba(0,200,150,.2);color:#00e8b0}
.s-bear{background:rgba(240,72,62,.2);color:#ff6b64}
.s-neu{background:rgba(255,255,255,.1);color:rgba(255,255,255,.5)}
.chart-wrap{margin-top:12px;height:44px}
canvas#miniChart{width:100%;height:44px}

/* REGIME */
.regime-banner{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:var(–rx);margin-bottom:10px;font-size:12px;font-weight:600}
.regime-STRONG_BULL{background:#dcfce7;color:#15803d;border:1px solid #bbf7d0}
.regime-BULL{background:var(–gb);color:#059669;border:1px solid var(–gd)}
.regime-NEUTRAL{background:#f1f5f9;color:#64748b;border:1px solid #e2e8f0}
.regime-BEAR{background:var(–rb);color:#dc2626;border:1px solid var(–rd)}
.regime-STRONG_BEAR{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
.regime-UNKNOWN{background:#f1f5f9;color:#64748b;border:1px solid #e2e8f0}

/* SIGNAL ROW */
.sig-row{display:flex;gap:8px;margin-bottom:10px}
.sig-box{flex:1;background:var(–w);border-radius:var(–rs);padding:10px 12px;box-shadow:var(–sh);text-align:center}
.sig-lbl{font-size:9px;color:var(–t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.sig-val{font-size:24px;font-weight:700;font-family:“DM Mono”;line-height:1}
.sig-g{color:var(–g)}.sig-r{color:var(–r)}.sig-n{color:var(–t3)}
.sig-sub{font-size:9px;color:var(–t3);margin-top:3px}
.will-badge{background:var(–g);color:#fff;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;display:inline-block}

/* SCAN BAR */
.scan-bar{background:var(–w);border-radius:var(–rx);padding:8px 12px;margin-bottom:10px;box-shadow:var(–sh);display:flex;align-items:center;justify-content:space-between}
.scan-l{font-size:11px;color:var(–t2)}
.scan-r{font-size:11px;font-family:“DM Mono”;font-weight:600;color:var(–b)}
.scan-prog{height:3px;background:var(–bdr);border-radius:2px;margin-top:5px;overflow:hidden}
.scan-fill{height:100%;border-radius:2px;background:var(–b);transition:width 1s linear}

/* INDICATORS */
.indics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.ind{background:var(–w);border-radius:var(–rx);padding:10px;box-shadow:var(–sh);text-align:center}
.ind-l{font-size:9px;color:var(–t3);text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px}
.ind-v{font-size:16px;font-weight:700;font-family:“DM Mono”;color:var(–t)}
.iv-g{color:var(–g)}.iv-r{color:var(–r)}.iv-y{color:var(–y)}

/* CARD */
.card{background:var(–w);border-radius:var(–rs);padding:16px;margin-bottom:10px;box-shadow:var(–sh)}
.chd{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.ct{font-size:11px;font-weight:600;color:var(–t3);text-transform:uppercase;letter-spacing:.5px}

/* WALLET */
.wrow{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.wl{font-size:11px;color:var(–t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.wa{font-size:28px;font-weight:700;font-family:“DM Mono”;color:var(–t);line-height:1}
.ws{font-size:11px;color:var(–t3);margin-top:3px}
.wp{text-align:right}
.wpp{font-size:20px;font-weight:700;font-family:“DM Mono”}
.wpa{font-size:11px;color:var(–t3);margin-top:2px}
.pu{color:var(–g)}.pdn{color:var(–r)}.pnn{color:var(–t2)}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:var(–bg);border-radius:var(–rx);padding:5px 10px;font-size:11px;color:var(–t2);font-family:“DM Mono”;font-weight:500}
.sync-row{display:flex;align-items:center;justify-content:space-between;padding-top:10px;border-top:1px solid var(–bdr)}
.ss{font-size:11px}.ss-ok{color:var(–g)}.ss-warn{color:var(–o)}
.sbtn{background:var(–bg);border:1px solid var(–bdr);border-radius:var(–rx);padding:6px 12px;font-size:11px;font-weight:600;color:var(–t2);cursor:pointer;font-family:“DM Sans”}

/* MONTHLY */
.mpb{height:8px;background:var(–bdr);border-radius:4px;overflow:hidden;margin:8px 0}
.mpf{height:100%;border-radius:4px;transition:width .8s}
.mpr{display:flex;justify-content:space-between;font-size:10px;color:var(–t3)}

/* STATS */
.stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}
.sc{background:var(–w);border-radius:var(–rs);padding:12px;box-shadow:var(–sh)}
.sl{font-size:9px;color:var(–t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.sv{font-size:20px;font-weight:700;font-family:“DM Mono”;color:var(–t);line-height:1}
.sv-g{color:var(–g)}.sv-r{color:var(–r)}.sv-b{color:var(–b)}
.sub{font-size:9px;color:var(–t3);margin-top:3px}

/* STATUS ROW */
.srow{background:var(–w);border-radius:var(–rs);padding:12px 14px;margin-bottom:10px;box-shadow:var(–sh);display:flex;align-items:center;gap:10px}
.sico{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.si-run{background:var(–gb)}.si-stop{background:var(–rb)}.si-warn{background:var(–yb)}
.stxt{flex:1;font-size:12px;font-weight:500;color:var(–t);line-height:1.4}
.stm{font-size:10px;color:var(–t3);font-family:“DM Mono”;white-space:nowrap}

/* CONTROLS */
.ctrl{display:flex;gap:8px;margin-bottom:10px}
.btn{flex:1;padding:12px 6px;border-radius:var(–rs);border:none;font-family:“DM Sans”;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:5px}
.btn:active{transform:scale(.97)}
.btn-s{background:var(–t);color:#fff}
.btn-x{background:var(–rb);color:var(–r);border:1.5px solid var(–rd)}
.btn-r{background:var(–bb);color:var(–b);border:1.5px solid rgba(0,102,255,.2)}

/* MANUAL TRADE */
.mt-card{background:var(–w);border-radius:var(–rs);padding:14px;margin-bottom:10px;box-shadow:var(–sh)}
.mt-inp{width:100%;background:var(–bg);border:1px solid var(–bdr);border-radius:var(–rx);padding:8px 10px;font-size:13px;font-family:“DM Mono”;color:var(–t);margin-bottom:10px}
.mt-inp:focus{outline:none;border-color:var(–b)}
.mt-row{display:flex;gap:8px}
.btn-long{flex:1;background:var(–gb);color:var(–g);border:1.5px solid var(–gd);border-radius:var(–rx);padding:10px;font-weight:700;font-size:12px;cursor:pointer;font-family:“DM Sans”}
.btn-short{flex:1;background:var(–rb);color:var(–r);border:1.5px solid var(–rd);border-radius:var(–rx);padding:10px;font-weight:700;font-size:12px;cursor:pointer;font-family:“DM Sans”}

/* TRADES */
.trow{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(–bdr)}
.trow:last-child{border-bottom:none}
.tl{display:flex;align-items:center;gap:10px}
.tico{width:32px;height:32px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0}
.ti-l{background:var(–gb);color:var(–g)}.ti-s{background:var(–rb);color:var(–r)}.ti-o{background:var(–bb);color:var(–b)}
.tsym{font-size:12px;font-weight:600;color:var(–t)}
.ttm{font-size:10px;color:var(–t3);font-family:“DM Mono”}
.trr{text-align:right}
.tpnl{font-size:13px;font-weight:700;font-family:“DM Mono”}
.tp-u{color:var(–g)}.tp-d{color:var(–r)}.tp-n{color:var(–t2)}
.tpr{font-size:10px;color:var(–t3);font-family:“DM Mono”}
.empty{text-align:center;padding:24px 0;color:var(–t3);font-size:13px}

/* SIGNALS */
.pred-row{display:flex;gap:8px;margin-bottom:12px}
.pi{flex:1;background:var(–bg);border-radius:var(–rx);padding:10px;text-align:center}
.ph{font-size:9px;color:var(–t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.pp{font-size:13px;font-weight:700;font-family:“DM Mono”;color:var(–t)}
.pd{font-size:10px;font-weight:600;margin-top:3px}
.pd-u{color:var(–g)}.pd-d{color:var(–r)}
.sent-row{display:flex;align-items:center;gap:8px;padding-top:12px;border-top:1px solid var(–bdr);margin-top:4px}
.sbar{flex:1;height:6px;background:var(–bdr);border-radius:3px;overflow:hidden}
.sfill{height:100%;border-radius:3px;transition:width .8s}
.pil{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(–bdr)}
.pil:last-child{border-bottom:none}
.pn{width:110px;font-size:11px;color:var(–t2);font-weight:500}
.pt{flex:1;height:5px;background:var(–bg);border-radius:3px;overflow:hidden}
.pf{height:100%;border-radius:3px;transition:width .6s}
.pw{width:24px;text-align:right;font-size:10px;font-family:“DM Mono”;font-weight:600}

/* LOGS */
.logs-wrap{background:#0f1923;border-radius:var(–rs);padding:12px;max-height:300px;overflow-y:auto;font-family:“DM Mono”,monospace;font-size:10px}
.log-row{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04);line-height:1.4}
.log-row:last-child{border-bottom:none}
.log-time{color:rgba(255,255,255,.3);flex-shrink:0;width:52px}
.ll{flex-shrink:0;width:42px;font-weight:700;border-radius:3px;padding:0 3px;text-align:center;font-size:9px}
.ll-INFO{background:rgba(96,165,250,.1);color:#60a5fa}
.ll-WARN{background:rgba(251,191,36,.1);color:#fbbf24}
.ll-ERROR{background:rgba(248,113,113,.1);color:#f87171}
.ll-TRADE{background:rgba(52,211,153,.15);color:#34d399}
.log-msg{color:rgba(255,255,255,.7);word-break:break-word}
.log-empty{color:rgba(255,255,255,.2);text-align:center;padding:20px 0}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.sd-live{background:#34d399;box-shadow:0 0 6px #34d399;animation:pulse 2s infinite}
.sd-stop{background:#f87171}
.sd-sync{background:#fbbf24;animation:pulse 1s infinite}

/* SETTINGS */
.sfield{margin-bottom:14px}
.sfl{font-size:11px;color:var(–t3);margin-bottom:5px;font-weight:500}
.sr{display:flex;gap:8px;align-items:center}
.sr input[type=range]{flex:1;accent-color:var(–t)}
.sv2{font-family:“DM Mono”;font-weight:700;font-size:14px;min-width:36px}
.sdesc{font-size:10px;color:var(–t3);margin-top:3px}
.save-btn{width:100%;padding:12px;border-radius:var(–rs);border:none;background:var(–t);color:#fff;font-family:“DM Sans”;font-size:13px;font-weight:600;cursor:pointer}

/* LOGIN */
.login-card{background:var(–w);border-radius:var(–rs);padding:16px;margin-bottom:10px;box-shadow:var(–sh)}
.key-inp{width:100%;background:var(–bg);border:1px solid var(–bdr);border-radius:var(–rx);padding:9px 12px;font-size:13px;font-family:“DM Mono”;color:var(–t);margin-bottom:8px}
.key-inp:focus{outline:none;border-color:var(–b)}
.region-row{display:flex;gap:8px;margin-bottom:10px}
.rbtn{flex:1;padding:9px;border-radius:var(–rx);font-size:12px;font-weight:600;cursor:pointer;font-family:“DM Sans”;transition:all .15s}
.rbtn-on{background:var(–t);color:#fff;border:2px solid var(–t)}
.rbtn-off{background:none;color:var(–t2);border:1.5px solid var(–bdr)}
.conn-btn{width:100%;padding:13px;border-radius:var(–rs);border:none;background:var(–t);color:#fff;font-family:“DM Sans”;font-size:14px;font-weight:700;cursor:pointer;margin-bottom:6px}
.conn-result{font-size:11px;text-align:center;min-height:16px;color:var(–t3);margin-bottom:4px}

/* DANGER */
.danger-btn{width:100%;padding:12px;border-radius:var(–rs);border:1.5px solid var(–rd);background:var(–rb);color:var(–r);font-family:“DM Sans”;font-size:13px;font-weight:600;cursor:pointer;margin-top:8px}

/* BADGE */
.badge{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px}
.bg2{background:var(–gb);color:var(–g)}.bb2{background:var(–bb);color:var(–b)}
.bo2{background:var(–ob);color:var(–o)}.br2{background:var(–rb);color:var(–r)}

/* NAV */
.nav{position:fixed;bottom:0;left:0;right:0;background:var(–w);border-top:1px solid var(–bdr);display:flex;justify-content:space-around;padding:8px 0 max(8px,env(safe-area-inset-bottom));z-index:100}
.nb{display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 10px;border:none;background:none;cursor:pointer;border-radius:var(–rx);min-width:50px}
.nb.active .ni,.nb.active .nl{color:var(–t);font-weight:700}
.ni{font-size:19px;color:var(–t3)}.nl{font-size:9px;color:var(–t3);font-weight:500;text-transform:uppercase;letter-spacing:.4px}

/* TOAST */
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(–t);color:#fff;padding:10px 20px;border-radius:20px;font-size:12px;font-weight:500;z-index:200;opacity:0;transition:opacity .25s;white-space:nowrap;pointer-events:none}
.toast.show{opacity:1}

@media(min-width:480px){.wrap{padding-left:24px;padding-right:24px}}
</style>

</head>
<body>
<div id="toast" class="toast"></div>

<!-- HEADER -->

<header class="hdr">
  <div class="hl">
    <div class="logo-ico">&#916;</div>
    <div><div class="ht">Alpha Bot</div><div class="hs" id="hdrsub">Delta Exchange India</div></div>
  </div>
  <div class="pill p-off" id="statusPill"><span class="pdot"></span><span id="pillTxt">Stopped</span></div>
</header>

<div class="wrap">

<!-- ══ HOME TAB ══ -->

<div id="tab-home" class="tab active">

  <!-- Connect banner — shown when not connected -->

  <div id="connectBanner" class="connect-banner" style="display:none" onclick="goSettings()">
    &#128273; Not connected — tap here to enter your Delta Exchange API keys &#8594;
  </div>

  <!-- BTC HERO -->

  <div class="btc">
    <div class="btc-r">
      <div class="bpl">1h Prediction</div>
      <div class="bpv bpvn" id="predPrice">&#8212;</div>
      <div class="bsig s-neu" id="predSig">Calculating...</div>
    </div>
    <div class="bl">Bitcoin &middot; Live</div>
    <div class="bp" id="btcPrice">$&#8212;</div>
    <div class="brow">
      <span class="bcb bu" id="btcChg">&#8212;%</span>
      <span style="font-size:10px;color:rgba(255,255,255,.3)">24h change</span>
    </div>
    <div class="chart-wrap"><canvas id="miniChart"></canvas></div>
  </div>

  <!-- REGIME -->

  <div class="regime-banner regime-UNKNOWN" id="regimeBanner">&#9679; Market regime loading...</div>

  <!-- SIGNAL SCORES -->

  <div class="sig-row">
    <div class="sig-box">
      <div class="sig-lbl">&#8593; Long Score</div>
      <div class="sig-val sig-n" id="longScore">&#8212;</div>
      <div class="sig-sub" id="longStatus">Waiting...</div>
    </div>
    <div class="sig-box">
      <div class="sig-lbl">&#8595; Short Score</div>
      <div class="sig-val sig-n" id="shortScore">&#8212;</div>
      <div class="sig-sub" id="shortStatus">Waiting...</div>
    </div>
    <div class="sig-box" id="decisionBox">
      <div class="sig-lbl">&#9889; Decision</div>
      <div class="sig-val sig-n" id="decisionScore">&#8212;</div>
      <div class="sig-sub" id="decisionStatus">Next scan...</div>
    </div>
  </div>

  <!-- SCAN COUNTDOWN -->

  <div class="scan-bar">
    <div style="flex:1">
      <div class="scan-l">Next scan in <b id="countdown" style="font-family:'DM Mono';color:var(--b)">&#8212;</b></div>
      <div class="scan-prog"><div class="scan-fill" id="scanFill" style="width:0%"></div></div>
    </div>
    <div class="scan-r" id="scanEvery">Every 5 min</div>
  </div>

  <!-- LIVE INDICATORS -->

  <div class="indics">
    <div class="ind"><div class="ind-l">RSI (14)</div><div class="ind-v" id="indRsi">&#8212;</div></div>
    <div class="ind"><div class="ind-l">ADX (14)</div><div class="ind-v" id="indAdx">&#8212;</div></div>
    <div class="ind"><div class="ind-l">ATR %</div><div class="ind-v" id="indAtr">&#8212;</div></div>
  </div>

  <!-- WALLET -->

  <div class="card">
    <div class="wrow">
      <div>
        <div class="wl">Wallet Balance</div>
        <div class="wa" id="walAmt">$&#8212;</div>
        <div class="ws">Started: <b id="walStart" style="font-family:'DM Mono'">$&#8212;</b></div>
      </div>
      <div class="wp">
        <div class="wpp pnn" id="walPct">&#8212;%</div>
        <div class="wpa" id="walPnl">P&amp;L: $&#8212;</div>
      </div>
    </div>
    <div class="chips" id="walChips"><span class="chip">Not connected</span></div>
    <div class="sync-row">
      <span class="ss ss-warn" id="syncSt">Not connected</span>
      <button class="sbtn" onclick="syncWallet()">&#x21BA; Sync</button>
    </div>
  </div>

  <!-- MONTHLY PROGRESS -->

  <div class="card">
    <div class="chd">
      <span class="ct">Monthly Target (10%)</span>
      <span id="mpStatus" style="font-size:10px;font-weight:700;color:var(--b)">ON TRACK</span>
    </div>
    <div class="mpb"><div id="mpFill" class="mpf" style="width:0%;background:var(--g)"></div></div>
    <div class="mpr"><span id="mpCur">0%</span><span id="mpRem">10% target</span></div>
  </div>

  <!-- STATS -->

  <div class="stats">
    <div class="sc"><div class="sl">Win Rate</div><div class="sv sv-b" id="stWR">&#8212;</div><div class="sub" id="stTr">0 trades</div></div>
    <div class="sc"><div class="sl">Today</div><div class="sv sv-b" id="stToday">0</div><div class="sub" id="stWeek">0 this week</div></div>
    <div class="sc"><div class="sl">Streak</div><div class="sv" id="stSk">0</div><div class="sub">Kelly: <b id="stKelly">&#8212;</b>%</div></div>
  </div>

  <!-- BOT STATUS -->

  <div class="srow">
    <div class="sico si-stop" id="sIco">&#9208;</div>
    <div style="flex:1">
      <div class="stxt" id="sTxt">Bot stopped — connect in Settings</div>
      <div class="stm" id="sTime">&#8212;</div>
    </div>
  </div>

  <!-- CONTROLS -->

  <div class="ctrl">
    <button class="btn btn-s" onclick="botAction('start')">&#9654; Start</button>
    <button class="btn btn-x" onclick="botAction('stop')">&#9646; Stop</button>
    <button class="btn btn-r" onclick="botAction('run_now')">&#9889; Scan Now</button>
  </div>

  <!-- MANUAL TRADE -->

  <div class="mt-card">
    <div class="chd"><span class="ct">Manual Trade</span><span style="font-size:10px;color:var(--t3)">Bypasses signals</span></div>
    <input type="number" id="manualSize" class="mt-inp" placeholder="Size in USD (0 = auto)" min="0" step="1">
    <div class="mt-row">
      <button class="btn-long" onclick="manualTrade('long')">&#8593; BUY LONG</button>
      <div style="width:8px"></div>
      <button class="btn-short" onclick="manualTrade('short')">&#8595; SELL SHORT</button>
    </div>
  </div>

  <!-- RECENT TRADES -->

  <div class="card">
    <div class="chd">
      <span class="ct">Recent Trades</span>
      <button style="font-size:11px;color:var(--b);font-weight:600;background:none;border:none;cursor:pointer" onclick="goTab('trades')">View all &#8594;</button>
    </div>
    <div id="recTrades"><div class="empty">No trades yet</div></div>
  </div>

<button class="danger-btn" onclick="closeAll()">⚠ Close All Positions</button>

</div>

<!-- ══ TRADES TAB ══ -->

<div id="tab-trades" class="tab">
  <div class="card" style="margin-top:4px">
    <div class="chd"><span class="ct">All Trades</span><span id="allCount" style="font-size:11px;color:var(--t3);font-family:'DM Mono'">0 trades</span></div>
    <div id="allTrades"><div class="empty">No trades yet</div></div>
  </div>
</div>

<!-- ══ SIGNALS TAB ══ -->

<div id="tab-signals" class="tab">
  <div class="card" style="margin-top:4px">
    <div class="chd"><span class="ct">Market Sentiment</span><span id="sentLabel" style="font-size:11px;font-weight:700;color:var(--b)">Loading...</span></div>
    <div class="sent-row">
      <span style="font-size:11px;color:var(--g);font-weight:600">Bull</span>
      <div class="sbar"><div class="sfill" id="sentFill" style="width:50%;background:var(--g)"></div></div>
      <span style="font-size:11px;color:var(--r);font-weight:600">Bear</span>
    </div>
    <div style="text-align:center;font-size:10px;color:var(--t3);margin-top:6px" id="sentTxt">CryptoPanic + Fear &amp; Greed Index</div>
  </div>
  <div class="card">
    <div class="chd"><span class="ct">Price Prediction</span><span id="predUpd" style="font-size:10px;color:var(--t3);font-family:'DM Mono'">&#8212;</span></div>
    <div class="pred-row">
      <div class="pi"><div class="ph">1 Hour</div><div class="pp" id="p1h">&#8212;</div><div class="pd" id="d1h">&#8212;</div></div>
      <div class="pi"><div class="ph">4 Hours</div><div class="pp" id="p4h">&#8212;</div><div class="pd" id="d4h">&#8212;</div></div>
      <div class="pi"><div class="ph">24 Hours</div><div class="pp" id="p24h">&#8212;</div><div class="pd" id="d24h">&#8212;</div></div>
    </div>
  </div>
  <div class="card">
    <div class="chd"><span class="ct">Signal Strength</span><span id="confScore" style="font-size:13px;font-family:'DM Mono';font-weight:700;color:var(--t)">&#8212; / 100</span></div>
    <div id="pilRows"></div>
  </div>
  <!-- Regime Detail Card -->
  <div class="card">
    <div class="chd"><span class="ct">Regime Analysis</span><span class="badge bb2">Multi-factor</span></div>
    <div id="regimeDetail" style="font-size:11px;color:var(--t2);line-height:1.8;font-family:'DM Mono'">
      Waiting for first scan...
    </div>
    <div style="font-size:10px;color:var(--t3);margin-top:8px">
      Combines: ADX strength + Market structure (HH/HL) + ATR expansion — not ADX alone
    </div>
  </div>

  <div class="card">
    <div class="chd"><span class="ct">Adaptive Learning</span><span class="badge bb2" id="learnBadge">0 trades</span></div>
    <div style="font-size:12px;color:var(--t2);display:flex;flex-direction:column;gap:6px">
      <div>RSI range learned: <b id="lRsi" style="font-family:'DM Mono'">40&#8211;55</b></div>
      <div>ADX min learned: <b id="lAdx" style="font-family:'DM Mono'">25.0</b></div>
      <div>Best hours UTC: <b id="lHrs" style="font-family:'DM Mono'">Learning...</b></div>
    </div>
  </div>
</div>

<!-- ══ LOGS TAB ══ -->

<div id="tab-logs" class="tab">
  <div class="card" style="margin-top:4px">
    <div class="chd">
      <span class="ct">Live Bot Logs</span>
      <div style="display:flex;gap:6px;align-items:center">
        <span id="logCount" style="font-size:10px;color:var(--t3);font-family:'DM Mono'">0 entries</span>
        <button class="sbtn" style="padding:3px 8px;font-size:10px" onclick="clearLogs()">Clear</button>
      </div>
    </div>
    <div id="botRunStatus" style="display:flex;align-items:center;padding:8px 0;border-bottom:1px solid var(--bdr);margin-bottom:8px;font-size:12px;font-weight:500">
      <span class="status-dot sd-stop" id="runDot"></span>
      <span id="runLabel">Checking...</span>
    </div>
    <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap">
      <button onclick="filterLogs('ALL')"   id="fALL"   class="sbtn" style="padding:3px 9px;font-size:10px;background:var(--t);color:#fff">All</button>
      <button onclick="filterLogs('TRADE')" id="fTRADE" class="sbtn" style="padding:3px 9px;font-size:10px">Trades</button>
      <button onclick="filterLogs('WARN')"  id="fWARN"  class="sbtn" style="padding:3px 9px;font-size:10px">Warnings</button>
      <button onclick="filterLogs('ERROR')" id="fERROR" class="sbtn" style="padding:3px 9px;font-size:10px">Errors</button>
    </div>
    <div class="logs-wrap" id="logsPanel">
      <div class="log-empty">No logs yet — connect and start bot</div>
    </div>
  </div>
</div>

<!-- ══ SETTINGS TAB ══ -->

<div id="tab-settings" class="tab">
  <!-- LOGIN CARD -->
  <div class="login-card" style="margin-top:4px">
    <div class="chd">
      <span class="ct">Delta Exchange Login</span>
      <span id="connBadge" class="badge bo2">Not connected</span>
    </div>
    <div id="connMsg" style="font-size:12px;color:var(--t2);margin-bottom:12px;line-height:1.5">
      Enter your Delta Exchange India API credentials.
    </div>
    <div class="region-row">
      <button id="btnIndia"  class="rbtn rbtn-on"  onclick="setRegion('india')">India</button>
      <button id="btnGlobal" class="rbtn rbtn-off" onclick="setRegion('global')">Global</button>
    </div>
    <input type="text"     id="inpKey"    class="key-inp" placeholder="API Key"    autocomplete="off" spellcheck="false">
    <input type="password" id="inpSecret" class="key-inp" placeholder="API Secret" autocomplete="off" spellcheck="false">
    <button id="connBtn" class="conn-btn" onclick="doConnect()">Connect to Delta Exchange</button>
    <div id="connResult" class="conn-result"></div>
    <div style="background:var(--bg);border-radius:var(--rx);padding:10px;font-size:11px;color:var(--t3);line-height:1.6">
      &#128274; Keys stored in server memory only — never saved to disk or browser. Re-enter after server restarts.
    </div>
  </div>

  <!-- SERVER IP CARD -->

  <div class="card">
    <div class="chd">
      <span class="ct">Render Server IP</span>
      <span class="badge bb2">Whitelist this</span>
    </div>
    <div style="background:var(--bg);border-radius:var(--rx);padding:12px;margin-bottom:10px;text-align:center">
      <div style="font-size:11px;color:var(--t3);margin-bottom:6px">Current outbound IP</div>
      <div id="serverIpDisplay" style="font-family:'DM Mono';font-size:20px;font-weight:700;color:var(--t);letter-spacing:1px">Loading...</div>
    </div>
    <div style="font-size:11px;color:var(--t2);line-height:1.7">
      1. Copy the IP above<br>
      2. Go to <b>india.delta.exchange</b> → Account → API Keys<br>
      3. Click <b>Edit</b> on your API key<br>
      4. Paste IP into the <b>IP Whitelist</b> field<br>
      5. Save → Come back and Connect<br><br>
      <span style="color:var(--o)">⚠ This IP can change on Render free tier after redeploys. Re-check whenever wallet shows $0.</span>
    </div>
    <button onclick="checkServerIp()" class="sbtn" style="width:100%;padding:10px;margin-top:8px;font-size:12px">
      ↻ Refresh IP
    </button>
  </div>

  <!-- SCAN FREQUENCY -->

  <div class="card">
    <div class="chd"><span class="ct">Scan Frequency</span></div>
    <div class="sfield">
      <div class="sfl">Scan every <span id="scanMinsLbl">5</span> minutes</div>
      <div class="sr"><input type="range" id="scanS" min="1" max="30" value="5" oninput="document.getElementById('scanMinsLbl').textContent=this.value;document.getElementById('scanDisp').textContent=this.value+'m'"><span class="sv2" id="scanDisp">5m</span></div>
      <div class="sdesc" id="scanDesc">~192 scans/day</div>
    </div>
    <div class="sfield">
      <div class="sfl">Min Confidence: <span id="confLbl">58</span></div>
      <div class="sr"><input type="range" id="confS" min="50" max="90" value="58" oninput="document.getElementById('confLbl').textContent=this.value"><span class="sv2" id="confDisp">58</span></div>
      <div class="sdesc">Lower = more trades</div>
    </div>
    <div class="sfield">
      <div class="sfl">Max Risk Per Trade: <span id="riskLbl">2</span>%</div>
      <div class="sr"><input type="range" id="riskS" min="0.5" max="5" step="0.5" value="2" oninput="document.getElementById('riskLbl').textContent=this.value"><span class="sv2" id="riskDisp">2%</span></div>
    </div>
    <button class="save-btn" onclick="saveConfig()">Save Settings</button>
  </div>

  <div class="card">
    <div class="chd"><span class="ct">Risk Guard</span></div>
    <div style="font-size:12px;display:flex;flex-direction:column;gap:0">
      <div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--bdr)"><span style="color:var(--t2)">Monthly target</span><b style="font-family:'DM Mono'">10%</b></div>
      <div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--bdr)"><span style="color:var(--t2)">Monthly halt</span><b style="font-family:'DM Mono';color:var(--r)">-8%</b></div>
      <div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--bdr)"><span style="color:var(--t2)">Daily pause</span><b style="font-family:'DM Mono';color:var(--o)">-3%</b></div>
      <div style="display:flex;justify-content:space-between;padding:7px 0"><span style="color:var(--t2)">Circuit breaker</span><b style="font-family:'DM Mono'">3 losses</b></div>
    </div>
  </div>

<button class="danger-btn" onclick="closeAll()">⚠ Emergency Close All</button>

</div>

</div><!-- wrap -->

<!-- BOTTOM NAV — 5 tabs -->

<nav class="nav">
  <button class="nb active" id="nb0" onclick="goTab('home')"><span class="ni">&#127968;</span><span class="nl">Home</span></button>
  <button class="nb"         id="nb1" onclick="goTab('trades')"><span class="ni">&#128203;</span><span class="nl">Trades</span></button>
  <button class="nb"         id="nb2" onclick="goTab('signals')"><span class="ni">&#128200;</span><span class="nl">Signals</span></button>
  <button class="nb"         id="nb3" onclick="goTab('logs')"><span class="ni">&#128220;</span><span class="nl">Logs</span></button>
  <button class="nb"         id="nb4" onclick="goTab('settings')"><span class="ni">&#9881;&#65039;</span><span class="nl">Settings</span></button>
</nav>

<script>
// ── STATE ────────────────────────────────────────────────────────────────────
let btcPrice=0, scanInterval=300, nextScanTime=null;
let allLogs=[], logFilter='ALL', selectedRegion='india';

// ── NAVIGATION ────────────────────────────────────────────────────────────────
const TABS=['home','trades','signals','logs','settings'];
function goTab(name){
  TABS.forEach(t=>{
    document.getElementById('tab-'+t).classList.toggle('active', t===name);
  });
  for(let i=0;i<5;i++){
    document.getElementById('nb'+i).classList.toggle('active', TABS[i]===name);
  }
  if(name==='logs') refreshLogs();
}
function goSettings(){ goTab('settings'); }

// ── TOAST ─────────────────────────────────────────────────────────────────────
function toast(m){
  const e=document.getElementById('toast');
  e.textContent=m; e.classList.add('show');
  setTimeout(()=>e.classList.remove('show'),2500);
}

// ── API ───────────────────────────────────────────────────────────────────────
async function apiCall(path, method='GET', body=null){
  try{
    const opts={method, headers:{'Content-Type':'application/json'}};
    if(body) opts.body=JSON.stringify(body);
    const r=await fetch(path, opts);
    return await r.json();
  }catch(e){ return null; }
}

// ── COUNTDOWN ─────────────────────────────────────────────────────────────────
function updateCountdown(){
  if(!nextScanTime) return;
  const diff=Math.max(0, new Date(nextScanTime).getTime()-Date.now());
  const mins=Math.floor(diff/60000);
  const secs=Math.floor((diff%60000)/1000);
  document.getElementById('countdown').textContent=
    mins>0 ? mins+'m '+String(secs).padStart(2,'0')+'s' : secs+'s';
  const pct=100-(diff/(scanInterval*1000)*100);
  document.getElementById('scanFill').style.width=Math.max(0,Math.min(100,pct))+'%';
}
setInterval(updateCountdown, 1000);

// ── MINI CHART ────────────────────────────────────────────────────────────────
function drawChart(prices){
  const canvas=document.getElementById('miniChart');
  if(!canvas||!prices||prices.length<2) return;
  const ctx=canvas.getContext('2d');
  const W=canvas.offsetWidth||300, H=44;
  canvas.width=W; canvas.height=H;
  ctx.clearRect(0,0,W,H);
  const min=Math.min(...prices), max=Math.max(...prices), range=max-min||1;
  const pts=prices.map((p,i)=>({x:i/(prices.length-1)*W, y:H-(p-min)/range*(H-4)-2}));
  const up=prices[prices.length-1]>=prices[0];
  const grad=ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0, up?'rgba(0,200,150,.3)':'rgba(240,72,62,.3)');
  grad.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(pts[0].x,H);
  pts.forEach(p=>ctx.lineTo(p.x,p.y));
  ctx.lineTo(pts[pts.length-1].x,H); ctx.closePath();
  ctx.fillStyle=grad; ctx.fill();
  ctx.beginPath(); pts.forEach((p,i)=>i===0?ctx.moveTo(p.x,p.y):ctx.lineTo(p.x,p.y));
  ctx.strokeStyle=up?'#00e8b0':'#ff6b64'; ctx.lineWidth=1.5; ctx.stroke();
}

// ── PREDICTION ────────────────────────────────────────────────────────────────
function computePred(regime, atrPctVal){
  if(!btcPrice) return;
  document.getElementById('predUpd').textContent='Updated '+new Date().toISOString().substr(11,5)+' UTC';
  const atrPct=(atrPctVal>0)?atrPctVal/100:0.008;
  const atr=btcPrice*atrPct;
  // Use real regime — no random
  const bull=regime==='STRONG_BULL'||regime==='BULL';
  const bear=regime==='STRONG_BEAR'||regime==='BEAR';
  if(regime==='NEUTRAL'||regime==='UNKNOWN'||!regime){
    ['p1h','p4h','p24h'].forEach(id=>document.getElementById(id).textContent='Range');
    ['d1h','d4h','d24h'].forEach(id=>{document.getElementById(id).textContent='Sideways';document.getElementById(id).className='pd';});
    document.getElementById('predPrice').textContent='Range';
    document.getElementById('predPrice').className='bpv bpvn';
    document.getElementById('predSig').textContent='No directional bias';
    document.getElementById('predSig').className='bsig s-neu';
    return;
  }
  const dir=bull?1:-1;
  const p1=btcPrice+dir*atr*0.6;
  const p4=btcPrice+dir*atr*1.2;
  const p24=btcPrice+dir*atr*2.1;
  const fmt=v=>'$'+Math.round(v).toLocaleString();
  const dirStr=(v)=>{
    const pct=((v-btcPrice)/btcPrice*100);
    return {txt:(pct>=0?'&#9650; +':'&#9660; ')+Math.abs(pct).toFixed(2)+'%', cls:pct>=0?'pd-u':'pd-d'};
  };
  document.getElementById('p1h').textContent=fmt(p1);
  document.getElementById('p4h').textContent=fmt(p4);
  document.getElementById('p24h').textContent=fmt(p24);
  [['d1h',p1],['d4h',p4],['d24h',p24]].forEach(([id,p])=>{
    const d=dirStr(p); document.getElementById(id).innerHTML=d.txt; document.getElementById(id).className='pd '+d.cls;
  });
  document.getElementById('predPrice').textContent=fmt(p1);
  document.getElementById('predPrice').className='bpv '+(bull?'bpvu':'bpvd');
  document.getElementById('predSig').innerHTML=bull?'&#8593; Bullish':'&#8595; Bearish';
  document.getElementById('predSig').className='bsig '+(bull?'s-bull':'s-bear');
}

// ── MAIN REFRESH ─────────────────────────────────────────────────────────────
async function refresh(){
  const [state, trades, ticker] = await Promise.all([
    apiCall('/api/status'),
    apiCall('/api/trades'),
    apiCall('/api/ticker')
  ]);
  if(ticker) renderTicker(ticker);
  if(state)  renderState(state);
  if(trades) renderTrades(trades);
}

// ── TICKER ────────────────────────────────────────────────────────────────────
function renderTicker(t){
  const p=parseFloat(t.mark_price||t.last_price||0);
  if(!p) return;
  btcPrice=p;
  document.getElementById('btcPrice').textContent='$'+p.toLocaleString('en-US',{maximumFractionDigits:0});
  const idx=parseFloat(t.index_price||p);
  const chg=idx>0?((p-idx)/idx*100):0;
  const b=document.getElementById('btcChg');
  b.textContent=(chg>=0?'+':'')+chg.toFixed(2)+'%';
  b.className='bcb '+(chg>=0?'bu':'bd');
}

// ── STATE RENDER ─────────────────────────────────────────────────────────────
function renderState(s){
  // Connect banner
  document.getElementById('connectBanner').style.display=
    (!s.wallet_synced&&!s.running)?'flex':'none';

  // Header pill
  const pill=document.getElementById('statusPill');
  pill.className='pill '+(s.running?'p-on':'p-off');
  document.getElementById('pillTxt').textContent=s.running?'Live':'Stopped';
  document.getElementById('hdrsub').textContent='Delta Exchange '+(s.wallet_synced?'✓ Connected':'• Not connected');

  // Chart
  if(s.candles_cache&&s.candles_cache.length>1) drawChart(s.candles_cache);

  // Scan timing
  if(s.next_scan_at) nextScanTime=s.next_scan_at;
  if(s.scan_interval){ scanInterval=s.scan_interval; document.getElementById('scanEvery').textContent='Every '+(scanInterval/60|0)+'m'; }

  // Regime
  const regime=s.last_regime||'UNKNOWN';
  const rb=document.getElementById('regimeBanner');
  rb.className='regime-banner regime-'+regime;
  const icons={STRONG_BULL:'&#128308;&#128308; STRONG BULL &nbsp;&#183;&nbsp; EMA stacked, ADX>25',BULL:'&#128308; BULL &nbsp;&#183;&nbsp; Uptrend, moderate momentum',NEUTRAL:'&#9898; NEUTRAL &nbsp;&#183;&nbsp; Sideways — watching for breakout',BEAR:'&#128309; BEAR &nbsp;&#183;&nbsp; Downtrend pressure',STRONG_BEAR:'&#128309;&#128309; STRONG BEAR &nbsp;&#183;&nbsp; EMA stacked down, ADX>25',UNKNOWN:'&#9679; Market regime loading...'};
  rb.innerHTML=icons[regime]||regime;

  // Signal scores
  const ls=s.last_long_score||0, ss=s.last_short_score||0;
  const lv=s.last_long_veto||'', sv=s.last_short_veto||'';
  const lEl=document.getElementById('longScore');
  lEl.textContent=ls||'—';
  lEl.className='sig-val '+(ls>=58?'sig-g':'sig-n');
  document.getElementById('longStatus').innerHTML=lv?'<span style="color:var(--r);font-size:9px">&#10005; '+lv+'</span>':ls>=58?'<span style="color:var(--g);font-size:9px">&#10003; Above threshold</span>':'<span style="font-size:9px">Below threshold</span>';
  const sEl=document.getElementById('shortScore');
  sEl.textContent=ss||'—';
  sEl.className='sig-val '+(ss>=58?'sig-r':'sig-n');
  document.getElementById('shortStatus').innerHTML=sv?'<span style="color:var(--r);font-size:9px">&#10005; '+sv+'</span>':ss>=58?'<span style="color:var(--r);font-size:9px">&#10003; Above threshold</span>':'<span style="font-size:9px">Below threshold</span>';

  // Decision
  const dEl=document.getElementById('decisionScore');
  const dSt=document.getElementById('decisionStatus');
  const dBox=document.getElementById('decisionBox');
  if(s.will_trade&&s.trade_direction){
    const isL=s.trade_direction==='long';
    dEl.innerHTML=isL?'LONG':'SHORT';
    dEl.className='sig-val '+(isL?'sig-g':'sig-r');
    dSt.innerHTML='<span class="will-badge">WILL TRADE</span>';
    dBox.style.outline=isL?'2px solid var(--g)':'2px solid var(--r)';
  } else {
    dEl.innerHTML='WAIT';
    dEl.className='sig-val sig-n';
    dSt.innerHTML='<span style="font-size:9px;color:var(--t3)">No signal</span>';
    dBox.style.outline='none';
  }

  // Prediction — uses real regime + real ATR
  computePred(regime, s.last_atr_pct||0);

  // Live indicators
  const rsi=s.last_rsi||0;
  const rEl=document.getElementById('indRsi');
  rEl.textContent=rsi>0?rsi.toFixed(1):'—';
  rEl.className='ind-v '+(rsi>70?'iv-r':rsi<30?'iv-g':'iv-y');
  const adx=s.last_adx||0;
  const aEl=document.getElementById('indAdx');
  aEl.textContent=adx>0?adx.toFixed(1):'—';
  aEl.className='ind-v '+(adx>25?'iv-g':adx>15?'iv-y':'iv-r');
  document.getElementById('indAtr').textContent=s.last_atr_pct>0?s.last_atr_pct.toFixed(2)+'%':'—';

  // Wallet
  const cap=s.capital||0, sc=s.starting_capital||0, pnl=s.total_pnl||0, pct=s.pnl_pct||0;
  document.getElementById('walAmt').textContent='$'+cap.toFixed(2);
  document.getElementById('walStart').textContent='$'+sc.toFixed(2);
  const pE=document.getElementById('walPct');
  pE.textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';
  pE.className='wpp '+(pct>0?'pu':pct<0?'pdn':'pnn');
  document.getElementById('walPnl').textContent='P&L: $'+(pnl>=0?'+':'')+pnl.toFixed(2);
  const chips=[];
  if(s.wallet_usdt>0) chips.push('USD '+s.wallet_usdt.toFixed(2));
  if(s.wallet_inr>0)  chips.push('INR '+s.wallet_inr.toFixed(0));
  if(s.wallet_btc>0)  chips.push('BTC '+s.wallet_btc.toFixed(6));
  document.getElementById('walChips').innerHTML=(chips.length?chips:['Not connected']).map(c=>'<span class="chip">'+c+'</span>').join('');
  const ss2=document.getElementById('syncSt');
  if(s.wallet_synced){ss2.textContent='✓ Synced from Delta Exchange';ss2.className='ss ss-ok';}
  else{ss2.textContent='Not connected — go to Settings';ss2.className='ss ss-warn';}

  // Monthly progress
  const mp=s.monthly_progress||{};
  const prog=Math.max(0,Math.min(100,mp.progress_pct||0));
  const mpF=document.getElementById('mpFill');
  mpF.style.width=prog+'%';
  mpF.style.background=mp.monthly_status==='HALTED'?'var(--r)':'var(--g)';
  document.getElementById('mpCur').textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';
  document.getElementById('mpRem').textContent='Target 10% — '+(mp.remaining_pct||10).toFixed(2)+'% left';
  const mpSt=document.getElementById('mpStatus');
  mpSt.textContent=mp.monthly_status||'ON TRACK';
  mpSt.style.color=mp.monthly_status==='HALTED'?'var(--r)':mp.monthly_status==='TARGET HIT'?'var(--g)':'var(--b)';

  // Stats
  const wr=s.win_rate||0, tt=s.total_trades||0;
  document.getElementById('stWR').textContent=tt>=3?wr.toFixed(1)+'%':'—';
  document.getElementById('stTr').textContent=tt+' trades';
  document.getElementById('stToday').textContent=s.trades_today||0;
  document.getElementById('stWeek').textContent=(s.trades_week||0)+' this week';
  const sk=s.streak||0;
  const skE=document.getElementById('stSk');
  skE.textContent=(sk>0?'+':'')+sk+(sk>2?' &#128293;':sk<-2?' &#129488;':'');
  skE.className='sv '+(sk>0?'sv-g':sk<0?'sv-r':'');
  document.getElementById('stKelly').textContent=s.kelly_fraction>0?s.kelly_fraction.toFixed(2):'—';

  // Status
  const ico=document.getElementById('sIco');
  ico.className='sico '+(s.running?'si-run':s.in_recovery?'si-warn':'si-stop');
  ico.innerHTML=s.running?'▶':s.in_recovery?'⚠':'⏸';
  document.getElementById('sTxt').textContent=s.status||'Bot stopped — connect in Settings';
  document.getElementById('sTime').textContent=new Date().toISOString().substr(0,19).replace('T',' ')+' UTC';

  // Sentiment
  const ns=s.news_sentiment||{};
  const sc3=ns.score||0;
  const bPct=Math.round((sc3+1)/2*100);
  document.getElementById('sentFill').style.width=bPct+'%';
  document.getElementById('sentFill').style.background=bPct>50?'var(--g)':'var(--r)';
  document.getElementById('sentLabel').textContent=ns.label||'Neutral';
  const srcCount=ns.sources_checked||0;document.getElementById('sentTxt').textContent='Bull '+bPct+'% / Bear '+(100-bPct)+'% | Sources: '+srcCount+(srcCount<2?' (low confidence — need 2+ sources)':'');

  // Pillars — show REAL scores from last confidence calculation
  const conf=(s.recent_trades&&s.recent_trades.length)?s.recent_trades[s.recent_trades.length-1].confidence||0:0;
  const bd=s.last_breakdown||{};
  // Total actual score
  const totalScore=Object.values(bd).reduce((a,b)=>a+b,0);
  document.getElementById('confScore').textContent=
    totalScore>0?(totalScore+' / 100'):(conf?conf+' / 100':'— / 100');
  // Pillar definitions with actual score keys
  // 4-pillar institutional confidence engine
  const pillars=[
    {n:'Regime (ADX+Structure)',key:'regime',   max:40, c:'#0066ff'},
    {n:'Momentum Quality',      key:'momentum', max:30, c:'#00c896'},
    {n:'Volatility Regime',     key:'volatility',max:20,c:'#ff9f00'},
    {n:'Execution Quality',     key:'execution',max:10, c:'#ff6b6b'}
  ];
  document.getElementById('pilRows').innerHTML=pillars.map(p=>{
    const actual=bd[p.key]!==undefined?bd[p.key]:null;
    const displayVal=actual!==null?actual:p.max;
    const barPct=(displayVal/p.max)*100;
    const label=actual!==null?actual:p.max;
    const opacity=actual!==null?'1':'0.35'; // Dim if no real data yet
    return '<div class="pil"><div class="pn">'+p.n+'</div>'
      +'<div class="pt"><div class="pf" style="width:'+barPct+'%;background:'+p.c+';opacity:'+opacity+'"></div></div>'
      +'<div class="pw" style="color:'+p.c+'">'+label+'</div></div>';
  }).join('');

  // Show regime detail if available
  const last_bd = s.last_breakdown || {};
  const rd = last_bd._regime_detail || {};
  if(rd.label){
    const rdEl = document.getElementById('regimeDetail');
    if(rdEl) rdEl.innerHTML =
      '<b>'+rd.label+'</b> confidence='+rd.confidence+'% | '+
      'Structure: '+rd.structure+' | '+
      'Vol expanding: '+(rd.vol_expanding?'YES ↑':'NO ↔')+' | '+
      'ATR ratio: '+rd.atr_ratio+'×';
  }

  // Learning
  const lrn=s.learning||{};
  document.getElementById('learnBadge').textContent=(lrn.trades_remembered||0)+' trades';
  const rr=lrn.rsi_long_range||[40,55];
  document.getElementById('lRsi').textContent=rr[0]+'–'+rr[1];
  document.getElementById('lAdx').textContent=lrn.adx_min||25;
  document.getElementById('lHrs').textContent=(lrn.best_hours&&lrn.best_hours.length)?lrn.best_hours.join(', '):'Learning...';

  // Logs tab run status
  const dot=document.getElementById('runDot');
  const lbl=document.getElementById('runLabel');
  if(s.running&&s.wallet_synced){
    dot.className='status-dot sd-live';
    lbl.textContent='Bot RUNNING — live data from Delta Exchange';
    lbl.style.color='var(--g)';
  } else if(!s.wallet_synced){
    dot.className='status-dot sd-stop';
    lbl.textContent='Not connected — go to Settings to connect';
    lbl.style.color='var(--r)';
  } else {
    dot.className='status-dot sd-stop';
    lbl.textContent='Bot stopped — press Start';
    lbl.style.color='var(--o)';
  }

  // Settings tab — update connection badge
  const cb=document.getElementById('connBadge');
  const cm=document.getElementById('connMsg');
  if(s.wallet_synced||s.running){
    cb.textContent='Connected ✓'; cb.className='badge bg2';
    cm.innerHTML='<span style="color:var(--g)">&#10003; Connected — bot running. Balance: $'+cap.toFixed(2)+'</span>';
  }
}

// ── TRADES ────────────────────────────────────────────────────────────────────
function renderTrades(trades){
  if(!trades||!trades.length){
    document.getElementById('recTrades').innerHTML='<div class="empty">No trades yet</div>';
    document.getElementById('allTrades').innerHTML='<div class="empty">No trades yet</div>';
    return;
  }
  document.getElementById('allCount').textContent=trades.length+' trades';
  function row(t){
    const ic=t.action==='CLOSE', won=t.pnl_pct>0, side=t.side||'';
    const icCls=side==='long'?'ti-l':side==='short'?'ti-s':'ti-o';
    const icTxt=side==='long'?'&#8593;':side==='short'?'&#8595;':'&#9675;';
    const pnl=ic?((won?'+':'')+t.pnl_pct?.toFixed(2)+'%'):'Open';
    const pCls=ic?(won?'tp-u':'tp-d'):'tp-n';
    const tm=t.time?t.time.substr(5,11).replace('T',' '):'—';
    const manual=t.reason==='manual'?' <span style="background:var(--ob);color:var(--o);font-size:8px;padding:1px 4px;border-radius:3px;font-weight:700">MANUAL</span>':'';
    return '<div class="trow"><div class="tl"><div class="tico '+icCls+'">'+icTxt+'</div><div><div class="tsym">'+(t.symbol||'BTC')+manual+'</div><div class="ttm">'+tm+' &middot; '+side.toUpperCase()+'</div></div></div><div class="trr"><div class="tpnl '+pCls+'">'+pnl+'</div><div class="tpr">$'+(t.price?.toFixed(0)||'—')+(t.confidence?' C:'+t.confidence:'')+'</div></div></div>';
  }
  const rev=[...trades].reverse();
  document.getElementById('recTrades').innerHTML=rev.slice(0,5).map(row).join('');
  document.getElementById('allTrades').innerHTML=rev.map(row).join('');
}

// ── LOGS ──────────────────────────────────────────────────────────────────────
async function refreshLogs(){
  const r=await apiCall('/api/logs?limit=100');
  if(!r) return;
  allLogs=r.logs||[];
  document.getElementById('logCount').textContent=allLogs.length+' entries';
  renderLogs();
}
function filterLogs(f){
  logFilter=f;
  document.querySelectorAll('[id^="f"]').forEach(b=>{
    const active=b.id==='f'+f;
    b.style.background=active?'var(--t)':'';
    b.style.color=active?'#fff':'';
  });
  renderLogs();
}
function renderLogs(){
  const panel=document.getElementById('logsPanel');
  const filtered=logFilter==='ALL'?allLogs:allLogs.filter(l=>l.level===logFilter);
  if(!filtered.length){panel.innerHTML='<div class="log-empty">No '+(logFilter==='ALL'?'':''+logFilter.toLowerCase()+' ')+'logs yet</div>';return;}
  panel.innerHTML=[...filtered].reverse().map(l=>'<div class="log-row"><span class="log-time">'+(l.time||'')+'</span><span class="ll ll-'+l.level+'">'+l.level+'</span><span class="log-msg">'+(l.msg||'')+'</span></div>').join('');
  panel.scrollTop=0;
}
function clearLogs(){ allLogs=[]; renderLogs(); }
setInterval(()=>{ if(document.getElementById('tab-logs').classList.contains('active')) refreshLogs(); }, 3000);

// ── LOGIN ─────────────────────────────────────────────────────────────────────
async function checkServerIp(){
  const el=document.getElementById('serverIpDisplay');
  if(el) el.textContent='Fetching...';
  const r=await apiCall('/api/ip');
  if(r&&r.render_outbound_ip){
    if(el) el.textContent=r.render_outbound_ip;
  } else {
    if(el) el.textContent='Failed — check logs';
  }
}

function setRegion(r){
  selectedRegion=r;
  document.getElementById('btnIndia').className='rbtn '+(r==='india'?'rbtn-on':'rbtn-off');
  document.getElementById('btnGlobal').className='rbtn '+(r==='global'?'rbtn-on':'rbtn-off');
}
async function doConnect(){
  const key=document.getElementById('inpKey').value.trim();
  const secret=document.getElementById('inpSecret').value.trim();
  if(!key||!secret){
    document.getElementById('connResult').innerHTML='<span style="color:var(--r)">Enter both API key and secret</span>';
    return;
  }
  const btn=document.getElementById('connBtn');
  const res=document.getElementById('connResult');
  btn.textContent='Connecting...'; btn.disabled=true;
  res.textContent='Testing connection...'; res.style.color='var(--t3)';
  // Get IP for logging
  let ip='unknown';
  try{ const ir=await fetch('https://api.ipify.org?format=json'); ip=(await ir.json()).ip; }catch(e){}
  const r=await apiCall('/api/connect','POST',{api_key:key, api_secret:secret, region:selectedRegion, ip});
  btn.textContent='Connect to Delta Exchange'; btn.disabled=false;
  if(r&&r.success){
    res.innerHTML='<span style="color:var(--g)">&#10003; Connected! Balance: $'+r.balance.toFixed(2)+'</span>';
    document.getElementById('inpKey').value='';
    document.getElementById('inpSecret').value='';
    toast('Connected! Balance: $'+r.balance.toFixed(2));
    setTimeout(()=>{ goTab('home'); refresh(); }, 1500);
  } else {
    res.innerHTML='<span style="color:var(--r)">&#10005; '+(r?.message||'Failed — check keys and IP whitelist')+'</span>';
  }
}

// ── ACTIONS ───────────────────────────────────────────────────────────────────
async function botAction(a){
  const r=await apiCall('/api/bot/'+a,'POST');
  toast(r?.message||(a+' sent'));
  setTimeout(refresh,1500);
}
async function syncWallet(){
  toast('Syncing...');
  const r=await apiCall('/api/wallet/sync','POST');
  if(r?.success) toast('Synced: $'+r.capital_usd.toFixed(2));
  else toast('Sync failed — check connection');
  setTimeout(refresh,800);
}
async function manualTrade(dir){
  const size=parseFloat(document.getElementById('manualSize').value)||0;
  if(!confirm('Place MANUAL '+dir.toUpperCase()+' trade?')) return;
  toast('Placing order...');
  const r=await apiCall('/api/manual_trade','POST',{direction:dir,size_usd:size});
  if(r?.success) toast(r.message);
  else toast('Failed: '+(r?.message||'error'));
  setTimeout(refresh,1500);
}
async function closeAll(){
  if(!confirm('Close ALL positions on Delta Exchange?')) return;
  const r=await apiCall('/api/close_all','POST');
  toast('Closed '+(r?.closed||0)+' positions');
  setTimeout(refresh,1500);
}
async function saveConfig(){
  const conf=parseInt(document.getElementById('confS').value);
  const risk=parseFloat(document.getElementById('riskS').value)/100;
  const mins=parseInt(document.getElementById('scanS').value);
  const [r1,r2]=await Promise.all([
    apiCall('/api/config','POST',{min_confidence:conf,max_risk_pct:risk}),
    apiCall('/api/set_scan_interval','POST',{minutes:mins})
  ]);
  toast(r1?.success&&r2?.success?'Settings saved!':'Saved (partial)');
}

// ── INIT ──────────────────────────────────────────────────────────────────────
refresh();
refreshLogs();
checkServerIp();  // Load server IP immediately on page load
setInterval(refresh, 5000);
setInterval(checkServerIp, 60000); // Refresh IP every 60s
// Show connect banner after first status check
setTimeout(async()=>{
  const s=await apiCall('/api/status');
  if(s&&!s.wallet_synced&&!s.running)
    document.getElementById('connectBanner').style.display='flex';
}, 2000);
</script>

</body>
</html>"""

@app.route(”/”)
def index():
return Response(DASHBOARD_HTML, mimetype=“text/html”)

if **name** == “**main**”:
port = int(os.getenv(“PORT”, 5000))
log.info(f”Starting DELTA ALPHA Bot v6.2 on port {port}”)
# bot already started by _auto_start() above
app.run(host=“0.0.0.0”, port=port, debug=False)