"""
ALPHA BOT — Delta Exchange India | BTCUSD Perpetual
Pure price action trading. No news. No external signals.
"""
import os, time, hmac, hashlib, json, math, logging, threading, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


class C:
    BASE      = "https://api.india.delta.exchange"
    KEY       = os.getenv("DELTA_API_KEY", "").strip()
    SECRET    = os.getenv("DELTA_API_SECRET", "").strip()
    PID       = 27
    LOT_BTC   = 0.001
    LEVERAGE  = 5
    SCAN      = 300
    MIN_CONF  = 58
    STOP_PCT  = 0.025
    TP_PCT    = 0.030
    RISK_PCT  = 0.015
    HALT_PCT  = 0.08
    PAUSE_PCT = 0.03
    STATE     = "/tmp/ab.json"


class API:
    def __init__(self):
        self.key    = C.KEY
        self.secret = C.SECRET
        self.base   = C.BASE
        self.sess   = requests.Session()

    def set(self, key, secret):
        self.key    = key.strip()
        self.secret = secret.strip()

    def _sign(self, method, path, qs="", body=""):
        ts  = str(int(time.time()))
        sig = hmac.new(
            self.secret.encode(),
            (method + ts + path + qs + body).encode(),
            hashlib.sha256).hexdigest()
        return {
            "api-key": self.key, "timestamp": ts,
            "signature": sig, "Content-Type": "application/json"
        }

    def get(self, path, params=None):
        qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
        try:
            r = self.sess.get(f"{self.base}{path}{qs}",
                headers=self._sign("GET", path, qs), timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"GET {path}: {e}")
            return None

    def post(self, path, body):
        b = json.dumps(body)
        try:
            r = self.sess.post(f"{self.base}{path}",
                headers=self._sign("POST", path, "", b),
                data=b, timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"POST {path}: {e}")
            return {}

    def price(self):
        try:
            r = self.sess.get(f"{self.base}/v2/tickers/BTCUSD", timeout=6)
            return float(r.json().get("result", {}).get("mark_price", 0) or 0)
        except Exception:
            return 0.0

    def balance(self):
        """Returns (amount, raw_response, error_string)."""
        d = self.get("/v2/wallet/balances")
        if not d:
            return 0.0, None, "No response from Delta"
        if not d.get("success"):
            err  = d.get("error", {})
            code = err.get("code", "") if isinstance(err, dict) else str(err)
            msg  = d.get("message", "")
            return 0.0, d, f"API error: {code} {msg}".strip()

        # Try every known balance field
        for b in d.get("result", []):
            sym = str(b.get("asset_symbol", "")).upper()
            if sym not in ("USD", "USDT"):
                continue
            avail   = float(b.get("available_balance", 0) or 0)
            blocked = float(b.get("blocked_margin",   0) or 0)
            total   = avail + blocked
            if total > 0:
                return round(total, 2), d, "ok"

        # Fallback: net_equity from meta
        ne = float((d.get("meta") or {}).get("net_equity", 0) or 0)
        if ne > 0:
            return round(ne, 2), d, "ok"

        assets = [b.get("asset_symbol") for b in d.get("result", [])]
        return 0.0, d, f"Balance is zero. Assets: {assets}"

    def candles(self, resolution="5m", limit=100):
        mins  = {"5m": 5, "15m": 15}.get(resolution, 5)
        end   = int(time.time())
        d     = self.get("/v2/history/candles", {
            "symbol": "BTCUSD", "resolution": resolution,
            "start": end - mins * 60 * limit, "end": end
        })
        return d.get("result", []) if d and d.get("success") else []

    def positions(self):
        d = self.get("/v2/positions/margined")
        if d and d.get("success"):
            return [p for p in d.get("result", [])
                    if abs(float(p.get("size", 0) or 0)) > 0]
        return []

    def order(self, side, lots):
        return self.post("/v2/orders", {
            "product_id": C.PID, "size": lots, "side": side,
            "order_type": "market_order", "time_in_force": "ioc"
        })

    def bracket(self, side, lots, stop, tp):
        return self.post("/v2/orders", {
            "product_id": C.PID, "size": lots, "side": side,
            "order_type": "stop_market_order",
            "stop_price": str(round(stop, 1)),
            "bracket_stop_loss_price":   str(round(stop, 1)),
            "bracket_take_profit_price": str(round(tp, 1)),
            "time_in_force": "gtc",
            "stop_trigger_method": "mark_price"
        })

    def close_all(self):
        n = 0
        for p in self.positions():
            sz  = float(p.get("size", 0) or 0)
            qty = abs(int(sz))
            if qty:
                self.post("/v2/orders", {
                    "product_id": p.get("product_id", C.PID),
                    "size": qty,
                    "side": "sell" if sz > 0 else "buy",
                    "order_type": "market_order",
                    "time_in_force": "ioc"
                })
                n += 1
        return n


# ── Indicators ────────────────────────────────────────────────────────

def parse_candles(raw):
    cl, hi, lo, vo = [], [], [], []
    for c in raw:
        try:
            v = float(c.get("close", 0) or 0)
            if v > 0:
                cl.append(v)
                hi.append(float(c.get("high",   v) or v))
                lo.append(float(c.get("low",    v) or v))
                vo.append(float(c.get("volume", 0) or 0))
        except Exception:
            pass
    return cl, hi, lo, vo


def calc_ema(prices, n):
    if len(prices) < n:
        return [prices[-1]] * len(prices) if prices else []
    k = 2.0 / (n + 1)
    v = [sum(prices[:n]) / n]
    for x in prices[n:]:
        v.append(x * k + v[-1] * (1 - k))
    return [v[0]] * (n - 1) + v


def calc_rsi(prices, n=14):
    if len(prices) < n + 2:
        return 50.0
    d = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    g = sum(max(x, 0)  for x in d[-n:]) / n
    l = sum(abs(min(x, 0)) for x in d[-n:]) / n
    return round(100.0 if l < 1e-10 else 100 - 100 / (1 + g / l), 1)


def calc_adx(hi, lo, cl, n=14):
    if len(cl) < n * 2 + 1:
        return 0.0, 0.0, 0.0
    tr, pm, nm = [], [], []
    for i in range(1, len(cl)):
        tr.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
        u = hi[i] - hi[i - 1]
        d = lo[i - 1] - lo[i]
        pm.append(u if u > d and u > 0 else 0.0)
        nm.append(d if d > u and d > 0 else 0.0)

    def wilder(a):
        s = sum(a[:n])
        r = [s]
        for v in a[n:]:
            s = s - s / n + v
            r.append(s)
        return r

    at = wilder(tr)
    pd = wilder(pm)
    nd = wilder(nm)
    pi = [100 * pd[i] / at[i] if at[i] > 0 else 0 for i in range(len(at))]
    ni = [100 * nd[i] / at[i] if at[i] > 0 else 0 for i in range(len(at))]
    dx = [abs(pi[i] - ni[i]) / (pi[i] + ni[i]) * 100
          if pi[i] + ni[i] > 0 else 0 for i in range(len(pi))]
    return round(sum(dx[-n:]) / n, 1), round(pi[-1], 1), round(ni[-1], 1)


