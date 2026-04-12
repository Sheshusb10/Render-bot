# ============================================
# ΔLPHA PRO — STABLE COMPUNDING BOT (404 FIX)
# ============================================

import time, threading, requests
from flask import Flask, jsonify,send_from_directory

app = Flask(__name__)

BASE_URL = "https://api.india.delta.exchange"

bot_running = False
bot_thread = None

bot_state = {
    "trail": {},
    "balance": 70,
    "last_exit": None
}

# ============================================
# LOGGER
# ============================================
def log(msg):
    print(f"[BOT] {msg}")

# ============================================
# MARKET DATA
# ============================================
def get_candles():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=50",
            timeout=5
        )
        return r.json()
    except:
        return []

def calc_rsi(closes):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < 14:
        return 50
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ============================================
# ANALYSIS
# ============================================
def analysis():
    candles = get_candles()
    if not candles:
        return None

    closes = [float(c[4]) for c in candles]

    price = closes[-1]
    rsi = calc_rsi(closes)
    momentum = closes[-1] - closes[-5]

    highs = [float(c[2]) for c in candles[-20:]]
    lows  = [float(c[3]) for c in candles[-20:]]

    support = min(lows)
    resistance = max(highs)

    if momentum > 40:
        return ("BTC", "BUY_CALL", price, 80, support, resistance)
    elif momentum < -40:
        return ("BTC", "BUY_PUT", price, 80, support, resistance)
    elif rsi < 35:
        return ("BTC", "BUY_CALL", price, 70, support, resistance)
    elif rsi > 65:
        return ("BTC", "BUY_PUT", price, 70, support, resistance)

    return ("BTC", "NO", price, 0, support, resistance)

# ============================================
# DELTA API
# ============================================
def pub_get(path):
    try:
        return requests.get(BASE_URL + path, timeout=5).json()
    except:
        return {}

def dx_post(path, body):
    try:
        return requests.post(BASE_URL + path, json=body, timeout=5).json()
    except:
        return {}

# ============================================
# OPTION LOGIC
# ============================================
def get_option(asset, direction, price):
    products = pub_get("/v2/products?states=live&page_size=500").get("result", [])

    opt_type = "call_options" if direction == "BUY_CALL" else "put_options"

    options = [
        p for p in products
        if p.get("contract_type") == opt_type
        and asset in p.get("symbol","")
        and float(p.get("volume",0)) > 1000
    ]

    if not options:
        return None

    return min(options, key=lambda p: abs(float(p["symbol"].split("-")[2]) - price))

def get_size():
    return 1

# ============================================
# CLOSE + COMPOUND MEMORY
# ============================================
def close(sym, size, pnl):
    log(f"Closing {sym} {pnl:.1f}%")

    dx_post("/v2/orders", {
        "product_symbol": sym,
        "size": int(abs(size)),
        "side": "sell" if size > 0 else "buy",
        "order_type": "market_order",
        "reduce_only": "true"
    })

    bot_state["last_exit"] = time.time()

# ============================================
# POSITION MANAGEMENT
# ============================================
def manage_positions():
    data = pub_get("/v2/positions/margined").get("result", [])

    for p in data:
        size = float(p.get("size", 0))
        if abs(size) == 0:
            continue

        sym = p["product_symbol"]
        entry = float(p["entry_price"])
        mark = float(p["mark_price"])

        pnl = (mark - entry) / entry * 100

        trail = bot_state["trail"].get(sym, {"peak": 0})

        if pnl > trail["peak"]:
            trail["peak"] = pnl

        bot_state["trail"][sym] = trail

        peak = trail["peak"]
        drawdown = peak - pnl

        if peak > 2 and drawdown > 1:
            close(sym, size, pnl)

        elif pnl < -3:
            close(sym, size, pnl)

# ============================================
# EXECUTE
# ============================================
def execute(asset, direction, price):
    if direction == "NO":
        return

    option = get_option(asset, direction, price)
    if not option:
        return

    log(f"{asset} {direction}")

    dx_post("/v2/orders", {
        "product_id": option["id"],
        "size": get_size(),
        "side": "buy",
        "order_type": "market_order"
    })

# ============================================
# COMPOUNDING
# ============================================
def compound():
    last = bot_state["last_exit"]
    if not last:
        return

    if time.time() - last < 30:
        a = analysis()
        if a and a[1] != "NO":
            log("🔥 RE-ENTRY")
            execute(a[0], a[1], a[2])
            bot_state["last_exit"] = None

# ============================================
# BOT LOOP
# ============================================
def bot_loop():
    global bot_running

    while bot_running:
        try:
            a = analysis()
            if a:
                execute(a[0], a[1], a[2])

            manage_positions()
            compound()

        except Exception as e:
            log(str(e))

        time.sleep(10)

# ============================================
# API ROUTES
# ============================================
from flask import render_template

from flask import send_from_directory

@app.route("/")
def home():
    return send_from_directory("static", "index.html")

@app.route("/api/start")
def start():
    global bot_running, bot_thread

    if not bot_running:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()

    return jsonify({"status": "started"})

@app.route("/api/stop")
def stop():
    global bot_running
    bot_running = False
    return jsonify({"status": "stopped"})

@app.route("/api/status")
def status():
    return jsonify({"running": bot_running})

# ============================================
# ✅ FIXED ROUTES (NO MORE 404)
# ============================================
@app.route("/api/bot/status")
def bot_status():
    return jsonify({"running": bot_running})

@app.route("/api/orders")
def orders():
    return jsonify(pub_get("/v2/orders"))

@app.route("/api/positions")
def positions():
    return jsonify(pub_get("/v2/positions/margined"))

@app.route("/api/analysis")
def analysis_route():
    a = analysis()
    if not a:
        return jsonify({"status": "no data"})

    return jsonify({
        "asset": a[0],
        "direction": a[1],
        "price": a[2],
        "confidence": a[3]
    })
# ============================================
# AUTO START BOT (FIX)
# ============================================
def auto_start():
    global bot_running, bot_thread
    if not bot_running:
        bot_running = True
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
        log("🔥 Bot auto-started")

auto_start()
# ============================================
# RUN
# ============================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)