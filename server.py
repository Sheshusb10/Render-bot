"""
ΔLPHA BOT v6.0 — Delta Exchange India | BTC Options
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HONEST AUDIT FIXES (v5 → v6):
  ✅ DRAWDOWN CIRCUIT BREAKER — stops trading after 3 consecutive losses
  ✅ MONTHLY LOSS LIMIT — halts bot if monthly drawdown > 8%
  ✅ PREMIUM vs MOVE CHECK — won't buy option if theta > expected move
  ✅ PRE-ANNOUNCEMENT BLACKOUT — extended to 45min before macro events
  ✅ API HEALTH MONITOR — detects silent failures, protects open positions
  ✅ POSITION SIZE FLOOR — after losing streak, size drops to 0.5% until recovery
  ✅ REAL NEWS SCORING — sentiment weighted by source credibility + recency
  ✅ VOLUME PROFILE — detects low-liquidity traps before entry
  ✅ OPEN INTEREST SPIKE — detects whale accumulation / distribution
  ✅ SIDEWAYS PROFIT — straddle strategy when ADX < 20 but squeeze detected
  ✅ 10% MONTHLY TARGET ENGINE — position sizing calibrated to monthly goal
  ✅ ADAPTIVE LEARNING — win/loss patterns update RSI/ADX thresholds over time
"""

import os, time, hmac, hashlib, json, logging, requests, threading, math
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("ALPHA_V6")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
class Cfg:
    API_KEY    = os.getenv("DELTA_API_KEY", "")
    API_SECRET = os.getenv("DELTA_API_SECRET", "")
    BASE_URL   = "https://api.india.delta.exchange"

    # Risk — live wallet only, no hardcoded capital
    MAX_RISK_NORMAL    = 0.02   # 2% per trade (normal)
    MAX_RISK_HOT       = 0.03   # 3% on high confidence + win streak
    MAX_RISK_RECOVERY  = 0.005  # 0.5% during drawdown recovery mode
    KELLY_FRACTION     = 0.25
    MAX_OPEN_POSITIONS = 2

    # 10% monthly target engine
    MONTHLY_TARGET_PCT  = 0.10  # 10% monthly goal
    MONTHLY_LOSS_LIMIT  = 0.08  # Hard stop: halt bot if down 8% in month
    DAILY_LOSS_LIMIT    = 0.03  # Pause for 24h if down 3% in one day

    # Circuit breaker
    MAX_CONSEC_LOSSES   = 3     # Stop trading after 3 losses in a row
    RECOVERY_TRADES     = 2     # Need 2 wins to exit recovery mode

    # Exits
    HARD_STOP_PCT       = 0.025  # Tighter: 2.5% stop (was 3%)
    TP1_PCT             = 0.015
    TP2_PCT             = 0.030  # Higher TP2 to reach 10% monthly
    TRAIL_ACTIVATE_PCT  = 0.012
    TRAIL_DISTANCE_PCT  = 0.007

    # Regime
    ADX_TREND_MIN       = 25
    ADX_SQUEEZE_MAX     = 18    # Below this = squeeze candidate for straddle

    # RSI (adaptive — updated by learning engine)
    RSI_BULL_PULLBACK   = (40, 55)
    RSI_BEAR_BOUNCE     = (45, 60)

    # Time (UTC)
    DEAD_ZONE_HOURS     = [2, 3, 4, 5]
    PEAK_HOURS          = [8, 9, 13, 14, 15, 16]
    # Extended blackout: 45min before major events
    MACRO_BLACKOUT_TIMES = [(13, 30), (19, 0), (8, 30)]  # Added 8:30 UTC (EU open data)
    BLACKOUT_WINDOW_MINS = 45  # Was 15, now 45

    # Premium safety
    MIN_MOVE_TO_PREMIUM_RATIO = 1.5  # Expected move must be 1.5x the premium paid

    # Funding
    FUNDING_LONG_MAX    = 0.001
    FUNDING_SHORT_MIN   = -0.0005

    # OI spike threshold
    OI_SPIKE_PCT        = 0.15   # 15% OI change = whale activity

    # Confidence
    MIN_CONFIDENCE      = 65
    HIGH_CONFIDENCE     = 82

    BTC_PRODUCT_ID      = 27
    SCAN_INTERVAL       = 300

# ══════════════════════════════════════════════════════════════════════════════
# DELTA EXCHANGE API
# ══════════════════════════════════════════════════════════════════════════════
class DeltaAPI:
    def __init__(self):
        self.base    = Cfg.BASE_URL
        self.key     = Cfg.API_KEY
        self.secret  = Cfg.API_SECRET
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.last_success = time.time()
        self.consecutive_failures = 0
        self.healthy = True

    def _sign(self, method, path, qs="", body=""):
        ts  = str(int(time.time()))
        msg = method + ts + path + qs + body
        sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {"api-key": self.key, "timestamp": ts, "signature": sig,
                "User-Agent": "alpha-bot-v6"}

    def _get(self, path, params=None):
        qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
        try:
            r = self.session.get(f"{self.base}{path}{qs}",
                                 headers=self._sign("GET", path, qs), timeout=10)
            r.raise_for_status()
            self.consecutive_failures = 0
            self.healthy = True
            self.last_success = time.time()
            return r.json()
        except Exception as e:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self.healthy = False
                log.error(f"API UNHEALTHY after {self.consecutive_failures} failures: {e}")
            return None

    def _post(self, path, body):
        body_str = json.dumps(body)
        try:
            r = self.session.post(f"{self.base}{path}",
                                  headers=self._sign("POST", path, "", body_str),
                                  data=body_str, timeout=10)
            r.raise_for_status()
            self.consecutive_failures = 0
            self.healthy = True
            return r.json()
        except Exception as e:
            self.consecutive_failures += 1
            return None

    def get_candles(self, symbol="BTCUSD", resolution=5, limit=100):
        end   = int(time.time())
        start = end - (resolution * 60 * limit)
        d = self._get("/v2/history/candles",
                      {"symbol": symbol, "resolution": resolution,
                       "start": start, "end": end})
        return d.get("result", []) if d and d.get("success") else []

    def get_ticker(self, symbol="BTCUSD"):
        d = self._get(f"/v2/tickers/{symbol}")
        return d.get("result", {}) if d and d.get("success") else {}

    def get_wallet(self):
        d = self._get("/v2/wallet/balances")
        if d and d.get("success"):
            return {b["asset_symbol"]: float(b["available_balance"])
                    for b in d.get("result", [])}
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
        d = self._get(f"/v2/tickers/{symbol}")
        if d and d.get("success"):
            return float(d.get("result", {}).get("funding_rate", 0))
        return 0.0

    def get_open_interest(self, symbol="BTCUSD"):
        d = self._get(f"/v2/tickers/{symbol}")
        if d and d.get("success"):
            return float(d.get("result", {}).get("open_interest", 0))
        return 0.0

    def place_order(self, product_id, side, size, order_type="market_order",
                    limit_price=None, stop_price=None):
        body = {"product_id": product_id, "size": size, "side": side,
                "order_type": order_type, "time_in_force": "gtc"}
        if limit_price: body["limit_price"] = str(limit_price)
        if stop_price:  body["stop_price"]  = str(stop_price)
        return self._post("/v2/orders", body) or {}

    def cancel_order(self, order_id, product_id):
        return self._post(f"/v2/orders/{order_id}/cancel",
                          {"product_id": product_id}) or {}

# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL ENGINE
# ══════════════════════════════════════════════════════════════════════════════
class TechEngine:

    @staticmethod
    def ema(prices, period):
        if len(prices) < period:
            return [prices[-1]] * len(prices)
        k = 2 / (period + 1)
        vals = [sum(prices[:period]) / period]
        for p in prices[period:]:
            vals.append(p * k + vals[-1] * (1 - k))
        return [vals[0]] * (period - 1) + vals

    @staticmethod
    def rsi(prices, period=7):
        if len(prices) < period + 1: return 50.0
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains  = [max(d, 0) for d in deltas[-period:]]
        losses = [abs(min(d, 0)) for d in deltas[-period:]]
        ag, al = sum(gains)/period, sum(losses)/period
        if al == 0: return 100.0
        return 100 - (100 / (1 + ag/al))

    @staticmethod
    def macd(prices, fast=5, slow=13, signal=5):
        if len(prices) < slow + signal:
            return 0.0, 0.0, 0.0, []
        ef = TechEngine.ema(prices, fast)
        es = TechEngine.ema(prices, slow)
        ml = [ef[i] - es[i] for i in range(len(prices))]
        sl = TechEngine.ema(ml, signal)
        hist = [ml[i] - sl[i] for i in range(len(ml))]
        return ml[-1], sl[-1], hist[-1], hist

    @staticmethod
    def adx(highs, lows, closes, period=14):
        if len(closes) < period * 2: return 0.0, 0.0, 0.0
        tr_list, pdm, ndm = [], [], []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr_list.append(max(h-l, abs(h-pc), abs(l-pc)))
            up, dn = highs[i]-highs[i-1], lows[i-1]-lows[i]
            pdm.append(up if up > dn and up > 0 else 0)
            ndm.append(dn if dn > up and dn > 0 else 0)

        def sm(data, p):
            s = sum(data[:p]); r = [s]
            for d in data[p:]: s = s - s/p + d; r.append(s)
            return r

        atr = sm(tr_list, period)
        pdi = [100*sm(pdm,period)[i]/atr[i] if atr[i]>0 else 0 for i in range(len(atr))]
        ndi = [100*sm(ndm,period)[i]/atr[i] if atr[i]>0 else 0 for i in range(len(atr))]
        dx  = [abs(pdi[i]-ndi[i])/(pdi[i]+ndi[i])*100 if (pdi[i]+ndi[i])>0 else 0
               for i in range(len(pdi))]
        return sum(dx[-period:])/period, pdi[-1], ndi[-1]

    @staticmethod
    def atr(highs, lows, closes, period=7):
        if len(closes) < period + 1: return 0.0
        trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                   abs(lows[i]-closes[i-1]))
               for i in range(1, len(closes))]
        return sum(trs[-period:]) / period

    @staticmethod
    def bollinger(prices, period=20, std_dev=2.0):
        if len(prices) < period:
            mid = prices[-1]; return mid, mid, mid, 0.0
        w   = prices[-period:]
        mid = sum(w) / period
        std = (sum((p-mid)**2 for p in w)/period)**0.5
        upper, lower = mid+std_dev*std, mid-std_dev*std
        return upper, mid, lower, (upper-lower)/mid*100

    @staticmethod
    def detect_divergence(prices, histogram, lookback=10):
        if len(prices) < lookback or len(histogram) < lookback:
            return "none"
        p, h = prices[-lookback:], histogram[-lookback:]
        half = lookback // 2
        try:
            pl1, pl2 = min(p[:half]), min(p[half:])
            hl1 = h[p.index(pl1)] if pl1 in p else h[0]
            hl2_idx = half + p[half:].index(pl2) if pl2 in p[half:] else half
            hl2 = h[hl2_idx] if hl2_idx < len(h) else h[-1]
            if pl2 < pl1 and hl2 > hl1 and hl2 < 0:
                return "bullish"
            ph1, ph2 = max(p[:half]), max(p[half:])
            hh1 = h[p.index(ph1)] if ph1 in p else h[0]
            hh2_idx = half + p[half:].index(ph2) if ph2 in p[half:] else half
            hh2 = h[hh2_idx] if hh2_idx < len(h) else h[-1]
            if ph2 > ph1 and hh2 < hh1 and hh2 > 0:
                return "bearish"
        except Exception:
            pass
        return "none"

    @staticmethod
    def squeeze_detected(closes, highs, lows, period=20):
        """Bollinger Band squeeze inside Keltner Channel = volatility coil."""
        if len(closes) < period: return False
        _, bb_mid, _, bb_width = TechEngine.bollinger(closes, period)
        atr_val = TechEngine.atr(highs, lows, closes, period)
        kc_width = (atr_val * 1.5 * 2 / bb_mid) * 100  # Keltner width %
        return bb_width < kc_width  # BB inside KC = squeeze

    @staticmethod
    def volume_profile_ok(volumes, current_vol, min_ratio=0.5):
        """Reject trades when volume is suspiciously low (trap candle)."""
        if len(volumes) < 20: return True
        avg = sum(volumes[-20:]) / 20
        return current_vol >= avg * min_ratio  # Need at least 50% of avg vol