def calc_atr(hi, lo, cl, n=14):
    if len(cl) < n + 1:
        return 0.0
    trs = [max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
           for i in range(1, len(cl))]
    return sum(trs[-n:]) / n


def signal_score(cl, hi, lo, vo, cl15, hour, direction):
    """Score a trade direction 0-100. Returns (score, veto_reason)."""
    if len(cl) < 55:
        return 0, "need_55_candles"
    if hour in [2, 3, 4, 5]:
        return 0, "dead_zone_UTC"
    if len(vo) >= 21:
        avg = sum(vo[-21:-1]) / 20
        if vo[-2] < avg * 0.10:
            return 0, "low_volume"

    adx_v, pdi, ndi = calc_adx(hi, lo, cl)
    rsi_v = calc_rsi(cl)
    e8  = calc_ema(cl, 8)[-1]
    e21 = calc_ema(cl, 21)[-1]
    e55 = calc_ema(cl, 55)[-1]
    price = cl[-1]
    bull  = price > e8 > e21 > e55 and adx_v > 20 and pdi > ndi
    bear  = price < e8 < e21 < e55 and adx_v > 20 and ndi > pdi

    s = 0
    # Regime (40 pts)
    if   direction == "long"  and bull: s += 40
    elif direction == "short" and bear: s += 40
    elif adx_v > 15: s += 15
    else: s += 5

    # RSI position (25 pts)
    if direction == "long":
        if   35 <= rsi_v <= 55: s += 25
        elif rsi_v < 35:        s += 20
        elif rsi_v <= 65:       s += 10
    else:
        if   45 <= rsi_v <= 65: s += 25
        elif rsi_v > 65:        s += 20
        elif rsi_v >= 35:       s += 10

    # 15m alignment (20 pts)
    if len(cl15) >= 21:
        e8_  = calc_ema(cl15, 8)[-1]
        e21_ = calc_ema(cl15, 21)[-1]
        if   direction == "long"  and cl15[-1] > e8_ > e21_: s += 20
        elif direction == "short" and cl15[-1] < e8_ < e21_: s += 20
        else: s += 5
    else:
        s += 10

    # ADX strength (15 pts)
    if   adx_v > 30: s += 15
    elif adx_v > 22: s += 10
    elif adx_v > 15: s += 5

    return min(s, 100), ""


