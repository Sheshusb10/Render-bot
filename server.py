# ============================================
# ΔLPHA PRO — FINAL SERVER + EXECUTION ENGINE
# ============================================

import time, threading, requests
from flask import Flask, jsonify

app = Flask(__name__)

BASE_URL = "https://api.india.delta.exchange"

bot_running = False

bot_state = {
    "trail": {},
    "balance": 70,
    "positions": []
}

# ============================================
# API HELPERS
# ============================================
def pub_get(path):
    try:
        return requests.get(BASE_URL + path).json()
    except:
        return {}

def dx_post(path, body):
    try:
        return requests.post(BASE_URL + path, json=body).json()
    except:
        return {}

# ============================================
# LOGGER
# ============================================
def log(msg):
    print(f"[BOT] {msg}")

# ============================================
# LIQUIDITY FILTER
# ============================================
def filter_liquid(options):
    return [
        p for p in options
        if float(p.get("volume", 0) or 0) > 1000
        or float(p.get("open_interest", 0) or 0) > 500
    ]

# ============================================
# STRIKE SELECTION
# ============================================
def select_option(options, price, direction):
    def score(p):
        try:
            strike = float(p["symbol"].split("-")[2])
            diff = abs(strike - price)
            return diff
        except:
            return 999999

    options.sort(key=score)
    return options[0] if options else None

# ============================================
# POSITION SIZE
# ============================================
def get_size(balance, confidence):
    if confidence >= 90:
        mult = 2
    elif confidence >= 80:
        mult = 1.5
    else:
        mult = 1

    return max(1, int(balance * 0.02 * mult / (70000 * 0.015)))

# ============================================
# MICRO TRAILING
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

        trail = bot_state["trail"].get(sym, {"peak": 0, "floor": None})

        if pnl > trail["peak"]:
            trail["peak"] = pnl

        # PROFIT FLOOR
        if pnl > 1: trail["floor"] = 0.5
        if pnl > 2: trail["floor"] = 1
        if pnl > 3: trail["floor"] = 2
        if pnl > 5: trail["floor"] = 3

        bot_state["trail"][sym] = trail

        # EXIT CONDITIONS
        if pnl <= -4 or (trail["floor"] and pnl < trail["floor"]):
            log(f"Closing {sym} pnl {pnl:.1f}%")

            dx_post("/v2/orders", {
                "product_symbol": sym,
                "size": int(abs(size)),
                "side": "sell" if size > 0 else "buy",
                "order_type": "market_order",
                "reduce_only": "true"
            })

# ============================================
# QUICK TRADE LOGIC
# ============================================
def quick_trade(price, support, resistance):

    if (price - support)/price*100 < 0.6:
        return "BUY_CALL", 70

    if (resistance - price)/price*100 < 0.6:
        return "BUY_PUT", 70

    return None, 0

# ============================================
# EXECUTE TRADE
# ============================================
def execute(asset, direction, price, confidence):

    if confidence < 65:
        return

    products = pub_get("/v2/products?states=live&page_size=500").get("result", [])

    opt_type = "call_options" if direction == "BUY_CALL" else "put_options"

    options = [
        p for p in products
        if p["contract_type"] == opt_type
        and asset in p["symbol"]
    ]

    options = filter_liquid(options)
    if not options:
        return

    product = select_option(options, price, direction)
    size = get_size(bot_state["balance"], confidence)

    log(f"{asset} {direction} size {size}")

    dx_post("/v2/orders", {
        "product_id": product["id"],
        "size": size,
        "side": "buy",
        "order_type": "market_order"
    })

# ============================================
# MOCK ANALYSIS (REPLACE WITH YOUR REAL ONE)
# ============================================
def analysis():
    return {
        "asset": "BTC",
        "price": 70000,
        "support": 69500,
        "resistance": 70500,
        "direction": "BUY_PUT",
        "confidence": 80
    }

# ============================================
# MAIN BOT LOOP (KEY FIX)
# ============================================
def bot_loop():
    global bot_running

    while bot_running:
        try:
            a = analysis()

            # QUICK TRADE
            d, c = quick_trade(a["price"], a["support"], a["resistance"])
            if d:
                execute(a["asset"], d, a["price"], c)

            # MAIN TRADE
            execute(a["asset"], a["direction"], a["price"], a["confidence"])

            # MANAGE POSITIONS
            manage_positions()

        except Exception as e:
            log(str(e))

        time.sleep(20)  # FAST LOOP

# ============================================
# API ROUTES
# ============================================
@app.route("/api/start")
def start():
    global bot_running
    if not bot_running:
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
# RUN SERVER
# ============================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)