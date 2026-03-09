# ============================================================
# bot.py — Main Bot Loop (v4 — correct entry/exit logic)
# Orchestrates broker, strategy, and state.
# Run this file to start the bot: python bot.py
# ============================================================

import logging
import time
import sys
import queue
from datetime import datetime
import pytz

from config import (
    LOG_FILE, LOG_LEVEL,
    INITIAL_QTY, PROFIT_RESERVE_PCT
)
from broker import Broker
from strategy import (
    evaluate, should_close_for_weekend, should_open_for_week,
    calculate_next_levels,
    ACTION_NONE, ACTION_BUY_INIT, ACTION_BUY_REENTER,
    ACTION_BUY_AVG, ACTION_SELL_ALL, ACTION_HOLD
)
from state import (
    load_state, save_state, reset_state,
    record_buy, record_sell, print_status
)

# ── Logging Setup ─────────────────────────────────────────────
def setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout)
        ]
    )

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')

def now_et():
    return datetime.now(ET)


# ============================================================
# Reconciliation
# ============================================================

def reconcile_state_with_broker(state, broker):
    """
    On restart, compare saved state with actual IBKR positions.
    Trusts IBKR as source of truth.
    """
    logger.info("Reconciling saved state with IBKR positions...")
    live_positions = broker.get_open_positions()

    saved_qty = state.get('total_qty', 0)
    live_qty  = sum(p['qty'] for p in live_positions) if live_positions else 0

    if saved_qty == live_qty:
        logger.info(f"Reconciliation OK: both show {live_qty} contracts held.")
        return state

    logger.warning(
        f"MISMATCH: State file shows {saved_qty} contracts, "
        f"IBKR shows {live_qty} contracts."
    )

    if live_qty == 0:
        logger.warning("IBKR shows flat — resetting state to clean slate.")
        return reset_state()

    if live_qty > 0:
        logger.warning(f"Reconstructing state from IBKR: {live_qty} contracts found.")
        avg_cost = live_positions[0]['avg_cost'] if live_positions else None
        if avg_cost:
            price_in_points = avg_cost / 5.0
            state = reset_state()
            state = record_buy(state, price_in_points, live_qty)
            save_state(state)
            logger.info(
                f"State reconstructed: {live_qty} contracts @ "
                f"{price_in_points:.2f} points"
            )

    return state


# ============================================================
# Main Bot Class
# ============================================================

