"""
DELTA ALPHA BOT - Final consolidated build
Delta Exchange India | BTCUSD Perpetual | Product ID: 27
"""
import os, time, hmac, hashlib, json, math, logging, threading, requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ════════════════════════════════════════════════════════════════════════
#  CONFIG  (all tuning in one place)
# ════════════════════════════════════════════════════════════════════════
class C:
    BASE       = "https://api.india.delta.exchange"
    KEY        = os.getenv("DELTA_API_KEY", "").strip()
    SECRET     = os.getenv("DELTA_API_SECRET", "").strip()
    PID        = 27           # BTCUSD perpetual (confirmed)
    LOT_BTC    = 0.001        # 1 lot = 0.001 BTC
    LEVERAGE   = 5
    SCAN       = 300          # 5 min scan interval
    MIN_CONF   = 58
    STOP_PCT   = 0.025        # 2.5% hard stop
    TP_PCT     = 0.030        # 3.0% take profit
    RISK_PCT   = 0.015        # 1.5% capital risk per trade
    HALT_PCT   = 0.08         # Halt if down 8% from start
    PAUSE_PCT  = 0.03         # Pause if down 3% today
    STATE_FILE = "/tmp/ab_state.json"

# ════════════════════════════════════════════════════════════════════════
#  DELTA API
# ════════════════════════════════════════════════════════════════════════
class API:
    def __init__(self):
        self.key = C.KEY
        self.secret = C.SECRET
        self.s = requests.Session()

    def creds(self, key, secret):
        self.key = key.strip()
        self.secret = secret.strip()

    def _sign(self, method, path, qs="", body=""):
        ts = str(int(time.time()))
        sig = hmac.new(self.secret.encode(),
            (method + ts + path + qs + body).encode(),
            hashlib.sha256).hexdigest()
        return {"api-key": self.key, "timestamp": ts,
                "signature": sig, "Content-Type": "application/json"}

    def get(self, path, p=None):
        qs = ("?" + "&".join(f"{k}={v}" for k,v in p.items())) if p else ""
        try:
            r = self.s.get(f"{C.BASE}{path}{qs}",
                headers=self._sign("GET", path, qs), timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"GET {path}: {e}")
            return None

    def post(self, path, body):
        b = json.dumps(body)
        try:
            r = self.s.post(f"{C.BASE}{path}",
                headers=self._sign("POST", path, "", b),
                data=b, timeout=10)
            return r.json()
        except Exception as e:
            log.warning(f"POST {path}: {e}")
            return {}

    def btc_price(self):
        try:
            r = self.s.get(f"{C.BASE}/v2/tickers/BTCUSD", timeout=6)
            return float(r.json().get("result", {}).get("mark_price", 0) or 0)
        except:
            return 0.0

    def balance(self):
        d = self.get("/v2/wallet/balances")
        if d and d.get("success"):
            for b in d.get("result", []):
                sym = str(b.get("asset_symbol", "")).upper()
                if sym in ("USD", "USDT"):
                    v = float(b.get("available_balance", 0) or 0)
                    if v > 0: return round(v, 2)
            ne = float((d.get("meta") or {}).get("net_equity", 0) or 0)
            if ne > 0: return round(ne, 2)
        return 0.0

    def candles(self, res="5m", n=100):
        mins = {"5m": 5, "15m": 15}.get(res, 5)
        end = int(time.time())
        d = self.get("/v2/history/candles", {
            "symbol": "BTCUSD", "resolution": res,
            "start": end - mins * 60 * n, "end": end})
        return d.get("result", []) if d and d.get("success") else []

    def positions(self):
        d = self.get("/v2/positions/margined")
        if d and d.get("success"):
            return [p for p in d.get("result", [])
                    if abs(float(p.get("size", 0) or 0)) > 0]
        return []

    def order(self, side, lots):
        r = self.post("/v2/orders", {
            "product_id": C.PID, "size": lots, "side": side,
            "order_type": "market_order", "time_in_force": "ioc"})
        return r

    def bracket(self, side, lots, stop, tp):
        """Place stop+TP order on Delta — survives bot restarts."""
        return self.post("/v2/orders", {
            "product_id": C.PID, "size": lots, "side": side,
            "order_type": "stop_market_order",
            "stop_price": str(round(stop, 1)),
            "bracket_stop_loss_price": str(round(stop, 1)),
            "bracket_take_profit_price": str(round(tp, 1)),
            "time_in_force": "gtc",
            "stop_trigger_method": "mark_price"})

    def close_all(self):
        n = 0
        for p in self.positions():
            sz = float(p.get("size", 0) or 0)
            q = abs(int(sz))
            if q:
                self.post("/v2/orders", {
                    "product_id": p.get("product_id", C.PID),
                    "size": q, "side": "sell" if sz > 0 else "buy",
                    "order_type": "market_order", "time_in_force": "ioc"})
                n += 1
        return n

# ════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ════════════════════════════════════════════════════════════════════════
def candles_to_arrays(raw):
    cl, hi, lo, vo = [], [], [], []
    for c in raw:
        try:
            cv = float(c.get("close", 0) or 0)
            if cv > 0:
                cl.append(cv)
                hi.append(float(c.get("high", cv) or cv))
                lo.append(float(c.get("low", cv) or cv))
                vo.append(float(c.get("volume", 0) or 0))
        except: pass
    return cl, hi, lo, vo

def ema(p, n):
    if len(p) < n: return [p[-1]] * len(p) if p else []
    k = 2 / (n + 1)
    v = [sum(p[:n]) / n]
    for x in p[n:]: v.append(x * k + v[-1] * (1 - k))
    return [v[0]] * (n - 1) + v

def rsi(p, n=14):
    if len(p) < n + 2: return 50.0
    d = [p[i] - p[i-1] for i in range(1, len(p))]
    g = sum(max(x, 0) for x in d[-n:]) / n
    l = sum(abs(min(x, 0)) for x in d[-n:]) / n
    return round(100.0 if l < 1e-10 else 100 - 100 / (1 + g / l), 1)

def adx(hi, lo, cl, n=14):
    if len(cl) < n * 2 + 1: return 0.0, 0.0, 0.0
    tr, pm, nm = [], [], []
    for i in range(1, len(cl)):
        tr.append(max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1])))
        u = hi[i]-hi[i-1]; d2 = lo[i-1]-lo[i]
        pm.append(u if u > d2 and u > 0 else 0.0)
        nm.append(d2 if d2 > u and d2 > 0 else 0.0)
    def ws(a):
        s = sum(a[:n]); r = [s]
        for v in a[n:]: s = s - s/n + v; r.append(s)
        return r
    at = ws(tr); pd = ws(pm); nd = ws(nm)
    pi = [100*pd[i]/at[i] if at[i] > 0 else 0 for i in range(len(at))]
    ni = [100*nd[i]/at[i] if at[i] > 0 else 0 for i in range(len(at))]
    dx = [abs(pi[i]-ni[i])/(pi[i]+ni[i])*100 if pi[i]+ni[i] > 0 else 0
          for i in range(len(pi))]
    return round(sum(dx[-n:])/n, 1), round(pi[-1], 1), round(ni[-1], 1)

def atr(hi, lo, cl, n=14):
    if len(cl) < n + 1: return 0.0
    t = [max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
         for i in range(1, len(cl))]
    return sum(t[-n:]) / n

