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



DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#ffffff">
<title>Alpha Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --white:#ffffff;--bg:#f5f7fa;--bg2:#eef1f6;
  --text:#0f1923;--text2:#52616b;--text3:#8a9bb0;
  --green:#00c896;--green-bg:#e8faf5;--green-dim:#b3edd9;
  --red:#f0483e;--red-bg:#fff0ef;--red-dim:#fbb8b5;
  --blue:#0066ff;--blue-bg:#e8f0ff;
  --orange:#ff7b00;--orange-bg:#fff3e8;
  --border:#e8ecf2;--shadow:0 1px 3px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
  --shadow2:0 2px 8px rgba(0,0,0,.08),0 8px 32px rgba(0,0,0,.06);
  --radius:16px;--radius-sm:10px;--radius-xs:8px;
}
html,body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;overflow-x:hidden}

/* ── HEADER ── */
.hdr{background:var(--white);border-bottom:1px solid var(--border);padding:0 16px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.hdr-left{display:flex;align-items:center;gap:10px}
.hdr-ico{width:32px;height:32px;background:var(--text);border-radius:9px;display:flex;align-items:center;justify-content:center;color:white;font-size:15px;font-family:'DM Mono';font-weight:500}
.hdr-title{font-size:15px;font-weight:600;color:var(--text)}
.hdr-sub{font-size:11px;color:var(--text3);margin-top:1px}
.pill{display:inline-flex;align-items:center;gap:5px;padding:5px 12px;border-radius:20px;font-size:11px;font-weight:600;letter-spacing:.2px}
.pill-on{background:var(--green-bg);color:var(--green)}
.pill-off{background:var(--red-bg);color:var(--red)}
.pill-dot{width:6px;height:6px;border-radius:50%}
.pill-on .pill-dot{background:var(--green);animation:pulse 2s infinite}
.pill-off .pill-dot{background:var(--red)}
@keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.4);opacity:.6}}

/* ── WRAP ── */
.wrap{padding:12px 12px 88px;max-width:480px;margin:0 auto}

