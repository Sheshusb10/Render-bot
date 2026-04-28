"""
WEALTH BUILDER BOT - PRODUCTION VERSION
Single File Deployment (For Render)

This is ONE file with EVERYTHING:
- Smart exit logic (TP1, TP2, hard stops)
- Professional position sizing (Kelly + streaks + regime)
- Main bot logic
- Flask API for monitoring
- Dashboard

DEPLOYMENT:
1. Replace your old server.py with this file
2. Git commit and push
3. Render auto-deploys
4. Done - bot runs with all upgrades

NO CHANGES TO YOUR WORKFLOW
"""

import os
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional


# ════════════════════════════════════════════════════════════════════════════
# 1. SMART EXIT ENGINE (Built-in)
# ════════════════════════════════════════════════════════════════════════════

class SmartExitEngine:
    """When to exit trades"""
    
    def __init__(self):
        self.active_trades = {}
        self.max_loss_pct = 0.03
        self.first_tp_pct = 0.015
        self.second_tp_pct = 0.025
    
    def should_exit(self, trade_id, current_price, entry_price, direction):
        """Check if trade should exit"""
        
        if direction == "CALL":
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - current_price) / entry_price) * 100
        
        # Hard stop (-3%)
        if pnl_pct <= -self.max_loss_pct * 100:
            return True, "HARD_STOP"
        
        # TP1 (1.5%)
        if pnl_pct >= self.first_tp_pct * 100:
            return True, "TP1"
        
        # TP2 (2.5%)
        if pnl_pct >= self.second_tp_pct * 100:
            return True, "TP2"
        
        return False, ""


# ════════════════════════════════════════════════════════════════════════════
# 2. PROFESSIONAL POSITION SIZER (Built-in)
# ════════════════════════════════════════════════════════════════════════════

class ProfessionalPositionSizer:
    """How much to risk per trade"""
    
    def __init__(self):
        self.win_rate = 0.55
        self.avg_win_pct = 2.0
        self.avg_loss_pct = 1.2
        self.current_streak = 0
        self.min_kelly = 0.005
        self.max_kelly = 0.03
    
    def calculate_kelly(self):
        """Kelly Criterion optimal sizing"""
        if self.avg_loss_pct <= 0:
            return 0.01
        
        p = self.win_rate
        q = 1 - p
        b = self.avg_win_pct / self.avg_loss_pct
        kelly = (b * p - q) / b
        kelly_safe = kelly * 0.25  # Use 25% of Kelly
        
        return max(self.min_kelly, min(self.max_kelly, kelly_safe))
    
    def get_streak_multiplier(self):
        """Scale based on winning/losing streak"""
        if self.current_streak >= 3:
            return 1.4
        elif self.current_streak == 2:
            return 1.2
        elif self.current_streak == 1:
            return 1.0
        elif self.current_streak == 0:
            return 1.0
        elif self.current_streak == -1:
            return 0.85
        else:
            return 0.7
    
    def get_regime_multiplier(self, regime, direction):
        """Scale based on market regime"""
        if regime == "STRONG_UPTREND" and direction == "CALL":
            return 1.3
        elif regime == "STRONG_DOWNTREND" and direction == "PUT":
            return 1.3
        elif regime == "CONSOLIDATION":
            return 0.0
        elif regime == "WEAK_UPTREND" and direction == "CALL":
            return 1.0
        elif regime == "WEAK_DOWNTREND" and direction == "PUT":
            return 1.0
        else:
            return 0.5
    
    def calculate_position_size(self, account, regime, direction, volatility):
        """Final position size"""
        kelly = self.calculate_kelly()
        base = account * kelly
        
        streak_mult = self.get_streak_multiplier()
        regime_mult = self.get_regime_multiplier(regime, direction)
        
        # Volatility adjustment
        if volatility < 1.0:
            vol_mult = 1.5
        elif volatility < 1.5:
            vol_mult = 1.2
        elif volatility < 2.5:
            vol_mult = 1.0
        elif volatility < 3.5:
            vol_mult = 0.8
        else:
            vol_mult = 0.5
        
        final_size = base * streak_mult * regime_mult * vol_mult
        final_size = max(account * 0.005, min(account * 0.03, final_size))
        
        return final_size
    
    def update_streak(self, pnl):
        """Update streak after trade"""
        if pnl > 0:
            if self.current_streak < 0:
                self.current_streak = 1
            else:
                self.current_streak += 1
        else:
            if self.current_streak > 0:
                self.current_streak = -1
            else:
                self.current_streak -= 1


# ════════════════════════════════════════════════════════════════════════════
# 3. MAIN WEALTH BUILDER BOT
# ════════════════════════════════════════════════════════════════════════════

