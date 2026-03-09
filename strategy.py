# ============================================================
# strategy.py — Grid Trading Logic Engine
# Pure logic — no broker calls, no I/O.
# Takes current price + state, returns an action decision.
# ============================================================

import logging
from config import (
    GRID_PCT, INITIAL_QTY, AVERAGING_QTY,
    MAX_GRID_LEVELS, MES_MARGIN_PER_CONTRACT,
    ACCOUNT_SIZE, MAX_MARGIN_USAGE
)
from state import get_unrealized_pnl

logger = logging.getLogger(__name__)

# Action constants
ACTION_NONE         = "NONE"
ACTION_BUY_INIT     = "BUY_INIT"     # First entry into position
ACTION_BUY_REENTER  = "BUY_REENTER"  # Re-entry after single contract win
ACTION_BUY_AVG      = "BUY_AVG"      # Average down on dip
ACTION_SELL_ALL     = "SELL_ALL"     # Sell entire position (profitable)
ACTION_HOLD         = "HOLD"         # At sell trigger but not profitable — hold


def check_margin_available(current_qty, additional_qty):
    """
    Safety check: ensures buying won't exceed max margin usage.
    """
    total_qty = current_qty + additional_qty
    required_margin = total_qty * MES_MARGIN_PER_CONTRACT
    max_allowed = ACCOUNT_SIZE * MAX_MARGIN_USAGE

    if required_margin > max_allowed:
        logger.warning(
            f"Margin check FAILED: {total_qty} contracts = "
            f"${required_margin:,.0f} margin, max allowed ${max_allowed:,.0f}"
        )
        return False
    return True


def evaluate(state, current_price):
    """
    Core grid logic. Returns (action, qty, reason).

    Entry logic:
      - If flat and no last_sell_price: buy immediately (fresh start)
      - If flat and last_sell_price set: wait for -1.2% from last sell before re-entering
      - If holding 1 and price rises 1.2%: sell, go flat (no immediate rebuy)
      - If holding 3+ (averaged down) and price rises 1.2% from lowest buy
        AND profitable: sell all, immediately rebuy 1
      - If holding any and price drops 1.2% from lowest: average down
    """

    # ── Case 1: Flat — decide whether to enter ──────────────────────
    if not state['is_active']:
        last_sell = state.get('last_sell_price')

        if last_sell is None:
            # Fresh start — no prior sell price, enter immediately
            if check_margin_available(0, INITIAL_QTY):
                return (
                    ACTION_BUY_INIT,
                    INITIAL_QTY,
                    f"Fresh start — entering with {INITIAL_QTY} contract @ {current_price:.2f}"
                )
            else:
                return (ACTION_NONE, 0, "Flat but margin limit reached")

        else:
            # We sold a single contract — wait for -1.2% from sell price
            reentry_trigger = last_sell * (1 - GRID_PCT)
            if current_price <= reentry_trigger:
                if check_margin_available(0, INITIAL_QTY):
                    return (
                        ACTION_BUY_REENTER,
                        INITIAL_QTY,
                        f"Price {current_price:.2f} <= re-entry trigger {reentry_trigger:.2f} "
                        f"(1.2% below last sell {last_sell:.2f}) — re-entering"
                    )
                else:
                    return (ACTION_NONE, 0, "Re-entry trigger hit but margin limit reached")
            else:
                return (
                    ACTION_NONE,
                    0,
                    f"Flat — waiting for re-entry at {reentry_trigger:.2f} "
                    f"(current: {current_price:.2f}, last sell: {last_sell:.2f})"
                )

    # We have an active position from here down
    lowest_buy = state['lowest_buy_price']
    avg_cost   = state['average_cost']
    total_qty  = state['total_qty']
    grid_level = state['grid_level']

    if lowest_buy is None or avg_cost is None:
        return (ACTION_NONE, 0, "State error: missing price data")

    # ── Key price levels ─────────────────────────────────────────────
    sell_trigger = lowest_buy * (1 + GRID_PCT)   # +1.2% from lowest buy
    dip_trigger  = lowest_buy * (1 - GRID_PCT)   # -1.2% from lowest buy
    unrealized   = get_unrealized_pnl(state, current_price)

    logger.debug(
        f"Price: {current_price:.2f} | Lowest: {lowest_buy:.2f} | "
        f"Avg: {avg_cost:.2f} | Sell: {sell_trigger:.2f} | "
        f"Dip: {dip_trigger:.2f} | uPnL: ${unrealized:.2f} | "
        f"Grid level: {grid_level}"
    )

    # ── Case 2: Price hit sell trigger ───────────────────────────────
    if current_price >= sell_trigger:
        if unrealized >= 0:
            return (
                ACTION_SELL_ALL,
                total_qty,
                f"Price {current_price:.2f} >= sell trigger {sell_trigger:.2f} | "
                f"uPnL ${unrealized:.2f} | grid_level={grid_level} — SELL ALL {total_qty}"
            )
        else:
            # At trigger but still underwater — hold for breakeven
            return (
                ACTION_HOLD,
                0,
                f"Price {current_price:.2f} >= sell trigger but uPnL "
                f"${unrealized:.2f} < 0 — holding for breakeven @ {avg_cost:.2f}"
            )

    # ── Case 3: Averaged-down position reached breakeven ─────────────
    # Catches case where sell trigger passed unprofitably and price
    # continued climbing to the actual breakeven
    if grid_level > 0 and current_price >= avg_cost and unrealized >= 0:
        return (
            ACTION_SELL_ALL,
            total_qty,
            f"Price {current_price:.2f} reached breakeven {avg_cost:.2f} "
            f"on averaged-down position — SELL ALL {total_qty}"
        )

    # ── Case 4: Price dropped to dip trigger — average down ──────────
    if current_price <= dip_trigger:
        if grid_level >= MAX_GRID_LEVELS:
            return (
                ACTION_NONE,
                0,
                f"Dip trigger hit but MAX_GRID_LEVELS ({MAX_GRID_LEVELS}) reached — holding"
            )
        if not check_margin_available(total_qty, AVERAGING_QTY):
            return (
                ACTION_NONE,
                0,
                "Dip trigger hit but margin limit would be exceeded — holding"
            )
        return (
            ACTION_BUY_AVG,
            AVERAGING_QTY,
            f"Price {current_price:.2f} <= dip trigger {dip_trigger:.2f} "
            f"— averaging down, buying {AVERAGING_QTY} more "
            f"(grid level {grid_level} -> {grid_level + 1})"
        )

    # ── Case 5: Price in range — nothing to do ───────────────────────
    return (
        ACTION_NONE,
        0,
        f"Price {current_price:.2f} between dip {dip_trigger:.2f} "
        f"and sell {sell_trigger:.2f} — holding {total_qty} contract(s)"
    )


def should_close_for_weekend(now):
    """Returns True if it's Friday 3:59:55 PM ET."""
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
    """Returns True if it's Sunday 5:00 PM ET."""
    from config import (
        WEEKLY_OPEN_DAY, WEEKLY_OPEN_HOUR, WEEKLY_OPEN_MINUTE
    )
    return (
        now.weekday() == WEEKLY_OPEN_DAY and
        now.hour == WEEKLY_OPEN_HOUR and
        now.minute == WEEKLY_OPEN_MINUTE
    )


def calculate_next_levels(state):
    """Returns (sell_trigger, dip_trigger) for status display."""
    if not state['is_active'] or not state['lowest_buy_price']:
        return None, None
    sell = state['lowest_buy_price'] * (1 + GRID_PCT)
    dip  = state['lowest_buy_price'] * (1 - GRID_PCT)
    return sell, dip