# ── Bot ───────────────────────────────────────────────────────────────

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
        self.price     = 0.0
        self.regime    = "—"
        self.rsi_v     = 50.0
        self.adx_v     = 0.0
        self.atr_pct   = 0.0
        self.l_sc      = 0
        self.s_sc      = 0
        self.l_vt      = ""
        self.s_vt      = ""
        self.total_tr  = 0
        self.wins      = 0
        self._stops    = set()

    def emit(self, level, msg):
        entry = {
            "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "l": level, "m": msg
        }
        self.logs.append(entry)
        if len(self.logs) > 500:
            self.logs.pop(0)
        fn = {"INFO": log.info, "WARN": log.warning,
              "ERROR": log.error, "TRADE": log.info}.get(level, log.info)
        fn(msg)

    def save(self):
        try:
            data = {
                "sc": self.start_cap, "ds": self.day_start,
                "halted": self.halted, "hm": self.halt_msg,
                "tr": self.total_tr, "w": self.wins,
                "trades": self.trades[-100:],
                "stops": list(self._stops)
            }
            with open(C.STATE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def load(self):
        try:
            if not os.path.exists(C.STATE):
                return False
            with open(C.STATE) as f:
                s = json.load(f)
            self.start_cap = float(s.get("sc", 0))
            self.day_start = float(s.get("ds", 0))
            self.halted    = bool(s.get("halted", False))
            self.halt_msg  = s.get("hm", "")
            self.total_tr  = int(s.get("tr", 0))
            self.wins      = int(s.get("w", 0))
            self.trades    = s.get("trades", [])
            self._stops    = set(s.get("stops", []))
            if self.start_cap > 0:
                self.emit("INFO",
                    f"Restored: start=${self.start_cap:.2f} "
                    f"trades={self.total_tr}")
                return True
        except Exception:
            pass
        return False

    def connect(self, key, secret):
        self.api.set(key, secret)
        bal, raw, err = self.api.balance()
        if bal <= 0:
            srv = "unknown"
            try:
                srv = requests.get(
                    "https://api.ipify.org?format=json",
                    timeout=4).json().get("ip", "?")
            except Exception:
                pass
            return {"success": False, "message": err,
                    "server_ip": srv, "raw_response": raw}
        self.capital   = bal
        self.connected = True
        if not self.load() or self.start_cap <= 0:
            self.start_cap = bal
            self.day_start = bal
            self.save()
        self.emit("INFO",
            f"Connected | ${bal:.2f} | "
            f"Start ${self.start_cap:.2f} | "
            f"Halt <${self.start_cap*(1-C.HALT_PCT):.2f}")
        self._sync_positions()
        if not self.running:
            self.start()
        return {"success": True, "balance": bal}

    def _sync_positions(self):
        """Read Delta positions → add to trades + place stops."""
        for p in self.api.positions():
            sz    = float(p.get("size", 0) or 0)
            entry = float(p.get("entry_price")
                          or p.get("avg_entry_price") or 0)
            if sz == 0 or entry == 0:
                continue
            pid  = str(p.get("product_id", C.PID))
            sym  = str(p.get("product_symbol", "BTCUSD"))
            side = "long" if sz > 0 else "short"
            lots = abs(int(sz))
            upnl = float(p.get("unrealized_pnl", 0) or 0)

            already = any(
                str(t.get("pid", "")) == pid and t.get("exit") is None
                for t in self.trades)
            if not already:
                self.trades.append({
                    "time":   datetime.now(timezone.utc).isoformat(),
                    "side":   side,
                    "entry":  round(entry, 1),
                    "exit":   None,
                    "lots":   lots,
                    "pnl":    None,
                    "pct":    None,
                    "reason": "synced",
                    "won":    None,
                    "pid":    pid,
                    "sym":    sym,
                    "upnl":   round(upnl, 3)
                })
                self.emit("INFO",
                    f"Synced: {side.upper()} {lots}L {sym} @ ${entry:.0f}")

            if pid not in self._stops:
                sp = entry * (1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT)
                tp = entry * (1+C.TP_PCT   if side=="long" else 1-C.TP_PCT)
                cs = "sell" if side == "long" else "buy"
                r  = self.api.bracket(cs, lots, sp, tp)
                if r.get("success"):
                    self._stops.add(pid)
                    self.emit("INFO",
                        f"Stop placed: stop=${sp:.0f} TP=${tp:.0f}")
                    self.save()
                else:
                    self.emit("WARN",
                        f"Stop FAILED — set manually: ${sp:.0f} | "
                        f"{r.get('error', '?')}")

    def _sync_wallet(self):
        bal, _, err = self.api.balance()
        if bal <= 0:
            self.emit("WARN", f"Wallet sync: {err}")
            return
        self.capital = bal
        if self.start_cap > 0:
            loss = (self.start_cap - bal) / self.start_cap
            if loss >= C.HALT_PCT and not self.halted:
                self.halted   = True
                self.halt_msg = (f"Down {loss*100:.1f}% "
                                 f"(${self.start_cap:.2f} -> ${bal:.2f})")
                self.emit("ERROR", f"BOT HALTED: {self.halt_msg}")
                self.save()
        status = "HALTED" if self.halted else "OK"
        self.emit("INFO", f"Wallet ${bal:.2f} | {status}")

    def _pos_display(self):
        out = []
        for p in self.api.positions():
            sz    = float(p.get("size", 0) or 0)
            entry = float(p.get("entry_price")
                          or p.get("avg_entry_price") or 0)
            if sz == 0 or entry == 0:
                continue
            mark = float(p.get("mark_price") or self.price or entry)
            upnl = float(p.get("unrealized_pnl") or 0)
            side = "long" if sz > 0 else "short"
            pct  = ((mark-entry)/entry if side == "long"
                    else (entry-mark)/entry) * 100
            out.append({
                "sym":  p.get("product_symbol", "BTCUSD"),
                "side": side,
                "lots": abs(sz),
                "entry": round(entry, 1),
                "mark":  round(mark, 1),
                "upnl":  round(upnl, 3),
                "pct":   round(pct, 2),
                "stop":  round(entry*(1-C.STOP_PCT if side=="long"
                                      else 1+C.STOP_PCT), 1),
                "tp":    round(entry*(1+C.TP_PCT if side=="long"
                                     else 1-C.TP_PCT), 1)
            })
        return out

    def _check_exits(self):
        if not self.price:
            return
        for p in self.api.positions():
            sz    = float(p.get("size", 0) or 0)
            entry = float(p.get("entry_price")
                          or p.get("avg_entry_price") or 0)
            if sz == 0 or entry == 0:
                continue
            side = "long" if sz > 0 else "short"
            pct  = ((self.price - entry) / entry if side == "long"
                    else (entry - self.price) / entry)
            lots = abs(int(sz))
            pid  = p.get("product_id", C.PID)

            if pct <= -C.STOP_PCT or pct >= C.TP_PCT:
                cs = "sell" if side == "long" else "buy"
                r  = self.api.post("/v2/orders", {
                    "product_id": pid, "size": lots, "side": cs,
                    "order_type": "market_order", "time_in_force": "ioc"
                })
                if r.get("success"):
                    pnl    = round(entry * lots * C.LOT_BTC * pct, 4)
                    reason = "stop" if pct <= -C.STOP_PCT else "tp"
                    icon   = "STOP" if pct < 0 else "TP"
                    self.emit("TRADE",
                        f"{icon} | {side.upper()} {lots}L "
                        f"${entry:.0f}->${self.price:.0f} "
                        f"P&L ${pnl:+.4f} ({pct*100:.2f}%)")
                    for t in reversed(self.trades):
                        if (t.get("side") == side
                                and t.get("entry") == round(entry, 1)
                                and t.get("exit") is None):
                            t["exit"]   = round(self.price, 1)
                            t["pnl"]    = pnl
                            t["pct"]    = round(pct * 100, 2)
                            t["won"]    = pct > 0
                            t["reason"] = reason
                            if pct > 0:
                                self.wins += 1
                            break
                    self.save()

    def scan(self):
        self.scan_n   += 1
        self.next_scan = (datetime.now(timezone.utc)
                          + timedelta(seconds=C.SCAN)).isoformat()

        p = self.api.price()
        if p > 0:
            self.price = p

        if self.scan_n % 5 == 0:
            self._sync_wallet()

        if self.halted:
            self.status = f"HALTED: {self.halt_msg}"
            return

        raw5  = self.api.candles("5m",  100)
        raw15 = self.api.candles("15m", 60)
        cl, hi, lo, vo = parse_candles(raw5)
        cl15, *_       = parse_candles(raw15)

        if len(cl) < 55:
            self.status = f"{len(cl)} candles — need 55"
            return

        self.price   = cl[-1]
        self.rsi_v   = calc_rsi(cl)
        self.adx_v, pdi, ndi = calc_adx(hi, lo, cl)
        self.atr_pct = round(calc_atr(hi, lo, cl) / self.price * 100, 3)

        e8  = calc_ema(cl, 8)[-1]
        e21 = calc_ema(cl, 21)[-1]
        e55 = calc_ema(cl, 55)[-1]

        if   self.price>e8>e21>e55 and self.adx_v>25 and pdi>ndi:
            self.regime = "STRONG BULL"
        elif self.price>e8>e21 and self.adx_v>18:
            self.regime = "BULL"
        elif self.price<e8<e21<e55 and self.adx_v>25 and ndi>pdi:
            self.regime = "STRONG BEAR"
        elif self.price<e8<e21 and self.adx_v>18:
            self.regime = "BEAR"
        else:
            self.regime = "NEUTRAL"

        real = self.api.positions()
        self._check_exits()
        self._sync_positions()

        if len(real) >= 1:
            d = self._pos_display()
            x = d[0] if d else {}
            self.status = (
                f"Holding {x.get('side','').upper()} "
                f"{x.get('lots',0):.0f}L @ ${x.get('entry',0):,.0f} | "
                f"UPL ${x.get('upnl',0):+.3f} ({x.get('pct',0):+.2f}%)")
            self.emit("INFO", self.status)
            return

        if (self.day_start > 0 and
                (self.capital - self.day_start) / self.day_start <= -C.PAUSE_PCT):
            self.status = "Paused — daily -3% limit"
            return

        hour = datetime.now(timezone.utc).hour
        ls, lv = signal_score(cl, hi, lo, vo, cl15, hour, "long")
        ss, sv = signal_score(cl, hi, lo, vo, cl15, hour, "short")
        self.l_sc = ls; self.s_sc = ss
        self.l_vt = lv; self.s_vt = sv

        lv_str = ("x" + lv) if lv else ""
        sv_str = ("x" + sv) if sv else ""
        self.emit("INFO",
            f"#{self.scan_n} ${self.price:,.0f} {self.regime} "
            f"RSI={self.rsi_v} ADX={self.adx_v} "
            f"L={ls}{lv_str} S={ss}{sv_str}")

        direction = score = None
        if not lv and ls >= C.MIN_CONF and ls > ss:
            direction, score = "long",  ls
        elif not sv and ss >= C.MIN_CONF and ss > ls:
            direction, score = "short", ss

        if not direction:
            why = lv or sv or f"score {max(ls,ss)}<{C.MIN_CONF}"
            self.status = f"Watching — {why} | {self.regime}"
            return

        # Lot sizing
        margin_per_lot = self.price * C.LOT_BTC / C.LEVERAGE
        risk_usd = max(self.capital * C.RISK_PCT, margin_per_lot)
        lots     = max(1, min(
            int(risk_usd / margin_per_lot),
            max(1, int(self.capital * 0.10 / margin_per_lot))
        ))

        side = "buy" if direction == "long" else "sell"
        self.emit("INFO",
            f"Placing {side.upper()} {lots}L @ ${self.price:,.0f} "
            f"score={score}")

        r = self.api.order(side, lots)
        if not r.get("success"):
            err = r.get("error", r.get("message", str(r)[:60]))
            self.status = f"Order failed: {err}"
            self.emit("ERROR", self.status)
            return

        sp = self.price * (1-C.STOP_PCT if direction=="long" else 1+C.STOP_PCT)
        tp = self.price * (1+C.TP_PCT   if direction=="long" else 1-C.TP_PCT)
        cs = "sell" if direction == "long" else "buy"
        sr = self.api.bracket(cs, lots, sp, tp)
        if sr.get("success"):
            self._stops.add(str(C.PID))
            self.emit("INFO", f"Stop ${sp:.0f} TP ${tp:.0f}")
        else:
            self.emit("WARN", f"BRACKET FAILED — set stop manually at ${sp:.0f}")

        self.status = f"{direction.upper()} {lots}L @ ${self.price:,.0f} score={score}"
        self.emit("TRADE", self.status)
        self.total_tr += 1
        self.trades.append({
            "time":   datetime.now(timezone.utc).isoformat(),
            "side":   direction,
            "entry":  round(self.price, 1),
            "exit":   None,
            "lots":   lots,
            "pnl":    None,
            "pct":    None,
            "reason": "bot",
            "won":    None,
            "pid":    str(C.PID),
            "sym":    "BTCUSD"
        })
        self.save()

    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self._loop, daemon=True).start()
            self.emit("INFO", "Bot started")

    def stop(self):
        self.running = False
        self.emit("INFO", "Bot stopped")

    def _loop(self):
        while self.running:
            try:
                self.scan()
            except Exception as e:
                log.error(f"Scan error: {e}", exc_info=True)
                self.status = f"Error: {e}"
            time.sleep(C.SCAN)

    def state(self):
        sc   = self.start_cap or self.capital
        pnl  = (self.capital - sc) / sc * 100 if sc > 0 else 0.0
        done = [t for t in self.trades if t.get("won") is not None]
        wr   = sum(1 for t in done if t["won"]) / len(done) * 100 if done else 0
        return {
            "running":      self.running,
            "connected":    self.connected,
            "halted":       self.halted,
            "halt_msg":     self.halt_msg,
            "status":       self.status,
            "price":        round(self.price, 1),
            "regime":       self.regime,
            "rsi":          self.rsi_v,
            "adx":          self.adx_v,
            "atr_pct":      self.atr_pct,
            "l_sc":         self.l_sc,
            "s_sc":         self.s_sc,
            "l_vt":         self.l_vt,
            "s_vt":         self.s_vt,
            "capital":      round(self.capital, 2),
            "start_cap":    round(sc, 2),
            "pnl_pct":      round(pnl, 2),
            "win_rate":     round(wr, 1),
            "total_trades": self.total_tr,
            "wins":         self.wins,
            "next_scan":    self.next_scan,
            "scan_n":       self.scan_n,
            "open_pos":     self._pos_display(),
            "trades":       list(reversed(self.trades[-50:])),
            "logs":         list(reversed(self.logs[-100:])),
            "guardrails": {
                "Hard stop":     f"{C.STOP_PCT*100:.1f}% bracket on Delta",
                "Take profit":   f"{C.TP_PCT*100:.1f}%",
                "Monthly halt":  f"Down {C.HALT_PCT*100:.0f}% from start",
                "Daily pause":   f"Down {C.PAUSE_PCT*100:.0f}% today",
                "Max positions": "1 (live Delta API check)"
            }
        }


