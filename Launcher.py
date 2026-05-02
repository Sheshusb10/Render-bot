#!/usr/bin/env python3
"""
ALPHA BOT LAUNCHER — permanent self-healing process
Monitors server.py and restarts it automatically.
Handles port conflicts, crashes, and updates.
You NEVER need to SSH again.
"""
import os,sys,time,subprocess,requests,logging,json,shutil,signal

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s LAUNCHER %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/alphabot/launcher.log")),
        logging.StreamHandler()
    ])
log=logging.getLogger("launcher")

DIR    = os.path.expanduser("~/alphabot")
DATA   = os.path.join(DIR,"data")
BOT    = os.path.join(DIR,"server.py")
BOTLOG = os.path.join(DIR,"bot.log")
USERS  = os.path.join(DATA,"users.json")
BACKUP = os.path.join(DIR,"users_backup.json")
GITHUB = "https://raw.githubusercontent.com/Sheshusb10/Render-bot/main/server.py"
PORT   = 5000

def protect_users():
    """Users are NEVER deleted. Sync primary ↔ backup."""
    os.makedirs(DATA,exist_ok=True)
    if os.path.exists(USERS):
        try:
            d=json.load(open(USERS))
            if d.get("users"): shutil.copy2(USERS,BACKUP)
        except: pass
    elif os.path.exists(BACKUP):
        try: shutil.copy2(BACKUP,USERS); log.info("Users restored from backup")
        except: pass

def kill_port():
    """Kill any process holding port 5000."""
    try:
        r=subprocess.run(["lsof","-ti",f"tcp:{PORT}"],
            capture_output=True,text=True,timeout=5)
        for pid in r.stdout.strip().split():
            try: os.kill(int(pid),signal.SIGTERM)
            except: pass
        time.sleep(2)
    except: pass

def download():
    try:
        r=requests.get(GITHUB,timeout=30)
        if r.status_code==200 and len(r.text)>10000:
            open(BOT,"w").write(r.text); log.info(f"Downloaded {len(r.text)//1024}KB")
            return True
    except Exception as e: log.warning(f"Download: {e}")
    return False

def verify():
    try:
        import ast
        ast.parse(open(BOT).read()); return True
    except Exception as e: log.error(f"Verify: {e}"); return False

def start():
    kill_port()
    protect_users()
    log.info("Starting bot...")
    with open(BOTLOG,"a") as lf:
        p=subprocess.Popen([sys.executable,BOT],
            stdout=lf,stderr=lf,cwd=DIR)
    log.info(f"Bot PID={p.pid}"); return p

def alive():
    try:
        r=requests.get(f"http://localhost:{PORT}/api/ip",timeout=3)
        return r.status_code in (200,401)
    except: return False

def main():
    log.info("="*40)
    log.info("Alpha Bot Launcher started")
    log.info("="*40)
    os.makedirs(DIR,exist_ok=True)
    os.makedirs(DATA,exist_ok=True)
    protect_users()

    if not verify():
        log.info("Bad bot — downloading"); download()

    proc=None; crashes=[]; tick=0

    while True:
        try:
            protect_users(); tick+=1

            # Periodic update check every 30 min
            if tick%60==0:
                if download() and verify():
                    if proc and proc.poll() is None:
                        log.info("Update downloaded — restarting")
                        proc.terminate(); time.sleep(3); proc=None

            needs=proc is None or proc.poll() is not None

            if needs:
                if proc:
                    try: proc.terminate()
                    except: pass

                # Too many crashes → download fresh
                now=time.time()
                crashes=[t for t in crashes if now-t<300]
                if len(crashes)>=3:
                    log.warning("3 crashes — downloading fresh copy")
                    if download(): crashes=[]
                    else: time.sleep(60); continue

                if not verify():
                    if not download() or not verify():
                        time.sleep(30); continue

                proc=start(); time.sleep(15)

                if proc.poll() is not None:
                    log.warning(f"Crashed on start (code={proc.poll()})")
                    crashes.append(time.time()); proc=None
                else:
                    log.info("Bot running ✓"); crashes=[]

            time.sleep(30)

            if proc and proc.poll() is not None:
                log.warning(f"Bot died (code={proc.poll()})")
                crashes.append(time.time()); proc=None

        except KeyboardInterrupt:
            log.info("Stopping...")
            if proc and proc.poll() is None: proc.terminate()
            break
        except Exception as e:
            log.error(f"Launcher error: {e}"); time.sleep(10)

if __name__=="__main__": main()