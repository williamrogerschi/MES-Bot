# ============================================================
# strategy.py — Grid Trading Logic Engine
# Pure logic — no broker calls, no I/O.
# Takes current price + state, returns an action decision.
# ============================================================

import logging
from config import (
    GRID_PCT, INITIAL_QTY, AVERAGING_QTY,
    MAX_GRID_LEVELS, PROFIT_RESERVE_PCT,
    MES_MARGIN_PER_CONTRACT, ACCOUNT_SIZE, MAX_MARGIN_USAGE
)
from state import get_unrealized_pnl

logger = logging.getLogger(__name__)

# Action constants — bot.py checks these to decide what to execute
ACTION_NONE      = "NONE"
ACTION_BUY_INIT  = "BUY_INIT"    # First buy to enter position
ACTION_BUY_AVG   = "BUY_AVG"     # Average down on dip
ACTION_SELL_ALL  = "SELL_ALL"    # Sell entire position (profitable)
ACTION_HOLD      = "HOLD"        # At sell trigger but not profitable yet — hold


def check_margin_available(current_qty, additional_qty):
    """
    Safety check: ensures buying more contracts won't exceed
    the max margin usage limit defined in config.
    """
    total_qty = current_qty + additional_qty
    required_margin = total_qty * MES_MARGIN_PER_CONTRACT
    max_allowed_margin = ACCOUNT_SIZE * MAX_MARGIN_USAGE

    if required_margin > max_allowed_margin:
        logger.warning(
            f"Margin check FAILED: {total_qty} contracts would require "
            f"${required_margin:,.0f} margin, but max allowed is "
            f"${max_allowed_margin:,.0f} ({MAX_MARGIN_USAGE*100:.0f}% of ${ACCOUNT_SIZE:,})"
        )
        return False
    return True


def evaluate(state, current_price):
    """
    Core grid logic. Given current state and price, returns:
        (action, qty, reason)

    action: one of the ACTION_* constants above
    qty:    number of contracts involved in the action
    reason: human-readable string explaining why

    Called on every price update from the broker.
    """

    # ── Case 1: Not in a position — need initial entry ──────────────
    if not state['is_active']:
        if check_margin_available(0, INITIAL_QTY):
            return (
                ACTION_BUY_INIT,
                INITIAL_QTY,
                f"No active position — entering with {INITIAL_QTY} contract @ market"
            )
        else:
            return (ACTION_NONE, 0, "No active position but margin limit reached — cannot enter")

    # We have an active position from here down
    lowest_buy  = state['lowest_buy_price']
    avg_cost    = state['average_cost']
    total_qty   = state['total_qty']
    grid_level  = state['grid_level']

    if lowest_buy is None or avg_cost is None:
        return (ACTION_NONE, 0, "State error: missing price data")

    # ── Compute key price levels ─────────────────────────────────────
    sell_trigger  = lowest_buy * (1 + GRID_PCT)   # +1.2% from lowest buy
    dip_trigger   = lowest_buy * (1 - GRID_PCT)   # -1.2% from lowest buy

    # Break-even price needed to be profitable on full position
    # (avg_cost is already the weighted average — selling above it = profit)
    breakeven     = avg_cost

    # Unrealized P&L in dollars
    unrealized    = get_unrealized_pnl(state, current_price)

    logger.debug(
        f"Price: {current_price:.2f} | Lowest: {lowest_buy:.2f} | "
        f"Avg: {avg_cost:.2f} | Sell trigger: {sell_trigger:.2f} | "
        f"Dip trigger: {dip_trigger:.2f} | uPnL: ${unrealized:.2f}"
    )

    # ── Case 2: Price hit sell trigger ───────────────────────────────
    if current_price >= sell_trigger:
        if unrealized >= 0:
            # Profitable — sell everything
            return (
                ACTION_SELL_ALL,
                total_qty,
                f"Price {current_price:.2f} >= sell trigger {sell_trigger:.2f} "
                f"and position profitable (uPnL: ${unrealized:.2f}) — SELL ALL {total_qty}"
            )
        else:
            # At sell trigger but still underwater — hold and wait for breakeven
            return (
                ACTION_HOLD,
                0,
                f"Price {current_price:.2f} >= sell trigger {sell_trigger:.2f} "
                f"but uPnL ${unrealized:.2f} < 0 — holding for breakeven at {breakeven:.2f}"
            )

    # ── Case 3: Holding and price hit breakeven (after being underwater) ──
    # This catches the scenario where we passed the sell trigger unprofitably
    # and price continued up to breakeven
    if current_price >= breakeven and unrealized >= 0 and grid_level > 0:
        return (
            ACTION_SELL_ALL,
            total_qty,
            f"Price {current_price:.2f} reached breakeven {breakeven:.2f} "
            f"on averaged-down position — SELL ALL {total_qty}"
        )

    # ── Case 4: Price dropped to dip trigger — average down ──────────
    if current_price <= dip_trigger:
        if grid_level >= MAX_GRID_LEVELS:
            return (
                ACTION_NONE,
                0,
                f"Price {current_price:.2f} hit dip trigger {dip_trigger:.2f} "
                f"but MAX_GRID_LEVELS ({MAX_GRID_LEVELS}) reached — holding"
            )

        if not check_margin_available(total_qty, AVERAGING_QTY):
            return (
                ACTION_NONE,
                0,
                f"Price hit dip trigger but margin limit would be exceeded — holding"
            )

        return (
            ACTION_BUY_AVG,
            AVERAGING_QTY,
            f"Price {current_price:.2f} <= dip trigger {dip_trigger:.2f} "
            f"— averaging down, buying {AVERAGING_QTY} more (grid level {grid_level} -> {grid_level+1})"
        )

    # ── Case 5: Price in the middle — nothing to do ──────────────────
    return (
        ACTION_NONE,
        0,
        f"Price {current_price:.2f} between dip {dip_trigger:.2f} "
        f"and sell {sell_trigger:.2f} — holding {total_qty} contract(s)"
    )


def should_close_for_weekend(now):
    """
    Returns True if it's time to execute the Friday close.
    now: datetime object in US Eastern time
    """
    from config import (
        WEEKLY_CLOSE_DAY, WEEKLY_CLOSE_HOUR,
        WEEKLY_CLOSE_MINUTE, WEEKLY_CLOSE_SECOND
    )
    return (
        now.weekday() == WEEKLY_CLOSE_DAY and
        now.hour == WEEKLY_CLOSE_HOUR and
        now.minute == WEEKLY_CLOSE_MINUTE and
        now.second >= WEEKLY_CLOSE_SECOND
    )


def should_open_for_week(now):
    """
    Returns True if it's time to place the Sunday re-entry buy.
    now: datetime object in US Eastern time
    """
    from config import (
        WEEKLY_OPEN_DAY, WEEKLY_OPEN_HOUR, WEEKLY_OPEN_MINUTE
    )
    return (
        now.weekday() == WEEKLY_OPEN_DAY and
        now.hour == WEEKLY_OPEN_HOUR and
        now.minute == WEEKLY_OPEN_MINUTE
    )


def calculate_next_levels(state):
    """
    Utility: returns the next sell trigger and dip trigger prices.
    Used for logging and status display.
    """
    if not state['is_active'] or not state['lowest_buy_price']:
        return None, None

    sell = state['lowest_buy_price'] * (1 + GRID_PCT)
    dip  = state['lowest_buy_price'] * (1 - GRID_PCT)
    return sell, dip