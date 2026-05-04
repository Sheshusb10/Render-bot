"""
ALPHA BOT v11 — Delta Exchange India BTC Options
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Expert BTC options trader logic:
- Multi-timeframe: 1m/5m/15m/1H/4H/Daily for direction
- Smart ITM vs ATM selection based on trend conviction
- Straddle when market is coiled (BB squeeze + low ADX)
- Directional options when trend is clear
- Profit floor: 64% of peak locked from first profit tick
- Hard stop: -15% on options, ATR-based on perps
- Scales lots with confidence (1x→2x→3x)
- Users + trades NEVER deleted on restart
- Bot learns from all users' trade history
- Reconciles ghost trades every scan
"""
import os,time,hmac,hashlib,json,math,logging,threading,requests,secrets,sys
from datetime import datetime,timezone,timedelta,date
from functools import wraps
from flask import Flask,jsonify,request,Response,session
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("alpha")

# ═══ PERSISTENT STORAGE — survives ALL restarts ═══════════════════
_DATA = os.path.expanduser("~/alphabot/data")
os.makedirs(_DATA, exist_ok=True)
USERS_FILE = os.path.join(_DATA,"users.json")
INTEL_FILE = os.path.join(_DATA,"intel.json")
MAX_USERS  = 5
# ── Stable session secret (persists across restarts) ─────────────
_SECRET_FILE = os.path.expanduser("~/alphabot/data/.secret")
def _load_secret():
    if os.path.exists(_SECRET_FILE):
        try:
            v = open(_SECRET_FILE).read().strip()
            if len(v) >= 32: return v
        except: pass
    v = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(_SECRET_FILE), exist_ok=True)
        open(_SECRET_FILE,"w").write(v)
    except: pass
    return v
BOT_SECRET = os.getenv("BOT_SECRET") or _load_secret()

# ═══ CONFIG ═══════════════════════════════════════════════════════
class C:
    BASE   = "https://api.india.delta.exchange"
    KEY    = os.getenv("DELTA_API_KEY","").strip()
    SECRET = os.getenv("DELTA_API_SECRET","").strip()
    PID    = 27; SYMBOL="BTCUSD"
    LOT    = 0.001   # Delta: 1 contract = 0.001 BTC (NEVER change)
    LEV    = 5; SCAN = 300

    # Perp TP/SL (ATR-dynamic overrides at runtime)
    STOP=0.025; TP=0.030; RISK=0.015

    # Options — tight and profitable
    OPT_TP   = 0.70  # +70% = hard take-profit (overridden by mode)
    OPT_STOP = 0.15  # -15% = hard stop-loss (overridden by mode)
    OPT_LOCK = 0.64  # floor = 64% of peak (peak +5% → floor at +3.2%)
    OPT_MAX  = 0.15  # max 15% capital per option
    OPT_EXP  = 180   # exit 3h before expiry

    # ── RISK MANAGEMENT RULES (from proven trading principles) ──────
    # Rule 1: Risk 1% of capital per trade MAX
    RISK_PER_TRADE = 0.01      # 1% per trade — non-negotiable
    # Rule 2: Daily loss limit 3% — shut down after
    DAILY_LOSS_LIMIT = 0.03    # 3% daily max loss
    # Rule 3: Weekly loss limit 8% — halt trading
    WEEKLY_LOSS_LIMIT = 0.08   # 8% weekly max loss
    # Rule 4: After losing trade — 4 hour cooling period
    LOSS_COOLDOWN_MINS = 60    # 60min after loss (4hrs too long for bot)
    # Rule 5: Circuit breaker — 3 losses in a row = pause
    CIRC_N=3; CIRC_MIN=120
    # Rule 6: Minimum hold time
    MIN_HOLD=15
    # Legacy (kept for compatibility)
    HALT=0.08; PAUSE=0.03; COOL=30

    # Signal thresholds
    CONF_BASE    = 62   # default
    CONF_MACRO   = 40   # macro aligned threshold (42+ passes)
    ADX_MIN      = 22

    # Session bias (from backtesting)
    DEAD_ZONE   = [11,12,13,18,19]  # From real trade data: UTC 11-13=IST17, UTC18-19=IST midnight
    PRIME_LONG  = [4,5,15,16,17]  # From real data: UTC 4-5=IST10am (best!), UTC15-17=IST21-23pm
    PRIME_SHORT = [13,14,15,16]  # UTC 1-4pm = IST 6:30-9:30pm (NY open, evening India)

    DEPLOY_TOKEN = os.getenv("DEPLOY_TOKEN","alphabot2025deploy")
    GITHUB = "https://raw.githubusercontent.com/Sheshusb10/Render-bot/main/server.py"

    # ── Trading Modes ─────────────────────────────────────────────
    # SAFE:   2% risk, +50% TP, -10% SL — protect capital
    # NORMAL: 5% risk, +70% TP, -15% SL — balanced (default)
    # PRO:    10% risk, +100% TP, -20% SL — only when balance>$500
    MODES = {
        "safe":   {"risk":0.02,"opt_tp":0.50,"opt_stop":0.10,"lot_mult":0.5,"min_bal":0},
        "normal": {"risk":0.05,"opt_tp":0.70,"opt_stop":0.15,"lot_mult":1.0,"min_bal":0},
        "pro":    {"risk":0.10,"opt_tp":1.00,"opt_stop":0.20,"lot_mult":2.0,"min_bal":500},
    }
    MODE = "normal"  # default

def pid_int(v):
    try: return int(v)
    except: return 0

# ═══ USER MANAGER — never deletes users ══════════════════════════
class UserManager:
    BACKUP = os.path.expanduser("~/alphabot/users_backup.json")

    def __init__(self):
        self._lk=threading.Lock()
        self.db=self._load()

    def _load(self):
        # Try primary first
        for path in [USERS_FILE, self.BACKUP]:
            try:
                if os.path.exists(path):
                    data = json.load(open(path))
                    if data.get("users"):  # only use if has actual users
                        log.info(f"Loaded users from {path}: {len(data['users'])} users")
                        return data
            except Exception as e:
                log.warning(f"Could not load {path}: {e}")
        return {"users":{},"invites":[]}

    def _save(self):
        """Save to BOTH locations — users are never lost."""
        for path in [USERS_FILE, self.BACKUP]:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                json.dump(self.db, open(path,"w"), indent=2)
            except Exception as e:
                log.warning(f"save users to {path}: {e}")

    def _hash(self,pw):
        return hashlib.pbkdf2_hmac("sha256",pw.encode(),b"alphabot2025",200000).hex()

    def setup_admin(self,username,password):
        with self._lk:
            if self.db["users"]: return False,"Already set up"
            uid=secrets.token_hex(8)
            self.db["users"][uid]={"username":username,"pw_hash":self._hash(password),
                "created":datetime.now(timezone.utc).isoformat(),"is_admin":True}
            self._save(); return True,uid

    def gen_invite(self):
        with self._lk:
            if not self.db["users"]: return None,"Create admin first"
            code=secrets.token_urlsafe(12)
            self.db["invites"].append(code); self._save(); return code,"ok"

    def register(self,invite,username,password):
        with self._lk:
            if invite not in self.db["invites"]: return False,"Invalid invite code"
            if len(self.db["users"])>=MAX_USERS: return False,f"Max {MAX_USERS} users"
            for u in self.db["users"].values():
                if u["username"].lower()==username.lower(): return False,"Username taken"
            if len(password)<6: return False,"Min 6 chars"
            uid=secrets.token_hex(8)
            self.db["users"][uid]={"username":username,"pw_hash":self._hash(password),
                "created":datetime.now(timezone.utc).isoformat(),"is_admin":False}
            self.db["invites"].remove(invite); self._save(); return True,uid

    def login(self,username,password):
        with self._lk:
            for uid,u in self.db["users"].items():
                if u["username"].lower()==username.lower() and \
                   u["pw_hash"]==self._hash(password):
                    return True,uid
            return False,None

    def get(self,uid): return self.db["users"].get(uid)
    def all(self): return {uid:{k:v for k,v in u.items() if k!="pw_hash"} for uid,u in self.db["users"].items()}
    def is_admin(self,uid): u=self.get(uid); return bool(u and u.get("is_admin"))
    def invites(self): return list(self.db.get("invites",[]))

um=UserManager(); bots={}

def _auto_setup():
    """
    Auto-create admin ONLY if NO users exist anywhere.
    Checks both primary and backup files before creating.
    NEVER overwrites existing users.
    """
    # Check primary
    for path in [USERS_FILE, um.BACKUP]:
        try:
            if os.path.exists(path):
                data = json.load(open(path))
                if data.get("users"):
                    # Users exist — restore if needed and return
                    if not um.db["users"]:
                        um.db = data
                        log.info(f"Restored {len(data['users'])} users from {path}")
                    return
        except: pass
    # Only reach here if NO users found anywhere
    pw=os.getenv("ADMIN_PASSWORD","Admin123")
    ok,_=um.setup_admin("admin",pw)
    if ok:
        log.info(f"First run: created admin (password: {pw})")
        for _ in range(4):
            code,_=um.gen_invite()
            log.info(f"Invite code: {code}")

def get_bot(uid):
    if uid not in bots:
        b=Bot()
        b._sf=os.path.join(_DATA,f"bot_{uid}.json")
        bots[uid]=b
        # Auto-reconnect if saved keys exist
        _try_auto_connect(uid, b)
    return bots[uid]

def _try_auto_connect(uid, b):
    """Load saved API keys and reconnect silently."""
    key_file=os.path.join(_DATA,f"keys_{uid}.json")
    if not os.path.exists(key_file): return
    try:
        import base64 as b64
        kd=json.load(open(key_file))
        k=b64.b64decode(kd["k"]).decode()
        s=b64.b64decode(kd["s"]).decode()
        if k and s:
            def _connect():
                result=b.connect(k,s)
                if result.get("success"):
                    log.info(f"Auto-connected user {uid[:6]} balance=${result.get('balance',0):.2f}")
                else:
                    log.warning(f"Auto-connect failed for {uid[:6]}: {result.get('message','?')}")
            threading.Thread(target=_connect,daemon=True).start()
    except Exception as e:
        log.warning(f"Auto-connect error {uid[:6]}: {e}")

# ═══ SHARED INTELLIGENCE — learns from all users ══════════════════
class Intel:
    def __init__(self):
        self._lk=threading.Lock()
        self.data=self._load()

    def _load(self):
        try:
            if os.path.exists(INTEL_FILE): return json.load(open(INTEL_FILE))
        except: pass
        return {"win_rate":0,"total":0,"by_hour":{},"by_regime":{},
                "good_hours":[],"bad_hours":[],"updated":None}

    def _save(self):
        try: json.dump(self.data,open(INTEL_FILE,"w"),indent=2)
        except: pass

    def ask_claude(self, trades, current_params):
        """
        Ask Claude to analyze trade history + bot logs and suggest improvements.
        Runs every 6 hours. Uses Anthropic API directly.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY","")
        if not api_key:
            log.info("Claude: no API key set"); return None
        if len(trades) < 3:
            log.info(f"Claude: need 3+ trades, have {len(trades)}"); return None

        # Read recent bot logs for error patterns
        recent_logs = []
        try:
            log_file = os.path.expanduser("~/alphabot/bot.log")
            with open(log_file) as lf:
                lines = lf.readlines()
                # Get last 50 lines with errors/warnings/trades
                recent_logs = [l.strip() for l in lines[-100:]
                    if any(x in l for x in ["ERROR","WARN","TRADE","veto","failed","HALT"])][-20:]
        except: pass
        try:
            # Build trade summary
            wins  = [t for t in trades if t.get("won")]
            losses= [t for t in trades if t.get("won")==False]
            wr    = len(wins)/len(trades)*100 if trades else 0

            # Hour breakdown
            hour_stats = {}
            for t in trades:
                try:
                    h = datetime.fromisoformat(t["time"]).hour
                    hour_stats.setdefault(h,{"w":0,"l":0,"pnl":0})
                    hour_stats[h]["pnl"] += float(t.get("pnl",0) or 0)
                    if t.get("won"): hour_stats[h]["w"]+=1
                    else: hour_stats[h]["l"]+=1
                except: pass

            # Side breakdown
            side_stats = {}
            for t in trades:
                s = t.get("side","?")
                side_stats.setdefault(s,{"w":0,"l":0,"pnl":0})
                side_stats[s]["pnl"] += float(t.get("pnl",0) or 0)
                if t.get("won"): side_stats[s]["w"]+=1
                else: side_stats[s]["l"]+=1

            prompt = f"""You are analyzing a BTC options trading bot on Delta Exchange India.
Current balance: ${current_params.get('capital',0):.2f}
Win rate: {wr:.1f}% over {len(trades)} trades
Wins: {len(wins)}, Losses: {len(losses)}

Trade P&L by side: {side_stats}
Trade P&L by UTC hour: {dict(sorted(hour_stats.items()))}

Current parameters:
- Confidence threshold: {current_params.get('conf_base',62)}
- Dead zone hours UTC: {current_params.get('dead_zone',[])}
- Prime long hours UTC: {current_params.get('prime_long',[])}

Last 10 trades (most recent first):
{chr(10).join(f"  {t.get('time','?')[:16]} {t.get('side','?')} {t.get('sym','?')} pnl=${t.get('pnl',0):+.4f} reason={t.get('reason','?')}" for t in sorted(trades,key=lambda x:x.get('time',''))[-10:][::-1])}

Recent bot errors/warnings:
{chr(10).join(recent_logs) if recent_logs else "None"}

Respond ONLY with a JSON object (no markdown, no explanation):
{{
  "conf_base": <integer 45-75, suggested confidence threshold>,
  "dead_zone_utc": [<hours to avoid, list of integers 0-23>],
  "prime_long_utc": [<best hours for calls, list of integers>],
  "insight": "<one sentence: what pattern you see in the trades and logs>",
  "action": "<one sentence: the single most important change to make>",
  "log_issue": "<one sentence: any critical error pattern spotted in logs, or null>"
}}"""

            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key":api_key,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"},
                json={"model":"claude-haiku-4-5-20251001","max_tokens":300,
                      "messages":[{"role":"user","content":prompt}]},
                timeout=30)

            if r.status_code != 200:
                log.warning(f"Claude API: {r.status_code}"); return None

            text = r.json()["content"][0]["text"].strip()
            # Strip markdown if present
            text = text.replace("```json","").replace("```","").strip()
            suggestions = json.loads(text)

            log.info(f"Claude insight: {suggestions.get('insight','')}")
            log.info(f"Claude action: {suggestions.get('action','')}")
            return suggestions

        except Exception as e:
            log.warning(f"Claude analysis error: {e}"); return None

    def apply_claude_suggestions(self, suggestions):
        """Apply Claude's suggestions to live config."""
        if not suggestions: return
        changed = []
        if "conf_base" in suggestions:
            new_conf = int(suggestions["conf_base"])
            if 45 <= new_conf <= 78:
                C.CONF_BASE = new_conf
                changed.append(f"conf={new_conf}")
        if "dead_zone_utc" in suggestions:
            new_dz = [int(h) for h in suggestions["dead_zone_utc"] if 0<=int(h)<=23]
            if new_dz:
                C.DEAD_ZONE = new_dz
                changed.append(f"dead={new_dz}")
        if "prime_long_utc" in suggestions:
            new_pl = [int(h) for h in suggestions["prime_long_utc"] if 0<=int(h)<=23]
            if new_pl:
                C.PRIME_LONG = new_pl
                changed.append(f"prime={new_pl}")
        if changed:
            log.info(f"🤖 Claude applied: {', '.join(changed)}")
        log_issue = suggestions.get("log_issue","")
        if log_issue and log_issue != "null":
            log.warning(f"🤖 Claude log issue: {log_issue}")
        log.info(f"🤖 Insight: {suggestions.get('insight','')}")
        log.info(f"🤖 Action:  {suggestions.get('action','')}")
        self.data["last_claude_insight"] = suggestions.get("insight","")
        self.data["last_claude_action"]  = suggestions.get("action","")
        self.data["last_claude_log_issue"] = log_issue
        self.data["last_claude_update"]  = datetime.now(timezone.utc).isoformat()
        self._save()

    def update(self,all_bots):
        with self._lk:
            trades=[{**t,"uid":uid} for uid,b in all_bots.items()
                    for t in b.trades if t.get("won") is not None]
            if len(trades)<3: return
            wins=[t for t in trades if t["won"]]
            wr=len(wins)/len(trades)*100

            # Learn from hours
            by_hour={}
            for t in trades:
                try:
                    h=str(datetime.fromisoformat(t["time"]).hour)
                    by_hour.setdefault(h,{"wins":0,"total":0,"pnl":0})
                    by_hour[h]["total"]+=1
                    by_hour[h]["pnl"]+=float(t.get("pnl",0) or 0)
                    if t["won"]: by_hour[h]["wins"]+=1
                except: pass
            for h in by_hour:
                v=by_hour[h]
                v["wr"]=round(v["wins"]/v["total"]*100,1) if v["total"]>0 else 0

            # Learn from regime conditions
            by_regime={}
            for t in trades:
                regime=t.get("reason","unknown")
                by_regime.setdefault(regime,{"wins":0,"total":0,"pnl":0})
                by_regime[regime]["total"]+=1
                by_regime[regime]["pnl"]+=float(t.get("pnl",0) or 0)
                if t.get("won"): by_regime[regime]["wins"]+=1

            # Learn: which option types performed best
            by_side={}
            for t in trades:
                side=t.get("side","unknown")
                by_side.setdefault(side,{"wins":0,"total":0,"pnl":0})
                by_side[side]["total"]+=1
                by_side[side]["pnl"]+=float(t.get("pnl",0) or 0)
                if t.get("won"): by_side[side]["wins"]+=1

            # Adjust thresholds dynamically based on performance
            # If win rate < 40% → raise confidence threshold
            # If win rate > 60% → we can be slightly more aggressive
            adj_conf = C.CONF_BASE
            if len(trades)>=10:
                if wr < 40: adj_conf = min(C.CONF_BASE+8, 75)  # be more selective
                elif wr > 60: adj_conf = max(C.CONF_BASE-3, 55)  # slightly more active
                if adj_conf != C.CONF_BASE:
                    C.CONF_BASE = adj_conf
                    log.info(f"Intel: WR={wr:.1f}% → confidence threshold adjusted to {adj_conf}")

            # Update dead/prime hours from real data
            good=[int(h) for h,v in by_hour.items() if v["wr"]>=60 and v["total"]>=3 and v["pnl"]>0]
            bad =[int(h) for h,v in by_hour.items() if v["wr"]<=35 and v["total"]>=3 and v["pnl"]<0]

            self.data.update({"win_rate":round(wr,1),"total":len(trades),
                "by_hour":by_hour,"by_regime":by_regime,"by_side":by_side,
                "good_hours":good,"bad_hours":bad,
                "conf_threshold":C.CONF_BASE,
                "updated":datetime.now(timezone.utc).isoformat()})
            if len(trades)>=10:
                if good: C.PRIME_LONG=list(set(C.PRIME_LONG+good))[:12]
                if bad:  C.DEAD_ZONE=list(set(C.DEAD_ZONE+bad))[:10]
                log.info(f"Intel: WR={wr:.1f}% good_hrs={good} bad_hrs={bad} trades={len(trades)}")
            self._save()

    def start(self,bots_ref):
        def loop():
            tick=0
            while True:
                time.sleep(3600)
                tick+=1
                try:
                    self.update(bots_ref)
                except Exception as e:
                    log.warning(f"intel update: {e}")
                # Ask Claude every 6 hours
                if tick % 6 == 0:
                    try:
                        all_trades=[t for b in bots_ref.values()
                                   for t in b.trades if t.get("won") is not None]
                        if all_trades:
                            params={"capital": max((b.capital for b in bots_ref.values()),default=0),
                                    "conf_base":C.CONF_BASE,"dead_zone":C.DEAD_ZONE,
                                    "prime_long":C.PRIME_LONG}
                            suggestions=self.ask_claude(all_trades,params)
                            if suggestions:
                                self.apply_claude_suggestions(suggestions)
                    except Exception as e:
                        log.warning(f"Claude learning: {e}")
        threading.Thread(target=loop,daemon=True).start()

    def summary(self): return dict(self.data)

intel=Intel()

# ═══ DELTA API ════════════════════════════════════════════════════
class DeltaAPI:
    def __init__(self):
        self.key=C.KEY; self.sec=C.SECRET
        self.sess=requests.Session(); self._lk=threading.Lock()

    def set(self,k,s): self.key=k.strip(); self.sec=s.strip()

    def _sign(self,method,path,qs="",body=""):
        with self._lk: ts=str(int(time.time()))
        sig=hmac.new(self.sec.encode(),(method+ts+path+qs+body).encode(),hashlib.sha256).hexdigest()
        return {"api-key":self.key,"timestamp":ts,"signature":sig,"Content-Type":"application/json"}

    def get(self,path,p=None):
        qs=("?"+"&".join(f"{k}={v}" for k,v in p.items())) if p else ""
        try:
            r=self.sess.get(f"{C.BASE}{path}{qs}",headers=self._sign("GET",path,qs),timeout=10)
            return r.json()
        except Exception as e: log.warning(f"GET {path}: {e}"); return None

    def post(self,path,body):
        b=json.dumps(body)
        try:
            r=self.sess.post(f"{C.BASE}{path}",headers=self._sign("POST",path,"",b),data=b,timeout=10)
            return r.json()
        except Exception as e: log.warning(f"POST {path}: {e}"); return {}

    def price(self):
        try:
            res=self.sess.get(f"{C.BASE}/v2/tickers/BTCUSD",timeout=6).json().get("result",{})
            return float(res.get("mark_price",0) or res.get("close",0) or 0)
        except: return 0.0

    def balance(self):
        d=self.get("/v2/wallet/balances")
        if not d: return 0.0,None,"No response"
        if not d.get("success"):
            err=d.get("error",{}); code=err.get("code","") if isinstance(err,dict) else str(err)
            return 0.0,d,f"API error: {code}"
        for b in d.get("result",[]):
            if str(b.get("asset_symbol","")).upper() in ("USD","USDT"):
                av=float(b.get("available_balance",0) or 0)
                bk=float(b.get("blocked_margin",0) or 0)
                if av+bk>0: return round(av+bk,2),d,"ok"
        ne=float((d.get("meta") or {}).get("net_equity",0) or 0)
        if ne>0: return round(ne,2),d,"ok"
        return 0.0,d,"Zero balance"

    def candles(self,res="5m",n=100):
        mins={"1m":1,"5m":5,"15m":15,"1h":60,"4h":240,"1d":1440}.get(res,5)
        end=int(time.time())
        for rf in [res,mins]:
            d=self.get("/v2/history/candles",{"symbol":C.SYMBOL,"resolution":rf,
                "start":end-mins*60*n,"end":end})
            if d and d.get("success") and d.get("result"): return d["result"]
        return []

    def btcusd_pos(self):
        d=self.get("/v2/positions/margined")
        if not d or not d.get("success"): return []
        return [p for p in d.get("result",[])
                if pid_int(p.get("product_id",0))==C.PID and abs(float(p.get("size",0) or 0))>0]

    def opt_pos(self):
        d=self.get("/v2/positions/margined")
        if not d or not d.get("success"): return []
        return [p for p in d.get("result",[])
                if str(p.get("product_symbol","")).startswith(("C-BTC","P-BTC"))
                and float(p.get("size",0) or 0)>0]

    def order(self,side,lots,pid=None):
        return self.post("/v2/orders",{"product_id":pid or C.PID,"size":lots,
            "side":side,"order_type":"market_order","time_in_force":"ioc"})

    def bracket(self,side,lots,stop,tp):
        return self.post("/v2/orders",{"product_id":C.PID,"size":lots,"side":side,
            "order_type":"stop_market_order","stop_price":str(round(stop,1)),
            "bracket_stop_loss_price":str(round(stop,1)),
            "bracket_take_profit_price":str(round(tp,1)),
            "time_in_force":"gtc","stop_trigger_method":"mark_price"})

    def close(self,size,pid=None):
        return self.post("/v2/orders",{"product_id":pid or C.PID,"size":abs(int(size)),
            "side":"sell" if size>0 else "buy","order_type":"market_order","time_in_force":"ioc"})

    def opt_pid(self,symbol):
        prefix="call_options" if symbol.startswith("C-") else "put_options"
        d=self.get("/v2/products",{"contract_type":prefix,"state":"live"})
        if d and d.get("success"):
            for p in d.get("result",[]):
                if p.get("symbol")==symbol: return p.get("id")
        td=self.get(f"/v2/tickers/{symbol}")
        if td and td.get("success"): return td.get("result",{}).get("product_id")
        return None

# ═══ INDICATORS ═══════════════════════════════════════════════════
def _parse(raw):
    out=[]
    for c in raw:
        try:
            v=float(c.get("close",0) or 0)
            if v>0: out.append({"c":v,"h":float(c.get("high",v) or v),
                "l":float(c.get("low",v) or v),"v":float(c.get("volume",0) or 0)})
        except: pass
    return out

def ema(p,n):
    if len(p)<n: return [p[-1]]*len(p) if p else []
    k=2/(n+1); v=[sum(p[:n])/n]
    for x in p[n:]: v.append(x*k+v[-1]*(1-k))
    return [v[0]]*(n-1)+v

def rsi(p,n=9):
    if len(p)<n+2: return 50.0
    d=[p[i]-p[i-1] for i in range(1,len(p))]
    g=sum(max(x,0) for x in d[-n:])/n
    l=sum(abs(min(x,0)) for x in d[-n:])/n
    return round(100 if l<1e-10 else 100-100/(1+g/l),1)

def adx_calc(hi,lo,cl,n=14):
    if len(cl)<n*2+1: return 0.0,0.0,0.0
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
    dx=[abs(pi[i]-ni[i])/(pi[i]+ni[i])*100 if pi[i]+ni[i]>0 else 0 for i in range(len(pi))]
    return round(sum(dx[-n:])/n,1),round(pi[-1],1),round(ni[-1],1)

def atr_val(hi,lo,cl,n=7):
    if len(cl)<n+1: return 0.0
    return sum(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1]))
               for i in range(1,len(cl)))/(len(cl)-1)

def bollinger(cl,n=20):
    if len(cl)<n: m=cl[-1]; return m,m,m,0.0
    w=cl[-n:]; m=sum(w)/n; s=math.sqrt(sum((p-m)**2 for p in w)/n)
    return m+2*s,m,m-2*s,(4*s/m*100) if m>0 else 0.0

def macd_hist(cl,fast=5,slow=13,sig=5):
    if len(cl)<slow+sig: return 0.0,0.0,0.0
    ef=ema(cl,fast); es=ema(cl,slow)
    line=[ef[i]-es[i] for i in range(len(es))]
    signal=ema(line,sig)
    return round(line[-1],4),round(signal[-1],4),round(line[-1]-signal[-1],4)

def divergence(cl,hi,lo,direction,lb=10):
    if len(cl)<lb+5: return False
    _,_,h_now=macd_hist(cl); _,_,h_prev=macd_hist(cl[:-lb])
    if direction=="long":
        return cl[-1]<min(cl[-lb:-1]) and h_now>h_prev
    return cl[-1]>max(cl[-lb:-1]) and h_now<h_prev

def trend_from_candles(candles_list):
    """Returns bull/bear/neutral. No ADX requirement — price position decides."""
    if len(candles_list)<21: return "neutral"
    cl=[c["c"] for c in candles_list]
    hi=[c["h"] for c in candles_list]
    lo=[c["l"] for c in candles_list]
    e8=ema(cl,8)[-1]; e21=ema(cl,21)[-1]
    adx_v,pdi,ndi=adx_calc(hi,lo,cl)
    p=cl[-1]
    # Primary: EMA stack + directional index
    if p>e8>e21 and pdi>ndi: return "bull"
    if p<e8<e21 and ndi>pdi: return "bear"
    # Secondary: just EMA stack (ADX doesn't matter for direction)
    if p>e8 and e8>e21: return "bull"
    if p<e8 and e8<e21: return "bear"
    return "neutral"

