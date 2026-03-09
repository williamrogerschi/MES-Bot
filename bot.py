# ============================================================
# bot.py — Main Bot Loop
# Orchestrates broker, strategy, and state.
# Run this file to start the bot: python bot.py
# ============================================================

import logging
import time
import sys
from datetime import datetime
import pytz

from config import (
    LOG_FILE, LOG_LEVEL, STATE_FILE,
    INITIAL_QTY, PROFIT_RESERVE_PCT
)
from broker import Broker
from strategy import (
    evaluate, should_close_for_weekend, should_open_for_week,
    calculate_next_levels,
    ACTION_NONE, ACTION_BUY_INIT, ACTION_BUY_AVG,
    ACTION_SELL_ALL, ACTION_HOLD
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
            logging.StreamHandler(sys.stdout)   # Also print to terminal
        ]
    )

logger = logging.getLogger(__name__)

# ── Timezone ──────────────────────────────────────────────────
ET = pytz.timezone('America/New_York')

def now_et():
    return datetime.now(ET)


# ============================================================
# Reconciliation — called on every startup
# ============================================================

def reconcile_state_with_broker(state, broker):
    """
    On restart, compare saved state with actual IBKR positions.
    If there's a mismatch, trust IBKR (it's the source of truth)
    and warn the user.
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

    if live_qty == 0 and saved_qty > 0:
        # Position was closed externally (manually or by expiry)
        logger.warning("IBKR shows flat — resetting state to match.")
        return reset_state()

    if live_qty > 0 and saved_qty == 0:
        # Bot was restarted but there's an open position we don't have state for
        # Best we can do: record it at the IBKR avg cost and resume
        logger.warning(
            f"Found open position ({live_qty} contracts) with no state record. "
            f"Reconstructing state from IBKR data."
        )
        avg_cost = live_positions[0]['avg_cost'] if live_positions else None
        if avg_cost:
            state = reset_state()
            state = record_buy(state, avg_cost, live_qty)
            save_state(state)
            logger.info(f"State reconstructed: {live_qty} contracts @ avg {avg_cost:.2f}")

    return state


# ============================================================
# Main Bot Class
# ============================================================

class MESBot:
    def __init__(self):
        self.broker   = Broker()
        self.state    = load_state()
        self.running  = False
        self._last_price     = None
        self._last_price_time = None
        self._weekend_closed = False    # Track if we've done the Friday close this week
        self._week_opened    = False    # Track if we've done the Sunday open this week

    def start(self):
        logger.info("="*60)
        logger.info("  MES Grid Bot Starting")
        logger.info("="*60)

        # Connect to IB Gateway
        if not self.broker.connect():
            logger.error("Cannot start — failed to connect to IB Gateway.")
            sys.exit(1)

        # Reconcile saved state with actual IBKR positions
        self.state = reconcile_state_with_broker(self.state, self.broker)

        # Print current status
        price = self.broker.get_current_price()
        print_status(self.state, price)

        # Start streaming prices — on_price() fires on every tick
        self.broker.start_price_stream(self.on_price)

        self.running = True
        logger.info("Bot is running. Press Ctrl+C to stop.\n")

        self._run_loop()

    def _run_loop(self):
        """
        Main loop. Keeps running until Ctrl+C.
        Every iteration:
          - Processes any pending IB events
          - Checks weekend schedule
          - Checks connection health
        Price-based decisions happen in on_price() callback.
        """
        try:
            while self.running:
                # Process IB events (price updates, order fills, etc.)
                self.broker.run_loop()

                # Check schedule (Friday close / Sunday open)
                self._check_schedule()

                # Check connection health every loop
                self.broker.reconnect_if_needed()

                # Status print every 5 minutes (300 iterations at ~1s each)
                if hasattr(self, '_loop_counter'):
                    self._loop_counter += 1
                else:
                    self._loop_counter = 0

                if self._loop_counter % 300 == 0:
                    print_status(self.state, self._last_price)
                    sell_trigger, dip_trigger = calculate_next_levels(self.state)
                    if sell_trigger:
                        logger.info(f"Next levels — Sell: {sell_trigger:.2f} | Dip: {dip_trigger:.2f}")

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Shutdown requested by user (Ctrl+C)")
            self._shutdown()

    def on_price(self, price):
        """
        Called on every price tick from the broker.
        This is where grid decisions are made.
        """
        self._last_price = price
        self._last_price_time = now_et()

        # Get action from strategy
        action, qty, reason = evaluate(self.state, price)

        if action == ACTION_NONE or action == ACTION_HOLD:
            if action == ACTION_HOLD:
                logger.debug(f"HOLD: {reason}")
            return  # Nothing to do

        logger.info(f"ACTION: {action} | {reason}")

        # ── Execute the action ────────────────────────────────

        if action == ACTION_BUY_INIT:
            filled_price = self.broker.buy(qty)
            if filled_price:
                self.state = record_buy(self.state, filled_price, qty)
                save_state(self.state)

        elif action == ACTION_BUY_AVG:
            filled_price = self.broker.buy(qty)
            if filled_price:
                self.state = record_buy(self.state, filled_price, qty)
                save_state(self.state)

        elif action == ACTION_SELL_ALL:
            filled_price = self.broker.sell(qty)
            if filled_price:
                self.state = record_sell(
                    self.state, filled_price, qty, PROFIT_RESERVE_PCT
                )
                save_state(self.state)

                # Immediately re-enter with 1 contract (per strategy rules)
                logger.info("Re-entering with 1 contract after sell...")
                time.sleep(1)  # Brief pause between sell and re-buy
                new_entry_price = self.broker.buy(INITIAL_QTY)
                if new_entry_price:
                    self.state = record_buy(self.state, new_entry_price, INITIAL_QTY)
                    save_state(self.state)
                    logger.info(f"Re-entry complete: 1 contract @ {new_entry_price:.2f}")

    def _check_schedule(self):
        """
        Checks if it's time for the weekly close (Friday) or
        weekly open (Sunday). Uses ET timezone.
        """
        now = now_et()

        # ── Friday Close ─────────────────────────────────────
        if should_close_for_weekend(now):
            if not self._weekend_closed:
                logger.info("WEEKLY CLOSE: Friday 3:59:55 PM ET — closing all positions.")
                if self.state['is_active']:
                    filled = self.broker.close_all_positions()
                    if filled:
                        self.state = record_sell(
                            self.state, filled,
                            self.state['total_qty'],
                            PROFIT_RESERVE_PCT
                        )
                        save_state(self.state)
                        logger.info(f"Weekly close complete. Realized PnL this week: "
                                    f"${self.state['realized_pnl']:.2f}")
                else:
                    logger.info("Friday close — no position to close.")

                self._weekend_closed = True
                self._week_opened = False  # Reset so Sunday open can fire

        # ── Sunday Open ──────────────────────────────────────
        elif should_open_for_week(now):
            if not self._week_opened:
                logger.info("WEEKLY OPEN: Sunday 5:00 PM ET — entering initial position.")
                if not self.state['is_active']:
                    price = self.broker.buy(INITIAL_QTY)
                    if price:
                        self.state = record_buy(self.state, price, INITIAL_QTY)
                        save_state(self.state)
                        logger.info(f"Weekly open complete: 1 contract @ {price:.2f}")
                else:
                    logger.info("Sunday open — already holding a position, skipping re-entry.")

                self._week_opened = True
                self._weekend_closed = False  # Reset for next Friday

    def _shutdown(self):
        """
        Clean shutdown — saves state and disconnects.
        Position is NOT closed on shutdown (intentional — bot can restart
        and resume holding the position).
        """
        logger.info("Shutting down bot...")
        save_state(self.state)
        print_status(self.state, self._last_price)
        self.broker.disconnect()
        logger.info("Bot stopped. State saved. Position held if open.")
        logger.info("Restart bot.py to resume.")


# ============================================================
# Entry Point
# ============================================================

if __name__ == '__main__':
    setup_logging()

    # Quick dependency check
    try:
        import ib_insync
        import pytz
    except ImportError as e:
        print(f"\nMissing dependency: {e}")
        print("Run: pip install ib_insync pytz")
        sys.exit(1)

    bot = MESBot()
    bot.start()