def score(cl, hi, lo, vo, cl15, hour, dirn):
    """Score a direction 0-100. Returns (score, veto_str)."""
    if len(cl) < 55: return 0, "need_55_candles"
    if hour in [2, 3, 4, 5]: return 0, "dead_zone"
    if len(vo) >= 21:
        avg = sum(vo[-21:-1]) / 20
        if vo[-2] < avg * 0.10: return 0, "low_volume"

    adx_v, pdi, ndi = adx(hi, lo, cl)
    rsi_v = rsi(cl)
    e8 = ema(cl, 8)[-1]; e21 = ema(cl, 21)[-1]; e55 = ema(cl, 55)[-1]
    price = cl[-1]
    bull = price > e8 > e21 > e55 and adx_v > 20 and pdi > ndi
    bear = price < e8 < e21 < e55 and adx_v > 20 and ndi > pdi

    s = 0
    # Regime (40pts)
    if dirn == "long"  and bull: s += 40
    elif dirn == "short" and bear: s += 40
    elif adx_v > 15: s += 15
    else: s += 5
    # RSI (25pts)
    if dirn == "long":
        if 35 <= rsi_v <= 55: s += 25
        elif rsi_v < 35: s += 20
        elif rsi_v <= 65: s += 10
    else:
        if 45 <= rsi_v <= 65: s += 25
        elif rsi_v > 65: s += 20
        elif rsi_v >= 35: s += 10
    # 15m alignment (20pts)
    if len(cl15) >= 21:
        e8_ = ema(cl15, 8)[-1]; e21_ = ema(cl15, 21)[-1]
        if dirn == "long"  and cl15[-1] > e8_ > e21_: s += 20
        elif dirn == "short" and cl15[-1] < e8_ < e21_: s += 20
        else: s += 5
    else: s += 10
    # ADX strength (15pts)
    if adx_v > 30: s += 15
    elif adx_v > 22: s += 10
    elif adx_v > 15: s += 5

    return min(s, 100), ""