class WealthBuilderBot:
    """Complete trading bot"""
    
    def __init__(self, starting_balance=10000):
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
        self.peak_balance = starting_balance
        
        self.exit_engine = SmartExitEngine()
        self.position_sizer = ProfessionalPositionSizer()
        
        self.open_trades = {}
        self.closed_trades = []
        self.trade_counter = 0
        self.equity_curve = []
        self.max_drawdown = 0.0
    
    def execute_trade(self, direction, entry_price, regime, confidence, volatility, atr):
        """Execute a trade"""
        
        # Calculate position size
        position_size = self.position_sizer.calculate_position_size(
            self.current_balance, regime, direction, volatility
        )
        
        if position_size < self.current_balance * 0.005:
            return None  # Position too small
        
        self.trade_counter += 1
        trade = {
            "trade_id": self.trade_counter,
            "entry_price": entry_price,
            "direction": direction,
            "position_size": position_size,
            "entry_time": datetime.utcnow()
        }
        
        self.open_trades[self.trade_counter] = trade
        return self.trade_counter
    
    def close_trade(self, trade_id, exit_price, reason):
        """Close a trade"""
        
        if trade_id not in self.open_trades:
            return None
        
        trade = self.open_trades[trade_id]
        entry_price = trade["entry_price"]
        position_size = trade["position_size"]
        direction = trade["direction"]
        
        if direction == "CALL":
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100
        
        pnl_dollars = position_size * (pnl_pct / 100)
        
        # Update balance
        self.current_balance += pnl_dollars
        
        # Update peak and drawdown
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
        
        current_dd = (self.peak_balance - self.current_balance) / self.peak_balance
        self.max_drawdown = max(self.max_drawdown, current_dd)
        
        # Update streak
        self.position_sizer.update_streak(pnl_dollars)
        
        # Log trade
        closed_trade = {
            "trade_id": trade_id,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pct": pnl_pct,
            "pnl_dollars": pnl_dollars,
            "reason": reason,
            "balance_after": self.current_balance
        }
        
        self.closed_trades.append(closed_trade)
        self.equity_curve.append((datetime.utcnow().isoformat(), self.current_balance))
        
        del self.open_trades[trade_id]
        
        return closed_trade
    
    def get_dashboard(self):
        """Performance metrics"""
        
        if not self.closed_trades:
            return {
                "status": "No trades yet",
                "balance": self.current_balance
            }
        
        wins = [t for t in self.closed_trades if t["pnl_dollars"] > 0]
        losses = [t for t in self.closed_trades if t["pnl_dollars"] < 0]
        
        total_profit = sum(t["pnl_dollars"] for t in wins)
        total_loss = abs(sum(t["pnl_dollars"] for t in losses))
        
        gain = self.current_balance - self.starting_balance
        gain_pct = (gain / self.starting_balance) * 100
        
        return {
            "status": "ACTIVE",
            "balance": self.current_balance,
            "starting": self.starting_balance,
            "gain": gain,
            "gain_pct": gain_pct,
            "trades": len(self.closed_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(self.closed_trades) if self.closed_trades else 0,
            "profit_factor": total_profit / total_loss if total_loss > 0 else 0,
            "drawdown": self.max_drawdown,
            "streak": self.position_sizer.current_streak
        }


# ════════════════════════════════════════════════════════════════════════════
# 4. FLASK API (Your old interface)
# ════════════════════════════════════════════════════════════════════════════

from flask import Flask, jsonify, request

app = Flask(__name__)
bot = WealthBuilderBot(10000)

@app.route('/api/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({"status": "healthy", "timestamp": datetime.utcnow().isoformat()})

@app.route('/api/dashboard', methods=['GET'])
def dashboard():
    """Get dashboard data"""
    dash = bot.get_dashboard()
    return jsonify(dash)

@app.route('/api/execute', methods=['POST'])
def execute():
    """Execute a trade"""
    data = request.json
    
    trade_id = bot.execute_trade(
        direction=data.get("direction"),
        entry_price=data.get("price"),
        regime=data.get("regime"),
        confidence=data.get("confidence"),
        volatility=data.get("volatility"),
        atr=data.get("atr")
    )
    
    if trade_id:
        return jsonify({"status": "executed", "trade_id": trade_id})
    else:
        return jsonify({"status": "rejected", "reason": "position_too_small"})

@app.route('/api/trades/<int:trade_id>/close', methods=['POST'])
def close_trade(trade_id):
    """Close a trade"""
    data = request.json
    
    result = bot.close_trade(
        trade_id=trade_id,
        exit_price=data.get("price"),
        reason=data.get("reason")
    )
    
    if result:
        return jsonify({"status": "closed", "pnl": result["pnl_dollars"]})
    else:
        return jsonify({"status": "error", "reason": "trade_not_found"})

@app.route('/api/status', methods=['GET'])
def status():
    """Get bot status"""
    return jsonify({
        "bot": "wealth_builder_v1",
        "balance": bot.current_balance,
        "trades_open": len(bot.open_trades),
        "trades_closed": len(bot.closed_trades),
        "timestamp": datetime.utcnow().isoformat()
    })


# ════════════════════════════════════════════════════════════════════════════
# 5. RENDER DEPLOYMENT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
