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
BOT_SECRET = os.getenv("BOT_SECRET", secrets.token_hex(32))

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
    CONF_MACRO   = 45   # when macro agrees (lowered: 48 was blocking valid signals)
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
        at_dip        = r5 < 50 and price_bb_pos < 0.4       # real dip, near lower band
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
        if regime in ("STRONG_BULL","BULL") and adx_v>25 and r5<52:
            direction="long"; conviction=62; opt_conviction="atm"
        elif regime in ("STRONG_BEAR","BEAR") and adx_v>25 and r5>48:
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
    # Confidence threshold — must be defined before funding veto uses it
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

    brain.update({"direction":direction,"conviction":conviction,"strategy":strategy,
                  "opt_type":"call" if direction=="long" else "put",
                  "opt_conviction":opt_conviction,"scale":scale,
                  "tf_bull":tf_bull_count,"tf_bear":tf_bear_count,
                  "tf_total":len(all_trends),
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
            "opt_score":b.brain.get("opt_score",0) if b.brain else 0,
            "opt_pillars":b.brain.get("opt_pillars",{}) if b.brain else {},
            "at_dip":b.brain.get("at_dip",False) if b.brain else False,
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
    if not ok: return jsonify({"success":False,"message":"Wrong username or password"}),401
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
    # Auto-reconnect if disconnected but keys saved
    if not b.connected:
        _try_auto_connect(uid, b)
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

@app.route("/api/opts/expiries",methods=["GET"])
@login_req
def api_opts_expiries():
    """Get available expiries for BTC options."""
    b=get_bot(session["uid"])
    if not b.opts: return jsonify({"error":"Not connected"})
    result=b.api.get("/v2/products",{"contract_type":"call_options","state":"live"})
    if not result or not result.get("success"):
        return jsonify({"expiries":[]})
    seen=set(); expiries=[]
    for p in result.get("result",[]):
        sym=p.get("symbol","")
        if not sym.startswith("C-BTC-"): continue
        parts=sym.split("-")
        if len(parts)<4: continue
        exp=parts[3]
        if exp not in seen:
            seen.add(exp)
            # Format: DDMMYY → readable
            try:
                from datetime import datetime as dt2
                d=dt2.strptime(exp,"%d%m%y")
                label=d.strftime("%d %b")
                days=(d-dt2.now()).days
                expiries.append({"code":exp,"label":label,"days":days})
            except:
                expiries.append({"code":exp,"label":exp,"days":0})
    expiries.sort(key=lambda x:x["days"])
    return jsonify({"expiries":expiries[:6]})  # next 6 expiries


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
_DASH = _b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEsbWF4aW11bS1zY2FsZT0xIj4KPHRpdGxlPkFscGhhIEJvdDwvdGl0bGU+CjxzdHlsZT4KKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowOy13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjp0cmFuc3BhcmVudH0KOnJvb3R7LS1nOiMwMGIzODY7LS1nYjojZThmOWYzOy0tZ2Q6I2E3ZjNkMDstLXI6I2U3NGMzYzstLXJiOiNmZWYyZjI7LS1yZDojZmNhNWE1Oy0teTojZjU5ZTBiOy0teWI6I2ZlZjNjNzstLWI6IzNiODJmNjstLWJiOiNlZmY2ZmY7LS10OiMwZjE3MmE7LS10MjojNjQ3NDhiOy0tdDM6Izk0YTNiODstLWJnOiNmMGYyZjU7LS13OiNmZmY7LS1iZHI6MXB4IHNvbGlkICNlMmU4ZjB9CmJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0tdCk7Zm9udC1mYW1pbHk6LWFwcGxlLXN5c3RlbSxCbGlua01hY1N5c3RlbUZvbnQsIlNlZ29lIFVJIixIZWx2ZXRpY2EsQXJpYWwsc2Fucy1zZXJpZjtmb250LXNpemU6MTRweDttaW4taGVpZ2h0OjEwMHZofQovKiBBVVRIICovCi5hdXRoLXdyYXB7bWluLWhlaWdodDoxMDB2aDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQouYXV0aC1jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxNnB4O3BhZGRpbmc6MjhweDt3aWR0aDoxMDAlO21heC13aWR0aDozODBweDtib3gtc2hhZG93OjAgNHB4IDI0cHggcmdiYSgwLDAsMCwuMDgpfQouYXV0aC1sb2dve2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToyMHB4fQouYXV0aC1pY297d2lkdGg6NDBweDtoZWlnaHQ6NDBweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Y29sb3I6I2ZmZjtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDB9Ci5hdXRoLXRpdGxle2ZvbnQtc2l6ZToyMnB4O2ZvbnQtd2VpZ2h0OjgwMH0uYXV0aC1zdWIye2ZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKX0KLmF1dGgtZGVzY3tmb250LXNpemU6MTNweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbToxOHB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlucHt3aWR0aDoxMDAlO2JvcmRlcjp2YXIoLS1iZHIpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTFweCAxM3B4O2ZvbnQtc2l6ZToxNHB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7b3V0bGluZTpub25lO2JhY2tncm91bmQ6I2Y4ZmFmYzttYXJnaW4tYm90dG9tOjEwcHh9Ci5pbnA6Zm9jdXN7Ym9yZGVyLWNvbG9yOnZhcigtLWcpO2JhY2tncm91bmQ6I2ZmZn0KLmF1dGgtYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmF1dGgtYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmF1dGgtbXNne3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O21hcmdpbi10b3A6MTBweDttaW4taGVpZ2h0OjIwcHg7bGluZS1oZWlnaHQ6MS43fQouYXV0aC1tc2cub2t7Y29sb3I6dmFyKC0tZyl9LmF1dGgtbXNnLmVycntjb2xvcjp2YXIoLS1yKX0KLmF1dGgtc3dpdGNoe3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjE0cHh9Ci5hdXRoLXN3aXRjaCBhe2NvbG9yOnZhcigtLWIpO2N1cnNvcjpwb2ludGVyO2ZvbnQtd2VpZ2h0OjYwMH0KLyogTUFJTiBBUFAgKi8KI2FwcHtkaXNwbGF5Om5vbmV9Ci5oZHJ7YmFja2dyb3VuZDp2YXIoLS13KTtwYWRkaW5nOjAgMTZweDtoZWlnaHQ6NTRweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTtwb3NpdGlvbjpzdGlja3k7dG9wOjA7ei1pbmRleDoxMDA7Ym94LXNoYWRvdzowIDFweCA0cHggcmdiYSgwLDAsMCwuMDYpfQoubG9nb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHh9Ci5saWN7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6OXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtjb2xvcjojZmZmO2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMH0KLmxue2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjcwMH0ubHN7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpfQouaHJpZ2h0e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KLnViYWRnZXtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3BhZGRpbmc6NHB4IDEwcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6MjBweDtib3JkZXI6dmFyKC0tYmRyKX0KLnBpbGx7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4O3BhZGRpbmc6NXB4IDEycHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnAtbGl2ZXtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0ucC1vZmZ7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcil9LnAtd2FybntiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KX0KLndyYXB7cGFkZGluZzoxMnB4IDE0cHggOTBweDttYXgtd2lkdGg6NDgwcHg7bWFyZ2luOjAgYXV0b30KLnBhZ2V7ZGlzcGxheTpub25lfS5wYWdlLnNob3d7ZGlzcGxheTpibG9ja30KLm5hdntwb3NpdGlvbjpmaXhlZDtib3R0b206MDtsZWZ0OjA7cmlnaHQ6MDtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci10b3A6dmFyKC0tYmRyKTtkaXNwbGF5OmZsZXg7cGFkZGluZzo4cHggMCBtYXgoOHB4LGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7ei1pbmRleDo5OX0KLm5ie2ZsZXg6MTtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6M3B4O3BhZGRpbmc6NHB4IDA7Ym9yZGVyOm5vbmU7YmFja2dyb3VuZDpub25lO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5uYiAuaWN7Zm9udC1zaXplOjIwcHg7Y29sb3I6dmFyKC0tdDMpfS5uYiAubGJ7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDMpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4fQoubmIub24gLmljLC5uYi5vbiAubGJ7Y29sb3I6dmFyKC0tdCl9Ci5jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTZweDttYXJnaW4tYm90dG9tOjEwcHg7Ym94LXNoYWRvdzowIDFweCAzcHggcmdiYSgwLDAsMCwuMDUpLDAgMnB4IDhweCByZ2JhKDAsMCwwLC4wNCl9Ci5jdHtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206MTJweH0KLyogQ09OTkVDVCBDQVJEICovCi5jY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNjBkZWcsIzBmMTcyYSwjMWUzYTVmKTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMnB4O21hcmdpbi1ib3R0b206MTBweH0KLmN0aXRsZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo4MDA7Y29sb3I6I2ZmZjttYXJnaW4tYm90dG9tOjZweH0KLmNzdWJ7Zm9udC1zaXplOjEycHg7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSk7bWFyZ2luLWJvdHRvbToxNnB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlwLXJvd3tiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7bWFyZ2luLWJvdHRvbToxNHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW59Ci5pcC1sYmx7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjQpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206NHB4fQouaXAtdmFse2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTZweDtmb250LXdlaWdodDo3MDA7Y29sb3I6I2ZmZjtsZXR0ZXItc3BhY2luZzoxcHh9Ci5pcC1jb3B5e2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTIpO2JvcmRlcjpub25lO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6OHB4IDE0cHg7Y29sb3I6I2ZmZjtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmNpbnB7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA4KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7Zm9udC1zaXplOjE0cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtjb2xvcjojZmZmO21hcmdpbi1ib3R0b206MTBweDtvdXRsaW5lOm5vbmV9Ci5jaW5wOmZvY3Vze2JvcmRlci1jb2xvcjp2YXIoLS1nKX0uY2lucDo6cGxhY2Vob2xkZXJ7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMyl9Ci5jYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6MTBweDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLWcpO2NvbG9yOiNmZmY7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5jYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmNtc2d7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjEycHg7bWFyZ2luLXRvcDoxMHB4O21pbi1oZWlnaHQ6MjBweDtsaW5lLWhlaWdodDoxLjd9Ci8qIEhFUk8gKi8KLmhlcm97YmFja2dyb3VuZDp2YXIoLS10KTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMHB4O21hcmdpbi1ib3R0b206MTBweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59Ci5oZXJvOjphZnRlcntjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO3RvcDotNDBweDtyaWdodDotNDBweDt3aWR0aDoxNjBweDtoZWlnaHQ6MTYwcHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5obHtmb250LXNpemU6MTBweDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC40KTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjhweDttYXJnaW4tYm90dG9tOjVweH0KLmhwe2ZvbnQtc2l6ZTo0MHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjojZmZmO2xpbmUtaGVpZ2h0OjE7bGV0dGVyLXNwYWNpbmc6LTEuNXB4fQouaHIye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDttYXJnaW4tdG9wOjlweDtmbGV4LXdyYXA6d3JhcH0KLmNoaXB7cGFkZGluZzozcHggMTBweDtib3JkZXItcmFkaXVzOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDB9Ci5jZ3tiYWNrZ3JvdW5kOnJnYmEoMCwyMDAsMTUwLC4yKTtjb2xvcjojMDBlOGIwfS5jcjJ7YmFja2dyb3VuZDpyZ2JhKDIzMSw3Niw2MCwuMik7Y29sb3I6I2ZmODA4MH0uY257YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSl9Ci5yYmFye3BhZGRpbmc6OXB4IDE0cHg7Ym9yZGVyLXJhZGl1czo4cHg7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnJiLWJ7YmFja2dyb3VuZDp2YXIoLS1nYik7Y29sb3I6IzA1OTY2OTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWdkKX0ucmItcntiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjojZGMyNjI2O2JvcmRlcjoxcHggc29saWQgdmFyKC0tcmQpfS5yYi1ue2JhY2tncm91bmQ6I2Y4ZmFmYztjb2xvcjp2YXIoLS10Mik7Ym9yZGVyOnZhcigtLWJkcil9LnJiLXd7YmFja2dyb3VuZDp2YXIoLS15Yik7Y29sb3I6IzkyNDAwZTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci8qIENPTkZJREVOQ0UgKi8KLmN3e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjE0cHg7cGFkZGluZzo0cHggMH0KLmNybmd7cG9zaXRpb246cmVsYXRpdmU7d2lkdGg6NzJweDtoZWlnaHQ6NzJweDtmbGV4LXNocmluazowfQouY3JuZyBzdmd7dHJhbnNmb3JtOnJvdGF0ZSgtOTBkZWcpO2Rpc3BsYXk6YmxvY2t9Ci5jb3Z7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQouY251bXtmb250LXNpemU6MjJweDtmb250LXdlaWdodDo4MDA7bGluZS1oZWlnaHQ6MX0uY2Rlbntmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTtmb250LXdlaWdodDo3MDB9Ci5jbXR7ZmxleDoxfS5jZGlye2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tYm90dG9tOjNweH0uY2RldHtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Mil9Ci5waWxsYXJze21hcmdpbi10b3A6MTJweH0KLnByb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O3BhZGRpbmc6N3B4IDA7Ym9yZGVyLWJvdHRvbTp2YXIoLS1iZHIpfS5wcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5wbnt3aWR0aDo4NnB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7ZmxleC1zaHJpbms6MH0KLnB0e2ZsZXg6MTtoZWlnaHQ6NXB4O2JhY2tncm91bmQ6I2YxZjVmOTtib3JkZXItcmFkaXVzOjNweDtvdmVyZmxvdzpoaWRkZW59LnBme2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6M3B4O3RyYW5zaXRpb246d2lkdGggLjVzfQoucHN7d2lkdGg6MzZweDt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQouaW5kc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLXRvcDoxMHB4fQouaW5ke2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjEwcHg7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOnZhcigtLWJkcil9Ci5pbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTozcHh9Lml2e2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMH0KLnNiYXJ7aGVpZ2h0OjNweDtiYWNrZ3JvdW5kOiNlMmU4ZjA7Ym9yZGVyLXJhZGl1czoycHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbi10b3A6OXB4fS5zZmlse2hlaWdodDoxMDAlO2JhY2tncm91bmQ6dmFyKC0tYik7Ym9yZGVyLXJhZGl1czoycHg7dHJhbnNpdGlvbjp3aWR0aCAuNXN9Ci5zcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbjtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDo0cHh9Ci8qIFBPU0lUSU9OUyAqLwoucG9ze2JvcmRlci1yYWRpdXM6MTJweDtwYWRkaW5nOjE0cHg7bWFyZ2luLWJvdHRvbToxMHB4fQoucG9zLWx7YmFja2dyb3VuZDojZjBmZGY0O2JvcmRlcjoxcHggc29saWQgdmFyKC0tZ2QpfS5wb3Mtc3tiYWNrZ3JvdW5kOiNmZmY1ZjU7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1yZCl9LnBvcy1ve2JhY2tncm91bmQ6dmFyKC0tYmIpO2JvcmRlcjoxcHggc29saWQgIzkzYzVmZH0KLnBoe2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxMHB4fS5wc3lte2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJhZGdle3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJse2JhY2tncm91bmQ6dmFyKC0tZyk7Y29sb3I6I2ZmZn0uYnNoe2JhY2tncm91bmQ6dmFyKC0tcik7Y29sb3I6I2ZmZn0uYmN7YmFja2dyb3VuZDp2YXIoLS1iKTtjb2xvcjojZmZmfS5icHtiYWNrZ3JvdW5kOiM4YjVjZjY7Y29sb3I6I2ZmZn0KLnBne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6NnB4fQoucGl7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC43NSk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo4cHh9LnBpbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi40cHg7bWFyZ2luLWJvdHRvbToycHh9LnBpdntmb250LXNpemU6MTRweDtmb250LXdlaWdodDo4MDB9LnBpZ3tjb2xvcjp2YXIoLS1nKX0ucGlye2NvbG9yOnZhcigtLXIpfQovKiBXQUxMRVQgKi8KLnd0e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQoud2x7ZmxleDoxfS53bGJ7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQzKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjVweDttYXJnaW4tYm90dG9tOjRweH0KLndhe2ZvbnQtc2l6ZTozMnB4O2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzotMXB4fS53c3tmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHh9Ci53cHtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDA7dGV4dC1hbGlnbjpyaWdodH0ud257Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246cmlnaHQ7bWFyZ2luLXRvcDoycHh9Ci8qIFNUQVRTICovCi5zZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouc3RhdHtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKX0KLnN0bHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTo0cHh9LnN0dntmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDB9Ci5iM3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouYnRue3BhZGRpbmc6MTNweCA2cHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2dhcDo1cHh9LmJ0bjphY3RpdmV7b3BhY2l0eTouOH0KLmJke2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZn0uYnIze2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1yZCl9LmJiM3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtib3JkZXI6MS41cHggc29saWQgI2JmZGJmZX0KLmJjYXtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MS41cHggc29saWQgdmFyKC0tcmQpO3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6OHB4fQovKiBPUFRJT05TICovCi50b2dyb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjEwcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7bWFyZ2luLWJvdHRvbToxMnB4fQoudGx7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6NzAwfS50czN7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4fQoudG9ne3Bvc2l0aW9uOnJlbGF0aXZlO3dpZHRoOjQ2cHg7aGVpZ2h0OjI2cHg7ZmxleC1zaHJpbms6MDtjdXJzb3I6cG9pbnRlcn0KLnRvZyBpbnB1dHtvcGFjaXR5OjA7d2lkdGg6MDtoZWlnaHQ6MDtwb3NpdGlvbjphYnNvbHV0ZX0KLnRvZ3Nse3Bvc2l0aW9uOmFic29sdXRlO2luc2V0OjA7YmFja2dyb3VuZDojZTJlOGYwO2JvcmRlci1yYWRpdXM6MTNweDt0cmFuc2l0aW9uOi4yc30KLnRvZ3NsOjpiZWZvcmV7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoyMHB4O2hlaWdodDoyMHB4O2xlZnQ6M3B4O2JvdHRvbTozcHg7YmFja2dyb3VuZDojZmZmO2JvcmRlci1yYWRpdXM6NTAlO3RyYW5zaXRpb246LjJzO2JveC1zaGFkb3c6MCAxcHggM3B4IHJnYmEoMCwwLDAsLjIpfQoudG9nIGlucHV0OmNoZWNrZWQrLnRvZ3Nse2JhY2tncm91bmQ6dmFyKC0tZyl9LnRvZyBpbnB1dDpjaGVja2VkKy50b2dzbDo6YmVmb3Jle3RyYW5zZm9ybTp0cmFuc2xhdGVYKDIwcHgpfQoub2luZm97ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjtnYXA6OHB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTBweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTttYXJnaW4tYm90dG9tOjEycHg7Zm9udC1zaXplOjExcHh9Ci5vYntkaXNwbGF5OmZsZXg7Z2FwOjhweH0KLm9iYnRue2ZsZXg6MTtwYWRkaW5nOjEwcHg7Ym9yZGVyLXJhZGl1czo4cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXJ9Ci5vYi1je2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpO2JvcmRlcjoxcHggc29saWQgI2JmZGJmZX0ub2ItcHtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXJkKX0ub2Itc3tiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci5vcmVze21hcmdpbi10b3A6MTBweDtwYWRkaW5nOjExcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2ZvbnQtc2l6ZToxMXB4O2xpbmUtaGVpZ2h0OjEuODtib3JkZXI6dmFyKC0tYmRyKTtkaXNwbGF5Om5vbmV9Ci5tcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi10b3A6OHB4fQouYnRubHtmbGV4OjE7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1nKTtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjgwMDtjdXJzb3I6cG9pbnRlcn0KLmJ0bnMye2ZsZXg6MTtwYWRkaW5nOjEzcHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXIpO2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyfQovKiBUUkFERVMgKi8KLnRyLXJvd3twYWRkaW5nOjExcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweH0udHItcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci50aWNve3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo5cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQoudGktbHtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0udGktc3tiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKX0udGktY3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKX0udGktcHtiYWNrZ3JvdW5kOiNmM2U4ZmY7Y29sb3I6IzdjM2FlZH0KLnRtaWR7ZmxleDoxO21pbi13aWR0aDowfS50c3lte2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0udG1ldGF7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4O3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KLnRyaWdodHt0ZXh0LWFsaWduOnJpZ2h0O2ZsZXgtc2hyaW5rOjB9LnRwbmx7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwfS50cGd7Y29sb3I6dmFyKC0tZyl9LnRwcntjb2xvcjp2YXIoLS1yKX0udHBue2NvbG9yOnZhcigtLXQzKX0KLyogTE9HUyAqLwoubGZ7ZGlzcGxheTpmbGV4O2dhcDo2cHg7bWFyZ2luLWJvdHRvbTo4cHh9Ci5sZmJ7cGFkZGluZzo0cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDp2YXIoLS13KTtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtZmFtaWx5OmluaGVyaXR9LmxmYi5vbntiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmY7Ym9yZGVyLWNvbG9yOnZhcigtLXQpfQoubGJveHtiYWNrZ3JvdW5kOiMwZjE3MmE7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzoxMnB4O21heC1oZWlnaHQ6NDAwcHg7b3ZlcmZsb3cteTphdXRvfQoubHJ7cGFkZGluZzo0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMWUyOTNiO2ZvbnQtc2l6ZToxMXB4O2Rpc3BsYXk6ZmxleDtnYXA6OHB4O2ZvbnQtZmFtaWx5Om1vbm9zcGFjZX0KLmx0e2NvbG9yOiM0NzU1Njk7d2hpdGUtc3BhY2U6bm93cmFwO2ZsZXgtc2hyaW5rOjB9LmxJe2NvbG9yOiM2NDc0OGJ9LmxXe2NvbG9yOnZhcigtLXkpfS5sRXtjb2xvcjp2YXIoLS1yKX0ubFR7Y29sb3I6dmFyKC0tZyk7Zm9udC13ZWlnaHQ6NzAwfQovKiBTRVRUSU5HUyAqLwouZ3JhaWwtcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjlweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKX0uZ3JhaWwtcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5ncmt7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tdDIpfS5ncnZ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWcpO3RleHQtYWxpZ246cmlnaHQ7bWF4LXdpZHRoOjYwJX0KLmRjLWJ0bnt3aWR0aDoxMDAlO3BhZGRpbmc6MTJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6NnB4fQovKiBPUFRJT05TIENIQUlOICovCi5jaGFpbi1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMmZyIDFmciAxZnI7Z2FwOjZweDtwYWRkaW5nOjdweCA4cHg7Ym9yZGVyLXJhZGl1czo4cHg7Y3Vyc29yOnBvaW50ZXI7bWFyZ2luLWJvdHRvbTo0cHg7YWxpZ24taXRlbXM6Y2VudGVyO2JvcmRlcjoxLjVweCBzb2xpZCB0cmFuc3BhcmVudDt0cmFuc2l0aW9uOmJvcmRlciAuMTVzLGJhY2tncm91bmQgLjE1c30KLmNoYWluLXJvdzpob3ZlcntiYWNrZ3JvdW5kOiNmMGYyZjV9Ci5jaGFpbi1yb3cuc2VsLWN7YmFja2dyb3VuZDp2YXIoLS1iYik7Ym9yZGVyLWNvbG9yOiM5M2M1ZmR9Ci5jaGFpbi1yb3cuc2VsLXB7YmFja2dyb3VuZDp2YXIoLS1yYik7Ym9yZGVyLWNvbG9yOiNmY2E1YTV9Ci5jaGFpbi1yb3cuYXRte2JhY2tncm91bmQ6dmFyKC0teWIpO2JvcmRlci1jb2xvcjojZmRlNjhhfQouY2t7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjcwMDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjRweH0KLmN2e2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMH0KLmV4cC1idG57cGFkZGluZzo1cHggMTBweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDojZjhmYWZjO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO2NvbG9yOnZhcigtLXQyKX0KLmV4cC1idG4uc2Vse2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtib3JkZXItY29sb3I6dmFyKC0tdCl9Ci8qIEFETUlOICovCi5hdXtiYWNrZ3JvdW5kOiNmOGZhZmM7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzoxMnB4O21hcmdpbi1ib3R0b206OHB4O2JvcmRlcjp2YXIoLS1iZHIpfQouYXUtbmFtZXtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo3MDA7bWFyZ2luLWJvdHRvbTo0cHh9Ci5hdS1zdGF0c3tkaXNwbGF5OmZsZXg7Z2FwOjEycHg7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpfQouaWNvZGV7Zm9udC1mYW1pbHk6bW9ub3NwYWNlO2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjcwMDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjEycHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2xldHRlci1zcGFjaW5nOjJweDttYXJnaW46OHB4IDB9Ci5pcGJveHtmb250LWZhbWlseTptb25vc3BhY2U7Zm9udC1zaXplOjE2cHg7Zm9udC13ZWlnaHQ6NzAwO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTNweDtiYWNrZ3JvdW5kOiNmOGZhZmM7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOnZhcigtLWJkcik7bGV0dGVyLXNwYWNpbmc6MnB4O21hcmdpbi1ib3R0b206MTBweH0KLmVtcHR5e3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MjhweDtjb2xvcjp2YXIoLS10Myk7Zm9udC1zaXplOjEzcHh9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+Cgo8IS0tIOKVkOKVkOKVkCBBVVRIIFNDUkVFTiDilZDilZDilZAgLS0+CjxkaXYgaWQ9ImF1dGhTY3JlZW4iIGNsYXNzPSJhdXRoLXdyYXAiPgogIDxkaXYgY2xhc3M9ImF1dGgtY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJhdXRoLWxvZ28iPgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLWljbyI+JiM5MTY7PC9kaXY+CiAgICAgIDxkaXY+PGRpdiBjbGFzcz0iYXV0aC10aXRsZSI+QWxwaGEgQm90PC9kaXY+PGRpdiBjbGFzcz0iYXV0aC1zdWIyIj5EZWx0YSBFeGNoYW5nZSBJbmRpYTwvZGl2PjwvZGl2PgogICAgPC9kaXY+CgogICAgPCEtLSBMb2dpbiBmb3JtIC0tPgogICAgPGRpdiBpZD0ibG9naW5Gb3JtIj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1kZXNjIj5TaWduIGluIHRvIHlvdXIgdHJhZGluZyBhY2NvdW50PC9kaXY+CiAgICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0ibFVzZXIiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSJVc2VybmFtZSIgYXV0b2NvbXBsZXRlPSJ1c2VybmFtZSIgYXV0b2NvcnJlY3Q9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im5vbmUiPgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9ImxQYXNzIiB0eXBlPSJwYXNzd29yZCIgcGxhY2Vob2xkZXI9IlBhc3N3b3JkIiBhdXRvY29tcGxldGU9ImN1cnJlbnQtcGFzc3dvcmQiPgogICAgICA8YnV0dG9uIGNsYXNzPSJhdXRoLWJ0biIgb25jbGljaz0iZG9Mb2dpbigpIj5TaWduIEluPC9idXR0b24+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtbXNnIiBpZD0ibE1zZyI+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtc3dpdGNoIj5IYXZlIGFuIGludml0ZSBjb2RlPyA8YSBvbmNsaWNrPSJzaG93UmVnKCkiPlJlZ2lzdGVyIGhlcmU8L2E+PC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8IS0tIFJlZ2lzdGVyIGZvcm0gLS0+CiAgICA8ZGl2IGlkPSJyZWdGb3JtIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1kZXNjIj5FbnRlciB5b3VyIGludml0ZSBjb2RlIHRvIGNyZWF0ZSBhbiBhY2NvdW50PC9kaXY+CiAgICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0ickludiIgIHR5cGU9InRleHQiICAgICBwbGFjZWhvbGRlcj0iSW52aXRlIGNvZGUiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJub25lIj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJyVXNlciIgdHlwZT0idGV4dCIgICAgIHBsYWNlaG9sZGVyPSJDaG9vc2UgYSB1c2VybmFtZSIgYXV0b2NvcnJlY3Q9Im9mZiIgYXV0b2NhcGl0YWxpemU9Im5vbmUiPgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9InJQYXNzIiB0eXBlPSJwYXNzd29yZCIgcGxhY2Vob2xkZXI9IkNob29zZSBhIHBhc3N3b3JkIChtaW4gNiBjaGFycykiPgogICAgICA8YnV0dG9uIGNsYXNzPSJhdXRoLWJ0biIgb25jbGljaz0iZG9SZWdpc3RlcigpIj5DcmVhdGUgQWNjb3VudDwvYnV0dG9uPgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLW1zZyIgaWQ9InJNc2ciPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLXN3aXRjaCI+QWxyZWFkeSByZWdpc3RlcmVkPyA8YSBvbmNsaWNrPSJzaG93TG9naW4oKSI+U2lnbiBpbjwvYT48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjwhLS0g4pWQ4pWQ4pWQIE1BSU4gQVBQIOKVkOKVkOKVkCAtLT4KPGRpdiBpZD0iYXBwIj4KPGRpdiBjbGFzcz0iaGRyIj4KICA8ZGl2IGNsYXNzPSJsb2dvIj48ZGl2IGNsYXNzPSJsaWMiPiYjOTE2OzwvZGl2PjxkaXY+PGRpdiBjbGFzcz0ibG4iPkFscGhhIEJvdDwvZGl2PjxkaXYgY2xhc3M9ImxzIj5EZWx0YSBFeGNoYW5nZSBJbmRpYTwvZGl2PjwvZGl2PjwvZGl2PgogIDxkaXYgY2xhc3M9ImhyaWdodCI+CiAgICA8c3BhbiBjbGFzcz0idWJhZGdlIiBpZD0idUJhZGdlIj4tLTwvc3Bhbj4KICAgIDxkaXYgY2xhc3M9InBpbGwgcC1vZmYiIGlkPSJzUGlsbCI+JiM5Njc5OyA8c3BhbiBpZD0ic1R4dCI+U3RvcHBlZDwvc3Bhbj48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8ZGl2IGNsYXNzPSJ3cmFwIj4KCjwhLS0gSE9NRSAtLT4KPGRpdiBjbGFzcz0icGFnZSBzaG93IiBpZD0icC1ob21lIj4KCiAgPCEtLSBDb25uZWN0IGNhcmQgLS0+CiAgPGRpdiBpZD0iY29ubmVjdENhcmQiIGNsYXNzPSJjY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJjdGl0bGUiPkNvbm5lY3QgdG8gRGVsdGEgRXhjaGFuZ2U8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNzdWIiPllvdXIgQVBJIGtleXMgYXJlIHN0b3JlZCBvbmx5IGluIHlvdXIgYnJvd3NlciBzZXNzaW9uIOKAlCBuZXZlciBzYXZlZCBvbiB0aGUgc2VydmVyLjwvZGl2PgogICAgPGRpdiBjbGFzcz0iaXAtcm93Ij4KICAgICAgPGRpdj48ZGl2IGNsYXNzPSJpcC1sYmwiPlNlcnZlciBJUCDigJQgd2hpdGVsaXN0IG9uIERlbHRhIGZpcnN0PC9kaXY+PGRpdiBjbGFzcz0iaXAtdmFsIiBpZD0ic0lQIj5Mb2FkaW5nLi4uPC9kaXY+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9ImlwLWNvcHkiIG9uY2xpY2s9ImNvcHlJUCgpIj5Db3B5PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxpbnB1dCBjbGFzcz0iY2lucCIgaWQ9ImNLZXkiIHR5cGU9InRleHQiICAgICBwbGFjZWhvbGRlcj0iQVBJIEtleSIgICAgYXV0b2NvbXBsZXRlPSJvZmYiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJub25lIj4KICAgIDxpbnB1dCBjbGFzcz0iY2lucCIgaWQ9ImNTZWMiIHR5cGU9InBhc3N3b3JkIiBwbGFjZWhvbGRlcj0iQVBJIFNlY3JldCI+CiAgICA8YnV0dG9uIGNsYXNzPSJjYnRuIiBvbmNsaWNrPSJkb0Nvbm5lY3QoKSI+Q29ubmVjdDwvYnV0dG9uPgogICAgPGRpdiBjbGFzcz0iY21zZyIgaWQ9ImNNc2ciPjwvZGl2PgogICAgPGRpdiBzdHlsZT0idGV4dC1hbGlnbjpjZW50ZXI7bWFyZ2luLXRvcDo4cHgiPgogICAgICA8c3BhbiBvbmNsaWNrPSJjbGVhclNhdmVkS2V5cygpIiBzdHlsZT0iZm9udC1zaXplOjEwcHg7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMyk7Y3Vyc29yOnBvaW50ZXI7dGV4dC1kZWNvcmF0aW9uOnVuZGVybGluZSI+Q2xlYXIgc2F2ZWQga2V5czwvc3Bhbj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIExpdmUgZGFzaGJvYXJkIC0tPgogIDxkaXYgaWQ9ImxpdmVEYXNoIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgIDxkaXYgY2xhc3M9Imhlcm8iPgogICAgICA8ZGl2IGNsYXNzPSJobCI+Qml0Y29pbiAmYnVsbDsgTGl2ZTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJocCIgaWQ9ImhQIj4kLS08L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iaHIyIj4KICAgICAgICA8c3BhbiBjbGFzcz0iY2hpcCBjbiIgaWQ9ImhSIj4tLTwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0iY2hpcCBjbiIgaWQ9ImhTIj4tLTwvc3Bhbj4KICAgICAgICA8c3BhbiBjbGFzcz0iY2hpcCBjbiIgaWQ9ImhWIj4tLTwvc3Bhbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InJiYXIgcmItbiIgaWQ9InJCYXIiPlNjYW5uaW5nLi4uPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEycHgiPkNvbmZpZGVuY2UgU2NvcmU8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iY3ciPgogICAgICAgIDxkaXYgY2xhc3M9ImNybmciPgogICAgICAgICAgPHN2ZyB2aWV3Qm94PSIwIDAgNzIgNzIiIHdpZHRoPSI3MiIgaGVpZ2h0PSI3MiI+CiAgICAgICAgICAgIDxjaXJjbGUgY3g9IjM2IiBjeT0iMzYiIHI9IjI4IiBmaWxsPSJub25lIiBzdHJva2U9IiNmMWY1ZjkiIHN0cm9rZS13aWR0aD0iNyIvPgogICAgICAgICAgICA8Y2lyY2xlIGlkPSJjQXJjIiBjeD0iMzYiIGN5PSIzNiIgcj0iMjgiIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwYjM4NiIgc3Ryb2tlLXdpZHRoPSI3IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1kYXNoYXJyYXk9IjE3NS45IiBzdHJva2UtZGFzaG9mZnNldD0iMTc1LjkiIHN0eWxlPSJ0cmFuc2l0aW9uOnN0cm9rZS1kYXNob2Zmc2V0IC42cyxzdHJva2UgLjNzIi8+CiAgICAgICAgICA8L3N2Zz4KICAgICAgICAgIDxkaXYgY2xhc3M9ImNvdiI+PGRpdiBjbGFzcz0iY251bSIgaWQ9ImNOIj4tLTwvZGl2PjxkaXYgY2xhc3M9ImNkZW4iPi8xMDA8L2Rpdj48L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJjbXQiPjxkaXYgY2xhc3M9ImNkaXIiIGlkPSJjRCI+V0FJVDwvZGl2PjxkaXYgY2xhc3M9ImNkZXQiIGlkPSJjRHQiPkdhdGhlcmluZyBkYXRhLi4uPC9kaXY+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGlkPSJ2ZXRvQmFyIiBzdHlsZT0iZGlzcGxheTpub25lO2ZvbnQtc2l6ZToxMXB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6NnB4IDEwcHg7YmFja2dyb3VuZDojZmVmM2M3O2JvcmRlci1yYWRpdXM6NnB4O21hcmdpbi1ib3R0b206OHB4O2ZvbnQtd2VpZ2h0OjcwMCI+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InBpbGxhcnMiIGlkPSJwaWxEaXYiPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJpbmRzIj4KICAgICAgICA8ZGl2IGNsYXNzPSJpbmQiPjxkaXYgY2xhc3M9ImlsIj5BRFg8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlBIj4tLTwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkJCIFdpZHRoPC9kaXY+PGRpdiBjbGFzcz0iaXYiIGlkPSJpQiI+LS08L2Rpdj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJpbmQiPjxkaXYgY2xhc3M9ImlsIj5BVFIgJTwvZGl2PjxkaXYgY2xhc3M9Iml2IiBpZD0iaVQiPi0tPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iaW5kIj48ZGl2IGNsYXNzPSJpbCI+UlNJPC9kaXY+PGRpdiBjbGFzcz0iaXYiIGlkPSJpUnNpIj4tLTwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkZ1bmRpbmc8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlGdW5kIj4tLTwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPk9JIFRyZW5kPC9kaXY+PGRpdiBjbGFzcz0iaXYiIGlkPSJpT2kiPi0tPC9kaXY+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzYmFyIj48ZGl2IGNsYXNzPSJzZmlsIiBpZD0ic0ZpbCIgc3R5bGU9IndpZHRoOjAlIj48L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ic3JvdyI+PHNwYW4gaWQ9InNTdGF0dXMiPk5vdCBydW5uaW5nPC9zcGFuPjxzcGFuIGlkPSJzY2QiIHN0eWxlPSJmb250LXdlaWdodDo4MDA7Y29sb3I6dmFyKC0tYikiPi0tPC9zcGFuPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJwZXJwRGl2Ij48L2Rpdj4KICAgIDxkaXYgaWQ9Im9wdHNEaXYiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTBweCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxNHB4Ij5XYWxsZXQ8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0id3QiPgogICAgICAgIDxkaXYgY2xhc3M9IndsIj48ZGl2IGNsYXNzPSJ3bGIiPkJhbGFuY2U8L2Rpdj48ZGl2IGNsYXNzPSJ3YSIgaWQ9IndBIj4kLS08L2Rpdj48ZGl2IGNsYXNzPSJ3cyIgaWQ9IndTdCI+PC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdj48ZGl2IGNsYXNzPSJ3cCIgaWQ9IndQIj4tLSU8L2Rpdj48ZGl2IGNsYXNzPSJ3biIgaWQ9IndOIj5QJmFtcDtMICQtLTwvZGl2PjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2ciPgogICAgICA8ZGl2IGNsYXNzPSJzdGF0Ij48ZGl2IGNsYXNzPSJzdGwiPldpbiBSYXRlPC9kaXY+PGRpdiBjbGFzcz0ic3R2IiBpZD0ic1dSIj4tLTwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzdGF0Ij48ZGl2IGNsYXNzPSJzdGwiPlRyYWRlczwvZGl2PjxkaXYgY2xhc3M9InN0diIgaWQ9InNUUiI+MDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzdGF0Ij48ZGl2IGNsYXNzPSJzdGwiPlNjYW4gIzwvZGl2PjxkaXYgY2xhc3M9InN0diIgc3R5bGU9ImNvbG9yOnZhcigtLWIpIiBpZD0ic1NOIj4wPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImIzIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRuIGJkIiAgb25jbGljaz0iYm90U3RhcnQoKSI+JiM5NjU0OyBTdGFydDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG4gYnIzIiBvbmNsaWNrPSJib3RTdG9wKCkiPiYjOTYzMjsgU3RvcDwvYnV0dG9uPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG4gYmIzIiBvbmNsaWNrPSJib3RSdW4oKSI+JiM5ODg5OyBSdW48L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5PcHRpb25zIE1vZGU8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0idG9ncm93Ij4KICAgICAgICA8ZGl2PjxkaXYgY2xhc3M9InRsIj5FbmFibGUgT3B0aW9ucyBUcmFkaW5nPC9kaXY+PGRpdiBjbGFzcz0idHMzIj5BVE0vSVRNIGNhbGxzICZhbXA7IHB1dHMgKyBzdHJhZGRsZXM8L2Rpdj48L2Rpdj4KICAgICAgICA8bGFiZWwgY2xhc3M9InRvZyI+PGlucHV0IHR5cGU9ImNoZWNrYm94IiBpZD0idG9nTyIgb25jaGFuZ2U9InRvZ2dsZU9wdHModGhpcy5jaGVja2VkKSI+PHNwYW4gY2xhc3M9InRvZ3NsIj48L3NwYW4+PC9sYWJlbD4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgaWQ9Im9wdHNQYW5lbCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICAgICAgPGRpdiBjbGFzcz0ib2luZm8iPgogICAgICAgICAgPGRpdj48ZGl2IHN0eWxlPSJmb250LXdlaWdodDo4MDA7Y29sb3I6dmFyKC0tZykiPis3MCU8L2Rpdj48ZGl2IHN0eWxlPSJjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHgiPlRha2UgUHJvZml0PC9kaXY+PC9kaXY+CiAgICAgICAgICA8ZGl2PjxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1yKSI+LTE1JTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+U3RvcCBMb3NzPC9kaXY+PC9kaXY+CiAgICAgICAgICA8ZGl2PjxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1iKSI+TG9jayA2NCU8L2Rpdj48ZGl2IHN0eWxlPSJjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHgiPm9mIHBlYWs8L2Rpdj48L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJvYiI+CiAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJvYmJ0biBvYi1jIiBvbmNsaWNrPSJjaGtPcHQoJ2NhbGwnKSI+Q2hlY2sgQ0FMTDwvYnV0dG9uPgogICAgICAgICAgPGJ1dHRvbiBjbGFzcz0ib2JidG4gb2ItcCIgb25jbGljaz0iY2hrT3B0KCdwdXQnKSI+Q2hlY2sgUFVUPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJvYmJ0biBvYi1zIiBvbmNsaWNrPSJjaGtTdCgpIj5TdHJhZGRsZTwvYnV0dG9uPgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgaWQ9Im9SZXMiIGNsYXNzPSJvcmVzIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTJweCI+VHJhZGUgU2V0dGluZ3M8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxMHB4O21hcmdpbi1ib3R0b206MTJweCI+CiAgICAgICAgPGRpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbTo2cHgiPkxvdHMgUGVyIFRyYWRlPC9kaXY+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHgiPgogICAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImFkakxvdHMoLTEpIiBzdHlsZT0id2lkdGg6MzJweDtoZWlnaHQ6MzJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOiNmOGZhZmM7Zm9udC1zaXplOjE4cHg7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdCI+4oiSPC9idXR0b24+CiAgICAgICAgICAgIDxzcGFuIGlkPSJsb3RzVmFsIiBzdHlsZT0iZm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO2ZsZXg6MTt0ZXh0LWFsaWduOmNlbnRlciI+MTwvc3Bhbj4KICAgICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJhZGpMb3RzKDEpIiBzdHlsZT0id2lkdGg6MzJweDtoZWlnaHQ6MzJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOiNmOGZhZmM7Zm9udC1zaXplOjE4cHg7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdCI+KzwvYnV0dG9uPgogICAgICAgICAgPC9kaXY+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10Myk7dGV4dC1hbGlnbjpjZW50ZXI7bWFyZ2luLXRvcDo0cHgiIGlkPSJsb3RCdGNWYWwiPjEwIGxvdHMgPSAwLjAxIEJUQzwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXY+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO21hcmdpbi1ib3R0b206NnB4Ij5NYXggVHJhZGVzL0RheTwvZGl2PgogICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4Ij4KICAgICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJhZGpEYWlseSgtMSkiIHN0eWxlPSJ3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2JhY2tncm91bmQ6I2Y4ZmFmYztmb250LXNpemU6MThweDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij7iiJI8L2J1dHRvbj4KICAgICAgICAgICAgPHNwYW4gaWQ9ImRhaWx5VmFsIiBzdHlsZT0iZm9udC1zaXplOjIwcHg7Zm9udC13ZWlnaHQ6ODAwO2ZsZXg6MTt0ZXh0LWFsaWduOmNlbnRlciI+MTA8L3NwYW4+CiAgICAgICAgICAgIDxidXR0b24gb25jbGljaz0iYWRqRGFpbHkoMSkiIHN0eWxlPSJ3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2JhY2tncm91bmQ6I2Y4ZmFmYztmb250LXNpemU6MThweDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij4rPC9idXR0b24+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOmNlbnRlcjttYXJnaW4tdG9wOjRweCIgaWQ9ImRhaWx5VXNlZCI+MCB1c2VkIHRvZGF5PC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8IS0tIFRyYWRpbmcgTW9kZSBTZWxlY3RvciAtLT4KICAgICAgPGRpdiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxNHB4Ij4KICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO21hcmdpbi1ib3R0b206OHB4Ij5UcmFkaW5nIE1vZGU8L2Rpdj4KICAgICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo2cHgiIGlkPSJtb2RlR3JpZCI+CiAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9InNldE1vZGUoJ3NhZmUnKSIgaWQ9Im1vZGUtc2FmZSIgc3R5bGU9InBhZGRpbmc6MTBweCA0cHg7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOjJweCBzb2xpZCB2YXIoLS1nKTtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlciI+CiAgICAgICAgICAgIPCfm6EgU0FGRTxicj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjlweDtmb250LXdlaWdodDo0MDAiPjIlIHJpc2s8YnI+KzUwJSBUUDwvc3Bhbj4KICAgICAgICAgIDwvYnV0dG9uPgogICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJzZXRNb2RlKCdub3JtYWwnKSIgaWQ9Im1vZGUtbm9ybWFsIiBzdHlsZT0icGFkZGluZzoxMHB4IDRweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MnB4IHNvbGlkIHZhcigtLWIpO2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyIj4KICAgICAgICAgICAg4pqWIE5PUk1BTDxicj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjlweDtmb250LXdlaWdodDo0MDAiPjUlIHJpc2s8YnI+KzcwJSBUUDwvc3Bhbj4KICAgICAgICAgIDwvYnV0dG9uPgogICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJzZXRNb2RlKCdwcm8nKSIgaWQ9Im1vZGUtcHJvIiBzdHlsZT0icGFkZGluZzoxMHB4IDRweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MnB4IHNvbGlkICNmNTllMGI7YmFja2dyb3VuZDojZmVmM2M3O2NvbG9yOiM5MjQwMGU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXIiPgogICAgICAgICAgICDwn5qAIFBSTzxicj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjlweDtmb250LXdlaWdodDo0MDAiPjEwJSByaXNrPGJyPisxMDAlIFRQPC9zcGFuPgogICAgICAgICAgPC9idXR0b24+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBpZD0ibW9kZU5vdGUiIHN0eWxlPSJmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDo2cHg7dGV4dC1hbGlnbjpjZW50ZXIiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJzYXZlVXNlclNldHRpbmdzKCkiIHN0eWxlPSJ3aWR0aDoxMDAlO3BhZGRpbmc6MTFweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmY7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTNweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXIiPlNhdmUgU2V0dGluZ3M8L2J1dHRvbj4KICAgICAgPGRpdiBpZD0ic2V0TXNnIiBzdHlsZT0idGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjExcHg7bWFyZ2luLXRvcDo2cHg7bWluLWhlaWdodDoxNnB4O2NvbG9yOnZhcigtLWcpIj48L2Rpdj4KICAgIDwvZGl2PgoKICAgIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTJweCI+TWFudWFsIFRyYWRlPC9kaXY+CgogICAgICA8IS0tIEZVVFVSRVMgLS0+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbTo2cHg7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHgiPkZ1dHVyZXMgKFBlcnApPC9kaXY+CiAgICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0ibUxvdHMiIHR5cGU9Im51bWJlciIgcGxhY2Vob2xkZXI9IkxvdHMgKGRlZmF1bHQ6IDEpIiBtaW49IjEiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjhweCI+CiAgICAgIDxkaXYgY2xhc3M9Im1yb3ciIHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImJ0bmwiICBvbmNsaWNrPSJtYW5UcmFkZSgnbG9uZycpIj4mIzg1OTM7IEJ1eSBMb25nPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRuczIiIG9uY2xpY2s9Im1hblRyYWRlKCdzaG9ydCcpIj4mIzg1OTU7IFNlbGwgU2hvcnQ8L2J1dHRvbj4KICAgICAgPC9kaXY+CgogICAgICA8IS0tIE9QVElPTlMgQ0hBSU4gLS0+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbToxMHB4O3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNXB4O21hcmdpbi10b3A6NHB4Ij5PcHRpb25zIENoYWluPC9kaXY+CgogICAgICA8IS0tIFN0ZXAgMTogRXhwaXJ5IHNlbGVjdG9yIC0tPgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbTo1cHg7Zm9udC13ZWlnaHQ6NzAwIj7ikaAgU0VMRUNUIEVYUElSWTwvZGl2PgogICAgICA8ZGl2IGlkPSJleHBpcnlSb3ciIHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjZweDtmbGV4LXdyYXA6d3JhcDttYXJnaW4tYm90dG9tOjEycHgiPjwvZGl2PgoKICAgICAgPCEtLSBTdGVwIDI6IFR5cGUgc2VsZWN0b3IgLS0+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjVweDtmb250LXdlaWdodDo3MDAiPuKRoSBDQUxMIG9yIFBVVDwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjZweDttYXJnaW4tYm90dG9tOjEycHgiPgogICAgICAgIDxidXR0b24gaWQ9InR5cGVDYWxsIiBvbmNsaWNrPSJzZWxUeXBlKCdjYWxsJykiIHN0eWxlPSJwYWRkaW5nOjlweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MnB4IHNvbGlkICNiZmRiZmU7YmFja2dyb3VuZDp2YXIoLS1iYik7Y29sb3I6dmFyKC0tYik7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXIiPvCfk4ggQ0FMTDwvYnV0dG9uPgogICAgICAgIDxidXR0b24gaWQ9InR5cGVQdXQiICBvbmNsaWNrPSJzZWxUeXBlKCdwdXQnKSIgIHN0eWxlPSJwYWRkaW5nOjlweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MnB4IHNvbGlkICNmY2E1YTU7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcik7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXIiPvCfk4kgUFVUPC9idXR0b24+CiAgICAgIDwvZGl2PgoKICAgICAgPCEtLSBTdGVwIDM6IFN0cmlrZSBjaGFpbiB0YWJsZSAtLT4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi1ib3R0b206NXB4O2ZvbnQtd2VpZ2h0OjcwMCI+4pGiIFNFTEVDVCBTVFJJS0U8L2Rpdj4KICAgICAgPGRpdiBpZD0iY2hhaW5Mb2FkaW5nIiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTBweDtkaXNwbGF5Om5vbmUiPkxvYWRpbmcgY2hhaW4uLi48L2Rpdj4KICAgICAgPGRpdiBpZD0iY2hhaW5UYWJsZSIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTJweCI+PC9kaXY+CgogICAgICA8IS0tIFN0ZXAgNDogUCZMIENhbGN1bGF0b3IgLS0+CiAgICAgIDxkaXYgaWQ9InBsQ2FsYyIgc3R5bGU9ImRpc3BsYXk6bm9uZTtiYWNrZ3JvdW5kOiNmOGZhZmM7Ym9yZGVyLXJhZGl1czoxMHB4O3BhZGRpbmc6MTJweDttYXJnaW4tYm90dG9tOjEwcHg7Ym9yZGVyOnZhcigtLWJkcikiPgogICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbTo4cHgiPuKRoyBQJkwgQ0FMQ1VMQVRPUjwvZGl2PgogICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjZweCIgaWQ9InNlbFN1bW1hcnkiPuKAlDwvZGl2PgogICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6OHB4O21hcmdpbi1ib3R0b206OHB4Ij4KICAgICAgICAgIDxkaXY+CiAgICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tYm90dG9tOjNweCI+TE9UUzwvZGl2PgogICAgICAgICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHgiPgogICAgICAgICAgICAgIDxidXR0b24gb25jbGljaz0iYWRqT0xvdHMoLTEpIiBzdHlsZT0id2lkdGg6MjhweDtoZWlnaHQ6MjhweDtib3JkZXItcmFkaXVzOjZweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOiNmMWY1Zjk7Zm9udC1zaXplOjE2cHg7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdCI+4oiSPC9idXR0b24+CiAgICAgICAgICAgICAgPHNwYW4gaWQ9Im9Mb3RzIiBzdHlsZT0iZm9udC1zaXplOjE4cHg7Zm9udC13ZWlnaHQ6ODAwO2ZsZXg6MTt0ZXh0LWFsaWduOmNlbnRlciI+MTwvc3Bhbj4KICAgICAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImFkak9Mb3RzKDEpIiAgc3R5bGU9IndpZHRoOjI4cHg7aGVpZ2h0OjI4cHg7Ym9yZGVyLXJhZGl1czo2cHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDojZjFmNWY5O2ZvbnQtc2l6ZToxNnB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXQiPis8L2J1dHRvbj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDxkaXY+CiAgICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjcwMDttYXJnaW4tYm90dG9tOjNweCI+QlRDIFRBUkdFVDwvZGl2PgogICAgICAgICAgICA8aW5wdXQgaWQ9Im9UYXJnZXQiIHR5cGU9Im51bWJlciIgcGxhY2Vob2xkZXI9ImUuZy4gODIwMDAiIHN0eWxlPSJ3aWR0aDoxMDAlO2JvcmRlcjp2YXIoLS1iZHIpO2JvcmRlci1yYWRpdXM6NnB4O3BhZGRpbmc6NnB4IDhweDtmb250LXNpemU6MTNweDtmb250LWZhbWlseTppbmhlcml0O291dGxpbmU6bm9uZTtiYWNrZ3JvdW5kOiNmZmYiIG9uaW5wdXQ9ImNhbGNQTCgpIj4KICAgICAgICAgIDwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDwhLS0gUCZMIFJlc3VsdCAtLT4KICAgICAgICA8ZGl2IGlkPSJwbFJlc3VsdCIgc3R5bGU9ImJvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTBweDt0ZXh0LWFsaWduOmNlbnRlcjtkaXNwbGF5Om5vbmUiPgogICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjtnYXA6NnB4Ij4KICAgICAgICAgICAgPGRpdiBzdHlsZT0iYmFja2dyb3VuZDojZmZmO2JvcmRlci1yYWRpdXM6NnB4O3BhZGRpbmc6OHB4Ij4KICAgICAgICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTtmb250LXdlaWdodDo3MDAiPlBSRU1JVU0gUEFJRDwvZGl2PgogICAgICAgICAgICAgIDxkaXYgaWQ9InBsQ29zdCIgc3R5bGU9ImZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1yKSI+4oCUPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOiNmZmY7Ym9yZGVyLXJhZGl1czo2cHg7cGFkZGluZzo4cHgiPgogICAgICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjcwMCI+RVNULiBQUk9GSVQ8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGlkPSJwbFByb2ZpdCIgc3R5bGU9ImZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1nKSI+4oCUPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOiNmZmY7Ym9yZGVyLXJhZGl1czo2cHg7cGFkZGluZzo4cHgiPgogICAgICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTo5cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtd2VpZ2h0OjcwMCI+QlJFQUtFVkVOPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBpZD0icGxCRSIgc3R5bGU9ImZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1iKSI+4oCUPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPC9kaXY+CiAgICAgICAgICA8ZGl2IGlkPSJwbEJhciIgc3R5bGU9Im1hcmdpbi10b3A6OHB4O2hlaWdodDo2cHg7Ym9yZGVyLXJhZGl1czozcHg7YmFja2dyb3VuZDojZTJlOGYwO292ZXJmbG93OmhpZGRlbiI+CiAgICAgICAgICAgIDxkaXYgaWQ9InBsRmlsbCIgc3R5bGU9ImhlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6M3B4O3dpZHRoOjAlO3RyYW5zaXRpb246d2lkdGggLjRzIj48L2Rpdj4KICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPGRpdiBpZD0icGxOb3RlIiBzdHlsZT0iZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6NHB4Ij7igJQ8L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8YnV0dG9uIGlkPSJidXlCdG4iIG9uY2xpY2s9ImV4ZWNNYW5PcHQoKSIgc3R5bGU9IndpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMDtjdXJzb3I6cG9pbnRlcjttYXJnaW4tdG9wOjhweDtkaXNwbGF5Om5vbmUiPlBsYWNlIE9yZGVyPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGlkPSJvTXNnIiBzdHlsZT0iZm9udC1zaXplOjExcHg7dGV4dC1hbGlnbjpjZW50ZXI7bWluLWhlaWdodDoxNnB4O2NvbG9yOnZhcigtLWcpIj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iYmNhIiBvbmNsaWNrPSJjbG9zZUFsbCgpIj4mIzk4ODg7IENsb3NlIEFsbCBQb3NpdGlvbnM8L2J1dHRvbj4KICA8L2Rpdj4KPC9kaXY+Cgo8IS0tIFRSQURFUyAtLT4KPGRpdiBjbGFzcz0icGFnZSIgaWQ9InAtdHJhZGVzIj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxMnB4Ij4KICAgICAgPHNwYW4gY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luOjAiPkFsbCBUcmFkZXM8L3NwYW4+CiAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6OHB4O2FsaWduLWl0ZW1zOmNlbnRlciI+CiAgICAgICAgPHNwYW4gaWQ9InRDbnQiIHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10MykiPjAgdHJhZGVzPC9zcGFuPgogICAgICAgIDxidXR0b24gb25jbGljaz0iY2xlYXJTdGFsZSgpIiBzdHlsZT0icGFkZGluZzo0cHggMTBweDtib3JkZXItcmFkaXVzOjZweDtib3JkZXI6MXB4IHNvbGlkICNmY2E1YTU7YmFja2dyb3VuZDojZmVmMmYyO2NvbG9yOiNlNzRjM2M7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXQiPvCfl5EgQ2xlYXIgU3RhbGU8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9InRMaXN0Ij48ZGl2IGNsYXNzPSJlbXB0eSI+Tm8gdHJhZGVzIHlldDwvZGl2PjwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjwhLS0gTE9HUyAtLT4KPGRpdiBjbGFzcz0icGFnZSIgaWQ9InAtbG9ncyI+CiAgPGRpdiBjbGFzcz0ibGYiPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIG9uIiBpZD0ibGZhIiBvbmNsaWNrPSJzZXRMRignJykiPkFsbDwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIiBpZD0ibGZ0IiBvbmNsaWNrPSJzZXRMRignVFJBREUnKSI+VHJhZGVzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIiIGlkPSJsZnciIG9uY2xpY2s9InNldExGKCdXQVJOJykiPldhcm5pbmdzPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJsZmIiIGlkPSJsZmUiIG9uY2xpY2s9InNldExGKCdFUlJPUicpIj5FcnJvcnM8L2J1dHRvbj4KICA8L2Rpdj4KICA8ZGl2IGlkPSJsQ250IiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi1ib3R0b206OHB4Ij4wIGVudHJpZXM8L2Rpdj4KICA8ZGl2IGNsYXNzPSJsYm94IiBpZD0ibEJveCI+PC9kaXY+CjwvZGl2PgoKPCEtLSBTRVRUSU5HUyAtLT4KPGRpdiBjbGFzcz0icGFnZSIgaWQ9InAtc2V0dGluZ3MiPgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjhweCI+U2VydmVyIElQIOKAlCBXaGl0ZWxpc3Qgb24gRGVsdGE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImlwYm94IiBpZD0ic2lwQm94Ij4tLTwvZGl2PgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO2xpbmUtaGVpZ2h0OjEuOSI+RGVsdGEgRXhjaGFuZ2UgJnJhcnI7IEFjY291bnQgJnJhcnI7IEFQSSBLZXlzICZyYXJyOyBFZGl0ICZyYXJyOyBJUCBXaGl0ZWxpc3Q8L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo0cHgiPkFjdGl2ZSBHdWFyZHJhaWxzPC9kaXY+CiAgICA8ZGl2IGlkPSJnckxpc3QiPjwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImNhcmQiPgogICAgPGJ1dHRvbiBjbGFzcz0iZGMtYnRuIiBzdHlsZT0iY29sb3I6dmFyKC0tcikiIG9uY2xpY2s9ImRvRGlzY29ubmVjdCgpIj4mIzEwMDA3OyBEaXNjb25uZWN0IERlbHRhIEV4Y2hhbmdlPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJkYy1idG4iIHN0eWxlPSJjb2xvcjp2YXIoLS10MikiIG9uY2xpY2s9ImRvTG9nb3V0KCkiPiYjODU5NDsgU2lnbiBPdXQ8L2J1dHRvbj4KICA8L2Rpdj4KICA8IS0tIEFkbWluIHBhbmVsIC0tPgogIDxkaXYgaWQ9ImFkbWluUGFuZWwiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgPGRpdiBjbGFzcz0iY2FyZCIgc3R5bGU9ImJvcmRlcjoycHggc29saWQgdmFyKC0teSkiPgogICAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206MTJweDtjb2xvcjp2YXIoLS15KSI+JiM5ODgxOyBBZG1pbiBQYW5lbDwvZGl2PgogICAgICA8ZGl2IGlkPSJhdUxpc3QiPjwvZGl2PgogICAgICA8YnV0dG9uIG9uY2xpY2s9Imdlbkludml0ZSgpIiBzdHlsZT0id2lkdGg6MTAwJTttYXJnaW4tdG9wOjEwcHg7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1iKTtiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlciI+KyBHZW5lcmF0ZSBJbnZpdGUgQ29kZTwvYnV0dG9uPgogICAgICA8ZGl2IGlkPSJuZXdJbnZpdGUiIHN0eWxlPSJkaXNwbGF5Om5vbmUiPgogICAgICAgIDxkaXYgY2xhc3M9Imljb2RlIiBpZD0iaW52Q29kZSI+PC9kaXY+CiAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246Y2VudGVyIj5TaGFyZSB0aGlzLiBPbmUtdGltZSB1c2Ugb25seS48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8L2Rpdj48IS0tIHdyYXAgLS0+CjxuYXYgY2xhc3M9Im5hdiI+CiAgPGJ1dHRvbiBjbGFzcz0ibmIgb24iIGlkPSJuYi1ob21lIiAgICAgb25jbGljaz0iZ29QYWdlKCdob21lJykiPjxzcGFuIGNsYXNzPSJpYyI+JiMxMjc5Njg7PC9zcGFuPjxzcGFuIGNsYXNzPSJsYiI+SG9tZTwvc3Bhbj48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJuYiIgICAgaWQ9Im5iLXRyYWRlcyIgICBvbmNsaWNrPSJnb1BhZ2UoJ3RyYWRlcycpIj48c3BhbiBjbGFzcz0iaWMiPiYjMTI4MjAzOzwvc3Bhbj48c3BhbiBjbGFzcz0ibGIiPlRyYWRlczwvc3Bhbj48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJuYiIgICAgaWQ9Im5iLWxvZ3MiICAgICBvbmNsaWNrPSJnb1BhZ2UoJ2xvZ3MnKSI+PHNwYW4gY2xhc3M9ImljIj4mIzEyODIyMDs8L3NwYW4+PHNwYW4gY2xhc3M9ImxiIj5Mb2dzPC9zcGFuPjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9Im5iIiAgICBpZD0ibmItc2V0dGluZ3MiIG9uY2xpY2s9ImdvUGFnZSgnc2V0dGluZ3MnKSI+PHNwYW4gY2xhc3M9ImljIj4mIzk4ODE7PC9zcGFuPjxzcGFuIGNsYXNzPSJsYiI+U2V0dGluZ3M8L3NwYW4+PC9idXR0b24+CjwvbmF2Pgo8L2Rpdj48IS0tIGFwcCAtLT4KCjxzY3JpcHQ+CnZhciBTVD17bG9nczpbXSxsZjoiIix0cmFkZXM6W10sbmV4dEF0Om51bGwsc3M6MzAwLGlzQWRtaW46ZmFsc2V9Owp2YXIgUEM9eyJSZWdpbWUiOiIjM2I4MmY2IiwiTVRGIEFsaWduIjoiIzAwYjM4NiIsIlJTSSI6IiNmNTllMGIiLCJNQUNEIjoiIzhiNWNmNiIsIlZvbGF0aWxpdHkiOiIjZWM0ODk5IiwiVm9sdW1lIjoiI2U3NGMzYyIsIlNlc3Npb24iOiIjMTRiOGE2In07CgpmdW5jdGlvbiBnZShpZCl7cmV0dXJuIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKTt9CmZ1bmN0aW9uIHN0KGlkLHYpe3ZhciBlPWdlKGlkKTtpZihlKWUudGV4dENvbnRlbnQ9djt9CmZ1bmN0aW9uIHNoKGlkLHYpe3ZhciBlPWdlKGlkKTtpZihlKWUuaW5uZXJIVE1MPXY7fQoKZnVuY3Rpb24geGhyKHVybCxib2R5LGNiKXsKICB2YXIgcmVxPW5ldyBYTUxIdHRwUmVxdWVzdCgpLGlzUD1ib2R5IT09dW5kZWZpbmVkJiZib2R5IT09bnVsbDsKICByZXEub3Blbihpc1A/IlBPU1QiOiJHRVQiLHVybCx0cnVlKTtyZXEud2l0aENyZWRlbnRpYWxzPXRydWU7CiAgaWYoaXNQKXJlcS5zZXRSZXF1ZXN0SGVhZGVyKCJDb250ZW50LVR5cGUiLCJhcHBsaWNhdGlvbi9qc29uIik7CiAgcmVxLm9ucmVhZHlzdGF0ZWNoYW5nZT1mdW5jdGlvbigpewogICAgaWYocmVxLnJlYWR5U3RhdGUhPT00KXJldHVybjsKICAgIGlmKCFjYilyZXR1cm47CiAgICBpZihyZXEuc3RhdHVzPT09MjAwKXt0cnl7Y2IoSlNPTi5wYXJzZShyZXEucmVzcG9uc2VUZXh0KSk7fWNhdGNoKGUpe2NiKG51bGwpO319CiAgICBlbHNlIGlmKHJlcS5zdGF0dXM9PT00MDEpe3Nob3dBdXRoKCk7fQogICAgZWxzZXtjYihudWxsKTt9CiAgfTsKICByZXEub25lcnJvcj1mdW5jdGlvbigpe2lmKGNiKWNiKG51bGwpO307CiAgcmVxLnNlbmQoaXNQP0pTT04uc3RyaW5naWZ5KGJvZHkpOm51bGwpOwp9CgpmdW5jdGlvbiBzaG93QXV0aCgpe2dlKCJhdXRoU2NyZWVuIikuc3R5bGUuZGlzcGxheT0iZmxleCI7Z2UoImFwcCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO30KZnVuY3Rpb24gc2hvd0Nvbm5lY3QoKXtnZSgiY29ubmVjdENhcmQiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7Z2UoImxpdmVEYXNoIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7bG9hZFNhdmVkS2V5cygpO30KZnVuY3Rpb24gc2hvd0FwcCgpe2dlKCJhdXRoU2NyZWVuIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7Z2UoImFwcCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjt9CmZ1bmN0aW9uIHNob3dMb2dpbigpe2dlKCJsb2dpbkZvcm0iKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7Z2UoInJlZ0Zvcm0iKS5zdHlsZS5kaXNwbGF5PSJub25lIjt9CmZ1bmN0aW9uIHNob3dSZWcoKXtnZSgibG9naW5Gb3JtIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7Z2UoInJlZ0Zvcm0iKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7fQoKZnVuY3Rpb24gZ29QYWdlKG4pewogIFsiaG9tZSIsInRyYWRlcyIsImxvZ3MiLCJzZXR0aW5ncyJdLmZvckVhY2goZnVuY3Rpb24odCl7CiAgICBnZSgicC0iK3QpLmNsYXNzTGlzdC50b2dnbGUoInNob3ciLHQ9PT1uKTsKICAgIGdlKCJuYi0iK3QpLmNsYXNzTGlzdC50b2dnbGUoIm9uIix0PT09bik7CiAgfSk7CiAgaWYobj09PSJ0cmFkZXMiKXJlbmRlclRyYWRlcygpOwogIGlmKG49PT0ibG9ncyIpcmVuZGVyTG9ncygpOwogIGlmKG49PT0ic2V0dGluZ3MiKWxvYWRBZG1pbigpOwp9CgpmdW5jdGlvbiBkb0xvZ2luKCl7CiAgdmFyIHU9Z2UoImxVc2VyIikudmFsdWUudHJpbSgpLHA9Z2UoImxQYXNzIikudmFsdWU7CiAgaWYoIXV8fCFwKXtzaG93TXNnKCJsTXNnIiwiRW50ZXIgdXNlcm5hbWUgYW5kIHBhc3N3b3JkIiwiZXJyIik7cmV0dXJuO30KICBzaG93TXNnKCJsTXNnIiwiU2lnbmluZyBpbi4uLiIsIiIpOwogIHhocigiL2F1dGgvbG9naW4iLHt1c2VybmFtZTp1LHBhc3N3b3JkOnB9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgU1QuaXNBZG1pbj1yLmlzX2FkbWluO3N0KCJ1QmFkZ2UiLHIudXNlcm5hbWUpO3Nob3dBcHAoKTtsb2FkSVAoKTtwb2xsKCk7CiAgICAgIHNldFRpbWVvdXQodHJ5QXV0b0Nvbm5lY3QsMzAwKTsKICAgIH1lbHNle3Nob3dNc2coImxNc2ciLHI/ci5tZXNzYWdlOiJMb2dpbiBmYWlsZWQiLCJlcnIiKTt9CiAgfSk7Cn0KZnVuY3Rpb24gZG9SZWdpc3RlcigpewogIHZhciBpPWdlKCJySW52IikudmFsdWUudHJpbSgpLHU9Z2UoInJVc2VyIikudmFsdWUudHJpbSgpLHA9Z2UoInJQYXNzIikudmFsdWU7CiAgaWYoIWl8fCF1fHwhcCl7c2hvd01zZygick1zZyIsIkFsbCBmaWVsZHMgcmVxdWlyZWQiLCJlcnIiKTtyZXR1cm47fQogIHNob3dNc2coInJNc2ciLCJDcmVhdGluZyBhY2NvdW50Li4uIiwiIik7CiAgeGhyKCIvYXV0aC9yZWdpc3RlciIse2ludml0ZTppLHVzZXJuYW1lOnUscGFzc3dvcmQ6cH0sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpewogICAgICBTVC5pc0FkbWluPWZhbHNlO3N0KCJ1QmFkZ2UiLHUpO3Nob3dBcHAoKTtsb2FkSVAoKTtwb2xsKCk7CiAgICB9ZWxzZXtzaG93TXNnKCJyTXNnIixyP3IubWVzc2FnZToiUmVnaXN0cmF0aW9uIGZhaWxlZCIsImVyciIpO30KICB9KTsKfQpmdW5jdGlvbiBzaG93TXNnKGlkLG1zZyxjbHMpe3ZhciBlPWdlKGlkKTtlLnRleHRDb250ZW50PW1zZztlLmNsYXNzTmFtZT0iYXV0aC1tc2ciKyhjbHM/IiAiK2NsczoiIik7fQpmdW5jdGlvbiBkb0xvZ291dCgpewogIGlmKCFjb25maXJtKCJTaWduIG91dD8iKSlyZXR1cm47CiAgeGhyKCIvYXV0aC9sb2dvdXQiLHt9LGZ1bmN0aW9uKCl7c2hvd0F1dGgoKTtnZSgibFVzZXIiKS52YWx1ZT0iIjtnZSgibFBhc3MiKS52YWx1ZT0iIjt9KTsKfQpmdW5jdGlvbiBkb0Rpc2Nvbm5lY3QoKXsKICBpZighY29uZmlybSgiRGlzY29ubmVjdCBEZWx0YSBFeGNoYW5nZT8iKSlyZXR1cm47CiAgZ2UoImNvbm5lY3RDYXJkIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO2dlKCJsaXZlRGFzaCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiOwp9CmZ1bmN0aW9uIGNvcHlJUCgpewogIHZhciBpcD1nZSgic0lQIikudGV4dENvbnRlbnQ7CiAgdHJ5e25hdmlnYXRvci5jbGlwYm9hcmQud3JpdGVUZXh0KGlwKTt9Y2F0Y2goZSl7fQogIHZhciBiPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoIi5pcC1jb3B5Iik7Yi50ZXh0Q29udGVudD0iQ29waWVkISI7CiAgc2V0VGltZW91dChmdW5jdGlvbigpe2IudGV4dENvbnRlbnQ9IkNvcHkiO30sMjAwMCk7Cn0KZnVuY3Rpb24gZG9Db25uZWN0KCl7CiAgdmFyIGs9Z2UoImNLZXkiKS52YWx1ZS50cmltKCkscz1nZSgiY1NlYyIpLnZhbHVlLnRyaW0oKTsKICBpZigha3x8IXMpe2dlKCJjTXNnIikuaW5uZXJIVE1MPSI8c3BhbiBzdHlsZT0nY29sb3I6I2Y4NzE3MSc+RW50ZXIgQVBJIGtleSBhbmQgc2VjcmV0PC9zcGFuPiI7cmV0dXJuO30KICBnZSgiY01zZyIpLnRleHRDb250ZW50PSJDb25uZWN0aW5nLi4uIjsKICB4aHIoIi9hcGkvY29ubmVjdCIse2FwaV9rZXk6ayxhcGlfc2VjcmV0OnN9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgLy8gU2F2ZSBrZXlzIHRvIGxvY2FsU3RvcmFnZSDigJQgcmVtZW1iZXJlZCBhY3Jvc3Mgc2Vzc2lvbnMKICAgICAgdHJ5e2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJhYl9rZXkiLGspO2xvY2FsU3RvcmFnZS5zZXRJdGVtKCJhYl9zZWMiLHMpO31jYXRjaChlKXt9CiAgICAgIGdlKCJjTXNnIikuaW5uZXJIVE1MPSI8c3BhbiBzdHlsZT0nY29sb3I6IzRhZGU4MCc+Q29ubmVjdGVkISAkIityLmJhbGFuY2UudG9GaXhlZCgyKSsiPC9zcGFuPiI7CiAgICAgIGdlKCJjb25uZWN0Q2FyZCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO2dlKCJsaXZlRGFzaCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsKICAgIH1lbHNlewogICAgICB2YXIgaXA9ciYmci5zZXJ2ZXJfaXA/IiB8IElQOiAiK3Iuc2VydmVyX2lwOiIiOwogICAgICBnZSgiY01zZyIpLmlubmVySFRNTD0iPHNwYW4gc3R5bGU9J2NvbG9yOiNmODcxNzEnPiIrKHI/ci5tZXNzYWdlOiJGYWlsZWQiKStpcCsiPC9zcGFuPiI7CiAgICB9CiAgfSk7Cn0KZnVuY3Rpb24gbG9hZFNhdmVkS2V5cygpewogIHRyeXsKICAgIHZhciBrPWxvY2FsU3RvcmFnZS5nZXRJdGVtKCJhYl9rZXkiKTsKICAgIHZhciBzPWxvY2FsU3RvcmFnZS5nZXRJdGVtKCJhYl9zZWMiKTsKICAgIGlmKGsmJnMpewogICAgICBnZSgiY0tleSIpLnZhbHVlPWs7IGdlKCJjU2VjIikudmFsdWU9czsKICAgICAgZ2UoImNNc2ciKS5pbm5lckhUTUw9IjxzcGFuIHN0eWxlPSdjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC41KSc+JiMxMjgyNzQ7IEtleXMgbG9hZGVkIOKAlCB0YXAgQ29ubmVjdDwvc3Bhbj4iOwogICAgfQogIH1jYXRjaChlKXt9Cn0KZnVuY3Rpb24gY2xlYXJTYXZlZEtleXMoKXsKICB0cnl7bG9jYWxTdG9yYWdlLnJlbW92ZUl0ZW0oImFiX2tleSIpO2xvY2FsU3RvcmFnZS5yZW1vdmVJdGVtKCJhYl9zZWMiKTt9Y2F0Y2goZSl7fQogIGdlKCJjS2V5IikudmFsdWU9IiI7Z2UoImNTZWMiKS52YWx1ZT0iIjsKICBnZSgiY01zZyIpLnRleHRDb250ZW50PSJLZXlzIGNsZWFyZWQiOwp9CmZ1bmN0aW9uIGJvdFN0YXJ0KCl7eGhyKCIvYXBpL2JvdC9zdGFydCIse30sbnVsbCk7fQpmdW5jdGlvbiBib3RTdG9wKCl7eGhyKCIvYXBpL2JvdC9zdG9wIix7fSxudWxsKTt9CmZ1bmN0aW9uIGJvdFJ1bigpe3N0KCJzU3RhdHVzIiwiU2Nhbm5pbmcuLi4iKTt4aHIoIi9hcGkvYm90L3J1bl9ub3ciLHt9LG51bGwpO30KZnVuY3Rpb24gY2xvc2VBbGwoKXsKICBpZighY29uZmlybSgiQ2xvc2UgQUxMIG9wZW4gcG9zaXRpb25zPyIpKXJldHVybjsKICB4aHIoIi9hcGkvY2xvc2VfYWxsIix7fSxmdW5jdGlvbihyKXthbGVydCgiQ2xvc2VkOiAiKygociYmci5jbG9zZWQpfHwwKSsiIHBvc2l0aW9ucyIpO30pOwp9CmZ1bmN0aW9uIG1hblRyYWRlKGRpcil7CiAgdmFyIGxvdHM9cGFyc2VJbnQoZ2UoIm1Mb3RzIikudmFsdWUpfHwxOwogIHhocigiL2FwaS9tYW51YWxfdHJhZGUiLHtkaXJlY3Rpb246ZGlyLGxvdHM6bG90c30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpYWxlcnQoZGlyLnRvVXBwZXJDYXNlKCkrIiAiK2xvdHMrIkxcbkVudHJ5ICQiK3IuZW50cnkrIlxuU3RvcCAkIityLnN0b3ArIlxuVFAgJCIrci50cCk7CiAgICBlbHNlIGFsZXJ0KCJGYWlsZWQ6ICIrKChyJiZyLm1lc3NhZ2UpfHwiQ2hlY2sgTG9ncyIpKTsKICB9KTsKfQovLyDilIDilIAgT1BUSU9OUyBDSEFJTiDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAKdmFyIE9DPXtleHBpcnk6bnVsbCx0eXBlOiJjYWxsIixzdHJpa2U6bnVsbCxtYXJrOjAsbG90czoxLHN5bTpudWxsfTsKCmZ1bmN0aW9uIGdldEV4cGlyaWVzKCl7CiAgLy8gTmV4dCA0IEZyaWRheXMKICB2YXIgZXhwaXJpZXM9W107IHZhciBkPW5ldyBEYXRlKCk7CiAgZm9yKHZhciBpPTA7aTwyODtpKyspewogICAgZC5zZXREYXRlKGQuZ2V0RGF0ZSgpKzEpOwogICAgaWYoZC5nZXREYXkoKT09PTUpewogICAgICB2YXIgZGQ9U3RyaW5nKGQuZ2V0RGF0ZSgpKS5wYWRTdGFydCgyLCIwIik7CiAgICAgIHZhciBtbT1TdHJpbmcoZC5nZXRNb250aCgpKzEpLnBhZFN0YXJ0KDIsIjAiKTsKICAgICAgdmFyIHl5PVN0cmluZyhkLmdldEZ1bGxZZWFyKCkpLnNsaWNlKC0yKTsKICAgICAgZXhwaXJpZXMucHVzaCh7bGFiZWw6ZGQrIi8iK21tKyIvIit5eSxjb2RlOmRkK21tK3l5LGRhdGU6bmV3IERhdGUoZCl9KTsKICAgICAgaWYoZXhwaXJpZXMubGVuZ3RoPj00KWJyZWFrOwogICAgfQogIH0KICByZXR1cm4gZXhwaXJpZXM7Cn0KCmZ1bmN0aW9uIGluaXRDaGFpbigpewogIHZhciByb3c9Z2UoImV4cGlyeVJvdyIpOyBpZighcm93KXJldHVybjsKICByb3cuaW5uZXJIVE1MPSI8c3BhbiBzdHlsZT0nZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpJz5Mb2FkaW5nIGV4cGlyaWVzLi4uPC9zcGFuPiI7CiAgeGhyKCIvYXBpL29wdHMvZXhwaXJpZXMiLG51bGwsZnVuY3Rpb24ocil7CiAgICByb3cuaW5uZXJIVE1MPSIiOwogICAgaWYoIXJ8fCFyLmV4cGlyaWVzfHwhci5leHBpcmllcy5sZW5ndGgpewogICAgICByb3cuaW5uZXJIVE1MPSI8c3BhbiBzdHlsZT0nZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tciknPk5vIGV4cGlyaWVzIGZvdW5kPC9zcGFuPiI7cmV0dXJuOwogICAgfQogICAgci5leHBpcmllcy5mb3JFYWNoKGZ1bmN0aW9uKGUsaSl7CiAgICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiYnV0dG9uIik7CiAgICAgIGJ0bi5jbGFzc05hbWU9ImV4cC1idG4iKyhpPT09MD8iIHNlbCI6IiIpOwogICAgICBidG4uaW5uZXJIVE1MPWUubGFiZWwrIjxicj48c3BhbiBzdHlsZT0nZm9udC1zaXplOjlweDtmb250LXdlaWdodDo0MDAnPiIrZS5kYXlzKyJkPC9zcGFuPiI7CiAgICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiLmV4cC1idG4iKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgic2VsIik7fSk7CiAgICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoInNlbCIpOyBPQy5leHBpcnk9ZS5jb2RlOyBsb2FkQ2hhaW4oKTsKICAgICAgfTsKICAgICAgaWYoaT09PTApT0MuZXhwaXJ5PWUuY29kZTsKICAgICAgcm93LmFwcGVuZENoaWxkKGJ0bik7CiAgICB9KTsKICAgIGlmKE9DLnR5cGUpbG9hZENoYWluKCk7CiAgfSk7Cn0KCmZ1bmN0aW9uIHNlbFR5cGUodCl7CiAgT0MudHlwZT10OyBPQy5zdHJpa2U9bnVsbDsgT0Muc3ltPW51bGw7CiAgZ2UoInR5cGVDYWxsIikuc3R5bGUub3BhY2l0eT10PT09ImNhbGwiPyIxIjoiMC40NSI7CiAgZ2UoInR5cGVQdXQiKS5zdHlsZS5vcGFjaXR5PXQ9PT0icHV0Ij8iMSI6IjAuNDUiOwogIGdlKCJ0eXBlQ2FsbCIpLnN0eWxlLnRyYW5zZm9ybT10PT09ImNhbGwiPyJzY2FsZSgxLjA0KSI6InNjYWxlKDEpIjsKICBnZSgidHlwZVB1dCIpLnN0eWxlLnRyYW5zZm9ybT10PT09InB1dCI/InNjYWxlKDEuMDQpIjoic2NhbGUoMSkiOwogIGdlKCJwbENhbGMiKS5zdHlsZS5kaXNwbGF5PSJub25lIjsKICBpZihnZSgiYnV5QnRuIikpZ2UoImJ1eUJ0biIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiOwogIGlmKE9DLmV4cGlyeSlsb2FkQ2hhaW4oKTsKfQoKZnVuY3Rpb24gbG9hZENoYWluKCl7CiAgaWYoIU9DLmV4cGlyeXx8IU9DLnR5cGUpcmV0dXJuOwogIHZhciB0Ymw9Z2UoImNoYWluVGFibGUiKTsKICBnZSgiY2hhaW5Mb2FkaW5nIikuc3R5bGUuZGlzcGxheT0iYmxvY2siOyB0YmwuaW5uZXJIVE1MPSIiOwogIHhocigiL2FwaS9vcHRzL2NoYWluIix7ZXhwaXJ5Ok9DLmV4cGlyeSx0eXBlOk9DLnR5cGV9LGZ1bmN0aW9uKHIpewogICAgZ2UoImNoYWluTG9hZGluZyIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiOwogICAgaWYoIXJ8fCFyLmNoYWlufHwhci5jaGFpbi5sZW5ndGgpewogICAgICB0YmwuaW5uZXJIVE1MPSI8ZGl2IHN0eWxlPSd0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjE2cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtc2l6ZToxMnB4Jz5ObyAiK09DLnR5cGUrIiBvcHRpb25zIGZvdW5kIGZvciB0aGlzIGV4cGlyeTwvZGl2PiI7CiAgICAgIHJldHVybjsKICAgIH0KICAgIHJlbmRlckNoYWluKHIuY2hhaW4sci5hdG0sci5wcmljZSk7CiAgfSk7Cn0KCmZ1bmN0aW9uIHJlbmRlckNoYWluKHJvd3MsYXRtLHByaWNlKXsKICB2YXIgdGJsPWdlKCJjaGFpblRhYmxlIik7IHRibC5pbm5lckhUTUw9IiI7CiAgLy8gSGVhZGVyCiAgdGJsLmlubmVySFRNTD0iPGRpdiBzdHlsZT0nZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMmZyIDFmciAxZnI7Z2FwOjZweDtwYWRkaW5nOjRweCA4cHg7bWFyZ2luLWJvdHRvbTo0cHgnPiIrCiAgICAiPGRpdiBjbGFzcz0nY2snPlN0cmlrZTwvZGl2PjxkaXYgY2xhc3M9J2NrJz5QcmVtaXVtPC9kaXY+PGRpdiBjbGFzcz0nY2snPk1hcms8L2Rpdj48ZGl2IGNsYXNzPSdjayc+VHlwZTwvZGl2PjwvZGl2PiI7CiAgcm93cy5mb3JFYWNoKGZ1bmN0aW9uKHIpewogICAgdmFyIGlzSXRtPShPQy50eXBlPT09ImNhbGwiJiZyLnN0cmlrZTxwcmljZSl8fChPQy50eXBlPT09InB1dCImJnIuc3RyaWtlPnByaWNlKTsKICAgIHZhciByb3c9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgiZGl2Iik7CiAgICByb3cuY2xhc3NOYW1lPSJjaGFpbi1yb3ciKyhyLmF0bT8iIGF0bSI6IiIpOwogICAgdmFyIG1vbmV5bmVzcz1yLmF0bT8iQVRNIjppc0l0bT8iSVRNIjoiT1RNIjsKICAgIHZhciBtY29sPXIuYXRtPyJ2YXIoLS15KSI6aXNJdG0/InZhcigtLWcpIjoidmFyKC0tdDMpIjsKICAgIHJvdy5pbm5lckhUTUw9CiAgICAgICI8ZGl2PjxkaXYgY2xhc3M9J2N2Jz4kIityLnN0cmlrZS50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj4iKwogICAgICAiPGRpdiBzdHlsZT0nZm9udC1zaXplOjlweDtjb2xvcjoiK21jb2wrIjtmb250LXdlaWdodDo3MDAnPiIrbW9uZXluZXNzKyI8L2Rpdj48L2Rpdj4iKwogICAgICAiPGRpdj48ZGl2IGNsYXNzPSdjdicgc3R5bGU9J2NvbG9yOnZhcigtLWIpJz4kIisoci5wcmVtPjA/ci5wcmVtLnRvRml4ZWQoMyk6IuKAlCIpKyI8L2Rpdj4iKwogICAgICAiPGRpdiBzdHlsZT0nZm9udC1zaXplOjlweDtjb2xvcjp2YXIoLS10MyknPiIrKHIuaXY+MD8iSVY6IityLml2KyIlIjoi4oCUIikrIjwvZGl2PjwvZGl2PiIrCiAgICAgICI8ZGl2PjxkaXYgY2xhc3M9J2N2JyBzdHlsZT0nZm9udC1zaXplOjEycHgnPiIrKHIubWFyaz4wPyIkIityLm1hcmsudG9GaXhlZCgwKToiTi9BIikrIjwvZGl2PiIrCiAgICAgICI8ZGl2IHN0eWxlPSdmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKSc+Iisoci5vaT4wPyJPSToiK3Iub2k6IiIpKyI8L2Rpdj48L2Rpdj4iKwogICAgICAiPGRpdiBzdHlsZT0nZm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOiIrKE9DLnR5cGU9PT0iY2FsbCI/InZhcigtLWIpIjoidmFyKC0tcikiKSsiJz4iK09DLnR5cGUudG9VcHBlckNhc2UoKSsiPC9kaXY+IjsKICAgIGlmKHIuZm91bmQmJnIubWFyaz4wKXsKICAgICAgcm93LnN0eWxlLmN1cnNvcj0icG9pbnRlciI7CiAgICAgIHJvdy5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgiLmNoYWluLXJvdyIpLmZvckVhY2goZnVuY3Rpb24oeCl7CiAgICAgICAgICB4LmNsYXNzTGlzdC5yZW1vdmUoInNlbC1jIiwic2VsLXAiKTt9KTsKICAgICAgICByb3cuY2xhc3NMaXN0LmFkZChPQy50eXBlPT09ImNhbGwiPyJzZWwtYyI6InNlbC1wIik7CiAgICAgICAgT0Muc3RyaWtlPXIuc3RyaWtlOyBPQy5tYXJrPXIubWFyazsgT0Muc3ltPXIuc3ltOwogICAgICAgIGdlKCJwbENhbGMiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7CiAgICAgICAgZ2UoInNlbFN1bW1hcnkiKS50ZXh0Q29udGVudD0oT0MudHlwZT09PSJjYWxsIj8i8J+TiCI6IvCfk4kiKSsiICIrT0MudHlwZS50b1VwcGVyQ2FzZSgpKwogICAgICAgICAgIiB8IFN0cmlrZSAkIityLnN0cmlrZS50b0xvY2FsZVN0cmluZygpKyIgfCBNYXJrICQiK3IubWFyaysiIHwgRXhwaXJ5ICIrT0MuZXhwaXJ5OwogICAgICAgIGdlKCJidXlCdG4iKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7CiAgICAgICAgZ2UoImJ1eUJ0biIpLnRleHRDb250ZW50PSJQbGFjZSAiK09DLnR5cGUudG9VcHBlckNhc2UoKSsiIE9yZGVyIOKAlCAkIisoci5wcmVtKk9DLmxvdHMpLnRvRml4ZWQoMyk7CiAgICAgICAgY2FsY1BMKCk7CiAgICAgIH07CiAgICB9IGVsc2UgewogICAgICByb3cuc3R5bGUub3BhY2l0eT0iMC40Ijsgcm93LnN0eWxlLmN1cnNvcj0iZGVmYXVsdCI7CiAgICB9CiAgICB0YmwuYXBwZW5kQ2hpbGQocm93KTsKICB9KTsKfQoKZnVuY3Rpb24gYWRqT0xvdHMoZCl7CiAgT0MubG90cz1NYXRoLm1heCgxLE1hdGgubWluKDIwLE9DLmxvdHMrZCkpOwogIGdlKCJvTG90cyIpLnRleHRDb250ZW50PU9DLmxvdHM7CiAgdmFyIHByaWNlPXBhcnNlRmxvYXQoKGdlKCJoUCIpfHx7fSkudGV4dENvbnRlbnQucmVwbGFjZSgvWyQsXS9nLCIiKSl8fDc5MDAwOwogIHZhciBwcmVtPXJvdW5kMihPQy5tYXJrKjAuMDAxKk9DLmxvdHMpOwogIGlmKGdlKCJidXlCdG4iKSlnZSgiYnV5QnRuIikudGV4dENvbnRlbnQ9IlBsYWNlICIrT0MudHlwZS50b1VwcGVyQ2FzZSgpKyIgT3JkZXIg4oCUICQiK3ByZW07CiAgY2FsY1BMKCk7Cn0KZnVuY3Rpb24gcm91bmQyKHYpe3JldHVybiBNYXRoLnJvdW5kKHYqMTAwMCkvMTAwMDt9CgpmdW5jdGlvbiBjYWxjUEwoKXsKICBpZighT0MubWFya3x8IU9DLnN0cmlrZSlyZXR1cm47CiAgdmFyIHRhcmdldD1wYXJzZUZsb2F0KGdlKCJvVGFyZ2V0IikudmFsdWUpfHwwOwogIHZhciBwcmljZT1wYXJzZUZsb2F0KChnZSgiaFAiKXx8e30pLnRleHRDb250ZW50LnJlcGxhY2UoL1skLF0vZywiIikpfHw3OTAwMDsKICB2YXIgcHJlbWl1bT1PQy5tYXJrKjAuMDAxKk9DLmxvdHM7IC8vIGNvc3QgaW4gVVNECiAgdmFyIHByPWdlKCJwbFJlc3VsdCIpOyBwci5zdHlsZS5kaXNwbGF5PSJibG9jayI7CiAgZ2UoInBsQ29zdCIpLnRleHRDb250ZW50PSItJCIrcHJlbWl1bS50b0ZpeGVkKDMpOwogIHZhciBwcm9maXQ9MDsgdmFyIGJlPTA7CiAgaWYoT0MudHlwZT09PSJjYWxsIil7CiAgICBiZT1PQy5zdHJpa2UrT0MubWFyazsgLy8gYnJlYWtldmVuCiAgICBpZih0YXJnZXQ+T0Muc3RyaWtlKXtwcm9maXQ9TWF0aC5tYXgoMCwodGFyZ2V0LU9DLnN0cmlrZSkqMC4wMDEqT0MubG90cy1wcmVtaXVtKTt9CiAgfSBlbHNlIHsKICAgIGJlPU9DLnN0cmlrZS1PQy5tYXJrOwogICAgaWYodGFyZ2V0PE9DLnN0cmlrZSl7cHJvZml0PU1hdGgubWF4KDAsKE9DLnN0cmlrZS10YXJnZXQpKjAuMDAxKk9DLmxvdHMtcHJlbWl1bSk7fQogIH0KICBnZSgicGxQcm9maXQiKS50ZXh0Q29udGVudD1wcm9maXQ+MD8iKyQiK3Byb2ZpdC50b0ZpeGVkKDMpOiLigJQiOwogIGdlKCJwbFByb2ZpdCIpLnN0eWxlLmNvbG9yPXByb2ZpdD4wPyJ2YXIoLS1nKSI6InZhcigtLXQzKSI7CiAgZ2UoInBsQkUiKS50ZXh0Q29udGVudD0iJCIrTWF0aC5yb3VuZChiZSkudG9Mb2NhbGVTdHJpbmcoKTsKICB2YXIgcm9pPXByZW1pdW0+MD9NYXRoLm1pbihwcm9maXQvcHJlbWl1bSoxMDAsMzAwKTowOwogIGdlKCJwbEZpbGwiKS5zdHlsZS53aWR0aD1yb2krIiUiOwogIGdlKCJwbEZpbGwiKS5zdHlsZS5iYWNrZ3JvdW5kPXByb2ZpdD4wPyJ2YXIoLS1nKSI6InZhcigtLXIpIjsKICBnZSgicGxOb3RlIikudGV4dENvbnRlbnQ9dGFyZ2V0PwogICAgKHByb2ZpdD4wPyJQcm9maXQgaWYgQlRDIHJlYWNoZXMgJCIrdGFyZ2V0LnRvTG9jYWxlU3RyaW5nKCkrIiB8IFJPSTogKyIrcm9pLnRvRml4ZWQoMCkrIiUiOgogICAgIk5lZWQgQlRDICIrKE9DLnR5cGU9PT0iY2FsbCI/ImFib3ZlIjoiYmVsb3ciKSsiICQiK01hdGgucm91bmQoYmUpLnRvTG9jYWxlU3RyaW5nKCkrIiB0byBwcm9maXQiKToKICAgICJFbnRlciBhIEJUQyB0YXJnZXQgcHJpY2UgdG8gc2VlIGVzdGltYXRlZCBQJkwiOwp9CgpmdW5jdGlvbiBleGVjTWFuT3B0KCl7CiAgaWYoIU9DLnN5bXx8IU9DLm1hcmspe2dlKCJvTXNnIikudGV4dENvbnRlbnQ9IlNlbGVjdCBhIHN0cmlrZSBmaXJzdCI7cmV0dXJuO30KICB2YXIgcHJlbWl1bT1yb3VuZDIoT0MubWFyayowLjAwMSpPQy5sb3RzKTsKICBnZSgib01zZyIpLnRleHRDb250ZW50PSJQbGFjaW5nIG9yZGVyLi4uIjsKICB4aHIoIi9hcGkvbWFudWFsX29wdCIse3R5cGU6T0MudHlwZSxzeW1ib2w6T0Muc3ltLGxvdHM6T0MubG90c30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpewogICAgICBnZSgib01zZyIpLnRleHRDb250ZW50PSLinIUgIitPQy50eXBlLnRvVXBwZXJDYXNlKCkrIiBvcGVuZWQ6ICIrT0Muc3ltKyIgfCAkIitwcmVtaXVtOwogICAgICBnZSgib01zZyIpLnN0eWxlLmNvbG9yPSJ2YXIoLS1nKSI7CiAgICAgIGdlKCJwbENhbGMiKS5zdHlsZS5kaXNwbGF5PSJub25lIjsKICAgIH0gZWxzZSB7CiAgICAgIGdlKCJvTXNnIikudGV4dENvbnRlbnQ9IuKdjCAiKyhyJiZyLm1lc3NhZ2V8fCJPcmRlciBmYWlsZWQiKTsKICAgICAgZ2UoIm9Nc2ciKS5zdHlsZS5jb2xvcj0idmFyKC0tcikiOwogICAgfQogIH0pOwp9CgovLyBJbml0IGNoYWluIHdoZW4gb3B0aW9ucyBwYW5lbCBvcGVucwp2YXIgX29wdEluaXRlZD1mYWxzZTsKZnVuY3Rpb24gdG9nZ2xlT3B0cyhvbil7CiAgeGhyKCIvYXBpL29wdHMvdG9nZ2xlIix7ZW5hYmxlZDpvbn0sZnVuY3Rpb24ocil7CiAgICBnZSgib3B0c1BhbmVsIikuc3R5bGUuZGlzcGxheT0ociYmci5vcHRzX21vZGUpPyJibG9jayI6Im5vbmUiOwogICAgaWYociYmci5vcHRzX21vZGUmJiFfb3B0SW5pdGVkKXtfb3B0SW5pdGVkPXRydWU7aW5pdENoYWluKCk7fQogIH0pOwp9CmZ1bmN0aW9uIHRvZ2dsZU9wdHMob24pewogIHhocigiL2FwaS9vcHRzL3RvZ2dsZSIse2VuYWJsZWQ6b259LGZ1bmN0aW9uKHIpewogICAgZ2UoIm9wdHNQYW5lbCIpLnN0eWxlLmRpc3BsYXk9KHImJnIub3B0c19tb2RlKT8iYmxvY2siOiJub25lIjsKICB9KTsKfQpmdW5jdGlvbiBjaGtPcHQodCl7CiAgdmFyIGVsPWdlKCJvUmVzIik7ZWwuc3R5bGUuZGlzcGxheT0iYmxvY2siO2VsLnRleHRDb250ZW50PSJDaGVja2luZy4uLiI7CiAgeGhyKCIvYXBpL29wdHMvZmluZCIse3R5cGU6dCxpdG06ZmFsc2V9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5mb3VuZCllbC5pbm5lckhUTUw9IjxiPiIrci5zeW1ib2wrIjwvYj48YnI+U3RyaWtlICQiKyhyLnN0cmlrZXx8MCkudG9Mb2NhbGVTdHJpbmcoKSsiIHwgTWFyayAkIisoci5tYXJrfHwwKS50b0ZpeGVkKDIpKyIgfCBQcmVtaXVtICQiKyhyLnByZW1pdW1fdXNkfHwwKS50b0ZpeGVkKDIpKyhyLml2PyIgfCBJViAiK3IuaXYrIiUiOiIiKSsiPGJyPiIrci5tb25leW5lc3MrIiB8IEV4cGlyeSAiK3IuZXhwaXJ5OwogICAgZWxzZSBlbC50ZXh0Q29udGVudD0iTm8gIit0KyIgZm91bmQuIEV4cGlyeTogIisoKHImJnIuZXhwaXJ5KXx8Ij8iKTsKICB9KTsKfQpmdW5jdGlvbiBjaGtTdCgpewogIHZhciBlbD1nZSgib1JlcyIpO2VsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtlbC50ZXh0Q29udGVudD0iQ2hlY2tpbmcuLi4iOwogIHhocigiL2FwaS9vcHRzL3N0cmFkZGxlIix7fSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuZm91bmQpZWwuaW5uZXJIVE1MPSI8Yj5TdHJhZGRsZTwvYj48YnI+VG90YWw6ICQiKyhyLnRvdGFsX3ByZW1pdW1fdXNkfHwwKS50b0ZpeGVkKDIpKyI8YnI+QkUgdXA6ICQiK01hdGgucm91bmQoci5icmVha2V2ZW5fdXB8fDApLnRvTG9jYWxlU3RyaW5nKCkrIiB8IGRvd246ICQiK01hdGgucm91bmQoci5icmVha2V2ZW5fZG93bnx8MCkudG9Mb2NhbGVTdHJpbmcoKTsKICAgIGVsc2UgZWwudGV4dENvbnRlbnQ9IkNhbm5vdCBidWlsZCBzdHJhZGRsZSByaWdodCBub3cuIjsKICB9KTsKfQpmdW5jdGlvbiBzZXRMRihmKXsKICBTVC5sZj1mOwogIHZhciBtPXsiIjoibGZhIiwiVFJBREUiOiJsZnQiLCJXQVJOIjoibGZ3IiwiRVJST1IiOiJsZmUifTsKICBPYmplY3Qua2V5cyhtKS5mb3JFYWNoKGZ1bmN0aW9uKGspe3ZhciBlbD1nZShtW2tdKTtpZihlbCllbC5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsaz09PWYpO30pOwogIHJlbmRlckxvZ3MoKTsKfQpmdW5jdGlvbiByZW5kZXIocyl7CiAgaWYoIXMpcmV0dXJuOwogIGlmKHMuY29ubmVjdGVkKXtnZSgiY29ubmVjdENhcmQiKS5zdHlsZS5kaXNwbGF5PSJub25lIjtnZSgibGl2ZURhc2giKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7fQogIHZhciBydW49cy5jb25uZWN0ZWQmJnMucnVubmluZyYmIXMuaGFsdGVkOwogIGdlKCJzUGlsbCIpLmNsYXNzTmFtZT0icGlsbCAiKyhzLmhhbHRlZD8icC13YXJuIjpydW4/InAtbGl2ZSI6InAtb2ZmIik7CiAgc3QoInNUeHQiLHMuaGFsdGVkPyJIQUxURUQiOnJ1bj8iTGl2ZSI6IlN0b3BwZWQiKTsKICBzdCgiaFAiLHMucHJpY2U/IiQiK3MucHJpY2UudG9Mb2NhbGVTdHJpbmcoKToiJC0tIik7CiAgLy8gVXBkYXRlIEFUTSBpbiBjaGFpbiB3aGVuIHByaWNlIGNoYW5nZXMKICBpZihzLnByaWNlJiZfb3B0SW5pdGVkKXt2YXIgYXRtMj1NYXRoLnJvdW5kKHMucHJpY2UvNTAwKSo1MDA7dmFyIGhwRWw9Z2UoIm9Mb3RzIik7fSAvLyBjaGFpbiBhdXRvLXVwZGF0ZXMgdmlhIGxvYWRDaGFpbgogIHZhciByZz1zLnJlZ2ltZXx8IiI7IHZhciBtYj1zLm1hY3JvX2JpYXN8fCJuZXV0cmFsIjsKICB2YXIgcmM9Z2UoImhSIik7cmMudGV4dENvbnRlbnQ9cmd8fCItLSI7cmMuY2xhc3NOYW1lPSJjaGlwICIrKHJnLmluZGV4T2YoIkJVTEwiKT49MD8iY2ciOnJnLmluZGV4T2YoIkJFQVIiKT49MD8iY3IyIjoiY24iKTsKICBzdCgiaFMiLHMuc3RyYXRlZ3l8fCItLSIpO3N0KCJoViIscy52b2xfcmVnaW1lfHwiLS0iKTsKICB2YXIgaDF0PXMuaDFfdHJlbmR8fCJuZXV0cmFsIjsKICB2YXIgaDFlbD1nZSgiaEgxIik7CiAgaWYoaDFlbCl7aDFlbC50ZXh0Q29udGVudD0iMUg6ICIraDF0LnRvVXBwZXJDYXNlKCk7aDFlbC5jbGFzc05hbWU9ImNoaXAgIisoaDF0PT09ImJ1bGwiPyJjZyI6aDF0PT09ImJlYXIiPyJjcjIiOiJjbiIpO30KICB2YXIgcmI9Z2UoInJCYXIiKTtyYi5jbGFzc05hbWU9InJiYXIgIisocmcuaW5kZXhPZigiQlVMTCIpPj0wPyJyYi1iIjpyZy5pbmRleE9mKCJCRUFSIik+PTA/InJiLXIiOnJnPT09IlNJREVXQVlTIj8icmItdyI6InJiLW4iKTsKICByYi50ZXh0Q29udGVudD1yZysiIFx1MjAxNCAiKyhzLnN0cmF0ZWd5fHwiQ2FsY3VsYXRpbmciKTsKICB2YXIgc2M9cy5jb25mX2xvbmd8fDA7c3QoImNOIixzY3x8Ii0tIik7CiAgdmFyIGFyYz1nZSgiY0FyYyIpO2FyYy5zdHlsZS5zdHJva2VEYXNob2Zmc2V0PTE3NS45LShzYy8xMDAqMTc1LjkpO2FyYy5zdHlsZS5zdHJva2U9c2M+PTcwPyIjMDBiMzg2IjpzYz49NTA/IiNmNTllMGIiOiIjZTc0YzNjIjsKICBnZSgiY04iKS5zdHlsZS5jb2xvcj1zYz49NzA/InZhcigtLWcpIjpzYz49NTA/InZhcigtLXkpIjoidmFyKC0tcikiOwogIHN0KCJjRCIscy5zdHJhdGVneT09PSJXQUlUIj8iV0FJVCI6KHMuZGlyZWN0aW9ufHxyZ3x8IldBSVQiKS50b1VwcGVyQ2FzZSgpKTsKICB2YXIgdHJlbmRzPXMudHJlbmRzfHx7fTsgdmFyIHRTdHI9T2JqZWN0LmVudHJpZXModHJlbmRzKS5tYXAoZnVuY3Rpb24oZSl7cmV0dXJuIGVbMF0rIjoiK2VbMV1bMF0udG9VcHBlckNhc2UoKTt9KS5qb2luKCIgIik7CiAgdmFyIGVxPXMuZW50cnlfcXVhbGl0eXx8e307CiAgdmFyIGZ1bmRUeHQ9cy5mdW5kaW5nIT09dW5kZWZpbmVkPyJmdW5kPSIrKHMuZnVuZGluZz49MD8iKyI6IiIpK3MuZnVuZGluZy50b0ZpeGVkKDMpKyIlIjoiIjsKICB2YXIgb2lUeHQ9cy5vaV90cmVuZCYmcy5vaV90cmVuZCE9PSJmbGF0Ij8iT0k9IitzLm9pX3RyZW5kOiIiOwogIHN0KCJjRHQiLCJDb252PSIrc2MrIiB8IEFEWD0iKyhzLmFkeHx8MCkrIiB8IFJTST0iKyhzLnJzaXx8MCkrIiB8ICIrbWIudG9VcHBlckNhc2UoKSsiIHwgIit0U3RyKyhmdW5kVHh0PyIgfCAiK2Z1bmRUeHQ6IiIpKyhvaVR4dD8iIHwgIitvaVR4dDoiIikpOwogIC8vIFZldG8gcmVhc29uIHNob3duIHByb21pbmVudGx5IHdoZW4gY29udj0wCiAgdmFyIHZldG9FbD1nZSgidmV0b0JhciIpOwogIGlmKHZldG9FbCl7CiAgICBpZihzLnZldG8mJnNjPT09MCl7dmV0b0VsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjt2ZXRvRWwudGV4dENvbnRlbnQ9IuKPuCAiK3MudmV0by5yZXBsYWNlKC9fL2csIiAiKTt2ZXRvRWwuc3R5bGUuY29sb3I9InZhcigtLXkpIjt9CiAgICBlbHNlIGlmKHMudmV0byl7dmV0b0VsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjt2ZXRvRWwudGV4dENvbnRlbnQ9IuKaoCAiK3MudmV0by5yZXBsYWNlKC9fL2csIiAiKTt2ZXRvRWwuc3R5bGUuY29sb3I9InZhcigtLXQzKSI7fQogICAgZWxzZXt2ZXRvRWwuc3R5bGUuZGlzcGxheT0ibm9uZSI7fQogIH0KICB2YXIgcGxzPXMucGlsbGFyc3x8e307dmFyIHBoPSIiOwogIE9iamVjdC5rZXlzKHBscykuZm9yRWFjaChmdW5jdGlvbihrKXt2YXIgdj1wbHNba107dmFyIHBjdD12Lm0+MD9NYXRoLnJvdW5kKHYucy92Lm0qMTAwKTowO3ZhciBjb2w9UENba118fCJ2YXIoLS1nKSI7cGgrPSI8ZGl2IGNsYXNzPSdwcm93Jz48ZGl2IGNsYXNzPSdwbic+IitrKyI8L2Rpdj48ZGl2IGNsYXNzPSdwdCc+PGRpdiBjbGFzcz0ncGYnIHN0eWxlPSd3aWR0aDoiK3BjdCsiJTtiYWNrZ3JvdW5kOiIrY29sKyInPjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BzJyBzdHlsZT0nY29sb3I6Iitjb2wrIic+Iit2LnMrIi8iK3YubSsiPC9kaXY+PC9kaXY+Ijt9KTsKICBzaCgicGlsRGl2IixwaCk7c3QoImlBIixzLmFkeHx8Ii0tIik7c3QoImlCIixzLmJ3P3MuYncrIiUiOiItLSIpO3N0KCJpVCIscy5hdHJfcGN0P3MuYXRyX3BjdCsiJSI6Ii0tIik7CiAgc3QoInNTdGF0dXMiLHMuc3RhdHVzfHwiLS0iKTtzdCgic1NOIixzLnNjYW5fbnx8MCk7CiAgLy8gSGlnaGxpZ2h0IHdoZW4gY2xvc2UgdG8gdGhyZXNob2xkCiAgdmFyIHNFbD1nZSgic1N0YXR1cyIpOwogIGlmKHNFbCAmJiBzLnN0YXR1cyAmJiBzLnN0YXR1cy5pbmRleE9mKCJuZWVkPSIpPj0wKXtzRWwuc3R5bGUuY29sb3I9InZhcigtLXkpIjt9CiAgZWxzZSBpZihzRWwpe3NFbC5zdHlsZS5jb2xvcj0iIjt9CiAgaWYocy5uZXh0X3NjYW4pU1QubmV4dEF0PW5ldyBEYXRlKHMubmV4dF9zY2FuKTsKICB2YXIgcHA9cy5vcGVuX3Bvc3x8W107dmFyIHBoMj0iIjsKICBwcC5mb3JFYWNoKGZ1bmN0aW9uKHApe3ZhciBuZWc9cC51cG5sPDA7cGgyKz0iPGRpdiBjbGFzcz0ncG9zIHBvcy0iKyhuZWc/InMiOiJsIikrIic+PGRpdiBjbGFzcz0ncGgnPjxzcGFuIGNsYXNzPSdwc3ltJz4iK3Auc3ltKyI8L3NwYW4+PHNwYW4gY2xhc3M9J2JhZGdlIGIiKyhwLnNpZGU9PT0ibG9uZyI/ImwiOiJzaCIpKyInPiIrcC5zaWRlLnRvVXBwZXJDYXNlKCkrIjwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSdwZyc+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+RW50cnk8L2Rpdj48ZGl2IGNsYXNzPSdwaXYnPiQiK3AuZW50cnkudG9Mb2NhbGVTdHJpbmcoKSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+TG90czwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+IitwLmxvdHMrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlVQTDwvZGl2PjxkaXYgY2xhc3M9J3BpdiAiKyhuZWc/InBpciI6InBpZyIpKyInPiIrKHAudXBubD49MD8iKyI6IiIpK3AudXBubCsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+TWFyazwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+JCIrKHAubWFya3x8cC5lbnRyeSkudG9Mb2NhbGVTdHJpbmcoKSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+U3RvcDwvZGl2PjxkaXYgY2xhc3M9J3BpdiBwaXInPiQiK3Auc3RvcC50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5UUDwvZGl2PjxkaXYgY2xhc3M9J3BpdiBwaWcnPiQiK3AudHAudG9Mb2NhbGVTdHJpbmcoKSsiPC9kaXY+PC9kaXY+PC9kaXY+PC9kaXY+Ijt9KTsKICBzaCgicGVycERpdiIscGgyKTsKICB2YXIgb3A9cy5vcHRzX3Bvc3x8W107dmFyIG9oPSIiOwogIG9wLmZvckVhY2goZnVuY3Rpb24obyl7dmFyIGlzQz1vLnR5cGU9PT0iQ0FMTCI7CiAgICB2YXIgZmxvb3JCYXI9by5mbG9vcl9hY3RpdmUKICAgICAgPyI8ZGl2IHN0eWxlPSdtYXJnaW4tdG9wOjhweDtwYWRkaW5nOjdweCAxMHB4O2JhY2tncm91bmQ6cmdiYSgwLDE3OSwxMzQsLjEyKTtib3JkZXItcmFkaXVzOjZweDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMCwxNzksMTM0LC4zKTtmb250LXNpemU6MTFweDtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW4nPjxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS1nKTtmb250LXdlaWdodDo3MDAnPvCflJIgRmxvb3IgYWN0aXZlPC9zcGFuPjxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS1nKTtmb250LXdlaWdodDo4MDAnPkV4aXQgPCAkIitvLmZsb29yX3ByaWNlKyIgKCsiK28uZmxvb3JfcGN0KyIlKTwvc3Bhbj48L2Rpdj4iCiAgICAgIDoiPGRpdiBzdHlsZT0nbWFyZ2luLXRvcDo4cHg7cGFkZGluZzo3cHggMTBweDtiYWNrZ3JvdW5kOiNmOGZhZmM7Ym9yZGVyLXJhZGl1czo2cHg7Ym9yZGVyOnZhcigtLWJkcik7Zm9udC1zaXplOjExcHg7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuJz48c3BhbiBzdHlsZT0nY29sb3I6dmFyKC0tdDMpJz5GbG9vcjogZmlyc3QgcHJvZml0IHRpY2s8L3NwYW4+PHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLXIpJz5IYXJkIFNMICQiK28uc2xfcHJpY2UrIjwvc3Bhbj48L2Rpdj4iOwogICAgb2grPSI8ZGl2IGNsYXNzPSdwb3MgcG9zLW8nPjxkaXYgY2xhc3M9J3BoJz48c3BhbiBjbGFzcz0ncHN5bScgc3R5bGU9J2ZvbnQtc2l6ZToxMnB4Jz4iK28uc3ltKyI8L3NwYW4+PHNwYW4gY2xhc3M9J2JhZGdlIGIiKyhpc0M/ImMiOiJwIikrIic+IitvLnR5cGUrIjwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSdwZyc+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+RW50cnk8L2Rpdj48ZGl2IGNsYXNzPSdwaXYnPiQiK28uZW50cnkrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPk1hcms8L2Rpdj48ZGl2IGNsYXNzPSdwaXYnPiQiK28ubWFyaysiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+UCZMPC9kaXY+PGRpdiBjbGFzcz0ncGl2ICIrKG8ucGN0PDA/InBpciI6InBpZyIpKyInPiIrKG8ucGN0Pj0wPyIrIjoiIikrby5wY3QrIiU8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5QZWFrPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpZyc+JCIrby5wZWFrKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5UUDwvZGl2PjxkaXYgY2xhc3M9J3BpdiBwaWcnPiQiK28udHBfcHJpY2UrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlNMPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpcic+JCIrby5zbF9wcmljZSsiPC9kaXY+PC9kaXY+PC9kaXY+IitmbG9vckJhcisiPC9kaXY+Ijt9KTsKICBzaCgib3B0c0RpdiIsb2gpOwogIHZhciBjYXA9cy5jYXBpdGFsfHwwLHNjMj1zLnN0YXJ0X2NhcHx8MCxwcDI9cy5wbmxfcGN0fHwwOwogIHN0KCJ3QSIsY2FwPyIkIitjYXAudG9GaXhlZCgyKToiJC0tIik7c3QoIndTdCIsc2MyPyJTdGFydGVkICQiK3NjMi50b0ZpeGVkKDIpOiIiKTsKICB2YXIgd3BFbD1nZSgid1AiKTt3cEVsLnRleHRDb250ZW50PShwcDI+PTA/IisiOiIiKStwcDIudG9GaXhlZCgyKSsiJSI7d3BFbC5zdHlsZS5jb2xvcj1wcDI+PTA/InZhcigtLWcpIjoidmFyKC0tcikiOwogIC8vIFdhbGxldCBQJkwgPSByZWFsIGJhbGFuY2UgY2hhbmdlIGluY2x1ZGluZyBmZWVzL2Z1bmRpbmcKICB2YXIgd1BubD1zLnBubF91c2R8fDA7CiAgc3QoIndOIiwiV2FsbGV0IFAmTCAkIisod1BubD49MD8iKyI6IiIpK3dQbmwudG9GaXhlZCgyKSk7CiAgLy8gVHJhZGUgUCZMID0gYm90IGNsb3NlZCB0cmFkZXMgb25seQogIHZhciB0UG5sPXMudHJhZGVfcG5sX3VzZHx8MDsKICB2YXIgdEVsPWdlKCJ0cmFkZVBubFJvdyIpOwogIGlmKHRFbCkgdEVsLnRleHRDb250ZW50PSJCb3QgdHJhZGVzIFAmTCAkIisodFBubD49MD8iKyI6IiIpK3RQbmwudG9GaXhlZCg0KTsKICBzdCgic1dSIixzLndpbl9yYXRlIT1udWxsP3Mud2luX3JhdGUrIiUiOiItLSIpO3N0KCJzVFIiLHMudG90YWxfdHJhZGVzfHwwKTsKICBpZihzLnVzZXJfc2V0dGluZ3MpewogICAgX2xvdHM9cy51c2VyX3NldHRpbmdzLmxvdF9zaXplfHwxOyBnZSgibG90c1ZhbCIpLnRleHRDb250ZW50PV9sb3RzOwogICAgX2RhaWx5PXMudXNlcl9zZXR0aW5ncy5tYXhfZGFpbHl8fDEwOyBnZSgiZGFpbHlWYWwiKS50ZXh0Q29udGVudD1fZGFpbHk7CiAgICB2YXIgdXNlZD1zLnVzZXJfc2V0dGluZ3MuZGFpbHlfdHJhZGVzfHwwOwogICAgdmFyIGVsPWdlKCJkYWlseVVzZWQiKTsgaWYoZWwpIGVsLnRleHRDb250ZW50PXVzZWQrIiB1c2VkIHRvZGF5ICgiKyhfZGFpbHktdXNlZCkrIiByZW1haW5pbmcpIjsKICAgIHZhciBzbT1zLnVzZXJfc2V0dGluZ3MuYWN0aXZlX21vZGV8fCJub3JtYWwiOwogICAgaWYoc20hPT1fbW9kZSl7X21vZGU9c207c2V0TW9kZShzbSk7fQogICAgaWYocy51c2VyX3NldHRpbmdzLm1vZGVfbG9ja2VkKXt2YXIgbW49Z2UoIm1vZGVOb3RlIik7aWYobW4pe21uLnRleHRDb250ZW50PSJQUk8gbG9ja2VkIOKAlCBuZWVkICQ1MDArIGJhbGFuY2UiO21uLnN0eWxlLmNvbG9yPSJ2YXIoLS1yKSI7fX0KICB9CiAgdmFyIG90PWdlKCJ0b2dPIik7aWYob3Qpb3QuY2hlY2tlZD0hIXMub3B0c19tb2RlOwogIGdlKCJvcHRzUGFuZWwiKS5zdHlsZS5kaXNwbGF5PXMub3B0c19tb2RlPyJibG9jayI6Im5vbmUiOwogIGlmKHMuZ3VhcmRyYWlscyl7dmFyIGdrPU9iamVjdC5rZXlzKHMuZ3VhcmRyYWlscyk7dmFyIGdoPSIiO2drLmZvckVhY2goZnVuY3Rpb24oayl7Z2grPSI8ZGl2IGNsYXNzPSdncmFpbC1yb3cnPjxzcGFuIGNsYXNzPSdncmsnPiIraysiPC9zcGFuPjxzcGFuIGNsYXNzPSdncnYnPiIrcy5ndWFyZHJhaWxzW2tdKyI8L3NwYW4+PC9kaXY+Ijt9KTtzaCgiZ3JMaXN0IixnaCk7fQogIGlmKHMubG9ncylTVC5sb2dzPXMubG9ncztpZihzLnRyYWRlcylTVC50cmFkZXM9cy50cmFkZXM7CiAgc3QoImxDbnQiLFNULmxvZ3MubGVuZ3RoKyIgZW50cmllcyIpOwogIGlmKGdlKCJwLWxvZ3MiKS5jbGFzc0xpc3QuY29udGFpbnMoInNob3ciKSlyZW5kZXJMb2dzKCk7CiAgaWYoZ2UoInAtdHJhZGVzIikuY2xhc3NMaXN0LmNvbnRhaW5zKCJzaG93IikpcmVuZGVyVHJhZGVzKCk7Cn0KZnVuY3Rpb24gcmVuZGVyVHJhZGVzKCl7CiAgc3QoInRDbnQiLFNULnRyYWRlcy5sZW5ndGgrIiB0cmFkZXMiKTsKICBpZighU1QudHJhZGVzLmxlbmd0aCl7c2goInRMaXN0IiwiPGRpdiBjbGFzcz0nZW1wdHknPk5vIHRyYWRlcyB5ZXQ8L2Rpdj4iKTtyZXR1cm47fQogIHZhciBoPSIiOwogIFNULnRyYWRlcy5mb3JFYWNoKGZ1bmN0aW9uKHQpewogICAgdmFyIG9wZW49dC5leGl0PT1udWxsLHNkPXQuc2lkZXx8IiI7CiAgICB2YXIgaWM9c2Q9PT0ibG9uZyI/InRpLWwiOnNkPT09InNob3J0Ij8idGktcyI6c2Q9PT0iY2FsbCI/InRpLWMiOiJ0aS1wIjsKICAgIHZhciBpY289c2Q9PT0ibG9uZyI/IiYjODU5MzsiOnNkPT09InNob3J0Ij8iJiM4NTk1OyI6c2Q9PT0iY2FsbCI/IkMiOiJQIjsKICAgIHZhciBwYz1vcGVuPyJ0cG4iOih0Lndvbj8idHBnIjoidHByIikscHY9b3Blbj8iT3Blblx1MjAyNiI6KHQud29uPyIrIjoiIikrKHQucG5sfHwwKS50b0ZpeGVkKDQpOwogICAgdmFyIHRtPXQudGltZT90LnRpbWUuc3Vic3RyKDUsMTEpLnJlcGxhY2UoIlQiLCIgIik6IiI7CiAgICBoKz0iPGRpdiBjbGFzcz0ndHItcm93Jz48ZGl2IGNsYXNzPSd0aWNvICIraWMrIic+IitpY28rIjwvZGl2PjxkaXYgY2xhc3M9J3RtaWQnPjxkaXYgY2xhc3M9J3RzeW0nPiIrKHQuc3ltfHwiQlRDVVNEIikrIjwvZGl2PjxkaXYgY2xhc3M9J3RtZXRhJz4iK3RtKyIgJm1pZGRvdDsgIisodC5yZWFzb258fCIiKSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ndHJpZ2h0Jz48ZGl2IGNsYXNzPSd0cG5sICIrcGMrIic+JCIrcHYrIjwvZGl2PjxkaXYgc3R5bGU9J2ZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKSc+IisodC5lbnRyeT8iQCQiK3QuZW50cnk6IiIpKyI8L2Rpdj48L2Rpdj48L2Rpdj4iOwogIH0pO3NoKCJ0TGlzdCIsaCk7Cn0KZnVuY3Rpb24gcmVuZGVyTG9ncygpewogIHZhciBmPVNULmxmP1NULmxvZ3MuZmlsdGVyKGZ1bmN0aW9uKGUpe3JldHVybiBlLmw9PT1TVC5sZjt9KTpTVC5sb2dzOwogIHZhciBoPSIiO2Yuc2xpY2UoMCwxNTApLmZvckVhY2goZnVuY3Rpb24oZSl7dmFyIGNscz0ibEkiO2lmKGUubD09PSJXQVJOIiljbHM9ImxXIjtlbHNlIGlmKGUubD09PSJFUlJPUiIpY2xzPSJsRSI7ZWxzZSBpZihlLmw9PT0iVFJBREUiKWNscz0ibFQiO2grPSI8ZGl2IGNsYXNzPSdscic+PHNwYW4gY2xhc3M9J2x0Jz4iK2UudCsiPC9zcGFuPjxzcGFuIGNsYXNzPSciK2NscysiJz4iK2UubSsiPC9zcGFuPjwvZGl2PiI7fSk7c2goImxCb3giLGgpOwp9CmZ1bmN0aW9uIGxvYWRBZG1pbigpewogIGlmKCFTVC5pc0FkbWluKXtnZSgiYWRtaW5QYW5lbCIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO3JldHVybjt9CiAgZ2UoImFkbWluUGFuZWwiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7CiAgeGhyKCIvYXBpL2FkbWluL3VzZXJzIixudWxsLGZ1bmN0aW9uKHIpewogICAgaWYoIXIpcmV0dXJuOwogICAgdmFyIGg9IiI7CiAgICBPYmplY3Qua2V5cyhyLnVzZXJzfHx7fSkuZm9yRWFjaChmdW5jdGlvbih1aWQpewogICAgICB2YXIgdT1yLnVzZXJzW3VpZF07CiAgICAgIGgrPSI8ZGl2IGNsYXNzPSdhdSc+PGRpdiBjbGFzcz0nYXUtbmFtZSc+IisodS5pc19hZG1pbj8iJiM5NzMzOyAiOiIiKSt1LnVzZXJuYW1lKyh1LmJvdF9ydW5uaW5nPyIgPHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLWcpO2ZvbnQtc2l6ZToxMHB4Jz4mIzk2Nzk7IExpdmU8L3NwYW4+IjoiIDxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS10Myk7Zm9udC1zaXplOjEwcHgnPk9mZmxpbmU8L3NwYW4+IikrIjwvZGl2PjxkaXYgY2xhc3M9J2F1LXN0YXRzJz48c3Bhbj4kIit1LmJhbGFuY2UudG9GaXhlZCgyKSsiPC9zcGFuPjxzcGFuPiIrdS50cmFkZXMrIiB0cmFkZXM8L3NwYW4+PC9kaXY+PC9kaXY+IjsKICAgIH0pOwogICAgc2goImF1TGlzdCIsaHx8IjxkaXYgY2xhc3M9J2VtcHR5Jz5ObyB1c2VycyB5ZXQ8L2Rpdj4iKTsKICAgIGlmKHIuaW52aXRlcyYmci5pbnZpdGVzLmxlbmd0aCl7dmFyIGloPSI8ZGl2IHN0eWxlPSdmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbTo0cHgnPlBlbmRpbmcgaW52aXRlIGNvZGVzOjwvZGl2PiI7ci5pbnZpdGVzLmZvckVhY2goZnVuY3Rpb24oYyl7aWgrPSI8ZGl2IGNsYXNzPSdpY29kZSc+IitjKyI8L2Rpdj4iO30pO3NoKCJuZXdJbnZpdGUiLGloKTtnZSgibmV3SW52aXRlIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO30KICB9KTsKfQpmdW5jdGlvbiBnZW5JbnZpdGUoKXsKICB4aHIoIi9hcGkvYWRtaW4vaW52aXRlIix7fSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuc3VjY2Vzcyl7c2goImludkNvZGUiLHIuY29kZSk7Z2UoImludkNvZGUiKS5jbGFzc05hbWU9Imljb2RlIjtnZSgibmV3SW52aXRlIikuaW5uZXJIVE1MPSI8ZGl2IHN0eWxlPSdmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbTo0cHgnPk5ldyBpbnZpdGUgY29kZTo8L2Rpdj48ZGl2IGNsYXNzPSdpY29kZSc+IityLmNvZGUrIjwvZGl2PjxkaXYgc3R5bGU9J2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOmNlbnRlcic+T25lLXRpbWUgdXNlIG9ubHk8L2Rpdj4iO2dlKCJuZXdJbnZpdGUiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7bG9hZEFkbWluKCk7fQogIH0pOwp9CmZ1bmN0aW9uIGxvYWRJUCgpewogIHhocigiL2FwaS9pcCIsbnVsbCxmdW5jdGlvbihyKXt2YXIgaXA9ciYmci5pcD9yLmlwOiJ1bmtub3duIjtzdCgic0lQIixpcCk7c3QoInNpcEJveCIsaXApO30pOwp9CnNldEludGVydmFsKGZ1bmN0aW9uKCl7CiAgaWYoIVNULm5leHRBdClyZXR1cm47CiAgdmFyIGQ9TWF0aC5tYXgoMCxNYXRoLnJvdW5kKChTVC5uZXh0QXQtRGF0ZS5ub3coKSkvMTAwMCkpOwogIHZhciBtPU1hdGguZmxvb3IoZC82MCkscz1kJTYwO3N0KCJzY2QiLGQ+MD8obSsibSAiK3MrInMiKToiU2Nhbm5pbmcuLi4iKTsKICBnZSgic0ZpbCIpLnN0eWxlLndpZHRoPU1hdGgubWF4KDAsMTAwLWQvU1Quc3MqMTAwKSsiJSI7Cn0sMTAwMCk7CmZ1bmN0aW9uIHBvbGwoKXt4aHIoIi9hcGkvc3RhdHVzIixudWxsLGZ1bmN0aW9uKHMpe2lmKHMpcmVuZGVyKHMpO30pO30KZnVuY3Rpb24gdHJ5QXV0b0Nvbm5lY3QoKXsKICAvLyBJZiBjb25uZWN0ZWQgY2FyZCB2aXNpYmxlIGFuZCBrZXlzIHNhdmVkLCB0cnkgY29ubmVjdGluZyBhdXRvbWF0aWNhbGx5CiAgaWYoZ2UoImNvbm5lY3RDYXJkIikuc3R5bGUuZGlzcGxheSE9PSJub25lIil7CiAgICBsb2FkU2F2ZWRLZXlzKCk7CiAgICB2YXIgaz1nZSgiY0tleSIpLnZhbHVlLnRyaW0oKSxzPWdlKCJjU2VjIikudmFsdWUudHJpbSgpOwogICAgaWYoayYmcyl7CiAgICAgIGdlKCJjTXNnIikudGV4dENvbnRlbnQ9IkF1dG8tY29ubmVjdGluZy4uLiI7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtkb0Nvbm5lY3QoKTt9LDUwMCk7CiAgICB9CiAgfQp9CmZ1bmN0aW9uIGNsZWFyU3RhbGUoKXsKICBpZighY29uZmlybSgiUmVtb3ZlIGFsbCBzdGFsZS9naG9zdCB0cmFkZXMgZnJvbSBoaXN0b3J5PyIpKSByZXR1cm47CiAgeGhyKCIvYXBpL2NsZWFyX3N0YWxlIix7fSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuc3VjY2Vzcyl7CiAgICAgIGFsZXJ0KCJSZW1vdmVkICIrci5yZW1vdmVkKyIgc3RhbGUgdHJhZGVzIik7CiAgICAgIHBvbGwoKTsKICAgIH0KICB9KTsKfQoKdmFyIF9sb3RzPTEsX2RhaWx5PTEwLF9tb2RlPSJub3JtYWwiOwpmdW5jdGlvbiBzZXRNb2RlKG0pewogIF9tb2RlPW07CiAgWyJzYWZlIiwibm9ybWFsIiwicHJvIl0uZm9yRWFjaChmdW5jdGlvbih4KXsKICAgIHZhciBlbD1nZSgibW9kZS0iK3gpOwogICAgaWYoIWVsKXJldHVybjsKICAgIHZhciBhY3RpdmU9eD09PW07CiAgICBpZih4PT09InNhZmUiKXtlbC5zdHlsZS5vcGFjaXR5PWFjdGl2ZT8iMSI6IjAuNCI7fQogICAgZWxzZSBpZih4PT09Im5vcm1hbCIpe2VsLnN0eWxlLm9wYWNpdHk9YWN0aXZlPyIxIjoiMC40Ijt9CiAgICBlbHNle2VsLnN0eWxlLm9wYWNpdHk9YWN0aXZlPyIxIjoiMC40Ijt9CiAgICBlbC5zdHlsZS50cmFuc2Zvcm09YWN0aXZlPyJzY2FsZSgxLjA1KSI6InNjYWxlKDEpIjsKICB9KTsKICB2YXIgbm90ZT1nZSgibW9kZU5vdGUiKTsKICBpZihub3RlKXsKICAgIGlmKG09PT0ic2FmZSIpIG5vdGUudGV4dENvbnRlbnQ9IkNvbnNlcnZhdGl2ZSDigJQgYmVzdCBmb3IgbGVhcm5pbmcuIFNtYWxsIGdhaW5zLCBzbWFsbCBsb3NzZXMuIjsKICAgIGVsc2UgaWYobT09PSJub3JtYWwiKSBub3RlLnRleHRDb250ZW50PSJCYWxhbmNlZCDigJQgcmVjb21tZW5kZWQuIEdvb2Qgcmlzay9yZXdhcmQgcmF0aW8uIjsKICAgIGVsc2Ugbm90ZS50ZXh0Q29udGVudD0i4pqg77iPIFBSTyByZXF1aXJlcyAkNTAwKyBiYWxhbmNlLiBIaWdoZXIgcmlzaywgaGlnaGVyIHJld2FyZC4iOwogICAgbm90ZS5zdHlsZS5jb2xvcj1tPT09InBybyI/InZhcigtLXkpIjoidmFyKC0tdDMpIjsKICB9Cn0KZnVuY3Rpb24gYWRqTG90cyhkKXsKICBfbG90cz1NYXRoLm1heCgxLE1hdGgubWluKDEwMCxfbG90cytkKSk7CiAgZ2UoImxvdHNWYWwiKS50ZXh0Q29udGVudD1fbG90czsKICB2YXIgZWw9Z2UoImxvdEJ0Y1ZhbCIpOwogIGlmKGVsKSBlbC50ZXh0Q29udGVudD1fbG90cysiIGxvdHMgPSAiKyhfbG90cyowLjAwMSkudG9GaXhlZCgzKSsiIEJUQyI7Cn0KZnVuY3Rpb24gYWRqRGFpbHkoZCl7X2RhaWx5PU1hdGgubWF4KDEsTWF0aC5taW4oNTAsX2RhaWx5K2QpKTtnZSgiZGFpbHlWYWwiKS50ZXh0Q29udGVudD1fZGFpbHk7fQpmdW5jdGlvbiBzYXZlVXNlclNldHRpbmdzKCl7CiAgeGhyKCIvYXBpL3VzZXIvc2V0dGluZ3MiLHtsb3Rfc2l6ZTpfbG90cyxtYXhfZGFpbHk6X2RhaWx5LG1vZGU6X21vZGV9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXsKICAgICAgZ2UoInNldE1zZyIpLnRleHRDb250ZW50PSJTYXZlZCEgTW9kZTogIityLm1vZGUudG9VcHBlckNhc2UoKTsKICAgICAgZ2UoInNldE1zZyIpLnN0eWxlLmNvbG9yPSJ2YXIoLS1nKSI7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnZSgic2V0TXNnIikudGV4dENvbnRlbnQ9IiI7fSwzMDAwKTsKICAgIH0gZWxzZSBpZihyJiZyLm1lc3NhZ2UpewogICAgICBnZSgic2V0TXNnIikudGV4dENvbnRlbnQ9ci5tZXNzYWdlOwogICAgICBnZSgic2V0TXNnIikuc3R5bGUuY29sb3I9InZhcigtLXIpIjsKICAgIH0KICB9KTsKfQovLyBPbiBsb2FkOiBjaGVjayBpZiBhbHJlYWR5IGxvZ2dlZCBpbgp4aHIoIi9hdXRoL21lIixudWxsLGZ1bmN0aW9uKHIpewogIGlmKHImJnIubG9nZ2VkX2luKXtTVC5pc0FkbWluPXIuaXNfYWRtaW47c3QoInVCYWRnZSIsci51c2VybmFtZSk7c2hvd0FwcCgpO2xvYWRJUCgpO3BvbGwoKTt9CiAgZWxzZXtzaG93QXV0aCgpO3NldFRpbWVvdXQoZnVuY3Rpb24oKXtsb2FkU2F2ZWRLZXlzKCk7fSwxMDApO30KfSk7CnNldEludGVydmFsKGZ1bmN0aW9uKCl7aWYoZ2UoImFwcCIpLnN0eWxlLmRpc3BsYXkhPT0ibm9uZSIpcG9sbCgpO30sNDAwMCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4=").decode("utf-8")

@app.route("/")
@app.route("/login")
def index(): return Response(_DASH, mimetype="text/html")

if __name__ == "__main__":
    if "--setup" in sys.argv:
        code,_=um.gen_invite(); print(f"Invite: {code}"); sys.exit()
    port=int(os.getenv("PORT",5000))
    # use_reloader=False prevents double-process on startup
    app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False,threaded=True)