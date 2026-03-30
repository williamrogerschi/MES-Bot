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
        "buys": [],                     # List of individual lots: [{price, qty, timestamp}]
        "lowest_buy_price": None,       # Lowest price we bought at in current grid
        "average_cost": None,           # Weighted average cost of all current contracts
        "realized_pnl": 0.0,           # Total realized P&L across all closed trades (session)
        "profit_reserve": 0.0,          # Sequestered profit reserve
        "last_action": None,            # Last action taken (for logging/debugging)
        "last_action_time": None,
        "last_sell_price": None,        # Price of last sell (re-entry trigger when flat)
        "last_price": None,             # Last known price (for restart context)
        "week_start_balance": None,     # Account balance at Sunday open (for weekly tracking)
        "weekend_closed": False,        # Flag to trigger immediate rebuy after missed Sunday open
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


def _recalculate(state):
    """Recalculate derived fields from buys list."""
    if not state['buys']:
        state['lowest_buy_price'] = None
        state['average_cost'] = None
        state['total_qty'] = 0
        state['grid_level'] = 0
        state['is_active'] = False
    else:
        state['lowest_buy_price'] = min(b['price'] for b in state['buys'])
        total_cost = sum(b['price'] * b['qty'] for b in state['buys'])
        state['total_qty'] = sum(b['qty'] for b in state['buys'])
        state['average_cost'] = total_cost / state['total_qty']
        state['grid_level'] = len(state['buys']) - 1
        state['is_active'] = True
    return state


def record_buy(state, price, qty):
    """
    Records a new buy lot into state and recalculates derived fields.
    """
    state['buys'].append({
        "price": price,
        "qty": qty,
        "timestamp": datetime.now().isoformat()
    })

    state = _recalculate(state)
    state['last_action'] = f"BUY {qty} @ {price:.2f}"
    state['last_action_time'] = datetime.now().isoformat()
    state['last_price'] = price

    logger.info(f"BUY recorded: {qty} contract(s) @ {price:.2f} | "
                f"total_qty={state['total_qty']} | avg_cost={state['average_cost']:.2f} | "
                f"grid_level={state['grid_level']}")
    return state


def record_lot_sell_and_rebuy(state, lot_index, sell_price, rebuy_price, profit_reserve_pct):
    """
    Records a lot sell + immediate rebuy of 1 contract.
    Sells the lot at lot_index, calculates P&L, then adds a new 1-contract lot at rebuy_price.
    Position stays active — bot never goes flat with this action.
    """
    if lot_index >= len(state['buys']):
        logger.error(f"record_lot_sell_and_rebuy: lot_index {lot_index} out of range")
        return state

    lot = state['buys'][lot_index]
    qty_sold = lot['qty']
    buy_price = lot['price']

    # Calculate P&L for this lot
    MES_POINT_VALUE = 5.0
    pnl_points = (sell_price - buy_price) * qty_sold
    pnl_dollars = pnl_points * MES_POINT_VALUE

    # Sequester portion of profit if profitable
    if pnl_dollars > 0:
        reserve_addition = pnl_dollars * profit_reserve_pct
        state['profit_reserve'] += reserve_addition
        logger.info(f"Profit reserve +${reserve_addition:.2f} (total: ${state['profit_reserve']:.2f})")

    state['realized_pnl'] += pnl_dollars

    # Remove the sold lot
    state['buys'].pop(lot_index)

    # Add rebuy as new lot
    state['buys'].append({
        "price": rebuy_price,
        "qty": 1,
        "timestamp": datetime.now().isoformat()
    })

    # Recalculate derived fields
    state = _recalculate(state)

    state['last_action'] = (f"SELL {qty_sold} @ {sell_price:.2f} | "
                            f"REBUY 1 @ {rebuy_price:.2f} | PnL: ${pnl_dollars:.2f}")
    state['last_action_time'] = datetime.now().isoformat()
    state['last_price'] = rebuy_price

    logger.info(f"SELL {qty_sold} @ {sell_price:.2f} + REBUY 1 @ {rebuy_price:.2f} | "
                f"PnL: ${pnl_dollars:.2f} | Total realized: ${state['realized_pnl']:.2f} | "
                f"Remaining lots: {len(state['buys'])} | total_qty={state['total_qty']}")
    return state


def record_sell(state, price, qty, profit_reserve_pct):
    """
    Records a FULL position close — used for Friday close only.
    Calculates P&L, sequesters reserve, resets all position fields.
    """
    if state['average_cost'] is None:
        logger.error("record_sell called but no average_cost in state.")
        return state

    MES_POINT_VALUE = 5.0
    pnl_points = (price - state['average_cost']) * qty
    pnl_dollars = pnl_points * MES_POINT_VALUE

    if pnl_dollars > 0:
        reserve_addition = pnl_dollars * profit_reserve_pct
        state['profit_reserve'] += reserve_addition
        logger.info(f"Profit reserve +${reserve_addition:.2f} (total: ${state['profit_reserve']:.2f})")

    state['realized_pnl'] += pnl_dollars
    state['last_sell_price'] = price
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

    logger.info(f"SELL ALL recorded: {qty} contract(s) @ {price:.2f} | "
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
    from config import GRID_PCT
    lines = []
    lines.append("\n" + "="*52)
    lines.append("  MES BOT STATUS")
    lines.append("="*52)
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
    lines.append(f"  Last Action:    {state.get('last_action', '—')}")

    # Show each lot's sell trigger
    if state.get('is_active') and state.get('buys'):
        lines.append("  Lots:")
        for i, lot in enumerate(state['buys']):
            trigger = lot['price'] * (1 + GRID_PCT)
            lines.append(f"    [{i}] {lot['qty']} @ {lot['price']:.2f} → sell trigger {trigger:.2f}")
    elif state.get('last_sell_price') and not state.get('is_active'):
        reentry = state['last_sell_price'] * (1 - GRID_PCT)
        lines.append(f"  Re-entry At:    {reentry:.2f} (1.2% below sell {state['last_sell_price']:.2f})")

    if state.get('lowest_buy_price'):
        dip_t = state['lowest_buy_price'] * (1 - GRID_PCT)
        lines.append(f"  Dip Trigger:    {dip_t:.2f}")

    lines.append("="*52 + "\n")
    print("\n".join(lines))
