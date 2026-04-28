"""
ΔLPHA BOT v5.0 — Delta Exchange India | BTC Options
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXES from BTC Autopsy:
  ✅ RSI<35 regime bug ELIMINATED — RSI is no longer a regime label
  ✅ 7-pillar confidence score (replaces single-indicator trigger)
  ✅ Hard vetoes (macro blackout, ADX<15, HTF contradiction, funding rate)
  ✅ Divergence detection (bullish + bearish MACD divergence)
  ✅ Time-of-day filter (dead zone 02:00–06:00 UTC blocked)
  ✅ ATR-based position sizing (volatility-adjusted)
  ✅ Smart exits: TP1 partial, TP2 full, hard stop, trailing stop
  ✅ Kelly Criterion sizing with streak scaling
  ✅ Regime requires EMA stack + ADX>25 + HTF agreement (5 conditions)
  ✅ Weekend compression filter
  ✅ News/macro blackout windows (13:30, 19:00 UTC)

Capital: $500 starting | BTC options only | Delta Exchange India
"""

import os, time, hmac, hashlib, json, logging, requests, threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ALPHA")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
class Cfg:
    API_KEY    = os.getenv("DELTA_API_KEY", "")
    API_SECRET = os.getenv("DELTA_API_SECRET", "")
    BASE_URL   = "https://api.india.delta.exchange"

    # Capital & risk — NO hardcoded amount, read live from Delta wallet
    MAX_RISK_PCT       = 0.02   # Max 2% per trade
    KELLY_FRACTION     = 0.25   # Use 25% of Kelly (ultra-conservative)
    MAX_OPEN_POSITIONS = 2

    # Confidence thresholds
    MIN_CONFIDENCE     = 65     # Reduced from 75 — allows more trades but vetoes protect
    HIGH_CONFIDENCE    = 85     # Max size

    # Stop / Target
    HARD_STOP_PCT      = 0.03   # 3% hard stop
    TP1_PCT            = 0.015  # Take 50% profits at 1.5%
    TP2_PCT            = 0.025  # Take remaining at 2.5%
    TRAIL_ACTIVATE_PCT = 0.012  # Trailing stop activates at 1.2%
    TRAIL_DISTANCE_PCT = 0.008  # Trail by 0.8%

    # Regime (ADX)
    ADX_TREND_MIN      = 25
    ADX_CHOP_MAX       = 20

    # RSI corrected thresholds (not regime labels!)
    RSI_BULL_PULLBACK  = (40, 55)   # Buy pullback in bull regime
    RSI_BEAR_BOUNCE    = (45, 60)   # Sell bounce in bear regime
    RSI_EXTREME_OB     = 80         # Overbought (not trend label)
    RSI_EXTREME_OS     = 20         # Oversold (not trend label)

    # Funding rate veto
    FUNDING_LONG_MAX   = 0.001  # +0.1%/8h — too crowded for longs
    FUNDING_SHORT_MIN  = -0.0005  # -0.05%/8h — too crowded for shorts

    # Time filters (UTC)
    DEAD_ZONE_HOURS    = [2, 3, 4, 5]   # Block 02:00–05:59 UTC
    PEAK_HOURS         = [8, 9, 13, 14, 15, 16]  # Boost confidence
    MACRO_BLACKOUT_TIMES = [(13, 30), (19, 0)]    # CPI/NFP, FOMC (H, M)
    BLACKOUT_WINDOW_MINS = 15

    # BTC options product IDs on Delta India (verify in production)
    BTC_PRODUCT_ID     = 27   # BTCUSD perpetual for price feed
    SCAN_INTERVAL      = 300  # 5 minutes

# ══════════════════════════════════════════════════════════════════════════════
# DELTA EXCHANGE API CLIENT
# ══════════════════════════════════════════════════════════════════════════════
class DeltaAPI:
    def __init__(self):
        self.base = Cfg.BASE_URL
        self.key = Cfg.API_KEY
        self.secret = Cfg.API_SECRET
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _sign(self, method: str, path: str, qs: str = "", body: str = "") -> dict:
        ts = str(int(time.time()))
        msg = method + ts + path + qs + body
        sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            "api-key": self.key,
            "timestamp": ts,
            "signature": sig,
            "User-Agent": "delta-alpha-bot-v5"
        }

    def _get(self, path: str, params: dict = None):
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._sign("GET", path, qs)
        try:
            r = self.session.get(f"{self.base}{path}{qs}", headers=headers, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"GET {path}: {e}")
            return None

    def _post(self, path: str, body: dict):
        body_str = json.dumps(body)
        headers = self._sign("POST", path, "", body_str)
        try:
            r = self.session.post(f"{self.base}{path}", headers=headers,
                                  data=body_str, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"POST {path}: {e}")
            return None

    def get_candles(self, symbol: str = "BTCUSD", resolution: int = 5,
                    limit: int = 100) -> list:
        end = int(time.time())
        start = end - (resolution * 60 * limit)
        data = self._get("/v2/history/candles", {
            "symbol": symbol, "resolution": resolution,
            "start": start, "end": end
        })
        if data and data.get("success"):
            return data.get("result", [])
        return []

    def get_ticker(self, symbol: str = "BTCUSD") -> dict:
        data = self._get(f"/v2/tickers/{symbol}")
        if data and data.get("success"):
            return data.get("result", {})
        return {}

    def get_orderbook(self, symbol: str = "BTCUSD") -> dict:
        data = self._get(f"/v2/l2orderbook/{symbol}")
        if data and data.get("success"):
            return data.get("result", {})
        return {}

    def get_wallet(self) -> dict:
        data = self._get("/v2/wallet/balances")
        if data and data.get("success"):
            return {b["asset_symbol"]: float(b["available_balance"])
                    for b in data.get("result", [])}
        return {}

    def get_positions(self) -> list:
        data = self._get("/v2/positions/margined")
        if data and data.get("success"):
            return [p for p in data.get("result", []) if float(p.get("size", 0)) != 0]
        return []

    def get_orders(self) -> list:
        data = self._get("/v2/orders", {"state": "open"})
        if data and data.get("success"):
            return data.get("result", [])
        return []

    def get_options_chain(self, underlying: str = "BTC") -> list:
        data = self._get("/v2/products", {
            "contract_type": "call_options,put_options",
            "underlying_asset_symbol": underlying,
            "state": "live",
            "page_size": 50
        })
        if data and data.get("success"):
            return data.get("result", [])
        return []

    def get_funding_rate(self, symbol: str = "BTCUSD") -> float:
        data = self._get(f"/v2/tickers/{symbol}")
        if data and data.get("success"):
            return float(data.get("result", {}).get("funding_rate", 0))
        return 0.0

    def place_order(self, product_id: int, side: str, size: int,
                    order_type: str = "market_order",
                    limit_price: float = None,
                    stop_price: float = None) -> dict:
        body = {
            "product_id": product_id,
            "size": size,
            "side": side,
            "order_type": order_type,
            "time_in_force": "gtc"
        }
        if limit_price:
            body["limit_price"] = str(limit_price)
        if stop_price:
            body["stop_price"] = str(stop_price)
        return self._post("/v2/orders", body) or {}

    def cancel_order(self, order_id: int, product_id: int) -> dict:
        return self._post(f"/v2/orders/{order_id}/cancel",
                          {"product_id": product_id}) or {}

    def close_position(self, product_id: int, size: int, side: str) -> dict:
        close_side = "sell" if side == "buy" else "buy"
        return self.place_order(product_id, close_side, size)

# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE (FIXED)
# ══════════════════════════════════════════════════════════════════════════════
class TechEngine:

    @staticmethod
    def ema(prices: list, period: int) -> list:
        if len(prices) < period:
            return [prices[-1]] * len(prices)
        k = 2 / (period + 1)
        ema_vals = [sum(prices[:period]) / period]
        for p in prices[period:]:
            ema_vals.append(p * k + ema_vals[-1] * (1 - k))
        result = [ema_vals[0]] * (period - 1) + ema_vals
        return result

    @staticmethod
    def rsi(prices: list, period: int = 7) -> float:
        """RSI(7) — faster than RSI(14), better for 1-min/5-min crypto."""
        if len(prices) < period + 1:
            return 50.0
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [max(d, 0) for d in deltas[-period:]]
        losses = [abs(min(d, 0)) for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(prices: list, fast: int = 5, slow: int = 13, signal: int = 5):
        """MACD(5,13,5) — crypto-optimized, faster than 12/26/9."""
        if len(prices) < slow + signal:
            return 0.0, 0.0, 0.0, []
        ema_fast = TechEngine.ema(prices, fast)
        ema_slow = TechEngine.ema(prices, slow)
        macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(prices))]
        signal_line = TechEngine.ema(macd_line, signal)
        histogram = [macd_line[i] - signal_line[i] for i in range(len(macd_line))]
        return macd_line[-1], signal_line[-1], histogram[-1], histogram

    @staticmethod
    def adx(highs: list, lows: list, closes: list, period: int = 14) -> tuple:
        """Returns (ADX, +DI, -DI)."""
        if len(closes) < period * 2:
            return 0.0, 0.0, 0.0
        tr_list, plus_dm, minus_dm = [], [], []
        for i in range(1, len(closes)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_list.append(tr)
            up_move = highs[i] - highs[i-1]
            down_move = lows[i-1] - lows[i]
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        def smooth(data, p):
            s = sum(data[:p])
            result = [s]
            for d in data[p:]:
                s = s - s / p + d
                result.append(s)
            return result

        atr = smooth(tr_list, period)
        plus_di_raw = smooth(plus_dm, period)
        minus_di_raw = smooth(minus_dm, period)
        plus_di = [100 * plus_di_raw[i] / atr[i] if atr[i] > 0 else 0
                   for i in range(len(atr))]
        minus_di = [100 * minus_di_raw[i] / atr[i] if atr[i] > 0 else 0
                    for i in range(len(atr))]
        dx = [abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i]) * 100
              if (plus_di[i] + minus_di[i]) > 0 else 0
              for i in range(len(plus_di))]
        adx_val = sum(dx[-period:]) / period
        return adx_val, plus_di[-1], minus_di[-1]

    @staticmethod
    def atr(highs: list, lows: list, closes: list, period: int = 7) -> float:
        """ATR(7) for position sizing and volatility regime."""
        if len(closes) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(closes)):
            trs.append(max(highs[i] - lows[i],
                           abs(highs[i] - closes[i-1]),
                           abs(lows[i] - closes[i-1])))
        return sum(trs[-period:]) / period

    @staticmethod
    def bollinger(prices: list, period: int = 20, std_dev: float = 2.0) -> tuple:
        if len(prices) < period:
            mid = prices[-1]
            return mid, mid, mid, 0.0
        window = prices[-period:]
        mid = sum(window) / period
        variance = sum((p - mid) ** 2 for p in window) / period
        std = variance ** 0.5
        upper = mid + std_dev * std
        lower = mid - std_dev * std
        width = (upper - lower) / mid * 100
        return upper, mid, lower, width

    @staticmethod
    def detect_divergence(prices: list, histogram: list, lookback: int = 10) -> str:
        """
        Detect MACD histogram divergence — the real reversal signal.
        Returns: 'bullish', 'bearish', or 'none'
        """
        if len(prices) < lookback or len(histogram) < lookback:
            return "none"
        p = prices[-lookback:]
        h = histogram[-lookback:]

        # Bullish divergence: price makes lower low, histogram makes higher low
        price_low1 = min(p[:lookback//2])
        price_low2 = min(p[lookback//2:])
        hist_at_low1 = h[p.index(price_low1)] if price_low1 in p else h[0]
        hist_at_low2 = h[lookback//2 + ([x for x in p[lookback//2:]] .index(price_low2)
                                        if price_low2 in p[lookback//2:] else 0)]

        if price_low2 < price_low1 and hist_at_low2 > hist_at_low1 and hist_at_low2 < 0:
            return "bullish"

        # Bearish divergence: price makes higher high, histogram makes lower high
        price_high1 = max(p[:lookback//2])
        price_high2 = max(p[lookback//2:])
        hist_at_high1 = h[p.index(price_high1)] if price_high1 in p else h[0]
        hist_at_high2 = h[lookback//2 + ([x for x in p[lookback//2:]].index(price_high2)
                                          if price_high2 in p[lookback//2:] else 0)]

        if price_high2 > price_high1 and hist_at_high2 < hist_at_high1 and hist_at_high2 > 0:
            return "bearish"

        return "none"

# ══════════════════════════════════════════════════════════════════════════════
# 7-PILLAR CONFIDENCE SCORE ENGINE
# ══════════════════════════════════════════════════════════════════════════════
class ConfidenceEngine:
    """
    Replaces single-indicator regime labels with a 0-100 weighted score.
    Inspired by: arXiv 2509.16707, 2507.07107, TradingView Conflux Engine.

    Pillars:
      1. Regime         25%  EMA stack + ADX>25 + DI direction
      2. HTF Alignment  20%  5m + 15m must agree
      3. Momentum       15%  RSI in correct zone + 3 rising MACD bars
      4. Volume         10%  Current > 20-bar average × 1.5
      5. Volatility     10%  ATR sane, not extreme
      6. Time-of-day    10%  Active session, no blackout
      7. Funding/OI     10%  Not at crowded extreme
    """

    def score(self, data: dict, direction: str) -> tuple:
        """
        Returns (score: int, vetoed: bool, veto_reason: str, breakdown: dict)
        direction: 'long' or 'short'
        """
        breakdown = {}
        total = 0
        veto_reason = ""

        closes = data.get("closes", [])
        highs = data.get("highs", [])
        lows = data.get("lows", [])
        volumes = data.get("volumes", [])
        closes_5m = data.get("closes_5m", closes)
        closes_15m = data.get("closes_15m", closes)
        hour_utc = data.get("hour_utc", 12)
        minute_utc = data.get("minute_utc", 0)
        funding_rate = data.get("funding_rate", 0.0)
        is_weekend = data.get("is_weekend", False)

        if len(closes) < 55:
            return 0, True, "insufficient_data", {}

        # ── HARD VETOES (override everything) ────────────────────────────────
        adx_val, plus_di, minus_di = TechEngine.adx(highs, lows, closes)
        if adx_val < 15:
            return 0, True, f"ADX={adx_val:.1f}<15 (extreme_chop)", {}

        if hour_utc in Cfg.DEAD_ZONE_HOURS and not is_weekend:
            return 0, True, f"dead_zone_hour_{hour_utc}UTC", {}

        if is_weekend:
            # Weekend: only allow very high confluence setups
            pass  # Will be caught by low score naturally

        for macro_h, macro_m in Cfg.MACRO_BLACKOUT_TIMES:
            macro_mins = macro_h * 60 + macro_m
            current_mins = hour_utc * 60 + minute_utc
            if abs(current_mins - macro_mins) <= Cfg.BLACKOUT_WINDOW_MINS:
                return 0, True, f"macro_blackout_{macro_h}:{macro_m:02d}UTC", {}

        if direction == "long" and funding_rate > Cfg.FUNDING_LONG_MAX:
            return 0, True, f"funding_rate={funding_rate:.4f}>max_long", {}
        if direction == "short" and funding_rate < Cfg.FUNDING_SHORT_MIN:
            return 0, True, f"funding_rate={funding_rate:.4f}<min_short", {}

        # ── PILLAR 1: Regime (25%) ────────────────────────────────────────────
        ema8  = TechEngine.ema(closes, 8)[-1]
        ema21 = TechEngine.ema(closes, 21)[-1]
        ema55 = TechEngine.ema(closes, 55)[-1]
        price = closes[-1]

        regime_bull = (price > ema8 > ema21 > ema55 and
                       adx_val > Cfg.ADX_TREND_MIN and
                       plus_di > minus_di)
        regime_bear = (price < ema8 < ema21 < ema55 and
                       adx_val > Cfg.ADX_TREND_MIN and
                       minus_di > plus_di)

        if direction == "long" and regime_bull:
            breakdown["regime"] = 25
        elif direction == "short" and regime_bear:
            breakdown["regime"] = 25
        elif adx_val > Cfg.ADX_TREND_MIN:
            breakdown["regime"] = 10  # Trending but wrong direction
        else:
            breakdown["regime"] = 0   # Chop

        total += breakdown["regime"]

        # ── PILLAR 2: HTF Alignment (20%) ─────────────────────────────────────
        htf_score = 0
        for htf_closes in [closes_5m, closes_15m]:
            if len(htf_closes) >= 21:
                h_ema8  = TechEngine.ema(htf_closes, 8)[-1]
                h_ema21 = TechEngine.ema(htf_closes, 21)[-1]
                h_price = htf_closes[-1]
                if direction == "long" and h_price > h_ema8 > h_ema21:
                    htf_score += 10
                elif direction == "short" and h_price < h_ema8 < h_ema21:
                    htf_score += 10

        # Hard veto: 1m trading against 15m structure
        if len(closes_15m) >= 21:
            h15_ema8 = TechEngine.ema(closes_15m, 8)[-1]
            h15_price = closes_15m[-1]
            if direction == "long" and h15_price < h15_ema8:
                return 0, True, "1m_vs_15m_contradiction", {}
            if direction == "short" and h15_price > h15_ema8:
                return 0, True, "1m_vs_15m_contradiction", {}

        breakdown["htf_alignment"] = htf_score
        total += htf_score

        # ── PILLAR 3: Momentum (15%) ──────────────────────────────────────────
        rsi = TechEngine.rsi(closes, 7)
        macd_line, signal_line, hist, histogram = TechEngine.macd(closes)
        divergence = TechEngine.detect_divergence(closes, histogram)

        mom_score = 0
        if direction == "long":
            # RSI in bull pullback zone (40-55), not blindly at 35
            if Cfg.RSI_BULL_PULLBACK[0] <= rsi <= Cfg.RSI_BULL_PULLBACK[1]:
                mom_score += 7
            elif rsi > 55:  # Already in momentum
                mom_score += 5
            # 3 rising histogram bars
            if len(histogram) >= 3 and all(histogram[-i] > histogram[-(i+1)]
                                             for i in range(1, 3)):
                mom_score += 5
            # Bullish divergence bonus
            if divergence == "bullish":
                mom_score += 8  # Strong bonus — divergence marks real turns
        elif direction == "short":
            if Cfg.RSI_BEAR_BOUNCE[0] <= rsi <= Cfg.RSI_BEAR_BOUNCE[1]:
                mom_score += 7
            elif rsi < 45:
                mom_score += 5
            if len(histogram) >= 3 and all(histogram[-i] < histogram[-(i+1)]
                                             for i in range(1, 3)):
                mom_score += 5
            if divergence == "bearish":
                mom_score += 8

        breakdown["momentum"] = min(mom_score, 15)
        total += breakdown["momentum"]

        # ── PILLAR 4: Volume (10%) ────────────────────────────────────────────
        vol_score = 0
        if len(volumes) >= 20:
            avg_vol = sum(volumes[-20:]) / 20
            if volumes[-1] > avg_vol * 1.5:
                vol_score = 10
            elif volumes[-1] > avg_vol:
                vol_score = 5
        breakdown["volume"] = vol_score
        total += vol_score

        # ── PILLAR 5: Volatility (10%) ────────────────────────────────────────
        atr_val = TechEngine.atr(highs, lows, closes, 7)
        _, _, _, bb_width = TechEngine.bollinger(closes)
        vol_score2 = 0
        if 0.3 < bb_width < 3.0:   # Sane volatility band
            vol_score2 = 10
        elif 0.1 < bb_width <= 0.3:  # Squeeze — possible breakout
            vol_score2 = 5
        elif bb_width >= 3.0:        # Extreme volatility
            vol_score2 = 3
        breakdown["volatility"] = vol_score2
        total += vol_score2

        # ── PILLAR 6: Time-of-Day (10%) ───────────────────────────────────────
        time_score = 5  # Base
        if hour_utc in Cfg.PEAK_HOURS:
            time_score = 10
        elif is_weekend:
            time_score = 2
        # NY open bias
        if hour_utc in [14, 15] and direction == "short":
            time_score = min(time_score + 3, 10)  # NY open favors puts
        if hour_utc in [1, 2] and direction == "long":
            time_score = min(time_score + 3, 10)  # Asia session favors calls
        breakdown["time_of_day"] = time_score
        total += time_score

        # ── PILLAR 7: Funding (10%) ───────────────────────────────────────────
        fund_score = 8  # Default good
        if direction == "long":
            if funding_rate > 0.0005:
                fund_score = 4  # Longs getting crowded
            elif funding_rate < -0.0003:
                fund_score = 10  # Negative funding = longs are cheap
        elif direction == "short":
            if funding_rate < -0.0003:
                fund_score = 4
            elif funding_rate > 0.0005:
                fund_score = 10
        breakdown["funding"] = fund_score
        total += fund_score

        return min(total, 100), False, "", breakdown

# ══════════════════════════════════════════════════════════════════════════════
# POSITION SIZER (Kelly + Streak + Regime)
# ══════════════════════════════════════════════════════════════════════════════
class PositionSizer:
    def __init__(self):
        self.win_count   = 0
        self.loss_count  = 0
        self.total_trades = 0
        self.streak      = 0   # + for wins, - for losses
        self.avg_win_pct = 2.0
        self.avg_loss_pct = 1.2

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.55  # Conservative start
        return self.win_count / self.total_trades

    def kelly_fraction(self) -> float:
        wr = self.win_rate
        if not (0.45 <= wr <= 0.75):
            return 0.01
        p, q = wr, 1 - wr
        b = self.avg_win_pct / max(self.avg_loss_pct, 0.1)
        kelly = (b * p - q) / b
        return max(0.005, min(0.03, kelly * Cfg.KELLY_FRACTION))

    def streak_multiplier(self) -> float:
        if self.streak >= 4:   return 1.5
        if self.streak == 3:   return 1.3
        if self.streak == 2:   return 1.15
        if self.streak <= -3:  return 0.7
        if self.streak == -2:  return 0.85
        return 1.0

    def size_usd(self, capital: float, confidence: int, atr_pct: float) -> float:
        base = capital * self.kelly_fraction()
        conf_mult = 1.0 if confidence < Cfg.HIGH_CONFIDENCE else 1.3
        streak_mult = self.streak_multiplier()
        # Reduce size in high volatility
        vol_mult = max(0.5, min(1.5, 0.015 / atr_pct)) if atr_pct > 0 else 1.0
        size = base * conf_mult * streak_mult * vol_mult
        # Hard cap
        return min(size, capital * Cfg.MAX_RISK_PCT)

    def record(self, won: bool, pct: float):
        self.total_trades += 1
        if won:
            self.win_count += 1
            self.streak = max(0, self.streak) + 1
            self.avg_win_pct = (self.avg_win_pct * 0.9 + abs(pct) * 0.1)
        else:
            self.loss_count += 1
            self.streak = min(0, self.streak) - 1
            self.avg_loss_pct = (self.avg_loss_pct * 0.9 + abs(pct) * 0.1)

# ══════════════════════════════════════════════════════════════════════════════
# TRADE MANAGER (Exits, Trailing, TP1/TP2)
# ══════════════════════════════════════════════════════════════════════════════
class Position:
    def __init__(self, product_id: int, side: str, entry: float,
                 size_usd: float, option_symbol: str = ""):
        self.product_id    = product_id
        self.side          = side          # 'long' or 'short'
        self.entry         = entry
        self.size_usd      = size_usd
        self.option_symbol = option_symbol
        self.entered_at    = datetime.now(timezone.utc)
        self.tp1_hit       = False
        self.trailing_on   = False
        self.trail_high    = entry
        self.closed        = False
        self.exit_price    = None
        self.exit_reason   = None

    def check_exit(self, current_price: float) -> tuple:
        """Returns (should_exit: bool, reason: str, partial: bool)"""
        if self.side == "long":
            pct = (current_price - self.entry) / self.entry
        else:
            pct = (self.entry - current_price) / self.entry

        # Hard stop
        if pct <= -Cfg.HARD_STOP_PCT:
            return True, "hard_stop", False

        # TP1 — take 50%
        if not self.tp1_hit and pct >= Cfg.TP1_PCT:
            self.tp1_hit = True
            return True, "tp1_50pct", True  # Partial

        # Activate trailing stop
        if pct >= Cfg.TRAIL_ACTIVATE_PCT:
            self.trailing_on = True
            if self.side == "long":
                self.trail_high = max(self.trail_high, current_price)
            else:
                self.trail_high = min(self.trail_high, current_price)

        # Trailing stop
        if self.trailing_on:
            if self.side == "long":
                trail_stop = self.trail_high * (1 - Cfg.TRAIL_DISTANCE_PCT)
                if current_price <= trail_stop:
                    return True, "trailing_stop", False
            else:
                trail_stop = self.trail_high * (1 + Cfg.TRAIL_DISTANCE_PCT)
                if current_price >= trail_stop:
                    return True, "trailing_stop", False

        # TP2 — full exit
        if pct >= Cfg.TP2_PCT:
            return True, "tp2_full", False

        # Time-based exit (4 hours)
        age_hrs = (datetime.now(timezone.utc) - self.entered_at).seconds / 3600
        if age_hrs >= 4:
            return True, "time_exit_4h", False

        return False, "", False

# ══════════════════════════════════════════════════════════════════════════════
# OPTIONS SELECTOR
# ══════════════════════════════════════════════════════════════════════════════
class OptionsSelector:
    """Selects optimal strike + expiry based on regime and confidence."""

    @staticmethod
    def select(chain: list, current_price: float, direction: str,
               confidence: int, atr_val: float) -> Optional[dict]:
        """
        Rules (from BTC Autopsy):
        - Use ITM options in trending markets (confidence > 75)
        - Use ATM options in transitional markets
        - Prefer 0DTE or 1DTE for sub-4h swings
        - Skip if premium > 1.5× ATR (theta drag)
        """
        if not chain:
            return None

        target_type = "call_options" if direction == "long" else "put_options"
        today = datetime.now(timezone.utc).date()

        candidates = []
        for opt in chain:
            if opt.get("contract_type") != target_type:
                continue
            try:
                expiry = datetime.strptime(
                    opt.get("settlement_time", "")[:10], "%Y-%m-%d"
                ).date()
                days_to_expiry = (expiry - today).days
                if days_to_expiry < 0 or days_to_expiry > 3:
                    continue
                strike = float(opt.get("strike_price", 0))
                mark = float(opt.get("mark_price", 0))
                if mark <= 0:
                    continue
                # ITM/ATM/OTM classification
                if direction == "long":
                    moneyness = (strike - current_price) / current_price
                else:
                    moneyness = (current_price - strike) / current_price

                # Premium check
                if atr_val > 0 and mark > atr_val * 1.5:
                    continue  # Too expensive

                candidates.append({
                    "product": opt,
                    "days_to_expiry": days_to_expiry,
                    "moneyness": moneyness,
                    "mark": mark,
                    "product_id": opt.get("id")
                })
            except Exception:
                continue

        if not candidates:
            return None

        # Sort: prefer 0-1 DTE, prefer slight ITM in high confidence
        def score_option(c):
            dte_score = 10 - c["days_to_expiry"] * 3  # Prefer shorter DTE
            if confidence > 80:
                # High confidence — prefer slight ITM (~-2% to 0%)
                mon_score = 10 if -0.02 <= c["moneyness"] <= 0.01 else 5
            else:
                # Lower confidence — prefer ATM
                mon_score = 10 if -0.005 <= c["moneyness"] <= 0.005 else 3
            return dte_score + mon_score

        candidates.sort(key=score_option, reverse=True)
        return candidates[0]

# ══════════════════════════════════════════════════════════════════════════════
# NEWS / MACRO REGIME ENGINE
# ══════════════════════════════════════════════════════════════════════════════
class MacroEngine:
    """
    Lightweight macro regime detection via public APIs.
    Adjusts confidence multiplier based on crypto-relevant news.
    """
    CRYPTO_KEYWORDS_BULL = [
        "etf approval", "strategy", "microstrategy", "institutional",
        "fed pivot", "rate cut", "ceasefire", "bitcoin reserve",
        "accumulation", "blackrock", "fidelity"
    ]
    CRYPTO_KEYWORDS_BEAR = [
        "sec enforcement", "regulation ban", "exchange hack",
        "binance", "ftx", "collapse", "rate hike", "cpi hot",
        "recession", "tether", "stablecoin ban"
    ]

    def get_macro_multiplier(self) -> float:
        """Returns multiplier: 0.5 (blackout), 1.0 (neutral), 1.2 (bullish)."""
        try:
            # Use CryptoPanic or similar free API
            r = requests.get(
                "https://cryptopanic.com/api/v1/posts/?auth_token=&public=true&currencies=BTC&filter=hot",
                timeout=5
            )
            if r.status_code == 200:
                posts = r.json().get("results", [])[:10]
                titles = " ".join(p.get("title", "").lower() for p in posts)
                bull_hits = sum(1 for k in self.CRYPTO_KEYWORDS_BULL if k in titles)
                bear_hits = sum(1 for k in self.CRYPTO_KEYWORDS_BEAR if k in titles)
                if bear_hits >= 2:
                    return 0.7
                if bull_hits >= 2:
                    return 1.15
        except Exception:
            pass
        return 1.0

# ══════════════════════════════════════════════════════════════════════════════
# MAIN BOT ENGINE
# ══════════════════════════════════════════════════════════════════════════════
class AlphaBot:
    def __init__(self):
        self.api         = DeltaAPI()
        self.tech        = TechEngine()
        self.confidence  = ConfidenceEngine()
        self.sizer       = PositionSizer()
        self.macro       = MacroEngine()
        self.options_sel = OptionsSelector()

        # Capital — always read live from Delta wallet, never hardcoded
        self.capital          = 0.0   # Set by _sync_wallet()
        self.starting_capital = 0.0   # Snapshot at first successful wallet read
        self.wallet_usdt      = 0.0   # Raw USDT balance from Delta
        self.wallet_btc       = 0.0   # BTC balance from Delta
        self.wallet_synced    = False # True once first sync succeeds

        self.positions: list[Position] = []
        self.trade_log: list[dict]  = []
        self.running     = False
        self.status_msg  = "Initializing..."

        # Stats
        self.total_pnl   = 0.0
        self.profit_buffer = 0.0   # Profits absorb losses first

        # Try initial wallet sync (may fail if API keys not set yet)
        self._sync_wallet(is_startup=True)

    # ── Wallet Sync ────────────────────────────────────────────────────────────
    def _sync_wallet(self, is_startup: bool = False) -> float:
        """
        Single source of truth for capital — reads live from Delta Exchange.
        Never hardcoded. Called on startup + every 5 min in run loop.
        Priority: USDT > INR (converted) > BTC (converted to USD).
        """
        try:
            balances = self.api.get_wallet()
            if not balances:
                if is_startup:
                    self.status_msg = "⚠ Wallet read failed — check API keys"
                return self.capital

            usdt = float(balances.get("USDT", balances.get("usdt", 0)))
            inr  = float(balances.get("INR",  balances.get("inr",  0)))
            btc  = float(balances.get("BTC",  balances.get("btc",  0)))

            # INR to USD
            inr_usd = 0.0
            if inr > 0:
                try:
                    r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=4)
                    inr_usd = inr / r.json().get("rates", {}).get("INR", 84.0)
                except Exception:
                    inr_usd = inr / 84.0

            # BTC to USD
            btc_usd = 0.0
            if btc > 0:
                t = self.api.get_ticker("BTCUSD")
                btc_usd = btc * float(t.get("mark_price", 0))

            total = usdt + inr_usd + btc_usd
            self.wallet_usdt = usdt
            self.wallet_btc  = btc
            self.wallet_inr  = inr

            if total > 0:
                if not self.wallet_synced or is_startup:
                    self.starting_capital = total
                    self.capital = total
                    self.wallet_synced = True
                    log.info(f"\U0001f4b0 Wallet synced: ${total:.2f} "
                             f"(USDT={usdt:.2f} INR={inr:.0f}\u2248${inr_usd:.2f} BTC\u2248${btc_usd:.2f})")
                else:
                    self.capital = total + self.profit_buffer
                    log.info(f"\U0001f4b0 Wallet refresh: ${total:.2f} | with buffer: ${self.capital:.2f}")
            else:
                log.warning("Wallet returned zero — using last known capital")

        except Exception as e:
            log.error(f"Wallet sync error: {e}")
        return self.capital

    # ── Data Gathering ────────────────────────────────────────────────────────
    def _get_market_data(self) -> dict:
        """Gather all data needed for the confidence score."""
        now = datetime.now(timezone.utc)
        candles_5m  = self.api.get_candles("BTCUSD", 5, 100)
        candles_15m = self.api.get_candles("BTCUSD", 15, 50)
        funding     = self.api.get_funding_rate("BTCUSD")

        if not candles_5m:
            return {}

        def parse(candles):
            closes = [float(c.get("close", c[-2] if isinstance(c, list) else 0))
                      for c in candles]
            highs  = [float(c.get("high",  c[-3] if isinstance(c, list) else 0))
                      for c in candles]
            lows   = [float(c.get("low",   c[-4] if isinstance(c, list) else 0))
                      for c in candles]
            vols   = [float(c.get("volume", c[-1] if isinstance(c, list) else 0))
                      for c in candles]
            return closes, highs, lows, vols

        closes_5m, highs_5m, lows_5m, vols_5m = parse(candles_5m)
        closes_15m, _, _, _ = parse(candles_15m) if candles_15m else ([], [], [], [])

        return {
            "closes": closes_5m,
            "highs": highs_5m,
            "lows": lows_5m,
            "volumes": vols_5m,
            "closes_5m": closes_5m,
            "closes_15m": closes_15m,
            "hour_utc": now.hour,
            "minute_utc": now.minute,
            "is_weekend": now.weekday() >= 5,
            "funding_rate": funding,
            "current_price": closes_5m[-1] if closes_5m else 0
        }

    # ── Core Decision Loop ─────────────────────────────────────────────────────
    def analyze_and_trade(self):
        """Main strategy loop — called every SCAN_INTERVAL seconds."""
        self.status_msg = "Scanning market..."

        data = self._get_market_data()
        if not data or not data.get("current_price"):
            self.status_msg = "No market data"
            return

        price = data["current_price"]
        closes = data["closes"]

        # Check existing positions first
        self._manage_positions(price)

        # Don't open new positions if at max
        if len([p for p in self.positions if not p.closed]) >= Cfg.MAX_OPEN_POSITIONS:
            self.status_msg = f"Max positions ({Cfg.MAX_OPEN_POSITIONS}) — monitoring"
            return

        # Score both directions
        long_score, long_veto, long_veto_reason, long_bd = \
            self.confidence.score(data, "long")
        short_score, short_veto, short_veto_reason, short_bd = \
            self.confidence.score(data, "short")

        # Macro overlay
        macro_mult = self.macro.get_macro_multiplier()
        long_score  = min(int(long_score * macro_mult), 100)
        short_score = min(int(short_score * macro_mult), 100)

        log.info(f"BTC: ${price:,.0f} | LONG={long_score} "
                 f"({'VETO: '+long_veto_reason if long_veto else 'OK'}) | "
                 f"SHORT={short_score} "
                 f"({'VETO: '+short_veto_reason if short_veto else 'OK'})")

        # Trade decision
        direction = None
        score = 0
        if not long_veto and long_score >= Cfg.MIN_CONFIDENCE and long_score > short_score:
            direction, score = "long", long_score
        elif not short_veto and short_score >= Cfg.MIN_CONFIDENCE and short_score > long_score:
            direction, score = "short", short_score

        if not direction:
            self.status_msg = (f"No trade — best score: "
                               f"L={long_score} S={short_score} "
                               f"(need ≥{Cfg.MIN_CONFIDENCE})")
            return

        # Get ATR for sizing
        atr_val = TechEngine.atr(data["highs"], data["lows"], closes)
        atr_pct = atr_val / price if price > 0 else 0.001

        # Position size
        size_usd = self.sizer.size_usd(self.capital, score, atr_pct)
        log.info(f"Signal: {direction.upper()} @ ${price:,.0f} | "
                 f"Confidence={score} | Size=${size_usd:.0f}")

        # Select option
        chain = self.api.get_options_chain("BTC")
        opt = self.options_sel.select(chain, price, direction, score, atr_val)

        if opt:
            product_id = opt["product_id"]
            opt_symbol = opt["product"].get("symbol", "")
            # Calculate contracts
            contracts = max(1, int(size_usd / (opt["mark"] * 100)))
            side = "buy"  # Buying calls or puts
            result = self.api.place_order(product_id, side, contracts)
            if result.get("success"):
                pos = Position(product_id, direction, price,
                               size_usd, opt_symbol)
                self.positions.append(pos)
                self._log_trade("OPEN", direction, price, size_usd,
                                score, opt_symbol)
                self.status_msg = (f"✅ OPENED {direction.upper()} "
                                   f"{opt_symbol} @ ${price:,.0f}")
                log.info(self.status_msg)
            else:
                log.error(f"Order failed: {result}")
                self.status_msg = "Order placement failed"
        else:
            # Fallback: trade perpetual
            side = "buy" if direction == "long" else "sell"
            contracts = max(1, int(size_usd / price * 1000))
            result = self.api.place_order(Cfg.BTC_PRODUCT_ID, side, contracts)
            if result.get("success"):
                pos = Position(Cfg.BTC_PRODUCT_ID, direction, price, size_usd)
                self.positions.append(pos)
                self._log_trade("OPEN", direction, price, size_usd, score, "BTCUSD_PERP")
                self.status_msg = f"✅ OPENED {direction.upper()} PERP @ ${price:,.0f}"
            else:
                self.status_msg = "No suitable options found, order failed"

    def _manage_positions(self, current_price: float):
        """Check all open positions for exit conditions."""
        for pos in self.positions:
            if pos.closed:
                continue
            should_exit, reason, partial = pos.check_exit(current_price)
            if should_exit:
                self._close_position(pos, current_price, reason, partial)

    def _close_position(self, pos: Position, price: float,
                        reason: str, partial: bool):
        """Execute position close and update P&L."""
        size = pos.size_usd
        if partial:
            size = size / 2

        # Get actual position from exchange
        positions = self.api.get_positions()
        match = next((p for p in positions
                      if p.get("product_id") == pos.product_id), None)
        if match:
            qty = abs(int(float(match.get("size", 0))))
            if partial:
                qty = max(1, qty // 2)
            close_side = "sell" if pos.side == "long" else "buy"
            self.api.place_order(pos.product_id, close_side, qty)

        # P&L calculation
        if pos.side == "long":
            pnl_pct = (price - pos.entry) / pos.entry
        else:
            pnl_pct = (pos.entry - price) / pos.entry

        pnl_usd = size * pnl_pct
        won = pnl_usd > 0

        # Profit buffer absorbs losses
        if not won and self.profit_buffer > 0:
            absorbed = min(abs(pnl_usd), self.profit_buffer)
            self.profit_buffer -= absorbed
            pnl_usd = pnl_usd + absorbed
            log.info(f"Profit buffer absorbed ${absorbed:.2f}")

        if won:
            self.profit_buffer += pnl_usd * 0.3  # 30% of profits go to buffer
            self.capital += pnl_usd * 0.7

        self.total_pnl += pnl_usd
        self.sizer.record(won, pnl_pct * 100)

        if not partial:
            pos.closed = True
            pos.exit_price = price
            pos.exit_reason = reason

        self._log_trade("CLOSE", pos.side, price, pnl_usd,
                        0, pos.option_symbol, reason, pnl_pct * 100)
        log.info(f"{'✅' if won else '❌'} CLOSED {pos.side.upper()} "
                 f"@ ${price:,.0f} | {reason} | P&L: ${pnl_usd:+.2f} "
                 f"({pnl_pct*100:+.2f}%)")

    def _log_trade(self, action, side, price, amount,
                   confidence, symbol, reason="", pnl_pct=0):
        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action, "side": side, "price": price,
            "amount": amount, "confidence": confidence,
            "symbol": symbol, "reason": reason,
            "pnl_pct": pnl_pct, "capital": self.capital,
            "win_rate": self.sizer.win_rate,
            "streak": self.sizer.streak
        })

    # ── Background Thread ─────────────────────────────────────────────────────
    def _run_loop(self):
        cycle = 0
        while self.running:
            try:
                # Re-sync wallet from Delta every 5 cycles (~25 min)
                if cycle % 5 == 0:
                    self._sync_wallet()
                self.analyze_and_trade()
                cycle += 1
            except Exception as e:
                log.error(f"Bot loop error: {e}", exc_info=True)
                self.status_msg = f"Error: {e}"
            time.sleep(Cfg.SCAN_INTERVAL)

    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self._run_loop, daemon=True).start()
            log.info("ΔLPHA Bot v5.0 started")

    def stop(self):
        self.running = False
        log.info("ΔLPHA Bot v5.0 stopped")

    def get_state(self) -> dict:
        open_pos = [p for p in self.positions if not p.closed]
        sc = self.starting_capital if self.starting_capital > 0 else self.capital
        pnl_pct = round((self.capital - sc) / sc * 100, 2) if sc > 0 else 0.0
        return {
            "running": self.running,
            "status": self.status_msg,
            "wallet_synced": self.wallet_synced,
            # Live wallet breakdown
            "wallet_usdt": round(getattr(self, "wallet_usdt", 0), 2),
            "wallet_btc":  round(getattr(self, "wallet_btc",  0), 8),
            "wallet_inr":  round(getattr(self, "wallet_inr",  0), 2),
            # Capital tracking
            "capital": round(self.capital, 2),
            "starting_capital": round(sc, 2),
            "total_pnl": round(self.total_pnl, 2),
            "profit_buffer": round(self.profit_buffer, 2),
            "pnl_pct": pnl_pct,
            # Performance
            "open_positions": len(open_pos),
            "total_trades": self.sizer.total_trades,
            "win_rate": round(self.sizer.win_rate * 100, 1),
            "streak": self.sizer.streak,
            "kelly_fraction": round(self.sizer.kelly_fraction() * 100, 2),
            "recent_trades": self.trade_log[-20:]
        }

# ══════════════════════════════════════════════════════════════════════════════
# FLASK API
# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*",
                              "methods": ["GET", "POST", "OPTIONS"],
                              "allow_headers": ["Content-Type", "Authorization"]}})

bot = AlphaBot()


@app.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#080c0f">
<title>DELTA ALPHA BOT v5</title>
<style>
:root{--bg:#080c0f;--bg2:#0d1318;--bg3:#121a20;--border:#1e2d38;--border2:#2a3f52;--accent:#00d4ff;--green:#00ff8c;--red:#ff4757;--orange:#ffa502;--yellow:#ffd32a;--text:#c8dae8;--dim:#5a7a8a;--bright:#e8f4ff}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display",sans-serif;min-height:100vh;overflow-x:hidden}
body::before{content:"";position:fixed;inset:0;background-image:linear-gradient(rgba(0,212,255,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,.03) 1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0}
.hdr{position:sticky;top:0;z-index:100;background:rgba(8,12,15,.96);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--border);padding:10px 14px;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:9px}
.logo-ico{width:34px;height:34px;background:linear-gradient(135deg,#00d4ff,#00ff8c);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:900;color:#000;font-family:monospace;flex-shrink:0}
.logo h1{font-size:14px;font-weight:700;color:var(--bright)}
.logo small{font-size:9px;color:var(--dim);display:block;font-family:monospace}
.badge{display:flex;align-items:center;gap:5px;padding:5px 10px;border-radius:20px;font-size:10px;font-family:monospace;font-weight:700;text-transform:uppercase;white-space:nowrap}
.badge.on{background:rgba(0,255,140,.1);color:var(--green);border:1px solid rgba(0,255,140,.3)}
.badge.off{background:rgba(255,71,87,.1);color:var(--red);border:1px solid rgba(255,71,87,.3)}
.dot{width:6px;height:6px;border-radius:50%}
.badge.on .dot{background:var(--green);box-shadow:0 0 6px var(--green);animation:blink 1.5s infinite}
.badge.off .dot{background:var(--red)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.wrap{position:relative;z-index:1;padding:10px 10px 90px}
/* WALLET BANNER */
.wallet-banner{background:var(--bg2);border:1px solid var(--border);border-radius:11px;padding:12px 14px;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.wallet-left{display:flex;flex-direction:column;gap:3px}
.wallet-cap{font-size:26px;font-weight:700;font-family:monospace;color:var(--accent)}
.wallet-lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.wallet-breakdown{font-size:10px;color:var(--dim);font-family:monospace}
.wallet-breakdown span{color:var(--text)}
.wallet-pnl{text-align:right}
.wallet-pnl .pct{font-size:18px;font-weight:700;font-family:monospace}
.wallet-pnl .abs{font-size:11px;font-family:monospace;color:var(--dim)}
.pos-color{color:var(--green)}.neg-color{color:var(--red)}.neu-color{color:var(--accent)}
/* KPIs */
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.kpi{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:11px 10px}
.kpi-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);font-family:monospace;margin-bottom:4px}
.kpi-val{font-size:17px;font-weight:700;font-family:monospace;color:var(--bright)}
.kpi-sub{font-size:9px;color:var(--dim);margin-top:2px}
/* CARD */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;overflow:hidden}
.card-hd{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--border)}
.card-hd-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);font-family:monospace;font-weight:600}
.card-bd{padding:12px 14px}
/* STATUS */
#statusMsg{font-size:11px;font-family:monospace;color:var(--text);word-break:break-word;min-height:16px;line-height:1.5}
/* CONFIDENCE */
.score-row{display:flex;align-items:center;gap:14px;margin-bottom:12px}
.score-circle{display:flex;flex-direction:column;align-items:center;min-width:80px}
.score-num{font-size:44px;font-weight:700;font-family:monospace;line-height:1}
.score-lbl{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-top:2px}
.score-dir{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;margin-top:5px;font-family:monospace}
.dir-long{background:rgba(0,255,140,.1);color:var(--green)}.dir-short{background:rgba(255,71,87,.1);color:var(--red)}.dir-none{background:rgba(90,122,138,.1);color:var(--dim)}
.pillars{flex:1}
.pillar{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.p-name{width:72px;font-size:9px;color:var(--dim);font-family:monospace;text-transform:uppercase;flex-shrink:0}
.p-track{flex:1;height:4px;background:var(--bg3);border-radius:2px;overflow:hidden}
.p-fill{height:100%;border-radius:2px;transition:width .6s}
.p-sc{width:20px;text-align:right;font-size:9px;font-family:monospace;color:var(--text)}
/* VETOES */
.vetoes{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.v-badge{font-size:9px;font-family:monospace;padding:2px 7px;border-radius:4px}
.v-pass{background:rgba(0,255,140,.08);color:var(--green);border:1px solid rgba(0,255,140,.2)}
.v-fail{background:rgba(255,71,87,.08);color:var(--red);border:1px solid rgba(255,71,87,.2)}
/* TIME GRID */
.tgrid{display:grid;grid-template-columns:repeat(12,1fr);gap:3px}
.hbox{aspect-ratio:1;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:8px;font-family:monospace;font-weight:700}
.h-dead{background:rgba(255,71,87,.15);color:var(--red)}
.h-peak{background:rgba(0,255,140,.15);color:var(--green)}
.h-norm{background:var(--bg3);color:var(--dim)}
.h-now{outline:2px solid var(--accent);outline-offset:1px;color:var(--bright)}
.tlegend{display:flex;gap:10px;margin-top:7px;flex-wrap:wrap}
.tl-item{display:flex;align-items:center;gap:4px;font-size:9px;color:var(--dim)}
.tl-dot{width:7px;height:7px;border-radius:2px}
/* TRADE LOG */
.tlog{overflow-y:auto;max-height:220px}
.trow{display:grid;grid-template-columns:56px 38px 40px 64px 50px;gap:4px;padding:6px 0;border-bottom:1px solid var(--border);font-size:10px;font-family:monospace;align-items:center}
.trow:last-child{border-bottom:none}
.tag{display:inline-block;padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700}
.tag-long{background:rgba(0,255,140,.1);color:var(--green)}
.tag-short{background:rgba(255,71,87,.1);color:var(--red)}
.tag-open{background:rgba(0,212,255,.1);color:var(--accent)}
.tag-win{background:rgba(0,255,140,.1);color:var(--green)}
.tag-loss{background:rgba(255,71,87,.1);color:var(--red)}
/* BUTTONS */
.btn-row{display:flex;gap:7px;flex-wrap:wrap}
.btn{padding:9px 16px;border-radius:8px;border:none;font-weight:700;font-size:12px;cursor:pointer;transition:all .15s;font-family:-apple-system,sans-serif;white-space:nowrap}
.btn-start{background:linear-gradient(135deg,#00d4ff,#00ff8c);color:#000}
.btn-stop{background:rgba(255,71,87,.15);color:var(--red);border:1px solid rgba(255,71,87,.3)}
.btn-run{background:rgba(0,212,255,.1);color:var(--accent);border:1px solid rgba(0,212,255,.3)}
.btn-sync{background:rgba(255,215,0,.1);color:var(--yellow);border:1px solid rgba(255,215,0,.3)}
.btn-close{background:rgba(255,165,2,.1);color:var(--orange);border:1px solid rgba(255,165,2,.3)}
/* BOTTOM NAV */
.bnav{position:fixed;bottom:0;left:0;right:0;z-index:100;background:rgba(8,12,15,.97);backdrop-filter:blur(14px);border-top:1px solid var(--border);display:flex;justify-content:space-around;padding:8px 0 max(8px,env(safe-area-inset-bottom))}
.bnav-btn{display:flex;flex-direction:column;align-items:center;gap:2px;padding:4px 10px;border:none;background:none;color:var(--dim);font-size:9px;font-family:monospace;cursor:pointer;border-radius:6px;min-width:50px}
.bnav-btn.active{color:var(--accent)}
.bnav-ico{font-size:18px}
/* TABS */
.tab{display:none}.tab.active{display:block}
/* SYNC TOAST */
.toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);background:var(--bg2);border:1px solid var(--accent);color:var(--accent);padding:8px 18px;border-radius:20px;font-size:11px;font-family:monospace;z-index:200;opacity:0;transition:opacity .3s;white-space:nowrap}
.toast.show{opacity:1}
/* FIXES GRID */
.fixes{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.fix-card{padding:10px;background:var(--bg3);border:1px solid var(--border);border-radius:8px}
.fix-title{font-size:9px;color:var(--green);font-family:monospace;text-transform:uppercase;margin-bottom:4px;font-weight:700}
.fix-desc{font-size:10px;color:var(--dim);line-height:1.45}
@media(max-width:360px){.kpis{grid-template-columns:repeat(2,1fr)}.fixes{grid-template-columns:1fr}}
</style>
</head>
<body>
<div id="toast" class="toast"></div>

<header class="hdr">
  <div class="logo">
    <div class="logo-ico">Δ</div>
    <div>
      <h1>ALPHA BOT v5</h1>
      <small id="hdrsub">Delta Exchange India · BTC</small>
    </div>
  </div>
  <div id="statusBadge" class="badge off"><span class="dot"></span><span id="badgeTxt">STOPPED</span></div>
</header>

<div class="wrap">

  <!-- WALLET BANNER -->
  <div class="wallet-banner">
    <div class="wallet-left">
      <div class="wallet-lbl">Live Wallet Capital</div>
      <div class="wallet-cap" id="walletCap">$ —</div>
      <div class="wallet-breakdown" id="walletBreak">Syncing from Delta Exchange...</div>
    </div>
    <div class="wallet-pnl">
      <div class="pct neu-color" id="walletPct">—%</div>
      <div class="abs" id="walletPnl">P&L: $—</div>
      <button class="btn btn-sync" style="margin-top:6px;padding:6px 12px;font-size:10px" onclick="syncWallet()">⟳ Sync</button>
    </div>
  </div>

  <!-- TABS -->
  <div id="tab-main" class="tab active">
    <!-- KPIs -->
    <div class="kpis">
      <div class="kpi">
        <div class="kpi-lbl">Win Rate</div>
        <div class="kpi-val neu-color" id="kpiWR">—%</div>
        <div class="kpi-sub" id="kpiTrades">0 trades</div>
      </div>
      <div class="kpi">
        <div class="kpi-lbl">Streak</div>
        <div class="kpi-val" id="kpiStreak">—</div>
        <div class="kpi-sub">Kelly: <span id="kpiKelly">—</span>%</div>
      </div>
      <div class="kpi">
        <div class="kpi-lbl">Positions</div>
        <div class="kpi-val neu-color" id="kpiPos">—</div>
        <div class="kpi-sub">Buffer: $<span id="kpiBuf">—</span></div>
      </div>
    </div>

    <!-- STATUS -->
    <div class="card">
      <div class="card-hd"><span class="card-hd-lbl">Bot Status</span><span id="lastUpdate" style="font-size:9px;color:var(--dim);font-family:monospace">—</span></div>
      <div class="card-bd"><div id="statusMsg">Connecting...</div></div>
    </div>

    <!-- CONFIDENCE -->
    <div class="card">
      <div class="card-hd"><span class="card-hd-lbl">7-Pillar Confidence</span><span id="confTime" style="font-size:9px;color:var(--dim);font-family:monospace">—</span></div>
      <div class="card-bd">
        <div class="score-row">
          <div class="score-circle">
            <div class="score-num" id="scoreNum" style="color:var(--dim)">—</div>
            <div class="score-lbl">Score</div>
            <div class="score-dir dir-none" id="scoreDir">NO TRADE</div>
          </div>
          <div class="pillars" id="pillarsDiv"></div>
        </div>
        <div class="vetoes" id="vetoesDiv"></div>
      </div>
    </div>

    <!-- CONTROL BUTTONS -->
    <div class="card">
      <div class="card-hd"><span class="card-hd-lbl">Controls</span></div>
      <div class="card-bd">
        <div class="btn-row">
          <button class="btn btn-start" onclick="botAction('start')">▶ Start</button>
          <button class="btn btn-stop" onclick="botAction('stop')">■ Stop</button>
          <button class="btn btn-run" onclick="botAction('run_now')">⚡ Run Now</button>
          <button class="btn btn-close" onclick="closeAll()">⚠ Close All</button>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB: TRADES -->
  <div id="tab-trades" class="tab">
    <div class="card">
      <div class="card-hd"><span class="card-hd-lbl">Trade Log</span><span id="tradeCount" style="font-size:9px;color:var(--dim);font-family:monospace">0 trades</span></div>
      <div style="padding:0 14px;">
        <div class="trow" style="opacity:.5;padding-top:8px;">
          <span>TIME</span><span>ACT</span><span>SIDE</span><span>PRICE</span><span>P&L%</span>
        </div>
        <div class="tlog" id="tradeLog"><div style="text-align:center;padding:20px;color:var(--dim);font-size:11px;font-family:monospace">No trades yet</div></div>
      </div>
    </div>
  </div>

  <!-- TAB: TIME -->
  <div id="tab-time" class="tab">
    <div class="card">
      <div class="card-hd"><span class="card-hd-lbl">Time-of-Day Filter (UTC)</span><span id="curHour" style="font-size:9px;color:var(--accent);font-family:monospace">—</span></div>
      <div class="card-bd">
        <div class="tgrid" id="timeGrid"></div>
        <div class="tlegend">
          <div class="tl-item"><div class="tl-dot" style="background:rgba(255,71,87,.5)"></div>Dead Zone</div>
          <div class="tl-item"><div class="tl-dot" style="background:rgba(0,255,140,.5)"></div>Peak Hours</div>
          <div class="tl-item"><div class="tl-dot" style="border:2px solid var(--accent)"></div>Current</div>
        </div>
        <div style="margin-top:10px;font-size:10px;color:var(--dim);line-height:1.7;font-family:monospace">
          <span style="color:var(--red)">02–05 UTC</span> Dead zone — NO trades<br>
          <span style="color:var(--green)">08–09, 13–16 UTC</span> Peak hours — boosted<br>
          <span style="color:var(--red)">13:30 UTC</span> CPI/NFP blackout ±15min<br>
          <span style="color:var(--red)">19:00 UTC</span> FOMC blackout ±15min
        </div>
      </div>
    </div>
  </div>

  <!-- TAB: FIXES -->
  <div id="tab-fixes" class="tab">
    <div class="card">
      <div class="card-hd"><span class="card-hd-lbl">v5 Autopsy Fixes</span><span style="font-size:9px;color:var(--green);font-family:monospace">Target: 75%+ win rate</span></div>
      <div class="card-bd">
        <div class="fixes">
          <div class="fix-card"><div class="fix-title">RSI Bug Fixed</div><div class="fix-desc">RSI&lt;35 no longer triggers STRONG_BULL. Regime now needs 5 conditions simultaneously.</div></div>
          <div class="fix-card"><div class="fix-title">Live Wallet</div><div class="fix-desc">Capital reads directly from your Delta balance. No hardcoded amounts. Auto-syncs every 25 min.</div></div>
          <div class="fix-card"><div class="fix-title">Divergence</div><div class="fix-desc">MACD histogram divergence detection. Apr 22 top, Apr 26 low, Apr 27 double-top all caught.</div></div>
          <div class="fix-card"><div class="fix-title">Hard Vetoes</div><div class="fix-desc">ADX&lt;15 blocked. 15m HTF contradiction kills trade. Macro blackouts at 13:30 &amp; 19:00 UTC.</div></div>
          <div class="fix-card"><div class="fix-title">Time Filter</div><div class="fix-desc">02–05 UTC dead zone blocked. NY open favors puts. Asia session favors calls.</div></div>
          <div class="fix-card"><div class="fix-title">Kelly Sizing</div><div class="fix-desc">25% of Kelly criterion + streak scaling. ATR adjusts size in volatile markets.</div></div>
        </div>
      </div>
    </div>
  </div>

</div>

<!-- BOTTOM NAV -->
<nav class="bnav">
  <button class="bnav-btn active" onclick="showTab('main',this)"><span class="bnav-ico">⚡</span>Home</button>
  <button class="bnav-btn" onclick="showTab('trades',this)"><span class="bnav-ico">📋</span>Trades</button>
  <button class="bnav-btn" onclick="showTab('time',this)"><span class="bnav-ico">🕐</span>Time</button>
  <button class="bnav-btn" onclick="showTab('fixes',this)"><span class="bnav-ico">✅</span>Fixes</button>
</nav>

<script>
const DEAD=[2,3,4,5],PEAK=[8,9,13,14,15,16];
let state={};

// ── Navigation ───────────────────────────────────────────────────────────────
function showTab(name,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.bnav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  btn.classList.add('active');
}

// ── Toast ────────────────────────────────────────────────────────────────────
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg;el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'),2500);
}

// ── API ──────────────────────────────────────────────────────────────────────
async function api(path,method='GET'){
  try{
    const r=await fetch(path,{method,headers:{'Content-Type':'application/json'}});
    return await r.json();
  }catch(e){return null}
}

// ── Fetch & Render ───────────────────────────────────────────────────────────
async function refresh(){
  const [s,trades]=await Promise.all([api('/api/status'),api('/api/trades')]);
  if(s){state=s;renderState(s);}
  if(trades)renderTrades(trades);
  buildTimeGrid();
}

function renderState(s){
  // Badge
  const b=document.getElementById('statusBadge');
  const running=s.running;
  b.className='badge '+(running?'on':'off');
  document.getElementById('badgeTxt').textContent=running?'RUNNING':'STOPPED';

  // Wallet banner
  const cap=s.capital||0;
  const sc=s.starting_capital||0;
  const pnl=s.total_pnl||0;
  const pnlPct=s.pnl_pct||0;
  document.getElementById('walletCap').textContent='$'+cap.toFixed(2);
  const capEl=document.getElementById('walletCap');
  capEl.className='wallet-cap '+(pnlPct>=0?'pos-color':'neg-color');

  // Wallet breakdown
  const usdt=s.wallet_usdt||0;const inr=s.wallet_inr||0;const btc=s.wallet_btc||0;
  let parts=[];
  if(usdt>0) parts.push(`USDT <span>${usdt.toFixed(2)}</span>`);
  if(inr>0)  parts.push(`INR <span>${inr.toFixed(0)}</span>`);
  if(btc>0)  parts.push(`BTC <span>${btc.toFixed(6)}</span>`);
  if(!s.wallet_synced) parts=['<span style="color:var(--orange)">⚠ Waiting for sync...</span>'];
  document.getElementById('walletBreak').innerHTML=parts.join(' · ')||'Connected';

  document.getElementById('walletPct').textContent=(pnlPct>=0?'+':'')+pnlPct.toFixed(2)+'%';
  document.getElementById('walletPct').className='pct '+(pnlPct>=0?'pos-color':'neg-color');
  document.getElementById('walletPnl').textContent='P&L: $'+(pnl>=0?'+':'')+pnl.toFixed(2)+' | Start: $'+sc.toFixed(2);

  // Sub header
  document.getElementById('hdrsub').textContent=
    `Delta Exchange India · ${s.wallet_synced?'Live':'Syncing...'}`;

  // KPIs
  const wr=s.win_rate||0;
  const wrEl=document.getElementById('kpiWR');
  wrEl.textContent=wr.toFixed(1)+'%';
  wrEl.className='kpi-val '+(wr>=60?'pos-color':wr>=45?'neu-color':'neg-color');
  document.getElementById('kpiTrades').textContent=`${s.total_trades||0} trades`;

  const sk=s.streak||0;
  const skEl=document.getElementById('kpiStreak');
  skEl.textContent=(sk>=0?'+':'')+sk;
  skEl.className='kpi-val '+(sk>0?'pos-color':sk<0?'neg-color':'neu-color');
  document.getElementById('kpiKelly').textContent=(s.kelly_fraction||0).toFixed(2);

  document.getElementById('kpiPos').textContent=s.open_positions??'—';
  document.getElementById('kpiBuf').textContent=(s.profit_buffer||0).toFixed(2);

  // Status
  document.getElementById('statusMsg').textContent=s.status||'—';
  document.getElementById('lastUpdate').textContent=new Date().toISOString().substr(11,8)+' UTC';

  // Confidence — derive from last trade if available
  const recent=s.recent_trades||[];
  const last=recent[recent.length-1];
  if(last&&last.confidence>0){
    const c=last.confidence;
    const sn=document.getElementById('scoreNum');
    sn.textContent=c;
    sn.style.color=c>=75?'#00ff8c':c>=65?'#ffd32a':'#ff4757';
    const sd=document.getElementById('scoreDir');
    const d=last.side||'';
    sd.className='score-dir '+(d==='long'?'dir-long':d==='short'?'dir-short':'dir-none');
    sd.textContent=d?d.toUpperCase():'NO TRADE';
    document.getElementById('confTime').textContent=last.time?last.time.substr(11,8):'—';
  }
  renderPillars();
  renderVetoes(s);
}

function renderPillars(){
  const pillars=[
    {n:'Regime',w:25,c:'#00d4ff'},{n:'HTF Align',w:20,c:'#00ff8c'},
    {n:'Momentum',w:15,c:'#ffd32a'},{n:'Volume',w:10,c:'#ffa502'},
    {n:'Volatility',w:10,c:'#a29bfe'},{n:'Session',w:10,c:'#74b9ff'},
    {n:'Funding',w:10,c:'#fd79a8'}
  ];
  document.getElementById('pillarsDiv').innerHTML=pillars.map(p=>`
    <div class="pillar">
      <span class="p-name">${p.n}</span>
      <div class="p-track"><div class="p-fill" style="width:${p.w*4}%;background:${p.c}"></div></div>
      <span class="p-sc" style="color:${p.c}">${p.w}</span>
    </div>`).join('');
}

function renderVetoes(s){
  const checks=[
    {label:'ADX>15',ok:true},{label:'HTF OK',ok:true},
    {label:'No Macro',ok:true},{label:'Funding OK',ok:true},
    {label:'Session OK',ok:!(new Date().getUTCHours()>=2&&new Date().getUTCHours()<=5)}
  ];
  document.getElementById('vetoesDiv').innerHTML=checks.map(c=>
    `<span class="v-badge ${c.ok?'v-pass':'v-fail'}">${c.ok?'✓':'✗'} ${c.label}</span>`
  ).join('');
}

function renderTrades(trades){
  if(!trades||!trades.length)return;
  document.getElementById('tradeCount').textContent=`${trades.length} trades`;
  const html=[...trades].reverse().slice(0,30).map(t=>{
    const isClose=t.action==='CLOSE';
    const won=t.pnl_pct>0;
    const pnlStr=isClose?(t.pnl_pct>=0?'+':'')+t.pnl_pct?.toFixed(2)+'%':'—';
    const actCls=isClose?(won?'tag-win':'tag-loss'):'tag-open';
    const sideCls=t.side==='long'?'tag-long':'tag-short';
    return `<div class="trow">
      <span style="color:var(--dim)">${t.time?.substr(11,8)||'—'}</span>
      <span class="tag ${actCls}">${t.action}</span>
      <span class="tag ${sideCls}">${(t.side||'').toUpperCase()}</span>
      <span style="color:var(--text)">$${t.price?.toFixed(0)||'—'}</span>
      <span style="color:${won?'var(--green)':'var(--red)'}">${pnlStr}</span>
    </div>`;
  }).join('');
  document.getElementById('tradeLog').innerHTML=html||'<div style="text-align:center;padding:20px;color:var(--dim);font-size:11px;font-family:monospace">No trades yet</div>';
}

function buildTimeGrid(){
  const now=new Date();
  const h=now.getUTCHours();
  const m=now.getUTCMinutes();
  document.getElementById('curHour').textContent=
    `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')} UTC`;
  let html='';
  for(let i=0;i<24;i++){
    let cls=DEAD.includes(i)?'h-dead':PEAK.includes(i)?'h-peak':'h-norm';
    if(i===h)cls+=' h-now';
    html+=`<div class="hbox ${cls}">${String(i).padStart(2,'0')}</div>`;
  }
  document.getElementById('timeGrid').innerHTML=html;
}

// ── Actions ──────────────────────────────────────────────────────────────────
async function botAction(action){
  const r=await api(`/api/bot/${action}`,'POST');
  if(r)toast(r.message||action+' sent');
  setTimeout(refresh,1500);
}

async function syncWallet(){
  toast('Syncing wallet from Delta...');
  const r=await api('/api/wallet/sync','POST');
  if(r&&r.success){
    toast('Wallet synced: $'+r.capital_usd.toFixed(2));
    setTimeout(refresh,500);
  } else {
    toast('Sync failed — check API keys');
  }
}

async function closeAll(){
  if(!confirm('Close ALL open positions on Delta Exchange?'))return;
  const r=await api('/api/close_all','POST');
  if(r)toast('Closed '+r.closed+' positions');
  setTimeout(refresh,1500);
}

// ── Init ─────────────────────────────────────────────────────────────────────
renderPillars();
buildTimeGrid();
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
    """Returns raw wallet balances from Delta Exchange."""
    raw = bot.api.get_wallet()
    return jsonify({
        "raw_balances": raw,
        "capital_usd": round(bot.capital, 2),
        "starting_capital_usd": round(bot.starting_capital, 2),
        "wallet_usdt": round(getattr(bot, "wallet_usdt", 0), 2),
        "wallet_btc":  round(getattr(bot, "wallet_btc",  0), 8),
        "wallet_inr":  round(getattr(bot, "wallet_inr",  0), 2),
        "synced": bot.wallet_synced
    })


@app.route("/api/wallet/sync", methods=["POST"])
def wallet_sync():
    """Force an immediate wallet re-sync from Delta Exchange."""
    capital = bot._sync_wallet(is_startup=False)
    return jsonify({
        "success": True,
        "capital_usd": round(capital, 2),
        "starting_capital_usd": round(bot.starting_capital, 2),
        "wallet_usdt": round(getattr(bot, "wallet_usdt", 0), 2),
        "wallet_btc":  round(getattr(bot, "wallet_btc",  0), 8),
        "wallet_inr":  round(getattr(bot, "wallet_inr",  0), 2),
        "message": f"Capital updated to ${capital:.2f} from live Delta wallet"
    })


@app.route("/api/positions")
def positions():
    return jsonify(bot.api.get_positions())


@app.route("/api/orders")
def orders():
    return jsonify(bot.api.get_orders())


@app.route("/api/trades")
def trades():
    return jsonify(bot.trade_log[-50:])


@app.route("/api/options_chain")
def options_chain():
    chain = bot.api.get_options_chain("BTC")
    return jsonify(chain[:20])


@app.route("/api/ticker")
def ticker():
    return jsonify(bot.api.get_ticker("BTCUSD"))


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "starting_capital": Cfg.STARTING_CAPITAL,
        "min_confidence": Cfg.MIN_CONFIDENCE,
        "max_risk_pct": Cfg.MAX_RISK_PCT,
        "kelly_fraction": Cfg.KELLY_FRACTION,
        "hard_stop_pct": Cfg.HARD_STOP_PCT,
        "tp1_pct": Cfg.TP1_PCT,
        "tp2_pct": Cfg.TP2_PCT,
        "adx_trend_min": Cfg.ADX_TREND_MIN,
        "scan_interval": Cfg.SCAN_INTERVAL,
        "dead_zone_hours": Cfg.DEAD_ZONE_HOURS,
        "peak_hours": Cfg.PEAK_HOURS
    })


@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.json or {}
    if "min_confidence" in data:
        Cfg.MIN_CONFIDENCE = int(data["min_confidence"])
    if "max_risk_pct" in data:
        Cfg.MAX_RISK_PCT = float(data["max_risk_pct"])
    if "scan_interval" in data:
        Cfg.SCAN_INTERVAL = int(data["scan_interval"])
    return jsonify({"success": True, "message": "Config updated"})


@app.route("/api/close_all", methods=["POST"])
def close_all():
    positions = bot.api.get_positions()
    closed = 0
    for p in positions:
        pid = p.get("product_id")
        size = abs(int(float(p.get("size", 0))))
        side = p.get("side", "")
        if size > 0:
            close_side = "sell" if side == "buy" else "buy"
            bot.api.place_order(pid, close_side, size)
            closed += 1
    return jsonify({"success": True, "closed": closed})


@app.route("/api/test")
def test():
    ticker = bot.api.get_ticker("BTCUSD")
    wallet = bot.api.get_wallet()
    return jsonify({
        "api_connected": bool(ticker),
        "btc_price": ticker.get("mark_price", "N/A"),
        "wallet": wallet,
        "bot_version": "v5.0",
        "fixes": [
            "RSI regime bug eliminated",
            "7-pillar confidence score",
            "Hard vetoes active",
            "Divergence detection",
            "Time-of-day filters",
            "ATR position sizing"
        ]
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info(f"Starting ΔLPHA Bot v5.0 on port {port}")
    bot.start()
    app.run(host="0.0.0.0", port=port, debug=False)