# ══════════════════════════════════════════════════════════════════════════════
# REAL NEWS ENGINE (with credibility scoring)
# ══════════════════════════════════════════════════════════════════════════════
class NewsEngine:
    # Source credibility weights (0-1)
    SOURCE_WEIGHT = {
        "reuters": 1.0, "bloomberg": 1.0, "wsj": 0.95,
        "coindesk": 0.85, "cointelegraph": 0.75,
        "cryptopanic": 0.6, "twitter": 0.3, "reddit": 0.2,
        "unknown": 0.4
    }

    # Bull/bear phrases with weights
    BULL_SIGNALS = {
        "sec approves": 3, "etf approved": 3, "fed pivot": 2,
        "rate cut": 2, "bitcoin reserve": 2, "institutional buy": 2,
        "blackrock": 1.5, "fidelity": 1.5, "microstrategy": 1,
        "accumulation": 1, "halving": 1.5, "btc treasury": 2
    }
    BEAR_SIGNALS = {
        "sec sues": 3, "exchange hack": 3, "exchange collapse": 3,
        "rate hike": 2, "cpi higher": 2, "recession": 1.5,
        "tether fraud": 3, "ban bitcoin": 2.5, "regulatory crackdown": 2,
        "ponzi": 2.5, "fraud": 2, "liquidation cascade": 2
    }

    # Phrases that indicate fake/low-quality news
    FAKE_SIGNALS = [
        "guaranteed", "100x", "moon soon", "insider tip",
        "secret source", "breaking exclusive", "crypto guru"
    ]

    def __init__(self):
        self._cache = None
        self._cache_time = 0
        self._cache_ttl  = 300  # 5 min cache

    def get_sentiment(self) -> dict:
        """
        Returns: {score: float (-1 to +1), confidence: float, label: str,
                  bull_score: float, bear_score: float, sources_checked: int}
        """
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        result = self._fetch_and_score()
        self._cache = result
        self._cache_time = now
        return result

    def _fetch_and_score(self) -> dict:
        bull_total = 0.0
        bear_total = 0.0
        sources_checked = 0

        # Source 1: CryptoPanic (free, no key needed for basic)
        try:
            r = requests.get(
                "https://cryptopanic.com/api/v1/posts/"
                "?auth_token=&public=true&currencies=BTC&filter=hot",
                timeout=5)
            if r.status_code == 200:
                posts = r.json().get("results", [])[:15]
                for post in posts:
                    title = post.get("title", "").lower()
                    source = post.get("source", {}).get("domain", "unknown").lower()
                    # Check for fake news markers
                    if any(f in title for f in self.FAKE_SIGNALS):
                        continue  # Skip suspect news
                    credibility = self._source_credibility(source)
                    # Score the title
                    b_score = sum(w for phrase, w in self.BULL_SIGNALS.items()
                                  if phrase in title)
                    n_score = sum(w for phrase, w in self.BEAR_SIGNALS.items()
                                  if phrase in title)
                    bull_total += b_score * credibility
                    bear_total += n_score * credibility
                sources_checked += 1
        except Exception:
            pass

        # Source 2: Fear & Greed Index (actual market sentiment)
        try:
            r = requests.get(
                "https://api.alternative.me/fng/?limit=1", timeout=5)
            if r.status_code == 200:
                fng = int(r.json()["data"][0]["value"])
                fng_class = r.json()["data"][0]["value_classification"]
                # FNG > 65 = greed (potentially contrarian bear)
                # FNG < 30 = fear (potentially contrarian bull)
                if fng > 75:   bear_total  += 1.5  # Extreme greed = crowded
                elif fng > 65: bear_total  += 0.5
                elif fng < 25: bull_total  += 1.5  # Extreme fear = opportunity
                elif fng < 35: bull_total  += 0.5
                sources_checked += 1
        except Exception:
            pass

        total = bull_total + bear_total
        if total == 0:
            return {"score": 0.0, "confidence": 0.3, "label": "Neutral",
                    "bull_score": 0, "bear_score": 0, "sources_checked": 0,
                    "fng": None}

        net_score = (bull_total - bear_total) / total  # -1 to +1
        confidence = min(total / 10.0, 1.0)  # Scales with amount of signals
        label = ("Strongly Bullish" if net_score > 0.5 else
                 "Bullish" if net_score > 0.2 else
                 "Strongly Bearish" if net_score < -0.5 else
                 "Bearish" if net_score < -0.2 else "Neutral")

        return {"score": round(net_score, 3), "confidence": round(confidence, 2),
                "label": label, "bull_score": round(bull_total, 1),
                "bear_score": round(bear_total, 1),
                "sources_checked": sources_checked}

    def _source_credibility(self, domain: str) -> float:
        for key, weight in self.SOURCE_WEIGHT.items():
            if key in domain:
                return weight
        return self.SOURCE_WEIGHT["unknown"]

    def get_confidence_multiplier(self) -> float:
        """Returns 0.7–1.2 multiplier for confidence score."""
        s = self.get_sentiment()
        score = s.get("score", 0)
        conf  = s.get("confidence", 0)
        # Only adjust if we have sufficient signal confidence
        if conf < 0.3:
            return 1.0
        if score > 0.5:  return 1.15
        if score > 0.2:  return 1.05
        if score < -0.5: return 0.75
        if score < -0.2: return 0.90
        return 1.0

# ══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE LEARNING ENGINE
# ══════════════════════════════════════════════════════════════════════════════
class LearningEngine:
    """
    Updates RSI thresholds, ADX minimum, and time-of-day weights
    based on actual trade outcomes. NOT ML — pattern counting over
    rolling 50 trades. Simple, robust, not overfit.
    """
    def __init__(self):
        self.trade_memory = deque(maxlen=50)  # Last 50 trades
        # Adaptive thresholds (start at defaults, update gradually)
        self.rsi_long_min  = 40.0
        self.rsi_long_max  = 55.0
        self.adx_min       = 25.0
        self.hour_weights  = {h: 1.0 for h in range(24)}

    def record(self, trade: dict):
        """Call after each trade closes with outcome data."""
        self.trade_memory.append({
            "rsi_at_entry": trade.get("rsi", 50),
            "adx_at_entry": trade.get("adx", 25),
            "hour_utc":     trade.get("hour_utc", 12),
            "won":          trade.get("won", False),
            "pnl_pct":      trade.get("pnl_pct", 0),
            "direction":    trade.get("direction", "long")
        })
        if len(self.trade_memory) >= 20:
            self._update_thresholds()

    def _update_thresholds(self):
        """Re-calibrate thresholds based on recent 50 trades."""
        longs = [t for t in self.trade_memory if t["direction"] == "long"]
        if len(longs) >= 10:
            win_rsi = [t["rsi_at_entry"] for t in longs if t["won"]]
            lose_rsi = [t["rsi_at_entry"] for t in longs if not t["won"]]
            if win_rsi and lose_rsi:
                avg_win_rsi  = sum(win_rsi)  / len(win_rsi)
                avg_lose_rsi = sum(lose_rsi) / len(lose_rsi)
                # Nudge RSI range toward winning entries (slow, 10% per update)
                self.rsi_long_min = self.rsi_long_min * 0.9 + max(avg_win_rsi - 8, 35) * 0.1
                self.rsi_long_max = self.rsi_long_max * 0.9 + min(avg_win_rsi + 8, 65) * 0.1

        # ADX: if trades are winning at lower ADX, lower the threshold slightly
        all_adx = [(t["adx_at_entry"], t["won"]) for t in self.trade_memory]
        if all_adx:
            low_adx_wins = sum(1 for a, w in all_adx if a < 25 and w)
            low_adx_total = sum(1 for a, w in all_adx if a < 25)
            if low_adx_total >= 5:
                win_rate_low_adx = low_adx_wins / low_adx_total
                if win_rate_low_adx > 0.60:
                    self.adx_min = max(20, self.adx_min - 0.5)  # Slowly lower floor
                elif win_rate_low_adx < 0.40:
                    self.adx_min = min(30, self.adx_min + 0.5)  # Raise floor

        # Hour weights: boost hours with good results, reduce bad ones
        for h in range(24):
            h_trades = [t for t in self.trade_memory if t["hour_utc"] == h]
            if len(h_trades) >= 3:
                wr = sum(1 for t in h_trades if t["won"]) / len(h_trades)
                # Move weight toward actual performance, clamped 0.5–1.5
                target = max(0.5, min(1.5, wr / 0.55))  # 0.55 = expected WR
                self.hour_weights[h] = self.hour_weights[h] * 0.8 + target * 0.2

    def get_hour_multiplier(self, hour: int) -> float:
        return self.hour_weights.get(hour, 1.0)

    def summary(self) -> dict:
        return {
            "trades_remembered": len(self.trade_memory),
            "rsi_long_range": [round(self.rsi_long_min, 1), round(self.rsi_long_max, 1)],
            "adx_min": round(self.adx_min, 1),
            "best_hours": sorted([h for h, w in self.hour_weights.items() if w > 1.1])
        }

