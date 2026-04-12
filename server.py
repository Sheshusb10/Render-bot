# ============================================
# ΔLPHA PRO — REAL SIGNAL + TRUE TRAILING BOT
# ============================================

import time, threading, requests
from flask import Flask, jsonify

app = Flask(__name__)

BASE_URL = "https://api.india.delta.exchange"

bot_running = False

bot_state = {
    "trail": {},
    "balance": 70
}

# ============================================
# LOGGER
# ============================================
def log(msg):
    print(f"[BOT] {msg}")

# ============================================
# BINANCE DATA (REAL SIGNAL)
# ============================================
def get_price():
    r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    return float(r.json()["price"])

def get_candles():
    r = requests.get(
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=50"
    )
    return r.json()

# ============================================
# RSI
# ============================================
def calc_rsi(candles, period=14):
    closes = [float(c[4]) for c in candles]
    gains, losses = [], []

    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ============================================
# REAL ANALYSIS
# ============================================
def analysis():
    candles = get_candles()
    price = float(candles[-1][4])

    rsi = calc_rsi(candles)

    highs = [float(c[2]) for c in candles[-20:]]
    lows  = [float(c[3]) for c in candles[-20:]]

    support = min(lows)
    resistance = max(highs)

    direction = "NO TRADE"
    confidence = 0

    if rsi < 35:
        direction = "BUY_CALL"
        confidence = 75

    elif rsi > 65:
        direction = "BUY_PUT"
        confidence = 75

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
# LIQUIDITY FILTER
# ============================================
def filter_liquid(options):
    return [
        p for p in options
        if float(p.get("volume", 0) or 0) > 1000
        or float(p.get("open_interest", 0) or 0) > 500
    ]

# ============================================
# OPTION SELECT
# ============================================
def select_option(options, price):
    def score(p):
        try:
            strike = float(p["symbol"].split("-")[2])
            return abs(strike - price)
        except:
            return 999999

    options.sort(key=score)
    return options[0]

# ============================================
# POSITION SIZE
# ============================================
def get_size(balance, confidence):
    mult = 1.5 if confidence > 80 else 1
    return max(1, int(balance * 0.02 * mult / (70000 * 0.015)))

# ============================================
# TRUE TRAILING (KEY UPGRADE)
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

        # Track peak profit
        if pnl > trail["peak"]:
            trail["peak"] = pnl

        bot_state["trail"][sym] = trail

        drawdown = trail["peak"] - pnl

        # 🔥 PROFIT LOCK ON REVERSAL
        if trail["peak"] > 2 and drawdown > 1.5:
            close(sym, size, pnl, "profit reversal")

        # ❌ FAST LOSS
        elif pnl < -4:
            close(sym, size, pnl, "stop loss")

def close(sym, size, pnl, reason):
    log(f"Closing {sym} {pnl:.1f}% ({reason})")

    dx_post("/v2/orders", {
        "product_symbol": sym,
        "size": int(abs(size)),
        "side": "sell" if size > 0 else "buy",
        "order_type": "market_order",
        "reduce_only": "true"
    })

# ============================================
# QUICK TRADE
# ============================================
def quick_trade(price, support, resistance):

    if (price - support)/price*100 < 0.5:
        return "BUY_CALL", 70

    if (resistance - price)/price*100 < 0.5:
        return "BUY_PUT", 70

    return None, 0

# ============================================
# EXECUTE
# ============================================
def execute(asset, direction, price, confidence):

    if direction == "NO TRADE" or confidence < 65:
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

    size = get_size(bot_state["balance"], confidence)

    log(f"{asset} {direction} size {size}")

    dx_post("/v2/orders", {
        "product_id": product["id"],
        "size": size,
        "side": "buy",
        "order_type": "market_order"
    })

# ============================================
# BOT LOOP
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

            # MAIN SIGNAL
            execute(a["asset"], a["direction"], a["price"], a["confidence"])

            manage_positions()

        except Exception as e:
            log(str(e))

        time.sleep(20)

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