class MESBot:
    def __init__(self):
        self.broker             = Broker()
        self.state              = load_state()
        self.running            = False
        self._last_price        = None
        self._last_price_time   = None
        self._loop_counter      = 0
        self._action_queue      = queue.Queue()
        self._pending_action    = False
        self._weekend_closed    = False
        self._week_opened       = False

    # ----------------------------------------------------------
    # Startup
    # ----------------------------------------------------------

    def start(self):
        logger.info("="*60)
        logger.info("  MES Grid Bot Starting")
        logger.info("="*60)

        if not self.broker.connect():
            logger.error("Cannot start — failed to connect to IB Gateway.")
            sys.exit(1)

        self.state = reconcile_state_with_broker(self.state, self.broker)

        price = self.broker.get_current_price()
        if price:
            self._last_price = price

        print_status(self.state, price)

        # Trigger immediate entry check on startup if flat
        if not self.state['is_active'] and price:
            logger.info("Flat on startup — running immediate entry check.")
            action, qty, reason = evaluate(self.state, price)
            if action in (ACTION_BUY_INIT, ACTION_BUY_REENTER):
                self._action_queue.put((action, qty, reason))
                self._pending_action = True

        # Start streaming prices
        self.broker.start_price_stream(self._on_price_tick)

        self.running = True
        logger.info("Bot is running. Press Ctrl+C to stop.\n")

        self._run_loop()

    # ----------------------------------------------------------
    # Main Loop
    # ----------------------------------------------------------

    def _run_loop(self):
        try:
            while self.running:
                self.broker.run_loop()
                self._process_action_queue()
                self._check_schedule()
                self.broker.reconnect_if_needed()

                self._loop_counter += 1
                if self._loop_counter % 300 == 0:
                    print_status(self.state, self._last_price)
                    sell_trigger, dip_trigger = calculate_next_levels(self.state)
                    if sell_trigger:
                        logger.info(
                            f"Next levels — Sell: {sell_trigger:.2f} | "
                            f"Dip: {dip_trigger:.2f}"
                        )
                    if not self.state['is_active']:
                        last_sell = self.state.get('last_sell_price')
                        if last_sell:
                            reentry = last_sell * (1 - 0.012)
                            logger.info(
                                f"Flat — re-entry trigger at {reentry:.2f} "
                                f"(last sell: {last_sell:.2f})"
                            )

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Shutdown requested (Ctrl+C)")
            self._shutdown()

    # ----------------------------------------------------------
    # Price Callback — inside ib event loop, queue only
    # ----------------------------------------------------------

    def _on_price_tick(self, price):
        """
        Fires on every price tick.
        NEVER calls broker here — only queues actions.
        """
        self._last_price      = price
        self._last_price_time = now_et()

        if self._pending_action:
            return

        action, qty, reason = evaluate(self.state, price)

        if action == ACTION_NONE:
            return
        if action == ACTION_HOLD:
            logger.debug(f"HOLD: {reason}")
            return

        self._action_queue.put((action, qty, reason))
        self._pending_action = True
        logger.info(f"Queued: {action} x{qty} | {reason}")

    # ----------------------------------------------------------
    # Action Queue Processor — outside ib event loop, safe to trade
    # ----------------------------------------------------------

    def _process_action_queue(self):
        try:
            action, qty, reason = self._action_queue.get_nowait()
        except queue.Empty:
            return

        logger.info(f"Executing: {action} x{qty}")

        try:
            if action in (ACTION_BUY_INIT, ACTION_BUY_REENTER):
                self._execute_buy(qty)

            elif action == ACTION_BUY_AVG:
                self._execute_buy(qty)

            elif action == ACTION_SELL_ALL:
                # Pass grid level so we know whether to rebuy immediately
                self._execute_sell(qty, self.state['grid_level'])

        except Exception as e:
            logger.error(f"Order execution error [{action}]: {e}")

        finally:
            self._pending_action = False

    # ----------------------------------------------------------
    # Order Execution
    # ----------------------------------------------------------

    def _execute_buy(self, qty):
        """Places a buy and records it."""
        filled_price = self.broker.buy(qty)
        if filled_price:
            self.state = record_buy(self.state, filled_price, qty)
            save_state(self.state)
            logger.info(f"Buy complete: {qty} contract(s) @ {filled_price:.2f}")
            sell_t, dip_t = calculate_next_levels(self.state)
            logger.info(f"Watching — Sell trigger: {sell_t:.2f} | Dip trigger: {dip_t:.2f}")
        else:
            logger.error(f"Buy failed for {qty} contract(s) — will retry on next tick")

    def _execute_sell(self, qty, grid_level):
        """
        Sells entire position.
        - grid_level == 0: single contract win — go flat, wait for -1.2% re-entry
        - grid_level > 0:  averaged-down win — sell all, immediately rebuy 1
        """
        filled_price = self.broker.sell(qty)
        if filled_price:
            self.state = record_sell(
                self.state, filled_price, qty, PROFIT_RESERVE_PCT
            )
            save_state(self.state)
            logger.info(
                f"Sell complete: {qty} contract(s) @ {filled_price:.2f} | "
                f"Session PnL: ${self.state['realized_pnl']:.2f} | "
                f"Reserve: ${self.state['profit_reserve']:.2f}"
            )

            if grid_level > 0:
                # Averaged-down position — rebuy 1 immediately
                time.sleep(1)
                logger.info(
                    "Averaged-down position closed profitably — "
                    "re-entering with 1 contract immediately..."
                )
                new_price = self.broker.buy(INITIAL_QTY)
                if new_price:
                    self.state = record_buy(self.state, new_price, INITIAL_QTY)
                    save_state(self.state)
                    logger.info(f"Re-entry complete: 1 contract @ {new_price:.2f}")
                else:
                    logger.error("Re-entry buy failed — flat, will re-enter on next tick")
            else:
                # Single contract win — go flat and wait
                reentry_trigger = filled_price * (1 - 0.012)
                logger.info(
                    f"Single contract win — going flat. "
                    f"Will re-enter when price drops to {reentry_trigger:.2f} "
                    f"(1.2% below sell price {filled_price:.2f})"
                )
        else:
            logger.error(f"Sell failed for {qty} contract(s)")

    # ----------------------------------------------------------
    # Weekly Schedule
    # ----------------------------------------------------------

    def _check_schedule(self):
        now = now_et()

        # ── Friday 3:59:55 PM ET — Close all ─────────────────
        if should_close_for_weekend(now):
            if not self._weekend_closed:
                logger.info("WEEKLY CLOSE: Friday 3:59:55 PM ET")
                # Clear any pending actions
                while not self._action_queue.empty():
                    self._action_queue.get_nowait()
                self._pending_action = False

                if self.state['is_active']:
                    filled = self.broker.close_all_positions()
                    if filled:
                        self.state = record_sell(
                            self.state,
                            filled,
                            self.state['total_qty'],
                            PROFIT_RESERVE_PCT
                        )
                        # Clear last_sell_price for clean Sunday re-entry
                        self.state['last_sell_price'] = None
                        save_state(self.state)
                        logger.info(
                            f"Weekly close done. "
                            f"PnL: ${self.state['realized_pnl']:.2f} | "
                            f"Reserve: ${self.state['profit_reserve']:.2f}"
                        )
                else:
                    logger.info("Friday close — no active position.")

                self._weekend_closed = True
                self._week_opened    = False

        # ── Sunday 5:00 PM ET — Re-enter ─────────────────────
        elif should_open_for_week(now):
            if not self._week_opened:
                logger.info("WEEKLY OPEN: Sunday 5:00 PM ET")
                if not self.state['is_active']:
                    price = self.broker.buy(INITIAL_QTY)
                    if price:
                        self.state = record_buy(self.state, price, INITIAL_QTY)
                        save_state(self.state)
                        logger.info(f"Weekly open: 1 contract @ {price:.2f}")
                else:
                    logger.info("Sunday open — already holding, skipping.")

                self._week_opened    = True
                self._weekend_closed = False

    # ----------------------------------------------------------
    # Shutdown
    # ----------------------------------------------------------

    def _shutdown(self):
        logger.info("Shutting down...")
        save_state(self.state)
        print_status(self.state, self._last_price)
        self.broker.disconnect()
        logger.info("Bot stopped. State saved. Restart bot.py to resume.")


# ============================================================
# Entry Point
# ============================================================

if __name__ == '__main__':
    setup_logging()

    try:
        import ib_insync
        import pytz
    except ImportError as e:
        print(f"\nMissing dependency: {e}")
        print("Run: pip install ib_insync pytz")
        sys.exit(1)

    bot = MESBot()
    bot.start()