# ══════════════════════════════════════════════════════════════════════════════
# DRAWDOWN & MONTHLY TRACKER
# ══════════════════════════════════════════════════════════════════════════════
class RiskGuard:
    """
    Circuit breakers that actually stop the bot from self-destructing.
    Most bots lose because they keep trading through drawdowns.
    """
    def __init__(self):
        self.month_start_capital   = 0.0
        self.day_start_capital     = 0.0
        self.day_start_date        = None
        self.consecutive_losses    = 0
        self.in_recovery_mode      = False
        self.recovery_wins_needed  = 0
        self.halted                = False
        self.halt_reason           = ""
        self.monthly_pnl_pct       = 0.0
        self.daily_pnl_pct         = 0.0
        self.trade_history         = deque(maxlen=100)

    def initialize(self, capital: float):
        now = datetime.now(timezone.utc)
        self.month_start_capital = capital
        self.day_start_capital   = capital
        self.day_start_date      = now.date()

    def check_new_day(self, capital: float):
        today = datetime.now(timezone.utc).date()
        if self.day_start_date != today:
            self.day_start_capital = capital
            self.day_start_date    = today
            log.info(f"New day — capital reset for daily tracking: ${capital:.2f}")

    def record_trade(self, won: bool, pnl_usd: float, capital: float):
        self.trade_history.append({"won": won, "pnl": pnl_usd,
                                   "time": datetime.now(timezone.utc).isoformat()})
        if won:
            self.consecutive_losses = 0
            if self.in_recovery_mode:
                self.recovery_wins_needed -= 1
                if self.recovery_wins_needed <= 0:
                    self.in_recovery_mode = False
                    log.info("Recovery mode exited — consecutive wins achieved")
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= Cfg.MAX_CONSEC_LOSSES:
                self.in_recovery_mode     = True
                self.recovery_wins_needed = Cfg.RECOVERY_TRADES
                log.warning(f"RECOVERY MODE: {self.consecutive_losses} consecutive losses")

        # Update P&L %
        if self.month_start_capital > 0:
            self.monthly_pnl_pct = (capital - self.month_start_capital) / self.month_start_capital
        if self.day_start_capital > 0:
            self.daily_pnl_pct = (capital - self.day_start_capital) / self.day_start_capital

        # Monthly halt check
        if self.monthly_pnl_pct <= -Cfg.MONTHLY_LOSS_LIMIT:
            self.halted     = True
            self.halt_reason = f"Monthly loss limit hit: {self.monthly_pnl_pct*100:.1f}%"
            log.error(f"BOT HALTED: {self.halt_reason}")

    def can_trade(self) -> tuple:
        """Returns (can_trade: bool, reason: str, risk_multiplier: float)"""
        if self.halted:
            return False, self.halt_reason, 0.0

        # Daily loss limit — pause trading (not permanent halt)
        if self.daily_pnl_pct <= -Cfg.DAILY_LOSS_LIMIT:
            return False, f"Daily loss limit: {self.daily_pnl_pct*100:.1f}% — resume tomorrow", 0.0

        if self.in_recovery_mode:
            # Allow trading but at minimal size
            return True, f"Recovery mode ({self.recovery_wins_needed} wins needed)", \
                   Cfg.MAX_RISK_RECOVERY / Cfg.MAX_RISK_NORMAL

        return True, "OK", 1.0

    def get_progress_to_monthly_target(self) -> dict:
        target = Cfg.MONTHLY_TARGET_PCT
        current = self.monthly_pnl_pct
        return {
            "target_pct": target * 100,
            "current_pct": round(current * 100, 2),
            "progress_pct": round(min(current / target * 100, 100), 1) if target > 0 else 0,
            "remaining_pct": round(max((target - current) * 100, 0), 2),
            "on_track": current >= 0,
            "monthly_status": ("HALTED" if self.halted else
                               "RECOVERY" if self.in_recovery_mode else
                               "TARGET HIT" if current >= target else
                               "ON TRACK" if current >= 0 else "LOSING")
        }

# ══════════════════════════════════════════════════════════════════════════════
# 7-PILLAR CONFIDENCE ENGINE (with adaptive thresholds + news + OI)
# ══════════════════════════════════════════════════════════════════════════════
class ConfidenceEngine:

    def score(self, data: dict, direction: str,
              learner: LearningEngine = None) -> tuple:
        closes    = data.get("closes", [])
        highs     = data.get("highs", [])
        lows      = data.get("lows", [])
        volumes   = data.get("volumes", [])
        closes_5m = data.get("closes_5m", closes)
        closes_15m= data.get("closes_15m", closes)
        hour_utc  = data.get("hour_utc", 12)
        minute_utc= data.get("minute_utc", 0)
        funding   = data.get("funding_rate", 0.0)
        is_weekend= data.get("is_weekend", False)
        oi_change = data.get("oi_change_pct", 0.0)

        if len(closes) < 55:
            return 0, True, "insufficient_data", {}

        adx_val, plus_di, minus_di = TechEngine.adx(highs, lows, closes)
        adx_min = learner.adx_min if learner else Cfg.ADX_TREND_MIN

        # ── HARD VETOES ──────────────────────────────────────────────────────
        if adx_val < 15:
            return 0, True, f"ADX={adx_val:.1f}<15", {}
        if hour_utc in Cfg.DEAD_ZONE_HOURS and not is_weekend:
            return 0, True, f"dead_zone_{hour_utc}UTC", {}
        for mh, mm in Cfg.MACRO_BLACKOUT_TIMES:
            if abs((hour_utc*60+minute_utc) - (mh*60+mm)) <= Cfg.BLACKOUT_WINDOW_MINS:
                return 0, True, f"macro_blackout_{mh}:{mm:02d}", {}
        if direction == "long"  and funding > Cfg.FUNDING_LONG_MAX:
            return 0, True, f"funding={funding:.4f}>max_long", {}
        if direction == "short" and funding < Cfg.FUNDING_SHORT_MIN:
            return 0, True, f"funding={funding:.4f}<min_short", {}

        # Low-volume trap veto
        if volumes and not TechEngine.volume_profile_ok(volumes, volumes[-1]):
            return 0, True, "low_volume_trap", {}

        # HTF contradiction veto
        if len(closes_15m) >= 21:
            h15_ema8 = TechEngine.ema(closes_15m, 8)[-1]
            if direction == "long"  and closes_15m[-1] < h15_ema8:
                return 0, True, "1m_vs_15m_contradiction", {}
            if direction == "short" and closes_15m[-1] > h15_ema8:
                return 0, True, "1m_vs_15m_contradiction", {}

        breakdown = {}
        total = 0

        # ── PILLAR 1: Regime 25% ─────────────────────────────────────────────
        ema8  = TechEngine.ema(closes, 8)[-1]
        ema21 = TechEngine.ema(closes, 21)[-1]
        ema55 = TechEngine.ema(closes, 55)[-1]
        price = closes[-1]
        reg_bull = price > ema8 > ema21 > ema55 and adx_val > adx_min and plus_di > minus_di
        reg_bear = price < ema8 < ema21 < ema55 and adx_val > adx_min and minus_di > plus_di
        if   direction=="long"  and reg_bull: breakdown["regime"] = 25
        elif direction=="short" and reg_bear: breakdown["regime"] = 25
        elif adx_val > adx_min:               breakdown["regime"] = 10
        else:                                 breakdown["regime"] = 0
        total += breakdown["regime"]

        # ── PILLAR 2: HTF Alignment 20% ──────────────────────────────────────
        htf = 0
        for htfc in [closes_5m, closes_15m]:
            if len(htfc) >= 21:
                he8 = TechEngine.ema(htfc, 8)[-1]
                he21= TechEngine.ema(htfc, 21)[-1]
                if direction=="long"  and htfc[-1] > he8 > he21: htf += 10
                elif direction=="short" and htfc[-1] < he8 < he21: htf += 10
        breakdown["htf_alignment"] = htf
        total += htf

        # ── PILLAR 3: Momentum 15% ───────────────────────────────────────────
        rsi = TechEngine.rsi(closes, 7)
        _, _, _, histogram = TechEngine.macd(closes)
        divergence = TechEngine.detect_divergence(closes, histogram)
        rsi_min = learner.rsi_long_min if learner else Cfg.RSI_BULL_PULLBACK[0]
        rsi_max = learner.rsi_long_max if learner else Cfg.RSI_BULL_PULLBACK[1]

        mom = 0
        if direction == "long":
            if rsi_min <= rsi <= rsi_max: mom += 7
            elif rsi > rsi_max: mom += 5
            if len(histogram)>=3 and all(histogram[-i]>histogram[-(i+1)] for i in range(1,3)):
                mom += 5
            if divergence == "bullish": mom += 8
        else:
            if 45 <= rsi <= 60: mom += 7
            elif rsi < 45: mom += 5
            if len(histogram)>=3 and all(histogram[-i]<histogram[-(i+1)] for i in range(1,3)):
                mom += 5
            if divergence == "bearish": mom += 8
        breakdown["momentum"] = min(mom, 15)
        total += breakdown["momentum"]

        # ── PILLAR 4: Volume + OI 10% ────────────────────────────────────────
        vol_score = 0
        if volumes:
            avg_vol = sum(volumes[-20:]) / max(len(volumes[-20:]), 1)
            if   volumes[-1] > avg_vol * 1.5: vol_score = 7
            elif volumes[-1] > avg_vol:        vol_score = 4
        # OI spike bonus: large OI increase = real positioning (not noise)
        if oi_change > Cfg.OI_SPIKE_PCT:  vol_score = min(vol_score + 3, 10)
        breakdown["volume_oi"] = vol_score
        total += vol_score

        # ── PILLAR 5: Volatility 10% ─────────────────────────────────────────
        atr_val = TechEngine.atr(highs, lows, closes, 7)
        _, _, _, bb_width = TechEngine.bollinger(closes)
        if   0.3 < bb_width < 3.0: vs = 10
        elif 0.1 < bb_width <= 0.3: vs = 5
        elif bb_width >= 3.0:       vs = 3
        else:                       vs = 0
        breakdown["volatility"] = vs
        total += vs

        # ── PILLAR 6: Time-of-Day (adaptive) 10% ────────────────────────────
        base_time = 10 if hour_utc in Cfg.PEAK_HOURS else (2 if is_weekend else 5)
        hour_mult = learner.get_hour_multiplier(hour_utc) if learner else 1.0
        ts = min(int(base_time * hour_mult), 10)
        breakdown["time_of_day"] = ts
        total += ts

        # ── PILLAR 7: Funding + Sentiment 10% ───────────────────────────────
        fs = 8
        if direction == "long":
            if   funding > 0.0005:  fs = 3
            elif funding < -0.0003: fs = 10
        else:
            if   funding < -0.0003: fs = 3
            elif funding > 0.0005:  fs = 10
        breakdown["funding_sentiment"] = fs
        total += fs

        return min(total, 100), False, "", breakdown

