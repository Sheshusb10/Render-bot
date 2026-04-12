# ============================================
# ΔLPHA PRO — COMPOUNDING EXECUTION ENGINE
# ============================================

import time, threading, requests
from flask import Flask, jsonify

app = Flask(__name__)

BASE_URL = "https://api.india.delta.exchange"

bot_running = False

bot_state = {
    "trail": {},
    "balance": 70,
    "last_exit": None   # 🔥 for compounding
}

# ============================================
# LOGGER
# ============================================
def log(msg):
    print(f"[BOT] {msg}")

# ============================================
# MARKET DATA (BINANCE)
# ============================================
def get_candles():
    r = requests.get(
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=50"
    )
    return r.json()

def calc_rsi(closes):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ============================================
# ANALYSIS ENGINE
# ============================================
def analysis():
    candles = get_candles()
    closes = [float(c[4]) for c in candles]

    price = closes[-1]
    rsi = calc_rsi(closes)

    momentum = closes[-1] - closes[-5]

    highs = [float(c[2]) for c in candles[-20:]]
    lows  = [float(c[3]) for c in candles[-20:]]

    support = min(lows)
    resistance = max(highs)

    direction = "NO TRADE"
    confidence = 0

    if momentum > 50:
        direction = "BUY_CALL"
        confidence = 80
    elif momentum < -50:
        direction = "BUY_PUT"
        confidence = 80
    elif rsi < 35:
        direction = "BUY_CALL"
        confidence = 70
    elif rsi > 65:
        direction = "BUY_PUT"
        confidence = 70

    return {
        "asset": "BTC",
        "price": price,
        "support": support,
        "resistance": resistance,
        "direction": direction,
        "confidence": confidence
    }

# ============================================
# DELTA API
# ============================================
def pub_get(path):
    return requests.get(BASE_URL + path).json()

def dx_post(path, body):
    return requests.post(BASE_URL + path, json=body).json()

# ============================================
# OPTION LOGIC
# ============================================
def filter_liquid(options):
    return [p for p in options if float(p.get("volume",0)) > 1000]

def select_option(options, price):
    return min(options, key=lambda p: abs(float(p["symbol"].split("-")[2]) - price))

def get_size(balance):
    return max(1, int(balance * 0.02 / (70000 * 0.015)))

# ============================================
# CLOSE FUNCTION (WITH COMPOUND MEMORY)
# ============================================
def close(sym, size, pnl, reason):
    log(f"Closing {sym} {pnl:.1f}% ({reason})")

    dx_post("/v2/orders", {
        "product_symbol": sym,
        "size": int(abs(size)),
        "side": "sell" if size > 0 else "buy",
        "order_type": "market_order",
        "reduce_only": "true"
    })

    # 🔥 STORE LAST EXIT
    bot_state["last_exit"] = {
        "time": time.time(),
        "pnl": pnl
    }

# ============================================
# TRAILING + EXIT
# ============================================
def manage_positions():
    positions = pub_get("/v2/positions/margined").get("result", [])

    for p in positions:
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

        # 🔥 DYNAMIC EXIT
        if peak > 2 and drawdown > 0.8:
            close(sym, size, pnl, "tight lock")

        elif peak > 5 and drawdown > 1.5:
            close(sym, size, pnl, "profit lock")

        elif peak > 8 and drawdown > 2.5:
            close(sym, size, pnl, "trend exit")

        elif pnl < -3:
            close(sym, size, pnl, "stop loss")

# ============================================
# QUICK TRADE
# ============================================
def quick_trade(price, support, resistance):

    if (price - support)/price*100 < 0.4:
        return "BUY_CALL", 72

    if (resistance - price)/price*100 < 0.4:
        return "BUY_PUT", 72

    return None, 0

# ============================================
# EXECUTE
# ============================================
def execute(asset, direction, price, confidence):

    if direction == "NO TRADE":
        return

    products = pub_get("/v2/products?states=live&page_size=500")["result"]

    opt_type = "call_options" if direction == "BUY_CALL" else "put_options"

    options = [
        p for p in products
        if p["contract_type"] == opt_type
        and asset in p["symbol"]
    ]

    options = filter_liquid(options)
    if not options:
        return

    product = select_option(options, price)
    size = get_size(bot_state["balance"])

    log(f"{asset} {direction} size {size}")

    dx_post("/v2/orders", {
        "product_id": product["id"],
        "size": size,
        "side": "buy",
        "order_type": "market_order"
    })

# ============================================
# 🔥 COMPOUNDING ENGINE
# ============================================
def compounding_reentry():

    last = bot_state.get("last_exit")

    if not last:
        return

    # only re-enter within 60 sec of exit
    if time.time() - last["time"] > 60:
        return

    a = analysis()

    if a["direction"] != "NO TRADE" and a["confidence"] > 65:
        log("🔥 COMPOUND RE-ENTRY")
        execute(a["asset"], a["direction"], a["price"], a["confidence"])

        # reset so it doesn't spam
        bot_state["last_exit"] = None

# ============================================
# BOT LOOP
# ============================================
def bot_loop():
    global bot_running

    while bot_running:
        try:
            a = analysis()

            # QUICK
            d, c = quick_trade(a["price"], a["support"], a["resistance"])
            if d:
                execute(a["asset"], d, a["price"], c)

            # MAIN
            execute(a["asset"], a["direction"], a["price"], a["confidence"])

            manage_positions()

            # 🔥 COMPOUNDING
            compounding_reentry()

        except Exception as e:
            log(str(e))

        time.sleep(15)

# ============================================
# API
# ============================================
@app.route("/api/start")
def start():
    global bot_running
    bot_running = True
    threading.Thread(target=bot_loop).start()
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
# RUN
# ============================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)