# ═══ EXPERT OPTIONS ENGINE ════════════════════════════════════════
class OptionsEngine:
    """
    Thinks like an expert BTC options trader:
    - Strong trend → ITM options (higher delta, moves more with BTC)
    - Weak trend / unclear → ATM options (cheaper, balanced)
    - Coiled market (BB squeeze + low ADX) → Straddle
    - Scans ALL expiries, picks best liquidity
    - Profit floor: 64% of peak from first profit tick
    """
    def __init__(self,api):
        self.api=api; self._peak={}; self._opened={}

    def expiries(self):
        today=date.today()
        return [(today+timedelta(days=i)).strftime("%d%m%y") for i in range(1,46)]

    def atm(self,price,iv=500): return round(price/iv)*iv

    def find(self,opt_type,price,conviction="atm"):
        """
        conviction: 'itm' (strong trend), 'atm' (moderate), 'otm' (cheap lottery)
        """
        prefix="C" if opt_type=="call" else "P"
        atm_strike=self.atm(price)
        if conviction=="itm":
            strikes=[atm_strike-500,atm_strike] if opt_type=="call" else [atm_strike+500,atm_strike]
        elif conviction=="otm":
            strikes=[atm_strike+500,atm_strike] if opt_type=="call" else [atm_strike-500,atm_strike]
        else:
            strikes=[atm_strike,atm_strike+500 if opt_type=="call" else atm_strike-500]

        best=None; best_score=999
        for expiry in self.expiries():
            for strike in strikes:
                sym=f"{prefix}-BTC-{strike}-{expiry}"
                d=self.api.get(f"/v2/tickers/{sym}")
                if not d or not d.get("success"): continue
                res=d.get("result",{})
                mark=float(res.get("mark_price",0) or 0)
                if mark<=0: continue
                bid=float(res.get("best_bid",0) or 0)
                ask=float(res.get("best_ask",0) or 0)
                iv=float(res.get("mark_iv",0) or 0)
                if iv>120 and iv>0: continue
                if bid<=0: continue
                spread=(ask-bid)/mark*100 if mark>0 and ask>bid else 0
                if spread>25: continue
                score=spread+(iv/10 if iv>0 else 0)
                if score<best_score:
                    best_score=score
                    best={"found":True,"symbol":sym,"strike":strike,"expiry":expiry,
                          "type":opt_type,"mark":mark,"bid":bid,"ask":ask,"iv":round(iv,1),
                          "conviction":conviction,"premium_usd":round(mark*C.LOT,3),
                          "spread_pct":round(spread,1)}
                if score<5: break
            if best and best_score<5: break
        return best or {"found":False,"expiry":self.expiries()[0] if self.expiries() else "?"}

    def should_exit(self,sym,cur,entry,opened_at):
        """
        Profit floor: keeps 64% of peak from FIRST profit tick.
        Peak +5% → floor at +3.2% (5 × 0.64)
        Hard stop: -15%. Hard TP: +70%.
        """
        if entry<=0: return {"exit":False,"reason":""}
        pct=(cur-entry)/entry
        now=datetime.now(timezone.utc)
        peak=self._peak.get(sym,entry)
        if cur>peak: self._peak[sym]=cur; peak=cur
        peak_pct=(peak-entry)/entry
        # Expiry protection
        exp=sym[-6:] if len(sym)>=6 else ""
        if exp:
            try:
                exp_dt=datetime.strptime(exp,"%d%m%y").replace(hour=12,minute=0,tzinfo=timezone.utc)
                if now>=exp_dt-timedelta(minutes=C.OPT_EXP):
                    return {"exit":True,"reason":f"expiry soon {int((exp_dt-now).total_seconds()/60)}m","pct":pct}
            except: pass
        # Use dynamic TP/SL from bot mode (passed via _mode_cfg)
        opt_tp   = getattr(self,"_opt_tp",  C.OPT_TP)
        opt_stop = getattr(self,"_opt_stop",C.OPT_STOP)
        if pct>=opt_tp:    return {"exit":True,"reason":f"TP +{pct*100:.1f}%","pct":pct}
        if pct<=-opt_stop: return {"exit":True,"reason":f"SL {pct*100:.1f}%","pct":pct}
        # Profit floor — from first profit tick
        if peak_pct>0:
            lock=peak_pct*C.OPT_LOCK
            if pct<lock:
                return {"exit":True,"reason":f"floor peak+{peak_pct*100:.1f}%→lock+{lock*100:.1f}% now+{pct*100:.1f}%","pct":pct}
        if opened_at and (now-opened_at).seconds<300:
            return {"exit":False,"reason":"min_hold_5m"}
        return {"exit":False,"reason":f"hold {pct*100:.1f}% | lock>{peak_pct*C.OPT_LOCK*100:.1f}%","pct":pct}

    def straddle(self,price):
        c=self.find("call",price,"atm"); p=self.find("put",price,"atm")
        if c.get("found") and p.get("found"):
            total=c["premium_usd"]+p["premium_usd"]
            return {"found":True,"call":c,"put":p,"total":round(total,3),
                    "be_up":c["strike"]+total/C.LOT,"be_down":p["strike"]-total/C.LOT}
        return {"found":False}

    def open(self,sym): self._opened[sym]=datetime.now(timezone.utc); self._peak[sym]=0
    def close(self,sym): self._opened.pop(sym,None); self._peak.pop(sym,None)
    def opened_at(self,sym): return self._opened.get(sym)

    def pos_display(self,positions):
        pos_map={p.get("product_symbol",""):p for p in positions}
        out=[]
        for sym,p in pos_map.items():
            if not sym.startswith(("C-BTC","P-BTC")): continue
            sz=float(p.get("size",0) or 0)
            entry=float(p.get("avg_entry_price") or p.get("entry_price") or 0)
            mark=float(p.get("mark_price") or 0)
            if sz<=0 or entry<=0: continue
            pct=(mark-entry)/entry*100 if entry>0 else 0
            peak=self._peak.get(sym,entry); peak_pct=(peak-entry)/entry*100 if entry>0 else 0
            lock_pct=peak_pct*C.OPT_LOCK; lock_price=entry*(1+lock_pct/100)
            out.append({"sym":sym,"lots":int(sz),"entry":round(entry,4),"mark":round(mark,4),
                "pct":round(pct,1),"peak":round(peak,4),"peak_pct":round(peak_pct,1),
                "type":"CALL" if sym.startswith("C-") else "PUT",
                "floor_price":round(lock_price,2),"floor_pct":round(lock_pct,1),
                "floor_active":peak_pct>0,"sl_price":round(entry*(1-C.OPT_STOP),2),
                "tp_price":round(entry*(1+C.OPT_TP),2),"upnl":round(float(p.get("unrealized_pnl") or 0),3)})
        return out

# ═══ MARKET BRAIN — expert top-down analysis ══════════════════════
def get_market_brain(candles):
    """
    Expert BTC trader top-down analysis:
    1. Daily trend = macro direction
    2. 4H trend = intermediate direction
    3. 1H trend = short-term bias
    4. 15m/5m = entry timing
    5. 1m = exact entry

    Returns a comprehensive market view with trade recommendation.
    """
    brain={"direction":"neutral","conviction":0,"raw_conviction":0,"strategy":"WAIT",
           "opt_type":None,"opt_conviction":"atm","macro_bias":"neutral",
           "trends":{},"adx5m":0,"bw":0,"atr_pct":0,"veto":"",
           "regime":"NEUTRAL","scale":1}

    # ── MARKET CONTEXT (funding + OI) ─────────────────────────────
    mkt=candles.get("market",{})
    funding=float(mkt.get("funding",0) or 0)
    oi_change=float(mkt.get("oi_change",0) or 0)
    brain["funding"]=funding; brain["oi_change"]=oi_change

    # Funding rate interpretation (from real BTC data):
    # > +0.05%: longs very crowded → likely flush down → avoid longs
    # > +0.10%: extreme long crowding → strong put signal
    # < -0.02%: shorts crowded → likely short squeeze → avoid shorts
    # < -0.05%: extreme short crowding → strong call signal
    # -0.01% to +0.03%: neutral → no adjustment
    if funding > 0.10:
        brain["funding_bias"]="strong_bear"   # extreme longs = fade
    elif funding > 0.05:
        brain["funding_bias"]="lean_bear"     # crowded longs = caution
    elif funding < -0.05:
        brain["funding_bias"]="strong_bull"   # extreme shorts = squeeze
    elif funding < -0.02:
        brain["funding_bias"]="lean_bull"     # shorts crowded = lean long
    else:
        brain["funding_bias"]="neutral"

    # OI interpretation:
    # OI rising + price rising = trend confirmed
    # OI falling + price rising = trend weakening (exit soon)
    # OI rising + price falling = shorts piling in = bearish
    brain["oi_trend"]="rising" if oi_change>0.1 else "falling" if oi_change<-0.1 else "flat"

    c5m=candles.get("5m",[]); c1m=candles.get("1m",[]); c15m=candles.get("15m",[])
    c1h=candles.get("1h",[]); c4h=candles.get("4h",[]); c1d=candles.get("1d",[])
    if len(c5m)<30: brain["veto"]="insufficient_data"; return brain

    # Trend at each timeframe
    trends={}
    for tf,data in [("1m",c1m),("5m",c5m),("15m",c15m),("1h",c1h),("4h",c4h),("1d",c1d)]:
        trends[tf]=trend_from_candles(data)
    brain["trends"]=trends

    # Macro vote: Daily + 4H + 1H
    macro_votes=[trends.get(t,"neutral") for t in ["1d","4h","1h"]]
    bull_votes=macro_votes.count("bull"); bear_votes=macro_votes.count("bear")
    macro_bias="bull" if bull_votes>=2 else "bear" if bear_votes>=2 else "neutral"
    brain["macro_bias"]=macro_bias

    # 5m technical analysis
    cl=[c["c"] for c in c5m]; hi=[c["h"] for c in c5m]
    lo=[c["l"] for c in c5m]; vo=[c["v"] for c in c5m]
    price=cl[-1]
    adx_v,pdi,ndi=adx_calc(hi,lo,cl)
    _,_,_,bw=bollinger(cl)
    atr_pct=atr_val(hi,lo,cl)/price*100 if price>0 else 0
    r5=rsi(cl); _,_,hist=macd_hist(cl)
    div_long=divergence(cl,hi,lo,"long"); div_short=divergence(cl,hi,lo,"short")
    e8=ema(cl,8)[-1]; e21=ema(cl,21)[-1]; e55=ema(cl,55)[-1] if len(cl)>=55 else cl[0]

    brain.update({"adx5m":round(adx_v,1),"bw":round(bw,2),"atr_pct":round(atr_pct,3),
                  "rsi":r5,"hist":hist})

    # 5m regime
    if price>e8>e21>e55 and adx_v>25 and pdi>ndi: regime="STRONG_BULL"
    elif price>e8>e21 and adx_v>18 and pdi>ndi:   regime="BULL"
    elif price<e8<e21<e55 and adx_v>25 and ndi>pdi: regime="STRONG_BEAR"
    elif price<e8<e21 and adx_v>18 and ndi>pdi:   regime="BEAR"
    elif adx_v<15:                                  regime="SIDEWAYS"
    else:                                           regime="NEUTRAL"
    brain["regime"]=regime

    hour=datetime.now(timezone.utc).hour
    # Session quality modifier (not a hard veto — just reduces conviction)
    # Real filtering happens via entry quality (RSI, BB position)
    session_penalty = 0
    if hour in C.DEAD_ZONE: session_penalty = 15  # reduce conviction, don't block

    # ── TIMEFRAME ALIGNMENT COUNT ─────────────────────────────────
    all_trends=[trends.get(t,"neutral") for t in ["1m","5m","15m","1h","4h","1d"]]
    tf_bull_count=all_trends.count("bull")
    tf_bear_count=all_trends.count("bear")

    # ── STRADDLE DECISION ──────────────────────────────────────────
    # Only straddle when macro is NEUTRAL (no clear direction)
    # If macro is clear → go directional even in squeeze
    squeeze=bw<1.2 and adx_v<20
    tf_aligned=max(tf_bull_count,tf_bear_count)>=4  # 4+ TFs agree
    if squeeze and macro_bias=="neutral" and not tf_aligned:
        brain["strategy"]="STRADDLE"; brain["conviction"]=55
        brain["raw_conviction"]=55
        brain["opt_type"]="straddle"; brain["opt_conviction"]="atm"
        return brain
    elif squeeze and macro_bias!="neutral":
        # Squeeze + clear direction = explosive directional move
        brain["squeeze_breakout"]=True  # boost conviction later

    # ── ENTRY QUALITY CHECK (from real trade data) ─────────────────
    # Real data shows: buying calls at RSI>65 = consistent losses
    # Buying calls at RSI<45 (dip in bull trend) = consistent wins
    # Same for puts: buy puts at RSI>55 (spike in bear) not RSI<35
    e21_5m = ema(cl,21)[-1]; e55_5m = ema(cl,55)[-1] if len(cl)>=55 else cl[0]
    upper_bb,mid_bb,lower_bb,_ = bollinger(cl)
    price_bb_pos = (price - lower_bb)/(upper_bb - lower_bb) if upper_bb>lower_bb else 0.5

    # ── DIRECTIONAL DECISION with entry quality ────────────────────
    if macro_bias=="bull":
        direction="long"
        # Entry quality: only buy calls when price is NOT at top
        # Ideal: RSI<55 (pullback) AND price near/below EMA21
        at_good_entry = r5 < 58 and price <= e21_5m * 1.005  # within 0.5% of EMA21
        at_dip        = r5 < 50 and r5 >= 35 and price_bb_pos < 0.4  # real dip, NOT a knife-catch (r5>=35)
        at_breakout   = r5 > 55 and price > e21_5m and hist > 0 and adx_v > 20  # real breakout

        if bull_votes==3 and at_dip:
            conviction=88; opt_conviction="itm"   # best entry: macro+dip = max size
        elif bull_votes==3 and at_breakout:
            conviction=82; opt_conviction="itm"   # strong breakout with all macro
        elif bull_votes>=2 and div_long:
            conviction=80; opt_conviction="itm"   # divergence = reversal signal
        elif bull_votes>=2 and at_dip:
            conviction=72; opt_conviction="atm"   # good entry on macro dip
        elif bull_votes>=2 and at_good_entry:
            conviction=65; opt_conviction="atm"   # macro ok, entry decent
        elif bull_votes>=2:
            conviction=45; opt_conviction="atm"   # macro but bad entry timing
        else:
            conviction=38; opt_conviction="atm"   # weak — likely veto
    elif macro_bias=="bear":
        direction="short"
        # For puts: buy when price is at top of range (RSI>55)
        at_good_entry = r5 > 42 and price >= e21_5m * 0.995
        at_spike      = r5 > 62 and price_bb_pos > 0.65 and hist < 0
        at_breakdown  = r5 < 42 and price < e21_5m and hist < 0 and adx_v > 22
        funding_ok    = funding < 0.05

        if bear_votes==3 and at_spike and funding_ok:
            conviction=88; opt_conviction="itm"
        elif bear_votes==3 and at_breakdown:
            conviction=82; opt_conviction="itm"
        elif bear_votes>=2 and div_short and funding_ok:
            conviction=78; opt_conviction="atm"
        elif bear_votes>=2 and at_spike and funding_ok:
            conviction=70; opt_conviction="atm"
        elif bear_votes>=2 and at_breakdown:
            conviction=65; opt_conviction="atm"
        elif bear_votes>=2 and funding_ok:
            conviction=42; opt_conviction="atm"
        else:
            conviction=30; opt_conviction="atm"
    else:
        # No macro bias — only trade very clear 5m signals
        # VETO: RSI<35 in any non-macro-bull context = downtrend trap (autopsy fix)
        if regime in ("STRONG_BULL","BULL") and adx_v>25 and r5<52:
            if r5 < 35:  # RSI oversold in no-macro-direction = "buy the knife" trap
                brain["veto"]="RSI<35_in_downtrend_trap"; return brain
            direction="long"; conviction=62; opt_conviction="atm"
        elif regime in ("STRONG_BEAR","BEAR") and adx_v>25 and r5>48:
            if r5 > 72:  # RSI overbought in no-macro-direction = "sell the spike" trap
                brain["veto"]="RSI>72_in_uptrend_trap"; return brain
            direction="short"; conviction=62; opt_conviction="atm"
        elif div_long and adx_v>18:
            direction="long"; conviction=60; opt_conviction="atm"
        elif div_short and adx_v>18:
            direction="short"; conviction=60; opt_conviction="atm"
        elif squeeze:
            brain["strategy"]="STRADDLE"; brain["conviction"]=55
            brain["opt_type"]="straddle"; brain["opt_conviction"]="atm"
            return brain
        else:
            brain["veto"]="no_clear_direction"; return brain

    # Apply session penalty
    conviction = max(0, conviction - session_penalty)

    # ── FUNDING RATE VETO ─────────────────────────────────────────
    # This is the single most powerful filter from real BTC data
    # ── TF ALIGNMENT BOOST ────────────────────────────────────────
    # 5/6 or 6/6 TFs aligned = strong trend regardless of ADX
    if tf_bull_count>=5 and direction=="long":
        boost=15 if tf_bull_count==6 else 10
        conviction=min(100,conviction+boost)
    elif tf_bear_count>=5 and direction=="short":
        boost=15 if tf_bear_count==6 else 10
        conviction=min(100,conviction+boost)

    # Confidence threshold — defined before funding veto uses it
    conf_needed=C.CONF_MACRO if macro_bias!="neutral" else C.CONF_BASE

    funding_bias=brain.get("funding_bias","neutral")
    if direction=="long" and funding_bias=="strong_bear":
        conviction=max(0,conviction-25)
        if conviction<conf_needed: brain["veto"]="funding_crowded_longs"
    elif direction=="long" and funding_bias=="lean_bear":
        conviction=max(0,conviction-12)
    elif direction=="short" and funding_bias=="strong_bull":
        conviction=max(0,conviction-25)
        if conviction<conf_needed: brain["veto"]="funding_crowded_shorts"
    elif direction=="short" and funding_bias=="lean_bull":
        conviction=max(0,conviction-12)
    elif direction=="long" and funding_bias in ("lean_bull","strong_bull"):
        conviction=min(100,conviction+8)
    elif direction=="short" and funding_bias in ("lean_bear","strong_bear"):
        conviction=min(100,conviction+8)

    # OI confirmation boost
    oi_trend=brain.get("oi_trend","flat")
    if direction=="long" and oi_trend=="rising": conviction=min(100,conviction+5)
    if direction=="short" and oi_trend=="rising" and macro_bias=="bear": conviction=min(100,conviction+5)
    if (direction=="long" and oi_trend=="falling") or        (direction=="short" and oi_trend=="falling" and macro_bias=="bull"):
        conviction=max(0,conviction-5)  # OI falling = trend weakening

    brain["entry_quality"]={
        "rsi":r5,"bb_pos":round(price_bb_pos,2),
        "vs_ema21":round((price-e21_5m)/e21_5m*100,2),
        "adx":adx_v,"hist":round(hist,4),
        "funding":funding,"oi_change":oi_change,
        "funding_bias":funding_bias,"oi_trend":oi_trend
    }

    # RSI trap vetoes
    if direction=="long" and r5<32 and macro_bias!="bull":
        brain["veto"]="rsi_trap_long"; return brain
    if direction=="short" and r5>68 and macro_bias!="bear":
        brain["veto"]="rsi_trap_short"; return brain
    if direction=="long" and r5>80:
        brain["veto"]=f"rsi_overbought_{r5:.0f}"; return brain
    if direction=="short" and r5<20:
        brain["veto"]=f"rsi_oversold_{r5:.0f}"; return brain

    # Final threshold check
    if conviction<conf_needed:
        brain["veto"]=f"entry_conv={conviction}_need={conf_needed}"; return brain

    # Lot scaling: 1x→2x→3x based on conviction
    if conviction>=80: scale=3
    elif conviction>=65: scale=2
    else: scale=1

    # Strategy
    strategy="SWING" if conviction>=70 else "SCALP"

    # ── OPTIONS SCORING (separate from perp conviction) ─────────────
    opt_score = 0
    opt_pillars = {}

    # Pillar 1: Trend direction (40pts)
    if direction=="long" and macro_bias=="bull":
        ts = min(40, int(bull_votes/3*40))
    elif direction=="short" and macro_bias=="bear":
        ts = min(40, int(bear_votes/3*40))
    else:
        ts = 0
    opt_pillars["Trend"] = {"s":ts,"m":40,"color":"#3b82f6"}
    opt_score += ts

    # Pillar 2: Entry quality (25pts)
    # at_pullback = RSI<58 near EMA (defined in bull section only)
    _at_pullback = r5<58 and price<=e21_5m*1.008
    _at_spike    = r5>62 and price_bb_pos>0.65
    if direction=="long":
        mom = 25 if at_dip else (20 if _at_pullback else (15 if at_breakout else 5))
    else:
        _fok = funding<0.05
        mom = 25 if (_fok and _at_spike) else (15 if (r5<42 and price<e21_5m) else 5)
    opt_pillars["Entry Quality"] = {"s":mom,"m":25,"color":"#f59e0b"}
    opt_score += mom

    # Pillar 3: Volatility (BB squeeze = good for options) (15pts)
    bw_score = 15 if bw<0.5 else (10 if bw<1.0 else (5 if bw<2.0 else 2))
    opt_pillars["Volatility"] = {"s":bw_score,"m":15,"color":"#ec4899"}
    opt_score += bw_score

    # Pillar 4: Funding (10pts) — negative funding = good for calls
    if direction=="long":
        fund_score = 10 if funding<-0.01 else (7 if funding<0.02 else (4 if funding<0.05 else 0))
    else:
        fund_score = 10 if funding>0.05 else (7 if funding>0.02 else (4 if funding>-0.01 else 0))
    opt_pillars["Funding"] = {"s":fund_score,"m":10,"color":"#14b8a6"}
    opt_score += fund_score

    # Pillar 5: Divergence bonus (10pts)
    div_score = 10 if (div_long and direction=="long") or (div_short and direction=="short") else 0
    opt_pillars["Divergence"] = {"s":div_score,"m":10,"color":"#8b5cf6"}
    opt_score += div_score

    brain.update({"direction":direction,"conviction":conviction,
                  "raw_conviction":conviction,  # always store real value
                  "strategy":strategy,
                  "opt_type":"call" if direction=="long" else "put",
                  "opt_conviction":opt_conviction,"scale":scale,
                  "tf_bull":tf_bull_count,"tf_bear":tf_bear_count,
                  "tf_total":len(all_trends),
                  "rsi":r5,
                  "div":div_long if direction=="long" else div_short,
                  "opt_score":opt_score,
                  "opt_pillars":opt_pillars,
                  "at_dip":at_dip if direction=="long" else False,
                  "at_breakout":at_breakout if direction=="long" else False})
    return brain