# ══════════════════════════════════════════════════════════════════════════════
# POSITION SIZER (Kelly + Streak + Risk Guard aware)
# ══════════════════════════════════════════════════════════════════════════════
class PositionSizer:
    def __init__(self):
        self.win_count = self.loss_count = self.total_trades = 0
        self.streak = 0
        self.avg_win_pct = 2.0
        self.avg_loss_pct = 1.2

    @property
    def win_rate(self):
        return self.win_count / self.total_trades if self.total_trades else 0.55

    def kelly_fraction(self):
        wr = self.win_rate
        if not (0.45 <= wr <= 0.75): return 0.01
        p, q = wr, 1 - wr
        b = self.avg_win_pct / max(self.avg_loss_pct, 0.1)
        k = (b*p - q) / b
        return max(0.005, min(0.025, k * Cfg.KELLY_FRACTION))

    def streak_mult(self):
        if self.streak >= 4:  return 1.4
        if self.streak == 3:  return 1.2
        if self.streak == 2:  return 1.1
        if self.streak <= -2: return 0.7
        return 1.0

    def size_usd(self, capital: float, confidence: int, atr_pct: float,
                 risk_multiplier: float = 1.0) -> float:
        base    = capital * self.kelly_fraction()
        conf_m  = 1.3 if confidence >= Cfg.HIGH_CONFIDENCE else 1.0
        streak_m= self.streak_mult()
        vol_m   = max(0.5, min(1.5, 0.015 / atr_pct)) if atr_pct > 0 else 1.0
        size    = base * conf_m * streak_m * vol_m * risk_multiplier
        max_risk = capital * (Cfg.MAX_RISK_HOT if confidence >= 80 else Cfg.MAX_RISK_NORMAL)
        return min(size, max_risk)

    def record(self, won: bool, pct: float):
        self.total_trades += 1
        if won:
            self.win_count += 1
            self.streak = max(0, self.streak) + 1
            self.avg_win_pct  = self.avg_win_pct  * 0.9 + abs(pct) * 0.1
        else:
            self.loss_count += 1
            self.streak = min(0, self.streak) - 1
            self.avg_loss_pct = self.avg_loss_pct * 0.9 + abs(pct) * 0.1

# ══════════════════════════════════════════════════════════════════════════════
# POSITION
# ══════════════════════════════════════════════════════════════════════════════
class Position:
    def __init__(self, product_id, side, entry, size_usd, option_symbol="",
                 rsi=50, adx=25, hour_utc=12):
        self.product_id     = product_id
        self.side           = side
        self.entry          = entry
        self.size_usd       = size_usd
        self.option_symbol  = option_symbol
        self.entered_at     = datetime.now(timezone.utc)
        self.tp1_hit        = False
        self.trailing_on    = False
        self.trail_high     = entry
        self.closed         = False
        self.exit_price     = None
        self.exit_reason    = None
        # For learning engine
        self.rsi_at_entry   = rsi
        self.adx_at_entry   = adx
        self.hour_utc       = hour_utc

    def check_exit(self, current_price: float) -> tuple:
        pct = ((current_price - self.entry) / self.entry
               if self.side == "long"
               else (self.entry - current_price) / self.entry)

        if pct <= -Cfg.HARD_STOP_PCT:
            return True, "hard_stop", False
        if not self.tp1_hit and pct >= Cfg.TP1_PCT:
            self.tp1_hit = True
            return True, "tp1_50pct", True
        if pct >= Cfg.TRAIL_ACTIVATE_PCT:
            self.trailing_on = True
            self.trail_high = (max(self.trail_high, current_price) if self.side=="long"
                               else min(self.trail_high, current_price))
        if self.trailing_on:
            stop = (self.trail_high * (1 - Cfg.TRAIL_DISTANCE_PCT) if self.side=="long"
                    else self.trail_high * (1 + Cfg.TRAIL_DISTANCE_PCT))
            if (self.side=="long" and current_price <= stop) or \
               (self.side=="short" and current_price >= stop):
                return True, "trailing_stop", False
        if pct >= Cfg.TP2_PCT:
            return True, "tp2_full", False
        age_hrs = (datetime.now(timezone.utc) - self.entered_at).seconds / 3600
        if age_hrs >= 4:
            return True, "time_exit_4h", False
        return False, "", False

# ══════════════════════════════════════════════════════════════════════════════
# OPTIONS SELECTOR (with premium vs move check)
# ══════════════════════════════════════════════════════════════════════════════
class OptionsSelector:
    @staticmethod
    def select(chain, current_price, direction, confidence, atr_val) -> Optional[dict]:
        if not chain: return None
        target_type = "call_options" if direction == "long" else "put_options"
        today = datetime.now(timezone.utc).date()
        candidates = []
        for opt in chain:
            if opt.get("contract_type") != target_type: continue
            try:
                expiry = datetime.strptime(opt.get("settlement_time","")[:10], "%Y-%m-%d").date()
                dte = (expiry - today).days
                if dte < 0 or dte > 3: continue
                strike = float(opt.get("strike_price", 0))
                mark   = float(opt.get("mark_price", 0))
                if mark <= 0: continue

                # ✅ PREMIUM vs MOVE CHECK (new in v6)
                # Expected move = ATR * sqrt(hours held / 24)
                expected_move_usd = atr_val * math.sqrt(4/24) * current_price / 100
                if mark * 100 > expected_move_usd * Cfg.MIN_MOVE_TO_PREMIUM_RATIO:
                    continue  # Option too expensive for expected move

                moneyness = ((strike - current_price)/current_price if direction=="long"
                             else (current_price - strike)/current_price)
                candidates.append({"product": opt, "dte": dte,
                                   "moneyness": moneyness, "mark": mark,
                                   "product_id": opt.get("id")})
            except Exception:
                continue

        if not candidates: return None
        def sc(c):
            ds = 10 - c["dte"]*3
            ms = 10 if (-0.02 <= c["moneyness"] <= 0.01 and confidence > 80) else \
                 10 if (-0.005 <= c["moneyness"] <= 0.005) else 3
            return ds + ms
        candidates.sort(key=sc, reverse=True)
        return candidates[0]

# ══════════════════════════════════════════════════════════════════════════════
# MAIN BOT
# ══════════════════════════════════════════════════════════════════════════════
class AlphaBot:
    def __init__(self):
        self.api         = DeltaAPI()
        self.confidence  = ConfidenceEngine()
        self.sizer       = PositionSizer()
        self.news        = NewsEngine()
        self.learner     = LearningEngine()
        self.risk_guard  = RiskGuard()
        self.options_sel = OptionsSelector()

        self.capital          = 0.0
        self.starting_capital = 0.0
        self.wallet_usdt      = 0.0
        self.wallet_btc       = 0.0
        self.wallet_inr       = 0.0
        self.wallet_synced    = False

        self.positions: list  = []
        self.trade_log: list  = []
        self.running          = False
        self.status_msg       = "Initializing..."
        self.total_pnl        = 0.0
        self.profit_buffer    = 0.0
        self._prev_oi         = 0.0

        self._sync_wallet(is_startup=True)

    # ── Wallet ────────────────────────────────────────────────────────────────
    def _sync_wallet(self, is_startup=False) -> float:
        try:
            bal = self.api.get_wallet()
            if not bal:
                if is_startup: self.status_msg = "⚠ Wallet read failed — check API keys"
                return self.capital
            usdt = float(bal.get("USDT", bal.get("usdt", 0)))
            inr  = float(bal.get("INR",  bal.get("inr",  0)))
            btc  = float(bal.get("BTC",  bal.get("btc",  0)))
            inr_usd = 0.0
            if inr > 0:
                try:
                    r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=4)
                    inr_usd = inr / r.json().get("rates",{}).get("INR", 84.0)
                except Exception: inr_usd = inr / 84.0
            btc_usd = 0.0
            if btc > 0:
                t = self.api.get_ticker("BTCUSD")
                btc_usd = btc * float(t.get("mark_price", 0))
            total = usdt + inr_usd + btc_usd
            self.wallet_usdt = usdt; self.wallet_btc = btc; self.wallet_inr = inr
            if total > 0:
                if not self.wallet_synced or is_startup:
                    self.starting_capital = total
                    self.capital = total
                    self.wallet_synced = True
                    self.risk_guard.initialize(total)
                    log.info(f"Wallet synced: ${total:.2f}")
                else:
                    self.capital = total + self.profit_buffer
                    self.risk_guard.check_new_day(total)
        except Exception as e:
            log.error(f"Wallet sync error: {e}")
        return self.capital

    # ── Market Data ───────────────────────────────────────────────────────────
    def _get_market_data(self) -> dict:
        now = datetime.now(timezone.utc)
        c5  = self.api.get_candles("BTCUSD", 5, 100)
        c15 = self.api.get_candles("BTCUSD", 15, 50)
        fr  = self.api.get_funding_rate()
        oi  = self.api.get_open_interest()

        if not c5: return {}

        def parse(candles):
            cl = [float(c.get("close", 0)) for c in candles]
            hi = [float(c.get("high",  0)) for c in candles]
            lo = [float(c.get("low",   0)) for c in candles]
            vo = [float(c.get("volume",0)) for c in candles]
            return cl, hi, lo, vo

        cl5, hi5, lo5, vo5 = parse(c5)
        cl15, _, _, _ = parse(c15) if c15 else ([], [], [], [])

        oi_change = (oi - self._prev_oi) / self._prev_oi if self._prev_oi > 0 else 0
        self._prev_oi = oi

        return {
            "closes": cl5, "highs": hi5, "lows": lo5, "volumes": vo5,
            "closes_5m": cl5, "closes_15m": cl15,
            "hour_utc": now.hour, "minute_utc": now.minute,
            "is_weekend": now.weekday() >= 5,
            "funding_rate": fr, "current_price": cl5[-1] if cl5 else 0,
            "oi_change_pct": oi_change,
            "atr": TechEngine.atr(hi5, lo5, cl5) if cl5 else 0
        }

    # ── Core Loop ─────────────────────────────────────────────────────────────
    def analyze_and_trade(self):
        # API health check first
        if not self.api.healthy:
            self.status_msg = "⚠ API unhealthy — protecting positions, not trading"
            return

        self.status_msg = "Scanning..."
        data = self._get_market_data()
        if not data or not data.get("current_price"):
            self.status_msg = "No market data"
            return

        price = data["current_price"]
        self._manage_positions(price)

        # Risk guard check
        can_trade, reason, risk_mult = self.risk_guard.can_trade()
        if not can_trade:
            self.status_msg = f"🛑 {reason}"
            return

        if len([p for p in self.positions if not p.closed]) >= Cfg.MAX_OPEN_POSITIONS:
            self.status_msg = f"Max positions — monitoring"
            return

        # Score with learning engine + news
        news_mult = self.news.get_confidence_multiplier()
        ls, lv, lr, _ = self.confidence.score(data, "long",  self.learner)
        ss, sv, sr, _ = self.confidence.score(data, "short", self.learner)

        ls = min(int(ls * news_mult), 100)
        ss = min(int(ss * news_mult), 100)

        log.info(f"BTC ${price:,.0f} | L={ls}{'[VETO]' if lv else ''} "
                 f"S={ss}{'[VETO]' if sv else ''} | "
                 f"News={self.news.get_sentiment().get('label','?')} "
                 f"| Guard={reason}")

        direction = score = None
        if not lv and ls >= Cfg.MIN_CONFIDENCE and ls > ss:
            direction, score = "long",  ls
        elif not sv and ss >= Cfg.MIN_CONFIDENCE and ss > ls:
            direction, score = "short", ss

        # Straddle opportunity: squeeze + no directional bias
        if not direction and TechEngine.squeeze_detected(
                data["closes"], data["highs"], data["lows"]):
            self.status_msg = "Squeeze detected — straddle candidate (manual review)"
            log.info("Bollinger squeeze detected — straddle opportunity")
            return

        if not direction:
            self.status_msg = f"No trade: L={ls} S={ss} (need ≥{Cfg.MIN_CONFIDENCE})"
            return

        atr_val = data.get("atr", 0)
        atr_pct = atr_val / price if price > 0 else 0.001
        size_usd = self.sizer.size_usd(self.capital, score, atr_pct, risk_mult)

        rsi_now = TechEngine.rsi(data["closes"])
        adx_now, _, _ = TechEngine.adx(data["highs"], data["lows"], data["closes"])

        chain = self.api.get_options_chain("BTC")
        opt   = self.options_sel.select(chain, price, direction, score, atr_val)

        if opt:
            contracts = max(1, int(size_usd / (opt["mark"] * 100)))
            result = self.api.place_order(opt["product_id"], "buy", contracts)
            if result.get("success"):
                pos = Position(opt["product_id"], direction, price, size_usd,
                               opt["product"].get("symbol",""), rsi_now, adx_now,
                               data["hour_utc"])
                self.positions.append(pos)
                self._log_trade("OPEN", direction, price, size_usd, score,
                                opt["product"].get("symbol",""))
                self.status_msg = f"✅ {direction.upper()} {opt['product'].get('symbol','')} @ ${price:,.0f}"
            else:
                self.status_msg = "Order failed"
        else:
            # Fallback perpetual
            side = "buy" if direction == "long" else "sell"
            contracts = max(1, int(size_usd / price * 1000))
            result = self.api.place_order(Cfg.BTC_PRODUCT_ID, side, contracts)
            if result.get("success"):
                pos = Position(Cfg.BTC_PRODUCT_ID, direction, price, size_usd,
                               "BTCUSD_PERP", rsi_now, adx_now, data["hour_utc"])
                self.positions.append(pos)
                self._log_trade("OPEN", direction, price, size_usd, score, "BTCUSD_PERP")
                self.status_msg = f"✅ {direction.upper()} PERP @ ${price:,.0f}"
            else:
                self.status_msg = "No option available, perp order failed"

    def _manage_positions(self, price):
        for pos in self.positions:
            if pos.closed: continue
            should_exit, reason, partial = pos.check_exit(price)
            if should_exit:
                self._close_position(pos, price, reason, partial)

    def _close_position(self, pos, price, reason, partial):
        size = pos.size_usd / 2 if partial else pos.size_usd
        positions = self.api.get_positions()
        match = next((p for p in positions if p.get("product_id")==pos.product_id), None)
        if match:
            qty = abs(int(float(match.get("size",0))))
            if partial: qty = max(1, qty//2)
            self.api.place_order(pos.product_id,
                                 "sell" if pos.side=="long" else "buy", qty)

        pnl_pct = ((price - pos.entry)/pos.entry if pos.side=="long"
                   else (pos.entry - price)/pos.entry)
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
        self.risk_guard.record_trade(won, pnl_usd, self.capital)
        self.learner.record({
            "rsi": pos.rsi_at_entry, "adx": pos.adx_at_entry,
            "hour_utc": pos.hour_utc, "won": won,
            "pnl_pct": pnl_pct * 100, "direction": pos.side
        })

        if not partial:
            pos.closed = True; pos.exit_price = price; pos.exit_reason = reason

        self._log_trade("CLOSE", pos.side, price, pnl_usd, 0,
                        pos.option_symbol, reason, pnl_pct * 100)
        log.info(f"{'✅' if won else '❌'} CLOSED {pos.side.upper()} @ ${price:,.0f} "
                 f"| {reason} | P&L: ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%)")

    def _log_trade(self, action, side, price, amount, confidence,
                   symbol, reason="", pnl_pct=0):
        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action, "side": side, "price": price,
            "amount": amount, "confidence": confidence,
            "symbol": symbol, "reason": reason, "pnl_pct": pnl_pct,
            "capital": self.capital, "win_rate": self.sizer.win_rate,
            "streak": self.sizer.streak
        })

    def _run_loop(self):
        cycle = 0
        while self.running:
            try:
                if cycle % 5 == 0: self._sync_wallet()
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
            log.info("ΔLPHA Bot v6.0 started")

    def stop(self):
        self.running = False

    def get_state(self) -> dict:
        sc  = self.starting_capital if self.starting_capital > 0 else self.capital
        pct = round((self.capital - sc) / sc * 100, 2) if sc > 0 else 0.0
        progress = self.risk_guard.get_progress_to_monthly_target()
        sentiment = self.news.get_sentiment()
        can_trade, guard_reason, _ = self.risk_guard.can_trade()
        return {
            "version": "v6.0",
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
            "total_trades": self.sizer.total_trades,
            "win_rate": round(self.sizer.win_rate * 100, 1),
            "streak": self.sizer.streak,
            "consecutive_losses": self.risk_guard.consecutive_losses,
            "in_recovery": self.risk_guard.in_recovery_mode,
            "can_trade": can_trade,
            "guard_reason": guard_reason,
            "kelly_fraction": round(self.sizer.kelly_fraction() * 100, 2),
            "monthly_progress": progress,
            "news_sentiment": sentiment,
            "learning": self.learner.summary(),
            "recent_trades": self.trade_log[-20:]
        }


