# ΔLPHA — Quant Options & Futures Bot
## Delta Exchange · Wall Street Quant Engine

---

## ⚡ QUICK START (3 steps)

```bash
# 1. Install dependencies
pip install flask flask-cors requests

# 2. Start the backend
python server.py

# 3. Open your browser
open http://localhost:5000
```

Then enter your Delta Exchange API key + secret and connect.

---

## 📁 FILE STRUCTURE

```
alpha_bot/
├── server.py          ← Flask backend (handles API auth, bot logic)
├── requirements.txt   ← Python dependencies
└── static/
    └── index.html     ← React dashboard (served by Flask)
```

---

## 🧠 STRATEGIES

| Strategy | Description |
|---|---|
| ⚡ Quant Auto | Full regime detection → signal generation → live order execution with SL/TP |
| 📈 ATM Calls | Buy at-the-money calls on BTC/ETH/SOL |
| 💰 OTM Puts | Sell 5%-OTM puts for premium income |
| ⚖️ Strangle | Sell OTM call + put (bet on time decay) |
| 🖱️ Manual | Dashboard only — place trades yourself |

---

## 🛡️ RISK CONTROLS (built-in)

- Max **1% capital per trade**
- Max **5% total exposure**
- **Daily loss limit: 3%** → bot halts automatically
- **2 consecutive losses** → size warning
- Every trade gets **automatic SL + TP1**

---

## 🔑 API KEY SETUP (Delta Exchange)

1. Log into Delta Exchange → Account → API Keys
2. Create key with **Trading** permission
3. Whitelist your IP if required
4. Copy key + secret into the dashboard

**India users:** Select "India" region in the login screen.
- Global: `https://api.delta.exchange`
- India:  `https://api.india.delta.exchange`

---

## ⚠️ IMPORTANT

- This bot places **real orders with real money**
- Test on **testnet** first: `https://testnet.delta.exchange`
  - Change `BASE_URL` in `server.py` to `https://testnet-api.delta.exchange`
- Never share your API secret
- Bot runs locally — keys never leave your machine

---

## 🔄 ENDPOINTS (for advanced users)

```
GET  /api/health          — server health check
POST /api/connect         — authenticate with Delta Exchange
GET  /api/analysis        — live regime + signal for BTC/ETH/SOL
GET  /api/products        — all listed options contracts
GET  /api/tickers         — live market data
GET  /api/positions       — open positions
GET  /api/orders          — open orders
POST /api/orders          — place order
DEL  /api/orders/<id>     — cancel order
POST /api/bot/start       — start auto-trading
POST /api/bot/stop        — stop auto-trading
POST /api/bot/run_now     — run one cycle immediately
GET  /api/bot/status      — bot status + activity log
POST /api/bot/reset_daily — reset daily risk counters
```