# ════════════════════════════════════════════════════════════════════════
#  BOT ENGINE
# ════════════════════════════════════════════════════════════════════════
class Bot:
    def __init__(self):
        self.api        = API()
        self.running    = False
        self.connected  = False
        self.capital    = 0.0
        self.start_cap  = 0.0
        self.day_start  = 0.0
        self.halted     = False
        self.halt_msg   = ""
        self.status     = "Not connected"
        self.logs       = []    # [{t, l, m}]
        self.trades     = []    # trade history shown in dashboard
        self.scan_n     = 0
        self.next_scan  = None
        self.price      = 0.0
        self.regime     = "—"
        self.rsi_v      = 50.0
        self.adx_v      = 0.0
        self.atr_pct    = 0.0
        self.l_score    = 0
        self.s_score    = 0
        self.l_veto     = ""
        self.s_veto     = ""
        self.total_tr   = 0
        self.wins       = 0
        self._stops     = set()   # product_ids with stop already placed

    # ── Logging ──────────────────────────────────────────────────────────
    def emit(self, level, msg):
        e = {"t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
             "l": level, "m": msg}
        self.logs.append(e)
        if len(self.logs) > 500: self.logs.pop(0)
        getattr(log, {"INFO":"info","WARN":"warning","ERROR":"error",
                      "TRADE":"info"}.get(level, "info"))(msg)

    # ── State persistence ─────────────────────────────────────────────────
    def save(self):
        try:
            json.dump({
                "start_cap": self.start_cap,
                "day_start": self.day_start,
                "halted": self.halted,
                "halt_msg": self.halt_msg,
                "total_tr": self.total_tr,
                "wins": self.wins,
                "trades": self.trades[-100:],
                "stops": list(self._stops),
            }, open(C.STATE_FILE, "w"))
        except: pass

    def load(self):
        try:
            if not os.path.exists(C.STATE_FILE): return False
            s = json.load(open(C.STATE_FILE))
            self.start_cap = float(s.get("start_cap", 0))
            self.day_start = float(s.get("day_start", 0))
            self.halted    = bool(s.get("halted", False))
            self.halt_msg  = s.get("halt_msg", "")
            self.total_tr  = int(s.get("total_tr", 0))
            self.wins      = int(s.get("wins", 0))
            self.trades    = s.get("trades", [])
            self._stops    = set(s.get("stops", []))
            if self.start_cap > 0:
                self.emit("INFO", f"Restored: start=${self.start_cap:.2f} "
                          f"trades={self.total_tr}")
                return True
        except: pass
        return False

    # ── Connect ───────────────────────────────────────────────────────────
    def connect(self, key, secret):
        self.api.creds(key, secret)
        bal = self.api.balance()
        if bal <= 0:
            return {"success": False,
                    "message": "Failed — check API key/secret and IP whitelist"}
        self.capital   = bal
        self.connected = True
        if not self.load() or self.start_cap <= 0:
            self.start_cap = bal
            self.day_start = bal
            self.save()
        self.emit("INFO",
            f"Connected ✓ | Balance ${bal:.2f} | "
            f"Start ${self.start_cap:.2f} | "
            f"Loss ceiling ${self.start_cap*(1-C.HALT_PCT):.2f}")
        self._sync_delta_positions()
        if not self.running: self.start()
        return {"success": True, "balance": bal}

    # ── Sync existing Delta positions into trades ──────────────────────────
    def _sync_delta_positions(self):
        """Read real positions from Delta and add to trades if not tracked."""
        positions = self.api.positions()
        for p in positions:
            sz    = float(p.get("size", 0) or 0)
            entry = float(p.get("entry_price") or
                          p.get("avg_entry_price") or 0)
            if sz == 0 or entry == 0: continue
            pid   = str(p.get("product_id", C.PID))
            sym   = str(p.get("product_symbol", "BTCUSD"))
            side  = "long" if sz > 0 else "short"
            lots  = abs(int(sz))
            upnl  = float(p.get("unrealized_pnl", 0) or 0)

            # Check if already in trades (open entry with same pid)
            exists = any(
                str(t.get("pid", "")) == pid and t.get("exit") is None
                for t in self.trades)

            if not exists:
                self.trades.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "side": side, "entry": round(entry, 1),
                    "exit": None, "lots": lots,
                    "pnl": None, "pct": None,
                    "reason": "delta_sync", "won": None,
                    "pid": pid, "symbol": sym,
                    "upnl": round(upnl, 3),
                })
                self.emit("INFO",
                    f"📋 Synced position: {side.upper()} {lots}L "
                    f"{sym} @ ${entry:.0f} UPL=${upnl:.3f}")

            # Place stop order if not already done
            if pid not in self._stops:
                stop_p = entry * (1-C.STOP_PCT if side=="long" else 1+C.STOP_PCT)
                tp_p   = entry * (1+C.TP_PCT   if side=="long" else 1-C.TP_PCT)
                cs     = "sell" if side=="long" else "buy"
                r = self.api.bracket(cs, lots, stop_p, tp_p)
                if r.get("success"):
                    self._stops.add(pid)
                    self.emit("INFO",
                        f"🛑 Stop placed: {side.upper()} {lots}L "
                        f"stop=${stop_p:.0f} TP=${tp_p:.0f}")
                    self.save()
                else:
                    self.emit("WARN",
                        f"⚠ Stop FAILED — SET MANUALLY: stop=${stop_p:.0f} | "
                        f"err={r.get('error','?')[:40]}")

    # ── Wallet sync with halt check ────────────────────────────────────────
    def _sync_wallet(self):
        bal = self.api.balance()
        if bal <= 0: return
        self.capital = bal
        if self.start_cap > 0:
            loss = (self.start_cap - bal) / self.start_cap
            if loss >= C.HALT_PCT and not self.halted:
                self.halted   = True
                self.halt_msg = (f"Down {loss*100:.1f}% from "
                                 f"${self.start_cap:.2f} → ${bal:.2f}")
                self.emit("ERROR", f"🛑 BOT HALTED: {self.halt_msg}")
                self.save()
        status = "🛑 HALTED" if self.halted else "✅ OK"
        self.emit("INFO", f"Wallet ${bal:.2f} | {status}")

    # ── Display helper for positions ───────────────────────────────────────
    def _pos_display(self):
        out = []
        for p in self.api.positions():
            sz    = float(p.get("size", 0) or 0)
            entry = float(p.get("entry_price") or
                          p.get("avg_entry_price") or 0)
            mark  = float(p.get("mark_price") or self.price or entry)
            upnl  = float(p.get("unrealized_pnl") or 0)
            if sz == 0 or entry == 0: continue
            side  = "long" if sz > 0 else "short"
            pct   = ((mark-entry)/entry if side=="long"
                     else (entry-mark)/entry) * 100
            out.append({
                "symbol": p.get("product_symbol", "BTCUSD"),
                "side": side, "lots": abs(sz),
                "entry": round(entry, 1), "mark": round(mark, 1),
                "upnl": round(upnl, 3), "pct": round(pct, 2),
                "stop": round(entry * (1-C.STOP_PCT if side=="long"
                                       else 1+C.STOP_PCT), 1),
                "tp":   round(entry * (1+C.TP_PCT if side=="long"
                                       else 1-C.TP_PCT), 1),
            })
        return out

    # ── Software exit check (backup to bracket orders) ─────────────────────
    def _check_exits(self, positions):
        if not self.price: return
        for p in positions:
            sz    = float(p.get("size", 0) or 0)
            entry = float(p.get("entry_price") or
                          p.get("avg_entry_price") or 0)
            if sz == 0 or entry == 0: continue
            side  = "long" if sz > 0 else "short"
            pct   = ((self.price-entry)/entry if side == "long"
                     else (entry-self.price)/entry)
            lots  = abs(int(sz))
            pid   = p.get("product_id", C.PID)

            if pct <= -C.STOP_PCT:
                cs = "sell" if side == "long" else "buy"
                r  = self.api.post("/v2/orders", {
                    "product_id": pid, "size": lots, "side": cs,
                    "order_type": "market_order", "time_in_force": "ioc"})
                if r.get("success"):
                    pnl = entry * lots * C.LOT_BTC * pct
                    self.emit("TRADE",
                        f"❌ STOP | {side.upper()} {lots}L "
                        f"${entry:.0f}→${self.price:.0f} "
                        f"P&L ${pnl:+.3f} ({pct*100:.2f}%)")
                    self._record_close(side, entry, self.price, lots, pct, "stop")

            elif pct >= C.TP_PCT:
                cs = "sell" if side == "long" else "buy"
                r  = self.api.post("/v2/orders", {
                    "product_id": pid, "size": lots, "side": cs,
                    "order_type": "market_order", "time_in_force": "ioc"})
                if r.get("success"):
                    pnl = entry * lots * C.LOT_BTC * pct
                    self.emit("TRADE",
                        f"✅ TP HIT | {side.upper()} {lots}L "
                        f"${entry:.0f}→${self.price:.0f} "
                        f"P&L ${pnl:+.3f} ({pct*100:.2f}%)")
                    self._record_close(side, entry, self.price, lots, pct, "tp")

    def _record_close(self, side, entry, exit_p, lots, pct, reason):
        won = pct > 0
        if won: self.wins += 1
        pnl = round(entry * lots * C.LOT_BTC * pct, 4)
        # Update open trade entry to closed
        for t in reversed(self.trades):
            if (t.get("side") == side and
                    t.get("entry") == round(entry, 1) and
                    t.get("exit") is None):
                t["exit"]   = round(exit_p, 1)
                t["pnl"]    = pnl
                t["pct"]    = round(pct*100, 2)
                t["won"]    = won
                t["reason"] = reason
                break
        self.save()

    # ── Main scan ─────────────────────────────────────────────────────────
    def scan(self):
        self.scan_n += 1
        self.next_scan = (datetime.now(timezone.utc) +
                          timedelta(seconds=C.SCAN)).isoformat()

        # Update price
        p = self.api.btc_price()
        if p > 0: self.price = p

        # Wallet every 5 scans
        if self.scan_n % 5 == 0:
            self._sync_wallet()

        if self.halted:
            self.status = f"🛑 HALTED: {self.halt_msg}"
            return

        # Fetch candles
        raw5  = self.api.candles("5m",  100)
        raw15 = self.api.candles("15m", 60)
        cl, hi, lo, vo = candles_to_arrays(raw5)
        cl15, *_       = candles_to_arrays(raw15)

        if len(cl) < 55:
            self.status = f"⚠ Only {len(cl)} candles (need 55)"
            self.emit("WARN", self.status)
            return

        self.price = cl[-1]
        self.rsi_v = rsi(cl)
        self.adx_v, pdi, ndi = adx(hi, lo, cl)
        atr_v      = atr(hi, lo, cl)
        self.atr_pct = round(atr_v / self.price * 100, 3)

        # Regime
        e8  = ema(cl, 8)[-1]
        e21 = ema(cl, 21)[-1]
        e55 = ema(cl, 55)[-1]
        if   self.price>e8>e21>e55 and self.adx_v>25 and pdi>ndi: self.regime="STRONG BULL"
        elif self.price>e8>e21 and self.adx_v>18:                  self.regime="BULL"
        elif self.price<e8<e21<e55 and self.adx_v>25 and ndi>pdi: self.regime="STRONG BEAR"
        elif self.price<e8<e21 and self.adx_v>18:                  self.regime="BEAR"
        else:                                                        self.regime="NEUTRAL"

        # Check real Delta positions
        real = self.api.positions()
        self._check_exits(real)
        self._sync_delta_positions()   # Ensure all positions tracked

        # Guard: max 1 position at a time
        if len(real) >= 1:
            d = self._pos_display()
            x = d[0] if d else {}
            self.status = (
                f"Holding {x.get('side','?').upper()} "
                f"{x.get('lots',0):.0f}L @ ${x.get('entry',0):,.0f} | "
                f"UPL ${x.get('upnl',0):+.3f} ({x.get('pct',0):+.2f}%)")
            self.emit("INFO", self.status)
            return

        # Daily loss check
        if self.day_start > 0:
            day_loss = (self.capital - self.day_start) / self.day_start
            if day_loss <= -C.PAUSE_PCT:
                self.status = f"⏸ Daily -{C.PAUSE_PCT*100:.0f}% limit — paused"
                return

        # Score signals
        ls, lv = score(cl, hi, lo, vo, cl15,
                        datetime.now(timezone.utc).hour, "long")
        ss, sv = score(cl, hi, lo, vo, cl15,
                        datetime.now(timezone.utc).hour, "short")
        self.l_score = ls; self.s_score = ss
        self.l_veto  = lv; self.s_veto  = sv

        self.emit("INFO",
            f"#{self.scan_n} ${self.price:,.0f} | {self.regime} | "
            f"RSI={self.rsi_v} ADX={self.adx_v} | "
            f"L={ls}{'✗'+lv if lv else '✓'} "
            f"S={ss}{'✗'+sv if sv else '✓'}")

        dirn = sc = None
        if not lv and ls >= C.MIN_CONF and ls > ss: dirn, sc = "long",  ls
        elif not sv and ss >= C.MIN_CONF and ss > ls: dirn, sc = "short", ss

        if not dirn:
            why = (lv or sv or
                   (f"ADX {self.adx_v}<20" if self.adx_v < 20
                    else f"score {max(ls,ss)}<{C.MIN_CONF}"))
            self.status = f"Watching — {why} | {self.regime}"
            return

        # Lot sizing (1.5% risk, min 1 lot)
        m = self.price * C.LOT_BTC / C.LEVERAGE   # margin per lot
        risk = max(self.capital * C.RISK_PCT, m)
        lots = max(1, min(int(risk / m),
                          max(1, int(self.capital * 0.10 / m))))

        side = "buy" if dirn == "long" else "sell"
        self.emit("INFO",
            f"→ {side.upper()} {lots}L @ ${self.price:,.0f} "
            f"score={sc} margin=${m*lots:.2f}")

        r = self.api.order(side, lots)
        if not r.get("success"):
            err = r.get("error", r.get("message", str(r)[:60]))
            self.status = f"❌ Order failed: {err}"
            self.emit("ERROR", f"Order FAILED: {err}")
            return

        # Place bracket stop immediately
        stop_p = self.price * (1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp_p   = self.price * (1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
        cs     = "sell" if dirn == "long" else "buy"
        sr = self.api.bracket(cs, lots, stop_p, tp_p)
        if sr.get("success"):
            self._stops.add(str(C.PID))
            self.emit("INFO",
                f"🛑 Stop ${stop_p:.0f} | TP ${tp_p:.0f}")
        else:
            self.emit("WARN",
                f"⚠ BRACKET FAILED — SET STOP MANUALLY @ ${stop_p:.0f}")

        self.status = (f"✅ {dirn.upper()} {lots}L @ "
                       f"${self.price:,.0f} (score={sc})")
        self.emit("TRADE", self.status)
        self.total_tr += 1
        self.trades.append({
            "time":   datetime.now(timezone.utc).isoformat(),
            "side":   dirn, "entry": round(self.price, 1),
            "exit":   None, "lots": lots, "pnl": None, "pct": None,
            "reason": "bot", "won": None,
            "pid":    str(C.PID), "symbol": "BTCUSD",
        })
        self.save()

    # ── Bot lifecycle ─────────────────────────────────────────────────────
    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self._loop, daemon=True).start()
            self.emit("INFO", "▶ Bot started")

    def stop(self):
        self.running = False
        self.emit("INFO", "■ Bot stopped")

    def _loop(self):
        while self.running:
            try: self.scan()
            except Exception as e:
                log.error(f"Scan error: {e}", exc_info=True)
                self.status = f"Error: {e}"
            time.sleep(C.SCAN)

    # ── State for dashboard ───────────────────────────────────────────────
    def state(self):
        sc   = self.start_cap or self.capital
        pnl  = (self.capital - sc) / sc * 100 if sc > 0 else 0.0
        done = [t for t in self.trades if t.get("won") is not None]
        wr   = sum(1 for t in done if t["won"]) / len(done) * 100 if done else 0
        return {
            "running":    self.running,
            "connected":  self.connected,
            "halted":     self.halted,
            "halt_msg":   self.halt_msg,
            "status":     self.status,
            "price":      round(self.price, 1),
            "regime":     self.regime,
            "rsi":        self.rsi_v,
            "adx":        self.adx_v,
            "atr_pct":    self.atr_pct,
            "l_score":    self.l_score,
            "s_score":    self.s_score,
            "l_veto":     self.l_veto,
            "s_veto":     self.s_veto,
            "capital":    round(self.capital, 2),
            "start_cap":  round(sc, 2),
            "pnl_pct":    round(pnl, 2),
            "win_rate":   round(wr, 1),
            "total_trades": self.total_tr,
            "wins":       self.wins,
            "next_scan":  self.next_scan,
            "scan_n":     self.scan_n,
            "open_pos":   self._pos_display(),
            "trades":     list(reversed(self.trades[-50:])),
            "logs":       list(reversed(self.logs[-100:])),
            "guardrails": {
                "Hard stop":     f"{C.STOP_PCT*100:.1f}% (bracket order on Delta)",
                "Take profit":   f"{C.TP_PCT*100:.1f}%",
                "Monthly halt":  f"Down {C.HALT_PCT*100:.0f}% from session start",
                "Daily pause":   f"Down {C.PAUSE_PCT*100:.0f}% in one day",
                "Max positions": "1 (checked from Delta API live)",
            },
        }



DASHBOARD = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">\n<title>Alpha Bot</title>\n<style>\n*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\nbody{background:#f5f5f5;color:#1a1a2e;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;font-size:14px;min-height:100vh}\n/* Header */\n.hdr{background:#fff;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 1px 4px rgba(0,0,0,.08);position:sticky;top:0;z-index:100}\n.hdr-logo{font-size:16px;font-weight:700;color:#00b386;letter-spacing:.5px;display:flex;align-items:center;gap:6px}\n.hdr-logo span{background:#00b386;color:#fff;width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:900}\n.conn-pill{font-size:11px;font-weight:600;padding:4px 10px;border-radius:20px;display:flex;align-items:center;gap:5px}\n.conn-pill.ok{background:#e6f9f3;color:#00b386}\n.conn-pill.off{background:#fff2f2;color:#e74c3c}\n.conn-dot{width:7px;height:7px;border-radius:50%;background:currentColor}\n/* Tabs */\n.tabs{background:#fff;display:flex;border-bottom:1px solid #eee;position:sticky;top:56px;z-index:99}\n.tab{flex:1;padding:12px 0;text-align:center;font-size:12px;font-weight:600;color:#888;border-bottom:2px solid transparent;cursor:pointer;text-transform:uppercase;letter-spacing:.5px;transition:.2s}\n.tab.on{color:#00b386;border-bottom-color:#00b386}\n/* Panels */\n.pnl{display:none;padding:12px}\n.pnl.on{display:block}\n/* Cards */\n.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,.06)}\n.c-title{font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}\n/* Price card */\n.price-big{font-size:38px;font-weight:700;color:#1a1a2e;line-height:1}\n.regime-chip{display:inline-block;margin-top:8px;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700}\n.chip-bull{background:#e6f9f3;color:#00b386}\n.chip-bear{background:#fff2f2;color:#e74c3c}\n.chip-neu{background:#f3f3f3;color:#888}\n/* Score row */\n.score-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}\n.score-box{background:#f8f8f8;border-radius:10px;padding:12px 8px;text-align:center}\n.score-lbl{font-size:10px;color:#888;margin-bottom:4px;font-weight:600;text-transform:uppercase}\n.score-num{font-size:24px;font-weight:700;line-height:1}\n.s-green{color:#00b386}\n.s-red{color:#e74c3c}\n.s-wait{color:#f39c12}\n.score-sub{font-size:9px;color:#aaa;margin-top:2px;min-height:12px}\n/* Indicators */\n.ind-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:10px}\n.ind{background:#f8f8f8;border-radius:10px;padding:10px;text-align:center}\n.ind-lbl{font-size:10px;color:#888;margin-bottom:3px;font-weight:600}\n.ind-val{font-size:17px;font-weight:700;color:#1a1a2e}\n/* Status bar */\n.status-bar{background:#f0f9ff;border:1px solid #d0eaf8;border-radius:10px;padding:10px 12px;font-size:12px;color:#2980b9;margin-bottom:10px;min-height:36px;line-height:1.5}\n.status-bar.warn{background:#fffbf0;border-color:#f8e5b0;color:#d68910}\n.status-bar.hold{background:#e8f5e9;border-color:#c8e6c9;color:#2e7d32}\n/* Progress */\n.progress{height:3px;background:#eee;border-radius:2px;overflow:hidden;margin:8px 0 4px}\n.progress-fill{height:100%;background:#00b386;border-radius:2px;transition:width .4s}\n.countdown{font-size:11px;color:#aaa}\n/* Open position card */\n.pos-card{border-radius:12px;padding:14px;margin-bottom:10px}\n.pos-long{background:#e6f9f3;border:1px solid #b2dfdb}\n.pos-short{background:#fff2f2;border:1px solid #ffcdd2}\n.pos-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}\n.pos-sym{font-size:16px;font-weight:700}\n.pos-badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}\n.badge-long{background:#00b386;color:#fff}\n.badge-short{background:#e74c3c;color:#fff}\n.pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}\n.pos-item{background:rgba(255,255,255,.7);border-radius:8px;padding:8px}\n.pos-item-lbl{font-size:10px;color:#666;margin-bottom:2px}\n.pos-item-val{font-size:14px;font-weight:700}\n.pos-stop{color:#e74c3c}\n.pos-tp{color:#00b386}\n.pos-upnl-neg{color:#e74c3c}\n.pos-upnl-pos{color:#00b386}\n/* Wallet */\n.wallet-row{display:flex;justify-content:space-between;align-items:baseline}\n.wallet-amt{font-size:30px;font-weight:700}\n.wallet-pct{font-size:16px;font-weight:700}\n.wallet-start{font-size:11px;color:#aaa;margin-top:2px}\n/* Stats row */\n.stats-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}\n.stat{background:#f8f8f8;border-radius:10px;padding:12px;text-align:center}\n.stat-lbl{font-size:10px;color:#888;margin-bottom:4px;font-weight:600}\n.stat-val{font-size:20px;font-weight:700}\n/* Buttons */\n.btn-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}\n.btn{padding:14px;border-radius:10px;border:none;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;letter-spacing:.3px;width:100%}\n.btn-start{background:#00b386;color:#fff}\n.btn-stop{background:#e74c3c;color:#fff}\n.btn-scan{background:#3498db;color:#fff;width:100%;margin-bottom:8px}\n.btn-close{background:#e74c3c;color:#fff;width:100%;opacity:.85}\n.btn-manual-l{background:#e6f9f3;color:#00b386;border:1.5px solid #00b386;flex:1}\n.btn-manual-s{background:#fff2f2;color:#e74c3c;border:1.5px solid #e74c3c;flex:1}\n.manual-row{display:flex;gap:8px;margin-top:8px}\n/* Input */\n.inp{width:100%;border:1.5px solid #ddd;border-radius:10px;padding:12px;font-size:14px;font-family:inherit;margin-bottom:8px;outline:none;transition:.2s}\n.inp:focus{border-color:#00b386}\n/* Trades */\n.trade-item{background:#f8f8f8;border-radius:10px;padding:12px;margin-bottom:8px}\n.trade-top{display:flex;justify-content:space-between;margin-bottom:6px}\n.trade-side-l{font-weight:700;color:#00b386}\n.trade-side-s{font-weight:700;color:#e74c3c}\n.trade-time{font-size:11px;color:#aaa}\n.trade-prices{display:flex;justify-content:space-between;font-size:12px;color:#666}\n.trade-pnl{font-size:13px;font-weight:700;margin-top:4px}\n.trade-pnl-pos{color:#00b386}\n.trade-pnl-neg{color:#e74c3c}\n.trade-open-tag{font-size:11px;color:#f39c12;font-style:italic;font-weight:600}\n.trade-tag{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-left:6px;font-weight:600}\n.tag-synced{background:#fff3cd;color:#856404}\n.tag-bot{background:#d4edda;color:#155724}\n.tag-manual{background:#d1ecf1;color:#0c5460}\n/* Logs */\n.log-box{background:#1a1a2e;border-radius:10px;padding:12px;max-height:400px;overflow-y:auto}\n.log-entry{padding:4px 0;border-bottom:1px solid #252540;font-size:11px;display:flex;gap:8px;font-family:\'Courier New\',monospace}\n.log-t{color:#555;white-space:nowrap}\n.log-INFO{color:#7f8c8d}\n.log-WARN{color:#f39c12}\n.log-ERROR{color:#e74c3c}\n.log-TRADE{color:#00b386;font-weight:700}\n/* Filter pills */\n.filter-row{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}\n.fpill{padding:5px 12px;border-radius:20px;border:1px solid #ddd;background:#fff;font-size:11px;font-weight:600;cursor:pointer;color:#888}\n.fpill.on{background:#1a1a2e;color:#fff;border-color:#1a1a2e}\n/* Guard list */\n.guard-item{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f0f0f0;font-size:13px}\n.guard-item:last-child{border:none}\n.guard-key{color:#555}\n.guard-val{color:#00b386;font-weight:600;text-align:right;max-width:55%}\n/* Halted banner */\n.halt-banner{background:#fff2f2;border:1.5px solid #e74c3c;border-radius:12px;padding:14px;margin-bottom:10px;text-align:center;color:#e74c3c;font-weight:700}\n/* IP card */\n.ip-display{font-size:22px;font-weight:700;color:#1a1a2e;text-align:center;padding:12px;background:#f8f8f8;border-radius:10px;font-family:\'Courier New\';letter-spacing:1px;margin-bottom:8px}\n</style>\n</head>\n<body>\n\n<!-- HEADER -->\n<div class="hdr">\n  <div class="hdr-logo"><span>Δ</span> ALPHA BOT</div>\n  <div class="conn-pill off" id="connPill">\n    <div class="conn-dot"></div><span id="connLabel">Not connected</span>\n  </div>\n</div>\n\n<!-- TABS -->\n<div class="tabs">\n  <div class="tab on"  onclick="switchTab(\'home\')">Home</div>\n  <div class="tab"     onclick="switchTab(\'trades\')">Trades</div>\n  <div class="tab"     onclick="switchTab(\'logs\')">Logs</div>\n  <div class="tab"     onclick="switchTab(\'settings\')">Settings</div>\n</div>\n\n<!-- HOME -->\n<div id="pnl-home" class="pnl on">\n  <div id="haltBanner" class="halt-banner" style="display:none"></div>\n\n  <div class="card">\n    <div class="c-title">Bitcoin · Live</div>\n    <div class="price-big" id="btcPrice">—</div>\n    <span class="regime-chip chip-neu" id="regimeChip">Loading...</span>\n  </div>\n\n  <div class="card">\n    <div id="statusBar" class="status-bar">Initializing...</div>\n    <div class="score-row">\n      <div class="score-box">\n        <div class="score-lbl">↑ Long</div>\n        <div class="score-num s-green" id="lScore">—</div>\n        <div class="score-sub" id="lVeto"></div>\n      </div>\n      <div class="score-box">\n        <div class="score-lbl">↓ Short</div>\n        <div class="score-num s-red" id="sScore">—</div>\n        <div class="score-sub" id="sVeto"></div>\n      </div>\n      <div class="score-box">\n        <div class="score-lbl">⚡ Signal</div>\n        <div class="score-num s-wait" id="decision">WAIT</div>\n        <div class="score-sub" id="decSub"></div>\n      </div>\n    </div>\n    <div class="ind-row">\n      <div class="ind"><div class="ind-lbl">RSI 14</div><div class="ind-val" id="rsiV">—</div></div>\n      <div class="ind"><div class="ind-lbl">ADX 14</div><div class="ind-val" id="adxV">—</div></div>\n      <div class="ind"><div class="ind-lbl">ATR %</div><div class="ind-val" id="atrV">—</div></div>\n    </div>\n    <div class="progress"><div class="progress-fill" id="scanBar" style="width:0"></div></div>\n    <div class="countdown" id="countdown">Next scan in —</div>\n  </div>\n\n  <!-- Open Positions -->\n  <div id="openPosArea"></div>\n\n  <div class="card">\n    <div class="c-title">Wallet Balance</div>\n    <div class="wallet-row">\n      <div class="wallet-amt" id="walletAmt">$—</div>\n      <div class="wallet-pct" id="walletPct">—</div>\n    </div>\n    <div class="wallet-start" id="walletStart"></div>\n  </div>\n\n  <div class="card">\n    <div class="stats-row">\n      <div class="stat"><div class="stat-lbl">Win Rate</div><div class="stat-val s-green" id="winRate">—</div></div>\n      <div class="stat"><div class="stat-lbl">Trades</div><div class="stat-val" id="tradeCount">0</div></div>\n      <div class="stat"><div class="stat-lbl">Scan #</div><div class="stat-val" style="color:#3498db" id="scanN">0</div></div>\n    </div>\n  </div>\n\n  <div class="btn-row"><button class="btn btn-start" onclick="startBot()">▶ Start</button><button class="btn btn-stop" onclick="stopBot()">■ Stop</button></div>\n  <button class="btn btn-scan" onclick="scanNow()">⚡ Scan Now</button>\n  <button class="btn btn-close" onclick="closeAll()">⚠ Close All Positions</button>\n\n  <div class="card" style="margin-top:10px">\n    <div class="c-title">Manual Trade</div>\n    <input class="inp" id="manLots" type="number" placeholder="Lots (default: 1)" min="1">\n    <div class="manual-row">\n      <button class="btn btn-manual-l" onclick="manual(\'long\')">↑ BUY LONG</button>\n      <button class="btn btn-manual-s" onclick="manual(\'short\')">↓ SELL SHORT</button>\n    </div>\n  </div>\n</div>\n\n<!-- TRADES -->\n<div id="pnl-trades" class="pnl">\n  <div id="tradesList">\n    <div style="text-align:center;padding:40px;color:#aaa;font-size:13px">No trades yet</div>\n  </div>\n</div>\n\n<!-- LOGS -->\n<div id="pnl-logs" class="pnl">\n  <div class="filter-row">\n    <div class="fpill on" onclick="flog(\'\')" id="fp-all">All</div>\n    <div class="fpill" onclick="flog(\'TRADE\')" id="fp-TRADE">Trades</div>\n    <div class="fpill" onclick="flog(\'WARN\')" id="fp-WARN">Warnings</div>\n    <div class="fpill" onclick="flog(\'ERROR\')" id="fp-ERROR">Errors</div>\n  </div>\n  <div id="logCount" style="font-size:11px;color:#aaa;margin-bottom:6px">0 entries</div>\n  <div class="log-box" id="logBox"></div>\n</div>\n\n<!-- SETTINGS -->\n<div id="pnl-settings" class="pnl">\n  <div class="card">\n    <div class="c-title">Delta Exchange Login</div>\n    <input class="inp" id="apiKey"    type="text"     placeholder="API Key">\n    <input class="inp" id="apiSecret" type="password" placeholder="API Secret">\n    <div style="display:flex;gap:8px;margin-bottom:4px">\n      <button class="btn" style="background:#1a1a2e;color:#fff;flex:1;font-size:12px" onclick="setRegion(\'india\')">India ✓</button>\n    </div>\n    <button class="btn btn-start" onclick="doConnect()">Connect to Delta Exchange</button>\n    <div id="connMsg" style="margin-top:8px;font-size:12px;text-align:center"></div>\n  </div>\n\n  <div class="card">\n    <div class="c-title">Server IP — Add to Delta Whitelist</div>\n    <div class="ip-display" id="serverIP">Loading...</div>\n    <div style="font-size:11px;color:#888;line-height:1.8">\n      1. Copy the IP above<br>\n      2. Delta Exchange → Account → API Keys → Edit<br>\n      3. Paste into IP Whitelist field → Save\n    </div>\n  </div>\n\n  <div class="card">\n    <div class="c-title">Active Guardrails</div>\n    <div id="guardList"></div>\n  </div>\n</div>\n\n<script>\nlet allLogs = [], logFilter = \'\';\n\nfunction switchTab(name) {\n  [\'home\',\'trades\',\'logs\',\'settings\'].forEach((t,i) => {\n    document.querySelectorAll(\'.tab\')[i].classList.toggle(\'on\', t === name);\n    document.getElementById(\'pnl-\'+t).classList.toggle(\'on\', t === name);\n  });\n  if (name === \'logs\') renderLogs();\n  if (name === \'trades\') renderTradesPanel();\n}\n\nasync function api(path, body) {\n  try {\n    const o = body ? {method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(body)} : {};\n    return await (await fetch(path, o)).json();\n  } catch(e) { return null; }\n}\n\nfunction fmt(n, d=2) {\n  return typeof n === \'number\' ? n.toLocaleString(undefined, {maximumFractionDigits:d}) : (n||\'—\');\n}\n\nlet currentTrades = [];\n\nfunction render(s) {\n  if (!s) return;\n\n  // Header\n  const ok = s.connected && !s.halted;\n  const pill = document.getElementById(\'connPill\');\n  pill.className = \'conn-pill \' + (ok ? \'ok\' : \'off\');\n  document.getElementById(\'connLabel\').textContent = s.halted ? \'HALTED\' : (s.connected ? \'Connected ✓\' : \'Not connected\');\n\n  // Halt banner\n  const hb = document.getElementById(\'haltBanner\');\n  hb.style.display = s.halted ? \'block\' : \'none\';\n  if (s.halted) hb.textContent = \'🛑 BOT HALTED: \' + s.halt_msg;\n\n  // Price\n  document.getElementById(\'btcPrice\').textContent = s.price ? \'$\' + s.price.toLocaleString() : \'—\';\n\n  // Regime\n  const rc = document.getElementById(\'regimeChip\');\n  rc.textContent = s.regime || \'—\';\n  const r = (s.regime||\'\').toLowerCase();\n  rc.className = \'regime-chip \' + (r.includes(\'bull\') ? \'chip-bull\' : r.includes(\'bear\') ? \'chip-bear\' : \'chip-neu\');\n\n  // Status\n  const sb = document.getElementById(\'statusBar\');\n  sb.textContent = s.status || \'—\';\n  sb.className = \'status-bar\' + (s.status?.includes(\'Holding\') ? \' hold\' : s.status?.includes(\'⚠\') ? \' warn\' : \'\');\n\n  // Scores\n  const ls = s.l_score || 0, ss = s.s_score || 0, mc = 58;\n  document.getElementById(\'lScore\').textContent = ls || \'—\';\n  document.getElementById(\'sScore\').textContent = ss || \'—\';\n  document.getElementById(\'lVeto\').textContent  = s.l_veto ? \'✗ \'+s.l_veto : (ls ? \'/100\' : \'\');\n  document.getElementById(\'sVeto\').textContent  = s.s_veto ? \'✗ \'+s.s_veto : (ss ? \'/100\' : \'\');\n\n  let dec = \'WAIT\', ds = \'No signal\', dc = \'s-wait\';\n  if (!s.l_veto && ls >= mc && ls > ss) { dec = \'LONG\';  ds = \'score=\'+ls; dc = \'s-green\'; }\n  if (!s.s_veto && ss >= mc && ss > ls) { dec = \'SHORT\'; ds = \'score=\'+ss; dc = \'s-red\';   }\n  const dEl = document.getElementById(\'decision\');\n  dEl.textContent = dec; dEl.className = \'score-num \'+dc;\n  document.getElementById(\'decSub\').textContent = ds;\n\n  // Indicators\n  document.getElementById(\'rsiV\').textContent = s.rsi   || \'—\';\n  document.getElementById(\'adxV\').textContent = s.adx   || \'—\';\n  document.getElementById(\'atrV\').textContent = s.atr_pct ? s.atr_pct+\'%\' : \'—\';\n\n  // Countdown\n  if (s.next_scan) {\n    const secs = Math.max(0, Math.round((new Date(s.next_scan) - Date.now()) / 1000));\n    document.getElementById(\'scanBar\').style.width = Math.max(0,100-secs/300*100)+\'%\';\n    document.getElementById(\'countdown\').textContent =\n      secs > 0 ? `Next scan in ${Math.floor(secs/60)}m ${secs%60}s` : \'Scanning now...\';\n  }\n\n  // Open positions\n  const ops = s.open_pos || [];\n  const oa = document.getElementById(\'openPosArea\');\n  oa.innerHTML = ops.map(p => `\n    <div class="pos-card pos-${p.side}">\n      <div class="pos-header">\n        <span class="pos-sym">${p.symbol}</span>\n        <span class="pos-badge badge-${p.side}">${p.side.toUpperCase()}</span>\n      </div>\n      <div class="pos-grid">\n        <div class="pos-item"><div class="pos-item-lbl">Entry Price</div>\n          <div class="pos-item-val">$${p.entry.toLocaleString()}</div></div>\n        <div class="pos-item"><div class="pos-item-lbl">Lots</div>\n          <div class="pos-item-val">${p.lots}</div></div>\n        <div class="pos-item"><div class="pos-item-lbl">Unrealised P&L</div>\n          <div class="pos-item-val ${p.upnl>=0?\'pos-upnl-pos\':\'pos-upnl-neg\'}">\n            $${p.upnl>=0?\'+\':\'\'}${p.upnl} (${p.pct>=0?\'+\':\'\'}${p.pct}%)</div></div>\n        <div class="pos-item"><div class="pos-item-lbl">Mark Price</div>\n          <div class="pos-item-val">$${(p.mark||p.entry).toLocaleString()}</div></div>\n        <div class="pos-item"><div class="pos-item-lbl">Stop Loss</div>\n          <div class="pos-item-val pos-stop">$${p.stop.toLocaleString()}</div></div>\n        <div class="pos-item"><div class="pos-item-lbl">Take Profit</div>\n          <div class="pos-item-val pos-tp">$${p.tp.toLocaleString()}</div></div>\n      </div>\n    </div>`).join(\'\');\n\n  // Wallet\n  document.getElementById(\'walletAmt\').textContent = s.capital ? \'$\'+s.capital.toFixed(2) : \'$—\';\n  const pp = s.pnl_pct || 0;\n  const wpEl = document.getElementById(\'walletPct\');\n  wpEl.textContent = (pp>=0?\'+\':\'\')+pp.toFixed(2)+\'%\';\n  wpEl.className = \'wallet-pct \' + (pp>=0?\'s-green\':\'s-red\');\n  document.getElementById(\'walletStart\').textContent = s.start_cap ? \'Started: $\'+s.start_cap.toFixed(2) : \'\';\n\n  // Stats\n  document.getElementById(\'winRate\').textContent    = s.win_rate != null ? s.win_rate+\'%\' : \'—\';\n  document.getElementById(\'tradeCount\').textContent = s.total_trades || 0;\n  document.getElementById(\'scanN\').textContent      = s.scan_n || 0;\n\n  // Logs\n  if (s.logs) allLogs = s.logs;\n  document.getElementById(\'logCount\').textContent = allLogs.length + \' entries\';\n  if (document.getElementById(\'pnl-logs\').classList.contains(\'on\')) renderLogs();\n\n  // Trades\n  if (s.trades) { currentTrades = s.trades; }\n  if (document.getElementById(\'pnl-trades\').classList.contains(\'on\')) renderTradesPanel();\n\n  // Guardrails\n  if (s.guardrails) {\n    document.getElementById(\'guardList\').innerHTML =\n      Object.entries(s.guardrails).map(([k,v])=>\n        `<div class="guard-item"><span class="guard-key">${k}</span>\n         <span class="guard-val">${v}</span></div>`).join(\'\');\n  }\n}\n\nfunction renderLogs() {\n  const f = logFilter ? allLogs.filter(e => e.l === logFilter) : allLogs;\n  document.getElementById(\'logBox\').innerHTML = f.slice(0, 100).map(e =>\n    `<div class="log-entry"><span class="log-t">${e.t}</span>\n     <span class="log-${e.l}">${e.m}</span></div>`).join(\'\');\n}\n\nfunction flog(f) {\n  logFilter = f;\n  [\'\',\'TRADE\',\'WARN\',\'ERROR\'].forEach(x => {\n    const id = \'fp-\'+(x||\'all\');\n    const el = document.getElementById(id);\n    if (el) el.classList.toggle(\'on\', f === x);\n  });\n  renderLogs();\n}\n\nfunction renderTradesPanel() {\n  const el = document.getElementById(\'tradesList\');\n  if (!currentTrades.length) {\n    el.innerHTML = \'<div style="text-align:center;padding:40px;color:#aaa;font-size:13px">No trades yet</div>\';\n    return;\n  }\n  el.innerHTML = currentTrades.map(t => {\n    const open = t.exit == null;\n    const sc   = t.side === \'long\' ? \'trade-side-l\' : \'trade-side-s\';\n    const tag  = t.reason === \'delta_sync\' ? \'<span class="trade-tag tag-synced">synced</span>\'\n               : t.reason === \'manual\'     ? \'<span class="trade-tag tag-manual">manual</span>\'\n               : \'<span class="trade-tag tag-bot">bot</span>\';\n    return `<div class="trade-item">\n      <div class="trade-top">\n        <span class="${sc}">${t.side.toUpperCase()} ${t.lots}L ${t.symbol||\'BTCUSD\'} ${tag}</span>\n        <span class="trade-time">${new Date(t.time).toLocaleTimeString()}</span>\n      </div>\n      <div class="trade-prices">\n        <span>Entry: $${(t.entry||0).toLocaleString()}</span>\n        ${open ? \'<span class="trade-open-tag">Open position...</span>\'\n               : `<span>Exit: $${(t.exit||0).toLocaleString()}</span>`}\n      </div>\n      ${!open ? `<div class="trade-pnl ${t.won?\'trade-pnl-pos\':\'trade-pnl-neg\'}">\n        ${t.won?\'✅ Profit\':\'❌ Loss\'}: $${t.pnl>0?\'+\':\'\'}${(t.pnl||0).toFixed(4)} (${t.pct>0?\'+\':\'\'}${(t.pct||0).toFixed(2)}%)\n        <span style="font-size:10px;color:#aaa;margin-left:6px">${t.reason}</span>\n      </div>` : \'\'}\n    </div>`;\n  }).join(\'\');\n}\n\nasync function startBot()   { await api(\'/api/bot/start\',{}); }\nasync function stopBot()    { await api(\'/api/bot/stop\',{}); }\nasync function scanNow()    { await api(\'/api/bot/run_now\',{}); document.getElementById(\'statusBar\').textContent=\'Scanning...\'; }\nasync function closeAll()   { if(!confirm(\'Close ALL positions?\')) return; const r=await api(\'/api/close_all\',{}); alert(\'Closed \'+r?.closed+\' positions\'); }\nasync function manual(d)    {\n  const lots=parseInt(document.getElementById(\'manLots\').value)||1;\n  const r=await api(\'/api/manual_trade\',{direction:d,lots});\n  if(r?.success) alert(`${d.toUpperCase()} ${lots}L placed\\nEntry: $${r.entry}\\nStop: $${r.stop}\\nTP: $${r.tp}`);\n  else alert(\'Failed: \'+(r?.message||\'check logs\'));\n}\nasync function doConnect()  {\n  const k=document.getElementById(\'apiKey\').value.trim();\n  const s=document.getElementById(\'apiSecret\').value.trim();\n  if(!k||!s){document.getElementById(\'connMsg\').innerHTML=\'<span style="color:#e74c3c">Enter both API key and secret</span>\';return;}\n  document.getElementById(\'connMsg\').textContent=\'Connecting...\';\n  const r=await api(\'/api/connect\',{api_key:k,api_secret:s});\n  document.getElementById(\'connMsg\').innerHTML = r?.success\n    ? `<span style="color:#00b386">✓ Connected — Balance $${(r.balance||0).toFixed(2)}</span>`\n    : `<span style="color:#e74c3c">✗ ${r?.message||\'Failed\'}</span>`;\n}\nfunction setRegion(r) {}\n\nasync function loadIp() {\n  try {\n    const r=await api(\'/api/ip\');\n    document.getElementById(\'serverIP\').textContent=r?.ip||\'unknown\';\n  } catch(e) {}\n}\n\nasync function poll() {\n  try { const s=await api(\'/api/status\'); if(s) render(s); } catch(e) {}\n}\n\nloadIp(); poll();\nsetInterval(poll, 4000);\nsetInterval(loadIp, 60000);\n</script>\n</body>\n</html>'


# ════════════════════════════════════════════════════════════════════════
#  FLASK APP
# ════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app)
bot = Bot()

if C.KEY and C.SECRET:
    threading.Thread(target=lambda: bot.connect(C.KEY, C.SECRET),
                     daemon=True).start()

@app.after_request
def cors_h(r):
    r.headers.update({"Access-Control-Allow-Origin": "*",
                       "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                       "Access-Control-Allow-Headers": "Content-Type"})
    return r

@app.route("/")
def index(): return Response(DASHBOARD, mimetype="text/html")

@app.route("/api/status")
@app.route("/api/bot/status")
def status(): return jsonify(bot.state())

@app.route("/api/connect", methods=["POST", "OPTIONS"])
def connect():
    if request.method == "OPTIONS": return jsonify({})
    d = request.json or {}
    k = d.get("api_key", "").strip()
    s = d.get("api_secret", "").strip()
    if not k or not s:
        return jsonify({"success": False,
                        "message": "api_key and api_secret required"})
    return jsonify(bot.connect(k, s))

@app.route("/api/bot/start",   methods=["POST"])
def start():   bot.start(); return jsonify({"success": True})

@app.route("/api/bot/stop",    methods=["POST"])
def stop():    bot.stop();  return jsonify({"success": True})

@app.route("/api/bot/run_now", methods=["POST"])
def run_now():
    threading.Thread(target=bot.scan, daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/trades")
def trades(): return jsonify(list(reversed(bot.trades[-50:])))

@app.route("/api/logs")
def logs():   return jsonify(bot.logs)

@app.route("/api/positions")
def positions():
    raw  = bot.api.positions()
    disp = bot._pos_display()
    return jsonify({"raw": raw, "display": disp})

@app.route("/api/ticker")
def ticker():
    p = bot.api.btc_price()
    if not p:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price"
                             "?ids=bitcoin&vs_currencies=usd", timeout=5)
            p = r.json().get("bitcoin", {}).get("usd", 0)
        except: pass
    return jsonify({"price": p, "mark_price": str(p)})

@app.route("/api/ip")
def ip():
    try:
        p = requests.get("https://api.ipify.org?format=json", timeout=5)
        i = p.json().get("ip", "unknown")
    except: i = "unknown"
    return jsonify({"ip": i, "whitelist_on_delta": i})

@app.route("/api/close_all", methods=["POST"])
def close_all():
    n = bot.api.close_all()
    bot.emit("TRADE", f"🔴 Emergency close: {n} position(s)")
    return jsonify({"success": True, "closed": n})

@app.route("/api/manual_trade", methods=["POST"])
def manual_trade():
    d    = request.json or {}
    dirn = d.get("direction", "")
    if dirn not in ("long", "short"):
        return jsonify({"success": False, "message": "direction: long or short"})
    p    = bot.price or bot.api.btc_price()
    lots = max(1, int(d.get("lots", 1)))
    side = "buy" if dirn == "long" else "sell"
    r    = bot.api.order(side, lots)
    if r.get("success"):
        stop = p * (1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
        tp   = p * (1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
        cs   = "sell" if dirn == "long" else "buy"
        bot.api.bracket(cs, lots, stop, tp)
        bot.emit("TRADE", f"MANUAL {dirn.upper()} {lots}L @ ${p:,.0f} "
                           f"stop=${stop:.0f} TP=${tp:.0f}")
        bot.trades.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "side": dirn, "entry": round(p, 1), "exit": None,
            "lots": lots, "pnl": None, "pct": None,
            "reason": "manual", "won": None,
            "pid": str(C.PID), "symbol": "BTCUSD",
        })
        bot.save()
        return jsonify({"success": True, "entry": round(p, 1),
                        "stop": round(stop, 1), "tp": round(tp, 1)})
    return jsonify({"success": False,
                    "message": r.get("error", "Order failed")})

@app.route("/api/set_stop", methods=["POST"])
def set_stop():
    d     = request.json or {}
    dirn  = d.get("direction", "long")
    entry = float(d.get("entry", bot.price or 77000))
    lots  = int(d.get("lots", 1))
    stop  = entry * (1-C.STOP_PCT if dirn=="long" else 1+C.STOP_PCT)
    tp    = entry * (1+C.TP_PCT   if dirn=="long" else 1-C.TP_PCT)
    cs    = "sell" if dirn == "long" else "buy"
    r     = bot.api.bracket(cs, lots, stop, tp)
    ok    = r.get("success", False)
    bot.emit("INFO" if ok else "WARN",
        f"{'✅' if ok else '❌'} Stop set: {dirn.upper()} "
        f"entry=${entry:.0f} stop=${stop:.0f} TP=${tp:.0f}")
    return jsonify({"success": ok, "stop": round(stop, 1),
                    "tp": round(tp, 1)})

@app.route("/api/debug/candles")
def debug_candles():
    for res in ["5m", "1m", "15m"]:
        d = bot.api.get("/v2/history/candles", {
            "symbol": "BTCUSD", "resolution": res,
            "start": int(time.time())-3600, "end": int(time.time())})
        if d and d.get("success") and d.get("result"):
            return jsonify({"ok": True, "res": res,
                            "count": len(d["result"]),
                            "sample": d["result"][0]})
    return jsonify({"ok": False})

@app.route("/api/debug/positions")
def debug_positions():
    return jsonify(bot.api.get("/v2/positions/margined") or
                   {"error": "no_response"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)