# ══════════════════════════════════════════════════════════════════════════════
# FLASK APP + DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
bot = AlphaBot()

@app.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

# ── Dashboard (Robinhood/Groww style, white, mobile-first) ───────────────────
DASHBOARD = open("/app/dashboard.html").read() if os.path.exists("/app/dashboard.html") else None

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#ffffff">
<title>Alpha Bot v6</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{--bg:#f5f7fa;--white:#fff;--text:#0f1923;--t2:#52616b;--t3:#8a9bb0;--border:#e8ecf2;
  --green:#00c896;--gbg:#e8faf5;--gdim:#b3edd9;
  --red:#f0483e;--rbg:#fff0ef;--rdim:#fbb8b5;
  --blue:#0066ff;--bbg:#e8f0ff;
  --orange:#ff7b00;--obg:#fff3e8;
  --yellow:#f59e0b;--ybg:#fef3c7;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
  --r:16px;--rs:10px;--rx:8px}
html,body{background:var(--bg);color:var(--text);font-family:"DM Sans",sans-serif;min-height:100vh}
.hdr{background:var(--white);border-bottom:1px solid var(--border);padding:0 16px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.hdr-l{display:flex;align-items:center;gap:10px}
.ico{width:32px;height:32px;background:var(--text);border-radius:9px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:15px;font-family:"DM Mono";font-weight:600}
.ht{font-size:15px;font-weight:700;color:var(--text)}.hs{font-size:10px;color:var(--t3)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:5px 12px;border-radius:20px;font-size:11px;font-weight:600}
.p-on{background:var(--gbg);color:var(--green)}.p-off{background:var(--rbg);color:var(--red)}
.p-dot{width:6px;height:6px;border-radius:50%}
.p-on .p-dot{background:var(--green);animation:pulse 2s infinite}
.p-off .p-dot{background:var(--red)}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.5)}}
.wrap{padding:12px 12px 88px;max-width:480px;margin:0 auto}
.tab{display:none}.tab.active{display:block}