# ═══ BOT ══════════════════════════════════════════════════════════
class Bot:
    def __init__(self):
        self.api=DeltaAPI(); self.opts=None
        self._sf=os.path.join(_DATA,"bot_default.json")
        self.running=False; self.connected=False; self.opts_mode=False
        self._start_time=None  # track when bot started
        self.capital=0.0; self.start_cap=0.0; self.day_start=0.0
        self.halted=False; self.halt_msg=""
        self.status="Not connected"; self.logs=[]; self.trades=[]
        self.scan_n=0; self.next_scan=None; self.price=0.0
        self.brain={}; self.total_tr=0; self.wins=0
        self._stops=set(); self._last_close=None; self._consec=0
        self._circuit=None; self._opened={}
        self.lot_size=10; self.max_daily=10
        self._daily_n=0; self._daily_date=""
        self.mode="normal"  # safe / normal / pro

    def emit(self,level,msg):
        e={"t":datetime.now(timezone.utc).strftime("%H:%M:%S"),"l":level,"m":msg}
        self.logs.append(e)
        if len(self.logs)>500: self.logs.pop(0)
        getattr(log,{"INFO":"info","WARN":"warning","ERROR":"error","TRADE":"info"}.get(level,"info"))(msg)

    @property
    def active_mode(self):
        """Returns effective mode — PRO locked below $500."""
        m = self.mode
        if m == "pro" and self.capital < C.MODES["pro"]["min_bal"]:
            return "normal"  # auto-downgrade
        return m

    @property
    def mode_cfg(self):
        return C.MODES[self.active_mode]

    def save(self):
        try:
            peak={}
            if self.opts: peak={k:v for k,v in self.opts._peak.items()}
            state={"start_cap":self.start_cap,"day_start":self.day_start,
                "halted":self.halted,"halt_msg":self.halt_msg,
                "total_tr":self.total_tr,"wins":self.wins,
                "trades":self.trades[-500:],"stops":list(self._stops),
                "consec":self._consec,
                "circuit":self._circuit.isoformat() if self._circuit else None,
                "last_close":self._last_close.isoformat() if self._last_close else None,
                "peak":peak,"lot_size":self.lot_size,"max_daily":self.max_daily,
                "mode":self.mode}
            json.dump(state,open(self._sf,"w"))
            # Append closed trades to permanent tradelog
            trade_log=self._sf.replace("bot_","tradelog_")
            try:
                existing=[]
                try: existing=json.load(open(trade_log))
                except: pass
                existing_times={t.get("time","") for t in existing}
                new_closed=[t for t in self.trades
                    if t.get("exit") is not None and t.get("time","") not in existing_times]
                if new_closed:
                    existing.extend(new_closed)
                    json.dump(existing[-2000:],open(trade_log,"w"))
            except Exception as te: log.warning(f"tradelog: {te}")
        except Exception as e: log.warning(f"save: {e}")

    def load(self):
        try:
            if not os.path.exists(self._sf): return False
            s=json.load(open(self._sf))
            self.start_cap=float(s.get("start_cap",0))
            self.day_start=float(s.get("day_start",0))
            self.halted=bool(s.get("halted",False))
            self.halt_msg=s.get("halt_msg","")
            self.total_tr=int(s.get("total_tr",0))
            self.wins=int(s.get("wins",0))
            self.trades=s.get("trades",[])
            # Restore full trade history from tradelog
            trade_log=self._sf.replace("bot_","tradelog_")
            try:
                all_trades=json.load(open(trade_log))
                # Merge: keep open trades from state, add closed from log
                open_trades=[t for t in self.trades if t.get("exit") is None]
                closed_log=[t for t in all_trades if t.get("exit") is not None]
                self.trades=closed_log[-200:]+open_trades
            except: pass
            self._stops=set(s.get("stops",[]))
            self._consec=int(s.get("consec",0))
            self.lot_size=int(s.get("lot_size",10))
            self.max_daily=int(s.get("max_daily",10))
            self.mode=s.get("mode","normal")
            cu=s.get("circuit"); self._circuit=datetime.fromisoformat(cu) if cu else None
            lc=s.get("last_close"); self._last_close=datetime.fromisoformat(lc) if lc else None
            if self.opts:
                for sym,pk in s.get("peak",{}).items(): self.opts._peak[sym]=float(pk)
            return self.start_cap>0
        except: return False

    def connect(self,key,secret):
        self.api.set(key,secret)
        bal,_,err=self.api.balance()
        if bal<=0:
            srv="?"
            try: srv=requests.get("https://api.ipify.org?format=json",timeout=4).json().get("ip","?")
            except: pass
            return {"success":False,"message":err,"server_ip":srv}
        self.capital=bal; self.connected=True
        self.opts=OptionsEngine(self.api)
        if not self.load() or self.start_cap<=0:
            self.start_cap=bal; self.day_start=bal; self.save()
        else:
            try:
                s=json.load(open(self._sf))
                for sym,pk in s.get("peak",{}).items(): self.opts._peak[sym]=float(pk)
            except: pass
        self.emit("INFO",f"Connected ${bal:.2f} | Start ${self.start_cap:.2f}")
        self._sync_pos()
        self._reconcile()
        self._start_time=datetime.now(timezone.utc)  # record connect time
        if not self.running: self.start()
        return {"success":True,"balance":bal}

    def _reconcile(self):
        """
        Sync bot trades with Delta positions.
        When a position is missing (manually closed), try to get real P&L
        from trade history API. Fall back to current price if unavailable.
        """
        if not self.connected: return
        real_syms=set()
        for p in self.api.btcusd_pos(): real_syms.add(str(pid_int(p.get("product_id",0))))
        for p in self.api.opt_pos(): real_syms.add(p.get("product_symbol",""))
        closed=False
        for t in self.trades:
            if t.get("exit") is not None: continue
            sym=str(t.get("sym","")); pid=str(t.get("pid",""))
            if sym not in real_syms and pid not in real_syms:
                # Try to get real exit price from current market price
                entry=float(t.get("entry",0) or 0)
                exit_price=self.price or entry
                pnl=0.0; won=False
                if entry>0 and exit_price>0:
                    side=t.get("side","long")
                    if side in ("long","call"):
                        pnl=round((exit_price-entry)*float(t.get("lots",1))*C.LOT,4)
                    elif side in ("short","put"):
                        pnl=round((entry-exit_price)*float(t.get("lots",1))*C.LOT,4)
                    won=pnl>0
                t["exit"]=round(exit_price,2); t["pnl"]=pnl
                t["won"]=won
                # Label it as manually closed not ghost
                t["reason"]="manual_close"
                if won: self.wins+=1
                self.emit("INFO",f"Manual close detected: {sym or pid} @ ${exit_price:.0f} P&L ${pnl:+.4f}")
                if self.opts: self.opts.close(sym)
                closed=True
        if closed: self.save()

    def _sync_wallet(self):
        bal,_,err=self.api.balance()
        if bal<=0: self.emit("WARN",f"Wallet: {err}"); return
        self.capital=bal
        if self.start_cap>0 and not self.halted:
            loss=(self.start_cap-bal)/self.start_cap
            if loss>=C.HALT:
                self.halted=True; self.halt_msg=f"Down {loss*100:.1f}%"
                self.emit("ERROR",f"HALTED: {self.halt_msg}"); self.save()
        self.emit("INFO",f"Wallet ${bal:.2f} | {'HALTED' if self.halted else 'OK'}")

    def _sync_pos(self):
        for p in self.api.btcusd_pos():
            sz=float(p.get("size",0) or 0)
            entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            pid=pid_int(p.get("product_id",C.PID))
            side="long" if sz>0 else "short"; lots=abs(int(sz))
            if not any(pid_int(t.get("pid",0))==pid and t.get("exit") is None for t in self.trades):
                now=datetime.now(timezone.utc)
                self.trades.append({"time":now.isoformat(),"side":side,
                    "entry":round(entry,1),"exit":None,"lots":lots,"pnl":None,
                    "pct":None,"reason":"synced","won":None,"pid":pid,"sym":C.SYMBOL})
                self._opened[pid]=now
            if pid not in self._stops and entry>0:
                sp=entry*(1-C.STOP if side=="long" else 1+C.STOP)
                tp=entry*(1+C.TP if side=="long" else 1-C.TP)
                r=self.api.bracket("sell" if side=="long" else "buy",lots,sp,tp)
                if r.get("success"): self._stops.add(pid); self.save()

    def _check_perp_exits(self,positions):
        if not self.price: return
        for p in positions:
            sz=float(p.get("size",0) or 0)
            entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            side="long" if sz>0 else "short"
            pct=(self.price-entry)/entry if side=="long" else (entry-self.price)/entry
            lots=abs(int(sz)); pid=pid_int(p.get("product_id",C.PID))
            now=datetime.now(timezone.utc); opened=self._opened.get(pid)
            hold=(now-opened).seconds//60 if opened else C.MIN_HOLD+1
            if hold<C.MIN_HOLD: continue
            if pct<=-C.STOP: reason="stop"
            elif pct>=C.TP:  reason="tp"
            else: continue
            r=self.api.close(sz,pid)
            if r.get("success"):
                pnl=round(entry*lots*C.LOT*pct,4); won=pct>0
                self.emit("TRADE",f"{'✅' if won else '❌'} {side.upper()} ${entry:.0f}→${self.price:.0f} ${pnl:+.4f}")
                self._on_close(won,pnl,entry,self.price,lots,reason)

    def _check_opt_exits(self):
        if not self.opts: return
        # Apply mode TP/SL to options engine
        cfg=self.mode_cfg
        self.opts._opt_tp   = cfg["opt_tp"]
        self.opts._opt_stop = cfg["opt_stop"]
        positions=self.api.opt_pos()
        pos_map={p.get("product_symbol",""):p for p in positions}
        for p in positions:
            sym=p.get("product_symbol",""); pid=p.get("product_id")
            size=float(p.get("size",0) or 0)
            entry=float(p.get("avg_entry_price") or p.get("entry_price") or 0)
            mark=float(p.get("mark_price") or 0)
            if size<=0 or entry<=0 or mark<=0 or not pid: continue
            chk=self.opts.should_exit(sym,mark,entry,self.opts.opened_at(sym))
            if chk["exit"]:
                r=self.api.close(size,pid)
                if r.get("success"):
                    pct=chk.get("pct",0); pnl=round((mark-entry)*int(size)*C.LOT,4); won=pnl>0
                    self.emit("TRADE",f"{'✅' if won else '❌'} OPT {chk['reason']} {sym} ${pnl:+.4f}")
                    self.opts.close(sym); self._on_close(won,pnl,entry,mark,int(size),chk["reason"])
        # Emergency hard stop on any option
        for t in self.trades:
            if t.get("exit") is not None: continue
            sym=t.get("sym","")
            if not sym.startswith(("C-BTC","P-BTC")): continue
            entry=float(t.get("entry",0) or 0)
            if entry<=0 or sym not in pos_map: continue
            mark=float(pos_map[sym].get("mark_price",0) or 0)
            if mark>0 and (mark-entry)/entry<=-C.OPT_STOP:
                pid=pos_map[sym].get("product_id")
                size=float(pos_map[sym].get("size",1) or 1)
                if pid:
                    r=self.api.close(size,pid)
                    if r.get("success"):
                        pnl=round((mark-entry)*size*C.LOT,4)
                        self.emit("TRADE",f"❌ HARD STOP {sym} ${pnl:+.4f}")
                        self.opts.close(sym); self._on_close(False,pnl,entry,mark,int(size),"hard_stop")

    def _on_close(self,won,pnl,entry,exit_p,lots,reason):
        now=datetime.now(timezone.utc); self._last_close=now
        self._last_was_win=won  # track for cooldown
        if won:
            self._consec=0; self.wins+=1
            self.emit("INFO",f"✅ WIN — confidence good")
        else:
            self._consec+=1
            self.emit("WARN",f"❌ Loss #{self._consec} | cooling {C.LOSS_COOLDOWN_MINS}m")
            if self._consec>=C.CIRC_N:
                self._circuit=now+timedelta(minutes=C.CIRC_MIN)
                self.emit("WARN",f"⚠️ CIRCUIT: {C.CIRC_N} consecutive losses → {C.CIRC_MIN}min pause")
        for t in reversed(self.trades):
            if t.get("exit") is None and t.get("entry")==round(entry,1):
                t.update({"exit":round(exit_p,1),"pnl":pnl,"won":won,"reason":reason}); break
        self.save()

    def _get_candles(self):
        def bnc(iv,n=100):
            try:
                r=requests.get("https://api.binance.com/api/v3/klines",
                    params={"symbol":"BTCUSDT","interval":iv,"limit":n},timeout=8)
                if r.status_code!=200: return []
                return [{"c":float(c[4]),"h":float(c[2]),"l":float(c[3]),"v":float(c[5])} for c in r.json()]
            except: return []
        d5m=_parse(self.api.candles("5m")); b5m=bnc("5m")
        d1m=_parse(self.api.candles("1m")); b1m=bnc("1m")
        d15m=_parse(self.api.candles("15m",60))
        b1h=bnc("1h",72); b4h=bnc("4h",42); b1d=bnc("1d",30)
        c5m=d5m if len(d5m)>=55 else b5m
        c1m=d1m if len(d1m)>=20 else b1m
        bnc_lead="neutral"
        if len(b1m)>=16 and len(d1m)>=16:
            diff=rsi([c["c"] for c in b1m])-rsi([c["c"] for c in d1m])
            if diff>8:   bnc_lead="binance_leading_bull"
            elif diff<-8: bnc_lead="binance_leading_bear"
        # ── COINGLASS: Funding rate + OI + Liquidations ──────────────
        market_data={"funding":0.0,"oi_change":0.0,"liq_long":0.0,"liq_short":0.0,"source":"none"}
        try:
            # Binance funding rate (free, no API key needed)
            fr=requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol":"BTCUSDT"},timeout=5).json()
            funding=float(fr.get("lastFundingRate",0) or 0)*100  # as percentage
            market_data["funding"]=round(funding,4)
            market_data["source"]="binance"

            # Binance OI (open interest trend)
            oi=requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol":"BTCUSDT","period":"5m","limit":3},timeout=5).json()
            if isinstance(oi,list) and len(oi)>=2:
                oi_now=float(oi[-1].get("sumOpenInterest",0))
                oi_prev=float(oi[0].get("sumOpenInterest",1))
                market_data["oi_change"]=round((oi_now-oi_prev)/oi_prev*100,3)

            log.info(f"Market: funding={funding:+.4f}% OI_change={market_data['oi_change']:+.3f}%")
        except Exception as e:
            log.warning(f"Market data fetch: {e}")

        return {"5m":c5m,"1m":c1m,"15m":d15m,"1h":b1h,"4h":b4h,"1d":b1d,
                "binance_lead":bnc_lead,"market":market_data}

    def _pos_disp(self,positions=None):
        if positions is None: positions=self.api.btcusd_pos()
        out=[]
        for p in positions:
            sz=float(p.get("size",0) or 0)
            entry=float(p.get("entry_price") or p.get("avg_entry_price") or 0)
            if sz==0 or entry==0: continue
            mark=float(p.get("mark_price") or self.price or entry)
            upnl=float(p.get("unrealized_pnl") or 0)
            side="long" if sz>0 else "short"
            pct=((mark-entry)/entry if side=="long" else (entry-mark)/entry)*100
            out.append({"sym":C.SYMBOL,"side":side,"lots":abs(sz),"entry":round(entry,1),
                "mark":round(mark,1),"upnl":round(upnl,3),"pct":round(pct,2),
                "stop":round(entry*(1-C.STOP if side=="long" else 1+C.STOP),1),
                "tp":  round(entry*(1+C.TP if side=="long" else 1-C.TP),1)})
        return out

    def scan(self):
        if getattr(self,'_scanning',False): return  # prevent concurrent scans
        self._scanning=True
        try:
            self._do_scan()
        finally:
            self._scanning=False

    def _do_scan(self):
        self.scan_n+=1
        self.next_scan=(datetime.now(timezone.utc)+timedelta(seconds=C.SCAN)).isoformat()
        live=self.api.price()
        if live>0: self.price=live
        if self.scan_n%5==0: self._sync_wallet()
        if self.halted: self.status=f"HALTED: {self.halt_msg}"; return

        candles=self._get_candles()
        if len(candles.get("5m",[]))<30: self.status="Fetching data…"; return

        live2=self.api.price()
        if live2>0: self.price=live2

        real=self.api.btcusd_pos()
        self._reconcile()
        self._check_perp_exits(real)
        self._check_opt_exits()
        self._sync_pos()

        brain=get_market_brain(candles)
        self.brain=brain

        direction=brain["direction"]; strategy=brain["strategy"]
        conviction=brain.get("raw_conviction",brain["conviction"])
        veto=brain["veto"]; macro_bias=brain["macro_bias"]; scale=brain["scale"]
        macro_confirms=(macro_bias=="bull" and direction=="long") or                        (macro_bias=="bear" and direction=="short")

        raw_conv=brain.get("raw_conviction",conviction)
        eq=brain.get("entry_quality",{})
        tf_b=brain.get("tf_bull",0); tf_br=brain.get("tf_bear",0)
        self.emit("INFO",
            f"#{self.scan_n} ${self.price:,.0f} | {brain['regime']} | "
            f"macro={macro_bias} | conv={raw_conv} | {strategy} | "
            f"adx={brain['adx5m']} bw={brain['bw']} "
            f"rsi={brain.get('rsi',0):.0f} "
            f"TF={tf_b}B/{tf_br}S "
            f"fund={eq.get('funding',0):+.3f}% "
            f"{'⚡DIV ' if brain.get('div') else ''}"
            f"{'✗'+veto if veto else '✓TRADE'}")

        now=datetime.now(timezone.utc)
        # ── PRE-TRADE CHECKLIST (disciplined, not emotional) ─────────
        # Check 1: Circuit breaker (3 consecutive losses)
        if self._circuit and now<self._circuit:
            mins=int((self._circuit-now).seconds/60)
            self.status=f"⚠️ Circuit breaker: {mins}m remaining (3 losses)"; return
        elif self._circuit and now>=self._circuit:
            self._circuit=None; self._consec=0
            self.emit("INFO","✅ Circuit breaker lifted — resuming")

        # Check 2: Loss cooldown (step away after loss)
        if self._last_close and not getattr(self,'_last_was_win',True):
            elapsed=(now-self._last_close).seconds//60
            if elapsed<C.LOSS_COOLDOWN_MINS:
                self.status=f"⏸ Post-loss cooldown: {C.LOSS_COOLDOWN_MINS-elapsed}m remaining"
                return

        # Check 3: Daily loss limit (3% = shut down for the day)
        today=now.strftime("%Y-%m-%d")
        if self._daily_date!=today:
            self._daily_date=today; self._daily_n=0
            self.day_start=self.capital  # reset daily start
        daily_pnl_pct=(self.capital-self.day_start)/self.day_start*100 if self.day_start>0 else 0
        if daily_pnl_pct<=-C.DAILY_LOSS_LIMIT*100:
            self.status=f"🛑 Daily loss limit hit ({daily_pnl_pct:.1f}%) — shut down for today"
            return

        # Check 4: Weekly loss limit (8% = full halt)
        if self.start_cap>0:
            total_pnl_pct=(self.capital-self.start_cap)/self.start_cap*100
            if total_pnl_pct<=-C.WEEKLY_LOSS_LIMIT*100:
                self.halted=True
                self.halt_msg=f"Down {abs(total_pnl_pct):.1f}% from start — manual review required"
                self.emit("ERROR",f"🛑 WEEKLY HALT: {self.halt_msg}"); self.save(); return

        if self._daily_n>=self.max_daily:
            self.status=f"Daily trade limit ({self.max_daily})"; return

        if veto:
            self.status=f"Waiting: {veto} | macro={macro_bias}"; return

        # Startup guard: wait 60s before first trade (let data stabilize)
        if self._start_time:
            secs_since_start=(datetime.now(timezone.utc)-self._start_time).total_seconds()
            if secs_since_start<5:
                self.status=f"Startup: warming up {int(5-secs_since_start)}s"
                return

        # PRO mode: only trade when ALL 3 timeframes aligned
        if self.active_mode=="pro":
            trends=brain.get("trends",{})
            bull_all=all(trends.get(t,"neutral")=="bull" for t in ["1d","4h","1h"])
            bear_all=all(trends.get(t,"neutral")=="bear" for t in ["1d","4h","1h"])
            if direction not in ("neutral","wait") and not (bull_all or bear_all):
                self.status=f"PRO: waiting 3/3 alignment (now {macro_bias} {sum(1 for t in ['1d','4h','1h'] if brain.get('trends',{}).get(t)==macro_bias)}/3)"; return

        # Existing positions
        if len(real)>=1:
            d=self._pos_disp(real); x=d[0] if d else {}
            upnl=x.get('upnl',0); pct=x.get('pct',0)
            self.status=f"Holding {x.get('side','').upper()} @ ${x.get('entry',0):,.0f} | UPL ${upnl:+.3f} ({pct:+.2f}%)"
            # Check option exits always
            if self.opts_mode and self.opts:
                self._check_opt_exits()
            # HIGH CONVICTION: also open option to amplify the move
            # conv>=80 = strong enough to run both perp + option
            if self.opts_mode and self.opts and conviction>=80 and not veto:
                opt_pos=self.api.opt_pos()
                local_open=[t for t in self.trades if t.get("exit") is None
                    and t.get("side") in ("call","put","straddle")]
                if not opt_pos and not local_open:
                    # Open supporting option alongside existing perp
                    opt_type="call" if direction=="long" else "put"
                    opt=self.opts.find(opt_type,self.price,brain.get("opt_conviction","atm"))
                    if opt.get("found") and opt["premium_usd"]<=self.capital*C.OPT_MAX:
                        pid=self.api.opt_pid(opt["symbol"])
                        if pid:
                            r=self.api.order("buy",1,pid)
                            if r.get("success"):
                                self.opts.open(opt["symbol"])
                                self.emit("TRADE",
                                    f"⚡ COMBO: {opt_type.upper()} alongside PERP "
                                    f"{opt['symbol']} ${opt['premium_usd']:.2f} conv={conviction}")
                                self.trades.append({"time":now.isoformat(),"side":opt_type,
                                    "entry":round(opt["mark"],4),"exit":None,"lots":1,
                                    "pnl":None,"pct":None,"reason":"combo_with_perp",
                                    "won":None,"pid":str(pid),"sym":opt["symbol"]})
                                self.save()
            return  # still don't open new perp

        # ── OPTIONS MODE ───────────────────────────────────────────
        if self.opts_mode and self.opts:
            opt_pos=self.api.opt_pos()
            local_open=[t for t in self.trades if t.get("exit") is None
                and t.get("side") in ("call","put","straddle")]
            if opt_pos or local_open:
                self.status=f"Holding {len(opt_pos or local_open)} option(s)"; return

            if strategy=="STRADDLE":
                st=self.opts.straddle(self.price)
                if st.get("found") and st["total"]<=self.capital*0.30:  # 30% total for straddle
                    cp=self.api.opt_pid(st["call"]["symbol"])
                    pp=self.api.opt_pid(st["put"]["symbol"])
                    if cp: self.api.order("buy",1,cp)
                    if pp: self.api.order("buy",1,pp)
                    if cp and pp:
                        self.opts.open(st["call"]["symbol"])
                        self.opts.open(st["put"]["symbol"])
                        be_dist=abs(st["be_up"]-self.price)
                        self.status=f"STRADDLE ${st['total']:.2f} | BE±${be_dist:.0f}"
                        self.emit("TRADE",self.status); self.total_tr+=1; self._daily_n+=1
                        for opt,otype,pid in [(st["call"],"call",cp),(st["put"],"put",pp)]:
                            self.trades.append({"time":now.isoformat(),"side":otype,
                                "entry":round(opt["mark"],4),"exit":None,"lots":1,
                                "pnl":None,"pct":None,"reason":"straddle",
                                "won":None,"pid":str(pid),"sym":opt["symbol"]})
                        self.save()
                else:
                    st_debug=f"Straddle: call={'found' if st.get('call',{}).get('found') else 'NOT found'} put={'found' if st.get('put',{}).get('found') else 'NOT found'}"
                    if st.get("found") and st["total"]>self.capital*C.OPT_MAX*2:
                        st_debug=f"Straddle found but premium ${st['total']:.2f} > limit ${self.capital*C.OPT_MAX*2:.2f}"
                    self.status=st_debug
                    self.emit("WARN",st_debug)
                return

            # Directional option
            opt_type=brain["opt_type"]
            opt_conv=brain["opt_conviction"]
            if not opt_type or conviction<=0:
                self.status=f"Waiting signal conv={conviction}"; return

            opt=self.opts.find(opt_type,self.price,opt_conv)
            if not opt.get("found"):
                self.emit("WARN",f"No {opt_type} ({opt_conv}) found"); return
            if opt["premium_usd"]>self.capital*C.OPT_MAX:
                self.emit("INFO",f"Premium ${opt['premium_usd']:.2f} too high"); return

            pid=self.api.opt_pid(opt["symbol"])
            if not pid: self.emit("WARN",f"No pid: {opt['symbol']}"); return

            # Scale lots: 1x/2x/3x based on conviction
            max_lots=max(1,int(self.capital*C.OPT_MAX/opt["premium_usd"])) if opt["premium_usd"]>0 else 1
            opt_lots=min(scale,max_lots)

            r=self.api.order("buy",opt_lots,pid)
            if r.get("success"):
                self.opts.open(opt["symbol"])
                self.status=(f"OPT {opt_type.upper()} {opt_conv.upper()} "
                    f"{opt['symbol']} {opt_lots}L ${opt['premium_usd']*opt_lots:.2f} "
                    f"conv={conviction}")
                self.emit("TRADE",self.status); self.total_tr+=1; self._daily_n+=1
                self.trades.append({"time":now.isoformat(),"side":opt_type,
                    "entry":round(opt["mark"],4),"exit":None,"lots":opt_lots,
                    "pnl":None,"pct":None,"reason":f"{strategy}_{opt_conv}",
                    "won":None,"pid":str(pid),"sym":opt["symbol"]})
                self.save()
                # SIMULTANEOUS: open perp at high conviction
                if conviction>=75 and len(self.api.btcusd_pos())==0:
                    ps="buy" if direction=="long" else "sell"
                    rp=self.api.order(ps,1)
                    if rp.get("success"):
                        atp2=brain.get("atr_pct",0.3)
                        sl2=max(atp2/100,0.008); tp2v=min(atp2*3/100,0.04)
                        sp2=self.price*(1-sl2 if direction=="long" else 1+sl2)
                        tp2p=self.price*(1+tp2v if direction=="long" else 1-tp2v)
                        self.api.bracket(ps,1,sp2,tp2p)
                        self.emit("TRADE",f"⚡ COMBO PERP+{opt_type.upper()} @ ${self.price:,.0f}")
                        self.trades.append({"time":now.isoformat(),"side":direction,
                            "entry":round(self.price,1),"exit":None,"lots":1,
                            "pnl":None,"pct":None,"reason":"combo_with_opt",
                            "won":None,"pid":str(C.PID),"sym":C.SYMBOL})
                        self.save()
            return

        # ── PERPS MODE ─────────────────────────────────────────────
        # Hard gate: never trade if conviction is 0 or below threshold
        if strategy in ("WAIT","STRADDLE") or direction=="neutral":
            self.status=f"Watching {brain['regime']} | macro={macro_bias} | conv={conviction}"
            return
        if conviction<=0:
            self.status=f"Waiting: zero conviction | {brain['regime']} | TF={brain.get('tf_bull',0)}B"
            return

        cfg=self.mode_cfg
        price_now=self.price if self.price>0 else 78000
        margin_per_lot=price_now*C.LOT/C.LEV

        # ── POSITION SIZING: 1% risk rule ─────────────────────────
        # Position Size = (Risk in $ × 100) / Stop-Loss %
        atp=brain.get("atr_pct",0.3); strategy_now=brain.get("strategy","SCALP")
        sl_pct=(atp*1.0/100) if strategy_now=="SWING" else (atp*0.8/100)
        sl_pct=max(sl_pct,0.005)  # min 0.5% stop
        # 1% risk = how many lots?
        risk_usd=self.capital*C.RISK_PER_TRADE
        # Each lot risks: entry × LOT × sl_pct
        risk_per_lot=price_now*C.LOT*sl_pct
        risk_based_lots=max(1,int(risk_usd/risk_per_lot)) if risk_per_lot>0 else 1

        # Scale up on high conviction (still within risk rules)
        conviction_mult=min(scale,3)  # max 3x on high conviction
        requested_lots=risk_based_lots*conviction_mult

        # Hard cap: never more than 10% of capital as margin
        max_margin_lots=1  # HARD CAP: never >1 lot (3-lot loss Apr29 = -$1.77)
        lots=max(1,min(requested_lots,max_margin_lots))
        lots=int(lots)

        actual_risk_usd=round(lots*risk_per_lot,2)
        actual_risk_pct=round(actual_risk_usd/self.capital*100,2) if self.capital>0 else 0
        self.emit("INFO",
            f"Sizing: 1%=${risk_usd:.2f} sl={sl_pct*100:.2f}% "
            f"→ {lots}L | risk=${actual_risk_usd} ({actual_risk_pct}%) "
            f"| conv_mult={conviction_mult}x | mode={self.active_mode}")

        r=self.api.order("buy" if direction=="long" else "sell",lots)
        if not r.get("success"):
            err=r.get("error",r.get("message","?"))
            self.emit("ERROR",f"Order FAILED: {err} | lots={lots} price=${self.price:,.0f}")
            # Log full response for debugging
            self.emit("WARN",f"Delta response: {str(r)[:200]}")
            return
        # Verify order was filled
        filled_size=float((r.get("result",{}) or {}).get("size",0) or lots)
        self.emit("TRADE",f"Order confirmed: {filled_size}L filled")

        # ATR-based TP/SL, wider for SWING
        atp=brain["atr_pct"]
        if strategy=="SWING":
            dyn_sl=max(atp*1.0/100,0.008); dyn_tp=min(atp*3.0/100,0.04)
        elif atp<0.30: dyn_tp=max(atp*1.5/100,0.008); dyn_sl=max(atp*1.0/100,0.005)
        elif atp<0.80: dyn_tp=atp*2.0/100; dyn_sl=atp*1.0/100
        else:          dyn_tp=min(atp*3.0/100,0.08); dyn_sl=min(atp*1.5/100,0.04)

        sp=self.price*(1-dyn_sl if direction=="long" else 1+dyn_sl)
        tp=self.price*(1+dyn_tp if direction=="long" else 1-dyn_tp)
        self.api.bracket("sell" if direction=="long" else "buy",lots,sp,tp)
        self._opened[pid_int(C.PID)]=now; self._daily_n+=1

        scale_label={1:"1x",2:"2x",3:"MAX"}.get(scale,"1x")
        self.status=(f"{'⚡' if strategy=='SWING' else ''}{direction.upper()} "
            f"{lots}L[{scale_label}] @ ${self.price:,.0f} | "
            f"conv={conviction} | SL=${sp:.0f} TP=${tp:.0f}")
        self.emit("TRADE",self.status); self.total_tr+=1
        self.trades.append({"time":now.isoformat(),"side":direction,
            "entry":round(self.price,1),"exit":None,"lots":lots,"pnl":None,
            "pct":None,"reason":strategy.lower(),"won":None,"pid":str(C.PID),"sym":C.SYMBOL})
        self.save()

    def start(self):
        if not self.running:
            self.running=True
            threading.Thread(target=self._loop,daemon=True).start()
            self.emit("INFO","▶ Bot started")

    def stop(self): self.running=False; self.emit("INFO","■ Bot stopped")

    def _loop(self):
        while self.running:
            try: self.scan()
            except Exception as e:
                log.error(f"scan: {e}",exc_info=True); self.status=f"Error: {e}"
            time.sleep(C.SCAN)

    def state(self):
        sc=self.start_cap or self.capital
        pnl_pct=(self.capital-sc)/sc*100 if sc>0 else 0
        pnl_usd=round(self.capital-sc,2)
        done=[t for t in self.trades if t.get("won") is not None]
        trade_pnl=round(sum(t.get("pnl",0) or 0 for t in done),4)
        wr=sum(1 for t in done if t["won"])/len(done)*100 if done else 0
        b=self.brain; trends=b.get("trends",{})
        opts_pos=self.opts.pos_display(self.api.opt_pos()) if self.opts else []
        return {
            "connected":self.connected,"running":self.running,
            "halted":self.halted,"halt_msg":self.halt_msg,
            "status":self.status,"price":round(self.price,1),
            "regime":b.get("regime","—"),"strategy":b.get("strategy","—"),
            "macro_bias":b.get("macro_bias","neutral"),
            "conviction":b.get("raw_conviction",b.get("conviction",0)),
            "scale":b.get("scale",1),
            "direction":b.get("direction","neutral"),
            "trends":trends,"adx":b.get("adx5m",0),
            "bw":b.get("bw",0),"atr_pct":b.get("atr_pct",0),"rsi":b.get("rsi",50),
            "veto":b.get("veto",""),
            "entry_quality":b.get("entry_quality",{}),
            "opt_score":b.get("opt_score",0),
            "opt_pillars":b.get("opt_pillars",{}),
            "at_dip":b.get("at_dip",False),
            "funding":b.get("funding",0),
            "funding_bias":b.get("funding_bias","neutral"),
            "oi_change":b.get("oi_change",0),
            "oi_trend":b.get("oi_trend","flat"),
            "capital":round(self.capital,2),"start_cap":round(sc,2),
            "pnl_pct":round(pnl_pct,2),"pnl_usd":pnl_usd,"trade_pnl_usd":trade_pnl,
            "win_rate":round(wr,1),"total_trades":self.total_tr,"wins":self.wins,
            "next_scan":self.next_scan,"scan_n":self.scan_n,"opts_mode":self.opts_mode,
            "open_pos":self._pos_disp(),"opts_pos":opts_pos,
            "trades":list(reversed(self.trades[-50:])),"logs":list(reversed(self.logs[-80:])),
            "user_settings":{"lot_size":self.lot_size,"max_daily":self.max_daily,
                "daily_trades":self._daily_n,"lot_btc":round(self.lot_size*C.LOT,4),
                "mode":self.mode,"active_mode":self.active_mode,
                "mode_locked":self.mode=="pro" and self.capital<C.MODES["pro"]["min_bal"]},
            "guardrails":{"Opt TP":"+70%","Opt SL":"-15%","Floor":"64% of peak",
                "Risk/trade":"1% of capital","Daily limit":"-3%","Weekly halt":"-8%",
                "Loss cooldown":f"{C.LOSS_COOLDOWN_MINS}min","Circuit":f"{C.CIRC_N} losses"},
            "risk_stats":{
                "daily_pnl_pct":round((self.capital-self.day_start)/self.day_start*100,2) if self.day_start>0 else 0,
                "daily_pnl_usd":round(self.capital-self.day_start,2) if self.day_start>0 else 0,
                "total_pnl_pct":round((self.capital-sc)/sc*100,2) if sc>0 else 0,
                "consec_losses":self._consec,
                "in_cooldown":bool(self._last_close and not getattr(self,'_last_was_win',True) and
                    (datetime.now(timezone.utc)-self._last_close).seconds//60<C.LOSS_COOLDOWN_MINS),
                "daily_loss_limit_pct":C.DAILY_LOSS_LIMIT*100,
                "risk_per_trade_pct":C.RISK_PER_TRADE*100}}

# ═══ FLASK ════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = BOT_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # allow HTTP (not just HTTPS)
    SESSION_COOKIE_NAME="alphabot_session",
    PERMANENT_SESSION_LIFETIME=timedelta(days=90))  # 90 days = never expires practically
CORS(app,supports_credentials=True)

if C.KEY and C.SECRET:
    threading.Thread(target=lambda:get_bot("env").connect(C.KEY,C.SECRET),daemon=True).start()

_auto_setup()
intel.start(bots)
# Auto-reconnect all users with saved keys
def _auto_reconnect_all():
    time.sleep(5)  # wait for Flask to start
    for uid in um.db.get("users",{}).keys():
        get_bot(uid)  # triggers auto-connect if keys saved
    # Run Claude analysis immediately if trades exist and API key set
    time.sleep(10)
    if os.getenv("ANTHROPIC_API_KEY",""):
        all_trades=[t for b in bots.values()
                   for t in b.trades if t.get("won") is not None]
        if len(all_trades)>=3:
            log.info(f"Running startup Claude analysis on {len(all_trades)} trades...")
            params={"capital":max((b.capital for b in bots.values()),default=0),
                    "conf_base":C.CONF_BASE,"dead_zone":C.DEAD_ZONE,
                    "prime_long":C.PRIME_LONG}
            suggestions=intel.ask_claude(all_trades,params)
            if suggestions:
                intel.apply_claude_suggestions(suggestions)
                log.info(f"Startup insight: {suggestions.get('insight','')}")
        else:
            log.info(f"Claude: waiting for more trades ({len(all_trades)}/3 so far)")
    else:
        log.info("Claude learning: set ANTHROPIC_API_KEY to enable")
threading.Thread(target=_auto_reconnect_all,daemon=True).start()

@app.after_request
def _h(r):
    r.headers.update({"Access-Control-Allow-Origin":request.headers.get("Origin","*"),
        "Access-Control-Allow-Methods":"GET,POST,OPTIONS",
        "Access-Control-Allow-Headers":"Content-Type","Access-Control-Allow-Credentials":"true"})
    return r

def login_req(f):
    @wraps(f)
    def w(*a,**kw):
        if "uid" not in session: return jsonify({"error":"unauthorized"}),401
        return f(*a,**kw)
    return w

def admin_req(f):
    @wraps(f)
    def w(*a,**kw):
        uid=session.get("uid")
        if not uid or not um.is_admin(uid): return jsonify({"error":"forbidden"}),403
        return f(*a,**kw)
    return w

@app.route("/auth/me")
def auth_me():
    uid=session.get("uid")
    if not uid: return jsonify({"logged_in":False})
    u=um.get(uid)
    if not u: return jsonify({"logged_in":False})
    return jsonify({"logged_in":True,"username":u["username"],"is_admin":u.get("is_admin",False)})

@app.route("/auth/login",methods=["POST","OPTIONS"])
def auth_login():
    if request.method=="OPTIONS": return jsonify({})
    d=request.json or {}
    ok,uid=um.login(d.get("username",""),d.get("password",""))
    if not ok: return jsonify({"success":False,"message":"Wrong username or password"})
    session["uid"]=uid; session.permanent=True; u=um.get(uid)
    return jsonify({"success":True,"username":u["username"],"is_admin":u.get("is_admin",False)})

@app.route("/auth/register",methods=["POST","OPTIONS"])
def auth_register():
    if request.method=="OPTIONS": return jsonify({})
    d=request.json or {}
    ok,result=um.register(d.get("invite",""),d.get("username","").strip(),d.get("password",""))
    if not ok: return jsonify({"success":False,"message":result}),400
    session["uid"]=result; session.permanent=True
    return jsonify({"success":True,"username":d["username"]})

@app.route("/auth/logout",methods=["POST"])
def auth_logout():
    uid=session.pop("uid",None)
    if uid and uid in bots: bots[uid].stop()
    return jsonify({"success":True})

@app.route("/auth/setup",methods=["POST"])
def auth_setup():
    if um.db["users"]: return jsonify({"error":"Already set up"}),403
    d=request.json or {}
    if d.get("setup_key")!=os.getenv("SETUP_KEY","alphabotsetup"):
        return jsonify({"error":"Wrong key"}),403
    ok,result=um.setup_admin(d.get("username","admin").strip(),d.get("password",""))
    if ok: return jsonify({"success":True,"message":"Admin created!"})
    return jsonify({"error":result}),400

@app.route("/api/status")
@app.route("/api/bot/status")
@login_req
def api_status():
    uid=session["uid"]; b=get_bot(uid)
    return jsonify(b.state())

@app.route("/api/connect",methods=["POST","OPTIONS"])
@login_req
def api_connect():
    if request.method=="OPTIONS": return jsonify({})
    d=request.json or {}; k=d.get("api_key",""); s=d.get("api_secret","")
    if not k or not s: return jsonify({"success":False,"message":"Key and secret required"})
    uid=session["uid"]; b=get_bot(uid)
    result=b.connect(k.strip(),s.strip())
    if result.get("success"):
        # Save API keys to user state — auto-reconnect on restart
        key_file=os.path.join(_DATA,f"keys_{uid}.json")
        try:
            import base64 as b64
            json.dump({"k":b64.b64encode(k.encode()).decode(),
                       "s":b64.b64encode(s.encode()).decode()},
                      open(key_file,"w"))
        except: pass
    return jsonify(result)

@app.route("/api/bot/start",methods=["POST"])
@login_req
def api_start(): get_bot(session["uid"]).start(); return jsonify({"success":True})

@app.route("/api/bot/stop",methods=["POST"])
@login_req
def api_stop(): get_bot(session["uid"]).stop(); return jsonify({"success":True})

@app.route("/api/bot/run_now",methods=["POST"])
@login_req
def api_run():
    threading.Thread(target=get_bot(session["uid"]).scan,daemon=True).start()
    return jsonify({"success":True})

@app.route("/api/clear_stale",methods=["POST"])
@login_req
def api_clear_stale():
    """Remove reconciled/ghost trades from history."""
    b=get_bot(session["uid"])
    # Get real open positions from Delta
    real_syms=set()
    for p in b.api.btcusd_pos(): real_syms.add(str(p.get("product_id","")))
    for p in b.api.opt_pos(): real_syms.add(p.get("product_symbol",""))

    cleaned=[]; removed=0
    for t in b.trades:
        is_open = t.get("exit") is None
        sym=str(t.get("sym","")); pid=str(t.get("pid",""))
        # Remove if: open trade not on Delta, OR zero-PnL ghost
        is_ghost_open = is_open and sym not in real_syms and pid not in real_syms
        is_zero_ghost = (t.get("reason") in ("reconciled","manual_close")
                        and t.get("pnl",0)==0 and not is_open)
        if is_ghost_open or is_zero_ghost:
            removed+=1
            if b.opts: b.opts.close(sym)
        else:
            cleaned.append(t)
    b.trades=cleaned; b.save()
    b.emit("INFO",f"Cleared {removed} stale trades | {len(cleaned)} remain")
    return jsonify({"success":True,"removed":removed,"remaining":len(cleaned)})

@app.route("/api/close_all",methods=["POST"])
@login_req
def api_close_all():
    b=get_bot(session["uid"]); n=0
    for p in b.api.btcusd_pos():
        if b.api.close(float(p.get("size",0)),p.get("product_id",C.PID)).get("success"): n+=1
    for p in b.api.opt_pos():
        if b.api.close(float(p.get("size",0)),p.get("product_id")).get("success"): n+=1
    b.emit("TRADE",f"Emergency close: {n}")
    return jsonify({"success":True,"closed":n})

