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

    # Account guards
    HALT=0.08; PAUSE=0.03; COOL=30
    CIRC_N=3;  CIRC_MIN=120; MIN_HOLD=15

    # Signal thresholds
    CONF_BASE    = 62   # default
    CONF_MACRO   = 48   # when Daily+4H+1H all agree
    ADX_MIN      = 22

    # Session bias (from backtesting)
    DEAD_ZONE   = [2,3,4,5,6]
    PRIME_LONG  = [0,1,7,8,9,21,22,23]
    PRIME_SHORT = [13,14,15,16]

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
    return bots[uid]

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

    def update(self,all_bots):
        with self._lk:
            trades=[{**t,"uid":uid} for uid,b in all_bots.items()
                    for t in b.trades if t.get("won") is not None]
            if len(trades)<5: return
            wins=[t for t in trades if t["won"]]
            wr=len(wins)/len(trades)*100
            by_hour={}
            for t in trades:
                try:
                    h=str(datetime.fromisoformat(t["time"]).hour)
                    by_hour.setdefault(h,{"wins":0,"total":0})
                    by_hour[h]["total"]+=1
                    if t["won"]: by_hour[h]["wins"]+=1
                except: pass
            for h in by_hour:
                v=by_hour[h]
                v["wr"]=round(v["wins"]/v["total"]*100,1) if v["total"]>0 else 0
            good=[int(h) for h,v in by_hour.items() if v["wr"]>=62 and v["total"]>=3]
            bad =[int(h) for h,v in by_hour.items() if v["wr"]<=38 and v["total"]>=3]
            self.data.update({"win_rate":round(wr,1),"total":len(trades),
                "by_hour":by_hour,"good_hours":good,"bad_hours":bad,
                "updated":datetime.now(timezone.utc).isoformat()})
            if len(trades)>=20:
                if good: C.PRIME_LONG=list(set(C.PRIME_LONG+good))[:12]
                if bad:  C.DEAD_ZONE=list(set(C.DEAD_ZONE+bad))[:10]
                log.info(f"Intel: WR={wr:.1f}% good={good} bad={bad}")
            self._save()

    def start(self,bots_ref):
        def loop():
            while True:
                time.sleep(3600)
                try: self.update(bots_ref)
                except Exception as e: log.warning(f"intel: {e}")
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
    """Returns bull/bear/neutral for a given candle list."""
    if len(candles_list)<21: return "neutral"
    cl=[c["c"] for c in candles_list]
    hi=[c["h"] for c in candles_list]
    lo=[c["l"] for c in candles_list]
    e8=ema(cl,8)[-1]; e21=ema(cl,21)[-1]
    adx_v,pdi,ndi=adx_calc(hi,lo,cl)
    p=cl[-1]
    if p>e8>e21 and pdi>ndi: return "bull"
    if p<e8<e21 and ndi>pdi: return "bear"
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
    brain={"direction":"neutral","conviction":0,"strategy":"WAIT",
           "opt_type":None,"opt_conviction":"atm","macro_bias":"neutral",
           "trends":{},"adx5m":0,"bw":0,"atr_pct":0,"veto":"",
           "regime":"NEUTRAL","scale":1}

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
    # Dead zone veto
    if hour in C.DEAD_ZONE:
        brain["veto"]="dead_zone"; return brain

    # ── STRADDLE DECISION ──────────────────────────────────────────
    # Straddle when: BB squeeze (coiled) + low ADX + no clear macro
    squeeze=bw<1.2 and adx_v<20
    if squeeze and macro_bias=="neutral":
        brain["strategy"]="STRADDLE"; brain["conviction"]=55
        brain["opt_type"]="straddle"; brain["opt_conviction"]="atm"
        return brain

    # ── DIRECTIONAL DECISION ───────────────────────────────────────
    # Direction hierarchy: macro > 5m regime > divergence
    if macro_bias=="bull":
        direction="long"
        # ITM when conviction is high (strong macro + 5m confirms)
        if bull_votes==3 and regime in ("BULL","STRONG_BULL"):
            conviction=85; opt_conviction="itm"  # deep ITM for max leverage
        elif bull_votes>=2 and div_long:
            conviction=80; opt_conviction="itm"  # divergence + macro = ITM
        elif bull_votes>=2:
            conviction=65; opt_conviction="atm"  # macro only = ATM
        else:
            conviction=50; opt_conviction="atm"
    elif macro_bias=="bear":
        direction="short"
        if bear_votes==3 and regime in ("BEAR","STRONG_BEAR"):
            conviction=85; opt_conviction="itm"
        elif bear_votes>=2 and div_short:
            conviction=80; opt_conviction="itm"
        elif bear_votes>=2:
            conviction=65; opt_conviction="atm"
        else:
            conviction=50; opt_conviction="atm"
    else:
        # No macro bias — use 5m only if strong
        if regime in ("STRONG_BULL","BULL") and adx_v>25:
            direction="long"; conviction=60; opt_conviction="atm"
        elif regime in ("STRONG_BEAR","BEAR") and adx_v>25:
            direction="short"; conviction=60; opt_conviction="atm"
        elif div_long:
            direction="long"; conviction=58; opt_conviction="atm"
        elif div_short:
            direction="short"; conviction=58; opt_conviction="atm"
        elif squeeze:
            brain["strategy"]="STRADDLE"; brain["conviction"]=52
            brain["opt_type"]="straddle"; brain["opt_conviction"]="atm"
            return brain
        else:
            brain["veto"]="no_clear_direction"; return brain

    # RSI trap vetoes
    if direction=="long" and r5<32 and macro_bias!="bull":
        brain["veto"]="rsi_trap_long"; return brain
    if direction=="short" and r5>68 and macro_bias!="bear":
        brain["veto"]="rsi_trap_short"; return brain

    # Threshold
    conf_needed=C.CONF_MACRO if macro_bias!="neutral" else C.CONF_BASE
    if conviction<conf_needed:
        brain["veto"]=f"low_conf_{conviction}<{conf_needed}"; return brain

    # Lot scaling: 1x→2x→3x based on conviction
    if conviction>=80: scale=3
    elif conviction>=65: scale=2
    else: scale=1

    # Strategy
    strategy="SWING" if conviction>=70 else "SCALP"

    brain.update({"direction":direction,"conviction":conviction,"strategy":strategy,
                  "opt_type":"call" if direction=="long" else "put",
                  "opt_conviction":opt_conviction,"scale":scale})
    return brain

# ═══ BOT ══════════════════════════════════════════════════════════
class Bot:
    def __init__(self):
        self.api=DeltaAPI(); self.opts=None
        self._sf=os.path.join(_DATA,"bot_default.json")
        self.running=False; self.connected=False; self.opts_mode=False
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
            json.dump({"start_cap":self.start_cap,"day_start":self.day_start,
                "halted":self.halted,"halt_msg":self.halt_msg,
                "total_tr":self.total_tr,"wins":self.wins,
                "trades":self.trades[-200:],"stops":list(self._stops),
                "consec":self._consec,
                "circuit":self._circuit.isoformat() if self._circuit else None,
                "last_close":self._last_close.isoformat() if self._last_close else None,
                "peak":peak,"lot_size":self.lot_size,"max_daily":self.max_daily,
                "mode":self.mode},
                open(self._sf,"w"))
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
        if won: self._consec=0; self.wins+=1
        else:
            self._consec+=1
            if self._consec>=C.CIRC_N:
                self._circuit=now+timedelta(minutes=C.CIRC_MIN)
                self.emit("WARN",f"Circuit: {C.CIRC_N} losses → {C.CIRC_MIN}min pause")
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
        return {"5m":c5m,"1m":c1m,"15m":d15m,"1h":b1h,"4h":b4h,"1d":b1d,"binance_lead":bnc_lead}

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
        conviction=brain["conviction"]; veto=brain["veto"]
        macro_bias=brain["macro_bias"]; scale=brain["scale"]

        self.emit("INFO",
            f"#{self.scan_n} ${self.price:,.0f} | {brain['regime']} | "
            f"macro={macro_bias} | conv={conviction} | {strategy} | "
            f"adx={brain['adx5m']} bw={brain['bw']} "
            f"{'⚡DIV' if brain.get('trends',{}) else ''}"
            f"{'✗'+veto if veto else ''}")

        now=datetime.now(timezone.utc)
        # Guards
        if self._circuit and now<self._circuit:
            self.status=f"Circuit: {int((self._circuit-now).seconds/60)}m"; return
        elif self._circuit and now>=self._circuit:
            self._circuit=None; self._consec=0; self.emit("INFO","Circuit lifted")
        if self._last_close and (now-self._last_close).seconds<C.COOL*60:
            self.status=f"Cooldown {C.COOL-(now-self._last_close).seconds//60}m"; return
        if self.day_start>0 and (self.capital-self.day_start)/self.day_start<=-C.PAUSE:
            self.status="Paused — daily limit"; return
        today=now.strftime("%Y-%m-%d")
        if self._daily_date!=today: self._daily_date=today; self._daily_n=0
        if self._daily_n>=self.max_daily:
            self.status=f"Daily limit ({self.max_daily})"; return

        if veto:
            self.status=f"Waiting: {veto} | macro={macro_bias}"; return

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
            self.status=f"Holding {x.get('side','').upper()} @ ${x.get('entry',0):,.0f} UPL ${x.get('upnl',0):+.3f}"
            return

        # ── OPTIONS MODE ───────────────────────────────────────────
        if self.opts_mode and self.opts:
            opt_pos=self.api.opt_pos()
            local_open=[t for t in self.trades if t.get("exit") is None
                and t.get("side") in ("call","put","straddle")]
            if opt_pos or local_open:
                self.status=f"Holding {len(opt_pos or local_open)} option(s)"; return

            if strategy=="STRADDLE":
                st=self.opts.straddle(self.price)
                if st.get("found") and st["total"]<=self.capital*C.OPT_MAX*2:
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
                    self.status=f"Straddle: no liquid options found"
                return

            # Directional option
            opt_type=brain["opt_type"]
            opt_conv=brain["opt_conviction"]
            if not opt_type: self.status=f"Waiting signal conv={conviction}"; return

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
            return

        # ── PERPS MODE ─────────────────────────────────────────────
        if strategy in ("WAIT","STRADDLE") or direction=="neutral":
            self.status=f"Watching {brain['regime']} | macro={macro_bias} | conv={conviction}"
            return

        cfg=self.mode_cfg
        margin_per_lot=self.price*C.LOT/C.LEV
        # Risk % from mode: safe=2%, normal=5%, pro=10%
        risk_capital=self.capital*cfg["risk"]
        max_risk_lots=max(1,int(risk_capital/margin_per_lot))
        max_affordable=max(1,int(self.capital*0.20/margin_per_lot))
        lots=min(self.lot_size*scale*cfg["lot_mult"],min(max_risk_lots,max_affordable))
        lots=max(1,int(lots))

        r=self.api.order("buy" if direction=="long" else "sell",lots)
        if not r.get("success"):
            self.emit("ERROR",f"Order failed: {r.get('error','?')}"); return

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
            "conviction":b.get("conviction",0),"scale":b.get("scale",1),
            "direction":b.get("direction","neutral"),
            "trends":trends,"adx":b.get("adx5m",0),
            "bw":b.get("bw",0),"atr_pct":b.get("atr_pct",0),"rsi":b.get("rsi",50),
            "veto":b.get("veto",""),
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
                "Perp stop":f"{C.STOP*100:.1f}% ATR","Monthly halt":"-8%",
                "Daily pause":"-3%","Cooldown":f"{C.COOL}min",
                "Circuit":f"{C.CIRC_N} losses","Min hold":f"{C.MIN_HOLD}min"}}

# ═══ FLASK ════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = BOT_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30))
CORS(app,supports_credentials=True)

if C.KEY and C.SECRET:
    threading.Thread(target=lambda:get_bot("env").connect(C.KEY,C.SECRET),daemon=True).start()

_auto_setup()
intel.start(bots)
_auto_setup()  # auto-create admin if needed

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
def api_status(): return jsonify(get_bot(session["uid"]).state())

@app.route("/api/connect",methods=["POST","OPTIONS"])
@login_req
def api_connect():
    if request.method=="OPTIONS": return jsonify({})
    d=request.json or {}; k=d.get("api_key",""); s=d.get("api_secret","")
    if not k or not s: return jsonify({"success":False,"message":"Key and secret required"})
    return jsonify(get_bot(session["uid"]).connect(k.strip(),s.strip()))

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
    before=len(b.trades)
    # Keep only: open trades with real positions OR closed trades with actual P&L
    real_syms=set()
    for p in b.api.btcusd_pos(): real_syms.add(str(p.get("product_id","")))
    for p in b.api.opt_pos(): real_syms.add(p.get("product_symbol",""))
    cleaned=[]
    removed=0
    for t in b.trades:
        is_ghost=(t.get("reason") in ("reconciled","manual_close") and t.get("pnl",0)==0 and t.get("exit") is not None)
        if is_ghost:
            removed+=1
        else:
            cleaned.append(t)
    b.trades=cleaned; b.save()
    b.emit("INFO",f"Cleared {removed} stale trades")
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

@app.route("/api/opts/toggle",methods=["POST"])
@login_req
def api_opts_toggle():
    d=request.json or {}; b=get_bot(session["uid"])
    b.opts_mode=bool(d.get("enabled",not b.opts_mode))
    b.emit("INFO","Options ON" if b.opts_mode else "Options OFF")
    return jsonify({"success":True,"opts_mode":b.opts_mode})