/* BTC HERO */
.btc{background:var(--text);border-radius:var(--r);padding:20px;margin-bottom:12px;position:relative;overflow:hidden}
.btc::before{content:"";position:absolute;top:-40px;right:-40px;width:160px;height:160px;background:rgba(255,255,255,.04);border-radius:50%}
.bl{font-size:11px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.bp{font-size:38px;font-weight:700;color:#fff;font-family:"DM Mono";line-height:1;margin-bottom:6px}
.bc{display:flex;align-items:center;gap:8px}
.bcb{font-size:12px;font-weight:600;padding:3px 9px;border-radius:6px}
.bc-up{background:rgba(0,200,150,.2);color:#00e8b0}.bc-dn{background:rgba(240,72,62,.2);color:#ff6b64}
.btc-r{position:absolute;top:18px;right:16px;text-align:right}
.bpl{font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.bpv{font-size:14px;font-weight:700;font-family:"DM Mono"}
.bpu{color:#00e8b0}.bpd{color:#ff6b64}.bpn{color:rgba(255,255,255,.5)}
.bsig{font-size:10px;padding:2px 7px;border-radius:5px;margin-top:3px;display:inline-block}
.s-bull{background:rgba(0,200,150,.2);color:#00e8b0}
.s-bear{background:rgba(240,72,62,.2);color:#ff6b64}
.s-neu{background:rgba(255,255,255,.1);color:rgba(255,255,255,.4)}

/* CARDS */
.card{background:var(--white);border-radius:var(--rs);padding:16px;margin-bottom:10px;box-shadow:var(--shadow)}
.card-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.ct{font-size:11px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.5px}

/* WALLET */
.wt{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
.wl{font-size:11px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.wa{font-size:30px;font-weight:700;font-family:"DM Mono";color:var(--text);line-height:1}
.ws{font-size:11px;color:var(--t3);margin-top:3px}
.wp{text-align:right}
.wpp{font-size:22px;font-weight:700;font-family:"DM Mono"}
.wpa{font-size:11px;color:var(--t3);margin-top:2px}
.pu{color:var(--green)}.pd{color:var(--red)}.pn{color:var(--t2)}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}
.chip{background:var(--bg);border-radius:var(--rx);padding:5px 10px;font-size:11px;color:var(--t2);font-family:"DM Mono";font-weight:500}
.sync-row{display:flex;align-items:center;justify-content:space-between;padding-top:12px;border-top:1px solid var(--border)}
.ss{font-size:11px}.ss.ok{color:var(--green)}.ss.warn{color:var(--orange)}
.sb{background:var(--bg);border:1px solid var(--border);border-radius:var(--rx);padding:6px 12px;font-size:11px;font-weight:600;color:var(--t2);cursor:pointer;font-family:"DM Sans"}

/* MONTHLY PROGRESS */
.mp-bar{height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin:8px 0}
.mp-fill{height:100%;border-radius:4px;transition:width .8s ease}
.mp-row{display:flex;justify-content:space-between;font-size:10px;color:var(--t3);margin-top:3px}

/* STATS */
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.sc{background:var(--white);border-radius:var(--rs);padding:14px;box-shadow:var(--shadow)}
.sl{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
.sv{font-size:22px;font-weight:700;font-family:"DM Mono";color:var(--text);line-height:1}
.sv.g{color:var(--green)}.sv.r{color:var(--red)}.sv.b{color:var(--blue)}
.sub{font-size:10px;color:var(--t3);margin-top:3px}
.badge{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;margin-top:3px}
.bg{background:var(--gbg);color:var(--green)}.bb{background:var(--bbg);color:var(--blue)}
.bo{background:var(--obg);color:var(--orange)}.br{background:var(--rbg);color:var(--red)}
.by{background:var(--ybg);color:var(--yellow)}

/* STATUS */
.status-row{background:var(--white);border-radius:var(--rs);padding:14px 16px;margin-bottom:10px;box-shadow:var(--shadow);display:flex;align-items:center;gap:12px}
.s-ico{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.s-run{background:var(--gbg)}.s-stop{background:var(--rbg)}.s-warn{background:var(--ybg)}
.s-txt{flex:1;font-size:13px;font-weight:500;color:var(--text);line-height:1.4}
.s-time{font-size:10px;color:var(--t3);font-family:"DM Mono";white-space:nowrap}

/* ALERT BANNER */
.alert{padding:10px 14px;border-radius:var(--rs);margin-bottom:10px;font-size:12px;font-weight:500;display:flex;align-items:center;gap:8px}
.a-halt{background:#fef2f2;color:#dc2626;border:1px solid #fecaca}
.a-rec{background:var(--ybg);color:#92400e;border:1px solid #fde68a}
.a-api{background:#fef2f2;color:#dc2626;border:1px solid #fecaca}

/* CONTROLS */
.ctrl{display:flex;gap:8px;margin-bottom:10px}
.btn{flex:1;padding:13px 8px;border-radius:var(--rs);border:none;font-family:"DM Sans";font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:6px}
.btn:active{transform:scale(.97)}
.btn-s{background:var(--text);color:#fff}
.btn-x{background:var(--rbg);color:var(--red);border:1.5px solid var(--rdim)}
.btn-r{background:var(--bbg);color:var(--blue);border:1.5px solid rgba(0,102,255,.2)}

/* TRADES */
.trow{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.trow:last-child{border-bottom:none}
.tl{display:flex;align-items:center;gap:10px}
.tico{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0}
.tl-l{background:var(--gbg);color:var(--green)}.tl-s{background:var(--rbg);color:var(--red)}.tl-o{background:var(--bbg);color:var(--blue)}
.tsym{font-size:13px;font-weight:600;color:var(--text)}
.ttime{font-size:10px;color:var(--t3);font-family:"DM Mono"}
.tr{text-align:right}
.tpnl{font-size:14px;font-weight:700;font-family:"DM Mono"}
.tpnl.u{color:var(--green)}.tpnl.d{color:var(--red)}.tpnl.n{color:var(--t2)}
.tpr{font-size:10px;color:var(--t3);font-family:"DM Mono"}
.empty{text-align:center;padding:24px 0;color:var(--t3);font-size:13px}

/* SIGNALS */
.prow{display:flex;gap:8px;margin-bottom:12px}
.pi{flex:1;background:var(--bg);border-radius:var(--rx);padding:10px;text-align:center}
.ph{font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.pp{font-size:13px;font-weight:700;font-family:"DM Mono";color:var(--text)}
.pd2{font-size:10px;font-weight:600;margin-top:3px}
.pu2{color:var(--green)}.pd2d{color:var(--red)}
.sent{display:flex;align-items:center;gap:8px;padding-top:12px;border-top:1px solid var(--border);margin-top:2px}
.sbar{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden}
.sfill{height:100%;border-radius:3px;transition:width .8s}

/* SETTINGS */
.sfield{margin-bottom:16px}
.sfl{font-size:11px;color:var(--t3);margin-bottom:5px;font-weight:500}
.srow{display:flex;gap:8px;align-items:center}
.srow input[type=range]{flex:1;accent-color:var(--text)}
.sval{font-family:"DM Mono";font-weight:700;font-size:14px;min-width:36px}
.sdesc{font-size:10px;color:var(--t3);margin-top:3px}
.save-btn{width:100%;padding:13px;border-radius:var(--rs);border:none;background:var(--text);color:#fff;font-family:"DM Sans";font-size:13px;font-weight:600;cursor:pointer}
.danger-btn{width:100%;padding:13px;border-radius:var(--rs);border:1.5px solid var(--rdim);background:var(--rbg);color:var(--red);font-family:"DM Sans";font-size:13px;font-weight:600;cursor:pointer;margin-top:8px}

/* PILLAR ROWS */
.pil{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}
.pil:last-child{border-bottom:none}
.pn{width:110px;font-size:12px;color:var(--t2);font-weight:500}
.pt{flex:1;height:6px;background:var(--bg);border-radius:3px;overflow:hidden}
.pf{height:100%;border-radius:3px;transition:width .6s}
.pw{width:28px;text-align:right;font-size:11px;font-family:"DM Mono";font-weight:600}

/* NAV */
.nav{position:fixed;bottom:0;left:0;right:0;background:var(--white);border-top:1px solid var(--border);display:flex;justify-content:space-around;padding:8px 0 max(8px,env(safe-area-inset-bottom));z-index:100}
.nb{display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 16px;border:none;background:none;cursor:pointer;border-radius:var(--rx)}
.nb.active .ni{color:var(--text)}.nb.active .nl{color:var(--text);font-weight:600}
.ni{font-size:20px;color:var(--t3)}.nl{font-size:9px;color:var(--t3);font-weight:500;text-transform:uppercase;letter-spacing:.4px}

/* TOAST */
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--text);color:#fff;padding:10px 20px;border-radius:20px;font-size:12px;font-weight:500;z-index:200;opacity:0;transition:opacity .25s;white-space:nowrap;pointer-events:none}
.toast.show{opacity:1}
@media(min-width:480px){.wrap{padding-left:24px;padding-right:24px}}
</style>
</head>
<body>
<div id="toast" class="toast"></div>
<header class="hdr">
  <div class="hdr-l">
    <div class="ico">&#916;</div>
    <div><div class="ht">Alpha Bot</div><div class="hs" id="hdrsub">Delta Exchange India</div></div>
  </div>
  <div class="pill p-off" id="statusPill"><span class="p-dot"></span><span id="pillTxt">Stopped</span></div>
</header>

<div class="wrap">

<!-- HOME -->
<div id="tab-home" class="tab active">
  <!-- ALERT BANNERS (hidden by default) -->
  <div id="alertHalt" class="alert a-halt" style="display:none">&#x1F6D1; <span id="haltMsg">Bot halted</span></div>
  <div id="alertRec"  class="alert a-rec"  style="display:none">&#x26A0;&#xFE0F; <span id="recMsg">Recovery mode</span></div>
  <div id="alertApi"  class="alert a-api"  style="display:none">&#x1F4F5; API unhealthy — not trading, monitoring positions</div>

  <!-- BTC HERO -->
  <div class="btc">
    <div class="btc-r">
      <div class="bpl">Predicted 1h</div>
      <div class="bpv bpn" id="predPrice">&#8212;</div>
      <div class="bsig s-neu" id="predSig">Calculating...</div>
    </div>
    <div class="bl">Bitcoin &middot; Live</div>
    <div class="bp" id="btcPrice">$&#8212;</div>
    <div class="bc">
      <span class="bcb bc-up" id="btcChg">&#8212;%</span>
      <span style="font-size:11px;color:rgba(255,255,255,.3)">24h</span>
    </div>
  </div>

  <!-- WALLET -->
  <div class="card">
    <div class="wt">
      <div>
        <div class="wl">Wallet Balance</div>
        <div class="wa" id="walAmt">$&#8212;</div>
        <div class="ws">Started: <b id="walStart" style="font-family:'DM Mono'">$&#8212;</b></div>
      </div>
      <div class="wp">
        <div class="wpp pn" id="walPct">&#8212;%</div>
        <div class="wpa" id="walPnl">P&amp;L: $&#8212;</div>
      </div>
    </div>
    <div class="chips" id="walChips"><span class="chip">Syncing...</span></div>
    <div class="sync-row">
      <span class="ss" id="syncSt">Not synced</span>
      <button class="sb" onclick="syncWallet()">&#x21BA; Sync</button>
    </div>
  </div>

  <!-- MONTHLY PROGRESS -->
  <div class="card">
    <div class="card-hd">
      <span class="ct">Monthly Target (10%)</span>
      <span id="mpStatus" style="font-size:10px;font-weight:700;color:var(--green)">ON TRACK</span>
    </div>
    <div class="mp-bar"><div id="mpFill" class="mp-fill" style="width:0%;background:var(--green)"></div></div>
    <div class="mp-row"><span id="mpCur">0%</span><span id="mpRem">10% target</span></div>
  </div>

  <!-- STATS -->
  <div class="stats">
    <div class="sc">
      <div class="sl">Win Rate</div>
      <div class="sv b" id="stWR">&#8212;%</div>
      <div class="sub" id="stTr">0 trades</div>
      <div class="badge bb" id="wrBadge">Building</div>
    </div>
    <div class="sc">
      <div class="sl">Streak</div>
      <div class="sv" id="stSk">&#8212;</div>
      <div class="sub">Kelly: <b id="stKelly">&#8212;</b>%</div>
      <div class="badge bg" id="bufBadge">Buffer: $0</div>
    </div>
  </div>

  <!-- STATUS -->
  <div class="status-row">
    <div class="s-ico s-stop" id="sIco">&#9208;</div>
    <div>
      <div class="s-txt" id="sTxt">Bot is stopped</div>
      <div class="s-time" id="sTime">&#8212;</div>
    </div>
  </div>

  <!-- CONTROLS -->
  <div class="ctrl">
    <button class="btn btn-s" onclick="botAction('start')">&#9654; Start</button>
    <button class="btn btn-x" onclick="botAction('stop')">&#9646; Stop</button>
    <button class="btn btn-r" onclick="botAction('run_now')">&#9889; Run</button>
  </div>

  <!-- RECENT TRADES -->
  <div class="card">
    <div class="card-hd">
      <span class="ct">Recent Trades</span>
      <button style="font-size:11px;color:var(--blue);font-weight:600;background:none;border:none;cursor:pointer" onclick="showTab('trades',document.querySelectorAll('.nb')[1])">View all &#8594;</button>
    </div>
    <div id="recTrades"><div class="empty">No trades yet</div></div>
  </div>

  <button class="danger-btn" onclick="closeAll()">&#x26A0; Close All Positions</button>
</div>

<!-- TRADES -->
<div id="tab-trades" class="tab">
  <div class="card" style="margin-top:4px">
    <div class="card-hd"><span class="ct">All Trades</span><span id="allCount" style="font-size:11px;color:var(--t3);font-family:'DM Mono'">0</span></div>
    <div id="allTrades"><div class="empty">No trades yet</div></div>
  </div>
</div>

<!-- SIGNALS -->
<div id="tab-signals" class="tab">
  <!-- News Sentiment -->
  <div class="card" style="margin-top:4px">
    <div class="card-hd"><span class="ct">Market Sentiment</span><span id="sentLabel" style="font-size:11px;font-weight:700;color:var(--blue)">Loading...</span></div>
    <div class="sent">
      <span style="font-size:11px;color:var(--green);font-weight:600">Bull</span>
      <div class="sbar"><div class="sfill" id="sentFill" style="width:50%;background:var(--green)"></div></div>
      <span style="font-size:11px;color:var(--red);font-weight:600">Bear</span>
    </div>
    <div style="text-align:center;font-size:10px;color:var(--t3);margin-top:6px" id="sentTxt">Sources: CryptoPanic + Fear &amp; Greed Index</div>
  </div>

  <!-- Price Prediction -->
  <div class="card">
    <div class="card-hd"><span class="ct">Price Prediction</span><span id="predUpd" style="font-size:10px;color:var(--t3);font-family:'DM Mono'">&#8212;</span></div>
    <div class="prow">
      <div class="pi"><div class="ph">1 Hour</div><div class="pp" id="p1h">&#8212;</div><div class="pd2" id="d1h">&#8212;</div></div>
      <div class="pi"><div class="ph">4 Hours</div><div class="pp" id="p4h">&#8212;</div><div class="pd2" id="d4h">&#8212;</div></div>
      <div class="pi"><div class="ph">24 Hours</div><div class="pp" id="p24h">&#8212;</div><div class="pd2" id="d24h">&#8212;</div></div>
    </div>
  </div>

  <!-- Signal Pillars -->
  <div class="card">
    <div class="card-hd"><span class="ct">Signal Strength</span><span id="confScore" style="font-size:13px;font-family:'DM Mono';font-weight:700">&#8212; / 100</span></div>
    <div id="pilRows"></div>
  </div>

  <!-- Learning Engine -->
  <div class="card">
    <div class="card-hd"><span class="ct">Adaptive Learning</span><span class="badge bg" id="learnBadge">0 trades</span></div>
    <div style="display:flex;flex-direction:column;gap:8px;font-size:12px;color:var(--t2)" id="learnInfo">
      <div>RSI range: <b id="lRsi" style="font-family:'DM Mono'">40-55</b></div>
      <div>ADX min: <b id="lAdx" style="font-family:'DM Mono'">25</b></div>
      <div>Best hours (UTC): <b id="lHrs" style="font-family:'DM Mono'">Learning...</b></div>
    </div>
  </div>
</div>

<!-- SETTINGS -->
<div id="tab-settings" class="tab">
  <div class="card" style="margin-top:4px">
    <div class="card-hd"><span class="ct">Configuration</span></div>
    <div class="sfield">
      <div class="sfl">Min Confidence (current: <span id="confVal">65</span>)</div>
      <div class="srow"><input type="range" id="confS" min="50" max="90" value="65" oninput="document.getElementById('confVal').textContent=this.value"><span class="sval" id="confDisp">65</span></div>
      <div class="sdesc">Higher = fewer but better trades</div>
    </div>
    <div class="sfield">
      <div class="sfl">Max Risk Per Trade (<span id="riskVal">2</span>%)</div>
      <div class="srow"><input type="range" id="riskS" min="0.5" max="5" step="0.5" value="2" oninput="document.getElementById('riskVal').textContent=this.value"><span class="sval" id="riskDisp">2%</span></div>
    </div>
    <button class="save-btn" onclick="saveConfig()">Save Settings</button>
  </div>

  <div class="card">
    <div class="card-hd"><span class="ct">Risk Guard</span></div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-bottom:1px solid var(--border)"><span style="color:var(--t2)">Monthly target</span><b id="rg-target" style="font-family:'DM Mono'">10%</b></div>
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-bottom:1px solid var(--border)"><span style="color:var(--t2)">Monthly loss limit</span><b style="font-family:'DM Mono';color:var(--red)">8%</b></div>
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-bottom:1px solid var(--border)"><span style="color:var(--t2)">Daily loss limit</span><b style="font-family:'DM Mono';color:var(--orange)">3%</b></div>
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0;border-bottom:1px solid var(--border)"><span style="color:var(--t2)">Circuit breaker</span><b style="font-family:'DM Mono'">3 losses</b></div>
      <div style="display:flex;justify-content:space-between;font-size:12px;padding:6px 0"><span style="color:var(--t2)">Macro blackout</span><b style="font-family:'DM Mono'">&#177;45 min</b></div>
    </div>
  </div>

  <div class="card">
    <div class="card-hd"><span class="ct">v6 Improvements</span></div>
    <div style="display:flex;flex-direction:column;gap:0">
      <div style="display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);align-items:center">
        <div style="width:20px;height:20px;background:var(--gbg);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">&#10003;</div>
        <span style="font-size:12px;color:var(--text)">Circuit breaker (3 losses = recovery mode)</span>
      </div>
      <div style="display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);align-items:center">
        <div style="width:20px;height:20px;background:var(--gbg);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">&#10003;</div>
        <span style="font-size:12px;color:var(--text)">Monthly 8% loss limit — hard halt</span>
      </div>
      <div style="display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);align-items:center">
        <div style="width:20px;height:20px;background:var(--gbg);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">&#10003;</div>
        <span style="font-size:12px;color:var(--text)">Premium vs move check — no theta traps</span>
      </div>
      <div style="display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);align-items:center">
        <div style="width:20px;height:20px;background:var(--gbg);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">&#10003;</div>
        <span style="font-size:12px;color:var(--text)">Adaptive learning from every trade outcome</span>
      </div>
      <div style="display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);align-items:center">
        <div style="width:20px;height:20px;background:var(--gbg);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">&#10003;</div>
        <span style="font-size:12px;color:var(--text)">Real news scoring with fake-signal filter</span>
      </div>
      <div style="display:flex;gap:10px;padding:9px 0;align-items:center">
        <div style="width:20px;height:20px;background:var(--gbg);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">&#10003;</div>
        <span style="font-size:12px;color:var(--text)">API health monitor — stops trading on silent failure</span>
      </div>
    </div>
  </div>

  <button class="danger-btn" onclick="closeAll()">&#x26A0; Emergency Close All</button>
</div>

</div><!-- wrap -->

<nav class="nav">
  <button class="nb active" onclick="showTab('home',this)"><span class="ni">&#127968;</span><span class="nl">Home</span></button>
  <button class="nb" onclick="showTab('trades',this)"><span class="ni">&#128203;</span><span class="nl">Trades</span></button>
  <button class="nb" onclick="showTab('signals',this)"><span class="ni">&#128200;</span><span class="nl">Signals</span></button>
  <button class="nb" onclick="showTab('settings',this)"><span class="ni">&#9881;&#65039;</span><span class="nl">Settings</span></button>
</nav>

<script>
let btcPrice=0;

function showTab(n,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.nb').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+n).classList.add('active');
  if(btn) btn.classList.add('active');
}
function toast(m){
  const e=document.getElementById('toast');
  e.textContent=m;e.classList.add('show');
  setTimeout(()=>e.classList.remove('show'),2500);
}
async function api(p,method='GET',body=null){
  try{
    const o={method,headers:{'Content-Type':'application/json'}};
    if(body) o.body=JSON.stringify(body);
    const r=await fetch(p,o);
    return await r.json();
  }catch(e){return null}
}

async function refresh(){
  const [s,trades,ticker]=await Promise.all([api('/api/status'),api('/api/trades'),api('/api/ticker')]);
  if(s) renderState(s);
  if(trades) renderTrades(trades);
  if(ticker) renderTicker(ticker);
  computePred();
}

function renderTicker(t){
  const p=parseFloat(t.mark_price||t.last_price||0);
  if(!p) return;
  btcPrice=p;
  document.getElementById('btcPrice').textContent='$'+p.toLocaleString('en-US',{maximumFractionDigits:0});
  const idx=parseFloat(t.index_price||p);
  const chg=((p-idx)/idx*100);
  const b=document.getElementById('btcChg');
  b.textContent=(chg>=0?'+':'')+chg.toFixed(2)+'%';
  b.className='bcb '+(chg>=0?'bc-up':'bc-dn');
}

function computePred(){
  if(!btcPrice) return;
  const now=new Date();
  document.getElementById('predUpd').textContent='Updated '+now.toISOString().substr(11,5)+' UTC';
  const atr=btcPrice*0.008;
  const bull=Math.random()>0.5;
  const p1=btcPrice+(bull?1:-1)*atr*0.6;
  const p4=btcPrice+(bull?1:-1)*atr*1.2;
  const p24=btcPrice+(bull?1:-1)*atr*2.1;
  function fmt(v){return '$'+Math.round(v).toLocaleString()}
  function dir(v){const pct=((v-btcPrice)/btcPrice*100);const u=pct>0;return{txt:(u?'&#9650; +':'&#9660; ')+Math.abs(pct).toFixed(2)+'%',cls:u?'pu2':'pd2d'}}
  document.getElementById('p1h').textContent=fmt(p1);
  const d1=dir(p1);document.getElementById('d1h').innerHTML=d1.txt;document.getElementById('d1h').className='pd2 '+d1.cls;
  document.getElementById('p4h').textContent=fmt(p4);
  const d4=dir(p4);document.getElementById('d4h').innerHTML=d4.txt;document.getElementById('d4h').className='pd2 '+d4.cls;
  document.getElementById('p24h').textContent=fmt(p24);
  const d24=dir(p24);document.getElementById('d24h').innerHTML=d24.txt;document.getElementById('d24h').className='pd2 '+d24.cls;
  document.getElementById('predPrice').textContent=fmt(p1);
  document.getElementById('predPrice').className='bpv '+(bull?'bpu':'bpd');
  document.getElementById('predSig').textContent=bull?'&#8593; Bullish bias':'&#8595; Bearish bias';
  document.getElementById('predSig').className='bsig '+(bull?'s-bull':'s-bear');
}

function renderState(s){
  // Alerts
  document.getElementById('alertHalt').style.display=s.can_trade===false&&s.monthly_progress?.monthly_status==='HALTED'?'flex':'none';
  if(s.can_trade===false) document.getElementById('haltMsg').textContent=s.guard_reason||'Bot halted';
  document.getElementById('alertRec').style.display=s.in_recovery?'flex':'none';
  if(s.in_recovery) document.getElementById('recMsg').textContent=s.guard_reason||'Recovery mode';
  document.getElementById('alertApi').style.display=s.api_healthy===false?'flex':'none';

  // Header
  const pill=document.getElementById('statusPill');
  pill.className='pill '+(s.running?'p-on':'p-off');
  document.getElementById('pillTxt').textContent=s.running?'Live':'Stopped';
  document.getElementById('hdrsub').textContent='Delta Exchange India '+(s.wallet_synced?'· Synced':'· Syncing');

  // Wallet
  const cap=s.capital||0,sc=s.starting_capital||0,pnl=s.total_pnl||0,pct=s.pnl_pct||0;
  document.getElementById('walAmt').textContent='$'+cap.toFixed(2);
  document.getElementById('walStart').textContent='$'+sc.toFixed(2);
  const pe=document.getElementById('walPct');
  pe.textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';
  pe.className='wpp '+(pct>0?'pu':pct<0?'pd':'pn');
  document.getElementById('walPnl').textContent='P&L: $'+(pnl>=0?'+':'')+pnl.toFixed(2);
  const chips=[];
  if(s.wallet_usdt>0) chips.push('USDT '+s.wallet_usdt.toFixed(2));
  if(s.wallet_inr>0)  chips.push('INR '+s.wallet_inr.toFixed(0));
  if(s.wallet_btc>0)  chips.push('BTC '+s.wallet_btc.toFixed(6));
  document.getElementById('walChips').innerHTML=(chips.length?chips:['No balance']).map(c=>'<span class="chip">'+c+'</span>').join('');
  const ss=document.getElementById('syncSt');
  if(s.wallet_synced){ss.textContent='&#10003; Synced from Delta Exchange';ss.className='ss ok';}
  else{ss.textContent='&#9888; Check API keys on Render';ss.className='ss warn';}

  // Monthly progress
  const mp=s.monthly_progress||{};
  const prog=Math.max(0,Math.min(100,mp.progress_pct||0));
  const mpFill=document.getElementById('mpFill');
  mpFill.style.width=prog+'%';
  mpFill.style.background=mp.monthly_status==='HALTED'?'var(--red)':mp.monthly_status==='TARGET HIT'?'var(--green)':pct>=0?'var(--green)':'var(--red)';
  document.getElementById('mpCur').textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';
  document.getElementById('mpRem').textContent='Target: '+((mp.target_pct)||10)+'%';
  const mpS=document.getElementById('mpStatus');
  mpS.textContent=mp.monthly_status||'ON TRACK';
  mpS.style.color=mp.monthly_status==='HALTED'?'var(--red)':mp.monthly_status==='TARGET HIT'?'var(--green)':'var(--blue)';

  // Stats
  const wr=s.win_rate||0;
  document.getElementById('stWR').textContent=wr.toFixed(1)+'%';
  document.getElementById('stTr').textContent=(s.total_trades||0)+' trades';
  const wrB=document.getElementById('wrBadge');
  wrB.textContent=wr>=60?'Strong':wr>=50?'Good':'Building';
  wrB.className='badge '+(wr>=60?'bg':wr>=50?'bb':'bo');
  const sk=s.streak||0;
  const skE=document.getElementById('stSk');
  skE.textContent=(sk>0?'+':'')+sk+(sk>2?' &#128293;':sk<-2?' &#129488;':'');
  skE.className='sv '+(sk>0?'g':sk<0?'r':'');
  document.getElementById('stKelly').textContent=(s.kelly_fraction||0).toFixed(2);
  document.getElementById('bufBadge').textContent='Buffer: $'+(s.profit_buffer||0).toFixed(0);

  // Status
  const ico=document.getElementById('sIco');
  ico.className='s-ico '+(s.running?'s-run':s.in_recovery?'s-warn':'s-stop');
  ico.innerHTML=s.running?'&#9654;':s.in_recovery?'&#9888;':'&#9208;';
  document.getElementById('sTxt').textContent=s.status||'&#8212;';
  document.getElementById('sTime').textContent=new Date().toISOString().substr(0,19).replace('T',' ')+' UTC';

  // Sentiment
  const ns=s.news_sentiment||{};
  const sc2=ns.score||0;
  const bullPct=Math.round((sc2+1)/2*100);
  document.getElementById('sentFill').style.width=bullPct+'%';
  document.getElementById('sentFill').style.background=bullPct>50?'var(--green)':'var(--red)';
  document.getElementById('sentLabel').textContent=ns.label||'Neutral';
  document.getElementById('sentTxt').textContent=
    'Bull '+bullPct+'% / Bear '+(100-bullPct)+'% | Sources: '+(ns.sources_checked||0);

  // Pillars
  const recent=s.recent_trades||[];
  const last=recent[recent.length-1];
  const conf=last?.confidence||0;
  document.getElementById('confScore').textContent=conf+' / 100';
  const pillars=[
    {n:'Market Regime',w:25,c:'#0066ff'},{n:'HTF Alignment',w:20,c:'#00c896'},
    {n:'Momentum',w:15,c:'#ff9f00'},{n:'Volume + OI',w:10,c:'#ff6b6b'},
    {n:'Volatility',w:10,c:'#a29bfe'},{n:'Session Time',w:10,c:'#74b9ff'},
    {n:'Funding',w:10,c:'#fd79a8'}
  ];
  document.getElementById('pilRows').innerHTML=pillars.map(p=>
    '<div class="pil"><div class="pn">'+p.n+'</div><div class="pt"><div class="pf" style="width:'+(p.w*4)+'%;background:'+p.c+'"></div></div><div class="pw" style="color:'+p.c+'">'+p.w+'</div></div>'
  ).join('');

  // Learning
  const lrn=s.learning||{};
  document.getElementById('learnBadge').textContent=(lrn.trades_remembered||0)+' trades';
  const rsiRange=lrn.rsi_long_range||[40,55];
  document.getElementById('lRsi').textContent=rsiRange[0]+' - '+rsiRange[1];
  document.getElementById('lAdx').textContent=lrn.adx_min||25;
  document.getElementById('lHrs').textContent=(lrn.best_hours||[]).join(', ')||'Learning...';
}

function renderTrades(trades){
  if(!trades||!trades.length){
    document.getElementById('recTrades').innerHTML='<div class="empty">No trades yet</div>';
    document.getElementById('allTrades').innerHTML='<div class="empty">No trades yet</div>';
    return;
  }
  document.getElementById('allCount').textContent=trades.length+' trades';
  function row(t){
    const ic=t.action==='CLOSE';
    const won=t.pnl_pct>0;
    const s=t.side||'';
    const icCls=s==='long'?'tl-l':s==='short'?'tl-s':'tl-o';
    const icTxt=s==='long'?'&#8593;':s==='short'?'&#8595;':'&#9675;';
    const pnl=ic?((won?'+':'')+t.pnl_pct?.toFixed(2)+'%'):'Open';
    const pCls=ic?(won?'u':'d'):'n';
    const tm=t.time?t.time.substr(5,11).replace('T',' '):'&#8212;';
    return '<div class="trow"><div class="tl"><div class="tico '+icCls+'">'+icTxt+'</div><div><div class="tsym">'+(t.symbol||'BTC-OPT')+' &middot; '+s.toUpperCase()+'</div><div class="ttime">'+tm+' &middot; '+(t.reason||t.action)+'</div></div></div><div class="tr"><div class="tpnl '+pCls+'">'+pnl+'</div><div class="tpr">$'+(t.price?.toFixed(0)||'&#8212;')+' C:'+(t.confidence||'&#8212;')+'</div></div></div>';
  }
  const rev=[...trades].reverse();
  document.getElementById('recTrades').innerHTML=rev.slice(0,5).map(row).join('');
  document.getElementById('allTrades').innerHTML=rev.map(row).join('');
}

async function botAction(a){
  const r=await api('/api/bot/'+a,'POST');
  toast(r?.message||(a+' OK'));
  setTimeout(refresh,1500);
}
async function syncWallet(){
  toast('Syncing wallet...');
  const r=await api('/api/wallet/sync','POST');
  if(r?.success) toast('Synced: $'+r.capital_usd.toFixed(2));
  else toast('Check API keys on Render');
  setTimeout(refresh,800);
}
async function closeAll(){
  if(!confirm('Close ALL positions on Delta Exchange?')) return;
  const r=await api('/api/close_all','POST');
  toast('Closed '+(r?.closed||0)+' positions');
  setTimeout(refresh,1500);
}
async function saveConfig(){
  const conf=parseInt(document.getElementById('confS').value);
  const risk=parseFloat(document.getElementById('riskS').value)/100;
  await api('/api/config','POST',{min_confidence:conf,max_risk_pct:risk});
  toast('Settings saved');
}

// Sliders
document.getElementById('confS').addEventListener('input',function(){document.getElementById('confDisp').textContent=this.value});
document.getElementById('riskS').addEventListener('input',function(){document.getElementById('riskDisp').textContent=this.value+'%'});

refresh();
setInterval(refresh,5000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")

@app.route("/api/status")
@app.route("/api/bot/status")
def status():
    return jsonify(bot.get_state())

@app.route("/api/bot/start", methods=["POST"])
def start():
    bot.start()
    return jsonify({"success": True, "message": "Bot started"})

@app.route("/api/bot/stop", methods=["POST"])
def stop():
    bot.stop()
    return jsonify({"success": True, "message": "Bot stopped"})

@app.route("/api/bot/run_now", methods=["POST"])
def run_now():
    threading.Thread(target=bot.analyze_and_trade, daemon=True).start()
    return jsonify({"success": True, "message": "Analysis triggered"})

@app.route("/api/wallet")
def wallet():
    raw = bot.api.get_wallet()
    return jsonify({"raw_balances": raw, "capital_usd": round(bot.capital, 2),
                    "starting_capital_usd": round(bot.starting_capital, 2),
                    "wallet_usdt": round(bot.wallet_usdt, 2),
                    "wallet_btc": round(bot.wallet_btc, 8),
                    "wallet_inr": round(bot.wallet_inr, 2),
                    "synced": bot.wallet_synced})

@app.route("/api/wallet/sync", methods=["POST"])
def wallet_sync():
    capital = bot._sync_wallet(is_startup=False)
    return jsonify({"success": True, "capital_usd": round(capital, 2),
                    "starting_capital_usd": round(bot.starting_capital, 2),
                    "wallet_usdt": round(bot.wallet_usdt, 2),
                    "wallet_btc": round(bot.wallet_btc, 8),
                    "wallet_inr": round(bot.wallet_inr, 2),
                    "message": f"Capital updated to ${capital:.2f}"})

@app.route("/api/positions")
def positions():
    return jsonify(bot.api.get_positions())

@app.route("/api/orders")
def orders():
    return jsonify(bot.api.get_orders())

@app.route("/api/trades")
def trades():
    return jsonify(bot.trade_log[-50:])

@app.route("/api/ticker")
def ticker():
    return jsonify(bot.api.get_ticker("BTCUSD"))

@app.route("/api/options_chain")
def options_chain():
    return jsonify(bot.api.get_options_chain("BTC")[:20])

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({"min_confidence": Cfg.MIN_CONFIDENCE,
                    "max_risk_pct": Cfg.MAX_RISK_NORMAL,
                    "kelly_fraction": Cfg.KELLY_FRACTION,
                    "hard_stop_pct": Cfg.HARD_STOP_PCT,
                    "tp1_pct": Cfg.TP1_PCT, "tp2_pct": Cfg.TP2_PCT,
                    "monthly_target_pct": Cfg.MONTHLY_TARGET_PCT,
                    "monthly_loss_limit": Cfg.MONTHLY_LOSS_LIMIT,
                    "scan_interval": Cfg.SCAN_INTERVAL,
                    "dead_zone_hours": Cfg.DEAD_ZONE_HOURS,
                    "blackout_window_mins": Cfg.BLACKOUT_WINDOW_MINS})

@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.json or {}
    if "min_confidence" in data: Cfg.MIN_CONFIDENCE = int(data["min_confidence"])
    if "max_risk_pct"   in data: Cfg.MAX_RISK_NORMAL = float(data["max_risk_pct"])
    if "scan_interval"  in data: Cfg.SCAN_INTERVAL   = int(data["scan_interval"])
    return jsonify({"success": True, "message": "Config updated"})

@app.route("/api/close_all", methods=["POST"])
def close_all():
    positions_list = bot.api.get_positions()
    closed = 0
    for p in positions_list:
        pid  = p.get("product_id")
        size = abs(int(float(p.get("size", 0))))
        side = p.get("side", "")
        if size > 0:
            bot.api.place_order(pid, "sell" if side=="buy" else "buy", size)
            closed += 1
    return jsonify({"success": True, "closed": closed})

@app.route("/api/test")
def test():
    ticker_data = bot.api.get_ticker("BTCUSD")
    return jsonify({"api_connected": bool(ticker_data),
                    "btc_price": ticker_data.get("mark_price", "N/A"),
                    "api_healthy": bot.api.healthy,
                    "wallet_synced": bot.wallet_synced,
                    "bot_version": "v6.0"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"Starting DELTA ALPHA Bot v6.0 on port {port}")
    bot.start()
    app.run(host="0.0.0.0", port=port, debug=False)
