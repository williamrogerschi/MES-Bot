# ============================================================
# state.py — Position State Persistence
# Writes bot state to disk after every trade so the bot
# can shut down and restart without losing position context.
# ============================================================

import json
import os
import logging
from datetime import datetime
from config import STATE_FILE

logger = logging.getLogger(__name__)


def default_state():
    """
    Returns a clean slate state — used on first ever run
    or after a manual reset.
    """
    return {
        "is_active": False,             # Is the bot currently holding or managing a position?
        "grid_level": 0,                # How many times we've averaged down (0 = just initial buy)
        "total_qty": 0,                 # Total contracts currently held
        "buys": [],                     # List of individual buys: [{price, qty, timestamp}]
        "lowest_buy_price": None,       # Lowest price we bought at in current grid
        "average_cost": None,           # Weighted average cost of all current contracts
        "realized_pnl": 0.0,           # Total realized P&L across all closed trades (session)
        "profit_reserve": 0.0,          # Sequestered profit reserve (Phase 2)
        "last_action": None,            # Last action taken (for logging/debugging)
        "last_action_time": None,
        "last_sell_price": None,       # Price of last single-contract sell (re-entry trigger)
        "last_price": None,             # Last known price (for restart context)
        "week_start_balance": None,     # Account balance at Sunday open (for weekly tracking)
    }


def load_state():
    """
    Loads state from disk. If no state file exists, returns default state.
    Called on bot startup to resume after a restart.
    """
    if not os.path.exists(STATE_FILE):
        logger.info("No state file found — starting with clean state.")
        return default_state()

    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
        logger.info(f"State loaded from {STATE_FILE}: grid_level={state.get('grid_level')}, "
                    f"total_qty={state.get('total_qty')}, is_active={state.get('is_active')}")
        return state
    except Exception as e:
        logger.error(f"Failed to load state file: {e} — starting with clean state.")
        return default_state()


def save_state(state):
    """
    Saves state to disk. Called after every trade action.
    Uses a temp file + rename for atomic write (prevents corruption
    if the bot crashes mid-write).
    """
    state['last_saved'] = datetime.now().isoformat()
    temp_file = STATE_FILE + '.tmp'
    try:
        with open(temp_file, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(temp_file, STATE_FILE)
        logger.debug(f"State saved to {STATE_FILE}")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def reset_state():
    """
    Resets to a clean slate and saves it.
    Called after Friday close or manual reset.
    """
    state = default_state()
    save_state(state)
    logger.info("State reset to clean slate.")
    return state


def record_buy(state, price, qty):
    """
    Records a new buy into state and recalculates derived fields.
    """
    state['buys'].append({
        "price": price,
        "qty": qty,
        "timestamp": datetime.now().isoformat()
    })
    state['total_qty'] += qty
    state['is_active'] = True
    state['grid_level'] = len(state['buys']) - 1  # 0-indexed: 0=initial, 1=first avg down, etc.

    # Recalculate lowest buy price
    state['lowest_buy_price'] = min(b['price'] for b in state['buys'])

    # Recalculate weighted average cost
    total_cost = sum(b['price'] * b['qty'] for b in state['buys'])
    state['average_cost'] = total_cost / state['total_qty']

    state['last_action'] = f"BUY {qty} @ {price:.2f}"
    state['last_action_time'] = datetime.now().isoformat()
    state['last_price'] = price

    logger.info(f"BUY recorded: {qty} contract(s) @ {price:.2f} | "
                f"total_qty={state['total_qty']} | avg_cost={state['average_cost']:.2f} | "
                f"grid_level={state['grid_level']}")
    return state


def record_sell(state, price, qty, profit_reserve_pct):
    """
    Records a full position close, calculates P&L, and sequesters profit reserve.
    Then resets position tracking fields.
    """
    if state['average_cost'] is None:
        logger.error("record_sell called but no average_cost in state.")
        return state

    # MES point value = $5 per point
    MES_POINT_VALUE = 5.0
    pnl_points = (price - state['average_cost']) * qty
    pnl_dollars = pnl_points * MES_POINT_VALUE

    # Sequester portion of profit (only if profitable)
    if pnl_dollars > 0:
        reserve_addition = pnl_dollars * profit_reserve_pct
        state['profit_reserve'] += reserve_addition
        logger.info(f"Profit reserve +${reserve_addition:.2f} (total: ${state['profit_reserve']:.2f})")

    state['realized_pnl'] += pnl_dollars
    state['last_sell_price'] = price   # Used for re-entry trigger after single contract win
    state['last_action'] = f"SELL {qty} @ {price:.2f} | PnL: ${pnl_dollars:.2f}"
    state['last_action_time'] = datetime.now().isoformat()
    state['last_price'] = price

    # Clear position tracking
    state['buys'] = []
    state['total_qty'] = 0
    state['grid_level'] = 0
    state['lowest_buy_price'] = None
    state['average_cost'] = None
    state['is_active'] = False

    logger.info(f"SELL recorded: {qty} contract(s) @ {price:.2f} | "
                f"PnL: ${pnl_dollars:.2f} | Total realized: ${state['realized_pnl']:.2f}")
    return state


def get_unrealized_pnl(state, current_price):
    """
    Calculates current unrealized P&L based on average cost vs current price.
    Returns dollar value.
    """
    if not state['is_active'] or state['average_cost'] is None:
        return 0.0
    MES_POINT_VALUE = 5.0
    return (current_price - state['average_cost']) * state['total_qty'] * MES_POINT_VALUE


def print_status(state, current_price=None):
    lines = []
    lines.append("\n" + "="*50)
    lines.append("  MES BOT STATUS")
    lines.append("="*50)
    lines.append(f"  Active:         {state['is_active']}")
    lines.append(f"  Grid Level:     {state['grid_level']}")
    lines.append(f"  Total Qty:      {state.get('total_qty', 0)} contracts")
    lines.append(f"  Avg Cost:       {state['average_cost']:.2f}" if state.get('average_cost') else "  Avg Cost:       —")
    lines.append(f"  Lowest Buy:     {state['lowest_buy_price']:.2f}" if state.get('lowest_buy_price') else "  Lowest Buy:     —")
    if current_price:
        upnl = get_unrealized_pnl(state, current_price)
        lines.append(f"  Current Price:  {current_price:.2f}")
        lines.append(f"  Unrealized PnL: ${upnl:.2f}")
    lines.append(f"  Realized PnL:   ${state.get('realized_pnl', 0):.2f}")
    lines.append(f"  Profit Reserve: ${state.get('profit_reserve', 0):.2f}")
    lines.append(f"  Last Action:    {state.get('last_action')}")
    lines.append("="*50 + "\n")
    print("\n".join(lines))