# ── Flask ─────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)
bot = Bot()

if C.KEY and C.SECRET:
    threading.Thread(
        target=lambda: bot.connect(C.KEY, C.SECRET),
        daemon=True).start()


@app.after_request
def _cors(r):
    r.headers.update({
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    })
    return r


@app.route("/api/status")
@app.route("/api/bot/status")
def api_status():
    return jsonify(bot.state())


@app.route("/api/connect", methods=["POST", "OPTIONS"])
def api_connect():
    if request.method == "OPTIONS":
        return jsonify({})
    d = request.json or {}
    k = d.get("api_key", "").strip()
    s = d.get("api_secret", "").strip()
    if not k or not s:
        return jsonify({"success": False, "message": "Key and secret required"})
    return jsonify(bot.connect(k, s))


@app.route("/api/bot/start", methods=["POST"])
def api_start():
    bot.start()
    return jsonify({"success": True})


@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    bot.stop()
    return jsonify({"success": True})


@app.route("/api/bot/run_now", methods=["POST"])
def api_run_now():
    threading.Thread(target=bot.scan, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/trades")
def api_trades():
    return jsonify(list(reversed(bot.trades[-50:])))


@app.route("/api/logs")
def api_logs():
    return jsonify(bot.logs)


@app.route("/api/positions")
def api_positions():
    return jsonify({"raw": bot.api.positions(), "display": bot._pos_display()})


@app.route("/api/ticker")
def api_ticker():
    p = bot.api.price()
    if not p:
        try:
            p = requests.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=bitcoin&vs_currencies=usd",
                timeout=5).json()["bitcoin"]["usd"]
        except Exception:
            p = 0
    return jsonify({"price": p})


@app.route("/api/ip")
def api_ip():
    try:
        ip = requests.get(
            "https://api.ipify.org?format=json",
            timeout=5).json().get("ip", "?")
    except Exception:
        ip = "unknown"
    return jsonify({"ip": ip})


@app.route("/api/close_all", methods=["POST"])
def api_close_all():
    n = bot.api.close_all()
    bot.emit("TRADE", f"Emergency close: {n} position(s)")
    return jsonify({"success": True, "closed": n})