@app.route("/api/manual_trade",methods=["POST"])
@login_req
def api_manual():
    d=request.json or {}; dirn=d.get("direction","")
    if dirn not in ("long","short"): return jsonify({"success":False,"message":"long or short"})
    b=get_bot(session["uid"]); p=b.price or b.api.price(); lots=max(1,int(d.get("lots",1)))
    r=b.api.order("buy" if dirn=="long" else "sell",lots)
    if r.get("success"):
        sp=p*(1-C.STOP if dirn=="long" else 1+C.STOP); tp=p*(1+C.TP if dirn=="long" else 1-C.TP)
        b.api.bracket("sell" if dirn=="long" else "buy",lots,sp,tp)
        b.emit("TRADE",f"MANUAL {dirn.upper()} {lots}L @ ${p:,.0f}")
        b.trades.append({"time":datetime.now(timezone.utc).isoformat(),"side":dirn,
            "entry":round(p,1),"exit":None,"lots":lots,"pnl":None,"pct":None,
            "reason":"manual","won":None,"pid":str(C.PID),"sym":C.SYMBOL})
        b.save()
        return jsonify({"success":True,"entry":round(p,1),"stop":round(sp,1),"tp":round(tp,1)})
    return jsonify({"success":False,"message":r.get("error","failed")})

@app.route("/api/manual_opt",methods=["POST"])
@login_req
def api_manual_opt():
    """Manually buy a specific option by symbol."""
    d=request.json or {}
    opt_type=d.get("type","call")
    symbol=d.get("symbol","")
    if not symbol: return jsonify({"success":False,"message":"Symbol required"})
    b=get_bot(session["uid"])
    if not b.opts: return jsonify({"success":False,"message":"Not connected"})
    pid=b.api.opt_pid(symbol)
    if not pid:
        # Try to get pid from ticker
        return jsonify({"success":False,"message":f"No market for {symbol} — check strike/expiry"})
    # Get current mark price
    td=b.api.get(f"/v2/tickers/{symbol}")
    if not td or not td.get("success"):
        return jsonify({"success":False,"message":f"Cannot get price for {symbol}"})
    mark=float(td.get("result",{}).get("mark_price",0) or 0)
    if mark<=0: return jsonify({"success":False,"message":"No mark price — option may be illiquid"})
    premium=round(mark*0.001,3)
    if premium>b.capital*0.20:
        return jsonify({"success":False,"message":f"Premium ${premium:.2f} too high for balance ${b.capital:.2f}"})
    lots=max(1,min(20,int(d.get("lots",1))))
    r=b.api.order("buy",lots,pid)
    if r.get("success"):
        b.opts.open(symbol)
        b.emit("TRADE",f"MANUAL OPT {opt_type.upper()} {symbol} {lots}L ${premium*lots:.3f}")
        b.trades.append({"time":datetime.now(timezone.utc).isoformat(),"side":opt_type,
            "entry":round(mark,4),"exit":None,"lots":lots,"pnl":None,"pct":None,
            "reason":"manual_opt","won":None,"pid":str(pid),"sym":symbol})
        b.save()
        return jsonify({"success":True,"symbol":symbol,"mark":round(mark,4),"premium":premium})
    err=r.get("error",r.get("message","Order failed"))
    return jsonify({"success":False,"message":str(err)})

@app.route("/api/opts/toggle",methods=["POST"])
@login_req
def api_opts_toggle():
    d=request.json or {}; b=get_bot(session["uid"])
    b.opts_mode=bool(d.get("enabled",not b.opts_mode))
    b.emit("INFO","Options ON" if b.opts_mode else "Options OFF")
    return jsonify({"success":True,"opts_mode":b.opts_mode})

@app.route("/api/opts/expiries")
@login_req
def api_opts_expiries():
    """Get all available BTC option expiry dates from Delta Exchange."""
    b=get_bot(session["uid"])
    if not b.connected: return jsonify({"expiries":[]})
    try:
        # Get all live call options to extract unique expiries
        prod=b.api.get("/v2/products",{"contract_type":"call_options","state":"live","page_size":"200"})
        expiries_seen=set(); expiry_list=[]
        if prod and prod.get("success"):
            from datetime import datetime as dt2
            today=__import__('datetime').date.today()
            for p in prod.get("result",[]):
                sym=p.get("symbol","")
                if "BTC" not in sym: continue
                parts=sym.split("-")
                if len(parts)<4: continue
                exp_code=parts[3]  # DDMMYY
                if exp_code in expiries_seen: continue
                expiries_seen.add(exp_code)
                try:
                    exp_date=__import__('datetime').datetime.strptime(exp_code,"%d%m%y").date()
                    if exp_date<=today: continue
                    days=(exp_date-today).days
                    label=exp_date.strftime("%d/%m/%y")
                    expiry_list.append({"code":exp_code,"label":label,"days":days,
                        "date":exp_date.isoformat()})
                except: pass
        expiry_list.sort(key=lambda x:x["days"])
        return jsonify({"expiries":expiry_list[:12]})  # max 12 expiries
    except Exception as e:
        log.warning(f"opts/expiries: {e}")
        return jsonify({"expiries":[],"error":str(e)})

@app.route("/api/opts/chain",methods=["POST"])
@login_req
def api_opts_chain():
    """Fetch full options chain for a given expiry and type from Delta in one call."""
    b=get_bot(session["uid"])
    if not b.opts: return jsonify({"error":"Not connected"})
    d=request.json or {}
    expiry=d.get("expiry","")
    opt_type=d.get("type","call")
    price=b.price or b.api.price() or 79000

    prefix="C" if opt_type=="call" else "P"
    contract_type="call_options" if opt_type=="call" else "put_options"

    # Fetch all live options from Delta
    result=b.api.get("/v2/products",{"contract_type":contract_type,"state":"live"})
    if not result or not result.get("success"):
        return jsonify({"error":"Cannot fetch options","chain":[]})

    products=result.get("result",[])
    # Filter by expiry if provided
    chain=[]
    atm=round(price/500)*500

    for p in products:
        sym=p.get("symbol","")
        if not sym.startswith(f"{prefix}-BTC-"): continue
        parts=sym.split("-")
        if len(parts)<4: continue
        strike=int(parts[2]) if parts[2].isdigit() else 0
        exp=parts[3]
        if expiry and exp!=expiry: continue
        if strike<price*0.9 or strike>price*1.1: continue  # within 10% of price

        # Get ticker for this option
        td=b.api.get(f"/v2/tickers/{sym}")
        if not td or not td.get("success"): continue
        res=td.get("result",{})
        mark=float(res.get("mark_price",0) or 0)
        bid=float(res.get("best_bid",0) or 0)
        ask=float(res.get("best_ask",0) or 0)
        iv=float(res.get("mark_iv",0) or 0)
        oi=float(res.get("open_interest",0) or 0)
        if mark<=0: continue

        moneyness="ATM" if strike==atm else ("ITM" if (opt_type=="call" and strike<price) or (opt_type=="put" and strike>price) else "OTM")
        premium=round(mark*0.001,4)
        be=round(strike+mark,1) if opt_type=="call" else round(strike-mark,1)

        chain.append({
            "sym":sym,"strike":strike,"expiry":exp,
            "mark":round(mark,1),"bid":round(bid,1),"ask":round(ask,1),
            "iv":round(iv,1),"oi":round(oi,0),
            "premium_usd":premium,"moneyness":moneyness,
            "breakeven":be,"type":opt_type
        })

    chain.sort(key=lambda x:x["strike"])
    return jsonify({"chain":chain,"price":round(price,1),"atm":atm,"expiry":expiry})


@app.route("/api/opts/find",methods=["POST"])
@login_req
def api_opts_find():
    b=get_bot(session["uid"])
    if not b.opts: return jsonify({"error":"Not connected"})
    d=request.json or {}
    sym_override=d.get("symbol_override","")
    if sym_override:
        td=b.api.get(f"/v2/tickers/{sym_override}")
        if td and td.get("success"):
            res=td.get("result",{})
            mark=float(res.get("mark_price",0) or 0)
            bid=float(res.get("best_bid",0) or 0)
            if mark>0:
                return jsonify({"found":True,"symbol":sym_override,"mark":round(mark,4),
                    "bid":round(bid,4),"premium_usd":round(mark*0.001,3),
                    "type":d.get("type","call"),
                    "strike":int(sym_override.split("-")[2]) if "-" in sym_override else 0})
        return jsonify({"found":False,"symbol":sym_override})
    return jsonify(b.opts.find(d.get("type","call"),b.price or b.api.price(),d.get("conviction","atm")))

@app.route("/api/opts/straddle",methods=["POST"])
@login_req
def api_opts_straddle():
    b=get_bot(session["uid"])
    if not b.opts: return jsonify({"error":"Not connected"})
    return jsonify(b.opts.straddle(b.price or b.api.price()))

@app.route("/api/user/settings",methods=["POST"])
@login_req
def api_user_settings():
    d=request.json or {}; b=get_bot(session["uid"])
    if "lot_size"  in d: b.lot_size =max(1,min(100,int(d["lot_size"])))
    if "max_daily" in d: b.max_daily=max(1,min(50, int(d["max_daily"])))
    if "mode" in d:
        new_mode=d["mode"].lower()
        if new_mode not in ("safe","normal","pro"):
            return jsonify({"success":False,"message":"Mode must be safe/normal/pro"})
        if new_mode=="pro" and b.capital<C.MODES["pro"]["min_bal"]:
            return jsonify({"success":False,
                "message":f"PRO mode requires ${C.MODES['pro']['min_bal']}. You have ${b.capital:.0f}"})
        b.mode=new_mode
        b.emit("INFO",f"Mode changed to {new_mode.upper()}")
    # Calculate what will ACTUALLY be traded
    price=b.price or 78000
    margin=price*C.LOT/C.LEV
    max_lots=max(1,int(b.capital*0.20/margin)) if b.capital>0 else 1
    cfg=b.mode_cfg
    actual_lots=max(1,min(int(b.lot_size*cfg["lot_mult"]),max_lots))
    actual_risk=round(actual_lots*margin,2)
    actual_pct=round(actual_risk/b.capital*100,1) if b.capital>0 else 0
    b.emit("INFO",f"Settings saved: lots={b.lot_size}(actual={actual_lots}) mode={b.mode} risk=${actual_risk}({actual_pct}%)")
    b.save()
    return jsonify({"success":True,"lot_size":b.lot_size,"max_daily":b.max_daily,"mode":b.mode,
        "actual_lots":actual_lots,"actual_risk_usd":actual_risk,"actual_risk_pct":actual_pct,
        "max_affordable_lots":max_lots,"margin_per_lot":round(margin,2)})

@app.route("/api/ip")
def api_ip():
    try: ip=requests.get("https://api.ipify.org?format=json",timeout=5).json().get("ip","?")
    except: ip="unknown"
    return jsonify({"ip":ip})

@app.route("/api/self_update",methods=["POST"])
def api_self_update():
    d=request.json or {}
    if d.get("token")!=C.DEPLOY_TOKEN: return jsonify({"error":"forbidden"}),403
    def do_update():
        try:
            r=requests.get(C.GITHUB,timeout=30)
            if r.status_code!=200: return
            sf=os.path.abspath(__file__)
            with open(sf,"w") as f: f.write(r.text)
            log.info("Self-update done. Restarting in 3s...")
            time.sleep(3)
            # Kill port before restart to avoid "Address already in use"
            # Kill old process holding port before restart
            import subprocess, signal
            # Find and kill process on port 5000
            try:
                r=subprocess.run(["lsof","-ti","tcp:5000"],capture_output=True,text=True,timeout=5)
                pids=r.stdout.strip().split()
                for pid in pids:
                    try: os.kill(int(pid),signal.SIGTERM)
                    except: pass
            except: pass
            time.sleep(2)
            import subprocess as _sp
            _sp.Popen(f"sleep 5 && python3 {sf}",shell=True,cwd=os.path.dirname(sf),
                stdout=open(sf.replace("server.py","bot.log"),"a"),stderr=_sp.STDOUT)
            log.info("Spawned new process — exiting cleanly")
            time.sleep(1); os._exit(0)
        except Exception as e: log.error(f"update: {e}")
    threading.Thread(target=do_update,daemon=True).start()
    return jsonify({"success":True,"message":"Updating in ~5s"})

@app.route("/api/admin/users")
@admin_req
def admin_users():
    users=um.all()
    for uid,u in users.items():
        b=bots.get(uid)
        u["bot_running"]=b.running if b else False
        u["balance"]=b.capital if b else 0
        u["trades"]=b.total_tr if b else 0
    return jsonify({"users":users,"invites":um.invites(),"max":MAX_USERS})

@app.route("/api/admin/invite",methods=["POST"])
@admin_req
def admin_invite():
    code,msg=um.gen_invite()
    if code: return jsonify({"success":True,"code":code})
    return jsonify({"success":False,"message":msg}),400

@app.route("/api/admin/intel")
@admin_req
def admin_intel():
    intel.update(bots)
    return jsonify(intel.summary())

@app.route("/api/admin/claude_learn",methods=["POST"])
@admin_req
def admin_claude_learn():
    """Manually trigger Claude to analyze trades and update parameters."""
    all_trades=[t for b in bots.values() for t in b.trades if t.get("won") is not None]
    if len(all_trades)<5:
        return jsonify({"success":False,"message":f"Need 5+ closed trades, have {len(all_trades)}"})
    params={"capital":max((b.capital for b in bots.values()),default=0),
            "conf_base":C.CONF_BASE,"dead_zone":C.DEAD_ZONE,"prime_long":C.PRIME_LONG}
    suggestions=intel.ask_claude(all_trades,params)
    if suggestions:
        intel.apply_claude_suggestions(suggestions)
        return jsonify({"success":True,"suggestions":suggestions,
            "applied":{"conf_base":C.CONF_BASE,"dead_zone":C.DEAD_ZONE}})
    return jsonify({"success":False,"message":"Claude analysis failed — check ANTHROPIC_API_KEY"})

@app.route("/api/admin/logs")
@admin_req
def admin_logs():
    all_logs=[]
    for uid,b in bots.items():
        u=um.get(uid); uname=u["username"] if u else uid[:6]
        for e in b.logs[-50:]: all_logs.append({**e,"user":uname})
    all_logs.sort(key=lambda x:x.get("t",""),reverse=True)
    return jsonify({"logs":all_logs[:200],"users":len(bots)})


