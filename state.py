# ============================================================
# state.py — Position State Persistence
# ============================================================

import json
import os
import logging
from datetime import datetime
from config import STATE_FILE

logger = logging.getLogger(__name__)


def default_state():
    return {
        "is_active": False,
        "grid_level": 0,
        "total_qty": 0,
        "buys": [],
        "lowest_buy_price": None,
        "average_cost": None,
        "realized_pnl": 0.0,
        "profit_reserve": 0.0,
        "last_action": None,
        "last_action_time": None,
        "last_sell_price": None,
        "last_price": None,
        "week_start_balance": None,
        "weekend_closed": False,
    }


def load_state():
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
    Sell an averaged-down lot (qty>=2) and immediately rebuy 1.
    Position stays active — never goes flat.
    """
    if lot_index >= len(state['buys']):
        logger.error(f"record_lot_sell_and_rebuy: lot_index {lot_index} out of range")
        return state

    lot = state['buys'][lot_index]
    qty_sold = lot['qty']
    buy_price = lot['price']

    MES_POINT_VALUE = 5.0
    pnl_points = (sell_price - buy_price) * qty_sold
    pnl_dollars = pnl_points * MES_POINT_VALUE

    if pnl_dollars > 0:
        reserve_addition = pnl_dollars * profit_reserve_pct
        state['profit_reserve'] += reserve_addition
        logger.info(f"Profit reserve +${reserve_addition:.2f} (total: ${state['profit_reserve']:.2f})")

    state['realized_pnl'] += pnl_dollars

    # Remove sold lot, add rebuy lot
    state['buys'].pop(lot_index)
    state['buys'].append({
        "price": rebuy_price,
        "qty": 1,
        "timestamp": datetime.now().isoformat()
    })

    state = _recalculate(state)
    state['last_action'] = (f"SELL {qty_sold} @ {sell_price:.2f} | "
                            f"REBUY 1 @ {rebuy_price:.2f} | PnL: ${pnl_dollars:.2f}")
    state['last_action_time'] = datetime.now().isoformat()
    state['last_price'] = rebuy_price

    logger.info(f"SELL {qty_sold} @ {sell_price:.2f} + REBUY 1 @ {rebuy_price:.2f} | "
                f"PnL: ${pnl_dollars:.2f} | Total realized: ${state['realized_pnl']:.2f} | "
                f"Remaining lots: {len(state['buys'])} | total_qty={state['total_qty']}")
    return state


def record_lot_sell_single(state, lot_index, sell_price, profit_reserve_pct):
    """
    Sell a single contract lot (qty==1) with no rebuy.
    If this was the last lot, position goes flat.
    """
    if lot_index >= len(state['buys']):
        logger.error(f"record_lot_sell_single: lot_index {lot_index} out of range")
        return state

    lot = state['buys'][lot_index]
    qty_sold = lot['qty']
    buy_price = lot['price']

    MES_POINT_VALUE = 5.0
    pnl_points = (sell_price - buy_price) * qty_sold
    pnl_dollars = pnl_points * MES_POINT_VALUE

    if pnl_dollars > 0:
        reserve_addition = pnl_dollars * profit_reserve_pct
        state['profit_reserve'] += reserve_addition
        logger.info(f"Profit reserve +${reserve_addition:.2f} (total: ${state['profit_reserve']:.2f})")

    state['realized_pnl'] += pnl_dollars
    state['last_sell_price'] = sell_price

    # Remove the sold lot
    state['buys'].pop(lot_index)

    state = _recalculate(state)
    state['last_action'] = f"SELL {qty_sold} @ {sell_price:.2f} | PnL: ${pnl_dollars:.2f}"
    state['last_action_time'] = datetime.now().isoformat()
    state['last_price'] = sell_price

    logger.info(f"SELL SINGLE {qty_sold} @ {sell_price:.2f} | "
                f"PnL: ${pnl_dollars:.2f} | Total realized: ${state['realized_pnl']:.2f} | "
                f"Remaining lots: {len(state['buys'])} | total_qty={state['total_qty']}")
    return state


def record_sell(state, price, qty, profit_reserve_pct):
    """Full position close — used for Friday close only."""
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

    if state.get('is_active') and state.get('buys'):
        lines.append("  Lots:")
        for i, lot in enumerate(state['buys']):
            trigger = lot['price'] * (1 + GRID_PCT)
            action = "sell+rebuy" if lot['qty'] >= 2 else "sell only"
            lines.append(f"    [{i}] {lot['qty']} @ {lot['price']:.2f} → sell {trigger:.2f} ({action})")
    elif state.get('last_sell_price') and not state.get('is_active'):
        reentry = state['last_sell_price'] * (1 - GRID_PCT)
        lines.append(f"  Re-entry At:    {reentry:.2f} (1.2% below sell {state['last_sell_price']:.2f})")

    if state.get('lowest_buy_price'):
        dip_t = state['lowest_buy_price'] * (1 - GRID_PCT)
        lines.append(f"  Dip Trigger:    {dip_t:.2f}")

    lines.append("="*52 + "\n")
    print("\n".join(lines))