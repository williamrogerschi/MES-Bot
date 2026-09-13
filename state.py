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
    """
    FULL wipe — clears position AND lifetime stats (realized_pnl,
    profit_reserve) back to zero. This is a destructive, intentional reset
    and should only ever be called for a deliberate "start completely over"
    action. It must NEVER be called automatically from broker reconciliation
    — a position-count mismatch has nothing to do with your trading history.
    Use reset_position_only() for reconciliation instead.
    """
    state = default_state()
    save_state(state)
    logger.info("State reset to clean slate (realized_pnl and profit_reserve zeroed).")
    return state


def reset_position_only(state):
    """
    Clear position-related fields only — is_active, buys, total_qty,
    grid_level, lowest_buy_price, average_cost, last_sell_price — while
    PRESERVING lifetime stats: realized_pnl, profit_reserve, and any other
    running totals.

    Use this for broker reconciliation mismatches (state says N contracts,
    IBKR says a different N — e.g. after a roll, a restart mid-trade, or a
    dropped connection). A position-count mismatch is a bookkeeping issue
    about the CURRENT position, not a reason to erase trading history.
    reset_state() (full wipe) is reserved for an explicit, intentional
    full reset — never called automatically from reconciliation.
    """
    state['is_active'] = False
    state['grid_level'] = 0
    state['total_qty'] = 0
    state['buys'] = []
    state['lowest_buy_price'] = None
    state['average_cost'] = None
    state['last_sell_price'] = None
    save_state(state)
    logger.info(
        f"Position fields reset to flat (realized_pnl=${state.get('realized_pnl', 0):.2f} "
        f"and profit_reserve=${state.get('profit_reserve', 0):.2f} preserved)."
    )
    return state


def _recalculate(state):
    """Recalculate derived fields from buys list."""
    # Defensive: drop any malformed lots (missing price/qty). A single bad
    # entry must never crash _recalculate — that failure mode leaves the
    # state perpetually 'flat' and triggers infinite re-buys.
    clean = [b for b in state['buys']
             if isinstance(b, dict) and 'price' in b and 'qty' in b]
    if len(clean) != len(state['buys']):
        logger.error(f"_recalculate: dropped {len(state['buys']) - len(clean)} malformed lot(s)")
        state['buys'] = clean

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


def record_partial_sell_fifo(state, sell_price, profit_reserve_pct):
    """
    Sell exactly ONE contract from the OLDEST lot (FIFO).
    Used by the Friday close when the position is RED and holds 2+ contracts.

    In an averaging-DOWN grid, buys[0] is the initial entry at the highest
    price — i.e. the contract furthest underwater. Selling one contract from
    it realizes the worst single-contract loss and pulls average cost down.

    If the oldest lot has qty > 1, it is split: qty decremented by 1, the lot
    stays. If the oldest lot has qty == 1, the lot is removed entirely.

    Position remains active afterward (caller guarantees 2+ contracts held).
    """
    if not state['buys']:
        logger.error("record_partial_sell_fifo: no lots to sell")
        return state

    lot = state['buys'][0]              # oldest = furthest in the hole
    buy_price = lot['price']

    MES_POINT_VALUE = 5.0
    pnl_points = (sell_price - buy_price) * 1     # selling exactly 1 contract
    pnl_dollars = pnl_points * MES_POINT_VALUE

    if pnl_dollars > 0:
        reserve_addition = pnl_dollars * profit_reserve_pct
        state['profit_reserve'] += reserve_addition
        logger.info(f"Profit reserve +${reserve_addition:.2f} (total: ${state['profit_reserve']:.2f})")

    state['realized_pnl'] += pnl_dollars

    # Split or remove the oldest lot
    if lot['qty'] > 1:
        lot['qty'] -= 1                 # keep the lot, drop one contract
    else:
        state['buys'].pop(0)            # single-contract lot — remove it

    state = _recalculate(state)
    state['last_action'] = f"FRI PARTIAL SELL 1 @ {sell_price:.2f} | PnL: ${pnl_dollars:.2f}"
    state['last_action_time'] = datetime.now().isoformat()
    state['last_price'] = sell_price

    logger.info(
        f"FRIDAY PARTIAL SELL 1 @ {sell_price:.2f} (from oldest lot @ {buy_price:.2f}) | "
        f"PnL: ${pnl_dollars:.2f} | Total realized: ${state['realized_pnl']:.2f} | "
        f"Remaining lots: {len(state['buys'])} | total_qty={state['total_qty']} | "
        f"grid_level={state['grid_level']}"
    )
    return state


def record_sell(state, price, qty, profit_reserve_pct):
    """Full position close — used for Friday close and contract roll."""
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