@app.route("/api/manual_trade", methods=["POST"])
def api_manual():
    d    = request.json or {}
    dirn = d.get("direction", "")
    if dirn not in ("long", "short"):
        return jsonify({"success": False, "message": "direction: long or short"})
    p    = bot.price or bot.api.price()
    lots = max(1, int(d.get("lots", 1)))
    side = "buy" if dirn == "long" else "sell"
    r    = bot.api.order(side, lots)
    if r.get("success"):
        sp = p * (1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp = p * (1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
        bot.api.bracket("sell" if dirn=="long" else "buy", lots, sp, tp)
        bot.emit("TRADE",
            f"MANUAL {dirn.upper()} {lots}L @${p:,.0f} "
            f"stop=${sp:.0f} TP=${tp:.0f}")
        bot.trades.append({
            "time":   datetime.now(timezone.utc).isoformat(),
            "side":   dirn, "entry": round(p, 1), "exit": None,
            "lots":   lots, "pnl": None, "pct": None,
            "reason": "manual", "won": None,
            "pid":    str(C.PID), "sym": "BTCUSD"
        })
        bot.save()
        return jsonify({"success": True, "entry": round(p, 1),
                        "stop": round(sp, 1), "tp": round(tp, 1)})
    return jsonify({"success": False,
                    "message": r.get("error", "Order failed")})


@app.route("/api/set_stop", methods=["POST"])
def api_set_stop():
    d     = request.json or {}
    dirn  = d.get("direction", "long")
    entry = float(d.get("entry", bot.price or 77000))
    lots  = int(d.get("lots", 1))
    sp    = entry * (1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
    tp    = entry * (1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
    cs    = "sell" if dirn == "long" else "buy"
    r     = bot.api.bracket(cs, lots, sp, tp)
    ok    = r.get("success", False)
    bot.emit("INFO" if ok else "WARN",
        f"{'OK' if ok else 'FAIL'} Stop: {dirn.upper()} "
        f"${entry:.0f} stop=${sp:.0f} TP=${tp:.0f}")
    return jsonify({"success": ok, "stop": round(sp, 1), "tp": round(tp, 1)})


@app.route("/api/debug/auth")
def api_debug_auth():
    out = {"key_len": len(bot.api.key), "key_set": bool(bot.api.key),
           "secret_len": len(bot.api.secret)}
    try:
        r = requests.get(f"{bot.api.base}/v2/tickers/BTCUSD", timeout=6)
        out["ticker_ok"]  = r.status_code == 200
        out["btc_price"]  = r.json().get("result", {}).get("mark_price", "?")
    except Exception as e:
        out["ticker_error"] = str(e)
    bal, raw, err = bot.api.balance()
    out["balance"]  = bal
    out["bal_err"]  = err
    out["raw"]      = raw
    return jsonify(out)


@app.route("/api/debug/candles")
def api_debug_candles():
    for res in ["5m", "1m", "15m"]:
        d = bot.api.get("/v2/history/candles", {
            "symbol": "BTCUSD", "resolution": res,
            "start": int(time.time()) - 3600,
            "end":   int(time.time())
        })
        if d and d.get("success") and d.get("result"):
            return jsonify({"ok": True, "res": res,
                            "count": len(d["result"]),
                            "sample": d["result"][0]})
    return jsonify({"ok": False, "error": "all resolutions failed"})


@app.route("/api/debug/positions")
def api_debug_positions():
    return jsonify(bot.api.get("/v2/positions/margined")
                   or {"error": "no_response"})



DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alpha Bot</title>
<style>
:root{--g:#00b386;--r:#e74c3c;--y:#f39c12;--b:#2980b9;--bg:#f5f6fa;--w:#fff;--t:#1a1a2e;--t2:#6b7280}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:14px}
.hdr{background:#fff;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 3px rgba(0,0,0,.07);position:sticky;top:0;z-index:100}
.logo{font-size:17px;font-weight:700;color:var(--g);display:flex;align-items:center;gap:8px}
.li{background:var(--g);color:#fff;width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:900}
.pill{font-size:11px;font-weight:600;padding:5px 11px;border-radius:20px;display:flex;align-items:center;gap:5px}
.pill-ok{background:#ecfdf5;color:var(--g)}.pill-off{background:#fef2f2;color:var(--r)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.tabs{background:#fff;display:flex;border-bottom:1px solid #e5e7eb;position:sticky;top:57px;z-index:99}
.tab{flex:1;padding:12px 4px;text-align:center;font-size:11px;font-weight:700;color:var(--t2);border-bottom:2px solid transparent;cursor:pointer;text-transform:uppercase;letter-spacing:.6px}
.tab.on{color:var(--g);border-color:var(--g)}
.pnl{display:none;padding:12px 14px 24px}.pnl.on{display:block}
.card{background:#fff;border-radius:14px;padding:16px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.ct{font-size:10px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.7px;margin-bottom:12px}
.priceval{font-size:38px;font-weight:700;letter-spacing:-2px}
.chip{display:inline-flex;align-items:center;gap:4px;margin-top:8px;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600}
.chip::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.c-bull{background:#ecfdf5;color:var(--g)}.c-bear{background:#fef2f2;color:var(--r)}.c-neu{background:#f9fafb;color:var(--t2)}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.sb{background:#f8f9fa;border-radius:10px;padding:11px 8px;text-align:center;border:1px solid #e5e7eb}
.slbl{font-size:9px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}
.snum{font-size:24px;font-weight:700;line-height:1}
.sg{color:var(--g)}.sr{color:var(--r)}.sy{color:var(--y)}
.ssub{font-size:9px;color:#bbb;margin-top:3px;min-height:11px}
.ib{background:#f8f9fa;border-radius:10px;padding:10px;text-align:center;border:1px solid #e5e7eb}
.il{font-size:9px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.iv{font-size:17px;font-weight:700}
.sbar{background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:10px 12px;font-size:12px;color:#1d4ed8;margin-bottom:10px;min-height:34px;line-height:1.6;font-weight:500}
.sbar-h{background:#f0fdf4;border-color:#bbf7d0;color:#15803d}
.sbar-e{background:#fef2f2;border-color:#fecaca;color:#dc2626}
.prog{height:3px;background:#e5e7eb;border-radius:2px;overflow:hidden;margin:8px 0 4px}
.progf{height:100%;background:var(--g);border-radius:2px;transition:width .5s}
.cd{font-size:11px;color:var(--t2)}
.pc{border-radius:12px;padding:14px;margin-bottom:10px}
.pc-long{background:#f0fdf4;border:1px solid #a7f3d0}.pc-short{background:#fff5f5;border:1px solid #fca5a5}
.ph{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.psym{font-size:15px;font-weight:700}
.pbadge{padding:3px 11px;border-radius:20px;font-size:11px;font-weight:700}
.pb-long{background:var(--g);color:#fff}.pb-short{background:var(--r);color:#fff}
.pg{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.pi{background:rgba(255,255,255,.75);border-radius:8px;padding:9px}
.pil{font-size:9px;color:var(--t2);font-weight:700;text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px}
.piv{font-size:14px;font-weight:700}.piv-r{color:var(--r)}.piv-g{color:var(--g)}
.wrow{display:flex;justify-content:space-between;align-items:baseline}
.wamt{font-size:32px;font-weight:700}.wpct{font-size:16px;font-weight:700}
.wst{font-size:11px;color:var(--t2);margin-top:3px}
.stbox{background:#f8f9fa;border-radius:10px;padding:12px;text-align:center;border:1px solid #e5e7eb}
.stl{font-size:10px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
.stv{font-size:20px;font-weight:700}
.btn{padding:14px;border-radius:12px;border:none;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;width:100%;transition:.15s}
.btn:active{opacity:.85}
.btn-g{background:var(--g);color:#fff}.btn-r{background:var(--r);color:#fff}
.btn-b{background:var(--b);color:#fff}.btn-rc{background:var(--r);color:#fff;opacity:.8}
.btn-gl{background:#ecfdf5;color:var(--g);border:1.5px solid var(--g);flex:1}
.btn-rl{background:#fef2f2;color:var(--r);border:1.5px solid var(--r);flex:1}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
.mrow{display:flex;gap:8px;margin-top:8px}
.inp{width:100%;border:1.5px solid #e5e7eb;border-radius:10px;padding:12px 14px;font-size:14px;font-family:inherit;outline:none;background:#fafafa}
.inp:focus{border-color:var(--g);background:#fff}
.ti{background:#f9fafb;border-radius:11px;padding:12px;margin-bottom:8px;border:1px solid #e5e7eb}
.tt{display:flex;justify-content:space-between;margin-bottom:6px}
.tsl{font-weight:700}.tsl-l{color:var(--g)}.tsl-s{color:var(--r)}
.ttm{font-size:11px;color:var(--t2)}
.tpr{display:flex;justify-content:space-between;font-size:12px;color:var(--t2)}
.tpnl{font-size:13px;font-weight:700;margin-top:5px}
.topen{font-size:11px;color:var(--y);font-style:italic}
.ttag{font-size:9px;padding:1px 6px;border-radius:6px;margin-left:5px;font-weight:700}
.ttag-b{background:#ede9fe;color:#5b21b6}.ttag-m{background:#dbeafe;color:#1e40af}
.ttag-s{background:#d1fae5;color:#065f46}.ttag-y{background:#fef9c3;color:#713f12}
.lb{background:#1e293b;border-radius:11px;padding:12px;max-height:370px;overflow-y:auto}
.le{padding:4px 0;border-bottom:1px solid #2d3748;font-size:11px;display:flex;gap:8px;font-family:monospace}
.lt{color:#64748b;white-space:nowrap;flex-shrink:0}
.lINFO{color:#94a3b8}.lWARN{color:var(--y)}.lERROR{color:var(--r)}.lTRADE{color:var(--g);font-weight:700}
.frow{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.fp{padding:5px 12px;border-radius:20px;border:1px solid #e5e7eb;background:#fff;font-size:11px;font-weight:700;cursor:pointer;color:var(--t2)}
.fp.on{background:#1e293b;color:#fff;border-color:#1e293b}
.gi{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f0f0f0;font-size:13px}
.gi:last-child{border:none}
.gk{color:var(--t2)}.gv{color:var(--g);font-weight:700;text-align:right;max-width:58%}
.hbanner{background:#fef2f2;border:1.5px solid #fca5a5;border-radius:12px;padding:14px;margin-bottom:10px;text-align:center;color:var(--r);font-weight:700}
.ipd{font-size:20px;font-weight:700;text-align:center;padding:12px;background:#f8f9fa;border-radius:10px;font-family:monospace;letter-spacing:2px;margin-bottom:8px;border:1px solid #e5e7eb}
.empty{text-align:center;padding:40px;color:var(--t2)}
.lc{font-size:11px;color:var(--t2);margin-bottom:8px}
</style>
</head>
<body>
<div class="hdr">
  <div class="logo"><div class="li">&#916;</div>ALPHA BOT</div>
  <div class="pill pill-off" id="cPill"><div class="dot"></div><span id="cLbl">Not connected</span></div>
</div>
<div class="tabs">
  <div class="tab on"  id="tab-home">Home</div>
  <div class="tab"     id="tab-trades">Trades</div>
  <div class="tab"     id="tab-logs">Logs</div>
  <div class="tab"     id="tab-settings">Settings</div>
</div>
<div id="pnl-home" class="pnl on">
  <div id="hbanner" class="hbanner" style="display:none"></div>
  <div class="card">
    <div class="ct">Bitcoin Live</div>
    <div class="priceval" id="btcPrice">$-</div>
    <div class="chip c-neu" id="regChip">Loading...</div>
  </div>
  <div class="card">
    <div class="sbar" id="statusBar">Initializing...</div>
    <div class="g3" style="margin-bottom:10px">
      <div class="sb"><div class="slbl">Long</div><div class="snum sg" id="lScore">-</div><div class="ssub" id="lVeto"></div></div>
      <div class="sb"><div class="slbl">Short</div><div class="snum sr" id="sScore">-</div><div class="ssub" id="sVeto"></div></div>
      <div class="sb"><div class="slbl">Signal</div><div class="snum sy" id="decVal">WAIT</div><div class="ssub" id="decSub">No signal</div></div>
    </div>
    <div class="g3">
      <div class="ib"><div class="il">RSI 14</div><div class="iv" id="rsiV">-</div></div>
      <div class="ib"><div class="il">ADX 14</div><div class="iv" id="adxV">-</div></div>
      <div class="ib"><div class="il">ATR %</div><div class="iv" id="atrV">-</div></div>
    </div>
    <div class="prog"><div class="progf" id="scanBar" style="width:0"></div></div>
    <div class="cd" id="countdown">Next scan in -</div>
  </div>
  <div id="openPosArea"></div>
  <div class="card">
    <div class="ct">Wallet Balance</div>
    <div class="wrow"><div class="wamt" id="walletAmt">$-</div><div class="wpct sg" id="walletPct">-</div></div>
    <div class="wst" id="walletStart"></div>
  </div>
  <div class="card">
    <div class="g3">
      <div class="stbox"><div class="stl">Win Rate</div><div class="stv sg" id="winRate">-</div></div>
      <div class="stbox"><div class="stl">Trades</div><div class="stv" id="tradeCount">0</div></div>
      <div class="stbox"><div class="stl">Scan</div><div class="stv" style="color:var(--b)" id="scanN">0</div></div>
    </div>
  </div>
  <div class="g2">
    <button class="btn btn-g" id="btnStart">&#9654; Start</button>
    <button class="btn btn-r" id="btnStop">&#9632; Stop</button>
  </div>
  <button class="btn btn-b" id="btnScan" style="margin-bottom:8px">&#9889; Scan Now</button>
  <button class="btn btn-rc" id="btnClose" style="margin-bottom:10px">Close All Positions</button>
  <div class="card">
    <div class="ct">Manual Trade</div>
    <input class="inp" id="manLots" type="number" placeholder="Lots (default: 1)" min="1">
    <div class="mrow">
      <button class="btn btn-gl" id="btnLong">Buy Long</button>
      <button class="btn btn-rl" id="btnShort">Sell Short</button>
    </div>
  </div>
</div>
<div id="pnl-trades" class="pnl">
  <div id="tradesList"><div class="empty">No trades yet</div></div>
</div>
<div id="pnl-logs" class="pnl">
  <div class="frow">
    <div class="fp on" id="fp-all">All</div>
    <div class="fp" id="fp-TRADE">Trades</div>
    <div class="fp" id="fp-WARN">Warnings</div>
    <div class="fp" id="fp-ERROR">Errors</div>
  </div>
  <div class="lc" id="logCount">0 entries</div>
  <div class="lb" id="logBox"></div>
</div>
<div id="pnl-settings" class="pnl">
  <div class="card">
    <div class="ct">Delta Exchange Login</div>
    <input class="inp" id="apiKey" type="text" placeholder="API Key">
    <input class="inp" id="apiSecret" type="password" placeholder="API Secret" style="margin-top:8px">
    <button class="btn" style="background:#1e293b;color:#fff;margin:8px 0" id="btnIndia">India Check</button>
    <button class="btn btn-g" id="btnConnect">Connect to Delta Exchange</button>
    <div id="connMsg" style="margin-top:8px;font-size:12px;text-align:center;line-height:1.6"></div>
  </div>
  <div class="card">
    <div class="ct">Server IP - Whitelist on Delta</div>
    <div class="ipd" id="serverIp">Loading...</div>
    <div style="font-size:12px;color:var(--t2);line-height:1.9">1. Copy IP above<br>2. Delta - Account - API Keys - Edit<br>3. Paste into IP Whitelist - Save</div>
  </div>
  <div class="card">
    <div class="ct">Active Guardrails</div>
    <div id="guardrailsList"></div>
  </div>
</div>
<script>
var allLogs = [], logFilter = '', currentTrades = [];

function switchTab(name) {
  var tabs = ['home', 'trades', 'logs', 'settings'];
  for (var i = 0; i < tabs.length; i++) {
    var t = tabs[i];
    document.getElementById('tab-' + t).classList.toggle('on', t === name);
    document.getElementById('pnl-' + t).classList.toggle('on', t === name);
  }
  if (name === 'logs') renderLogs();
  if (name === 'trades') renderTrades();
}

function callApi(url, body) {
  var opts = {};
  if (body !== undefined) {
    opts = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  }
  return fetch(url, opts).then(function(r) { return r.json(); }).catch(function(e) {
    console.error('API error:', url, e);
    return null;
  });
}

function render(s) {
  if (!s) return;
  var ok = s.connected && !s.halted;
  document.getElementById('cPill').className = 'pill ' + (ok ? 'pill-ok' : 'pill-off');
  document.getElementById('cLbl').textContent = s.halted ? 'HALTED' : (s.connected ? 'Connected' : 'Not connected');

  var hb = document.getElementById('hbanner');
  hb.style.display = s.halted ? 'block' : 'none';
  if (s.halted) hb.textContent = 'HALTED: ' + s.halt_msg;

  document.getElementById('btcPrice').textContent = s.price ? '$' + s.price.toLocaleString() : '$-';

  var rc = document.getElementById('regChip');
  rc.textContent = s.regime || '-';
  var rg = (s.regime || '').toLowerCase();
  rc.className = 'chip ' + (rg.indexOf('bull') >= 0 ? 'c-bull' : rg.indexOf('bear') >= 0 ? 'c-bear' : 'c-neu');

  var sb = document.getElementById('statusBar');
  sb.textContent = s.status || '-';
  var sbCls = 'sbar';
  if (s.status && s.status.indexOf('Holding') >= 0) sbCls += ' sbar-h';
  else if (s.status && s.status.indexOf('HALT') >= 0) sbCls += ' sbar-e';
  sb.className = sbCls;

  var ls = s.l_sc || 0, ss = s.s_sc || 0;
  document.getElementById('lScore').textContent = ls || '-';
  document.getElementById('sScore').textContent = ss || '-';
  document.getElementById('lVeto').textContent = s.l_vt ? 'x ' + s.l_vt : '';
  document.getElementById('sVeto').textContent = s.s_vt ? 'x ' + s.s_vt : '';

  var dec = 'WAIT', ds = 'No signal', dc = 'sy';
  if (!s.l_vt && ls >= 58 && ls > ss) { dec = 'LONG'; ds = 'score=' + ls; dc = 'sg'; }
  if (!s.s_vt && ss >= 58 && ss > ls) { dec = 'SHORT'; ds = 'score=' + ss; dc = 'sr'; }
  var de = document.getElementById('decVal');
  de.textContent = dec; de.className = 'snum ' + dc;
  document.getElementById('decSub').textContent = ds;

  document.getElementById('rsiV').textContent = s.rsi ? String(s.rsi) : '-';
  document.getElementById('adxV').textContent = s.adx ? String(s.adx) : '-';
  document.getElementById('atrV').textContent = s.atr_pct ? s.atr_pct + '%' : '-';

  if (s.next_scan) {
    var secs = Math.max(0, Math.round((new Date(s.next_scan) - Date.now()) / 1000));
    document.getElementById('scanBar').style.width = Math.max(0, 100 - secs / 300 * 100) + '%';
    document.getElementById('countdown').textContent = secs > 0 ?
      ('Next scan in ' + Math.floor(secs / 60) + 'm ' + (secs % 60) + 's') : 'Scanning...';
  }

  var ops = s.open_pos || [];
  var opa = document.getElementById('openPosArea');
  var opsHtml = '';
  for (var i = 0; i < ops.length; i++) {
    var p = ops[i];
    var neg = p.upnl < 0;
    opsHtml += '<div class="pc pc-' + p.side + '">';
    opsHtml += '<div class="ph"><span class="psym">' + p.sym + '</span>';
    opsHtml += '<span class="pbadge pb-' + p.side + '">' + p.side.toUpperCase() + '</span></div>';
    opsHtml += '<div class="pg">';
    opsHtml += '<div class="pi"><div class="pil">Entry</div><div class="piv">$' + p.entry.toLocaleString() + '</div></div>';
    opsHtml += '<div class="pi"><div class="pil">Lots</div><div class="piv">' + p.lots + '</div></div>';
    var uplCls = neg ? 'piv-r' : 'piv-g';
    var uplSign = p.upnl >= 0 ? '+' : '';
    var pctSign = p.pct >= 0 ? '+' : '';
    opsHtml += '<div class="pi"><div class="pil">UPL</div><div class="piv ' + uplCls + '">' + uplSign + p.upnl + ' (' + pctSign + p.pct + '%)</div></div>';
    opsHtml += '<div class="pi"><div class="pil">Mark</div><div class="piv">$' + (p.mark || p.entry).toLocaleString() + '</div></div>';
    opsHtml += '<div class="pi"><div class="pil">Stop Loss</div><div class="piv piv-r">$' + p.stop.toLocaleString() + '</div></div>';
    opsHtml += '<div class="pi"><div class="pil">Take Profit</div><div class="piv piv-g">$' + p.tp.toLocaleString() + '</div></div>';
    opsHtml += '</div></div>';
  }
  opa.innerHTML = opsHtml;

  document.getElementById('walletAmt').textContent = s.capital ? '$' + s.capital.toFixed(2) : '$-';
  var pp = s.pnl_pct || 0;
  var wp = document.getElementById('walletPct');
  wp.textContent = (pp >= 0 ? '+' : '') + pp.toFixed(2) + '%';
  wp.className = 'wpct ' + (pp >= 0 ? 'sg' : 'sr');
  document.getElementById('walletStart').textContent = s.start_cap ? 'Started: $' + s.start_cap.toFixed(2) : '';
  document.getElementById('winRate').textContent = s.win_rate != null ? s.win_rate + '%' : '-';
  document.getElementById('tradeCount').textContent = s.total_trades || 0;
  document.getElementById('scanN').textContent = s.scan_n || 0;

  if (s.logs) allLogs = s.logs;
  if (s.trades) currentTrades = s.trades;
  document.getElementById('logCount').textContent = allLogs.length + ' entries';
  if (document.getElementById('pnl-logs').classList.contains('on')) renderLogs();
  if (document.getElementById('pnl-trades').classList.contains('on')) renderTrades();

  if (s.guardrails) {
    var keys = Object.keys(s.guardrails);
    var gHtml = '';
    for (var i = 0; i < keys.length; i++) {
      gHtml += '<div class="gi"><span class="gk">' + keys[i] + '</span><span class="gv">' + s.guardrails[keys[i]] + '</span></div>';
    }
    document.getElementById('guardrailsList').innerHTML = gHtml;
  }
}

function renderLogs() {
  var f = logFilter ? allLogs.filter(function(e) { return e.l === logFilter; }) : allLogs;
  var html = '';
  for (var i = 0; i < Math.min(f.length, 120); i++) {
    var e = f[i];
    html += '<div class="le"><span class="lt">' + e.t + '</span><span class="l' + e.l + '">' + e.m + '</span></div>';
  }
  document.getElementById('logBox').innerHTML = html;
}

function FL(f) {
  logFilter = f;
  var ids = ['fp-all', 'fp-TRADE', 'fp-WARN', 'fp-ERROR'];
  var vals = ['', 'TRADE', 'WARN', 'ERROR'];
  for (var i = 0; i < ids.length; i++) {
    var el = document.getElementById(ids[i]);
    if (el) el.classList.toggle('on', f === vals[i]);
  }
  renderLogs();
}

function renderTrades() {
  var el = document.getElementById('tradesList');
  if (!currentTrades.length) {
    el.innerHTML = '<div class="empty">No trades yet</div>';
    return;
  }
  var html = '';
  for (var i = 0; i < currentTrades.length; i++) {
    var t = currentTrades[i];
    var open = t.exit == null;
    var sc = t.side === 'long' ? 'tsl tsl-l' : 'tsl tsl-s';
    var tag = t.reason === 'synced' ? '<span class="ttag ttag-y">synced</span>' :
              t.reason === 'manual' ? '<span class="ttag ttag-m">manual</span>' :
              '<span class="ttag ttag-b">bot</span>';
    var pnlStr = '';
    if (!open) {
      var pnlSign = (t.pnl > 0) ? '+' : '';
      var pctSign = (t.pct > 0) ? '+' : '';
      pnlStr = '<div class="tpnl ' + (t.won ? 'sg' : 'sr') + '">' +
        (t.won ? 'Profit' : 'Loss') + ': $' + pnlSign + (t.pnl || 0).toFixed(4) +
        ' (' + pctSign + (t.pct || 0).toFixed(2) + '%) ' + t.reason + '</div>';
    } else {
      pnlStr = '<div class="topen">Open position...</div>';
    }
    var exitStr = open ? '' : '<span>Exit $' + (t.exit || 0).toLocaleString() + '</span>';
    html += '<div class="ti">';
    html += '<div class="tt"><span class="' + sc + '">' + t.side.toUpperCase() + ' ' + t.lots + 'L ' + (t.sym || 'BTCUSD') + tag + '</span>';
    html += '<span class="ttm">' + new Date(t.time).toLocaleTimeString() + '</span></div>';
    html += '<div class="tpr"><span>Entry $' + (t.entry || 0).toLocaleString() + '</span>' + exitStr + '</div>';
    html += pnlStr + '</div>';
  }
  el.innerHTML = html;
}

function poll() {
  callApi('/api/status').then(function(s) { if (s) render(s); });
}

function loadIp() {
  callApi('/api/ip').then(function(r) {
    if (r && r.ip) document.getElementById('serverIp').textContent = r.ip;
  });
}

document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('tab-home').addEventListener('click', function() { switchTab('home'); });
  document.getElementById('tab-trades').addEventListener('click', function() { switchTab('trades'); });
  document.getElementById('tab-logs').addEventListener('click', function() { switchTab('logs'); });
  document.getElementById('tab-settings').addEventListener('click', function() { switchTab('settings'); });

  document.getElementById('btnStart').addEventListener('click', function() { callApi('/api/bot/start', {}); });
  document.getElementById('btnStop').addEventListener('click', function() { callApi('/api/bot/stop', {}); });
  document.getElementById('btnScan').addEventListener('click', function() {
    document.getElementById('statusBar').textContent = 'Scanning...';
    callApi('/api/bot/run_now', {});
  });
  document.getElementById('btnClose').addEventListener('click', function() {
    if (!confirm('Close ALL open positions on Delta Exchange?')) return;
    callApi('/api/close_all', {}).then(function(r) { alert('Closed: ' + (r ? r.closed : 0)); });
  });
  document.getElementById('btnLong').addEventListener('click', function() {
    var lots = parseInt(document.getElementById('manLots').value) || 1;
    callApi('/api/manual_trade', {direction: 'long', lots: lots}).then(function(r) {
      if (r && r.success) alert('LONG ' + lots + 'L
Entry: $' + r.entry + '
Stop: $' + r.stop + '
TP: $' + r.tp);
      else alert('Failed: ' + (r ? r.message : 'check logs'));
    });
  });
  document.getElementById('btnShort').addEventListener('click', function() {
    var lots = parseInt(document.getElementById('manLots').value) || 1;
    callApi('/api/manual_trade', {direction: 'short', lots: lots}).then(function(r) {
      if (r && r.success) alert('SHORT ' + lots + 'L
Entry: $' + r.entry + '
Stop: $' + r.stop + '
TP: $' + r.tp);
      else alert('Failed: ' + (r ? r.message : 'check logs'));
    });
  });
  document.getElementById('btnConnect').addEventListener('click', function() {
    var k = document.getElementById('apiKey').value.trim();
    var s = document.getElementById('apiSecret').value.trim();
    if (!k || !s) {
      document.getElementById('connMsg').innerHTML = '<span style="color:var(--r)">Enter key and secret</span>';
      return;
    }
    document.getElementById('connMsg').textContent = 'Connecting...';
    callApi('/api/connect', {api_key: k, api_secret: s}).then(function(r) {
      if (r && r.success) {
        document.getElementById('connMsg').innerHTML =
          '<span style="color:var(--g)">Connected - Balance $' + (r.balance || 0).toFixed(2) + '</span>';
      } else {
        var msg = r ? r.message : 'Failed';
        var ip = (r && r.server_ip) ? '<br><small>Server IP: ' + r.server_ip + '</small>' : '';
        document.getElementById('connMsg').innerHTML =
          '<span style="color:var(--r)">' + msg + '</span>' + ip + '<br><small>Debug: /api/debug/auth</small>';
      }
    });
  });
  document.getElementById('fp-all').addEventListener('click',   function() { FL(''); });
  document.getElementById('fp-TRADE').addEventListener('click', function() { FL('TRADE'); });
  document.getElementById('fp-WARN').addEventListener('click',  function() { FL('WARN'); });
  document.getElementById('fp-ERROR').addEventListener('click', function() { FL('ERROR'); });
});

loadIp();
poll();
setInterval(poll, 4000);
setInterval(loadIp, 60000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return Response(DASHBOARD, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)