import base64 as _b64
_DASH = _b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEsbWF4aW11bS1zY2FsZT0xIj4KPHRpdGxlPkFscGhhIEJvdDwvdGl0bGU+CjxzdHlsZT4KKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowOy13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjp0cmFuc3BhcmVudH0KOnJvb3R7LS1nOiMwMGIzODY7LS1nYjojZThmOWYzOy0tZ2Q6I2E3ZjNkMDstLXI6I2U3NGMzYzstLXJiOiNmZWYyZjI7LS1yZDojZmNhNWE1Oy0teTojZjU5ZTBiOy0teWI6I2ZlZjNjNzstLWI6IzNiODJmNjstLWJiOiNlZmY2ZmY7LS10OiMwZjE3MmE7LS10MjojNjQ3NDhiOy0tdDM6Izk0YTNiODstLWJnOiNmMGYyZjU7LS13OiNmZmY7LS1iZHI6MXB4IHNvbGlkICNlMmU4ZjB9CmJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0tdCk7Zm9udC1mYW1pbHk6LWFwcGxlLXN5c3RlbSxCbGlua01hY1N5c3RlbUZvbnQsIlNlZ29lIFVJIixIZWx2ZXRpY2EsQXJpYWwsc2Fucy1zZXJpZjtmb250LXNpemU6MTRweDttaW4taGVpZ2h0OjEwMHZofQovKiBBVVRIICovCi5hdXRoLXdyYXB7bWluLWhlaWdodDoxMDB2aDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQouYXV0aC1jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxNnB4O3BhZGRpbmc6MjhweDt3aWR0aDoxMDAlO21heC13aWR0aDozODBweDtib3gtc2hhZG93OjAgNHB4IDI0cHggcmdiYSgwLDAsMCwuMDgpfQouYXV0aC1sb2dve2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToyMHB4fQouYXV0aC1pY297d2lkdGg6NDBweDtoZWlnaHQ6NDBweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Y29sb3I6I2ZmZjtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDB9Ci5hdXRoLXRpdGxle2ZvbnQtc2l6ZToyMnB4O2ZvbnQtd2VpZ2h0OjgwMH0uYXV0aC1zdWIye2ZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKX0KLmF1dGgtZGVzY3tmb250LXNpemU6MTNweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbToxOHB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlucHt3aWR0aDoxMDAlO2JvcmRlcjp2YXIoLS1iZHIpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTFweCAxM3B4O2ZvbnQtc2l6ZToxNHB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7b3V0bGluZTpub25lO2JhY2tncm91bmQ6I2Y4ZmFmYzttYXJnaW4tYm90dG9tOjEwcHh9Ci5pbnA6Zm9jdXN7Ym9yZGVyLWNvbG9yOnZhcigtLWcpO2JhY2tncm91bmQ6I2ZmZn0KLmF1dGgtYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmF1dGgtYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmF1dGgtbXNne3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O21hcmdpbi10b3A6MTBweDttaW4taGVpZ2h0OjIwcHg7bGluZS1oZWlnaHQ6MS43fQouYXV0aC1tc2cub2t7Y29sb3I6dmFyKC0tZyl9LmF1dGgtbXNnLmVycntjb2xvcjp2YXIoLS1yKX0KLmF1dGgtc3dpdGNoe3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjE0cHh9Ci5hdXRoLXN3aXRjaCBhe2NvbG9yOnZhcigtLWIpO2N1cnNvcjpwb2ludGVyO2ZvbnQtd2VpZ2h0OjYwMH0KLyogTUFJTiBBUFAgKi8KI2FwcHtkaXNwbGF5Om5vbmV9Ci5oZHJ7YmFja2dyb3VuZDp2YXIoLS13KTtwYWRkaW5nOjAgMTZweDtoZWlnaHQ6NTRweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTtwb3NpdGlvbjpzdGlja3k7dG9wOjA7ei1pbmRleDoxMDA7Ym94LXNoYWRvdzowIDFweCA0cHggcmdiYSgwLDAsMCwuMDYpfQoubG9nb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHh9Ci5saWN7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6OXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtjb2xvcjojZmZmO2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMH0KLmxue2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjcwMH0ubHN7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpfQouaHJpZ2h0e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KLnViYWRnZXtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3BhZGRpbmc6NHB4IDEwcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6MjBweDtib3JkZXI6dmFyKC0tYmRyKX0KLnBpbGx7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4O3BhZGRpbmc6NXB4IDEycHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnAtbGl2ZXtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0ucC1vZmZ7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcil9LnAtd2FybntiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KX0KLndyYXB7cGFkZGluZzoxMnB4IDE0cHggOTBweDttYXgtd2lkdGg6NDgwcHg7bWFyZ2luOjAgYXV0b30KLnBhZ2V7ZGlzcGxheTpub25lfS5wYWdlLnNob3d7ZGlzcGxheTpibG9ja30KLm5hdntwb3NpdGlvbjpmaXhlZDtib3R0b206MDtsZWZ0OjA7cmlnaHQ6MDtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci10b3A6dmFyKC0tYmRyKTtkaXNwbGF5OmZsZXg7cGFkZGluZzo4cHggMCBtYXgoOHB4LGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7ei1pbmRleDo5OX0KLm5ie2ZsZXg6MTtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6M3B4O3BhZGRpbmc6NHB4IDA7Ym9yZGVyOm5vbmU7YmFja2dyb3VuZDpub25lO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5uYiAuaWN7Zm9udC1zaXplOjIwcHg7Y29sb3I6dmFyKC0tdDMpfS5uYiAubGJ7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDMpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4fQoubmIub24gLmljLC5uYi5vbiAubGJ7Y29sb3I6dmFyKC0tdCl9Ci5jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTZweDttYXJnaW4tYm90dG9tOjEwcHg7Ym94LXNoYWRvdzowIDFweCAzcHggcmdiYSgwLDAsMCwuMDUpLDAgMnB4IDhweCByZ2JhKDAsMCwwLC4wNCl9Ci5jdHtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206MTJweH0KLyogQ09OTkVDVCBDQVJEICovCi5jY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNjBkZWcsIzBmMTcyYSwjMWUzYTVmKTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMnB4O21hcmdpbi1ib3R0b206MTBweH0KLmN0aXRsZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo4MDA7Y29sb3I6I2ZmZjttYXJnaW4tYm90dG9tOjZweH0KLmNzdWJ7Zm9udC1zaXplOjEycHg7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSk7bWFyZ2luLWJvdHRvbToxNnB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlwLXJvd3tiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7bWFyZ2luLWJvdHRvbToxNHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW59Ci5pcC1sYmx7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjQpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206NHB4fQouaXAtdmFse2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTZweDtmb250LXdlaWdodDo3MDA7Y29sb3I6I2ZmZjtsZXR0ZXItc3BhY2luZzoxcHh9Ci5pcC1jb3B5e2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTIpO2JvcmRlcjpub25lO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6OHB4IDE0cHg7Y29sb3I6I2ZmZjtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmNpbnB7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA4KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7Zm9udC1zaXplOjE0cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtjb2xvcjojZmZmO21hcmdpbi1ib3R0b206MTBweDtvdXRsaW5lOm5vbmV9Ci5jaW5wOmZvY3Vze2JvcmRlci1jb2xvcjp2YXIoLS1nKX0uY2lucDo6cGxhY2Vob2xkZXJ7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMyl9Ci5jYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6MTBweDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLWcpO2NvbG9yOiNmZmY7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5jYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmNtc2d7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjEycHg7bWFyZ2luLXRvcDoxMHB4O21pbi1oZWlnaHQ6MjBweDtsaW5lLWhlaWdodDoxLjd9Ci8qIEhFUk8gKi8KLmhlcm97YmFja2dyb3VuZDp2YXIoLS10KTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMHB4O21hcmdpbi1ib3R0b206MTBweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59Ci5oZXJvOjphZnRlcntjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO3RvcDotNDBweDtyaWdodDotNDBweDt3aWR0aDoxNjBweDtoZWlnaHQ6MTYwcHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5obHtmb250LXNpemU6MTBweDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC40KTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjhweDttYXJnaW4tYm90dG9tOjVweH0KLmhwe2ZvbnQtc2l6ZTo0MHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjojZmZmO2xpbmUtaGVpZ2h0OjE7bGV0dGVyLXNwYWNpbmc6LTEuNXB4fQouaHIye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDttYXJnaW4tdG9wOjlweDtmbGV4LXdyYXA6d3JhcH0KLmNoaXB7cGFkZGluZzozcHggMTBweDtib3JkZXItcmFkaXVzOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDB9Ci5jZ3tiYWNrZ3JvdW5kOnJnYmEoMCwyMDAsMTUwLC4yKTtjb2xvcjojMDBlOGIwfS5jcjJ7YmFja2dyb3VuZDpyZ2JhKDIzMSw3Niw2MCwuMik7Y29sb3I6I2ZmODA4MH0uY257YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSl9Ci5yYmFye3BhZGRpbmc6OXB4IDE0cHg7Ym9yZGVyLXJhZGl1czo4cHg7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnJiLWJ7YmFja2dyb3VuZDp2YXIoLS1nYik7Y29sb3I6IzA1OTY2OTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWdkKX0ucmItcntiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjojZGMyNjI2O2JvcmRlcjoxcHggc29saWQgdmFyKC0tcmQpfS5yYi1ue2JhY2tncm91bmQ6I2Y4ZmFmYztjb2xvcjp2YXIoLS10Mik7Ym9yZGVyOnZhcigtLWJkcil9LnJiLXd7YmFja2dyb3VuZDp2YXIoLS15Yik7Y29sb3I6IzkyNDAwZTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci8qIENPTkZJREVOQ0UgKi8KLmN3e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjE0cHg7cGFkZGluZzo0cHggMH0KLmNybmd7cG9zaXRpb246cmVsYXRpdmU7d2lkdGg6NzJweDtoZWlnaHQ6NzJweDtmbGV4LXNocmluazowfQouY3JuZyBzdmd7dHJhbnNmb3JtOnJvdGF0ZSgtOTBkZWcpO2Rpc3BsYXk6YmxvY2t9Ci5jb3Z7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQouY251bXtmb250LXNpemU6MjJweDtmb250LXdlaWdodDo4MDA7bGluZS1oZWlnaHQ6MX0uY2Rlbntmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTtmb250LXdlaWdodDo3MDB9Ci5jbXR7ZmxleDoxfS5jZGlye2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tYm90dG9tOjNweH0uY2RldHtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Mil9Ci5waWxsYXJze21hcmdpbi10b3A6MTJweH0KLnByb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O3BhZGRpbmc6N3B4IDA7Ym9yZGVyLWJvdHRvbTp2YXIoLS1iZHIpfS5wcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5wbnt3aWR0aDo4NnB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7ZmxleC1zaHJpbms6MH0KLnB0e2ZsZXg6MTtoZWlnaHQ6NXB4O2JhY2tncm91bmQ6I2YxZjVmOTtib3JkZXItcmFkaXVzOjNweDtvdmVyZmxvdzpoaWRkZW59LnBme2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6M3B4O3RyYW5zaXRpb246d2lkdGggLjVzfQoucHN7d2lkdGg6MzZweDt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQouaW5kc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLXRvcDoxMHB4fQouaW5ke2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjEwcHg7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOnZhcigtLWJkcil9Ci5pbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTozcHh9Lml2e2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMH0KLnNiYXJ7aGVpZ2h0OjNweDtiYWNrZ3JvdW5kOiNlMmU4ZjA7Ym9yZGVyLXJhZGl1czoycHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbi10b3A6OXB4fS5zZmlse2hlaWdodDoxMDAlO2JhY2tncm91bmQ6dmFyKC0tYik7Ym9yZGVyLXJhZGl1czoycHg7dHJhbnNpdGlvbjp3aWR0aCAuNXN9Ci5zcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbjtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDo0cHh9Ci8qIFBPU0lUSU9OUyAqLwoucG9ze2JvcmRlci1yYWRpdXM6MTJweDtwYWRkaW5nOjE0cHg7bWFyZ2luLWJvdHRvbToxMHB4fQoucG9zLWx7YmFja2dyb3VuZDojZjBmZGY0O2JvcmRlcjoxcHggc29saWQgdmFyKC0tZ2QpfS5wb3Mtc3tiYWNrZ3JvdW5kOiNmZmY1ZjU7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1yZCl9LnBvcy1ve2JhY2tncm91bmQ6dmFyKC0tYmIpO2JvcmRlcjoxcHggc29saWQgIzkzYzVmZH0KLnBoe2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxMHB4fS5wc3lte2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJhZGdle3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJse2JhY2tncm91bmQ6dmFyKC0tZyk7Y29sb3I6I2ZmZn0uYnNoe2JhY2tncm91bmQ6dmFyKC0tcik7Y29sb3I6I2ZmZn0uYmN7YmFja2dyb3VuZDp2YXIoLS1iKTtjb2xvcjojZmZmfS5icHtiYWNrZ3JvdW5kOiM4YjVjZjY7Y29sb3I6I2ZmZn0KLnBne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6NnB4fQoucGl7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC43NSk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo4cHh9LnBpbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi40cHg7bWFyZ2luLWJvdHRvbToycHh9LnBpdntmb250LXNpemU6MTRweDtmb250LXdlaWdodDo4MDB9LnBpZ3tjb2xvcjp2YXIoLS1nKX0ucGlye2NvbG9yOnZhcigtLXIpfQovKiBXQUxMRVQgKi8KLnd0e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQoud2x7ZmxleDoxfS53bGJ7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQzKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjVweDttYXJnaW4tYm90dG9tOjRweH0KLndhe2ZvbnQtc2l6ZTozMnB4O2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzotMXB4fS53c3tmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHh9Ci53cHtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDA7dGV4dC1hbGlnbjpyaWdodH0ud257Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246cmlnaHQ7bWFyZ2luLXRvcDoycHh9Ci8qIFNUQVRTICovCi5zZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouc3RhdHtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKX0KLnN0bHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTo0cHh9LnN0dntmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDB9Ci5iM3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouYnRue3BhZGRpbmc6MTNweCA2cHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2dhcDo1cHh9LmJ0bjphY3RpdmV7b3BhY2l0eTouOH0KLmJke2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZn0uYnIze2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1yZCl9LmJiM3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtib3JkZXI6MS41cHggc29saWQgI2JmZGJmZX0KLmJjYXtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MS41cHggc29saWQgdmFyKC0tcmQpO3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6OHB4fQovKiBPUFRJT05TICovCi50b2dyb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjEwcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7bWFyZ2luLWJvdHRvbToxMnB4fQoudGx7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6NzAwfS50czN7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4fQoudG9ne3Bvc2l0aW9uOnJlbGF0aXZlO3dpZHRoOjQ2cHg7aGVpZ2h0OjI2cHg7ZmxleC1zaHJpbms6MDtjdXJzb3I6cG9pbnRlcn0KLnRvZyBpbnB1dHtvcGFjaXR5OjA7d2lkdGg6MDtoZWlnaHQ6MDtwb3NpdGlvbjphYnNvbHV0ZX0KLnRvZ3Nse3Bvc2l0aW9uOmFic29sdXRlO2luc2V0OjA7YmFja2dyb3VuZDojZTJlOGYwO2JvcmRlci1yYWRpdXM6MTNweDt0cmFuc2l0aW9uOi4yc30KLnRvZ3NsOjpiZWZvcmV7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoyMHB4O2hlaWdodDoyMHB4O2xlZnQ6M3B4O2JvdHRvbTozcHg7YmFja2dyb3VuZDojZmZmO2JvcmRlci1yYWRpdXM6NTAlO3RyYW5zaXRpb246LjJzO2JveC1zaGFkb3c6MCAxcHggM3B4IHJnYmEoMCwwLDAsLjIpfQoudG9nIGlucHV0OmNoZWNrZWQrLnRvZ3Nse2JhY2tncm91bmQ6dmFyKC0tZyl9LnRvZyBpbnB1dDpjaGVja2VkKy50b2dzbDo6YmVmb3Jle3RyYW5zZm9ybTp0cmFuc2xhdGVYKDIwcHgpfQoub2luZm97ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjtnYXA6OHB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTBweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTttYXJnaW4tYm90dG9tOjEycHg7Zm9udC1zaXplOjExcHh9Ci5vYntkaXNwbGF5OmZsZXg7Z2FwOjhweH0KLm9iYnRue2ZsZXg6MTtwYWRkaW5nOjEwcHg7Ym9yZGVyLXJhZGl1czo4cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXJ9Ci5vYi1je2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpO2JvcmRlcjoxcHggc29saWQgI2JmZGJmZX0ub2ItcHtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXJkKX0ub2Itc3tiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci5vcmVze21hcmdpbi10b3A6MTBweDtwYWRkaW5nOjExcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2ZvbnQtc2l6ZToxMXB4O2xpbmUtaGVpZ2h0OjEuODtib3JkZXI6dmFyKC0tYmRyKTtkaXNwbGF5Om5vbmV9Ci5tcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi10b3A6OHB4fQouYnRubHtmbGV4OjE7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1nKTtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjgwMDtjdXJzb3I6cG9pbnRlcn0KLmJ0bnMye2ZsZXg6MTtwYWRkaW5nOjEzcHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXIpO2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyfQovKiBUUkFERVMgKi8KLnRyLXJvd3twYWRkaW5nOjExcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweH0udHItcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci50aWNve3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo5cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQoudGktbHtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0udGktc3tiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKX0udGktY3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKX0udGktcHtiYWNrZ3JvdW5kOiNmM2U4ZmY7Y29sb3I6IzdjM2FlZH0KLnRtaWR7ZmxleDoxO21pbi13aWR0aDowfS50c3lte2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0udG1ldGF7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4O3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KLnRyaWdodHt0ZXh0LWFsaWduOnJpZ2h0O2ZsZXgtc2hyaW5rOjB9LnRwbmx7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwfS50cGd7Y29sb3I6dmFyKC0tZyl9LnRwcntjb2xvcjp2YXIoLS1yKX0udHBue2NvbG9yOnZhcigtLXQzKX0KLyogTE9HUyAqLwoubGZ7ZGlzcGxheTpmbGV4O2dhcDo2cHg7bWFyZ2luLWJvdHRvbTo4cHh9Ci5sZmJ7cGFkZGluZzo0cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDp2YXIoLS13KTtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtZmFtaWx5OmluaGVyaXR9LmxmYi5vbntiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmY7Ym9yZGVyLWNvbG9yOnZhcigtLXQpfQoubGJveHtiYWNrZ3JvdW5kOiMwZjE3MmE7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzoxMnB4O21heC1oZWlnaHQ6NDAwcHg7b3ZlcmZsb3cteTphdXRvfQoubHJ7cGFkZGluZzo0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMWUyOTNiO2ZvbnQtc2l6ZToxMXB4O2Rpc3BsYXk6ZmxleDtnYXA6OHB4O2ZvbnQtZmFtaWx5Om1vbm9zcGFjZX0KLmx0e2NvbG9yOiM0NzU1Njk7d2hpdGUtc3BhY2U6bm93cmFwO2ZsZXgtc2hyaW5rOjB9LmxJe2NvbG9yOiM2NDc0OGJ9LmxXe2NvbG9yOnZhcigtLXkpfS5sRXtjb2xvcjp2YXIoLS1yKX0ubFR7Y29sb3I6dmFyKC0tZyk7Zm9udC13ZWlnaHQ6NzAwfQovKiBTRVRUSU5HUyAqLwouZ3JhaWwtcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjlweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKX0uZ3JhaWwtcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5ncmt7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tdDIpfS5ncnZ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWcpO3RleHQtYWxpZ246cmlnaHQ7bWF4LXdpZHRoOjYwJX0KLmRjLWJ0bnt3aWR0aDoxMDAlO3BhZGRpbmc6MTJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6NnB4fQovKiBPUFRJT05TIENIQUlOICovCi5vYy1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoyZnIgMS41ZnIgMWZyIDFmcjtnYXA6NHB4O3BhZGRpbmc6OHB4IDEwcHg7Ym9yZGVyLXJhZGl1czo4cHg7Y3Vyc29yOnBvaW50ZXI7bWFyZ2luLWJvdHRvbTozcHg7YWxpZ24taXRlbXM6Y2VudGVyO2JvcmRlcjoxLjVweCBzb2xpZCB0cmFuc3BhcmVudDt0cmFuc2l0aW9uOmFsbCAuMTVzO2JhY2tncm91bmQ6dmFyKC0tdyl9Ci5vYy1yb3c6aG92ZXJ7Ym9yZGVyLWNvbG9yOnZhcigtLWIpO2JhY2tncm91bmQ6I2Y4ZmFmY30KLm9jLXJvdy5hdG17Ym9yZGVyLWNvbG9yOiNmZGU2OGE7YmFja2dyb3VuZDp2YXIoLS15Yil9Ci5vYy1yb3cuc2Vse2JvcmRlci1jb2xvcjp2YXIoLS1iKTtiYWNrZ3JvdW5kOnZhcigtLWJiKX0KLm9jLXJvdy5zZWwtcHtib3JkZXItY29sb3I6dmFyKC0tcik7YmFja2dyb3VuZDp2YXIoLS1yYil9Ci5vYy1oZHJ7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoyZnIgMS41ZnIgMWZyIDFmcjtnYXA6NHB4O3BhZGRpbmc6NHB4IDEwcHg7bWFyZ2luLWJvdHRvbTo0cHh9Ci5vYy1obHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHh9Ci5vYy1za3tmb250LXNpemU6MTNweDtmb250LXdlaWdodDo4MDB9Ci5vYy1wbXtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tYil9Ci5vYy1pdntmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10Myl9Ci5vYy1tbntmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtwYWRkaW5nOjJweCA1cHg7Ym9yZGVyLXJhZGl1czo0cHg7ZGlzcGxheTppbmxpbmUtYmxvY2t9Ci5vYy1tbi5pdG17YmFja2dyb3VuZDp2YXIoLS1nYik7Y29sb3I6dmFyKC0tZyl9Ci5vYy1tbi5hdG17YmFja2dyb3VuZDp2YXIoLS15Yik7Y29sb3I6IzkyNDAwZX0KLm9jLW1uLm90bXtiYWNrZ3JvdW5kOiNmMWY1Zjk7Y29sb3I6dmFyKC0tdDMpfQoub2MtZXhwe3BhZGRpbmc6NnB4IDExcHg7Ym9yZGVyLXJhZGl1czoyMHB4O2JvcmRlcjp2YXIoLS1iZHIpO2JhY2tncm91bmQ6dmFyKC0tdyk7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6dmFyKC0tdDIpO3doaXRlLXNwYWNlOm5vd3JhcDt0ZXh0LWFsaWduOmNlbnRlcn0KLm9jLWV4cC5zZWx7YmFja2dyb3VuZDp2YXIoLS10KTtjb2xvcjojZmZmO2JvcmRlci1jb2xvcjp2YXIoLS10KX0KLyogQURNSU4gKi8KLmF1e2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjEycHg7bWFyZ2luLWJvdHRvbTo4cHg7Ym9yZGVyOnZhcigtLWJkcil9Ci5hdS1uYW1le2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tYm90dG9tOjRweH0KLmF1LXN0YXRze2Rpc3BsYXk6ZmxleDtnYXA6MTJweDtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myl9Ci5pY29kZXtmb250LWZhbWlseTptb25vc3BhY2U7Zm9udC1zaXplOjE1cHg7Zm9udC13ZWlnaHQ6NzAwO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTJweDtiYWNrZ3JvdW5kOiNmOGZhZmM7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOnZhcigtLWJkcik7bGV0dGVyLXNwYWNpbmc6MnB4O21hcmdpbjo4cHggMH0KLmlwYm94e2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTZweDtmb250LXdlaWdodDo3MDA7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtsZXR0ZXItc3BhY2luZzoycHg7bWFyZ2luLWJvdHRvbToxMHB4fQouZW1wdHl7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoyOHB4O2NvbG9yOnZhcigtLXQzKTtmb250LXNpemU6MTNweH0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KCjwhLS0g4pWQ4pWQ4pWQIEFVVEggU0NSRUVOIOKVkOKVkOKVkCAtLT4KPGRpdiBpZD0iYXV0aFNjcmVlbiIgY2xhc3M9ImF1dGgtd3JhcCI+CiAgPGRpdiBjbGFzcz0iYXV0aC1jYXJkIj4KICAgIDxkaXYgY2xhc3M9ImF1dGgtbG9nbyI+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtaWNvIj4mIzkxNjs8L2Rpdj4KICAgICAgPGRpdj48ZGl2IGNsYXNzPSJhdXRoLXRpdGxlIj5BbHBoYSBCb3Q8L2Rpdj48ZGl2IGNsYXNzPSJhdXRoLXN1YjIiPkRlbHRhIEV4Y2hhbmdlIEluZGlhPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8IS0tIExvZ2luIGZvcm0gLS0+CiAgICA8ZGl2IGlkPSJsb2dpbkZvcm0iPgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLWRlc2MiPlNpZ24gaW4gdG8geW91ciB0cmFkaW5nIGFjY291bnQ8L2Rpdj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJsVXNlciIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9IlVzZXJuYW1lIiBhdXRvY29tcGxldGU9InVzZXJuYW1lIiBhdXRvY29ycmVjdD0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ibm9uZSI+CiAgICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0ibFBhc3MiIHR5cGU9InBhc3N3b3JkIiBwbGFjZWhvbGRlcj0iUGFzc3dvcmQiIGF1dG9jb21wbGV0ZT0iY3VycmVudC1wYXNzd29yZCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImF1dGgtYnRuIiBvbmNsaWNrPSJkb0xvZ2luKCkiPlNpZ24gSW48L2J1dHRvbj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1tc2ciIGlkPSJsTXNnIj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1zd2l0Y2giPkhhdmUgYW4gaW52aXRlIGNvZGU/IDxhIG9uY2xpY2s9InNob3dSZWcoKSI+UmVnaXN0ZXIgaGVyZTwvYT48L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gUmVnaXN0ZXIgZm9ybSAtLT4KICAgIDxkaXYgaWQ9InJlZ0Zvcm0iIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLWRlc2MiPkVudGVyIHlvdXIgaW52aXRlIGNvZGUgdG8gY3JlYXRlIGFuIGFjY291bnQ8L2Rpdj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJySW52IiAgdHlwZT0idGV4dCIgICAgIHBsYWNlaG9sZGVyPSJJbnZpdGUgY29kZSIgYXV0b2NvcnJlY3Q9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im5vbmUiPgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9InJVc2VyIiB0eXBlPSJ0ZXh0IiAgICAgcGxhY2Vob2xkZXI9IkNob29zZSBhIHVzZXJuYW1lIiBhdXRvY29ycmVjdD0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ibm9uZSI+CiAgICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0iclBhc3MiIHR5cGU9InBhc3N3b3JkIiBwbGFjZWhvbGRlcj0iQ2hvb3NlIGEgcGFzc3dvcmQgKG1pbiA2IGNoYXJzKSI+CiAgICAgIDxidXR0b24gY2xhc3M9ImF1dGgtYnRuIiBvbmNsaWNrPSJkb1JlZ2lzdGVyKCkiPkNyZWF0ZSBBY2NvdW50PC9idXR0b24+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtbXNnIiBpZD0ick1zZyI+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtc3dpdGNoIj5BbHJlYWR5IHJlZ2lzdGVyZWQ/IDxhIG9uY2xpY2s9InNob3dMb2dpbigpIj5TaWduIGluPC9hPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPCEtLSDilZDilZDilZAgTUFJTiBBUFAg4pWQ4pWQ4pWQIC0tPgo8ZGl2IGlkPSJhcHAiPgo8ZGl2IGNsYXNzPSJoZHIiPgogIDxkaXYgY2xhc3M9ImxvZ28iPjxkaXYgY2xhc3M9ImxpYyI+JiM5MTY7PC9kaXY+PGRpdj48ZGl2IGNsYXNzPSJsbiI+QWxwaGEgQm90PC9kaXY+PGRpdiBjbGFzcz0ibHMiPkRlbHRhIEV4Y2hhbmdlIEluZGlhPC9kaXY+PC9kaXY+PC9kaXY+CiAgPGRpdiBjbGFzcz0iaHJpZ2h0Ij4KICAgIDxzcGFuIGNsYXNzPSJ1YmFkZ2UiIGlkPSJ1QmFkZ2UiPi0tPC9zcGFuPgogICAgPGRpdiBjbGFzcz0icGlsbCBwLW9mZiIgaWQ9InNQaWxsIj4mIzk2Nzk7IDxzcGFuIGlkPSJzVHh0Ij5TdG9wcGVkPC9zcGFuPjwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjxkaXYgY2xhc3M9IndyYXAiPgoKPCEtLSBIT01FIC0tPgo8ZGl2IGNsYXNzPSJwYWdlIHNob3ciIGlkPSJwLWhvbWUiPgoKICA8IS0tIENvbm5lY3QgY2FyZCAtLT4KICA8ZGl2IGlkPSJjb25uZWN0Q2FyZCIgY2xhc3M9ImNjYXJkIj4KICAgIDxkaXYgY2xhc3M9ImN0aXRsZSI+Q29ubmVjdCB0byBEZWx0YSBFeGNoYW5nZTwvZGl2PgogICAgPGRpdiBjbGFzcz0iY3N1YiI+WW91ciBBUEkga2V5cyBhcmUgc3RvcmVkIG9ubHkgaW4geW91ciBicm93c2VyIHNlc3Npb24g4oCUIG5ldmVyIHNhdmVkIG9uIHRoZSBzZXJ2ZXIuPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJpcC1yb3ciPgogICAgICA8ZGl2PjxkaXYgY2xhc3M9ImlwLWxibCI+U2VydmVyIElQIOKAlCB3aGl0ZWxpc3Qgb24gRGVsdGEgZmlyc3Q8L2Rpdj48ZGl2IGNsYXNzPSJpcC12YWwiIGlkPSJzSVAiPkxvYWRpbmcuLi48L2Rpdj48L2Rpdj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iaXAtY29weSIgb25jbGljaz0iY29weUlQKCkiPkNvcHk8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGlucHV0IGNsYXNzPSJjaW5wIiBpZD0iY0tleSIgdHlwZT0idGV4dCIgICAgIHBsYWNlaG9sZGVyPSJBUEkgS2V5IiAgICBhdXRvY29tcGxldGU9Im9mZiIgYXV0b2NvcnJlY3Q9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im5vbmUiPgogICAgPGlucHV0IGNsYXNzPSJjaW5wIiBpZD0iY1NlYyIgdHlwZT0icGFzc3dvcmQiIHBsYWNlaG9sZGVyPSJBUEkgU2VjcmV0Ij4KICAgIDxidXR0b24gY2xhc3M9ImNidG4iIG9uY2xpY2s9ImRvQ29ubmVjdCgpIj5Db25uZWN0PC9idXR0b24+CiAgICA8ZGl2IGNsYXNzPSJjbXNnIiBpZD0iY01zZyI+PC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJ0ZXh0LWFsaWduOmNlbnRlcjttYXJnaW4tdG9wOjhweCI+CiAgICAgIDxzcGFuIG9uY2xpY2s9ImNsZWFyU2F2ZWRLZXlzKCkiIHN0eWxlPSJmb250LXNpemU6MTBweDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4zKTtjdXJzb3I6cG9pbnRlcjt0ZXh0LWRlY29yYXRpb246dW5kZXJsaW5lIj5DbGVhciBzYXZlZCBrZXlzPC9zcGFuPgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gTGl2ZSBkYXNoYm9hcmQgLS0+CiAgPGRpdiBpZD0ibGl2ZURhc2giIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgPGRpdiBjbGFzcz0iaGVybyI+CiAgICAgIDxkaXYgY2xhc3M9ImhsIj5CaXRjb2luICZidWxsOyBMaXZlPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImhwIiBpZD0iaFAiPiQtLTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJocjIiPgogICAgICAgIDxzcGFuIGNsYXNzPSJjaGlwIGNuIiBpZD0iaFIiPi0tPC9zcGFuPgogICAgICAgIDxzcGFuIGNsYXNzPSJjaGlwIGNuIiBpZD0iaFMiPi0tPC9zcGFuPgogICAgICAgIDxzcGFuIGNsYXNzPSJjaGlwIGNuIiBpZD0iaFYiPi0tPC9zcGFuPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0icmJhciByYi1uIiBpZD0ickJhciI+U2Nhbm5pbmcuLi48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTJweCI+Q29uZmlkZW5jZSBTY29yZTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJjdyI+CiAgICAgICAgPGRpdiBjbGFzcz0iY3JuZyI+CiAgICAgICAgICA8c3ZnIHZpZXdCb3g9IjAgMCA3MiA3MiIgd2lkdGg9IjcyIiBoZWlnaHQ9IjcyIj4KICAgICAgICAgICAgPGNpcmNsZSBjeD0iMzYiIGN5PSIzNiIgcj0iMjgiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2YxZjVmOSIgc3Ryb2tlLXdpZHRoPSI3Ii8+CiAgICAgICAgICAgIDxjaXJjbGUgaWQ9ImNBcmMiIGN4PSIzNiIgY3k9IjM2IiByPSIyOCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjMDBiMzg2IiBzdHJva2Utd2lkdGg9IjciIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWRhc2hhcnJheT0iMTc1LjkiIHN0cm9rZS1kYXNob2Zmc2V0PSIxNzUuOSIgc3R5bGU9InRyYW5zaXRpb246c3Ryb2tlLWRhc2hvZmZzZXQgLjZzLHN0cm9rZSAuM3MiLz4KICAgICAgICAgIDwvc3ZnPgogICAgICAgICAgPGRpdiBjbGFzcz0iY292Ij48ZGl2IGNsYXNzPSJjbnVtIiBpZD0iY04iPi0tPC9kaXY+PGRpdiBjbGFzcz0iY2RlbiI+LzEwMDwvZGl2PjwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImNtdCI+PGRpdiBjbGFzcz0iY2RpciIgaWQ9ImNEIj5XQUlUPC9kaXY+PGRpdiBjbGFzcz0iY2RldCIgaWQ9ImNEdCI+R2F0aGVyaW5nIGRhdGEuLi48L2Rpdj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgaWQ9InZldG9CYXIiIHN0eWxlPSJkaXNwbGF5Om5vbmU7Zm9udC1zaXplOjExcHg7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo2cHggMTBweDtiYWNrZ3JvdW5kOiNmZWYzYzc7Ym9yZGVyLXJhZGl1czo2cHg7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NzAwIj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0icGlsbGFycyIgaWQ9InBpbERpdiI+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImluZHMiPgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkFEWDwvZGl2PjxkaXYgY2xhc3M9Iml2IiBpZD0iaUEiPi0tPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iaW5kIj48ZGl2IGNsYXNzPSJpbCI+QkIgV2lkdGg8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlCIj4tLTwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkFUUiAlPC9kaXY+PGRpdiBjbGFzcz0iaXYiIGlkPSJpVCI+LS08L2Rpdj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJpbmQiPjxkaXYgY2xhc3M9ImlsIj5SU0k8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlSc2kiPi0tPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iaW5kIj48ZGl2IGNsYXNzPSJpbCI+RnVuZGluZzwvZGl2PjxkaXYgY2xhc3M9Iml2IiBpZD0iaUZ1bmQiPi0tPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iaW5kIj48ZGl2IGNsYXNzPSJpbCI+T0kgVHJlbmQ8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlPaSI+LS08L2Rpdj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InNiYXIiPjxkaXYgY2xhc3M9InNmaWwiIGlkPSJzRmlsIiBzdHlsZT0id2lkdGg6MCUiPjwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzcm93Ij48c3BhbiBpZD0ic1N0YXR1cyI+Tm90IHJ1bm5pbmc8L3NwYW4+PHNwYW4gaWQ9InNjZCIgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1iKSI+LS08L3NwYW4+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9InBlcnBEaXYiPjwvZGl2PgogICAgPGRpdiBpZD0ib3B0c0RpdiI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPldhbGxldDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ3dCI+CiAgICAgICAgPGRpdiBjbGFzcz0id2wiPjxkaXYgY2xhc3M9IndsYiI+QmFsYW5jZTwvZGl2PjxkaXYgY2xhc3M9IndhIiBpZD0id0EiPiQtLTwvZGl2PjxkaXYgY2xhc3M9IndzIiBpZD0id1N0Ij48L2Rpdj48L2Rpdj4KICAgICAgICA8ZGl2PjxkaXYgY2xhc3M9IndwIiBpZD0id1AiPi0tJTwvZGl2PjxkaXYgY2xhc3M9InduIiBpZD0id04iPlAmYW1wO0wgJC0tPC9kaXY+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZyI+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+V2luIFJhdGU8L2Rpdj48ZGl2IGNsYXNzPSJzdHYiIGlkPSJzV1IiPi0tPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+VHJhZGVzPC9kaXY+PGRpdiBjbGFzcz0ic3R2IiBpZD0ic1RSIj4wPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+U2NhbiAjPC9kaXY+PGRpdiBjbGFzcz0ic3R2IiBzdHlsZT0iY29sb3I6dmFyKC0tYikiIGlkPSJzU04iPjA8L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iYjMiPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG4gYmQiICBvbmNsaWNrPSJib3RTdGFydCgpIj4mIzk2NTQ7IFN0YXJ0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImJ0biBicjMiIG9uY2xpY2s9ImJvdFN0b3AoKSI+JiM5NjMyOyBTdG9wPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImJ0biBiYjMiIG9uY2xpY2s9ImJvdFJ1bigpIj4mIzk4ODk7IFJ1bjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEycHgiPk9wdGlvbnMgTW9kZTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ0b2dyb3ciPgogICAgICAgIDxkaXY+PGRpdiBjbGFzcz0idGwiPkVuYWJsZSBPcHRpb25zIFRyYWRpbmc8L2Rpdj48ZGl2IGNsYXNzPSJ0czMiPkFUTS9JVE0gY2FsbHMgJmFtcDsgcHV0cyArIHN0cmFkZGxlczwvZGl2PjwvZGl2PgogICAgICAgIDxsYWJlbCBjbGFzcz0idG9nIj48aW5wdXQgdHlwZT0iY2hlY2tib3giIGlkPSJ0b2dPIiBvbmNoYW5nZT0idG9nZ2xlT3B0cyh0aGlzLmNoZWNrZWQpIj48c3BhbiBjbGFzcz0idG9nc2wiPjwvc3Bhbj48L2xhYmVsPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBpZD0ib3B0c1BhbmVsIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgICA8ZGl2IGNsYXNzPSJvaW5mbyI+CiAgICAgICAgICA8ZGl2PjxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1nKSI+KzcwJTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+VGFrZSBQcm9maXQ8L2Rpdj48L2Rpdj4KICAgICAgICAgIDxkaXY+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLXIpIj4tMTUlPC9kaXY+PGRpdiBzdHlsZT0iY29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MnB4Ij5TdG9wIExvc3M8L2Rpdj48L2Rpdj4KICAgICAgICAgIDxkaXY+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWIpIj5Mb2NrIDY0JTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+b2YgcGVhazwvZGl2PjwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im9iIj4KICAgICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9iLWMiIG9uY2xpY2s9ImNoa09wdCgnY2FsbCcpIj5DaGVjayBDQUxMPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJvYmJ0biBvYi1wIiBvbmNsaWNrPSJjaGtPcHQoJ3B1dCcpIj5DaGVjayBQVVQ8L2J1dHRvbj4KICAgICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9iLXMiIG9uY2xpY2s9ImNoa1N0KCkiPlN0cmFkZGxlPC9idXR0b24+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBpZD0ib1JlcyIgY2xhc3M9Im9yZXMiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5UcmFkZSBTZXR0aW5nczwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToxMnB4Ij4KICAgICAgICA8ZGl2PgogICAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQyKTttYXJnaW4tYm90dG9tOjZweCI+TG90cyBQZXIgVHJhZGU8L2Rpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweCI+CiAgICAgICAgICAgIDxidXR0b24gb25jbGljaz0iYWRqTG90cygtMSkiIHN0eWxlPSJ3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2JhY2tncm91bmQ6I2Y4ZmFmYztmb250LXNpemU6MThweDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij7iiJI8L2J1dHRvbj4KICAgICAgICAgICAgPHNwYW4gaWQ9ImxvdHNWYWwiIHN0eWxlPSJmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7ZmxleDoxO3RleHQtYWxpZ246Y2VudGVyIj4xPC9zcGFuPgogICAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImFkakxvdHMoMSkiIHN0eWxlPSJ3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2JhY2tncm91bmQ6I2Y4ZmFmYztmb250LXNpemU6MThweDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij4rPC9idXR0b24+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOmNlbnRlcjttYXJnaW4tdG9wOjRweCIgaWQ9ImxvdEJ0Y1ZhbCI+MTAgbG90cyA9IDAuMDEgQlRDPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbTo2cHgiPk1heCBUcmFkZXMvRGF5PC9kaXY+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHgiPgogICAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImFkakRhaWx5KC0xKSIgc3R5bGU9IndpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDojZjhmYWZjO2ZvbnQtc2l6ZToxOHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXQiPuKIkjwvYnV0dG9uPgogICAgICAgICAgICA8c3BhbiBpZD0iZGFpbHlWYWwiIHN0eWxlPSJmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7ZmxleDoxO3RleHQtYWxpZ246Y2VudGVyIj4xMDwvc3Bhbj4KICAgICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJhZGpEYWlseSgxKSIgc3R5bGU9IndpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDojZjhmYWZjO2ZvbnQtc2l6ZToxOHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXQiPis8L2J1dHRvbj4KICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246Y2VudGVyO21hcmdpbi10b3A6NHB4IiBpZD0iZGFpbHlVc2VkIj4wIHVzZWQgdG9kYXk8L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDwhLS0gVHJhZGluZyBNb2RlIFNlbGVjdG9yIC0tPgogICAgICA8ZGl2IHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPgogICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbTo4cHgiPlRyYWRpbmcgTW9kZTwvZGl2PgogICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmciAxZnI7Z2FwOjZweCIgaWQ9Im1vZGVHcmlkIj4KICAgICAgICAgIDxidXR0b24gb25jbGljaz0ic2V0TW9kZSgnc2FmZScpIiBpZD0ibW9kZS1zYWZlIiBzdHlsZT0icGFkZGluZzoxMHB4IDRweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MnB4IHNvbGlkIHZhcigtLWcpO2JhY2tncm91bmQ6dmFyKC0tZ2IpO2NvbG9yOnZhcigtLWcpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyIj4KICAgICAgICAgICAg8J+boSBTQUZFPGJyPjxzcGFuIHN0eWxlPSJmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjQwMCI+MiUgcmlzazxicj4rNTAlIFRQPC9zcGFuPgogICAgICAgICAgPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9InNldE1vZGUoJ25vcm1hbCcpIiBpZD0ibW9kZS1ub3JtYWwiIHN0eWxlPSJwYWRkaW5nOjEwcHggNHB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoycHggc29saWQgdmFyKC0tYik7YmFja2dyb3VuZDp2YXIoLS1iYik7Y29sb3I6dmFyKC0tYik7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXIiPgogICAgICAgICAgICDimpYgTk9STUFMPGJyPjxzcGFuIHN0eWxlPSJmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjQwMCI+NSUgcmlzazxicj4rNzAlIFRQPC9zcGFuPgogICAgICAgICAgPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9InNldE1vZGUoJ3BybycpIiBpZD0ibW9kZS1wcm8iIHN0eWxlPSJwYWRkaW5nOjEwcHggNHB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoycHggc29saWQgI2Y1OWUwYjtiYWNrZ3JvdW5kOiNmZWYzYzc7Y29sb3I6IzkyNDAwZTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlciI+CiAgICAgICAgICAgIPCfmoAgUFJPPGJyPjxzcGFuIHN0eWxlPSJmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjQwMCI+MTAlIHJpc2s8YnI+KzEwMCUgVFA8L3NwYW4+CiAgICAgICAgICA8L2J1dHRvbj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGlkPSJtb2RlTm90ZSIgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjZweDt0ZXh0LWFsaWduOmNlbnRlciI+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8YnV0dG9uIG9uY2xpY2s9InNhdmVVc2VyU2V0dGluZ3MoKSIgc3R5bGU9IndpZHRoOjEwMCU7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlciI+U2F2ZSBTZXR0aW5nczwvYnV0dG9uPgogICAgICA8ZGl2IGlkPSJzZXRNc2ciIHN0eWxlPSJ0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MTFweDttYXJnaW4tdG9wOjZweDttaW4taGVpZ2h0OjE2cHg7Y29sb3I6dmFyKC0tZykiPjwvZGl2PgogICAgPC9kaXY+CgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5NYW51YWwgVHJhZGU8L2Rpdj4KCiAgICAgIDwhLS0gRlVUVVJFUyAtLT4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQyKTttYXJnaW4tYm90dG9tOjZweDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjVweCI+RnV0dXJlcyAoUGVycCk8L2Rpdj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJtTG90cyIgdHlwZT0ibnVtYmVyIiBwbGFjZWhvbGRlcj0iTG90cyAoZGVmYXVsdDogMSkiIG1pbj0iMSIgc3R5bGU9Im1hcmdpbi1ib3R0b206OHB4Ij4KICAgICAgPGRpdiBjbGFzcz0ibXJvdyIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTRweCI+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRubCIgIG9uY2xpY2s9Im1hblRyYWRlKCdsb25nJykiPiYjODU5MzsgQnV5IExvbmc8L2J1dHRvbj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJidG5zMiIgb25jbGljaz0ibWFuVHJhZGUoJ3Nob3J0JykiPiYjODU5NTsgU2VsbCBTaG9ydDwvYnV0dG9uPgogICAgICA8L2Rpdj4KCiAgICAgIDwhLS0gT1BUSU9OUyBDSEFJTiDigJQgRnVsbCBNYW51YWwgVHJhZGluZyBJbnRlcmZhY2UgLS0+CiAgICAgIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6NHB4Ij4KICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO21hcmdpbi1ib3R0b206MTBweDtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyIj4KICAgICAgICAgIDxzcGFuPk9wdGlvbnMgQ2hhaW48L3NwYW4+CiAgICAgICAgICA8c3BhbiBpZD0ib2NMaXZlUHJpY2UiIHN0eWxlPSJmb250LXNpemU6MTNweDtmb250LXdlaWdodDo4MDA7Y29sb3I6dmFyKC0tZykiPiQtLTwvc3Bhbj4KICAgICAgICA8L2Rpdj4KCiAgICAgICAgPCEtLSBTVEVQIDE6IEVYUElSWSDigJQgYWxsIGF2YWlsYWJsZSBkYXRlcyBmcm9tIERlbHRhIC0tPgogICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjcwMDtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206NnB4Ij7ikaAgRVhQSVJZIERBVEU8L2Rpdj4KICAgICAgICA8ZGl2IGlkPSJvY0V4cGlyeVJvdyIgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6NXB4O2ZsZXgtd3JhcDp3cmFwO21hcmdpbi1ib3R0b206MTRweCI+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7cGFkZGluZzo0cHgiPkxvYWRpbmcgZXhwaXJpZXMuLi48L2Rpdj4KICAgICAgICA8L2Rpdj4KCiAgICAgICAgPCEtLSBTVEVQIDI6IFRZUEUgLS0+CiAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS10Myk7Zm9udC13ZWlnaHQ6NzAwO2xldHRlci1zcGFjaW5nOi42cHg7bWFyZ2luLWJvdHRvbTo2cHgiPuKRoSBPUFRJT04gVFlQRTwvZGl2PgogICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6OHB4O21hcmdpbi1ib3R0b206MTRweCI+CiAgICAgICAgICA8YnV0dG9uIGlkPSJvY0NhbGxCdG4iIG9uY2xpY2s9Im9jU2VsVHlwZSgnY2FsbCcpIiBzdHlsZT0icGFkZGluZzoxMHB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoycHggc29saWQgIzkzYzVmZDtiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjgwMDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOi4xNXMiPvCfk4ggQ0FMTDwvYnV0dG9uPgogICAgICAgICAgPGJ1dHRvbiBpZD0ib2NQdXRCdG4iICBvbmNsaWNrPSJvY1NlbFR5cGUoJ3B1dCcpIiAgc3R5bGU9InBhZGRpbmc6MTBweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MnB4IHNvbGlkICNmY2E1YTU7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcik7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjouMTVzO29wYWNpdHk6LjUiPvCfk4kgUFVUPC9idXR0b24+CiAgICAgICAgPC9kaXY+CgogICAgICAgIDwhLS0gU1RFUCAzOiBTVFJJS0UgQ0hBSU4gVEFCTEUgLS0+CiAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS10Myk7Zm9udC13ZWlnaHQ6NzAwO2xldHRlci1zcGFjaW5nOi42cHg7bWFyZ2luLWJvdHRvbTo2cHgiPuKRoiBTRUxFQ1QgU1RSSUtFPC9kaXY+CiAgICAgICAgPGRpdiBpZD0ib2NDaGFpbldyYXAiPgogICAgICAgICAgPGRpdiBzdHlsZT0idGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNnB4O2NvbG9yOnZhcigtLXQzKTtmb250LXNpemU6MTJweCI+U2VsZWN0IGV4cGlyeSB0byBsb2FkIGNoYWluPC9kaXY+CiAgICAgICAgPC9kaXY+CgogICAgICAgIDwhLS0gU1RFUCA0OiBQJkwgQ0FMQ1VMQVRPUiAoc2hvd3Mgd2hlbiBzdHJpa2Ugc2VsZWN0ZWQpIC0tPgogICAgICAgIDxkaXYgaWQ9Im9jUExDYXJkIiBzdHlsZT0iZGlzcGxheTpub25lO2JhY2tncm91bmQ6dmFyKC0tYmcpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjE0cHg7bWFyZ2luLXRvcDoxMHB4O2JvcmRlcjp2YXIoLS1iZHIpIj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbToxMHB4Ij7ikaMgUCZMIFBSRVZJRVc8L2Rpdj4KICAgICAgICAgIDxkaXYgaWQ9Im9jU2VsRGVzYyIgc3R5bGU9ImZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXQpO21hcmdpbi1ib3R0b206MTJweDtmb250LXdlaWdodDo3MDAiPuKAlDwvZGl2PgoKICAgICAgICAgIDwhLS0gTG90cyBhbmQgdGFyZ2V0IC0tPgogICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxMHB4O21hcmdpbi1ib3R0b206MTJweCI+CiAgICAgICAgICAgIDxkaXY+CiAgICAgICAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS10Myk7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1ib3R0b206NHB4Ij5MT1RTPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4Ij4KICAgICAgICAgICAgICAgIDxidXR0b24gb25jbGljaz0ib2NBZGpMb3RzKC0xKSIgc3R5bGU9IndpZHRoOjMwcHg7aGVpZ2h0OjMwcHg7Ym9yZGVyLXJhZGl1czo3cHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDp2YXIoLS13KTtmb250LXNpemU6MThweDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij7iiJI8L2J1dHRvbj4KICAgICAgICAgICAgICAgIDxzcGFuIGlkPSJvY0xvdHMiIHN0eWxlPSJmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7ZmxleDoxO3RleHQtYWxpZ246Y2VudGVyIj4xPC9zcGFuPgogICAgICAgICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJvY0FkakxvdHMoMSkiICBzdHlsZT0id2lkdGg6MzBweDtoZWlnaHQ6MzBweDtib3JkZXItcmFkaXVzOjdweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2ZvbnQtc2l6ZToxOHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXQiPis8L2J1dHRvbj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGlkPSJvY1RvdGFsQ29zdCIgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOmNlbnRlcjttYXJnaW4tdG9wOjNweCI+VG90YWw6ICQtLTwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdj4KICAgICAgICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTtmb250LXdlaWdodDo3MDA7bWFyZ2luLWJvdHRvbTo0cHgiPkJUQyBUQVJHRVQgUFJJQ0U8L2Rpdj4KICAgICAgICAgICAgICA8aW5wdXQgaWQ9Im9jVGFyZ2V0IiB0eXBlPSJudW1iZXIiIHBsYWNlaG9sZGVyPSJlLmcuIDgyMDAwIgogICAgICAgICAgICAgICAgc3R5bGU9IndpZHRoOjEwMCU7Ym9yZGVyOnZhcigtLWJkcik7Ym9yZGVyLXJhZGl1czo3cHg7cGFkZGluZzo4cHg7Zm9udC1zaXplOjEzcHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtvdXRsaW5lOm5vbmU7YmFja2dyb3VuZDp2YXIoLS13KSIKICAgICAgICAgICAgICAgIG9uaW5wdXQ9Im9jQ2FsY1BMKCkiPgogICAgICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6M3B4Ij5FbnRlciB0YXJnZXQgdG8gc2VlIFAmTDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDwvZGl2PgoKICAgICAgICAgIDwhLS0gUCZMIFN0YXRzIC0tPgogICAgICAgICAgPGRpdiBpZD0ib2NQTFN0YXRzIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjtnYXA6NnB4O21hcmdpbi1ib3R0b206MTBweCI+CiAgICAgICAgICAgICAgPGRpdiBzdHlsZT0iYmFja2dyb3VuZDp2YXIoLS13KTtib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjEwcHg7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOnZhcigtLWJkcikiPgogICAgICAgICAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS10Myk7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1ib3R0b206NHB4Ij5NQVggTE9TUzwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBpZD0ib2NNYXhMb3NzIiBzdHlsZT0iZm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLXIpIj7igJQ8L2Rpdj4KICAgICAgICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpIj5QcmVtaXVtIHBhaWQ8L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTBweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKSI+CiAgICAgICAgICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTtmb250LXdlaWdodDo3MDA7bWFyZ2luLWJvdHRvbTo0cHgiPkVTVC4gUFJPRklUPC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGlkPSJvY0VzdFByb2ZpdCIgc3R5bGU9ImZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1nKSI+4oCUPC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGlkPSJvY1JPSSIgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpIj5ST0k6IOKAlDwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICAgIDxkaXYgc3R5bGU9ImJhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzoxMHB4O3RleHQtYWxpZ246Y2VudGVyO2JvcmRlcjp2YXIoLS1iZHIpIj4KICAgICAgICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tYm90dG9tOjRweCI+QlJFQUtFVkVOPC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGlkPSJvY0JFIiBzdHlsZT0iZm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWIpIj7igJQ8L2Rpdj4KICAgICAgICAgICAgICAgIDxkaXYgaWQ9Im9jQkVEaXN0IiBzdHlsZT0iZm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS10MykiPuKAlDwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KCiAgICAgICAgICAgIDwhLS0gUGF5b2ZmIGJhciAtLT4KICAgICAgICAgICAgPGRpdiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo4cHgiPgogICAgICAgICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbjtmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjNweCI+CiAgICAgICAgICAgICAgICA8c3Bhbj5Mb3NzIHpvbmU8L3NwYW4+PHNwYW4gaWQ9Im9jQkVMYWJlbCI+QkU6ICQtLTwvc3Bhbj48c3Bhbj5Qcm9maXQgem9uZTwvc3Bhbj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IHN0eWxlPSJoZWlnaHQ6OHB4O2JhY2tncm91bmQ6I2ZlZTJlMjtib3JkZXItcmFkaXVzOjRweDtvdmVyZmxvdzpoaWRkZW47cG9zaXRpb246cmVsYXRpdmUiPgogICAgICAgICAgICAgICAgPGRpdiBpZD0ib2NQYXlvZmZCYXIiIHN0eWxlPSJwb3NpdGlvbjphYnNvbHV0ZTtoZWlnaHQ6MTAwJTtiYWNrZ3JvdW5kOnZhcigtLWcpO2JvcmRlci1yYWRpdXM6NHB4O2xlZnQ6NTAlO3dpZHRoOjAlO3RyYW5zaXRpb246d2lkdGggLjRzIj48L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgaWQ9Im9jTm90ZSIgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOmNlbnRlcjtsaW5lLWhlaWdodDoxLjUiPuKAlDwvZGl2PgogICAgICAgICAgPC9kaXY+CgogICAgICAgICAgPCEtLSBQbGFjZSBvcmRlciBidXR0b24gLS0+CiAgICAgICAgICA8YnV0dG9uIGlkPSJvY1BsYWNlQnRuIiBvbmNsaWNrPSJvY1BsYWNlT3JkZXIoKSIKICAgICAgICAgICAgc3R5bGU9IndpZHRoOjEwMCU7cGFkZGluZzoxNHB4O2JvcmRlci1yYWRpdXM6MTBweDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmY7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7bWFyZ2luLXRvcDoxMnB4O2Rpc3BsYXk6bm9uZSI+CiAgICAgICAgICAgIFBsYWNlIE9yZGVyCiAgICAgICAgICA8L2J1dHRvbj4KICAgICAgICAgIDxkaXYgaWQ9Im9jTXNnIiBzdHlsZT0iZm9udC1zaXplOjExcHg7dGV4dC1hbGlnbjpjZW50ZXI7bWFyZ2luLXRvcDo4cHg7bWluLWhlaWdodDoxNnB4Ij48L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJiY2EiIG9uY2xpY2s9ImNsb3NlQWxsKCkiPiYjOTg4ODsgQ2xvc2UgQWxsIFBvc2l0aW9uczwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KCjwhLS0gVFJBREVTIC0tPgo8ZGl2IGNsYXNzPSJwYWdlIiBpZD0icC10cmFkZXMiPgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjEycHgiPgogICAgICA8c3BhbiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW46MCI+QWxsIFRyYWRlczwvc3Bhbj4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2dhcDo4cHg7YWxpZ24taXRlbXM6Y2VudGVyIj4KICAgICAgICA8c3BhbiBpZD0idENudCIgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKSI+MCB0cmFkZXM8L3NwYW4+CiAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJjbGVhclN0YWxlKCkiIHN0eWxlPSJwYWRkaW5nOjRweCAxMHB4O2JvcmRlci1yYWRpdXM6NnB4O2JvcmRlcjoxcHggc29saWQgI2ZjYTVhNTtiYWNrZ3JvdW5kOiNmZWYyZjI7Y29sb3I6I2U3NGMzYztmb250LXNpemU6MTBweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdCI+8J+XkSBDbGVhciBTdGFsZTwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0idExpc3QiPjxkaXYgY2xhc3M9ImVtcHR5Ij5ObyB0cmFkZXMgeWV0PC9kaXY+PC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPCEtLSBMT0dTIC0tPgo8ZGl2IGNsYXNzPSJwYWdlIiBpZD0icC1sb2dzIj4KICA8ZGl2IGNsYXNzPSJsZiI+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIgb24iIGlkPSJsZmEiIG9uY2xpY2s9InNldExGKCcnKSI+QWxsPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIiIGlkPSJsZnQiIG9uY2xpY2s9InNldExGKCdUUkFERScpIj5UcmFkZXM8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImxmYiIgaWQ9ImxmdyIgb25jbGljaz0ic2V0TEYoJ1dBUk4nKSI+V2FybmluZ3M8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImxmYiIgaWQ9ImxmZSIgb25jbGljaz0ic2V0TEYoJ0VSUk9SJykiPkVycm9yczwvYnV0dG9uPgogIDwvZGl2PgogIDxkaXYgaWQ9ImxDbnQiIHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbTo4cHgiPjAgZW50cmllczwvZGl2PgogIDxkaXYgY2xhc3M9Imxib3giIGlkPSJsQm94Ij48L2Rpdj4KPC9kaXY+Cgo8IS0tIFNFVFRJTkdTIC0tPgo8ZGl2IGNsYXNzPSJwYWdlIiBpZD0icC1zZXR0aW5ncyI+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206OHB4Ij5TZXJ2ZXIgSVAg4oCUIFdoaXRlbGlzdCBvbiBEZWx0YTwvZGl2PgogICAgPGRpdiBjbGFzcz0iaXBib3giIGlkPSJzaXBCb3giPi0tPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bGluZS1oZWlnaHQ6MS45Ij5EZWx0YSBFeGNoYW5nZSAmcmFycjsgQWNjb3VudCAmcmFycjsgQVBJIEtleXMgJnJhcnI7IEVkaXQgJnJhcnI7IElQIFdoaXRlbGlzdDwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjRweCI+QWN0aXZlIEd1YXJkcmFpbHM8L2Rpdj4KICAgIDxkaXYgaWQ9ImdyTGlzdCI+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8YnV0dG9uIGNsYXNzPSJkYy1idG4iIHN0eWxlPSJjb2xvcjp2YXIoLS1yKSIgb25jbGljaz0iZG9EaXNjb25uZWN0KCkiPiYjMTAwMDc7IERpc2Nvbm5lY3QgRGVsdGEgRXhjaGFuZ2U8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImRjLWJ0biIgc3R5bGU9ImNvbG9yOnZhcigtLXQyKSIgb25jbGljaz0iZG9Mb2dvdXQoKSI+JiM4NTk0OyBTaWduIE91dDwvYnV0dG9uPgogIDwvZGl2PgogIDwhLS0gQWRtaW4gcGFuZWwgLS0+CiAgPGRpdiBpZD0iYWRtaW5QYW5lbCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0iYm9yZGVyOjJweCBzb2xpZCB2YXIoLS15KSI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4O2NvbG9yOnZhcigtLXkpIj4mIzk4ODE7IEFkbWluIFBhbmVsPC9kaXY+CiAgICAgIDxkaXYgaWQ9ImF1TGlzdCI+PC9kaXY+CiAgICAgIDxidXR0b24gb25jbGljaz0iZ2VuSW52aXRlKCkiIHN0eWxlPSJ3aWR0aDoxMDAlO21hcmdpbi10b3A6MTBweDtwYWRkaW5nOjExcHg7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLWIpO2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyIj4rIEdlbmVyYXRlIEludml0ZSBDb2RlPC9idXR0b24+CiAgICAgIDxkaXYgaWQ9Im5ld0ludml0ZSIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICAgICAgPGRpdiBjbGFzcz0iaWNvZGUiIGlkPSJpbnZDb2RlIj48L2Rpdj4KICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7dGV4dC1hbGlnbjpjZW50ZXIiPlNoYXJlIHRoaXMuIE9uZS10aW1lIHVzZSBvbmx5LjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjwvZGl2PjwhLS0gd3JhcCAtLT4KPG5hdiBjbGFzcz0ibmF2Ij4KICA8YnV0dG9uIGNsYXNzPSJuYiBvbiIgaWQ9Im5iLWhvbWUiICAgICBvbmNsaWNrPSJnb1BhZ2UoJ2hvbWUnKSI+PHNwYW4gY2xhc3M9ImljIj4mIzEyNzk2ODs8L3NwYW4+PHNwYW4gY2xhc3M9ImxiIj5Ib21lPC9zcGFuPjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9Im5iIiAgICBpZD0ibmItdHJhZGVzIiAgIG9uY2xpY2s9ImdvUGFnZSgndHJhZGVzJykiPjxzcGFuIGNsYXNzPSJpYyI+JiMxMjgyMDM7PC9zcGFuPjxzcGFuIGNsYXNzPSJsYiI+VHJhZGVzPC9zcGFuPjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9Im5iIiAgICBpZD0ibmItbG9ncyIgICAgIG9uY2xpY2s9ImdvUGFnZSgnbG9ncycpIj48c3BhbiBjbGFzcz0iaWMiPiYjMTI4MjIwOzwvc3Bhbj48c3BhbiBjbGFzcz0ibGIiPkxvZ3M8L3NwYW4+PC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibmIiICAgIGlkPSJuYi1zZXR0aW5ncyIgb25jbGljaz0iZ29QYWdlKCdzZXR0aW5ncycpIj48c3BhbiBjbGFzcz0iaWMiPiYjOTg4MTs8L3NwYW4+PHNwYW4gY2xhc3M9ImxiIj5TZXR0aW5nczwvc3Bhbj48L2J1dHRvbj4KPC9uYXY+CjwvZGl2PjwhLS0gYXBwIC0tPgoKPHNjcmlwdD4KdmFyIFNUPXtsb2dzOltdLGxmOiIiLHRyYWRlczpbXSxuZXh0QXQ6bnVsbCxzczozMDAsaXNBZG1pbjpmYWxzZX07CnZhciBQQz17IlJlZ2ltZSI6IiMzYjgyZjYiLCJNVEYgQWxpZ24iOiIjMDBiMzg2IiwiUlNJIjoiI2Y1OWUwYiIsIk1BQ0QiOiIjOGI1Y2Y2IiwiVm9sYXRpbGl0eSI6IiNlYzQ4OTkiLCJWb2x1bWUiOiIjZTc0YzNjIiwiU2Vzc2lvbiI6IiMxNGI4YTYifTsKCmZ1bmN0aW9uIGdlKGlkKXtyZXR1cm4gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpO30KZnVuY3Rpb24gc3QoaWQsdil7dmFyIGU9Z2UoaWQpO2lmKGUpZS50ZXh0Q29udGVudD12O30KZnVuY3Rpb24gc2goaWQsdil7dmFyIGU9Z2UoaWQpO2lmKGUpZS5pbm5lckhUTUw9djt9CgpmdW5jdGlvbiB4aHIodXJsLGJvZHksY2IpewogIHZhciByZXE9bmV3IFhNTEh0dHBSZXF1ZXN0KCksaXNQPWJvZHkhPT11bmRlZmluZWQmJmJvZHkhPT1udWxsOwogIHJlcS5vcGVuKGlzUD8iUE9TVCI6IkdFVCIsdXJsLHRydWUpO3JlcS53aXRoQ3JlZGVudGlhbHM9dHJ1ZTsKICBpZihpc1ApcmVxLnNldFJlcXVlc3RIZWFkZXIoIkNvbnRlbnQtVHlwZSIsImFwcGxpY2F0aW9uL2pzb24iKTsKICByZXEub25yZWFkeXN0YXRlY2hhbmdlPWZ1bmN0aW9uKCl7CiAgICBpZihyZXEucmVhZHlTdGF0ZSE9PTQpcmV0dXJuOwogICAgaWYoIWNiKXJldHVybjsKICAgIGlmKHJlcS5zdGF0dXM9PT0yMDApe3RyeXtjYihKU09OLnBhcnNlKHJlcS5yZXNwb25zZVRleHQpKTt9Y2F0Y2goZSl7Y2IobnVsbCk7fX0KICAgIGVsc2UgaWYocmVxLnN0YXR1cz09PTQwMSl7c2hvd0F1dGgoKTt9CiAgICBlbHNle2NiKG51bGwpO30KICB9OwogIHJlcS5vbmVycm9yPWZ1bmN0aW9uKCl7aWYoY2IpY2IobnVsbCk7fTsKICByZXEuc2VuZChpc1A/SlNPTi5zdHJpbmdpZnkoYm9keSk6bnVsbCk7Cn0KCmZ1bmN0aW9uIHNob3dBdXRoKCl7Z2UoImF1dGhTY3JlZW4iKS5zdHlsZS5kaXNwbGF5PSJmbGV4IjtnZSgiYXBwIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7fQpmdW5jdGlvbiBzaG93Q29ubmVjdCgpe2dlKCJjb25uZWN0Q2FyZCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtnZSgibGl2ZURhc2giKS5zdHlsZS5kaXNwbGF5PSJub25lIjtsb2FkU2F2ZWRLZXlzKCk7fQpmdW5jdGlvbiBzaG93QXBwKCl7Z2UoImF1dGhTY3JlZW4iKS5zdHlsZS5kaXNwbGF5PSJub25lIjtnZSgiYXBwIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO30KZnVuY3Rpb24gc2hvd0xvZ2luKCl7Z2UoImxvZ2luRm9ybSIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtnZSgicmVnRm9ybSIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO30KZnVuY3Rpb24gc2hvd1JlZygpe2dlKCJsb2dpbkZvcm0iKS5zdHlsZS5kaXNwbGF5PSJub25lIjtnZSgicmVnRm9ybSIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjt9CgpmdW5jdGlvbiBnb1BhZ2Uobil7CiAgWyJob21lIiwidHJhZGVzIiwibG9ncyIsInNldHRpbmdzIl0uZm9yRWFjaChmdW5jdGlvbih0KXsKICAgIGdlKCJwLSIrdCkuY2xhc3NMaXN0LnRvZ2dsZSgic2hvdyIsdD09PW4pOwogICAgZ2UoIm5iLSIrdCkuY2xhc3NMaXN0LnRvZ2dsZSgib24iLHQ9PT1uKTsKICB9KTsKICBpZihuPT09InRyYWRlcyIpcmVuZGVyVHJhZGVzKCk7CiAgaWYobj09PSJsb2dzIilyZW5kZXJMb2dzKCk7CiAgaWYobj09PSJzZXR0aW5ncyIpbG9hZEFkbWluKCk7Cn0KCmZ1bmN0aW9uIGRvTG9naW4oKXsKICB2YXIgdT1nZSgibFVzZXIiKS52YWx1ZS50cmltKCkscD1nZSgibFBhc3MiKS52YWx1ZTsKICBpZighdXx8IXApe3Nob3dNc2coImxNc2ciLCJFbnRlciB1c2VybmFtZSBhbmQgcGFzc3dvcmQiLCJlcnIiKTtyZXR1cm47fQogIHNob3dNc2coImxNc2ciLCJTaWduaW5nIGluLi4uIiwiIik7CiAgeGhyKCIvYXV0aC9sb2dpbiIse3VzZXJuYW1lOnUscGFzc3dvcmQ6cH0sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpewogICAgICBTVC5pc0FkbWluPXIuaXNfYWRtaW47c3QoInVCYWRnZSIsci51c2VybmFtZSk7c2hvd0FwcCgpO2xvYWRJUCgpO3BvbGwoKTsKICAgIH1lbHNle3Nob3dNc2coImxNc2ciLHI/ci5tZXNzYWdlOiJMb2dpbiBmYWlsZWQiLCJlcnIiKTt9CiAgfSk7Cn0KZnVuY3Rpb24gZG9SZWdpc3RlcigpewogIHZhciBpPWdlKCJySW52IikudmFsdWUudHJpbSgpLHU9Z2UoInJVc2VyIikudmFsdWUudHJpbSgpLHA9Z2UoInJQYXNzIikudmFsdWU7CiAgaWYoIWl8fCF1fHwhcCl7c2hvd01zZygick1zZyIsIkFsbCBmaWVsZHMgcmVxdWlyZWQiLCJlcnIiKTtyZXR1cm47fQogIHNob3dNc2coInJNc2ciLCJDcmVhdGluZyBhY2NvdW50Li4uIiwiIik7CiAgeGhyKCIvYXV0aC9yZWdpc3RlciIse2ludml0ZTppLHVzZXJuYW1lOnUscGFzc3dvcmQ6cH0sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpewogICAgICBTVC5pc0FkbWluPWZhbHNlO3N0KCJ1QmFkZ2UiLHUpO3Nob3dBcHAoKTtsb2FkSVAoKTtwb2xsKCk7CiAgICB9ZWxzZXtzaG93TXNnKCJyTXNnIixyP3IubWVzc2FnZToiUmVnaXN0cmF0aW9uIGZhaWxlZCIsImVyciIpO30KICB9KTsKfQpmdW5jdGlvbiBzaG93TXNnKGlkLG1zZyxjbHMpe3ZhciBlPWdlKGlkKTtlLnRleHRDb250ZW50PW1zZztlLmNsYXNzTmFtZT0iYXV0aC1tc2ciKyhjbHM/IiAiK2NsczoiIik7fQpmdW5jdGlvbiBkb0xvZ291dCgpewogIGlmKCFjb25maXJtKCJTaWduIG91dD8iKSlyZXR1cm47CiAgeGhyKCIvYXV0aC9sb2dvdXQiLHt9LGZ1bmN0aW9uKCl7c2hvd0F1dGgoKTtnZSgibFVzZXIiKS52YWx1ZT0iIjtnZSgibFBhc3MiKS52YWx1ZT0iIjt9KTsKfQpmdW5jdGlvbiBkb0Rpc2Nvbm5lY3QoKXsKICBpZighY29uZmlybSgiRGlzY29ubmVjdCBEZWx0YSBFeGNoYW5nZT8iKSlyZXR1cm47CiAgZ2UoImNvbm5lY3RDYXJkIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO2dlKCJsaXZlRGFzaCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiOwp9CmZ1bmN0aW9uIGNvcHlJUCgpewogIHZhciBpcD1nZSgic0lQIikudGV4dENvbnRlbnQ7CiAgdHJ5e25hdmlnYXRvci5jbGlwYm9hcmQud3JpdGVUZXh0KGlwKTt9Y2F0Y2goZSl7fQogIHZhciBiPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoIi5pcC1jb3B5Iik7Yi50ZXh0Q29udGVudD0iQ29waWVkISI7CiAgc2V0VGltZW91dChmdW5jdGlvbigpe2IudGV4dENvbnRlbnQ9IkNvcHkiO30sMjAwMCk7Cn0KZnVuY3Rpb24gZG9Db25uZWN0KCl7CiAgdmFyIGs9Z2UoImNLZXkiKS52YWx1ZS50cmltKCkscz1nZSgiY1NlYyIpLnZhbHVlLnRyaW0oKTsKICBpZigha3x8IXMpe2dlKCJjTXNnIikuaW5uZXJIVE1MPSI8c3BhbiBzdHlsZT0nY29sb3I6I2Y4NzE3MSc+RW50ZXIgQVBJIGtleSBhbmQgc2VjcmV0PC9zcGFuPiI7cmV0dXJuO30KICBnZSgiY01zZyIpLnRleHRDb250ZW50PSJDb25uZWN0aW5nLi4uIjsKICB4aHIoIi9hcGkvY29ubmVjdCIse2FwaV9rZXk6ayxhcGlfc2VjcmV0OnN9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgLy8gU2F2ZSBrZXlzIHRvIGxvY2FsU3RvcmFnZSDigJQgcmVtZW1iZXJlZCBhY3Jvc3Mgc2Vzc2lvbnMKICAgICAgdHJ5e2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJhYl9rZXkiLGspO2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJhYl9zZWMiLHMpO31jYXRjaChlKXt9CiAgICAgIGdlKCJjTXNnIikuaW5uZXJIVE1MPSI8c3BhbiBzdHlsZT0nY29sb3I6IzRhZGU4MCc+Q29ubmVjdGVkISAkIityLmJhbGFuY2UudG9GaXhlZCgyKSsiPC9zcGFuPiI7CiAgICAgIGdlKCJjb25uZWN0Q2FyZCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO2dlKCJsaXZlRGFzaCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsKICAgIH1lbHNlewogICAgICB2YXIgaXA9ciYmci5zZXJ2ZXJfaXA/IiB8IElQOiAiK3Iuc2VydmVyX2lwOiIiOwogICAgICBnZSgiY01zZyIpLmlubmVySFRNTD0iPHNwYW4gc3R5bGU9J2NvbG9yOiNmODcxNzEnPiIrKHI/ci5tZXNzYWdlOiJGYWlsZWQiKStpcCsiPC9zcGFuPiI7CiAgICB9CiAgfSk7Cn0KZnVuY3Rpb24gbG9hZFNhdmVkS2V5cygpewogIHRyeXsKICAgIHZhciBrPWxvY2FsU3RvcmFnZS5nZXRJdGVtKCJhYl9rZXkiKTsKICAgIHZhciBzPWxvY2FsU3RvcmFnZS5nZXRJdGVtKCJhYl9zZWMiKTsKICAgIGlmKGsmJnMpewogICAgICBnZSgiY0tleSIpLnZhbHVlPWs7IGdlKCJjU2VjIikudmFsdWU9czsKICAgICAgZ2UoImNNc2ciKS5pbm5lckhUTUw9IjxzcGFuIHN0eWxlPSdjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC41KSc+JiMxMjgyNzQ7IEtleXMgbG9hZGVkIOKAlCB0YXAgQ29ubmVjdDwvc3Bhbj4iOwogICAgfQogIH1jYXRjaChlKXt9Cn0KZnVuY3Rpb24gY2xlYXJTYXZlZEtleXMoKXsKICB0cnl7bG9jYWxTdG9yYWdlLnJlbW92ZUl0ZW0oImFiX2tleSIpO2xvY2FsU3RvcmFnZS5yZW1vdmVJdGVtKCJhYl9zZWMiKTt9Y2F0Y2goZSl7fQogIGdlKCJjS2V5IikudmFsdWU9IiI7Z2UoImNTZWMiKS52YWx1ZT0iIjsKICBnZSgiY01zZyIpLnRleHRDb250ZW50PSJLZXlzIGNsZWFyZWQiOwp9CmZ1bmN0aW9uIGJvdFN0YXJ0KCl7eGhyKCIvYXBpL2JvdC9zdGFydCIse30sbnVsbCk7fQpmdW5jdGlvbiBib3RTdG9wKCl7eGhyKCIvYXBpL2JvdC9zdG9wIix7fSxudWxsKTt9CmZ1bmN0aW9uIGJvdFJ1bigpe3N0KCJzU3RhdHVzIiwiU2Nhbm5pbmcuLi4iKTt4aHIoIi9hcGkvYm90L3J1bl9ub3ciLHt9LG51bGwpO30KZnVuY3Rpb24gY2xvc2VBbGwoKXsKICBpZighY29uZmlybSgiQ2xvc2UgQUxMIG9wZW4gcG9zaXRpb25zPyIpKXJldHVybjsKICB4aHIoIi9hcGkvY2xvc2VfYWxsIix7fSxmdW5jdGlvbihyKXthbGVydCgiQ2xvc2VkOiAiKygociYmci5jbG9zZWQpfHwwKSsiIHBvc2l0aW9ucyIpO30pOwp9CmZ1bmN0aW9uIG1hblRyYWRlKGRpcil7CiAgdmFyIGxvdHM9cGFyc2VJbnQoZ2UoIm1Mb3RzIikudmFsdWUpfHwxOwogIHhocigiL2FwaS9tYW51YWxfdHJhZGUiLHtkaXJlY3Rpb246ZGlyLGxvdHM6bG90c30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpYWxlcnQoZGlyLnRvVXBwZXJDYXNlKCkrIiAiK2xvdHMrIkxcbkVudHJ5ICQiK3IuZW50cnkrIlxuU3RvcCAkIityLnN0b3ArIlxuVFAgJCIrci50cCk7CiAgICBlbHNlIGFsZXJ0KCJGYWlsZWQ6ICIrKChyJiZyLm1lc3NhZ2UpfHwiQ2hlY2sgTG9ncyIpKTsKICB9KTsKfQovLyDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZDilZAKLy8gT1BUSU9OUyBDSEFJTiDigJQgTWFudWFsIFRyYWRpbmcgSW50ZXJmYWNlCi8vIOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkOKVkAp2YXIgT0M9e2V4cGlyeTpudWxsLHR5cGU6ImNhbGwiLHN0cmlrZTpudWxsLG1hcms6MCxsb3RzOjEsc3ltOm51bGwscGlkOm51bGwsYWxsRXhwaXJpZXM6W119OwoKLy8gTG9hZCBhbGwgYXZhaWxhYmxlIGV4cGlyeSBkYXRlcyBmcm9tIERlbHRhIEV4Y2hhbmdlCmZ1bmN0aW9uIG9jTG9hZEV4cGlyaWVzKCl7CiAgeGhyKCIvYXBpL29wdHMvZXhwaXJpZXMiLG51bGwsZnVuY3Rpb24ocil7CiAgICBpZighcnx8IXIuZXhwaXJpZXN8fCFyLmV4cGlyaWVzLmxlbmd0aCl7CiAgICAgIC8vIEZhbGxiYWNrOiBnZW5lcmF0ZSBuZXh0IDggRnJpZGF5cwogICAgICB2YXIgZXhwcz1bXTsgdmFyIGQ9bmV3IERhdGUoKTsKICAgICAgZm9yKHZhciBpPTA7aTw2MCYmZXhwcy5sZW5ndGg8ODtpKyspewogICAgICAgIGQuc2V0RGF0ZShkLmdldERhdGUoKSsxKTsKICAgICAgICBpZihkLmdldERheSgpPT09NSl7CiAgICAgICAgICB2YXIgZGQ9U3RyaW5nKGQuZ2V0RGF0ZSgpKS5wYWRTdGFydCgyLCcwJyk7CiAgICAgICAgICB2YXIgbW09U3RyaW5nKGQuZ2V0TW9udGgoKSsxKS5wYWRTdGFydCgyLCcwJyk7CiAgICAgICAgICB2YXIgeXk9U3RyaW5nKGQuZ2V0RnVsbFllYXIoKSkuc2xpY2UoLTIpOwogICAgICAgICAgdmFyIGxhYmVsPWRkKycvJyttbSsnLycreXk7CiAgICAgICAgICB2YXIgZGF5c0xlZnQ9TWF0aC5jZWlsKChkLW5ldyBEYXRlKCkpLzg2NDAwMDAwKTsKICAgICAgICAgIGV4cHMucHVzaCh7Y29kZTpkZCttbSt5eSxsYWJlbDpsYWJlbCxkYXlzOmRheXNMZWZ0fSk7CiAgICAgICAgfQogICAgICB9CiAgICAgIHJlbmRlckV4cGlyaWVzKGV4cHMpOwogICAgfSBlbHNlIHsKICAgICAgcmVuZGVyRXhwaXJpZXMoci5leHBpcmllcyk7CiAgICB9CiAgfSk7Cn0KCmZ1bmN0aW9uIHJlbmRlckV4cGlyaWVzKGV4cHMpewogIE9DLmFsbEV4cGlyaWVzPWV4cHM7CiAgdmFyIHJvdz1nZSgnb2NFeHBpcnlSb3cnKTsgaWYoIXJvdylyZXR1cm47CiAgcm93LmlubmVySFRNTD0nJzsKICBleHBzLmZvckVhY2goZnVuY3Rpb24oZSxpKXsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7CiAgICBidG4uY2xhc3NOYW1lPSdvYy1leHAnKyhpPT09MD8nIHNlbCc6JycpOwogICAgYnRuLmlubmVySFRNTD0nPGRpdj4nK2UubGFiZWwrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Zm9udC13ZWlnaHQ6NDAwO29wYWNpdHk6LjciPicrZS5kYXlzKydkPC9kaXY+JzsKICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5vYy1leHAnKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdzZWwnKTsKICAgICAgT0MuZXhwaXJ5PWUuY29kZTsKICAgICAgT0Muc3RyaWtlPW51bGw7IE9DLnN5bT1udWxsOyBPQy5waWQ9bnVsbDsKICAgICAgZ2UoJ29jUExDYXJkJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgICAgIG9jTG9hZENoYWluKCk7CiAgICB9OwogICAgaWYoaT09PTApe09DLmV4cGlyeT1lLmNvZGU7fQogICAgcm93LmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7CiAgaWYoZXhwcy5sZW5ndGg+MCYmT0MudHlwZSl7b2NMb2FkQ2hhaW4oKTt9Cn0KCmZ1bmN0aW9uIG9jU2VsVHlwZSh0KXsKICBPQy50eXBlPXQ7IE9DLnN0cmlrZT1udWxsOyBPQy5zeW09bnVsbDsgT0MucGlkPW51bGw7CiAgZ2UoJ29jQ2FsbEJ0bicpLnN0eWxlLm9wYWNpdHk9dD09PSdjYWxsJz8nMSc6JzAuNDUnOwogIGdlKCdvY1B1dEJ0bicpLnN0eWxlLm9wYWNpdHk9dD09PSdwdXQnPycxJzonMC40NSc7CiAgZ2UoJ29jQ2FsbEJ0bicpLnN0eWxlLnRyYW5zZm9ybT10PT09J2NhbGwnPydzY2FsZSgxLjA0KSc6J3NjYWxlKDEpJzsKICBnZSgnb2NQdXRCdG4nKS5zdHlsZS50cmFuc2Zvcm09dD09PSdwdXQnPydzY2FsZSgxLjA0KSc6J3NjYWxlKDEpJzsKICBnZSgnb2NQTENhcmQnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBpZihPQy5leHBpcnkpb2NMb2FkQ2hhaW4oKTsKfQoKZnVuY3Rpb24gb2NMb2FkQ2hhaW4oKXsKICBpZighT0MuZXhwaXJ5fHwhT0MudHlwZSlyZXR1cm47CiAgdmFyIHdyYXA9Z2UoJ29jQ2hhaW5XcmFwJyk7CiAgd3JhcC5pbm5lckhUTUw9JzxkaXYgc3R5bGU9InRleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTZweDtjb2xvcjp2YXIoLS10Myk7Zm9udC1zaXplOjExcHgiPuKPsyBMb2FkaW5nIGNoYWluLi4uPC9kaXY+JzsKICB4aHIoJy9hcGkvb3B0cy9jaGFpbicse2V4cGlyeTpPQy5leHBpcnksdHlwZTpPQy50eXBlfSxmdW5jdGlvbihyKXsKICAgIGlmKCFyfHwhci5jaGFpbil7CiAgICAgIHdyYXAuaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJ0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjE2cHg7Y29sb3I6dmFyKC0tcik7Zm9udC1zaXplOjExcHgiPkZhaWxlZCB0byBsb2FkLiBDaGVjayBjb25uZWN0aW9uLjwvZGl2Pic7CiAgICAgIHJldHVybjsKICAgIH0KICAgIHZhciBmb3VuZD1yLmNoYWluLmZpbHRlcihmdW5jdGlvbih4KXtyZXR1cm4geC5mb3VuZDt9KTsKICAgIGlmKGZvdW5kLmxlbmd0aD09PTApewogICAgICB3cmFwLmlubmVySFRNTD0nPGRpdiBzdHlsZT0idGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNnB4O2NvbG9yOnZhcigtLXQzKTtmb250LXNpemU6MTFweCI+Tm8gbGlxdWlkIG9wdGlvbnMgZm9yIHRoaXMgZXhwaXJ5Ljxicj5UcnkgYSBkaWZmZXJlbnQgZGF0ZS48L2Rpdj4nOwogICAgICByZXR1cm47CiAgICB9CiAgICBvY1JlbmRlckNoYWluKHIuY2hhaW4sci5hdG0sci5wcmljZSk7CiAgfSk7Cn0KCmZ1bmN0aW9uIG9jUmVuZGVyQ2hhaW4ocm93cyxhdG0scHJpY2UpewogIHZhciB3cmFwPWdlKCdvY0NoYWluV3JhcCcpOwogIHZhciBjb2xvcj1PQy50eXBlPT09J2NhbGwnPyd2YXIoLS1iKSc6J3ZhcigtLXIpJzsKICB2YXIgaHRtbD0nPGRpdiBjbGFzcz0ib2MtaGRyIj48ZGl2IGNsYXNzPSJvYy1obCI+U3RyaWtlPC9kaXY+PGRpdiBjbGFzcz0ib2MtaGwiPlByZW1pdW08L2Rpdj48ZGl2IGNsYXNzPSJvYy1obCI+SVY8L2Rpdj48ZGl2IGNsYXNzPSJvYy1obCI+VHlwZTwvZGl2PjwvZGl2Pic7CiAgcm93cy5mb3JFYWNoKGZ1bmN0aW9uKHIpewogICAgaWYoIXIuZm91bmQpe3JldHVybjt9IC8vIHNraXAgaWxsaXF1aWQKICAgIHZhciBtbkNsYXNzPXIubW9uZXluZXNzPT09J0FUTSc/J2F0bSc6ci5tb25leW5lc3M9PT0nSVRNJz8naXRtJzonb3RtJzsKICAgIHZhciByb3dDbGFzcz0nb2Mtcm93Jysoci5zdHJpa2U9PT1hdG0/JyBhdG0nOicnKTsKICAgIGh0bWwrPSc8ZGl2IGNsYXNzPSInK3Jvd0NsYXNzKyciIGRhdGEtc3RyaWtlPSInK3Iuc3RyaWtlKyciIGRhdGEtc3ltPSInK3Iuc3ltKyciIGRhdGEtbWFyaz0iJytyLm1hcmsrJyIgZGF0YS1wcmVtPSInK3IucHJlbWl1bV91c2QrJyIgb25jbGljaz0ib2NTZWxTdHJpa2UodGhpcywnK3Iuc3RyaWtlKycsJycrci5zeW0rJycsJytyLm1hcmsrJywnK3IucHJlbWl1bV91c2QrJykiPicKICAgICAgKyc8ZGl2PjxkaXYgY2xhc3M9Im9jLXNrIj4kJytyLnN0cmlrZS50b0xvY2FsZVN0cmluZygpKyc8L2Rpdj4nCiAgICAgICsnPHNwYW4gY2xhc3M9Im9jLW1uICcrbW5DbGFzcysnIj4nK3IubW9uZXluZXNzKyc8L3NwYW4+PC9kaXY+JwogICAgICArJzxkaXY+PGRpdiBjbGFzcz0ib2MtcG0iIHN0eWxlPSJjb2xvcjonK2NvbG9yKyciPiQnK3IucHJlbWl1bV91c2QudG9GaXhlZCgzKSsnPC9kaXY+JwogICAgICArJzxkaXYgY2xhc3M9Im9jLWl2Ij5tYXJrICQnK3IubWFyay50b0ZpeGVkKDApKyc8L2Rpdj48L2Rpdj4nCiAgICAgICsnPGRpdiBjbGFzcz0ib2MtaXYiPicrKHIuaXY+MD9yLml2KyclJzon4oCUJykrJzwvZGl2PicKICAgICAgKyc8ZGl2IHN0eWxlPSJmb250LXNpemU6MTBweDtmb250LXdlaWdodDo3MDA7Y29sb3I6Jytjb2xvcisnIj4nKyhPQy50eXBlPT09J2NhbGwnPydDRSc6J1BFJykrJzwvZGl2PicKICAgICAgKyc8L2Rpdj4nOwogIH0pOwogIHdyYXAuaW5uZXJIVE1MPWh0bWw7CiAgZ2UoJ29jTGl2ZVByaWNlJykudGV4dENvbnRlbnQ9JyQnKyhwcmljZXx8MCkudG9Mb2NhbGVTdHJpbmcoKTsKfQoKZnVuY3Rpb24gb2NTZWxTdHJpa2UoZWwsc3RyaWtlLHN5bSxtYXJrLHByZW0pewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5vYy1yb3cnKS5mb3JFYWNoKGZ1bmN0aW9uKHIpe3IuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJywnc2VsLXAnKTt9KTsKICBlbC5jbGFzc0xpc3QuYWRkKE9DLnR5cGU9PT0nY2FsbCc/J3NlbCc6J3NlbC1wJyk7CiAgT0Muc3RyaWtlPXN0cmlrZTsgT0Muc3ltPXN5bTsgT0MubWFyaz1tYXJrOwogIHZhciBjYXJkPWdlKCdvY1BMQ2FyZCcpOyBjYXJkLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKICB2YXIgcHJpY2U9cGFyc2VGbG9hdCgoZ2UoJ2hQJyl8fHt9KS50ZXh0Q29udGVudC5yZXBsYWNlKC9bJCxdL2csJycpKXx8NzkwMDA7CiAgdmFyIGl0bT0oT0MudHlwZT09PSdjYWxsJyYmc3RyaWtlPHByaWNlKXx8KE9DLnR5cGU9PT0ncHV0JyYmc3RyaWtlPnByaWNlKTsKICB2YXIgZW1vPU9DLnR5cGU9PT0nY2FsbCc/J/Cfk4gnOifwn5OJJzsKICBnZSgnb2NTZWxEZXNjJykudGV4dENvbnRlbnQ9ZW1vKycgJysoT0MudHlwZT09PSdjYWxsJz8nQ0FMTCc6J1BVVCcpKycgfCBTdHJpa2UgJCcrc3RyaWtlLnRvTG9jYWxlU3RyaW5nKCkrJyB8ICcrT0MuZXhwaXJ5KyhpdG0/JyAoSVRNKSc6c3RyaWtlPT09TWF0aC5yb3VuZChwcmljZS81MDApKjUwMD8nIChBVE0pJzonIChPVE0pJyk7CiAgZ2UoJ29jVG90YWxDb3N0JykudGV4dENvbnRlbnQ9J1RvdGFsOiAkJysocHJlbSpPQy5sb3RzKS50b0ZpeGVkKDMpOwogIGdlKCdvY1BsYWNlQnRuJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwogIGdlKCdvY1BsYWNlQnRuJykudGV4dENvbnRlbnQ9J1BsYWNlICcrKE9DLnR5cGU9PT0nY2FsbCc/J0NBTEwnOidQVVQnKSsnIE9yZGVyIOKAlCAkJysocHJlbSpPQy5sb3RzKS50b0ZpeGVkKDMpOwogIGdlKCdvY01zZycpLnRleHRDb250ZW50PScnOwogIG9jQ2FsY1BMKCk7Cn0KCmZ1bmN0aW9uIG9jQWRqTG90cyhkKXsKICBPQy5sb3RzPU1hdGgubWF4KDEsTWF0aC5taW4oMjAsT0MubG90cytkKSk7CiAgZ2UoJ29jTG90cycpLnRleHRDb250ZW50PU9DLmxvdHM7CiAgdmFyIHByZW09T0MubWFyayowLjAwMTsKICBnZSgnb2NUb3RhbENvc3QnKS50ZXh0Q29udGVudD0nVG90YWw6ICQnKyhwcmVtKk9DLmxvdHMpLnRvRml4ZWQoMyk7CiAgaWYoZ2UoJ29jUGxhY2VCdG4nKSlnZSgnb2NQbGFjZUJ0bicpLnRleHRDb250ZW50PSdQbGFjZSAnKyhPQy50eXBlPT09J2NhbGwnPydDQUxMJzonUFVUJykrJyBPcmRlciDigJQgJCcrKHByZW0qT0MubG90cykudG9GaXhlZCgzKTsKICBvY0NhbGNQTCgpOwp9CgpmdW5jdGlvbiBvY0NhbGNQTCgpewogIGlmKCFPQy5tYXJrfHwhT0Muc3RyaWtlKXJldHVybjsKICB2YXIgcHJpY2U9cGFyc2VGbG9hdCgoZ2UoJ2hQJyl8fHt9KS50ZXh0Q29udGVudC5yZXBsYWNlKC9bJCxdL2csJycpKXx8NzkwMDA7CiAgdmFyIHRhcmdldD1wYXJzZUZsb2F0KGdlKCdvY1RhcmdldCcpLnZhbHVlKXx8MDsKICB2YXIgcHJlbT1PQy5tYXJrKjAuMDAxOyAvLyBVU0QgcGVyIGxvdAogIHZhciB0b3RhbENvc3Q9cHJlbSpPQy5sb3RzOwogIHZhciBiZT1PQy50eXBlPT09J2NhbGwnP09DLnN0cmlrZStPQy5tYXJrOk9DLnN0cmlrZS1PQy5tYXJrOwogIHZhciBiZURpc3Q9TWF0aC5hYnMoYmUtcHJpY2UpOwogIHZhciBiZURpcj1iZT5wcmljZT8nYWJvdmUnOidiZWxvdyc7CgogIGdlKCdvY01heExvc3MnKS50ZXh0Q29udGVudD0nLSQnK3RvdGFsQ29zdC50b0ZpeGVkKDMpOwogIGdlKCdvY0JFJykudGV4dENvbnRlbnQ9JyQnK01hdGgucm91bmQoYmUpLnRvTG9jYWxlU3RyaW5nKCk7CiAgZ2UoJ29jQkVEaXN0JykudGV4dENvbnRlbnQ9JyQnK01hdGgucm91bmQoYmVEaXN0KS50b0xvY2FsZVN0cmluZygpKycgJytiZURpcjsKICBnZSgnb2NCRUxhYmVsJykudGV4dENvbnRlbnQ9J0JFOiAkJytNYXRoLnJvdW5kKGJlKS50b0xvY2FsZVN0cmluZygpOwoKICB2YXIgcHJvZml0PTA7IHZhciByb2k9MDsKICBpZih0YXJnZXQ+MCl7CiAgICBpZihPQy50eXBlPT09J2NhbGwnJiZ0YXJnZXQ+T0Muc3RyaWtlKXsKICAgICAgcHJvZml0PU1hdGgubWF4KDAsKHRhcmdldC1PQy5zdHJpa2UpKjAuMDAxKk9DLmxvdHMtdG90YWxDb3N0KTsKICAgIH0gZWxzZSBpZihPQy50eXBlPT09J3B1dCcmJnRhcmdldDxPQy5zdHJpa2UpewogICAgICBwcm9maXQ9TWF0aC5tYXgoMCwoT0Muc3RyaWtlLXRhcmdldCkqMC4wMDEqT0MubG90cy10b3RhbENvc3QpOwogICAgfQogICAgcm9pPXRvdGFsQ29zdD4wP3Byb2ZpdC90b3RhbENvc3QqMTAwOjA7CiAgICBnZSgnb2NFc3RQcm9maXQnKS50ZXh0Q29udGVudD1wcm9maXQ+MD8nKyQnK3Byb2ZpdC50b0ZpeGVkKDMpOickMCAoYmVsb3cgQkUpJzsKICAgIGdlKCdvY0VzdFByb2ZpdCcpLnN0eWxlLmNvbG9yPXByb2ZpdD4wPyd2YXIoLS1nKSc6J3ZhcigtLXQzKSc7CiAgICBnZSgnb2NST0knKS50ZXh0Q29udGVudD0nUk9JOiAnKyhyb2k+MD8nKycrcm9pLnRvRml4ZWQoMCkrJyUnOicwJScpOwogICAgLy8gUGF5b2ZmIGJhcgogICAgdmFyIGJhclc9TWF0aC5taW4oTWF0aC5tYXgoMCxyb2kvMjAwKjEwMCksMTAwKTsKICAgIGdlKCdvY1BheW9mZkJhcicpLnN0eWxlLndpZHRoPWJhclcrJyUnOwogICAgZ2UoJ29jUGF5b2ZmQmFyJykuc3R5bGUuYmFja2dyb3VuZD1wcm9maXQ+MD8ndmFyKC0tZyknOid2YXIoLS1yKSc7CiAgICBnZSgnb2NOb3RlJykudGV4dENvbnRlbnQ9cHJvZml0PjAKICAgICAgPyfinIUgUHJvZml0IGlmIEJUQyByZWFjaGVzICQnK3RhcmdldC50b0xvY2FsZVN0cmluZygpKycgfCBST0k6ICsnK3JvaS50b0ZpeGVkKDApKyclJwogICAgICA6J+KaoO+4jyBCVEMgbmVlZHMgdG8gJysoT0MudHlwZT09PSdjYWxsJz8ncmlzZSBhYm92ZSc6J2ZhbGwgYmVsb3cnKSsnICQnK01hdGgucm91bmQoYmUpLnRvTG9jYWxlU3RyaW5nKCkrJyB0byBwcm9maXQnOwogIH0gZWxzZSB7CiAgICBnZSgnb2NFc3RQcm9maXQnKS50ZXh0Q29udGVudD0n4oCUJzsKICAgIGdlKCdvY1JPSScpLnRleHRDb250ZW50PSdST0k6IOKAlCc7CiAgICBnZSgnb2NOb3RlJykudGV4dENvbnRlbnQ9J0VudGVyIGEgQlRDIHRhcmdldCBwcmljZSB0byBzZWUgZXN0aW1hdGVkIFAmTCc7CiAgICBnZSgnb2NQYXlvZmZCYXInKS5zdHlsZS53aWR0aD0nMCUnOwogIH0KICBnZSgnb2NQTFN0YXRzJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwp9CgpmdW5jdGlvbiBvY1BsYWNlT3JkZXIoKXsKICBpZighT0Muc3ltfHwhT0MubWFya3x8IU9DLnN0cmlrZSl7Z2UoJ29jTXNnJykudGV4dENvbnRlbnQ9J1NlbGVjdCBhIHN0cmlrZSBmaXJzdCc7cmV0dXJuO30KICB2YXIgcHJlbT0oT0MubWFyayowLjAwMSpPQy5sb3RzKS50b0ZpeGVkKDMpOwogIHZhciBjb25mPSdQbGFjZSAnKyhPQy50eXBlPT09J2NhbGwnPydDQUxMJzonUFVUJykrJyBvcmRlclxuJysKICAgICdTdHJpa2U6ICQnK09DLnN0cmlrZS50b0xvY2FsZVN0cmluZygpKydcbicrCiAgICAnTG90czogJytPQy5sb3RzKydcbicrCiAgICAnVG90YWwgcHJlbWl1bTogJCcrcHJlbSsnXG5cbkNvbmZpcm0/JzsKICBpZighY29uZmlybShjb25mKSlyZXR1cm47CiAgZ2UoJ29jTXNnJykudGV4dENvbnRlbnQ9J1BsYWNpbmcgb3JkZXIuLi4nOwogIGdlKCdvY1BsYWNlQnRuJykuZGlzYWJsZWQ9dHJ1ZTsKICB4aHIoJy9hcGkvbWFudWFsX29wdCcse3R5cGU6T0MudHlwZSxzeW1ib2w6T0Muc3ltLGxvdHM6T0MubG90c30sZnVuY3Rpb24ocil7CiAgICBnZSgnb2NQbGFjZUJ0bicpLmRpc2FibGVkPWZhbHNlOwogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgZ2UoJ29jTXNnJykudGV4dENvbnRlbnQ9J+KchSBPcmRlciBwbGFjZWQ6ICcrT0Muc3ltKycgfCAkJytwcmVtOwogICAgICBnZSgnb2NNc2cnKS5zdHlsZS5jb2xvcj0ndmFyKC0tZyknOwogICAgICBnZSgnb2NQTENhcmQnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICAgICAgLy8gUmVzZXQgc2VsZWN0aW9uCiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5vYy1yb3cnKS5mb3JFYWNoKGZ1bmN0aW9uKHgpe3guY2xhc3NMaXN0LnJlbW92ZSgnc2VsJywnc2VsLXAnKTt9KTsKICAgIH0gZWxzZSB7CiAgICAgIGdlKCdvY01zZycpLnRleHRDb250ZW50PSfinYwgJysociYmci5tZXNzYWdlfHwnT3JkZXIgZmFpbGVkIOKAlCBjaGVjayBsb2dzJyk7CiAgICAgIGdlKCdvY01zZycpLnN0eWxlLmNvbG9yPSd2YXIoLS1yKSc7CiAgICB9CiAgfSk7Cn0KCi8vIFVwZGF0ZSBsaXZlIHByaWNlIGluIGNoYWluCmZ1bmN0aW9uIG9jVXBkYXRlUHJpY2UocCl7CiAgdmFyIGVsPWdlKCdvY0xpdmVQcmljZScpOyBpZihlbCYmcCllbC50ZXh0Q29udGVudD0nJCcrcC50b0xvY2FsZVN0cmluZygpOwp9Cgp2YXIgX29jSW5pdGVkPWZhbHNlOwpmdW5jdGlvbiB0b2dnbGVPcHRzKG9uKXsKICB4aHIoIi9hcGkvb3B0cy90b2dnbGUiLHtlbmFibGVkOm9ufSxmdW5jdGlvbihyKXsKICAgIGdlKCJvcHRzUGFuZWwiKS5zdHlsZS5kaXNwbGF5PShyJiZyLm9wdHNfbW9kZSk/ImJsb2NrIjoibm9uZSI7CiAgICBpZihyJiZyLm9wdHNfbW9kZSYmIV9vY0luaXRlZCl7X29jSW5pdGVkPXRydWU7b2NMb2FkRXhwaXJpZXMoKTt9CiAgfSk7Cn0KZnVuY3Rpb24gY2hrT3B0KHQpewogIHZhciBlbD1nZSgib1JlcyIpO2VsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtlbC50ZXh0Q29udGVudD0iQ2hlY2tpbmcuLi4iOwogIHhocigiL2FwaS9vcHRzL2ZpbmQiLHt0eXBlOnQsaXRtOmZhbHNlfSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuZm91bmQpZWwuaW5uZXJIVE1MPSI8Yj4iK3Iuc3ltYm9sKyI8L2I+PGJyPlN0cmlrZSAkIisoci5zdHJpa2V8fDApLnRvTG9jYWxlU3RyaW5nKCkrIiB8IE1hcmsgJCIrKHIubWFya3x8MCkudG9GaXhlZCgyKSsiIHwgUHJlbWl1bSAkIisoci5wcmVtaXVtX3VzZHx8MCkudG9GaXhlZCgyKSsoci5pdj8iIHwgSVYgIityLml2KyIlIjoiIikrIjxicj4iK3IubW9uZXluZXNzKyIgfCBFeHBpcnkgIityLmV4cGlyeTsKICAgIGVsc2UgZWwudGV4dENvbnRlbnQ9Ik5vICIrdCsiIGZvdW5kLiBFeHBpcnk6ICIrKChyJiZyLmV4cGlyeSl8fCI/Iik7CiAgfSk7Cn0KZnVuY3Rpb24gY2hrU3QoKXsKICB2YXIgZWw9Z2UoIm9SZXMiKTtlbC5zdHlsZS5kaXNwbGF5PSJibG9jayI7ZWwudGV4dENvbnRlbnQ9IkNoZWNraW5nLi4uIjsKICB4aHIoIi9hcGkvb3B0cy9zdHJhZGRsZSIse30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLmZvdW5kKWVsLmlubmVySFRNTD0iPGI+U3RyYWRkbGU8L2I+PGJyPlRvdGFsOiAkIisoci50b3RhbF9wcmVtaXVtX3VzZHx8MCkudG9GaXhlZCgyKSsiPGJyPkJFIHVwOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX3VwfHwwKS50b0xvY2FsZVN0cmluZygpKyIgfCBkb3duOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX2Rvd258fDApLnRvTG9jYWxlU3RyaW5nKCk7CiAgICBlbHNlIGVsLnRleHRDb250ZW50PSJDYW5ub3QgYnVpbGQgc3RyYWRkbGUgcmlnaHQgbm93LiI7CiAgfSk7Cn0KZnVuY3Rpb24gc2V0TEYoZil7CiAgU1QubGY9ZjsKICB2YXIgbT17IiI6ImxmYSIsIlRSQURFIjoibGZ0IiwiV0FSTiI6ImxmdyIsIkVSUk9SIjoibGZlIn07CiAgT2JqZWN0LmtleXMobSkuZm9yRWFjaChmdW5jdGlvbihrKXt2YXIgZWw9Z2UobVtrXSk7aWYoZWwpZWwuY2xhc3NMaXN0LnRvZ2dsZSgib24iLGs9PT1mKTt9KTsKICByZW5kZXJMb2dzKCk7Cn0KZnVuY3Rpb24gcmVuZGVyKHMpewogIGlmKCFzKXJldHVybjsKICBpZihzLmNvbm5lY3RlZCl7Z2UoImNvbm5lY3RDYXJkIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7Z2UoImxpdmVEYXNoIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO30KICB2YXIgcnVuPXMuY29ubmVjdGVkJiZzLnJ1bm5pbmcmJiFzLmhhbHRlZDsKICBnZSgic1BpbGwiKS5jbGFzc05hbWU9InBpbGwgIisocy5oYWx0ZWQ/InAtd2FybiI6cnVuPyJwLWxpdmUiOiJwLW9mZiIpOwogIHN0KCJzVHh0IixzLmhhbHRlZD8iSEFMVEVEIjpydW4/IkxpdmUiOiJTdG9wcGVkIik7CiAgc3QoImhQIixzLnByaWNlPyIkIitzLnByaWNlLnRvTG9jYWxlU3RyaW5nKCk6IiQtLSIpOwogIGlmKHMucHJpY2Upb2NVcGRhdGVQcmljZShzLnByaWNlKTsKICAvLyBVcGRhdGUgQVRNIGluIGNoYWluIHdoZW4gcHJpY2UgY2hhbmdlcwogIGlmKHMucHJpY2UmJl9vcHRJbml0ZWQpe3ZhciBhdG0yPU1hdGgucm91bmQocy5wcmljZS81MDApKjUwMDt2YXIgaHBFbD1nZSgib0xvdHMiKTt9IC8vIGNoYWluIGF1dG8tdXBkYXRlcyB2aWEgbG9hZENoYWluCiAgdmFyIHJnPXMucmVnaW1lfHwiIjsgdmFyIG1iPXMubWFjcm9fYmlhc3x8Im5ldXRyYWwiOwogIHZhciByYz1nZSgiaFIiKTtyYy50ZXh0Q29udGVudD1yZ3x8Ii0tIjtyYy5jbGFzc05hbWU9ImNoaXAgIisocmcuaW5kZXhPZigiQlVMTCIpPj0wPyJjZyI6cmcuaW5kZXhPZigiQkVBUiIpPj0wPyJjcjIiOiJjbiIpOwogIHN0KCJoUyIscy5zdHJhdGVneXx8Ii0tIik7c3QoImhWIixzLnZvbF9yZWdpbWV8fCItLSIpOwogIHZhciBoMXQ9cy5oMV90cmVuZHx8Im5ldXRyYWwiOwogIHZhciBoMWVsPWdlKCJoSDEiKTsKICBpZihoMWVsKXtoMWVsLnRleHRDb250ZW50PSIxSDogIitoMXQudG9VcHBlckNhc2UoKTtoMWVsLmNsYXNzTmFtZT0iY2hpcCAiKyhoMXQ9PT0iYnVsbCI/ImNnIjpoMXQ9PT0iYmVhciI/ImNyMiI6ImNuIik7fQogIHZhciByYj1nZSgickJhciIpO3JiLmNsYXNzTmFtZT0icmJhciAiKyhyZy5pbmRleE9mKCJCVUxMIik+PTA/InJiLWIiOnJnLmluZGV4T2YoIkJFQVIiKT49MD8icmItciI6cmc9PT0iU0lERVdBWVMiPyJyYi13IjoicmItbiIpOwogIHJiLnRleHRDb250ZW50PXJnKyIgXHUyMDE0ICIrKHMuc3RyYXRlZ3l8fCJDYWxjdWxhdGluZyIpOwogIHZhciBzYz1zLmNvbmZfbG9uZ3x8MDtzdCgiY04iLHNjfHwiLS0iKTsKICB2YXIgYXJjPWdlKCJjQXJjIik7YXJjLnN0eWxlLnN0cm9rZURhc2hvZmZzZXQ9MTc1LjktKHNjLzEwMCoxNzUuOSk7YXJjLnN0eWxlLnN0cm9rZT1zYz49NzA/IiMwMGIzODYiOnNjPj01MD8iI2Y1OWUwYiI6IiNlNzRjM2MiOwogIGdlKCJjTiIpLnN0eWxlLmNvbG9yPXNjPj03MD8idmFyKC0tZykiOnNjPj01MD8idmFyKC0teSkiOiJ2YXIoLS1yKSI7CiAgc3QoImNEIixzLnN0cmF0ZWd5PT09IldBSVQiPyJXQUlUIjoocy5kaXJlY3Rpb258fHJnfHwiV0FJVCIpLnRvVXBwZXJDYXNlKCkpOwogIHZhciB0cmVuZHM9cy50cmVuZHN8fHt9OyB2YXIgdFN0cj1PYmplY3QuZW50cmllcyh0cmVuZHMpLm1hcChmdW5jdGlvbihlKXtyZXR1cm4gZVswXSsiOiIrZVsxXVswXS50b1VwcGVyQ2FzZSgpO30pLmpvaW4oIiAiKTsKICB2YXIgZXE9cy5lbnRyeV9xdWFsaXR5fHx7fTsKICB2YXIgZnVuZFR4dD1zLmZ1bmRpbmchPT11bmRlZmluZWQ/ImZ1bmQ9Iisocy5mdW5kaW5nPj0wPyIrIjoiIikrcy5mdW5kaW5nLnRvRml4ZWQoMykrIiUiOiIiOwogIHZhciBvaVR4dD1zLm9pX3RyZW5kJiZzLm9pX3RyZW5kIT09ImZsYXQiPyJPST0iK3Mub2lfdHJlbmQ6IiI7CiAgc3QoImNEdCIsIkNvbnY9IitzYysiIHwgQURYPSIrKHMuYWR4fHwwKSsiIHwgUlNJPSIrKHMucnNpfHwwKSsiIHwgIittYi50b1VwcGVyQ2FzZSgpKyIgfCAiK3RTdHIrKGZ1bmRUeHQ/IiB8ICIrZnVuZFR4dDoiIikrKG9pVHh0PyIgfCAiK29pVHh0OiIiKSk7CiAgLy8gVmV0byByZWFzb24gc2hvd24gcHJvbWluZW50bHkgd2hlbiBjb252PTAKICB2YXIgdmV0b0VsPWdlKCJ2ZXRvQmFyIik7CiAgaWYodmV0b0VsKXsKICAgIGlmKHMudmV0byYmc2M9PT0wKXt2ZXRvRWwuc3R5bGUuZGlzcGxheT0iYmxvY2siO3ZldG9FbC50ZXh0Q29udGVudD0i4o+4ICIrcy52ZXRvLnJlcGxhY2UoL18vZywiICIpO3ZldG9FbC5zdHlsZS5jb2xvcj0idmFyKC0teSkiO30KICAgIGVsc2UgaWYocy52ZXRvKXt2ZXRvRWwuc3R5bGUuZGlzcGxheT0iYmxvY2siO3ZldG9FbC50ZXh0Q29udGVudD0i4pqgICIrcy52ZXRvLnJlcGxhY2UoL18vZywiICIpO3ZldG9FbC5zdHlsZS5jb2xvcj0idmFyKC0tdDMpIjt9CiAgICBlbHNle3ZldG9FbC5zdHlsZS5kaXNwbGF5PSJub25lIjt9CiAgfQogIHZhciBwbHM9cy5waWxsYXJzfHx7fTt2YXIgcGg9IiI7CiAgT2JqZWN0LmtleXMocGxzKS5mb3JFYWNoKGZ1bmN0aW9uKGspe3ZhciB2PXBsc1trXTt2YXIgcGN0PXYubT4wP01hdGgucm91bmQodi5zL3YubSoxMDApOjA7dmFyIGNvbD1QQ1trXXx8InZhcigtLWcpIjtwaCs9IjxkaXYgY2xhc3M9J3Byb3cnPjxkaXYgY2xhc3M9J3BuJz4iK2srIjwvZGl2PjxkaXYgY2xhc3M9J3B0Jz48ZGl2IGNsYXNzPSdwZicgc3R5bGU9J3dpZHRoOiIrcGN0KyIlO2JhY2tncm91bmQ6Iitjb2wrIic+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncHMnIHN0eWxlPSdjb2xvcjoiK2NvbCsiJz4iK3YucysiLyIrdi5tKyI8L2Rpdj48L2Rpdj4iO30pOwogIHNoKCJwaWxEaXYiLHBoKTtzdCgiaUEiLHMuYWR4fHwiLS0iKTtzdCgiaUIiLHMuYnc/cy5idysiJSI6Ii0tIik7c3QoImlUIixzLmF0cl9wY3Q/cy5hdHJfcGN0KyIlIjoiLS0iKTsKICBzdCgic1N0YXR1cyIscy5zdGF0dXN8fCItLSIpO3N0KCJzU04iLHMuc2Nhbl9ufHwwKTsKICAvLyBIaWdobGlnaHQgd2hlbiBjbG9zZSB0byB0aHJlc2hvbGQKICB2YXIgc0VsPWdlKCJzU3RhdHVzIik7CiAgaWYoc0VsICYmIHMuc3RhdHVzICYmIHMuc3RhdHVzLmluZGV4T2YoIm5lZWQ9Iik+PTApe3NFbC5zdHlsZS5jb2xvcj0idmFyKC0teSkiO30KICBlbHNlIGlmKHNFbCl7c0VsLnN0eWxlLmNvbG9yPSIiO30KICBpZihzLm5leHRfc2NhbilTVC5uZXh0QXQ9bmV3IERhdGUocy5uZXh0X3NjYW4pOwogIHZhciBwcD1zLm9wZW5fcG9zfHxbXTt2YXIgcGgyPSIiOwogIHBwLmZvckVhY2goZnVuY3Rpb24ocCl7dmFyIG5lZz1wLnVwbmw8MDtwaDIrPSI8ZGl2IGNsYXNzPSdwb3MgcG9zLSIrKG5lZz8icyI6ImwiKSsiJz48ZGl2IGNsYXNzPSdwaCc+PHNwYW4gY2xhc3M9J3BzeW0nPiIrcC5zeW0rIjwvc3Bhbj48c3BhbiBjbGFzcz0nYmFkZ2UgYiIrKHAuc2lkZT09PSJsb25nIj8ibCI6InNoIikrIic+IitwLnNpZGUudG9VcHBlckNhc2UoKSsiPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9J3BnJz48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5FbnRyeTwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+JCIrcC5lbnRyeS50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5Mb3RzPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4iK3AubG90cysiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+VVBMPC9kaXY+PGRpdiBjbGFzcz0ncGl2ICIrKG5lZz8icGlyIjoicGlnIikrIic+IisocC51cG5sPj0wPyIrIjoiIikrcC51cG5sKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5NYXJrPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIisocC5tYXJrfHxwLmVudHJ5KS50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5TdG9wPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpcic+JCIrcC5zdG9wLnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlRQPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpZyc+JCIrcC50cC50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj48L2Rpdj48L2Rpdj4iO30pOwogIHNoKCJwZXJwRGl2IixwaDIpOwogIHZhciBvcD1zLm9wdHNfcG9zfHxbXTt2YXIgb2g9IiI7CiAgb3AuZm9yRWFjaChmdW5jdGlvbihvKXt2YXIgaXNDPW8udHlwZT09PSJDQUxMIjsKICAgIHZhciBmbG9vckJhcj1vLmZsb29yX2FjdGl2ZQogICAgICA/IjxkaXYgc3R5bGU9J21hcmdpbi10b3A6OHB4O3BhZGRpbmc6N3B4IDEwcHg7YmFja2dyb3VuZDpyZ2JhKDAsMTc5LDEzNCwuMTIpO2JvcmRlci1yYWRpdXM6NnB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgwLDE3OSwxMzQsLjMpO2ZvbnQtc2l6ZToxMXB4O2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbic+PHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLWcpO2ZvbnQtd2VpZ2h0OjcwMCc+8J+UkiBGbG9vciBhY3RpdmU8L3NwYW4+PHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLWcpO2ZvbnQtd2VpZ2h0OjgwMCc+RXhpdCA8ICQiK28uZmxvb3JfcHJpY2UrIiAoKyIrby5mbG9vcl9wY3QrIiUpPC9zcGFuPjwvZGl2PiIKICAgICAgOiI8ZGl2IHN0eWxlPSdtYXJnaW4tdG9wOjhweDtwYWRkaW5nOjdweCAxMHB4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjZweDtib3JkZXI6dmFyKC0tYmRyKTtmb250LXNpemU6MTFweDtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW4nPjxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS10MyknPkZsb29yOiBmaXJzdCBwcm9maXQgdGljazwvc3Bhbj48c3BhbiBzdHlsZT0nY29sb3I6dmFyKC0tciknPkhhcmQgU0wgJCIrby5zbF9wcmljZSsiPC9zcGFuPjwvZGl2PiI7CiAgICBvaCs9IjxkaXYgY2xhc3M9J3BvcyBwb3Mtbyc+PGRpdiBjbGFzcz0ncGgnPjxzcGFuIGNsYXNzPSdwc3ltJyBzdHlsZT0nZm9udC1zaXplOjEycHgnPiIrby5zeW0rIjwvc3Bhbj48c3BhbiBjbGFzcz0nYmFkZ2UgYiIrKGlzQz8iYyI6InAiKSsiJz4iK28udHlwZSsiPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9J3BnJz48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5FbnRyeTwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+JCIrby5lbnRyeSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+TWFyazwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+JCIrby5tYXJrKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5QJkw8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgIisoby5wY3Q8MD8icGlyIjoicGlnIikrIic+Iisoby5wY3Q+PTA/IisiOiIiKStvLnBjdCsiJTwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlBlYWs8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgcGlnJz4kIitvLnBlYWsrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlRQPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpZyc+JCIrby50cF9wcmljZSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+U0w8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgcGlyJz4kIitvLnNsX3ByaWNlKyI8L2Rpdj48L2Rpdj48L2Rpdj4iK2Zsb29yQmFyKyI8L2Rpdj4iO30pOwogIHNoKCJvcHRzRGl2IixvaCk7CiAgdmFyIGNhcD1zLmNhcGl0YWx8fDAsc2MyPXMuc3RhcnRfY2FwfHwwLHBwMj1zLnBubF9wY3R8fDA7CiAgc3QoIndBIixjYXA/IiQiK2NhcC50b0ZpeGVkKDIpOiIkLS0iKTtzdCgid1N0IixzYzI/IlN0YXJ0ZWQgJCIrc2MyLnRvRml4ZWQoMik6IiIpOwogIHZhciB3cEVsPWdlKCJ3UCIpO3dwRWwudGV4dENvbnRlbnQ9KHBwMj49MD8iKyI6IiIpK3BwMi50b0ZpeGVkKDIpKyIlIjt3cEVsLnN0eWxlLmNvbG9yPXBwMj49MD8idmFyKC0tZykiOiJ2YXIoLS1yKSI7CiAgLy8gV2FsbGV0IFAmTCA9IHJlYWwgYmFsYW5jZSBjaGFuZ2UgaW5jbHVkaW5nIGZlZXMvZnVuZGluZwogIHZhciB3UG5sPXMucG5sX3VzZHx8MDsKICBzdCgid04iLCJXYWxsZXQgUCZMICQiKyh3UG5sPj0wPyIrIjoiIikrd1BubC50b0ZpeGVkKDIpKTsKICAvLyBUcmFkZSBQJkwgPSBib3QgY2xvc2VkIHRyYWRlcyBvbmx5CiAgdmFyIHRQbmw9cy50cmFkZV9wbmxfdXNkfHwwOwogIHZhciB0RWw9Z2UoInRyYWRlUG5sUm93Iik7CiAgaWYodEVsKSB0RWwudGV4dENvbnRlbnQ9IkJvdCB0cmFkZXMgUCZMICQiKyh0UG5sPj0wPyIrIjoiIikrdFBubC50b0ZpeGVkKDQpOwogIHN0KCJzV1IiLHMud2luX3JhdGUhPW51bGw/cy53aW5fcmF0ZSsiJSI6Ii0tIik7c3QoInNUUiIscy50b3RhbF90cmFkZXN8fDApOwogIGlmKHMudXNlcl9zZXR0aW5ncyl7CiAgICBfbG90cz1zLnVzZXJfc2V0dGluZ3MubG90X3NpemV8fDE7IGdlKCJsb3RzVmFsIikudGV4dENvbnRlbnQ9X2xvdHM7CiAgICBfZGFpbHk9cy51c2VyX3NldHRpbmdzLm1heF9kYWlseXx8MTA7IGdlKCJkYWlseVZhbCIpLnRleHRDb250ZW50PV9kYWlseTsKICAgIHZhciB1c2VkPXMudXNlcl9zZXR0aW5ncy5kYWlseV90cmFkZXN8fDA7CiAgICB2YXIgZWw9Z2UoImRhaWx5VXNlZCIpOyBpZihlbCkgZWwudGV4dENvbnRlbnQ9dXNlZCsiIHVzZWQgdG9kYXkgKCIrKF9kYWlseS11c2VkKSsiIHJlbWFpbmluZykiOwogICAgdmFyIHNtPXMudXNlcl9zZXR0aW5ncy5hY3RpdmVfbW9kZXx8Im5vcm1hbCI7CiAgICBpZihzbSE9PV9tb2RlKXtfbW9kZT1zbTtzZXRNb2RlKHNtKTt9CiAgICBpZihzLnVzZXJfc2V0dGluZ3MubW9kZV9sb2NrZWQpe3ZhciBtbj1nZSgibW9kZU5vdGUiKTtpZihtbil7bW4udGV4dENvbnRlbnQ9IlBSTyBsb2NrZWQg4oCUIG5lZWQgJDUwMCsgYmFsYW5jZSI7bW4uc3R5bGUuY29sb3I9InZhcigtLXIpIjt9fQogIH0KICB2YXIgb3Q9Z2UoInRvZ08iKTtpZihvdClvdC5jaGVja2VkPSEhcy5vcHRzX21vZGU7CiAgZ2UoIm9wdHNQYW5lbCIpLnN0eWxlLmRpc3BsYXk9cy5vcHRzX21vZGU/ImJsb2NrIjoibm9uZSI7CiAgaWYocy5ndWFyZHJhaWxzKXt2YXIgZ2s9T2JqZWN0LmtleXMocy5ndWFyZHJhaWxzKTt2YXIgZ2g9IiI7Z2suZm9yRWFjaChmdW5jdGlvbihrKXtnaCs9IjxkaXYgY2xhc3M9J2dyYWlsLXJvdyc+PHNwYW4gY2xhc3M9J2dyayc+IitrKyI8L3NwYW4+PHNwYW4gY2xhc3M9J2dydic+IitzLmd1YXJkcmFpbHNba10rIjwvc3Bhbj48L2Rpdj4iO30pO3NoKCJnckxpc3QiLGdoKTt9CiAgaWYocy5sb2dzKVNULmxvZ3M9cy5sb2dzO2lmKHMudHJhZGVzKVNULnRyYWRlcz1zLnRyYWRlczsKICBzdCgibENudCIsU1QubG9ncy5sZW5ndGgrIiBlbnRyaWVzIik7CiAgaWYoZ2UoInAtbG9ncyIpLmNsYXNzTGlzdC5jb250YWlucygic2hvdyIpKXJlbmRlckxvZ3MoKTsKICBpZihnZSgicC10cmFkZXMiKS5jbGFzc0xpc3QuY29udGFpbnMoInNob3ciKSlyZW5kZXJUcmFkZXMoKTsKfQpmdW5jdGlvbiByZW5kZXJUcmFkZXMoKXsKICBzdCgidENudCIsU1QudHJhZGVzLmxlbmd0aCsiIHRyYWRlcyIpOwogIGlmKCFTVC50cmFkZXMubGVuZ3RoKXtzaCgidExpc3QiLCI8ZGl2IGNsYXNzPSdlbXB0eSc+Tm8gdHJhZGVzIHlldDwvZGl2PiIpO3JldHVybjt9CiAgdmFyIGg9IiI7CiAgU1QudHJhZGVzLmZvckVhY2goZnVuY3Rpb24odCl7CiAgICB2YXIgb3Blbj10LmV4aXQ9PW51bGwsc2Q9dC5zaWRlfHwiIjsKICAgIHZhciBpYz1zZD09PSJsb25nIj8idGktbCI6c2Q9PT0ic2hvcnQiPyJ0aS1zIjpzZD09PSJjYWxsIj8idGktYyI6InRpLXAiOwogICAgdmFyIGljbz1zZD09PSJsb25nIj8iJiM4NTkzOyI6c2Q9PT0ic2hvcnQiPyImIzg1OTU7IjpzZD09PSJjYWxsIj8iQyI6IlAiOwogICAgdmFyIHBjPW9wZW4/InRwbiI6KHQud29uPyJ0cGciOiJ0cHIiKSxwdj1vcGVuPyJPcGVuXHUyMDI2IjoodC53b24/IisiOiIiKSsodC5wbmx8fDApLnRvRml4ZWQoNCk7CiAgICB2YXIgdG09dC50aW1lP3QudGltZS5zdWJzdHIoNSwxMSkucmVwbGFjZSgiVCIsIiAiKToiIjsKICAgIGgrPSI8ZGl2IGNsYXNzPSd0ci1yb3cnPjxkaXYgY2xhc3M9J3RpY28gIitpYysiJz4iK2ljbysiPC9kaXY+PGRpdiBjbGFzcz0ndG1pZCc+PGRpdiBjbGFzcz0ndHN5bSc+IisodC5zeW18fCJCVENVU0QiKSsiPC9kaXY+PGRpdiBjbGFzcz0ndG1ldGEnPiIrdG0rIiAmbWlkZG90OyAiKyh0LnJlYXNvbnx8IiIpKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSd0cmlnaHQnPjxkaXYgY2xhc3M9J3RwbmwgIitwYysiJz4kIitwdisiPC9kaXY+PGRpdiBzdHlsZT0nZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpJz4iKyh0LmVudHJ5PyJAJCIrdC5lbnRyeToiIikrIjwvZGl2PjwvZGl2PjwvZGl2PiI7CiAgfSk7c2goInRMaXN0IixoKTsKfQpmdW5jdGlvbiByZW5kZXJMb2dzKCl7CiAgdmFyIGY9U1QubGY/U1QubG9ncy5maWx0ZXIoZnVuY3Rpb24oZSl7cmV0dXJuIGUubD09PVNULmxmO30pOlNULmxvZ3M7CiAgdmFyIGg9IiI7Zi5zbGljZSgwLDE1MCkuZm9yRWFjaChmdW5jdGlvbihlKXt2YXIgY2xzPSJsSSI7aWYoZS5sPT09IldBUk4iKWNscz0ibFciO2Vsc2UgaWYoZS5sPT09IkVSUk9SIiljbHM9ImxFIjtlbHNlIGlmKGUubD09PSJUUkFERSIpY2xzPSJsVCI7aCs9IjxkaXYgY2xhc3M9J2xyJz48c3BhbiBjbGFzcz0nbHQnPiIrZS50KyI8L3NwYW4+PHNwYW4gY2xhc3M9JyIrY2xzKyInPiIrZS5tKyI8L3NwYW4+PC9kaXY+Ijt9KTtzaCgibEJveCIsaCk7Cn0KZnVuY3Rpb24gbG9hZEFkbWluKCl7CiAgaWYoIVNULmlzQWRtaW4pe2dlKCJhZG1pblBhbmVsIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7cmV0dXJuO30KICBnZSgiYWRtaW5QYW5lbCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsKICB4aHIoIi9hcGkvYWRtaW4vdXNlcnMiLG51bGwsZnVuY3Rpb24ocil7CiAgICBpZighcilyZXR1cm47CiAgICB2YXIgaD0iIjsKICAgIE9iamVjdC5rZXlzKHIudXNlcnN8fHt9KS5mb3JFYWNoKGZ1bmN0aW9uKHVpZCl7CiAgICAgIHZhciB1PXIudXNlcnNbdWlkXTsKICAgICAgaCs9IjxkaXYgY2xhc3M9J2F1Jz48ZGl2IGNsYXNzPSdhdS1uYW1lJz4iKyh1LmlzX2FkbWluPyImIzk3MzM7ICI6IiIpK3UudXNlcm5hbWUrKHUuYm90X3J1bm5pbmc/IiA8c3BhbiBzdHlsZT0nY29sb3I6dmFyKC0tZyk7Zm9udC1zaXplOjEwcHgnPiYjOTY3OTsgTGl2ZTwvc3Bhbj4iOiIgPHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLXQzKTtmb250LXNpemU6MTBweCc+T2ZmbGluZTwvc3Bhbj4iKSsiPC9kaXY+PGRpdiBjbGFzcz0nYXUtc3RhdHMnPjxzcGFuPiQiK3UuYmFsYW5jZS50b0ZpeGVkKDIpKyI8L3NwYW4+PHNwYW4+Iit1LnRyYWRlcysiIHRyYWRlczwvc3Bhbj48L2Rpdj48L2Rpdj4iOwogICAgfSk7CiAgICBzaCgiYXVMaXN0IixofHwiPGRpdiBjbGFzcz0nZW1wdHknPk5vIHVzZXJzIHlldDwvZGl2PiIpOwogICAgaWYoci5pbnZpdGVzJiZyLmludml0ZXMubGVuZ3RoKXt2YXIgaWg9IjxkaXYgc3R5bGU9J2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjRweCc+UGVuZGluZyBpbnZpdGUgY29kZXM6PC9kaXY+IjtyLmludml0ZXMuZm9yRWFjaChmdW5jdGlvbihjKXtpaCs9IjxkaXYgY2xhc3M9J2ljb2RlJz4iK2MrIjwvZGl2PiI7fSk7c2goIm5ld0ludml0ZSIsaWgpO2dlKCJuZXdJbnZpdGUiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7fQogIH0pOwp9CmZ1bmN0aW9uIGdlbkludml0ZSgpewogIHhocigiL2FwaS9hZG1pbi9pbnZpdGUiLHt9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXtzaCgiaW52Q29kZSIsci5jb2RlKTtnZSgiaW52Q29kZSIpLmNsYXNzTmFtZT0iaWNvZGUiO2dlKCJuZXdJbnZpdGUiKS5pbm5lckhUTUw9IjxkaXYgc3R5bGU9J2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjRweCc+TmV3IGludml0ZSBjb2RlOjwvZGl2PjxkaXYgY2xhc3M9J2ljb2RlJz4iK3IuY29kZSsiPC9kaXY+PGRpdiBzdHlsZT0nZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246Y2VudGVyJz5PbmUtdGltZSB1c2Ugb25seTwvZGl2PiI7Z2UoIm5ld0ludml0ZSIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtsb2FkQWRtaW4oKTt9CiAgfSk7Cn0KZnVuY3Rpb24gbG9hZElQKCl7CiAgeGhyKCIvYXBpL2lwIixudWxsLGZ1bmN0aW9uKHIpe3ZhciBpcD1yJiZyLmlwP3IuaXA6InVua25vd24iO3N0KCJzSVAiLGlwKTtzdCgic2lwQm94IixpcCk7fSk7Cn0Kc2V0SW50ZXJ2YWwoZnVuY3Rpb24oKXsKICBpZighU1QubmV4dEF0KXJldHVybjsKICB2YXIgZD1NYXRoLm1heCgwLE1hdGgucm91bmQoKFNULm5leHRBdC1EYXRlLm5vdygpKS8xMDAwKSk7CiAgdmFyIG09TWF0aC5mbG9vcihkLzYwKSxzPWQlNjA7c3QoInNjZCIsZD4wPyhtKyJtICIrcysicyIpOiJTY2FubmluZy4uLiIpOwogIGdlKCJzRmlsIikuc3R5bGUud2lkdGg9TWF0aC5tYXgoMCwxMDAtZC9TVC5zcyoxMDApKyIlIjsKfSwxMDAwKTsKZnVuY3Rpb24gcG9sbCgpe3hocigiL2FwaS9zdGF0dXMiLG51bGwsZnVuY3Rpb24ocyl7aWYocylyZW5kZXIocyk7fSk7fQpmdW5jdGlvbiB0cnlBdXRvQ29ubmVjdCgpewogIC8vIElmIGNvbm5lY3RlZCBjYXJkIHZpc2libGUgYW5kIGtleXMgc2F2ZWQsIHRyeSBjb25uZWN0aW5nIGF1dG9tYXRpY2FsbHkKICBpZihnZSgiY29ubmVjdENhcmQiKS5zdHlsZS5kaXNwbGF5IT09Im5vbmUiKXsKICAgIGxvYWRTYXZlZEtleXMoKTsKICAgIHZhciBrPWdlKCJjS2V5IikudmFsdWUudHJpbSgpLHM9Z2UoImNTZWMiKS52YWx1ZS50cmltKCk7CiAgICBpZihrJiZzKXsKICAgICAgZ2UoImNNc2ciKS50ZXh0Q29udGVudD0iQXV0by1jb25uZWN0aW5nLi4uIjsKICAgICAgc2V0VGltZW91dChmdW5jdGlvbigpe2RvQ29ubmVjdCgpO30sNTAwKTsKICAgIH0KICB9Cn0KZnVuY3Rpb24gY2xlYXJTdGFsZSgpewogIGlmKCFjb25maXJtKCJSZW1vdmUgYWxsIHN0YWxlL2dob3N0IHRyYWRlcyBmcm9tIGhpc3Rvcnk/IikpIHJldHVybjsKICB4aHIoIi9hcGkvY2xlYXJfc3RhbGUiLHt9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgYWxlcnQoIlJlbW92ZWQgIityLnJlbW92ZWQrIiBzdGFsZSB0cmFkZXMiKTsKICAgICAgcG9sbCgpOwogICAgfQogIH0pOwp9Cgp2YXIgX2xvdHM9MSxfZGFpbHk9MTAsX21vZGU9Im5vcm1hbCI7CmZ1bmN0aW9uIHNldE1vZGUobSl7CiAgX21vZGU9bTsKICBbInNhZmUiLCJub3JtYWwiLCJwcm8iXS5mb3JFYWNoKGZ1bmN0aW9uKHgpewogICAgdmFyIGVsPWdlKCJtb2RlLSIreCk7CiAgICBpZighZWwpcmV0dXJuOwogICAgdmFyIGFjdGl2ZT14PT09bTsKICAgIGlmKHg9PT0ic2FmZSIpe2VsLnN0eWxlLm9wYWNpdHk9YWN0aXZlPyIxIjoiMC40Ijt9CiAgICBlbHNlIGlmKHg9PT0ibm9ybWFsIil7ZWwuc3R5bGUub3BhY2l0eT1hY3RpdmU/IjEiOiIwLjQiO30KICAgIGVsc2V7ZWwuc3R5bGUub3BhY2l0eT1hY3RpdmU/IjEiOiIwLjQiO30KICAgIGVsLnN0eWxlLnRyYW5zZm9ybT1hY3RpdmU/InNjYWxlKDEuMDUpIjoic2NhbGUoMSkiOwogIH0pOwogIHZhciBub3RlPWdlKCJtb2RlTm90ZSIpOwogIGlmKG5vdGUpewogICAgaWYobT09PSJzYWZlIikgbm90ZS50ZXh0Q29udGVudD0iQ29uc2VydmF0aXZlIOKAlCBiZXN0IGZvciBsZWFybmluZy4gU21hbGwgZ2FpbnMsIHNtYWxsIGxvc3Nlcy4iOwogICAgZWxzZSBpZihtPT09Im5vcm1hbCIpIG5vdGUudGV4dENvbnRlbnQ9IkJhbGFuY2VkIOKAlCByZWNvbW1lbmRlZC4gR29vZCByaXNrL3Jld2FyZCByYXRpby4iOwogICAgZWxzZSBub3RlLnRleHRDb250ZW50PSLimqDvuI8gUFJPIHJlcXVpcmVzICQ1MDArIGJhbGFuY2UuIEhpZ2hlciByaXNrLCBoaWdoZXIgcmV3YXJkLiI7CiAgICBub3RlLnN0eWxlLmNvbG9yPW09PT0icHJvIj8idmFyKC0teSkiOiJ2YXIoLS10MykiOwogIH0KfQpmdW5jdGlvbiBhZGpMb3RzKGQpewogIF9sb3RzPU1hdGgubWF4KDEsTWF0aC5taW4oMTAwLF9sb3RzK2QpKTsKICBnZSgibG90c1ZhbCIpLnRleHRDb250ZW50PV9sb3RzOwogIHZhciBlbD1nZSgibG90QnRjVmFsIik7CiAgaWYoZWwpIGVsLnRleHRDb250ZW50PV9sb3RzKyIgbG90cyA9ICIrKF9sb3RzKjAuMDAxKS50b0ZpeGVkKDMpKyIgQlRDIjsKfQpmdW5jdGlvbiBhZGpEYWlseShkKXtfZGFpbHk9TWF0aC5tYXgoMSxNYXRoLm1pbig1MCxfZGFpbHkrZCkpO2dlKCJkYWlseVZhbCIpLnRleHRDb250ZW50PV9kYWlseTt9CmZ1bmN0aW9uIHNhdmVVc2VyU2V0dGluZ3MoKXsKICB4aHIoIi9hcGkvdXNlci9zZXR0aW5ncyIse2xvdF9zaXplOl9sb3RzLG1heF9kYWlseTpfZGFpbHksbW9kZTpfbW9kZX0sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpewogICAgICBnZSgic2V0TXNnIikudGV4dENvbnRlbnQ9IlNhdmVkISBNb2RlOiAiK3IubW9kZS50b1VwcGVyQ2FzZSgpOwogICAgICBnZSgic2V0TXNnIikuc3R5bGUuY29sb3I9InZhcigtLWcpIjsKICAgICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dlKCJzZXRNc2ciKS50ZXh0Q29udGVudD0iIjt9LDMwMDApOwogICAgfSBlbHNlIGlmKHImJnIubWVzc2FnZSl7CiAgICAgIGdlKCJzZXRNc2ciKS50ZXh0Q29udGVudD1yLm1lc3NhZ2U7CiAgICAgIGdlKCJzZXRNc2ciKS5zdHlsZS5jb2xvcj0idmFyKC0tcikiOwogICAgfQogIH0pOwp9Ci8vIE9uIGxvYWQ6IGNoZWNrIGlmIGFscmVhZHkgbG9nZ2VkIGluCnhocigiL2F1dGgvbWUiLG51bGwsZnVuY3Rpb24ocil7CiAgaWYociYmci5sb2dnZWRfaW4pe1NULmlzQWRtaW49ci5pc19hZG1pbjtzdCgidUJhZGdlIixyLnVzZXJuYW1lKTtzaG93QXBwKCk7bG9hZElQKCk7cG9sbCgpO30KICBlbHNle3Nob3dBdXRoKCk7c2V0VGltZW91dChmdW5jdGlvbigpe2xvYWRTYXZlZEtleXMoKTt9LDEwMCk7fQp9KTsKc2V0SW50ZXJ2YWwoZnVuY3Rpb24oKXtpZihnZSgiYXBwIikuc3R5bGUuZGlzcGxheSE9PSJub25lIilwb2xsKCk7fSw0MDAwKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPg==").decode("utf-8")

@app.route("/")
@app.route("/login")
def index(): return Response(_DASH, mimetype="text/html")

if __name__ == "__main__":
    if "--setup" in sys.argv:
        code,_=um.gen_invite(); print(f"Invite: {code}"); sys.exit()
    port=int(os.getenv("PORT",5000))
    # use_reloader=False prevents double-process on startup
    app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False,threaded=True)