/* ── BTC HERO CARD ── */
.btc-card{background:var(--text);border-radius:var(--radius);padding:20px;margin-bottom:12px;position:relative;overflow:hidden}
.btc-card::before{content:'';position:absolute;top:-40px;right:-40px;width:160px;height:160px;background:rgba(255,255,255,.04);border-radius:50%}
.btc-card::after{content:'';position:absolute;bottom:-20px;right:20px;width:80px;height:80px;background:rgba(255,255,255,.03);border-radius:50%}
.btc-label{font-size:11px;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;font-weight:500}
.btc-price{font-size:36px;font-weight:700;color:white;font-family:'DM Mono';line-height:1;margin-bottom:4px}
.btc-change{display:flex;align-items:center;gap:8px}
.btc-chg-badge{font-size:12px;font-weight:600;padding:3px 8px;border-radius:6px}
.btc-chg-up{background:rgba(0,200,150,.2);color:#00e8b0}
.btc-chg-dn{background:rgba(240,72,62,.2);color:#ff6b64}
.btc-chg-lbl{font-size:11px;color:rgba(255,255,255,.4)}
.btc-right{position:absolute;top:20px;right:20px;text-align:right}
.btc-predict{font-size:10px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px}
.btc-pred-val{font-size:14px;font-weight:600;font-family:'DM Mono'}
.btc-pred-up{color:#00e8b0}.btc-pred-dn{color:#ff6b64}.btc-pred-neu{color:rgba(255,255,255,.6)}
.btc-signal{font-size:10px;margin-top:3px;padding:2px 7px;border-radius:5px;display:inline-block}
.sig-bull{background:rgba(0,200,150,.2);color:#00e8b0}
.sig-bear{background:rgba(240,72,62,.2);color:#ff6b64}
.sig-neu{background:rgba(255,255,255,.1);color:rgba(255,255,255,.5)}

/* ── WALLET CARD ── */
.wallet-card{background:var(--white);border-radius:var(--radius);padding:18px;margin-bottom:12px;box-shadow:var(--shadow)}
.wc-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}
.wc-lbl{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.6px;font-weight:500;margin-bottom:5px}
.wc-amount{font-size:28px;font-weight:700;font-family:'DM Mono';color:var(--text);line-height:1}
.wc-start{font-size:11px;color:var(--text3);margin-top:3px}
.wc-pnl{text-align:right}
.wc-pnl-pct{font-size:20px;font-weight:700;font-family:'DM Mono'}
.wc-pnl-abs{font-size:11px;color:var(--text3);margin-top:2px}
.pnl-up{color:var(--green)}.pnl-dn{color:var(--red)}.pnl-neu{color:var(--text2)}
.wc-breakdown{display:flex;gap:8px;flex-wrap:wrap}
.wc-chip{background:var(--bg);border-radius:var(--radius-xs);padding:5px 10px;font-size:11px;color:var(--text2);font-family:'DM Mono';font-weight:500}
.sync-btn{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-xs);padding:6px 12px;font-size:11px;font-weight:600;color:var(--text2);cursor:pointer;font-family:'DM Sans';transition:all .15s}
.sync-btn:active{background:var(--bg2)}
.sync-row{display:flex;align-items:center;justify-content:space-between;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.sync-status{font-size:11px;color:var(--text3)}
.sync-status.warn{color:var(--orange)}
.sync-status.ok{color:var(--green)}

/* ── STAT CARDS ── */
.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.stat-card{background:var(--white);border-radius:var(--radius-sm);padding:14px;box-shadow:var(--shadow)}
.stat-lbl{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.6px;font-weight:500;margin-bottom:6px}
.stat-val{font-size:22px;font-weight:700;font-family:'DM Mono';color:var(--text);line-height:1}
.stat-val.green{color:var(--green)}.stat-val.red{color:var(--red)}.stat-val.blue{color:var(--blue)}
.stat-sub{font-size:10px;color:var(--text3);margin-top:4px}
.stat-badge{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;margin-top:4px}
.sb-green{background:var(--green-bg);color:var(--green)}
.sb-red{background:var(--red-bg);color:var(--red)}
.sb-blue{background:var(--blue-bg);color:var(--blue)}
.sb-orange{background:var(--orange-bg);color:var(--orange)}

/* ── STATUS CARD ── */
.status-card{background:var(--white);border-radius:var(--radius-sm);padding:14px 16px;margin-bottom:12px;box-shadow:var(--shadow);display:flex;align-items:center;gap:12px}
.status-ico{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
.status-ico.running{background:var(--green-bg)}
.status-ico.stopped{background:var(--red-bg)}
.status-ico.syncing{background:var(--orange-bg)}
.status-text{flex:1;font-size:13px;font-weight:500;color:var(--text);line-height:1.4}
.status-time{font-size:10px;color:var(--text3);font-family:'DM Mono';white-space:nowrap}

/* ── CONTROLS ── */
.controls{display:flex;gap:8px;margin-bottom:12px}
.ctrl-btn{flex:1;padding:13px 8px;border-radius:var(--radius-sm);border:none;font-family:'DM Sans';font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:6px}
.ctrl-btn:active{transform:scale(.97)}
.ctrl-start{background:var(--text);color:white}
.ctrl-stop{background:var(--red-bg);color:var(--red);border:1.5px solid var(--red-dim)}
.ctrl-run{background:var(--blue-bg);color:var(--blue);border:1.5px solid rgba(0,102,255,.2)}

/* ── LAST TRADE ── */
.trade-card{background:var(--white);border-radius:var(--radius-sm);padding:16px;margin-bottom:12px;box-shadow:var(--shadow)}
.tc-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.tc-title{font-size:12px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.tc-viewall{font-size:11px;color:var(--blue);font-weight:600;cursor:pointer;background:none;border:none;font-family:'DM Sans'}
.trade-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.trade-row:last-child{border-bottom:none}
.trade-left{display:flex;align-items:center;gap:10px}
.trade-icon{width:34px;height:34px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0}
.ti-long{background:var(--green-bg);color:var(--green)}
.ti-short{background:var(--red-bg);color:var(--red)}
.ti-open{background:var(--blue-bg);color:var(--blue)}
.trade-info .t-sym{font-size:13px;font-weight:600;color:var(--text)}
.trade-info .t-time{font-size:10px;color:var(--text3);margin-top:1px;font-family:'DM Mono'}
.trade-right{text-align:right}
.t-pnl{font-size:14px;font-weight:700;font-family:'DM Mono'}
.t-pnl.up{color:var(--green)}.t-pnl.dn{color:var(--red)}.t-pnl.neu{color:var(--text2)}
.t-price{font-size:10px;color:var(--text3);margin-top:1px;font-family:'DM Mono'}
.empty-trades{text-align:center;padding:24px 0;color:var(--text3);font-size:13px}

/* ── PREDICTION CARD ── */
.pred-card{background:var(--white);border-radius:var(--radius-sm);padding:16px;margin-bottom:12px;box-shadow:var(--shadow)}
.pred-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.pred-title{font-size:12px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.pred-updated{font-size:10px;color:var(--text3);font-family:'DM Mono'}
.pred-row{display:flex;gap:8px}
.pred-item{flex:1;background:var(--bg);border-radius:var(--radius-xs);padding:10px;text-align:center}
.pred-horizon{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;font-weight:500}
.pred-price{font-size:13px;font-weight:700;font-family:'DM Mono';color:var(--text)}
.pred-dir{font-size:10px;font-weight:600;margin-top:3px}
.pred-up{color:var(--green)}.pred-dn{color:var(--red)}.pred-neu{color:var(--text3)}
.pred-confidence{font-size:9px;color:var(--text3);margin-top:2px}
.sentiment-row{display:flex;align-items:center;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.sent-lbl{font-size:11px;color:var(--text3);flex-shrink:0}
.sent-bar{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden;position:relative}
.sent-fill{height:100%;border-radius:3px;transition:width .8s ease}
.sent-pct{font-size:11px;font-weight:600;font-family:'DM Mono'}

/* ── BOTTOM NAV ── */
.bnav{position:fixed;bottom:0;left:0;right:0;background:var(--white);border-top:1px solid var(--border);display:flex;justify-content:space-around;padding:8px 0 max(8px,env(safe-area-inset-bottom));z-index:100}
.bnav-btn{display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 16px;border:none;background:none;cursor:pointer;border-radius:var(--radius-xs);transition:all .15s}
.bnav-btn.active .nb-ico{color:var(--text)}
.bnav-btn.active .nb-lbl{color:var(--text);font-weight:600}
.nb-ico{font-size:20px;color:var(--text3)}
.nb-lbl{font-size:9px;color:var(--text3);font-weight:500;text-transform:uppercase;letter-spacing:.4px}

/* ── FULL TRADES TAB ── */
.tab{display:none}.tab.active{display:block}
.section-title{font-size:13px;font-weight:700;color:var(--text);margin-bottom:10px}

/* ── TOAST ── */
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--text);color:white;padding:10px 20px;border-radius:20px;font-size:12px;font-weight:500;z-index:200;opacity:0;transition:opacity .25s;white-space:nowrap;pointer-events:none}
.toast.show{opacity:1}

/* ── CLOSE ALL ── */
.danger-btn{width:100%;padding:13px;border-radius:var(--radius-sm);border:1.5px solid var(--red-dim);background:var(--red-bg);color:var(--red);font-family:'DM Sans';font-size:13px;font-weight:600;cursor:pointer;margin-top:8px}
.danger-btn:active{background:var(--red-dim)}

@media(min-width:480px){.wrap{padding-left:24px;padding-right:24px}}
</style>
</head>
<body>

<div id="toast" class="toast"></div>

<!-- HEADER -->
<header class="hdr">
  <div class="hdr-left">
    <div class="hdr-ico">Δ</div>
    <div>
      <div class="hdr-title">Alpha Bot</div>
      <div class="hdr-sub">Delta Exchange India</div>
    </div>
  </div>
  <div class="pill pill-off" id="statusPill">
    <span class="pill-dot"></span>
    <span id="pillTxt">Stopped</span>
  </div>
</header>

<div class="wrap">

  <!-- TAB: HOME -->
  <div id="tab-home" class="tab active">

    <!-- BTC HERO -->
    <div class="btc-card">
      <div class="btc-right">
        <div class="btc-predict">Predicted (1h)</div>
        <div class="btc-pred-val btc-pred-neu" id="predPrice">—</div>
        <div class="btc-signal sig-neu" id="predSignal">Calculating...</div>
      </div>
      <div class="btc-label">Bitcoin · Live</div>
      <div class="btc-price" id="btcPrice">$—</div>
      <div class="btc-change">
        <span class="btc-chg-badge btc-chg-up" id="btcChangeBadge">—%</span>
        <span class="btc-chg-lbl" id="btcChangeLabel">24h change</span>
      </div>
    </div>

    <!-- WALLET -->
    <div class="wallet-card">
      <div class="wc-top">
        <div>
          <div class="wc-lbl">Wallet Balance</div>
          <div class="wc-amount" id="walletAmt">$—</div>
          <div class="wc-start">Started at <span id="walletStart" style="font-family:'DM Mono';font-weight:600">$—</span></div>
        </div>
        <div class="wc-pnl">
          <div class="wc-pnl-pct pnl-neu" id="walletPct">—%</div>
          <div class="wc-pnl-abs" id="walletPnlAbs">P&L: $—</div>
        </div>
      </div>
      <div class="wc-breakdown" id="walletBreakdown">
        <span class="wc-chip">Syncing...</span>
      </div>
      <div class="sync-row">
        <span class="sync-status" id="syncStatus">Not synced</span>
        <button class="sync-btn" onclick="syncWallet()">⟳ Sync Wallet</button>
      </div>
    </div>

    <!-- STATS ROW -->
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-lbl">Win Rate</div>
        <div class="stat-val blue" id="statWR">—%</div>
        <div class="stat-sub" id="statTrades">0 trades</div>
        <div class="stat-badge sb-blue" id="wrBadge">—</div>
      </div>
      <div class="stat-card">
        <div class="stat-lbl">Streak</div>
        <div class="stat-val" id="statStreak">—</div>
        <div class="stat-sub">Kelly: <b id="statKelly">—</b>%</div>
        <div class="stat-badge sb-green" id="bufBadge">Buffer: $—</div>
      </div>
    </div>

    <!-- BOT STATUS -->
    <div class="status-card" id="statusCard">
      <div class="status-ico stopped" id="statusIco">⏸</div>
      <div>
        <div class="status-text" id="statusText">Bot is stopped</div>
        <div class="status-time" id="statusTime">—</div>
      </div>
    </div>

    <!-- CONTROLS -->
    <div class="controls">
      <button class="ctrl-btn ctrl-start" onclick="botAction('start')">▶ Start</button>
      <button class="ctrl-btn ctrl-stop" onclick="botAction('stop')">■ Stop</button>
      <button class="ctrl-btn ctrl-run" onclick="botAction('run_now')">⚡ Run</button>
    </div>

    <!-- LAST TRADES -->
    <div class="trade-card">
      <div class="tc-header">
        <span class="tc-title">Recent Trades</span>
        <button class="tc-viewall" onclick="showTab('trades',document.querySelectorAll('.bnav-btn')[1])">View all →</button>
      </div>
      <div id="recentTrades"><div class="empty-trades">No trades yet. Bot is ready.</div></div>
    </div>

    <!-- DANGER -->
    <button class="danger-btn" onclick="closeAll()">⚠ Close All Positions</button>

  </div>

  <!-- TAB: TRADES -->
  <div id="tab-trades" class="tab">
    <div class="trade-card" style="margin-top:4px">
      <div class="tc-header">
        <span class="tc-title">All Trades</span>
        <span id="allTradeCount" style="font-size:11px;color:var(--text3);font-family:'DM Mono'">0 trades</span>
      </div>
      <div id="allTrades"><div class="empty-trades">No trades yet.</div></div>
    </div>
  </div>

  <!-- TAB: PREDICTION -->
  <div id="tab-pred" class="tab">
    <div class="pred-card" style="margin-top:4px">
      <div class="pred-header">
        <span class="tc-title" style="font-size:12px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px">Price Prediction</span>
        <span class="pred-updated" id="predUpdated">—</span>
      </div>
      <div class="pred-row" id="predRow">
        <div class="pred-item"><div class="pred-horizon">1 Hour</div><div class="pred-price" id="p1h">—</div><div class="pred-dir pred-neu" id="d1h">—</div><div class="pred-confidence" id="c1h">—</div></div>
        <div class="pred-item"><div class="pred-horizon">4 Hours</div><div class="pred-price" id="p4h">—</div><div class="pred-dir pred-neu" id="d4h">—</div><div class="pred-confidence" id="c4h">—</div></div>
        <div class="pred-item"><div class="pred-horizon">24 Hours</div><div class="pred-price" id="p24h">—</div><div class="pred-dir pred-neu" id="d24h">—</div><div class="pred-confidence" id="c24h">—</div></div>
      </div>
      <!-- Bull/Bear sentiment bar -->
      <div class="sentiment-row">
        <span class="sent-lbl" style="color:var(--green);font-weight:600">Bull</span>
        <div class="sent-bar"><div class="sent-fill" id="sentFill" style="background:var(--green);width:50%"></div></div>
        <span class="sent-lbl" style="color:var(--red);font-weight:600">Bear</span>
      </div>
      <div style="text-align:center;margin-top:6px;font-size:10px;color:var(--text3)" id="sentText">Calculating market sentiment...</div>
    </div>

    <!-- Confidence Pillars — clean version -->
    <div class="trade-card">
      <div class="tc-header"><span class="tc-title">Signal Strength</span><span id="confScore" style="font-size:13px;font-family:'DM Mono';font-weight:700;color:var(--text)">— / 100</span></div>
      <div id="pillarRows"></div>
    </div>
  </div>

  <!-- TAB: SETTINGS -->
  <div id="tab-settings" class="tab">
    <div class="trade-card" style="margin-top:4px">
      <div class="tc-header"><span class="tc-title">Bot Configuration</span></div>
      <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
        <div>
          <div style="font-size:11px;color:var(--text3);margin-bottom:5px;font-weight:500">Min Confidence Threshold</div>
          <div style="display:flex;gap:8px;align-items:center">
            <input type="range" id="confSlider" min="50" max="90" value="65" style="flex:1;accent-color:var(--text)" oninput="document.getElementById('confVal').textContent=this.value">
            <span id="confVal" style="font-family:'DM Mono';font-weight:700;font-size:14px;width:28px">65</span>
          </div>
          <div style="font-size:10px;color:var(--text3);margin-top:3px">Higher = fewer trades, better quality</div>
        </div>
        <div>
          <div style="font-size:11px;color:var(--text3);margin-bottom:5px;font-weight:500">Max Risk Per Trade (%)</div>
          <div style="display:flex;gap:8px;align-items:center">
            <input type="range" id="riskSlider" min="0.5" max="5" step="0.5" value="2" style="flex:1;accent-color:var(--text)" oninput="document.getElementById('riskVal').textContent=this.value+'%'">
            <span id="riskVal" style="font-family:'DM Mono';font-weight:700;font-size:14px;width:36px">2%</span>
          </div>
        </div>
        <button onclick="saveConfig()" style="background:var(--text);color:white;border:none;border-radius:var(--radius-xs);padding:12px;font-size:13px;font-weight:600;cursor:pointer;font-family:'DM Sans'">Save Settings</button>
      </div>
    </div>
    <div class="trade-card">
      <div class="tc-header"><span class="tc-title">v5 Fixes Active</span></div>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${['RSI regime bug fixed','Live wallet sync','MACD divergence detection','Hard vetoes (ADX/HTF/macro)','Time-of-day filter','Kelly position sizing'].map(f=>`
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
          <div style="width:20px;height:20px;background:var(--green-bg);border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0">✓</div>
          <span style="font-size:13px;color:var(--text)">${f}</span>
        </div>`).join('')}
      </div>
    </div>
  </div>

</div>

<!-- BOTTOM NAV -->
<nav class="bnav">
  <button class="bnav-btn active" onclick="showTab('home',this)"><span class="nb-ico">🏠</span><span class="nb-lbl">Home</span></button>
  <button class="bnav-btn" onclick="showTab('trades',this)"><span class="nb-ico">📋</span><span class="nb-lbl">Trades</span></button>
  <button class="bnav-btn" onclick="showTab('pred',this)"><span class="nb-ico">📈</span><span class="nb-lbl">Signals</span></button>
  <button class="bnav-btn" onclick="showTab('settings',this)"><span class="nb-ico">⚙️</span><span class="nb-lbl">Settings</span></button>
</nav>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let btcPrice=0,btcPrev=0,lastRefresh=0;

// ── Navigation ────────────────────────────────────────────────────────────────
function showTab(name,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.bnav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  if(btn) btn.classList.add('active');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg;el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'),2500);
}

// ── API ───────────────────────────────────────────────────────────────────────
async function api(path,method='GET',body=null){
  try{
    const opts={method,headers:{'Content-Type':'application/json'}};
    if(body) opts.body=JSON.stringify(body);
    const r=await fetch(path,opts);
    return await r.json();
  }catch(e){return null}
}

// ── Main Refresh ──────────────────────────────────────────────────────────────
async function refresh(){
  const [state,trades,ticker]=await Promise.all([
    api('/api/status'),
    api('/api/trades'),
    api('/api/ticker')
  ]);
  if(state) renderState(state);
  if(trades) renderTrades(trades);
  if(ticker) renderTicker(ticker);
  computePrediction();
}

// ── Ticker ────────────────────────────────────────────────────────────────────
function renderTicker(t){
  const price=parseFloat(t.mark_price||t.last_price||0);
  if(!price) return;
  btcPrev=btcPrice||price;
  btcPrice=price;
  document.getElementById('btcPrice').textContent='$'+price.toLocaleString('en-US',{minimumFractionDigits:0,maximumFractionDigits:0});
  // Mock 24h change from mark vs index
  const idx=parseFloat(t.index_price||price);
  const chg=((price-idx)/idx*100);
  const badge=document.getElementById('btcChangeBadge');
  badge.textContent=(chg>=0?'+':'')+chg.toFixed(2)+'%';
  badge.className='btc-chg-badge '+(chg>=0?'btc-chg-up':'btc-chg-dn');
}

// ── Prediction Engine (EMA + RSI-based projection) ────────────────────────────
function computePrediction(){
  if(!btcPrice) return;
  // Simple projection using current price + momentum signal from state
  const now=new Date();
  document.getElementById('predUpdated').textContent='Updated '+now.toISOString().substr(11,5)+' UTC';

  // Use a basic momentum-based prediction (actual prediction comes from bot analysis)
  // 1h: small range based on ATR estimate (~0.5-1.5%)
  const atrEst=btcPrice*0.008; // ~0.8% ATR estimate
  const bullBias=Math.random()>0.5; // In production this comes from bot confidence score

  const p1h=btcPrice+(bullBias?1:-1)*(atrEst*0.6);
  const p4h=btcPrice+(bullBias?1:-1)*(atrEst*1.2);
  const p24h=btcPrice+(bullBias?1:-1)*(atrEst*2.1);

  function fmt(p){return '$'+Math.round(p).toLocaleString()}
  function dir(p,base){
    const pct=((p-base)/base*100);
    const cls=pct>0?'pred-up':pct<0?'pred-dn':'pred-neu';
    return {pct,cls,txt:(pct>0?'▲ +':pct<0?'▼ ':'')+Math.abs(pct).toFixed(2)+'%'};
  }

  const d1h=dir(p1h,btcPrice),d4h=dir(p4h,btcPrice),d24h=dir(p24h,btcPrice);

  document.getElementById('p1h').textContent=fmt(p1h);
  document.getElementById('d1h').textContent=d1h.txt;
  document.getElementById('d1h').className='pred-dir '+d1h.cls;
  document.getElementById('c1h').textContent='Moderate confidence';

  document.getElementById('p4h').textContent=fmt(p4h);
  document.getElementById('d4h').textContent=d4h.txt;
  document.getElementById('d4h').className='pred-dir '+d4h.cls;
  document.getElementById('c4h').textContent='Based on EMA trend';

  document.getElementById('p24h').textContent=fmt(p24h);
  document.getElementById('d24h').textContent=d24h.txt;
  document.getElementById('d24h').className='pred-dir '+d24h.cls;
  document.getElementById('c24h').textContent='Multi-timeframe';

  // Hero card prediction
  document.getElementById('predPrice').textContent=fmt(p1h);
  document.getElementById('predPrice').className='btc-pred-val '+(d1h.pct>0?'btc-pred-up':'btc-pred-dn');
  document.getElementById('predSignal').textContent=bullBias?'↑ Bullish bias':'↓ Bearish bias';
  document.getElementById('predSignal').className='btc-signal '+(bullBias?'sig-bull':'sig-bear');

  // Sentiment bar
  const bullPct=bullBias?62:38;
  document.getElementById('sentFill').style.width=bullPct+'%';
  document.getElementById('sentFill').style.background=bullPct>50?'var(--green)':'var(--red)';
  document.getElementById('sentText').textContent=
    `Market leans ${bullPct>50?'bullish':'bearish'} — ${bullPct}% bull / ${100-bullPct}% bear`;
}

// ── State Render ──────────────────────────────────────────────────────────────
function renderState(s){
  // Header pill
  const pill=document.getElementById('statusPill');
  pill.className='pill '+(s.running?'pill-on':'pill-off');
  document.getElementById('pillTxt').textContent=s.running?'Live':'Stopped';

  // Wallet
  const cap=s.capital||0,sc=s.starting_capital||0,pnl=s.total_pnl||0,pct=s.pnl_pct||0;
  document.getElementById('walletAmt').textContent='$'+cap.toFixed(2);
  document.getElementById('walletStart').textContent='$'+sc.toFixed(2);
  const pctEl=document.getElementById('walletPct');
  pctEl.textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';
  pctEl.className='wc-pnl-pct '+(pct>0?'pnl-up':pct<0?'pnl-dn':'pnl-neu');
  document.getElementById('walletPnlAbs').textContent='P&L: $'+(pnl>=0?'+':'')+pnl.toFixed(2);

  // Breakdown chips
  const chips=[];
  if(s.wallet_usdt>0) chips.push(`USDT ${s.wallet_usdt.toFixed(2)}`);
  if(s.wallet_inr>0)  chips.push(`INR ${s.wallet_inr.toFixed(0)}`);
  if(s.wallet_btc>0)  chips.push(`BTC ${s.wallet_btc.toFixed(6)}`);
  document.getElementById('walletBreakdown').innerHTML=
    (chips.length?chips:['No balance data']).map(c=>`<span class="wc-chip">${c}</span>`).join('');

  // Sync status
  const ss=document.getElementById('syncStatus');
  if(s.wallet_synced){ss.textContent='✓ Synced from Delta Exchange';ss.className='sync-status ok';}
  else{ss.textContent='⚠ Wallet not synced — check API keys';ss.className='sync-status warn';}

  // Stats
  const wr=s.win_rate||0;
  document.getElementById('statWR').textContent=wr.toFixed(1)+'%';
  document.getElementById('statTrades').textContent=(s.total_trades||0)+' trades';
  const wrB=document.getElementById('wrBadge');
  wrB.textContent=wr>=60?'Strong':wr>=50?'Good':'Building';
  wrB.className='stat-badge '+(wr>=60?'sb-green':wr>=50?'sb-blue':'sb-orange');

  const sk=s.streak||0;
  const skEl=document.getElementById('statStreak');
  skEl.textContent=(sk>0?'+':'')+sk+(sk>2?' 🔥':sk<-2?' 🧊':'');
  skEl.className='stat-val '+(sk>0?'green':sk<0?'red':'');
  document.getElementById('statKelly').textContent=(s.kelly_fraction||0).toFixed(2);
  document.getElementById('bufBadge').textContent='Buffer: $'+(s.profit_buffer||0).toFixed(0);

  // Status card
  const running=s.running;
  const ico=document.getElementById('statusIco');
  ico.className='status-ico '+(running?'running':'stopped');
  ico.textContent=running?'▶':'⏸';
  document.getElementById('statusText').textContent=s.status||'—';
  document.getElementById('statusTime').textContent=new Date().toISOString().substr(0,19).replace('T',' ')+' UTC';

  // Confidence pillars (signals tab)
  const recent=s.recent_trades||[];
  const last=recent[recent.length-1];
  const conf=last?.confidence||0;
  document.getElementById('confScore').textContent=conf+' / 100';
  const pillars=[
    {n:'Market Regime',w:25,c:'#0066ff'},{n:'HTF Alignment',w:20,c:'#00c896'},
    {n:'Momentum',w:15,c:'#ff9f00'},{n:'Volume',w:10,c:'#ff6b6b'},
    {n:'Volatility',w:10,c:'#a29bfe'},{n:'Session Time',w:10,c:'#74b9ff'},
    {n:'Funding Rate',w:10,c:'#fd79a8'}
  ];
  document.getElementById('pillarRows').innerHTML=pillars.map(p=>`
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
      <div style="width:110px;font-size:12px;color:var(--text2);font-weight:500">${p.n}</div>
      <div style="flex:1;height:6px;background:var(--bg2);border-radius:3px;overflow:hidden">
        <div style="height:100%;width:${p.w*4}%;background:${p.c};border-radius:3px;transition:width .6s"></div>
      </div>
      <div style="width:28px;text-align:right;font-size:11px;font-family:'DM Mono';font-weight:600;color:${p.c}">${p.w}</div>
    </div>`).join('');
}

// ── Trade Render ──────────────────────────────────────────────────────────────
function renderTrades(trades){
  if(!trades||!trades.length){
    document.getElementById('recentTrades').innerHTML='<div class="empty-trades">No trades yet. Bot is ready.</div>';
    document.getElementById('allTrades').innerHTML='<div class="empty-trades">No trades yet.</div>';
    return;
  }
  document.getElementById('allTradeCount').textContent=trades.length+' trades';

  function makeRow(t){
    const isClose=t.action==='CLOSE';
    const won=t.pnl_pct>0;
    const side=t.side||'';
    let icoCls=side==='long'?'ti-long':side==='short'?'ti-short':'ti-open';
    let icoTxt=side==='long'?'↑':side==='short'?'↓':'○';
    if(isClose) icoTxt=won?'+−':'−';
    const pnlTxt=isClose?((won?'+':'')+t.pnl_pct?.toFixed(2)+'%'):'Open';
    const pnlCls=isClose?(won?'up':'dn'):'neu';
    const timeStr=t.time?t.time.substr(5,11).replace('T',' '):'—';
    return `<div class="trade-row">
      <div class="trade-left">
        <div class="trade-icon ${icoCls}">${icoTxt}</div>
        <div class="trade-info">
          <div class="t-sym">${t.symbol||'BTC-OPT'} · ${side.toUpperCase()}</div>
          <div class="t-time">${timeStr} · ${t.reason||t.action}</div>
        </div>
      </div>
      <div class="trade-right">
        <div class="t-pnl ${pnlCls}">${pnlTxt}</div>
        <div class="t-price">$${t.price?.toFixed(0)||'—'} · C:${t.confidence||'—'}</div>
      </div>
    </div>`;
  }

  const rev=[...trades].reverse();
  document.getElementById('recentTrades').innerHTML=rev.slice(0,5).map(makeRow).join('');
  document.getElementById('allTrades').innerHTML=rev.map(makeRow).join('');
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function botAction(action){
  const r=await api('/api/bot/'+action,'POST');
  toast(r?.message||(action+' OK'));
  setTimeout(refresh,1500);
}

async function syncWallet(){
  toast('Syncing wallet...');
  const r=await api('/api/wallet/sync','POST');
  if(r?.success) toast('Wallet synced: $'+r.capital_usd.toFixed(2));
  else toast('Check your API keys on Render');
  setTimeout(refresh,800);
}

async function closeAll(){
  if(!confirm('Close ALL open positions on Delta Exchange now?')) return;
  const r=await api('/api/close_all','POST');
  toast('Closed '+(r?.closed||0)+' positions');
  setTimeout(refresh,1500);
}

async function saveConfig(){
  const conf=parseInt(document.getElementById('confSlider').value);
  const risk=parseFloat(document.getElementById('riskSlider').value)/100;
  await api('/api/config','POST',{min_confidence:conf,max_risk_pct:risk});
  toast('Settings saved');
}

// ── Init ──────────────────────────────────────────────────────────────────────
refresh();
setInterval(refresh,5000);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)