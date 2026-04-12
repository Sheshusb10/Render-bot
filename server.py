# ============================================
# ΔLPHA PRO — CONTINUOUS EXECUTION BOT (FINAL)
# ============================================

import time, requests

BASE_URL = "https://api.india.delta.exchange"

bot_state = {
    "trail_state": {},
    "stats": {
        "current_balance": 70,
        "trades_today": 0
    }
}

# ============================================
# LOGGER
# ============================================
def blog(msg, level="info"):
    print(f"[{level.upper()}] {msg}")

# ============================================
# API
# ============================================
def pub_get(path):
    return requests.get(BASE_URL + path).json()

def dx_post(path, body):
    return requests.post(BASE_URL + path, json=body).json()

# ============================================
# LIQUIDITY FILTER
# ============================================
def filter_liquid_options(options):
    liquid = []
    for p in options:
        vol = float(p.get("volume", 0) or 0)
        oi  = float(p.get("open_interest", 0) or 0)

        if vol > 1000 or oi > 500:
            liquid.append(p)

    return liquid

# ============================================
# SMART STRIKE SELECTION
# ============================================
def select_best_option(options, price, direction):

    def score(p):
        try:
            strike = float(p["symbol"].split("-")[2])

            if direction == "BUY_CALL":
                diff = strike - price
            else:
                diff = price - strike

            if 0 < diff <= price * 0.02:
                return (0, diff)
            elif diff > 0:
                return (1, diff)
            else:
                return (2, abs(diff))
        except:
            return (3, 999999)

    options.sort(key=score)
    return options[0]

# ============================================
# POSITION SIZE
# ============================================
def get_position_size(balance, confidence):
    base = 0.02

    if confidence >= 90:
        mult = 2
    elif confidence >= 80:
        mult = 1.5
    elif confidence >= 70:
        mult = 1.2
    else:
        mult = 0.7

    return balance * base * mult

# ============================================
# MICRO TRAILING + STOP LOSS
# ============================================
def micro_manage(sym, entry, mark):

    pnl = ((mark - entry) / entry * 100)

    trail = bot_state["trail_state"].get(sym, {"peak": 0, "floor": None})

    if pnl > trail["peak"]:
        trail["peak"] = pnl

    # PROFIT FLOORING
    if pnl > 1: trail["floor"] = 0.5
    if pnl > 2: trail["floor"] = 1
    if pnl > 3: trail["floor"] = 2
    if pnl > 5: trail["floor"] = 3
    if pnl > 8: trail["floor"] = 5

    bot_state["trail_state"][sym] = trail

    # FAST LOSS EXIT
    if pnl <= -4:
        return True, f"Stop loss {pnl:.1f}%"

    # PROFIT LOCK EXIT
    if trail["floor"] and pnl < trail["floor"]:
        return True, f"Locked profit {trail['floor']}%"

    return False, ""

# ============================================
# QUICK FLOOR/CEILING TRADES
# ============================================
def quick_trade(price, support, resistance):

    if (price - support)/price*100 < 0.6:
        return "BUY_CALL", 68

    if (resistance - price)/price*100 < 0.6:
        return "BUY_PUT", 68

    return None, 0

# ============================================
# EXECUTE TRADE
# ============================================
def execute_trade(asset, direction, price, confidence):

    if confidence < 65:
        return

    products = pub_get("/v2/products?states=live&page_size=500")["result"]

    opt_type = "call_options" if direction == "BUY_CALL" else "put_options"

    options = [p for p in products
               if p["contract_type"] == opt_type
               and asset in p["symbol"]]

    options = filter_liquid_options(options)

    if not options:
        blog(f"{asset} no liquidity", "warning")
        return

    product = select_best_option(options, price, direction)

    balance = bot_state["stats"]["current_balance"]
    risk = get_position_size(balance, confidence)

    size = max(1, min(3, int(risk / (price * 0.015))))

    blog(f"{asset} {direction} size {size} conf {confidence}")

    dx_post("/v2/orders", {
        "product_id": product["id"],
        "size": size,
        "side": "buy",
        "order_type": "market_order"
    })

# ============================================
# POSITION MANAGER
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

        should_close, reason = micro_manage(sym, entry, mark)

        if should_close:
            blog(f"{sym} closing {reason}")

            dx_post("/v2/orders", {
                "product_symbol": sym,
                "size": int(abs(size)),
                "side": "sell" if size > 0 else "buy",
                "order_type": "market_order",
                "reduce_only": "true"
            })

# ============================================
# MOCK ANALYSIS (REPLACE WITH YOUR ENGINE)
# ============================================
def fake_analysis():
    return {
        "asset": "BTC",
        "price": 70000,
        "support": 69500,
        "resistance": 70500,
        "direction": "BUY_PUT",
        "confidence": 82
    }

# ============================================
# MAIN LOOP
# ============================================
def run():

    while True:
        try:
            a = fake_analysis()

            # QUICK TRADE
            qt_dir, qt_conf = quick_trade(
                a["price"], a["support"], a["resistance"]
            )

            if qt_dir:
                execute_trade(a["asset"], qt_dir, a["price"], qt_conf)

            # SWING TRADE
            execute_trade(
                a["asset"],
                a["direction"],
                a["price"],
                a["confidence"]
            )

            # MANAGE POSITIONS
            manage_positions()

        except Exception as e:
            blog(str(e), "error")

        time.sleep(30)  # FAST LOOP

# ============================================
# START
# ============================================
if __name__ == "__main__":
    run()