#!/usr/bin/env python3
"""
ALPHA BOT LAUNCHER v2 — self-healing, never deletes users
"""
import os,sys,time,subprocess,requests,logging,json,shutil

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s LAUNCHER %(message)s",
    handlers=[logging.FileHandler(os.path.expanduser("~/alphabot/launcher.log")),
              logging.StreamHandler()])
log=logging.getLogger("launcher")

DIR=os.path.expanduser("~/alphabot")
DATA=os.path.join(DIR,"data")
BOT=os.path.join(DIR,"server.py")
BOT_LOG=os.path.join(DIR,"bot.log")
USERS=os.path.join(DATA,"users.json")
BACKUP=os.path.join(DIR,"users_backup.json")
GITHUB="https://raw.githubusercontent.com/Sheshusb10/Render-bot/main/server.py"
PORT=5000; MAX_CRASHES=3; CRASH_WINDOW=300

def protect_users():
    """IRON RULE: users never deleted. Always backup."""
    os.makedirs(DATA,exist_ok=True)
    if os.path.exists(USERS):
        try:
            d=json.load(open(USERS))
            if d.get("users"):
                shutil.copy2(USERS,BACKUP)
        except: pass
    elif os.path.exists(BACKUP):
        try:
            shutil.copy2(BACKUP,USERS)
            log.info("Users restored from backup")
        except: pass

def free_port():
    try: subprocess.run(["fuser","-k",f"{PORT}/tcp"],capture_output=True,timeout=5)
    except: pass
    time.sleep(2)

def download():
    try:
        log.info("Downloading server.py from GitHub...")
        r=requests.get(GITHUB,timeout=30)
        if r.status_code==200 and len(r.text)>10000:
            open(BOT,"w").write(r.text)
            log.info(f"Downloaded {len(r.text)//1024}KB")
            return True
        log.warning(f"Bad response {r.status_code}")
        return False
    except Exception as e:
        log.warning(f"Download failed: {e}"); return False

def verify():
    try:
        import ast
        ast.parse(open(BOT).read()); return True
    except SyntaxError as e:
        log.error(f"Syntax error line {e.lineno}: {e.msg}"); return False
    except: log.error("server.py missing"); return False

def start():
    free_port(); protect_users()
    log.info("Starting bot...")
    with open(BOT_LOG,"a") as lf:
        proc=subprocess.Popen([sys.executable,BOT],stdout=lf,stderr=lf,cwd=DIR)
    log.info(f"Bot PID={proc.pid}"); return proc

def main():
    log.info("="*50)
    log.info("Alpha Bot Launcher started")
    log.info("="*50)
    os.makedirs(DIR,exist_ok=True); os.makedirs(DATA,exist_ok=True)
    protect_users()
    if not verify():
        log.info("Bot invalid — downloading"); download()
    crashes=[]; proc=None; tick=0
    while True:
        try:
            protect_users(); tick+=1
            # Every 30min check for updates
            if tick%60==0:
                if download():
                    if proc and proc.poll() is None:
                        log.info("New code — restarting"); proc.terminate(); time.sleep(3); proc=None
            needs=proc is None or proc.poll() is not None
            if needs:
                if proc and proc.poll() is None:
                    try: proc.terminate()
                    except: pass
                now=time.time(); crashes=[t for t in crashes if now-t<CRASH_WINDOW]
                if len(crashes)>=MAX_CRASHES:
                    log.warning(f"{len(crashes)} crashes — downloading fresh")
                    if download(): crashes=[]
                    else: time.sleep(60); continue
                if not verify():
                    if not download() or not verify():
                        time.sleep(30); continue
                proc=start(); time.sleep(15)
                if proc.poll() is not None:
                    log.warning("Crashed on start"); crashes.append(time.time()); proc=None
                else:
                    log.info("Bot running ✓"); crashes=[]
            time.sleep(30)
            if proc and proc.poll() is not None:
                log.warning(f"Bot exited={proc.poll()}"); crashes.append(time.time()); proc=None
        except KeyboardInterrupt:
            log.info("Launcher stopped")
            if proc and proc.poll() is None: proc.terminate()
            break
        except Exception as e:
            log.error(f"Launcher error: {e}"); time.sleep(10)

if __name__=="__main__": main()