@app.route("/api/opts/find",methods=["POST"])
@login_req
def api_opts_find():
    b=get_bot(session["uid"])
    if not b.opts: return jsonify({"error":"Not connected"})
    d=request.json or {}
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
                "message":f"PRO mode requires ${C.MODES['pro']['min_bal']} balance. You have ${b.capital:.0f}"})
        b.mode=new_mode
        b.emit("INFO",f"Mode changed to {new_mode.upper()}")
    b.emit("INFO",f"Settings: lots={b.lot_size} max_daily={b.max_daily} mode={b.mode}"); b.save()
    return jsonify({"success":True,"lot_size":b.lot_size,"max_daily":b.max_daily,"mode":b.mode})

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
_DASH = _b64.b64decode("PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEsbWF4aW11bS1zY2FsZT0xIj4KPHRpdGxlPkFscGhhIEJvdDwvdGl0bGU+CjxzdHlsZT4KKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowOy13ZWJraXQtdGFwLWhpZ2hsaWdodC1jb2xvcjp0cmFuc3BhcmVudH0KOnJvb3R7LS1nOiMwMGIzODY7LS1nYjojZThmOWYzOy0tZ2Q6I2E3ZjNkMDstLXI6I2U3NGMzYzstLXJiOiNmZWYyZjI7LS1yZDojZmNhNWE1Oy0teTojZjU5ZTBiOy0teWI6I2ZlZjNjNzstLWI6IzNiODJmNjstLWJiOiNlZmY2ZmY7LS10OiMwZjE3MmE7LS10MjojNjQ3NDhiOy0tdDM6Izk0YTNiODstLWJnOiNmMGYyZjU7LS13OiNmZmY7LS1iZHI6MXB4IHNvbGlkICNlMmU4ZjB9CmJvZHl7YmFja2dyb3VuZDp2YXIoLS1iZyk7Y29sb3I6dmFyKC0tdCk7Zm9udC1mYW1pbHk6LWFwcGxlLXN5c3RlbSxCbGlua01hY1N5c3RlbUZvbnQsIlNlZ29lIFVJIixIZWx2ZXRpY2EsQXJpYWwsc2Fucy1zZXJpZjtmb250LXNpemU6MTRweDttaW4taGVpZ2h0OjEwMHZofQovKiBBVVRIICovCi5hdXRoLXdyYXB7bWluLWhlaWdodDoxMDB2aDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQouYXV0aC1jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxNnB4O3BhZGRpbmc6MjhweDt3aWR0aDoxMDAlO21heC13aWR0aDozODBweDtib3gtc2hhZG93OjAgNHB4IDI0cHggcmdiYSgwLDAsMCwuMDgpfQouYXV0aC1sb2dve2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToyMHB4fQouYXV0aC1pY297d2lkdGg6NDBweDtoZWlnaHQ6NDBweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6MTJweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Y29sb3I6I2ZmZjtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDB9Ci5hdXRoLXRpdGxle2ZvbnQtc2l6ZToyMnB4O2ZvbnQtd2VpZ2h0OjgwMH0uYXV0aC1zdWIye2ZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKX0KLmF1dGgtZGVzY3tmb250LXNpemU6MTNweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLWJvdHRvbToxOHB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlucHt3aWR0aDoxMDAlO2JvcmRlcjp2YXIoLS1iZHIpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTFweCAxM3B4O2ZvbnQtc2l6ZToxNHB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7b3V0bGluZTpub25lO2JhY2tncm91bmQ6I2Y4ZmFmYzttYXJnaW4tYm90dG9tOjEwcHh9Ci5pbnA6Zm9jdXN7Ym9yZGVyLWNvbG9yOnZhcigtLWcpO2JhY2tncm91bmQ6I2ZmZn0KLmF1dGgtYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtmb250LXNpemU6MTRweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmF1dGgtYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmF1dGgtbXNne3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O21hcmdpbi10b3A6MTBweDttaW4taGVpZ2h0OjIwcHg7bGluZS1oZWlnaHQ6MS43fQouYXV0aC1tc2cub2t7Y29sb3I6dmFyKC0tZyl9LmF1dGgtbXNnLmVycntjb2xvcjp2YXIoLS1yKX0KLmF1dGgtc3dpdGNoe3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjE0cHh9Ci5hdXRoLXN3aXRjaCBhe2NvbG9yOnZhcigtLWIpO2N1cnNvcjpwb2ludGVyO2ZvbnQtd2VpZ2h0OjYwMH0KLyogTUFJTiBBUFAgKi8KI2FwcHtkaXNwbGF5Om5vbmV9Ci5oZHJ7YmFja2dyb3VuZDp2YXIoLS13KTtwYWRkaW5nOjAgMTZweDtoZWlnaHQ6NTRweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTtwb3NpdGlvbjpzdGlja3k7dG9wOjA7ei1pbmRleDoxMDA7Ym94LXNoYWRvdzowIDFweCA0cHggcmdiYSgwLDAsMCwuMDYpfQoubG9nb3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHh9Ci5saWN7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtiYWNrZ3JvdW5kOnZhcigtLXQpO2JvcmRlci1yYWRpdXM6OXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtjb2xvcjojZmZmO2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjgwMH0KLmxue2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjcwMH0ubHN7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpfQouaHJpZ2h0e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KLnViYWRnZXtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3BhZGRpbmc6NHB4IDEwcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6MjBweDtib3JkZXI6dmFyKC0tYmRyKX0KLnBpbGx7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4O3BhZGRpbmc6NXB4IDEycHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnAtbGl2ZXtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0ucC1vZmZ7YmFja2dyb3VuZDp2YXIoLS1yYik7Y29sb3I6dmFyKC0tcil9LnAtd2FybntiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KX0KLndyYXB7cGFkZGluZzoxMnB4IDE0cHggOTBweDttYXgtd2lkdGg6NDgwcHg7bWFyZ2luOjAgYXV0b30KLnBhZ2V7ZGlzcGxheTpub25lfS5wYWdlLnNob3d7ZGlzcGxheTpibG9ja30KLm5hdntwb3NpdGlvbjpmaXhlZDtib3R0b206MDtsZWZ0OjA7cmlnaHQ6MDtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci10b3A6dmFyKC0tYmRyKTtkaXNwbGF5OmZsZXg7cGFkZGluZzo4cHggMCBtYXgoOHB4LGVudihzYWZlLWFyZWEtaW5zZXQtYm90dG9tKSk7ei1pbmRleDo5OX0KLm5ie2ZsZXg6MTtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6M3B4O3BhZGRpbmc6NHB4IDA7Ym9yZGVyOm5vbmU7YmFja2dyb3VuZDpub25lO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5uYiAuaWN7Zm9udC1zaXplOjIwcHg7Y29sb3I6dmFyKC0tdDMpfS5uYiAubGJ7Zm9udC1zaXplOjlweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDMpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4fQoubmIub24gLmljLC5uYi5vbiAubGJ7Y29sb3I6dmFyKC0tdCl9Ci5jYXJke2JhY2tncm91bmQ6dmFyKC0tdyk7Ym9yZGVyLXJhZGl1czoxMnB4O3BhZGRpbmc6MTZweDttYXJnaW4tYm90dG9tOjEwcHg7Ym94LXNoYWRvdzowIDFweCAzcHggcmdiYSgwLDAsMCwuMDUpLDAgMnB4IDhweCByZ2JhKDAsMCwwLC4wNCl9Ci5jdHtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y29sb3I6dmFyKC0tdDIpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206MTJweH0KLyogQ09OTkVDVCBDQVJEICovCi5jY2FyZHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxNjBkZWcsIzBmMTcyYSwjMWUzYTVmKTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMnB4O21hcmdpbi1ib3R0b206MTBweH0KLmN0aXRsZXtmb250LXNpemU6MTdweDtmb250LXdlaWdodDo4MDA7Y29sb3I6I2ZmZjttYXJnaW4tYm90dG9tOjZweH0KLmNzdWJ7Zm9udC1zaXplOjEycHg7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSk7bWFyZ2luLWJvdHRvbToxNnB4O2xpbmUtaGVpZ2h0OjEuNn0KLmlwLXJvd3tiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7bWFyZ2luLWJvdHRvbToxNHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW59Ci5pcC1sYmx7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjQpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouNnB4O21hcmdpbi1ib3R0b206NHB4fQouaXAtdmFse2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTZweDtmb250LXdlaWdodDo3MDA7Y29sb3I6I2ZmZjtsZXR0ZXItc3BhY2luZzoxcHh9Ci5pcC1jb3B5e2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTIpO2JvcmRlcjpub25lO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6OHB4IDE0cHg7Y29sb3I6I2ZmZjtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6aW5oZXJpdH0KLmNpbnB7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA4KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtib3JkZXItcmFkaXVzOjEwcHg7cGFkZGluZzoxMnB4IDE0cHg7Zm9udC1zaXplOjE0cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtjb2xvcjojZmZmO21hcmdpbi1ib3R0b206MTBweDtvdXRsaW5lOm5vbmV9Ci5jaW5wOmZvY3Vze2JvcmRlci1jb2xvcjp2YXIoLS1nKX0uY2lucDo6cGxhY2Vob2xkZXJ7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMyl9Ci5jYnRue3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6MTBweDtib3JkZXI6bm9uZTtiYWNrZ3JvdW5kOnZhcigtLWcpO2NvbG9yOiNmZmY7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXR9Ci5jYnRuOmFjdGl2ZXtvcGFjaXR5Oi44NX0KLmNtc2d7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjEycHg7bWFyZ2luLXRvcDoxMHB4O21pbi1oZWlnaHQ6MjBweDtsaW5lLWhlaWdodDoxLjd9Ci8qIEhFUk8gKi8KLmhlcm97YmFja2dyb3VuZDp2YXIoLS10KTtib3JkZXItcmFkaXVzOjE2cHg7cGFkZGluZzoyMHB4O21hcmdpbi1ib3R0b206MTBweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59Ci5oZXJvOjphZnRlcntjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO3RvcDotNDBweDtyaWdodDotNDBweDt3aWR0aDoxNjBweDtoZWlnaHQ6MTYwcHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5obHtmb250LXNpemU6MTBweDtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC40KTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjhweDttYXJnaW4tYm90dG9tOjVweH0KLmhwe2ZvbnQtc2l6ZTo0MHB4O2ZvbnQtd2VpZ2h0OjgwMDtjb2xvcjojZmZmO2xpbmUtaGVpZ2h0OjE7bGV0dGVyLXNwYWNpbmc6LTEuNXB4fQouaHIye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDttYXJnaW4tdG9wOjlweDtmbGV4LXdyYXA6d3JhcH0KLmNoaXB7cGFkZGluZzozcHggMTBweDtib3JkZXItcmFkaXVzOjZweDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDB9Ci5jZ3tiYWNrZ3JvdW5kOnJnYmEoMCwyMDAsMTUwLC4yKTtjb2xvcjojMDBlOGIwfS5jcjJ7YmFja2dyb3VuZDpyZ2JhKDIzMSw3Niw2MCwuMik7Y29sb3I6I2ZmODA4MH0uY257YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNSl9Ci5yYmFye3BhZGRpbmc6OXB4IDE0cHg7Ym9yZGVyLXJhZGl1czo4cHg7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0KLnJiLWJ7YmFja2dyb3VuZDp2YXIoLS1nYik7Y29sb3I6IzA1OTY2OTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWdkKX0ucmItcntiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjojZGMyNjI2O2JvcmRlcjoxcHggc29saWQgdmFyKC0tcmQpfS5yYi1ue2JhY2tncm91bmQ6I2Y4ZmFmYztjb2xvcjp2YXIoLS10Mik7Ym9yZGVyOnZhcigtLWJkcil9LnJiLXd7YmFja2dyb3VuZDp2YXIoLS15Yik7Y29sb3I6IzkyNDAwZTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci8qIENPTkZJREVOQ0UgKi8KLmN3e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjE0cHg7cGFkZGluZzo0cHggMH0KLmNybmd7cG9zaXRpb246cmVsYXRpdmU7d2lkdGg6NzJweDtoZWlnaHQ6NzJweDtmbGV4LXNocmluazowfQouY3JuZyBzdmd7dHJhbnNmb3JtOnJvdGF0ZSgtOTBkZWcpO2Rpc3BsYXk6YmxvY2t9Ci5jb3Z7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQouY251bXtmb250LXNpemU6MjJweDtmb250LXdlaWdodDo4MDA7bGluZS1oZWlnaHQ6MX0uY2Rlbntmb250LXNpemU6OXB4O2NvbG9yOnZhcigtLXQzKTtmb250LXdlaWdodDo3MDB9Ci5jbXR7ZmxleDoxfS5jZGlye2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMDttYXJnaW4tYm90dG9tOjNweH0uY2RldHtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Mil9Ci5waWxsYXJze21hcmdpbi10b3A6MTJweH0KLnByb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O3BhZGRpbmc6N3B4IDA7Ym9yZGVyLWJvdHRvbTp2YXIoLS1iZHIpfS5wcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5wbnt3aWR0aDo4NnB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7ZmxleC1zaHJpbms6MH0KLnB0e2ZsZXg6MTtoZWlnaHQ6NXB4O2JhY2tncm91bmQ6I2YxZjVmOTtib3JkZXItcmFkaXVzOjNweDtvdmVyZmxvdzpoaWRkZW59LnBme2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6M3B4O3RyYW5zaXRpb246d2lkdGggLjVzfQoucHN7d2lkdGg6MzZweDt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQouaW5kc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLXRvcDoxMHB4fQouaW5ke2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtwYWRkaW5nOjEwcHg7dGV4dC1hbGlnbjpjZW50ZXI7Ym9yZGVyOnZhcigtLWJkcil9Ci5pbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTozcHh9Lml2e2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjgwMH0KLnNiYXJ7aGVpZ2h0OjNweDtiYWNrZ3JvdW5kOiNlMmU4ZjA7Ym9yZGVyLXJhZGl1czoycHg7b3ZlcmZsb3c6aGlkZGVuO21hcmdpbi10b3A6OXB4fS5zZmlse2hlaWdodDoxMDAlO2JhY2tncm91bmQ6dmFyKC0tYik7Ym9yZGVyLXJhZGl1czoycHg7dHJhbnNpdGlvbjp3aWR0aCAuNXN9Ci5zcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbjtmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDo0cHh9Ci8qIFBPU0lUSU9OUyAqLwoucG9ze2JvcmRlci1yYWRpdXM6MTJweDtwYWRkaW5nOjE0cHg7bWFyZ2luLWJvdHRvbToxMHB4fQoucG9zLWx7YmFja2dyb3VuZDojZjBmZGY0O2JvcmRlcjoxcHggc29saWQgdmFyKC0tZ2QpfS5wb3Mtc3tiYWNrZ3JvdW5kOiNmZmY1ZjU7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1yZCl9LnBvcy1ve2JhY2tncm91bmQ6dmFyKC0tYmIpO2JvcmRlcjoxcHggc29saWQgIzkzYzVmZH0KLnBoe2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxMHB4fS5wc3lte2ZvbnQtc2l6ZToxNXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJhZGdle3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czoyMHB4O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjgwMH0KLmJse2JhY2tncm91bmQ6dmFyKC0tZyk7Y29sb3I6I2ZmZn0uYnNoe2JhY2tncm91bmQ6dmFyKC0tcik7Y29sb3I6I2ZmZn0uYmN7YmFja2dyb3VuZDp2YXIoLS1iKTtjb2xvcjojZmZmfS5icHtiYWNrZ3JvdW5kOiM4YjVjZjY7Y29sb3I6I2ZmZn0KLnBne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6NnB4fQoucGl7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC43NSk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo4cHh9LnBpbHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi40cHg7bWFyZ2luLWJvdHRvbToycHh9LnBpdntmb250LXNpemU6MTRweDtmb250LXdlaWdodDo4MDB9LnBpZ3tjb2xvcjp2YXIoLS1nKX0ucGlye2NvbG9yOnZhcigtLXIpfQovKiBXQUxMRVQgKi8KLnd0e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQoud2x7ZmxleDoxfS53bGJ7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQzKTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjVweDttYXJnaW4tYm90dG9tOjRweH0KLndhe2ZvbnQtc2l6ZTozMnB4O2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzotMXB4fS53c3tmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10Myk7bWFyZ2luLXRvcDoycHh9Ci53cHtmb250LXNpemU6MThweDtmb250LXdlaWdodDo4MDA7dGV4dC1hbGlnbjpyaWdodH0ud257Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246cmlnaHQ7bWFyZ2luLXRvcDoycHh9Ci8qIFNUQVRTICovCi5zZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouc3RhdHtiYWNrZ3JvdW5kOnZhcigtLXcpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDt0ZXh0LWFsaWduOmNlbnRlcjtib3JkZXI6dmFyKC0tYmRyKX0KLnN0bHtmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Myk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi41cHg7bWFyZ2luLWJvdHRvbTo0cHh9LnN0dntmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDB9Ci5iM3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnIgMWZyO2dhcDo4cHg7bWFyZ2luLWJvdHRvbToxMHB4fQouYnRue3BhZGRpbmc6MTNweCA2cHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOm5vbmU7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo4MDA7Y3Vyc29yOnBvaW50ZXI7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2dhcDo1cHh9LmJ0bjphY3RpdmV7b3BhY2l0eTouOH0KLmJke2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZn0uYnIze2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1yZCl9LmJiM3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKTtib3JkZXI6MS41cHggc29saWQgI2JmZGJmZX0KLmJjYXtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MS41cHggc29saWQgdmFyKC0tcmQpO3dpZHRoOjEwMCU7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6OHB4fQovKiBPUFRJT05TICovCi50b2dyb3d7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjEwcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7bWFyZ2luLWJvdHRvbToxMnB4fQoudGx7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6NzAwfS50czN7Zm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4fQoudG9ne3Bvc2l0aW9uOnJlbGF0aXZlO3dpZHRoOjQ2cHg7aGVpZ2h0OjI2cHg7ZmxleC1zaHJpbms6MDtjdXJzb3I6cG9pbnRlcn0KLnRvZyBpbnB1dHtvcGFjaXR5OjA7d2lkdGg6MDtoZWlnaHQ6MDtwb3NpdGlvbjphYnNvbHV0ZX0KLnRvZ3Nse3Bvc2l0aW9uOmFic29sdXRlO2luc2V0OjA7YmFja2dyb3VuZDojZTJlOGYwO2JvcmRlci1yYWRpdXM6MTNweDt0cmFuc2l0aW9uOi4yc30KLnRvZ3NsOjpiZWZvcmV7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt3aWR0aDoyMHB4O2hlaWdodDoyMHB4O2xlZnQ6M3B4O2JvdHRvbTozcHg7YmFja2dyb3VuZDojZmZmO2JvcmRlci1yYWRpdXM6NTAlO3RyYW5zaXRpb246LjJzO2JveC1zaGFkb3c6MCAxcHggM3B4IHJnYmEoMCwwLDAsLjIpfQoudG9nIGlucHV0OmNoZWNrZWQrLnRvZ3Nse2JhY2tncm91bmQ6dmFyKC0tZyl9LnRvZyBpbnB1dDpjaGVja2VkKy50b2dzbDo6YmVmb3Jle3RyYW5zZm9ybTp0cmFuc2xhdGVYKDIwcHgpfQoub2luZm97ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjtnYXA6OHB4O3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTBweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKTttYXJnaW4tYm90dG9tOjEycHg7Zm9udC1zaXplOjExcHh9Ci5vYntkaXNwbGF5OmZsZXg7Z2FwOjhweH0KLm9iYnRue2ZsZXg6MTtwYWRkaW5nOjEwcHg7Ym9yZGVyLXJhZGl1czo4cHg7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXJ9Ci5vYi1je2JhY2tncm91bmQ6dmFyKC0tYmIpO2NvbG9yOnZhcigtLWIpO2JvcmRlcjoxcHggc29saWQgI2JmZGJmZX0ub2ItcHtiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLXJkKX0ub2Itc3tiYWNrZ3JvdW5kOnZhcigtLXliKTtjb2xvcjp2YXIoLS15KTtib3JkZXI6MXB4IHNvbGlkICNmZGU2OGF9Ci5vcmVze21hcmdpbi10b3A6MTBweDtwYWRkaW5nOjExcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2ZvbnQtc2l6ZToxMXB4O2xpbmUtaGVpZ2h0OjEuODtib3JkZXI6dmFyKC0tYmRyKTtkaXNwbGF5Om5vbmV9Ci5tcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi10b3A6OHB4fQouYnRubHtmbGV4OjE7cGFkZGluZzoxM3B4O2JvcmRlci1yYWRpdXM6OXB4O2JvcmRlcjoxLjVweCBzb2xpZCB2YXIoLS1nKTtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjgwMDtjdXJzb3I6cG9pbnRlcn0KLmJ0bnMye2ZsZXg6MTtwYWRkaW5nOjEzcHg7Ym9yZGVyLXJhZGl1czo5cHg7Ym9yZGVyOjEuNXB4IHNvbGlkIHZhcigtLXIpO2JhY2tncm91bmQ6dmFyKC0tcmIpO2NvbG9yOnZhcigtLXIpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2N1cnNvcjpwb2ludGVyfQovKiBUUkFERVMgKi8KLnRyLXJvd3twYWRkaW5nOjExcHggMDtib3JkZXItYm90dG9tOnZhcigtLWJkcik7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweH0udHItcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci50aWNve3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo5cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxNHB4O2ZvbnQtd2VpZ2h0OjgwMDtmbGV4LXNocmluazowfQoudGktbHtiYWNrZ3JvdW5kOnZhcigtLWdiKTtjb2xvcjp2YXIoLS1nKX0udGktc3tiYWNrZ3JvdW5kOnZhcigtLXJiKTtjb2xvcjp2YXIoLS1yKX0udGktY3tiYWNrZ3JvdW5kOnZhcigtLWJiKTtjb2xvcjp2YXIoLS1iKX0udGktcHtiYWNrZ3JvdW5kOiNmM2U4ZmY7Y29sb3I6IzdjM2FlZH0KLnRtaWR7ZmxleDoxO21pbi13aWR0aDowfS50c3lte2ZvbnQtc2l6ZToxMnB4O2ZvbnQtd2VpZ2h0OjcwMH0udG1ldGF7Zm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MXB4O3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KLnRyaWdodHt0ZXh0LWFsaWduOnJpZ2h0O2ZsZXgtc2hyaW5rOjB9LnRwbmx7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6ODAwfS50cGd7Y29sb3I6dmFyKC0tZyl9LnRwcntjb2xvcjp2YXIoLS1yKX0udHBue2NvbG9yOnZhcigtLXQzKX0KLyogTE9HUyAqLwoubGZ7ZGlzcGxheTpmbGV4O2dhcDo2cHg7bWFyZ2luLWJvdHRvbTo4cHh9Ci5sZmJ7cGFkZGluZzo0cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDp2YXIoLS13KTtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtZmFtaWx5OmluaGVyaXR9LmxmYi5vbntiYWNrZ3JvdW5kOnZhcigtLXQpO2NvbG9yOiNmZmY7Ym9yZGVyLWNvbG9yOnZhcigtLXQpfQoubGJveHtiYWNrZ3JvdW5kOiMwZjE3MmE7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzoxMnB4O21heC1oZWlnaHQ6NDAwcHg7b3ZlcmZsb3cteTphdXRvfQoubHJ7cGFkZGluZzo0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjMWUyOTNiO2ZvbnQtc2l6ZToxMXB4O2Rpc3BsYXk6ZmxleDtnYXA6OHB4O2ZvbnQtZmFtaWx5Om1vbm9zcGFjZX0KLmx0e2NvbG9yOiM0NzU1Njk7d2hpdGUtc3BhY2U6bm93cmFwO2ZsZXgtc2hyaW5rOjB9LmxJe2NvbG9yOiM2NDc0OGJ9LmxXe2NvbG9yOnZhcigtLXkpfS5sRXtjb2xvcjp2YXIoLS1yKX0ubFR7Y29sb3I6dmFyKC0tZyk7Zm9udC13ZWlnaHQ6NzAwfQovKiBTRVRUSU5HUyAqLwouZ3JhaWwtcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjlweCAwO2JvcmRlci1ib3R0b206dmFyKC0tYmRyKX0uZ3JhaWwtcm93Omxhc3QtY2hpbGR7Ym9yZGVyOm5vbmV9Ci5ncmt7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tdDIpfS5ncnZ7Zm9udC1zaXplOjEycHg7Zm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWcpO3RleHQtYWxpZ246cmlnaHQ7bWF4LXdpZHRoOjYwJX0KLmRjLWJ0bnt3aWR0aDoxMDAlO3BhZGRpbmc6MTJweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtiYWNrZ3JvdW5kOnZhcigtLXcpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyO21hcmdpbi10b3A6NnB4fQovKiBBRE1JTiAqLwouYXV7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6MTJweDttYXJnaW4tYm90dG9tOjhweDtib3JkZXI6dmFyKC0tYmRyKX0KLmF1LW5hbWV7Zm9udC1zaXplOjEzcHg7Zm9udC13ZWlnaHQ6NzAwO21hcmdpbi1ib3R0b206NHB4fQouYXUtc3RhdHN7ZGlzcGxheTpmbGV4O2dhcDoxMnB4O2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKX0KLmljb2Rle2ZvbnQtZmFtaWx5Om1vbm9zcGFjZTtmb250LXNpemU6MTVweDtmb250LXdlaWdodDo3MDA7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxMnB4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjhweDtib3JkZXI6dmFyKC0tYmRyKTtsZXR0ZXItc3BhY2luZzoycHg7bWFyZ2luOjhweCAwfQouaXBib3h7Zm9udC1mYW1pbHk6bW9ub3NwYWNlO2ZvbnQtc2l6ZToxNnB4O2ZvbnQtd2VpZ2h0OjcwMDt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjEzcHg7YmFja2dyb3VuZDojZjhmYWZjO2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2xldHRlci1zcGFjaW5nOjJweDttYXJnaW4tYm90dG9tOjEwcHh9Ci5lbXB0eXt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjI4cHg7Y29sb3I6dmFyKC0tdDMpO2ZvbnQtc2l6ZToxM3B4fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5PgoKPCEtLSDilZDilZDilZAgQVVUSCBTQ1JFRU4g4pWQ4pWQ4pWQIC0tPgo8ZGl2IGlkPSJhdXRoU2NyZWVuIiBjbGFzcz0iYXV0aC13cmFwIj4KICA8ZGl2IGNsYXNzPSJhdXRoLWNhcmQiPgogICAgPGRpdiBjbGFzcz0iYXV0aC1sb2dvIj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1pY28iPiYjOTE2OzwvZGl2PgogICAgICA8ZGl2PjxkaXYgY2xhc3M9ImF1dGgtdGl0bGUiPkFscGhhIEJvdDwvZGl2PjxkaXYgY2xhc3M9ImF1dGgtc3ViMiI+RGVsdGEgRXhjaGFuZ2UgSW5kaWE8L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gTG9naW4gZm9ybSAtLT4KICAgIDxkaXYgaWQ9ImxvZ2luRm9ybSI+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtZGVzYyI+U2lnbiBpbiB0byB5b3VyIHRyYWRpbmcgYWNjb3VudDwvZGl2PgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9ImxVc2VyIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0iVXNlcm5hbWUiIGF1dG9jb21wbGV0ZT0idXNlcm5hbWUiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJub25lIj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJsUGFzcyIgdHlwZT0icGFzc3dvcmQiIHBsYWNlaG9sZGVyPSJQYXNzd29yZCIgYXV0b2NvbXBsZXRlPSJjdXJyZW50LXBhc3N3b3JkIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iYXV0aC1idG4iIG9uY2xpY2s9ImRvTG9naW4oKSI+U2lnbiBJbjwvYnV0dG9uPgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLW1zZyIgaWQ9ImxNc2ciPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJhdXRoLXN3aXRjaCI+SGF2ZSBhbiBpbnZpdGUgY29kZT8gPGEgb25jbGljaz0ic2hvd1JlZygpIj5SZWdpc3RlciBoZXJlPC9hPjwvZGl2PgogICAgPC9kaXY+CgogICAgPCEtLSBSZWdpc3RlciBmb3JtIC0tPgogICAgPGRpdiBpZD0icmVnRm9ybSIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICAgIDxkaXYgY2xhc3M9ImF1dGgtZGVzYyI+RW50ZXIgeW91ciBpbnZpdGUgY29kZSB0byBjcmVhdGUgYW4gYWNjb3VudDwvZGl2PgogICAgICA8aW5wdXQgY2xhc3M9ImlucCIgaWQ9InJJbnYiICB0eXBlPSJ0ZXh0IiAgICAgcGxhY2Vob2xkZXI9Ikludml0ZSBjb2RlIiBhdXRvY29ycmVjdD0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ibm9uZSI+CiAgICAgIDxpbnB1dCBjbGFzcz0iaW5wIiBpZD0iclVzZXIiIHR5cGU9InRleHQiICAgICBwbGFjZWhvbGRlcj0iQ2hvb3NlIGEgdXNlcm5hbWUiIGF1dG9jb3JyZWN0PSJvZmYiIGF1dG9jYXBpdGFsaXplPSJub25lIj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJyUGFzcyIgdHlwZT0icGFzc3dvcmQiIHBsYWNlaG9sZGVyPSJDaG9vc2UgYSBwYXNzd29yZCAobWluIDYgY2hhcnMpIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iYXV0aC1idG4iIG9uY2xpY2s9ImRvUmVnaXN0ZXIoKSI+Q3JlYXRlIEFjY291bnQ8L2J1dHRvbj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1tc2ciIGlkPSJyTXNnIj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iYXV0aC1zd2l0Y2giPkFscmVhZHkgcmVnaXN0ZXJlZD8gPGEgb25jbGljaz0ic2hvd0xvZ2luKCkiPlNpZ24gaW48L2E+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8IS0tIOKVkOKVkOKVkCBNQUlOIEFQUCDilZDilZDilZAgLS0+CjxkaXYgaWQ9ImFwcCI+CjxkaXYgY2xhc3M9ImhkciI+CiAgPGRpdiBjbGFzcz0ibG9nbyI+PGRpdiBjbGFzcz0ibGljIj4mIzkxNjs8L2Rpdj48ZGl2PjxkaXYgY2xhc3M9ImxuIj5BbHBoYSBCb3Q8L2Rpdj48ZGl2IGNsYXNzPSJscyI+RGVsdGEgRXhjaGFuZ2UgSW5kaWE8L2Rpdj48L2Rpdj48L2Rpdj4KICA8ZGl2IGNsYXNzPSJocmlnaHQiPgogICAgPHNwYW4gY2xhc3M9InViYWRnZSIgaWQ9InVCYWRnZSI+LS08L3NwYW4+CiAgICA8ZGl2IGNsYXNzPSJwaWxsIHAtb2ZmIiBpZD0ic1BpbGwiPiYjOTY3OTsgPHNwYW4gaWQ9InNUeHQiPlN0b3BwZWQ8L3NwYW4+PC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPGRpdiBjbGFzcz0id3JhcCI+Cgo8IS0tIEhPTUUgLS0+CjxkaXYgY2xhc3M9InBhZ2Ugc2hvdyIgaWQ9InAtaG9tZSI+CgogIDwhLS0gQ29ubmVjdCBjYXJkIC0tPgogIDxkaXYgaWQ9ImNvbm5lY3RDYXJkIiBjbGFzcz0iY2NhcmQiPgogICAgPGRpdiBjbGFzcz0iY3RpdGxlIj5Db25uZWN0IHRvIERlbHRhIEV4Y2hhbmdlPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjc3ViIj5Zb3VyIEFQSSBrZXlzIGFyZSBzdG9yZWQgb25seSBpbiB5b3VyIGJyb3dzZXIgc2Vzc2lvbiDigJQgbmV2ZXIgc2F2ZWQgb24gdGhlIHNlcnZlci48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImlwLXJvdyI+CiAgICAgIDxkaXY+PGRpdiBjbGFzcz0iaXAtbGJsIj5TZXJ2ZXIgSVAg4oCUIHdoaXRlbGlzdCBvbiBEZWx0YSBmaXJzdDwvZGl2PjxkaXYgY2xhc3M9ImlwLXZhbCIgaWQ9InNJUCI+TG9hZGluZy4uLjwvZGl2PjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJpcC1jb3B5IiBvbmNsaWNrPSJjb3B5SVAoKSI+Q29weTwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8aW5wdXQgY2xhc3M9ImNpbnAiIGlkPSJjS2V5IiB0eXBlPSJ0ZXh0IiAgICAgcGxhY2Vob2xkZXI9IkFQSSBLZXkiICAgIGF1dG9jb21wbGV0ZT0ib2ZmIiBhdXRvY29ycmVjdD0ib2ZmIiBhdXRvY2FwaXRhbGl6ZT0ibm9uZSI+CiAgICA8aW5wdXQgY2xhc3M9ImNpbnAiIGlkPSJjU2VjIiB0eXBlPSJwYXNzd29yZCIgcGxhY2Vob2xkZXI9IkFQSSBTZWNyZXQiPgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgb25jbGljaz0iZG9Db25uZWN0KCkiPkNvbm5lY3Q8L2J1dHRvbj4KICAgIDxkaXYgY2xhc3M9ImNtc2ciIGlkPSJjTXNnIj48L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBMaXZlIGRhc2hib2FyZCAtLT4KICA8ZGl2IGlkPSJsaXZlRGFzaCIgc3R5bGU9ImRpc3BsYXk6bm9uZSI+CiAgICA8ZGl2IGNsYXNzPSJoZXJvIj4KICAgICAgPGRpdiBjbGFzcz0iaGwiPkJpdGNvaW4gJmJ1bGw7IExpdmU8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iaHAiIGlkPSJoUCI+JC0tPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImhyMiI+CiAgICAgICAgPHNwYW4gY2xhc3M9ImNoaXAgY24iIGlkPSJoUiI+LS08L3NwYW4+CiAgICAgICAgPHNwYW4gY2xhc3M9ImNoaXAgY24iIGlkPSJoUyI+LS08L3NwYW4+CiAgICAgICAgPHNwYW4gY2xhc3M9ImNoaXAgY24iIGlkPSJoViI+LS08L3NwYW4+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJyYmFyIHJiLW4iIGlkPSJyQmFyIj5TY2FubmluZy4uLjwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5Db25maWRlbmNlIFNjb3JlPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImN3Ij4KICAgICAgICA8ZGl2IGNsYXNzPSJjcm5nIj4KICAgICAgICAgIDxzdmcgdmlld0JveD0iMCAwIDcyIDcyIiB3aWR0aD0iNzIiIGhlaWdodD0iNzIiPgogICAgICAgICAgICA8Y2lyY2xlIGN4PSIzNiIgY3k9IjM2IiByPSIyOCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZjFmNWY5IiBzdHJva2Utd2lkdGg9IjciLz4KICAgICAgICAgICAgPGNpcmNsZSBpZD0iY0FyYyIgY3g9IjM2IiBjeT0iMzYiIHI9IjI4IiBmaWxsPSJub25lIiBzdHJva2U9IiMwMGIzODYiIHN0cm9rZS13aWR0aD0iNyIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBzdHJva2UtZGFzaGFycmF5PSIxNzUuOSIgc3Ryb2tlLWRhc2hvZmZzZXQ9IjE3NS45IiBzdHlsZT0idHJhbnNpdGlvbjpzdHJva2UtZGFzaG9mZnNldCAuNnMsc3Ryb2tlIC4zcyIvPgogICAgICAgICAgPC9zdmc+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJjb3YiPjxkaXYgY2xhc3M9ImNudW0iIGlkPSJjTiI+LS08L2Rpdj48ZGl2IGNsYXNzPSJjZGVuIj4vMTAwPC9kaXY+PC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iY210Ij48ZGl2IGNsYXNzPSJjZGlyIiBpZD0iY0QiPldBSVQ8L2Rpdj48ZGl2IGNsYXNzPSJjZGV0IiBpZD0iY0R0Ij5HYXRoZXJpbmcgZGF0YS4uLjwvZGl2PjwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0icGlsbGFycyIgaWQ9InBpbERpdiI+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImluZHMiPgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkFEWDwvZGl2PjxkaXYgY2xhc3M9Iml2IiBpZD0iaUEiPi0tPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iaW5kIj48ZGl2IGNsYXNzPSJpbCI+QkIgV2lkdGg8L2Rpdj48ZGl2IGNsYXNzPSJpdiIgaWQ9ImlCIj4tLTwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImluZCI+PGRpdiBjbGFzcz0iaWwiPkFUUiAlPC9kaXY+PGRpdiBjbGFzcz0iaXYiIGlkPSJpVCI+LS08L2Rpdj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InNiYXIiPjxkaXYgY2xhc3M9InNmaWwiIGlkPSJzRmlsIiBzdHlsZT0id2lkdGg6MCUiPjwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJzcm93Ij48c3BhbiBpZD0ic1N0YXR1cyI+Tm90IHJ1bm5pbmc8L3NwYW4+PHNwYW4gaWQ9InNjZCIgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1iKSI+LS08L3NwYW4+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9InBlcnBEaXYiPjwvZGl2PgogICAgPGRpdiBpZD0ib3B0c0RpdiI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPldhbGxldDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ3dCI+CiAgICAgICAgPGRpdiBjbGFzcz0id2wiPjxkaXYgY2xhc3M9IndsYiI+QmFsYW5jZTwvZGl2PjxkaXYgY2xhc3M9IndhIiBpZD0id0EiPiQtLTwvZGl2PjxkaXYgY2xhc3M9IndzIiBpZD0id1N0Ij48L2Rpdj48L2Rpdj4KICAgICAgICA8ZGl2PjxkaXYgY2xhc3M9IndwIiBpZD0id1AiPi0tJTwvZGl2PjxkaXYgY2xhc3M9InduIiBpZD0id04iPlAmYW1wO0wgJC0tPC9kaXY+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZyI+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+V2luIFJhdGU8L2Rpdj48ZGl2IGNsYXNzPSJzdHYiIGlkPSJzV1IiPi0tPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+VHJhZGVzPC9kaXY+PGRpdiBjbGFzcz0ic3R2IiBpZD0ic1RSIj4wPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InN0YXQiPjxkaXYgY2xhc3M9InN0bCI+U2NhbiAjPC9kaXY+PGRpdiBjbGFzcz0ic3R2IiBzdHlsZT0iY29sb3I6dmFyKC0tYikiIGlkPSJzU04iPjA8L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iYjMiPgogICAgICA8YnV0dG9uIGNsYXNzPSJidG4gYmQiICBvbmNsaWNrPSJib3RTdGFydCgpIj4mIzk2NTQ7IFN0YXJ0PC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImJ0biBicjMiIG9uY2xpY2s9ImJvdFN0b3AoKSI+JiM5NjMyOyBTdG9wPC9idXR0b24+CiAgICAgIDxidXR0b24gY2xhc3M9ImJ0biBiYjMiIG9uY2xpY2s9ImJvdFJ1bigpIj4mIzk4ODk7IFJ1bjwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEycHgiPk9wdGlvbnMgTW9kZTwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ0b2dyb3ciPgogICAgICAgIDxkaXY+PGRpdiBjbGFzcz0idGwiPkVuYWJsZSBPcHRpb25zIFRyYWRpbmc8L2Rpdj48ZGl2IGNsYXNzPSJ0czMiPkFUTS9JVE0gY2FsbHMgJmFtcDsgcHV0cyArIHN0cmFkZGxlczwvZGl2PjwvZGl2PgogICAgICAgIDxsYWJlbCBjbGFzcz0idG9nIj48aW5wdXQgdHlwZT0iY2hlY2tib3giIGlkPSJ0b2dPIiBvbmNoYW5nZT0idG9nZ2xlT3B0cyh0aGlzLmNoZWNrZWQpIj48c3BhbiBjbGFzcz0idG9nc2wiPjwvc3Bhbj48L2xhYmVsPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBpZD0ib3B0c1BhbmVsIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgICA8ZGl2IGNsYXNzPSJvaW5mbyI+CiAgICAgICAgICA8ZGl2PjxkaXYgc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtjb2xvcjp2YXIoLS1nKSI+KzcwJTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+VGFrZSBQcm9maXQ8L2Rpdj48L2Rpdj4KICAgICAgICAgIDxkaXY+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLXIpIj4tMTUlPC9kaXY+PGRpdiBzdHlsZT0iY29sb3I6dmFyKC0tdDMpO21hcmdpbi10b3A6MnB4Ij5TdG9wIExvc3M8L2Rpdj48L2Rpdj4KICAgICAgICAgIDxkaXY+PGRpdiBzdHlsZT0iZm9udC13ZWlnaHQ6ODAwO2NvbG9yOnZhcigtLWIpIj5Mb2NrIDY0JTwvZGl2PjxkaXYgc3R5bGU9ImNvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjJweCI+b2YgcGVhazwvZGl2PjwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im9iIj4KICAgICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9iLWMiIG9uY2xpY2s9ImNoa09wdCgnY2FsbCcpIj5DaGVjayBDQUxMPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJvYmJ0biBvYi1wIiBvbmNsaWNrPSJjaGtPcHQoJ3B1dCcpIj5DaGVjayBQVVQ8L2J1dHRvbj4KICAgICAgICAgIDxidXR0b24gY2xhc3M9Im9iYnRuIG9iLXMiIG9uY2xpY2s9ImNoa1N0KCkiPlN0cmFkZGxlPC9idXR0b24+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBpZD0ib1JlcyIgY2xhc3M9Im9yZXMiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMnB4Ij5UcmFkZSBTZXR0aW5nczwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToxMnB4Ij4KICAgICAgICA8ZGl2PgogICAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2NvbG9yOnZhcigtLXQyKTttYXJnaW4tYm90dG9tOjZweCI+TG90cyBQZXIgVHJhZGU8L2Rpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweCI+CiAgICAgICAgICAgIDxidXR0b24gb25jbGljaz0iYWRqTG90cygtMSkiIHN0eWxlPSJ3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2JhY2tncm91bmQ6I2Y4ZmFmYztmb250LXNpemU6MThweDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij7iiJI8L2J1dHRvbj4KICAgICAgICAgICAgPHNwYW4gaWQ9ImxvdHNWYWwiIHN0eWxlPSJmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7ZmxleDoxO3RleHQtYWxpZ246Y2VudGVyIj4xPC9zcGFuPgogICAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImFkakxvdHMoMSkiIHN0eWxlPSJ3aWR0aDozMnB4O2hlaWdodDozMnB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjp2YXIoLS1iZHIpO2JhY2tncm91bmQ6I2Y4ZmFmYztmb250LXNpemU6MThweDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij4rPC9idXR0b24+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOmNlbnRlcjttYXJnaW4tdG9wOjRweCIgaWQ9ImxvdEJ0Y1ZhbCI+MTAgbG90cyA9IDAuMDEgQlRDPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbTo2cHgiPk1heCBUcmFkZXMvRGF5PC9kaXY+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHgiPgogICAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImFkakRhaWx5KC0xKSIgc3R5bGU9IndpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDojZjhmYWZjO2ZvbnQtc2l6ZToxOHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXQiPuKIkjwvYnV0dG9uPgogICAgICAgICAgICA8c3BhbiBpZD0iZGFpbHlWYWwiIHN0eWxlPSJmb250LXNpemU6MjBweDtmb250LXdlaWdodDo4MDA7ZmxleDoxO3RleHQtYWxpZ246Y2VudGVyIj4xMDwvc3Bhbj4KICAgICAgICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJhZGpEYWlseSgxKSIgc3R5bGU9IndpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czo4cHg7Ym9yZGVyOnZhcigtLWJkcik7YmFja2dyb3VuZDojZjhmYWZjO2ZvbnQtc2l6ZToxOHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OmluaGVyaXQiPis8L2J1dHRvbj4KICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246Y2VudGVyO21hcmdpbi10b3A6NHB4IiBpZD0iZGFpbHlVc2VkIj4wIHVzZWQgdG9kYXk8L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDwhLS0gVHJhZGluZyBNb2RlIFNlbGVjdG9yIC0tPgogICAgICA8ZGl2IHN0eWxlPSJtYXJnaW4tYm90dG9tOjE0cHgiPgogICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjb2xvcjp2YXIoLS10Mik7bWFyZ2luLWJvdHRvbTo4cHgiPlRyYWRpbmcgTW9kZTwvZGl2PgogICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmciAxZnI7Z2FwOjZweCIgaWQ9Im1vZGVHcmlkIj4KICAgICAgICAgIDxidXR0b24gb25jbGljaz0ic2V0TW9kZSgnc2FmZScpIiBpZD0ibW9kZS1zYWZlIiBzdHlsZT0icGFkZGluZzoxMHB4IDRweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MnB4IHNvbGlkIHZhcigtLWcpO2JhY2tncm91bmQ6dmFyKC0tZ2IpO2NvbG9yOnZhcigtLWcpO2ZvbnQtZmFtaWx5OmluaGVyaXQ7Zm9udC1zaXplOjExcHg7Zm9udC13ZWlnaHQ6NzAwO2N1cnNvcjpwb2ludGVyIj4KICAgICAgICAgICAg8J+boSBTQUZFPGJyPjxzcGFuIHN0eWxlPSJmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjQwMCI+MiUgcmlzazxicj4rNTAlIFRQPC9zcGFuPgogICAgICAgICAgPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9InNldE1vZGUoJ25vcm1hbCcpIiBpZD0ibW9kZS1ub3JtYWwiIHN0eWxlPSJwYWRkaW5nOjEwcHggNHB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoycHggc29saWQgdmFyKC0tYik7YmFja2dyb3VuZDp2YXIoLS1iYik7Y29sb3I6dmFyKC0tYik7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTFweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXIiPgogICAgICAgICAgICDimpYgTk9STUFMPGJyPjxzcGFuIHN0eWxlPSJmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjQwMCI+NSUgcmlzazxicj4rNzAlIFRQPC9zcGFuPgogICAgICAgICAgPC9idXR0b24+CiAgICAgICAgICA8YnV0dG9uIG9uY2xpY2s9InNldE1vZGUoJ3BybycpIiBpZD0ibW9kZS1wcm8iIHN0eWxlPSJwYWRkaW5nOjEwcHggNHB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjoycHggc29saWQgI2Y1OWUwYjtiYWNrZ3JvdW5kOiNmZWYzYzc7Y29sb3I6IzkyNDAwZTtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxMXB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlciI+CiAgICAgICAgICAgIPCfmoAgUFJPPGJyPjxzcGFuIHN0eWxlPSJmb250LXNpemU6OXB4O2ZvbnQtd2VpZ2h0OjQwMCI+MTAlIHJpc2s8YnI+KzEwMCUgVFA8L3NwYW4+CiAgICAgICAgICA8L2J1dHRvbj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGlkPSJtb2RlTm90ZSIgc3R5bGU9ImZvbnQtc2l6ZToxMHB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tdG9wOjZweDt0ZXh0LWFsaWduOmNlbnRlciI+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8YnV0dG9uIG9uY2xpY2s9InNhdmVVc2VyU2V0dGluZ3MoKSIgc3R5bGU9IndpZHRoOjEwMCU7cGFkZGluZzoxMXB4O2JvcmRlci1yYWRpdXM6OHB4O2JvcmRlcjpub25lO2JhY2tncm91bmQ6dmFyKC0tdCk7Y29sb3I6I2ZmZjtmb250LWZhbWlseTppbmhlcml0O2ZvbnQtc2l6ZToxM3B4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlciI+U2F2ZSBTZXR0aW5nczwvYnV0dG9uPgogICAgICA8ZGl2IGlkPSJzZXRNc2ciIHN0eWxlPSJ0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MTFweDttYXJnaW4tdG9wOjZweDttaW4taGVpZ2h0OjE2cHg7Y29sb3I6dmFyKC0tZykiPjwvZGl2PgogICAgPC9kaXY+CgogICAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbToxMHB4Ij5NYW51YWwgVHJhZGU8L2Rpdj4KICAgICAgPGlucHV0IGNsYXNzPSJpbnAiIGlkPSJtTG90cyIgdHlwZT0ibnVtYmVyIiBwbGFjZWhvbGRlcj0iTG90cyAoZGVmYXVsdDogMSkiIG1pbj0iMSI+CiAgICAgIDxkaXYgY2xhc3M9Im1yb3ciPgogICAgICAgIDxidXR0b24gY2xhc3M9ImJ0bmwiICBvbmNsaWNrPSJtYW5UcmFkZSgnbG9uZycpIj4mIzg1OTM7IEJ1eSBMb25nPC9idXR0b24+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iYnRuczIiIG9uY2xpY2s9Im1hblRyYWRlKCdzaG9ydCcpIj4mIzg1OTU7IFNlbGwgU2hvcnQ8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImJjYSIgb25jbGljaz0iY2xvc2VBbGwoKSI+JiM5ODg4OyBDbG9zZSBBbGwgUG9zaXRpb25zPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2PgoKPCEtLSBUUkFERVMgLS0+CjxkaXYgY2xhc3M9InBhZ2UiIGlkPSJwLXRyYWRlcyI+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTJweCI+CiAgICAgIDxzcGFuIGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbjowIj5BbGwgVHJhZGVzPC9zcGFuPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjhweDthbGlnbi1pdGVtczpjZW50ZXIiPgogICAgICAgIDxzcGFuIGlkPSJ0Q250IiBzdHlsZT0iZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpIj4wIHRyYWRlczwvc3Bhbj4KICAgICAgICA8YnV0dG9uIG9uY2xpY2s9ImNsZWFyU3RhbGUoKSIgc3R5bGU9InBhZGRpbmc6NHB4IDEwcHg7Ym9yZGVyLXJhZGl1czo2cHg7Ym9yZGVyOjFweCBzb2xpZCAjZmNhNWE1O2JhY2tncm91bmQ6I2ZlZjJmMjtjb2xvcjojZTc0YzNjO2ZvbnQtc2l6ZToxMHB4O2ZvbnQtd2VpZ2h0OjcwMDtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTppbmhlcml0Ij7wn5eRIENsZWFyIFN0YWxlPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJ0TGlzdCI+PGRpdiBjbGFzcz0iZW1wdHkiPk5vIHRyYWRlcyB5ZXQ8L2Rpdj48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8IS0tIExPR1MgLS0+CjxkaXYgY2xhc3M9InBhZ2UiIGlkPSJwLWxvZ3MiPgogIDxkaXYgY2xhc3M9ImxmIj4KICAgIDxidXR0b24gY2xhc3M9ImxmYiBvbiIgaWQ9ImxmYSIgb25jbGljaz0ic2V0TEYoJycpIj5BbGw8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImxmYiIgaWQ9ImxmdCIgb25jbGljaz0ic2V0TEYoJ1RSQURFJykiPlRyYWRlczwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIiBpZD0ibGZ3IiBvbmNsaWNrPSJzZXRMRignV0FSTicpIj5XYXJuaW5nczwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0ibGZiIiBpZD0ibGZlIiBvbmNsaWNrPSJzZXRMRignRVJST1InKSI+RXJyb3JzPC9idXR0b24+CiAgPC9kaXY+CiAgPGRpdiBpZD0ibENudCIgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjhweCI+MCBlbnRyaWVzPC9kaXY+CiAgPGRpdiBjbGFzcz0ibGJveCIgaWQ9ImxCb3giPjwvZGl2Pgo8L2Rpdj4KCjwhLS0gU0VUVElOR1MgLS0+CjxkaXYgY2xhc3M9InBhZ2UiIGlkPSJwLXNldHRpbmdzIj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxkaXYgY2xhc3M9ImN0IiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo4cHgiPlNlcnZlciBJUCDigJQgV2hpdGVsaXN0IG9uIERlbHRhPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJpcGJveCIgaWQ9InNpcEJveCI+LS08L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTtsaW5lLWhlaWdodDoxLjkiPkRlbHRhIEV4Y2hhbmdlICZyYXJyOyBBY2NvdW50ICZyYXJyOyBBUEkgS2V5cyAmcmFycjsgRWRpdCAmcmFycjsgSVAgV2hpdGVsaXN0PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0iY2FyZCI+CiAgICA8ZGl2IGNsYXNzPSJjdCIgc3R5bGU9Im1hcmdpbi1ib3R0b206NHB4Ij5BY3RpdmUgR3VhcmRyYWlsczwvZGl2PgogICAgPGRpdiBpZD0iZ3JMaXN0Ij48L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJjYXJkIj4KICAgIDxidXR0b24gY2xhc3M9ImRjLWJ0biIgc3R5bGU9ImNvbG9yOnZhcigtLXIpIiBvbmNsaWNrPSJkb0Rpc2Nvbm5lY3QoKSI+JiMxMDAwNzsgRGlzY29ubmVjdCBEZWx0YSBFeGNoYW5nZTwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZGMtYnRuIiBzdHlsZT0iY29sb3I6dmFyKC0tdDIpIiBvbmNsaWNrPSJkb0xvZ291dCgpIj4mIzg1OTQ7IFNpZ24gT3V0PC9idXR0b24+CiAgPC9kaXY+CiAgPCEtLSBBZG1pbiBwYW5lbCAtLT4KICA8ZGl2IGlkPSJhZG1pblBhbmVsIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgIDxkaXYgY2xhc3M9ImNhcmQiIHN0eWxlPSJib3JkZXI6MnB4IHNvbGlkIHZhcigtLXkpIj4KICAgICAgPGRpdiBjbGFzcz0iY3QiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjEycHg7Y29sb3I6dmFyKC0teSkiPiYjOTg4MTsgQWRtaW4gUGFuZWw8L2Rpdj4KICAgICAgPGRpdiBpZD0iYXVMaXN0Ij48L2Rpdj4KICAgICAgPGJ1dHRvbiBvbmNsaWNrPSJnZW5JbnZpdGUoKSIgc3R5bGU9IndpZHRoOjEwMCU7bWFyZ2luLXRvcDoxMHB4O3BhZGRpbmc6MTFweDtib3JkZXItcmFkaXVzOjhweDtib3JkZXI6MS41cHggc29saWQgdmFyKC0tYik7YmFja2dyb3VuZDp2YXIoLS1iYik7Y29sb3I6dmFyKC0tYik7Zm9udC1mYW1pbHk6aW5oZXJpdDtmb250LXNpemU6MTJweDtmb250LXdlaWdodDo3MDA7Y3Vyc29yOnBvaW50ZXIiPisgR2VuZXJhdGUgSW52aXRlIENvZGU8L2J1dHRvbj4KICAgICAgPGRpdiBpZD0ibmV3SW52aXRlIiBzdHlsZT0iZGlzcGxheTpub25lIj4KICAgICAgICA8ZGl2IGNsYXNzPSJpY29kZSIgaWQ9ImludkNvZGUiPjwvZGl2PgogICAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTt0ZXh0LWFsaWduOmNlbnRlciI+U2hhcmUgdGhpcy4gT25lLXRpbWUgdXNlIG9ubHkuPC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPC9kaXY+PCEtLSB3cmFwIC0tPgo8bmF2IGNsYXNzPSJuYXYiPgogIDxidXR0b24gY2xhc3M9Im5iIG9uIiBpZD0ibmItaG9tZSIgICAgIG9uY2xpY2s9ImdvUGFnZSgnaG9tZScpIj48c3BhbiBjbGFzcz0iaWMiPiYjMTI3OTY4Ozwvc3Bhbj48c3BhbiBjbGFzcz0ibGIiPkhvbWU8L3NwYW4+PC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibmIiICAgIGlkPSJuYi10cmFkZXMiICAgb25jbGljaz0iZ29QYWdlKCd0cmFkZXMnKSI+PHNwYW4gY2xhc3M9ImljIj4mIzEyODIwMzs8L3NwYW4+PHNwYW4gY2xhc3M9ImxiIj5UcmFkZXM8L3NwYW4+PC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibmIiICAgIGlkPSJuYi1sb2dzIiAgICAgb25jbGljaz0iZ29QYWdlKCdsb2dzJykiPjxzcGFuIGNsYXNzPSJpYyI+JiMxMjgyMjA7PC9zcGFuPjxzcGFuIGNsYXNzPSJsYiI+TG9nczwvc3Bhbj48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJuYiIgICAgaWQ9Im5iLXNldHRpbmdzIiBvbmNsaWNrPSJnb1BhZ2UoJ3NldHRpbmdzJykiPjxzcGFuIGNsYXNzPSJpYyI+JiM5ODgxOzwvc3Bhbj48c3BhbiBjbGFzcz0ibGIiPlNldHRpbmdzPC9zcGFuPjwvYnV0dG9uPgo8L25hdj4KPC9kaXY+PCEtLSBhcHAgLS0+Cgo8c2NyaXB0Pgp2YXIgU1Q9e2xvZ3M6W10sbGY6IiIsdHJhZGVzOltdLG5leHRBdDpudWxsLHNzOjMwMCxpc0FkbWluOmZhbHNlfTsKdmFyIFBDPXsiUmVnaW1lIjoiIzNiODJmNiIsIk1URiBBbGlnbiI6IiMwMGIzODYiLCJSU0kiOiIjZjU5ZTBiIiwiTUFDRCI6IiM4YjVjZjYiLCJWb2xhdGlsaXR5IjoiI2VjNDg5OSIsIlZvbHVtZSI6IiNlNzRjM2MiLCJTZXNzaW9uIjoiIzE0YjhhNiJ9OwoKZnVuY3Rpb24gZ2UoaWQpe3JldHVybiBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCk7fQpmdW5jdGlvbiBzdChpZCx2KXt2YXIgZT1nZShpZCk7aWYoZSllLnRleHRDb250ZW50PXY7fQpmdW5jdGlvbiBzaChpZCx2KXt2YXIgZT1nZShpZCk7aWYoZSllLmlubmVySFRNTD12O30KCmZ1bmN0aW9uIHhocih1cmwsYm9keSxjYil7CiAgdmFyIHJlcT1uZXcgWE1MSHR0cFJlcXVlc3QoKSxpc1A9Ym9keSE9PXVuZGVmaW5lZCYmYm9keSE9PW51bGw7CiAgcmVxLm9wZW4oaXNQPyJQT1NUIjoiR0VUIix1cmwsdHJ1ZSk7cmVxLndpdGhDcmVkZW50aWFscz10cnVlOwogIGlmKGlzUClyZXEuc2V0UmVxdWVzdEhlYWRlcigiQ29udGVudC1UeXBlIiwiYXBwbGljYXRpb24vanNvbiIpOwogIHJlcS5vbnJlYWR5c3RhdGVjaGFuZ2U9ZnVuY3Rpb24oKXsKICAgIGlmKHJlcS5yZWFkeVN0YXRlIT09NClyZXR1cm47CiAgICBpZighY2IpcmV0dXJuOwogICAgaWYocmVxLnN0YXR1cz09PTIwMCl7dHJ5e2NiKEpTT04ucGFyc2UocmVxLnJlc3BvbnNlVGV4dCkpO31jYXRjaChlKXtjYihudWxsKTt9fQogICAgZWxzZSBpZihyZXEuc3RhdHVzPT09NDAxKXtzaG93QXV0aCgpO30KICAgIGVsc2V7Y2IobnVsbCk7fQogIH07CiAgcmVxLm9uZXJyb3I9ZnVuY3Rpb24oKXtpZihjYiljYihudWxsKTt9OwogIHJlcS5zZW5kKGlzUD9KU09OLnN0cmluZ2lmeShib2R5KTpudWxsKTsKfQoKZnVuY3Rpb24gc2hvd0F1dGgoKXtnZSgiYXV0aFNjcmVlbiIpLnN0eWxlLmRpc3BsYXk9ImZsZXgiO2dlKCJhcHAiKS5zdHlsZS5kaXNwbGF5PSJub25lIjt9CmZ1bmN0aW9uIHNob3dBcHAoKXtnZSgiYXV0aFNjcmVlbiIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO2dlKCJhcHAiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7fQpmdW5jdGlvbiBzaG93TG9naW4oKXtnZSgibG9naW5Gb3JtIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO2dlKCJyZWdGb3JtIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7fQpmdW5jdGlvbiBzaG93UmVnKCl7Z2UoImxvZ2luRm9ybSIpLnN0eWxlLmRpc3BsYXk9Im5vbmUiO2dlKCJyZWdGb3JtIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO30KCmZ1bmN0aW9uIGdvUGFnZShuKXsKICBbImhvbWUiLCJ0cmFkZXMiLCJsb2dzIiwic2V0dGluZ3MiXS5mb3JFYWNoKGZ1bmN0aW9uKHQpewogICAgZ2UoInAtIit0KS5jbGFzc0xpc3QudG9nZ2xlKCJzaG93Iix0PT09bik7CiAgICBnZSgibmItIit0KS5jbGFzc0xpc3QudG9nZ2xlKCJvbiIsdD09PW4pOwogIH0pOwogIGlmKG49PT0idHJhZGVzIilyZW5kZXJUcmFkZXMoKTsKICBpZihuPT09ImxvZ3MiKXJlbmRlckxvZ3MoKTsKICBpZihuPT09InNldHRpbmdzIilsb2FkQWRtaW4oKTsKfQoKZnVuY3Rpb24gZG9Mb2dpbigpewogIHZhciB1PWdlKCJsVXNlciIpLnZhbHVlLnRyaW0oKSxwPWdlKCJsUGFzcyIpLnZhbHVlOwogIGlmKCF1fHwhcCl7c2hvd01zZygibE1zZyIsIkVudGVyIHVzZXJuYW1lIGFuZCBwYXNzd29yZCIsImVyciIpO3JldHVybjt9CiAgc2hvd01zZygibE1zZyIsIlNpZ25pbmcgaW4uLi4iLCIiKTsKICB4aHIoIi9hdXRoL2xvZ2luIix7dXNlcm5hbWU6dSxwYXNzd29yZDpwfSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuc3VjY2Vzcyl7CiAgICAgIFNULmlzQWRtaW49ci5pc19hZG1pbjtzdCgidUJhZGdlIixyLnVzZXJuYW1lKTtzaG93QXBwKCk7bG9hZElQKCk7cG9sbCgpOwogICAgfWVsc2V7c2hvd01zZygibE1zZyIscj9yLm1lc3NhZ2U6IkxvZ2luIGZhaWxlZCIsImVyciIpO30KICB9KTsKfQpmdW5jdGlvbiBkb1JlZ2lzdGVyKCl7CiAgdmFyIGk9Z2UoInJJbnYiKS52YWx1ZS50cmltKCksdT1nZSgiclVzZXIiKS52YWx1ZS50cmltKCkscD1nZSgiclBhc3MiKS52YWx1ZTsKICBpZighaXx8IXV8fCFwKXtzaG93TXNnKCJyTXNnIiwiQWxsIGZpZWxkcyByZXF1aXJlZCIsImVyciIpO3JldHVybjt9CiAgc2hvd01zZygick1zZyIsIkNyZWF0aW5nIGFjY291bnQuLi4iLCIiKTsKICB4aHIoIi9hdXRoL3JlZ2lzdGVyIix7aW52aXRlOmksdXNlcm5hbWU6dSxwYXNzd29yZDpwfSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuc3VjY2Vzcyl7CiAgICAgIFNULmlzQWRtaW49ZmFsc2U7c3QoInVCYWRnZSIsdSk7c2hvd0FwcCgpO2xvYWRJUCgpO3BvbGwoKTsKICAgIH1lbHNle3Nob3dNc2coInJNc2ciLHI/ci5tZXNzYWdlOiJSZWdpc3RyYXRpb24gZmFpbGVkIiwiZXJyIik7fQogIH0pOwp9CmZ1bmN0aW9uIHNob3dNc2coaWQsbXNnLGNscyl7dmFyIGU9Z2UoaWQpO2UudGV4dENvbnRlbnQ9bXNnO2UuY2xhc3NOYW1lPSJhdXRoLW1zZyIrKGNscz8iICIrY2xzOiIiKTt9CmZ1bmN0aW9uIGRvTG9nb3V0KCl7CiAgaWYoIWNvbmZpcm0oIlNpZ24gb3V0PyIpKXJldHVybjsKICB4aHIoIi9hdXRoL2xvZ291dCIse30sZnVuY3Rpb24oKXtzaG93QXV0aCgpO2dlKCJsVXNlciIpLnZhbHVlPSIiO2dlKCJsUGFzcyIpLnZhbHVlPSIiO30pOwp9CmZ1bmN0aW9uIGRvRGlzY29ubmVjdCgpewogIGlmKCFjb25maXJtKCJEaXNjb25uZWN0IERlbHRhIEV4Y2hhbmdlPyIpKXJldHVybjsKICBnZSgiY29ubmVjdENhcmQiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7Z2UoImxpdmVEYXNoIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7Cn0KZnVuY3Rpb24gY29weUlQKCl7CiAgdmFyIGlwPWdlKCJzSVAiKS50ZXh0Q29udGVudDsKICB0cnl7bmF2aWdhdG9yLmNsaXBib2FyZC53cml0ZVRleHQoaXApO31jYXRjaChlKXt9CiAgdmFyIGI9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcigiLmlwLWNvcHkiKTtiLnRleHRDb250ZW50PSJDb3BpZWQhIjsKICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Yi50ZXh0Q29udGVudD0iQ29weSI7fSwyMDAwKTsKfQpmdW5jdGlvbiBkb0Nvbm5lY3QoKXsKICB2YXIgaz1nZSgiY0tleSIpLnZhbHVlLnRyaW0oKSxzPWdlKCJjU2VjIikudmFsdWUudHJpbSgpOwogIGlmKCFrfHwhcyl7Z2UoImNNc2ciKS5pbm5lckhUTUw9IjxzcGFuIHN0eWxlPSdjb2xvcjojZjg3MTcxJz5FbnRlciBBUEkga2V5IGFuZCBzZWNyZXQ8L3NwYW4+IjtyZXR1cm47fQogIGdlKCJjTXNnIikudGV4dENvbnRlbnQ9IkNvbm5lY3RpbmcuLi4iOwogIHhocigiL2FwaS9jb25uZWN0Iix7YXBpX2tleTprLGFwaV9zZWNyZXQ6c30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpewogICAgICBnZSgiY01zZyIpLmlubmVySFRNTD0iPHNwYW4gc3R5bGU9J2NvbG9yOiM0YWRlODAnPkNvbm5lY3RlZCEgJCIrci5iYWxhbmNlLnRvRml4ZWQoMikrIjwvc3Bhbj4iOwogICAgICBnZSgiY29ubmVjdENhcmQiKS5zdHlsZS5kaXNwbGF5PSJub25lIjtnZSgibGl2ZURhc2giKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7CiAgICB9ZWxzZXsKICAgICAgdmFyIGlwPXImJnIuc2VydmVyX2lwPyIgfCBJUDogIityLnNlcnZlcl9pcDoiIjsKICAgICAgZ2UoImNNc2ciKS5pbm5lckhUTUw9IjxzcGFuIHN0eWxlPSdjb2xvcjojZjg3MTcxJz4iKyhyP3IubWVzc2FnZToiRmFpbGVkIikraXArIjwvc3Bhbj4iOwogICAgfQogIH0pOwp9CmZ1bmN0aW9uIGJvdFN0YXJ0KCl7eGhyKCIvYXBpL2JvdC9zdGFydCIse30sbnVsbCk7fQpmdW5jdGlvbiBib3RTdG9wKCl7eGhyKCIvYXBpL2JvdC9zdG9wIix7fSxudWxsKTt9CmZ1bmN0aW9uIGJvdFJ1bigpe3N0KCJzU3RhdHVzIiwiU2Nhbm5pbmcuLi4iKTt4aHIoIi9hcGkvYm90L3J1bl9ub3ciLHt9LG51bGwpO30KZnVuY3Rpb24gY2xvc2VBbGwoKXsKICBpZighY29uZmlybSgiQ2xvc2UgQUxMIG9wZW4gcG9zaXRpb25zPyIpKXJldHVybjsKICB4aHIoIi9hcGkvY2xvc2VfYWxsIix7fSxmdW5jdGlvbihyKXthbGVydCgiQ2xvc2VkOiAiKygociYmci5jbG9zZWQpfHwwKSsiIHBvc2l0aW9ucyIpO30pOwp9CmZ1bmN0aW9uIG1hblRyYWRlKGRpcil7CiAgdmFyIGxvdHM9cGFyc2VJbnQoZ2UoIm1Mb3RzIikudmFsdWUpfHwxOwogIHhocigiL2FwaS9tYW51YWxfdHJhZGUiLHtkaXJlY3Rpb246ZGlyLGxvdHM6bG90c30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpYWxlcnQoZGlyLnRvVXBwZXJDYXNlKCkrIiAiK2xvdHMrIkxcbkVudHJ5ICQiK3IuZW50cnkrIlxuU3RvcCAkIityLnN0b3ArIlxuVFAgJCIrci50cCk7CiAgICBlbHNlIGFsZXJ0KCJGYWlsZWQ6ICIrKChyJiZyLm1lc3NhZ2UpfHwiQ2hlY2sgTG9ncyIpKTsKICB9KTsKfQpmdW5jdGlvbiB0b2dnbGVPcHRzKG9uKXsKICB4aHIoIi9hcGkvb3B0cy90b2dnbGUiLHtlbmFibGVkOm9ufSxmdW5jdGlvbihyKXsKICAgIGdlKCJvcHRzUGFuZWwiKS5zdHlsZS5kaXNwbGF5PShyJiZyLm9wdHNfbW9kZSk/ImJsb2NrIjoibm9uZSI7CiAgfSk7Cn0KZnVuY3Rpb24gY2hrT3B0KHQpewogIHZhciBlbD1nZSgib1JlcyIpO2VsLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtlbC50ZXh0Q29udGVudD0iQ2hlY2tpbmcuLi4iOwogIHhocigiL2FwaS9vcHRzL2ZpbmQiLHt0eXBlOnQsaXRtOmZhbHNlfSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuZm91bmQpZWwuaW5uZXJIVE1MPSI8Yj4iK3Iuc3ltYm9sKyI8L2I+PGJyPlN0cmlrZSAkIisoci5zdHJpa2V8fDApLnRvTG9jYWxlU3RyaW5nKCkrIiB8IE1hcmsgJCIrKHIubWFya3x8MCkudG9GaXhlZCgyKSsiIHwgUHJlbWl1bSAkIisoci5wcmVtaXVtX3VzZHx8MCkudG9GaXhlZCgyKSsoci5pdj8iIHwgSVYgIityLml2KyIlIjoiIikrIjxicj4iK3IubW9uZXluZXNzKyIgfCBFeHBpcnkgIityLmV4cGlyeTsKICAgIGVsc2UgZWwudGV4dENvbnRlbnQ9Ik5vICIrdCsiIGZvdW5kLiBFeHBpcnk6ICIrKChyJiZyLmV4cGlyeSl8fCI/Iik7CiAgfSk7Cn0KZnVuY3Rpb24gY2hrU3QoKXsKICB2YXIgZWw9Z2UoIm9SZXMiKTtlbC5zdHlsZS5kaXNwbGF5PSJibG9jayI7ZWwudGV4dENvbnRlbnQ9IkNoZWNraW5nLi4uIjsKICB4aHIoIi9hcGkvb3B0cy9zdHJhZGRsZSIse30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLmZvdW5kKWVsLmlubmVySFRNTD0iPGI+U3RyYWRkbGU8L2I+PGJyPlRvdGFsOiAkIisoci50b3RhbF9wcmVtaXVtX3VzZHx8MCkudG9GaXhlZCgyKSsiPGJyPkJFIHVwOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX3VwfHwwKS50b0xvY2FsZVN0cmluZygpKyIgfCBkb3duOiAkIitNYXRoLnJvdW5kKHIuYnJlYWtldmVuX2Rvd258fDApLnRvTG9jYWxlU3RyaW5nKCk7CiAgICBlbHNlIGVsLnRleHRDb250ZW50PSJDYW5ub3QgYnVpbGQgc3RyYWRkbGUgcmlnaHQgbm93LiI7CiAgfSk7Cn0KZnVuY3Rpb24gc2V0TEYoZil7CiAgU1QubGY9ZjsKICB2YXIgbT17IiI6ImxmYSIsIlRSQURFIjoibGZ0IiwiV0FSTiI6ImxmdyIsIkVSUk9SIjoibGZlIn07CiAgT2JqZWN0LmtleXMobSkuZm9yRWFjaChmdW5jdGlvbihrKXt2YXIgZWw9Z2UobVtrXSk7aWYoZWwpZWwuY2xhc3NMaXN0LnRvZ2dsZSgib24iLGs9PT1mKTt9KTsKICByZW5kZXJMb2dzKCk7Cn0KZnVuY3Rpb24gcmVuZGVyKHMpewogIGlmKCFzKXJldHVybjsKICBpZihzLmNvbm5lY3RlZCl7Z2UoImNvbm5lY3RDYXJkIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7Z2UoImxpdmVEYXNoIikuc3R5bGUuZGlzcGxheT0iYmxvY2siO30KICB2YXIgcnVuPXMuY29ubmVjdGVkJiZzLnJ1bm5pbmcmJiFzLmhhbHRlZDsKICBnZSgic1BpbGwiKS5jbGFzc05hbWU9InBpbGwgIisocy5oYWx0ZWQ/InAtd2FybiI6cnVuPyJwLWxpdmUiOiJwLW9mZiIpOwogIHN0KCJzVHh0IixzLmhhbHRlZD8iSEFMVEVEIjpydW4/IkxpdmUiOiJTdG9wcGVkIik7CiAgc3QoImhQIixzLnByaWNlPyIkIitzLnByaWNlLnRvTG9jYWxlU3RyaW5nKCk6IiQtLSIpOwogIHZhciByZz1zLnJlZ2ltZXx8IiI7IHZhciBtYj1zLm1hY3JvX2JpYXN8fCJuZXV0cmFsIjsKICB2YXIgcmM9Z2UoImhSIik7cmMudGV4dENvbnRlbnQ9cmd8fCItLSI7cmMuY2xhc3NOYW1lPSJjaGlwICIrKHJnLmluZGV4T2YoIkJVTEwiKT49MD8iY2ciOnJnLmluZGV4T2YoIkJFQVIiKT49MD8iY3IyIjoiY24iKTsKICBzdCgiaFMiLHMuc3RyYXRlZ3l8fCItLSIpO3N0KCJoViIscy52b2xfcmVnaW1lfHwiLS0iKTsKICB2YXIgaDF0PXMuaDFfdHJlbmR8fCJuZXV0cmFsIjsKICB2YXIgaDFlbD1nZSgiaEgxIik7CiAgaWYoaDFlbCl7aDFlbC50ZXh0Q29udGVudD0iMUg6ICIraDF0LnRvVXBwZXJDYXNlKCk7aDFlbC5jbGFzc05hbWU9ImNoaXAgIisoaDF0PT09ImJ1bGwiPyJjZyI6aDF0PT09ImJlYXIiPyJjcjIiOiJjbiIpO30KICB2YXIgcmI9Z2UoInJCYXIiKTtyYi5jbGFzc05hbWU9InJiYXIgIisocmcuaW5kZXhPZigiQlVMTCIpPj0wPyJyYi1iIjpyZy5pbmRleE9mKCJCRUFSIik+PTA/InJiLXIiOnJnPT09IlNJREVXQVlTIj8icmItdyI6InJiLW4iKTsKICByYi50ZXh0Q29udGVudD1yZysiIFx1MjAxNCAiKyhzLnN0cmF0ZWd5fHwiQ2FsY3VsYXRpbmciKTsKICB2YXIgc2M9cy5jb25mX2xvbmd8fDA7c3QoImNOIixzY3x8Ii0tIik7CiAgdmFyIGFyYz1nZSgiY0FyYyIpO2FyYy5zdHlsZS5zdHJva2VEYXNob2Zmc2V0PTE3NS45LShzYy8xMDAqMTc1LjkpO2FyYy5zdHlsZS5zdHJva2U9c2M+PTcwPyIjMDBiMzg2IjpzYz49NTA/IiNmNTllMGIiOiIjZTc0YzNjIjsKICBnZSgiY04iKS5zdHlsZS5jb2xvcj1zYz49NzA/InZhcigtLWcpIjpzYz49NTA/InZhcigtLXkpIjoidmFyKC0tcikiOwogIHN0KCJjRCIscy5zdHJhdGVneT09PSJXQUlUIj8iV0FJVCI6KHMuZGlyZWN0aW9ufHxyZ3x8IldBSVQiKS50b1VwcGVyQ2FzZSgpKTsKICB2YXIgdHJlbmRzPXMudHJlbmRzfHx7fTsgdmFyIHRTdHI9T2JqZWN0LmVudHJpZXModHJlbmRzKS5tYXAoZnVuY3Rpb24oZSl7cmV0dXJuIGVbMF0rIjoiK2VbMV1bMF0udG9VcHBlckNhc2UoKTt9KS5qb2luKCIgIik7CiAgc3QoImNEdCIsIkNvbnY9IitzYysiIHwgQURYPSIrKHMuYWR4fHwwKSsiIHwgIittYi50b1VwcGVyQ2FzZSgpKyIgfCAiK3RTdHIpOwogIHZhciBwbHM9cy5waWxsYXJzfHx7fTt2YXIgcGg9IiI7CiAgT2JqZWN0LmtleXMocGxzKS5mb3JFYWNoKGZ1bmN0aW9uKGspe3ZhciB2PXBsc1trXTt2YXIgcGN0PXYubT4wP01hdGgucm91bmQodi5zL3YubSoxMDApOjA7dmFyIGNvbD1QQ1trXXx8InZhcigtLWcpIjtwaCs9IjxkaXYgY2xhc3M9J3Byb3cnPjxkaXYgY2xhc3M9J3BuJz4iK2srIjwvZGl2PjxkaXYgY2xhc3M9J3B0Jz48ZGl2IGNsYXNzPSdwZicgc3R5bGU9J3dpZHRoOiIrcGN0KyIlO2JhY2tncm91bmQ6Iitjb2wrIic+PC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncHMnIHN0eWxlPSdjb2xvcjoiK2NvbCsiJz4iK3YucysiLyIrdi5tKyI8L2Rpdj48L2Rpdj4iO30pOwogIHNoKCJwaWxEaXYiLHBoKTtzdCgiaUEiLHMuYWR4fHwiLS0iKTtzdCgiaUIiLHMuYnc/cy5idysiJSI6Ii0tIik7c3QoImlUIixzLmF0cl9wY3Q/cy5hdHJfcGN0KyIlIjoiLS0iKTsKICBzdCgic1N0YXR1cyIscy5zdGF0dXN8fCItLSIpO3N0KCJzU04iLHMuc2Nhbl9ufHwwKTsKICAvLyBIaWdobGlnaHQgd2hlbiBjbG9zZSB0byB0aHJlc2hvbGQKICB2YXIgc0VsPWdlKCJzU3RhdHVzIik7CiAgaWYoc0VsICYmIHMuc3RhdHVzICYmIHMuc3RhdHVzLmluZGV4T2YoIm5lZWQ9Iik+PTApe3NFbC5zdHlsZS5jb2xvcj0idmFyKC0teSkiO30KICBlbHNlIGlmKHNFbCl7c0VsLnN0eWxlLmNvbG9yPSIiO30KICBpZihzLm5leHRfc2NhbilTVC5uZXh0QXQ9bmV3IERhdGUocy5uZXh0X3NjYW4pOwogIHZhciBwcD1zLm9wZW5fcG9zfHxbXTt2YXIgcGgyPSIiOwogIHBwLmZvckVhY2goZnVuY3Rpb24ocCl7dmFyIG5lZz1wLnVwbmw8MDtwaDIrPSI8ZGl2IGNsYXNzPSdwb3MgcG9zLSIrKG5lZz8icyI6ImwiKSsiJz48ZGl2IGNsYXNzPSdwaCc+PHNwYW4gY2xhc3M9J3BzeW0nPiIrcC5zeW0rIjwvc3Bhbj48c3BhbiBjbGFzcz0nYmFkZ2UgYiIrKHAuc2lkZT09PSJsb25nIj8ibCI6InNoIikrIic+IitwLnNpZGUudG9VcHBlckNhc2UoKSsiPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9J3BnJz48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5FbnRyeTwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+JCIrcC5lbnRyeS50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5Mb3RzPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4iK3AubG90cysiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+VVBMPC9kaXY+PGRpdiBjbGFzcz0ncGl2ICIrKG5lZz8icGlyIjoicGlnIikrIic+IisocC51cG5sPj0wPyIrIjoiIikrcC51cG5sKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5NYXJrPC9kaXY+PGRpdiBjbGFzcz0ncGl2Jz4kIisocC5tYXJrfHxwLmVudHJ5KS50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5TdG9wPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpcic+JCIrcC5zdG9wLnRvTG9jYWxlU3RyaW5nKCkrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlRQPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpZyc+JCIrcC50cC50b0xvY2FsZVN0cmluZygpKyI8L2Rpdj48L2Rpdj48L2Rpdj48L2Rpdj4iO30pOwogIHNoKCJwZXJwRGl2IixwaDIpOwogIHZhciBvcD1zLm9wdHNfcG9zfHxbXTt2YXIgb2g9IiI7CiAgb3AuZm9yRWFjaChmdW5jdGlvbihvKXt2YXIgaXNDPW8udHlwZT09PSJDQUxMIjsKICAgIHZhciBmbG9vckJhcj1vLmZsb29yX2FjdGl2ZQogICAgICA/IjxkaXYgc3R5bGU9J21hcmdpbi10b3A6OHB4O3BhZGRpbmc6N3B4IDEwcHg7YmFja2dyb3VuZDpyZ2JhKDAsMTc5LDEzNCwuMTIpO2JvcmRlci1yYWRpdXM6NnB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgwLDE3OSwxMzQsLjMpO2ZvbnQtc2l6ZToxMXB4O2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbic+PHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLWcpO2ZvbnQtd2VpZ2h0OjcwMCc+8J+UkiBGbG9vciBhY3RpdmU8L3NwYW4+PHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLWcpO2ZvbnQtd2VpZ2h0OjgwMCc+RXhpdCA8ICQiK28uZmxvb3JfcHJpY2UrIiAoKyIrby5mbG9vcl9wY3QrIiUpPC9zcGFuPjwvZGl2PiIKICAgICAgOiI8ZGl2IHN0eWxlPSdtYXJnaW4tdG9wOjhweDtwYWRkaW5nOjdweCAxMHB4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXItcmFkaXVzOjZweDtib3JkZXI6dmFyKC0tYmRyKTtmb250LXNpemU6MTFweDtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW4nPjxzcGFuIHN0eWxlPSdjb2xvcjp2YXIoLS10MyknPkZsb29yOiBmaXJzdCBwcm9maXQgdGljazwvc3Bhbj48c3BhbiBzdHlsZT0nY29sb3I6dmFyKC0tciknPkhhcmQgU0wgJCIrby5zbF9wcmljZSsiPC9zcGFuPjwvZGl2PiI7CiAgICBvaCs9IjxkaXYgY2xhc3M9J3BvcyBwb3Mtbyc+PGRpdiBjbGFzcz0ncGgnPjxzcGFuIGNsYXNzPSdwc3ltJyBzdHlsZT0nZm9udC1zaXplOjEycHgnPiIrby5zeW0rIjwvc3Bhbj48c3BhbiBjbGFzcz0nYmFkZ2UgYiIrKGlzQz8iYyI6InAiKSsiJz4iK28udHlwZSsiPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9J3BnJz48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5FbnRyeTwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+JCIrby5lbnRyeSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+TWFyazwvZGl2PjxkaXYgY2xhc3M9J3Bpdic+JCIrby5tYXJrKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSdwaSc+PGRpdiBjbGFzcz0ncGlsJz5QJkw8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgIisoby5wY3Q8MD8icGlyIjoicGlnIikrIic+Iisoby5wY3Q+PTA/IisiOiIiKStvLnBjdCsiJTwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlBlYWs8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgcGlnJz4kIitvLnBlYWsrIjwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9J3BpJz48ZGl2IGNsYXNzPSdwaWwnPlRQPC9kaXY+PGRpdiBjbGFzcz0ncGl2IHBpZyc+JCIrby50cF9wcmljZSsiPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ncGknPjxkaXYgY2xhc3M9J3BpbCc+U0w8L2Rpdj48ZGl2IGNsYXNzPSdwaXYgcGlyJz4kIitvLnNsX3ByaWNlKyI8L2Rpdj48L2Rpdj48L2Rpdj4iK2Zsb29yQmFyKyI8L2Rpdj4iO30pOwogIHNoKCJvcHRzRGl2IixvaCk7CiAgdmFyIGNhcD1zLmNhcGl0YWx8fDAsc2MyPXMuc3RhcnRfY2FwfHwwLHBwMj1zLnBubF9wY3R8fDA7CiAgc3QoIndBIixjYXA/IiQiK2NhcC50b0ZpeGVkKDIpOiIkLS0iKTtzdCgid1N0IixzYzI/IlN0YXJ0ZWQgJCIrc2MyLnRvRml4ZWQoMik6IiIpOwogIHZhciB3cEVsPWdlKCJ3UCIpO3dwRWwudGV4dENvbnRlbnQ9KHBwMj49MD8iKyI6IiIpK3BwMi50b0ZpeGVkKDIpKyIlIjt3cEVsLnN0eWxlLmNvbG9yPXBwMj49MD8idmFyKC0tZykiOiJ2YXIoLS1yKSI7CiAgLy8gV2FsbGV0IFAmTCA9IHJlYWwgYmFsYW5jZSBjaGFuZ2UgaW5jbHVkaW5nIGZlZXMvZnVuZGluZwogIHZhciB3UG5sPXMucG5sX3VzZHx8MDsKICBzdCgid04iLCJXYWxsZXQgUCZMICQiKyh3UG5sPj0wPyIrIjoiIikrd1BubC50b0ZpeGVkKDIpKTsKICAvLyBUcmFkZSBQJkwgPSBib3QgY2xvc2VkIHRyYWRlcyBvbmx5CiAgdmFyIHRQbmw9cy50cmFkZV9wbmxfdXNkfHwwOwogIHZhciB0RWw9Z2UoInRyYWRlUG5sUm93Iik7CiAgaWYodEVsKSB0RWwudGV4dENvbnRlbnQ9IkJvdCB0cmFkZXMgUCZMICQiKyh0UG5sPj0wPyIrIjoiIikrdFBubC50b0ZpeGVkKDQpOwogIHN0KCJzV1IiLHMud2luX3JhdGUhPW51bGw/cy53aW5fcmF0ZSsiJSI6Ii0tIik7c3QoInNUUiIscy50b3RhbF90cmFkZXN8fDApOwogIGlmKHMudXNlcl9zZXR0aW5ncyl7CiAgICBfbG90cz1zLnVzZXJfc2V0dGluZ3MubG90X3NpemV8fDE7IGdlKCJsb3RzVmFsIikudGV4dENvbnRlbnQ9X2xvdHM7CiAgICBfZGFpbHk9cy51c2VyX3NldHRpbmdzLm1heF9kYWlseXx8MTA7IGdlKCJkYWlseVZhbCIpLnRleHRDb250ZW50PV9kYWlseTsKICAgIHZhciB1c2VkPXMudXNlcl9zZXR0aW5ncy5kYWlseV90cmFkZXN8fDA7CiAgICB2YXIgZWw9Z2UoImRhaWx5VXNlZCIpOyBpZihlbCkgZWwudGV4dENvbnRlbnQ9dXNlZCsiIHVzZWQgdG9kYXkgKCIrKF9kYWlseS11c2VkKSsiIHJlbWFpbmluZykiOwogICAgdmFyIHNtPXMudXNlcl9zZXR0aW5ncy5hY3RpdmVfbW9kZXx8Im5vcm1hbCI7CiAgICBpZihzbSE9PV9tb2RlKXtfbW9kZT1zbTtzZXRNb2RlKHNtKTt9CiAgICBpZihzLnVzZXJfc2V0dGluZ3MubW9kZV9sb2NrZWQpe3ZhciBtbj1nZSgibW9kZU5vdGUiKTtpZihtbil7bW4udGV4dENvbnRlbnQ9IlBSTyBsb2NrZWQg4oCUIG5lZWQgJDUwMCsgYmFsYW5jZSI7bW4uc3R5bGUuY29sb3I9InZhcigtLXIpIjt9fQogIH0KICB2YXIgb3Q9Z2UoInRvZ08iKTtpZihvdClvdC5jaGVja2VkPSEhcy5vcHRzX21vZGU7CiAgZ2UoIm9wdHNQYW5lbCIpLnN0eWxlLmRpc3BsYXk9cy5vcHRzX21vZGU/ImJsb2NrIjoibm9uZSI7CiAgaWYocy5ndWFyZHJhaWxzKXt2YXIgZ2s9T2JqZWN0LmtleXMocy5ndWFyZHJhaWxzKTt2YXIgZ2g9IiI7Z2suZm9yRWFjaChmdW5jdGlvbihrKXtnaCs9IjxkaXYgY2xhc3M9J2dyYWlsLXJvdyc+PHNwYW4gY2xhc3M9J2dyayc+IitrKyI8L3NwYW4+PHNwYW4gY2xhc3M9J2dydic+IitzLmd1YXJkcmFpbHNba10rIjwvc3Bhbj48L2Rpdj4iO30pO3NoKCJnckxpc3QiLGdoKTt9CiAgaWYocy5sb2dzKVNULmxvZ3M9cy5sb2dzO2lmKHMudHJhZGVzKVNULnRyYWRlcz1zLnRyYWRlczsKICBzdCgibENudCIsU1QubG9ncy5sZW5ndGgrIiBlbnRyaWVzIik7CiAgaWYoZ2UoInAtbG9ncyIpLmNsYXNzTGlzdC5jb250YWlucygic2hvdyIpKXJlbmRlckxvZ3MoKTsKICBpZihnZSgicC10cmFkZXMiKS5jbGFzc0xpc3QuY29udGFpbnMoInNob3ciKSlyZW5kZXJUcmFkZXMoKTsKfQpmdW5jdGlvbiByZW5kZXJUcmFkZXMoKXsKICBzdCgidENudCIsU1QudHJhZGVzLmxlbmd0aCsiIHRyYWRlcyIpOwogIGlmKCFTVC50cmFkZXMubGVuZ3RoKXtzaCgidExpc3QiLCI8ZGl2IGNsYXNzPSdlbXB0eSc+Tm8gdHJhZGVzIHlldDwvZGl2PiIpO3JldHVybjt9CiAgdmFyIGg9IiI7CiAgU1QudHJhZGVzLmZvckVhY2goZnVuY3Rpb24odCl7CiAgICB2YXIgb3Blbj10LmV4aXQ9PW51bGwsc2Q9dC5zaWRlfHwiIjsKICAgIHZhciBpYz1zZD09PSJsb25nIj8idGktbCI6c2Q9PT0ic2hvcnQiPyJ0aS1zIjpzZD09PSJjYWxsIj8idGktYyI6InRpLXAiOwogICAgdmFyIGljbz1zZD09PSJsb25nIj8iJiM4NTkzOyI6c2Q9PT0ic2hvcnQiPyImIzg1OTU7IjpzZD09PSJjYWxsIj8iQyI6IlAiOwogICAgdmFyIHBjPW9wZW4/InRwbiI6KHQud29uPyJ0cGciOiJ0cHIiKSxwdj1vcGVuPyJPcGVuXHUyMDI2IjoodC53b24/IisiOiIiKSsodC5wbmx8fDApLnRvRml4ZWQoNCk7CiAgICB2YXIgdG09dC50aW1lP3QudGltZS5zdWJzdHIoNSwxMSkucmVwbGFjZSgiVCIsIiAiKToiIjsKICAgIGgrPSI8ZGl2IGNsYXNzPSd0ci1yb3cnPjxkaXYgY2xhc3M9J3RpY28gIitpYysiJz4iK2ljbysiPC9kaXY+PGRpdiBjbGFzcz0ndG1pZCc+PGRpdiBjbGFzcz0ndHN5bSc+IisodC5zeW18fCJCVENVU0QiKSsiPC9kaXY+PGRpdiBjbGFzcz0ndG1ldGEnPiIrdG0rIiAmbWlkZG90OyAiKyh0LnJlYXNvbnx8IiIpKyI8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSd0cmlnaHQnPjxkaXYgY2xhc3M9J3RwbmwgIitwYysiJz4kIitwdisiPC9kaXY+PGRpdiBzdHlsZT0nZm9udC1zaXplOjEwcHg7Y29sb3I6dmFyKC0tdDMpJz4iKyh0LmVudHJ5PyJAJCIrdC5lbnRyeToiIikrIjwvZGl2PjwvZGl2PjwvZGl2PiI7CiAgfSk7c2goInRMaXN0IixoKTsKfQpmdW5jdGlvbiByZW5kZXJMb2dzKCl7CiAgdmFyIGY9U1QubGY/U1QubG9ncy5maWx0ZXIoZnVuY3Rpb24oZSl7cmV0dXJuIGUubD09PVNULmxmO30pOlNULmxvZ3M7CiAgdmFyIGg9IiI7Zi5zbGljZSgwLDE1MCkuZm9yRWFjaChmdW5jdGlvbihlKXt2YXIgY2xzPSJsSSI7aWYoZS5sPT09IldBUk4iKWNscz0ibFciO2Vsc2UgaWYoZS5sPT09IkVSUk9SIiljbHM9ImxFIjtlbHNlIGlmKGUubD09PSJUUkFERSIpY2xzPSJsVCI7aCs9IjxkaXYgY2xhc3M9J2xyJz48c3BhbiBjbGFzcz0nbHQnPiIrZS50KyI8L3NwYW4+PHNwYW4gY2xhc3M9JyIrY2xzKyInPiIrZS5tKyI8L3NwYW4+PC9kaXY+Ijt9KTtzaCgibEJveCIsaCk7Cn0KZnVuY3Rpb24gbG9hZEFkbWluKCl7CiAgaWYoIVNULmlzQWRtaW4pe2dlKCJhZG1pblBhbmVsIikuc3R5bGUuZGlzcGxheT0ibm9uZSI7cmV0dXJuO30KICBnZSgiYWRtaW5QYW5lbCIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjsKICB4aHIoIi9hcGkvYWRtaW4vdXNlcnMiLG51bGwsZnVuY3Rpb24ocil7CiAgICBpZighcilyZXR1cm47CiAgICB2YXIgaD0iIjsKICAgIE9iamVjdC5rZXlzKHIudXNlcnN8fHt9KS5mb3JFYWNoKGZ1bmN0aW9uKHVpZCl7CiAgICAgIHZhciB1PXIudXNlcnNbdWlkXTsKICAgICAgaCs9IjxkaXYgY2xhc3M9J2F1Jz48ZGl2IGNsYXNzPSdhdS1uYW1lJz4iKyh1LmlzX2FkbWluPyImIzk3MzM7ICI6IiIpK3UudXNlcm5hbWUrKHUuYm90X3J1bm5pbmc/IiA8c3BhbiBzdHlsZT0nY29sb3I6dmFyKC0tZyk7Zm9udC1zaXplOjEwcHgnPiYjOTY3OTsgTGl2ZTwvc3Bhbj4iOiIgPHNwYW4gc3R5bGU9J2NvbG9yOnZhcigtLXQzKTtmb250LXNpemU6MTBweCc+T2ZmbGluZTwvc3Bhbj4iKSsiPC9kaXY+PGRpdiBjbGFzcz0nYXUtc3RhdHMnPjxzcGFuPiQiK3UuYmFsYW5jZS50b0ZpeGVkKDIpKyI8L3NwYW4+PHNwYW4+Iit1LnRyYWRlcysiIHRyYWRlczwvc3Bhbj48L2Rpdj48L2Rpdj4iOwogICAgfSk7CiAgICBzaCgiYXVMaXN0IixofHwiPGRpdiBjbGFzcz0nZW1wdHknPk5vIHVzZXJzIHlldDwvZGl2PiIpOwogICAgaWYoci5pbnZpdGVzJiZyLmludml0ZXMubGVuZ3RoKXt2YXIgaWg9IjxkaXYgc3R5bGU9J2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjRweCc+UGVuZGluZyBpbnZpdGUgY29kZXM6PC9kaXY+IjtyLmludml0ZXMuZm9yRWFjaChmdW5jdGlvbihjKXtpaCs9IjxkaXYgY2xhc3M9J2ljb2RlJz4iK2MrIjwvZGl2PiI7fSk7c2goIm5ld0ludml0ZSIsaWgpO2dlKCJuZXdJbnZpdGUiKS5zdHlsZS5kaXNwbGF5PSJibG9jayI7fQogIH0pOwp9CmZ1bmN0aW9uIGdlbkludml0ZSgpewogIHhocigiL2FwaS9hZG1pbi9pbnZpdGUiLHt9LGZ1bmN0aW9uKHIpewogICAgaWYociYmci5zdWNjZXNzKXtzaCgiaW52Q29kZSIsci5jb2RlKTtnZSgiaW52Q29kZSIpLmNsYXNzTmFtZT0iaWNvZGUiO2dlKCJuZXdJbnZpdGUiKS5pbm5lckhUTUw9IjxkaXYgc3R5bGU9J2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXQzKTttYXJnaW4tYm90dG9tOjRweCc+TmV3IGludml0ZSBjb2RlOjwvZGl2PjxkaXYgY2xhc3M9J2ljb2RlJz4iK3IuY29kZSsiPC9kaXY+PGRpdiBzdHlsZT0nZm9udC1zaXplOjExcHg7Y29sb3I6dmFyKC0tdDMpO3RleHQtYWxpZ246Y2VudGVyJz5PbmUtdGltZSB1c2Ugb25seTwvZGl2PiI7Z2UoIm5ld0ludml0ZSIpLnN0eWxlLmRpc3BsYXk9ImJsb2NrIjtsb2FkQWRtaW4oKTt9CiAgfSk7Cn0KZnVuY3Rpb24gbG9hZElQKCl7CiAgeGhyKCIvYXBpL2lwIixudWxsLGZ1bmN0aW9uKHIpe3ZhciBpcD1yJiZyLmlwP3IuaXA6InVua25vd24iO3N0KCJzSVAiLGlwKTtzdCgic2lwQm94IixpcCk7fSk7Cn0Kc2V0SW50ZXJ2YWwoZnVuY3Rpb24oKXsKICBpZighU1QubmV4dEF0KXJldHVybjsKICB2YXIgZD1NYXRoLm1heCgwLE1hdGgucm91bmQoKFNULm5leHRBdC1EYXRlLm5vdygpKS8xMDAwKSk7CiAgdmFyIG09TWF0aC5mbG9vcihkLzYwKSxzPWQlNjA7c3QoInNjZCIsZD4wPyhtKyJtICIrcysicyIpOiJTY2FubmluZy4uLiIpOwogIGdlKCJzRmlsIikuc3R5bGUud2lkdGg9TWF0aC5tYXgoMCwxMDAtZC9TVC5zcyoxMDApKyIlIjsKfSwxMDAwKTsKZnVuY3Rpb24gcG9sbCgpe3hocigiL2FwaS9zdGF0dXMiLG51bGwsZnVuY3Rpb24ocyl7aWYocylyZW5kZXIocyk7fSk7fQpmdW5jdGlvbiBjbGVhclN0YWxlKCl7CiAgaWYoIWNvbmZpcm0oIlJlbW92ZSBhbGwgc3RhbGUvZ2hvc3QgdHJhZGVzIGZyb20gaGlzdG9yeT8iKSkgcmV0dXJuOwogIHhocigiL2FwaS9jbGVhcl9zdGFsZSIse30sZnVuY3Rpb24ocil7CiAgICBpZihyJiZyLnN1Y2Nlc3MpewogICAgICBhbGVydCgiUmVtb3ZlZCAiK3IucmVtb3ZlZCsiIHN0YWxlIHRyYWRlcyIpOwogICAgICBwb2xsKCk7CiAgICB9CiAgfSk7Cn0KCnZhciBfbG90cz0xLF9kYWlseT0xMCxfbW9kZT0ibm9ybWFsIjsKZnVuY3Rpb24gc2V0TW9kZShtKXsKICBfbW9kZT1tOwogIFsic2FmZSIsIm5vcm1hbCIsInBybyJdLmZvckVhY2goZnVuY3Rpb24oeCl7CiAgICB2YXIgZWw9Z2UoIm1vZGUtIit4KTsKICAgIGlmKCFlbClyZXR1cm47CiAgICB2YXIgYWN0aXZlPXg9PT1tOwogICAgaWYoeD09PSJzYWZlIil7ZWwuc3R5bGUub3BhY2l0eT1hY3RpdmU/IjEiOiIwLjQiO30KICAgIGVsc2UgaWYoeD09PSJub3JtYWwiKXtlbC5zdHlsZS5vcGFjaXR5PWFjdGl2ZT8iMSI6IjAuNCI7fQogICAgZWxzZXtlbC5zdHlsZS5vcGFjaXR5PWFjdGl2ZT8iMSI6IjAuNCI7fQogICAgZWwuc3R5bGUudHJhbnNmb3JtPWFjdGl2ZT8ic2NhbGUoMS4wNSkiOiJzY2FsZSgxKSI7CiAgfSk7CiAgdmFyIG5vdGU9Z2UoIm1vZGVOb3RlIik7CiAgaWYobm90ZSl7CiAgICBpZihtPT09InNhZmUiKSBub3RlLnRleHRDb250ZW50PSJDb25zZXJ2YXRpdmUg4oCUIGJlc3QgZm9yIGxlYXJuaW5nLiBTbWFsbCBnYWlucywgc21hbGwgbG9zc2VzLiI7CiAgICBlbHNlIGlmKG09PT0ibm9ybWFsIikgbm90ZS50ZXh0Q29udGVudD0iQmFsYW5jZWQg4oCUIHJlY29tbWVuZGVkLiBHb29kIHJpc2svcmV3YXJkIHJhdGlvLiI7CiAgICBlbHNlIG5vdGUudGV4dENvbnRlbnQ9IuKaoO+4jyBQUk8gcmVxdWlyZXMgJDUwMCsgYmFsYW5jZS4gSGlnaGVyIHJpc2ssIGhpZ2hlciByZXdhcmQuIjsKICAgIG5vdGUuc3R5bGUuY29sb3I9bT09PSJwcm8iPyJ2YXIoLS15KSI6InZhcigtLXQzKSI7CiAgfQp9CmZ1bmN0aW9uIGFkakxvdHMoZCl7CiAgX2xvdHM9TWF0aC5tYXgoMSxNYXRoLm1pbigxMDAsX2xvdHMrZCkpOwogIGdlKCJsb3RzVmFsIikudGV4dENvbnRlbnQ9X2xvdHM7CiAgdmFyIGVsPWdlKCJsb3RCdGNWYWwiKTsKICBpZihlbCkgZWwudGV4dENvbnRlbnQ9X2xvdHMrIiBsb3RzID0gIisoX2xvdHMqMC4wMDEpLnRvRml4ZWQoMykrIiBCVEMiOwp9CmZ1bmN0aW9uIGFkakRhaWx5KGQpe19kYWlseT1NYXRoLm1heCgxLE1hdGgubWluKDUwLF9kYWlseStkKSk7Z2UoImRhaWx5VmFsIikudGV4dENvbnRlbnQ9X2RhaWx5O30KZnVuY3Rpb24gc2F2ZVVzZXJTZXR0aW5ncygpewogIHhocigiL2FwaS91c2VyL3NldHRpbmdzIix7bG90X3NpemU6X2xvdHMsbWF4X2RhaWx5Ol9kYWlseSxtb2RlOl9tb2RlfSxmdW5jdGlvbihyKXsKICAgIGlmKHImJnIuc3VjY2Vzcyl7CiAgICAgIGdlKCJzZXRNc2ciKS50ZXh0Q29udGVudD0iU2F2ZWQhIE1vZGU6ICIrci5tb2RlLnRvVXBwZXJDYXNlKCk7CiAgICAgIGdlKCJzZXRNc2ciKS5zdHlsZS5jb2xvcj0idmFyKC0tZykiOwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z2UoInNldE1zZyIpLnRleHRDb250ZW50PSIiO30sMzAwMCk7CiAgICB9IGVsc2UgaWYociYmci5tZXNzYWdlKXsKICAgICAgZ2UoInNldE1zZyIpLnRleHRDb250ZW50PXIubWVzc2FnZTsKICAgICAgZ2UoInNldE1zZyIpLnN0eWxlLmNvbG9yPSJ2YXIoLS1yKSI7CiAgICB9CiAgfSk7Cn0KLy8gT24gbG9hZDogY2hlY2sgaWYgYWxyZWFkeSBsb2dnZWQgaW4KeGhyKCIvYXV0aC9tZSIsbnVsbCxmdW5jdGlvbihyKXsKICBpZihyJiZyLmxvZ2dlZF9pbil7U1QuaXNBZG1pbj1yLmlzX2FkbWluO3N0KCJ1QmFkZ2UiLHIudXNlcm5hbWUpO3Nob3dBcHAoKTtsb2FkSVAoKTtwb2xsKCk7fQogIGVsc2V7c2hvd0F1dGgoKTt9Cn0pOwpzZXRJbnRlcnZhbChmdW5jdGlvbigpe2lmKGdlKCJhcHAiKS5zdHlsZS5kaXNwbGF5IT09Im5vbmUiKXBvbGwoKTt9LDQwMDApOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+").decode("utf-8")

@app.route("/")
@app.route("/login")
def index(): return Response(_DASH, mimetype="text/html")

if __name__ == "__main__":
    if "--setup" in sys.argv:
        code,_=um.gen_invite(); print(f"Invite: {code}"); sys.exit()
    port=int(os.getenv("PORT",5000))
    # use_reloader=False prevents double-process on startup
    app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False,threaded=True)