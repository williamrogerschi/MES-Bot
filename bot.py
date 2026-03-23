# ============================================================
# bot.py — Main Bot Loop (v5 — thread-safe status printing)
# ============================================================

import logging
import time
import sys
import queue
import threading
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
    record_buy, record_sell, get_unrealized_pnl
)

# ── Logging Setup ─────────────────────────────────────────────
def setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # File handler — flush after every write
    fh = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8', delay=False)
    fh.setLevel(level)
    fh.setFormatter(fmt)
    fh.flush = lambda: fh.stream.flush()  # Force flush on every record

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = []
    root.addHandler(fh)
    import atexit
    atexit.register(logging.shutdown)
    root.addHandler(ch)

    # Force flush after every log record
    logging.raiseExceptions = False
    for handler in root.handlers:
        handler.flush()

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')

def now_et():
    return datetime.now(ET)


# ============================================================
# Thread-safe status printer
# ============================================================

_print_lock = threading.Lock()

def print_status(state, current_price=None):
    """Builds the full status block as one string and prints atomically."""
    lines = []
    lines.append("\n" + "="*52)
    lines.append("  MES BOT STATUS")
    lines.append("="*52)
    lines.append(f"  Active:         {state.get('is_active', False)}")
    lines.append(f"  Grid Level:     {state.get('grid_level', 0)}")
    lines.append(f"  Total Qty:      {state.get('total_qty', 0)} contracts")

    avg = state.get('average_cost')
    low = state.get('lowest_buy_price')
    lines.append(f"  Avg Cost:       {avg:.2f}" if avg else "  Avg Cost:       —")
    lines.append(f"  Lowest Buy:     {low:.2f}" if low else "  Lowest Buy:     —")

    if current_price:
        upnl = get_unrealized_pnl(state, current_price)
        lines.append(f"  Current Price:  {current_price:.2f}")
        lines.append(f"  Unrealized PnL: ${upnl:.2f}")

    lines.append(f"  Realized PnL:   ${state.get('realized_pnl', 0.0):.2f}")
    lines.append(f"  Profit Reserve: ${state.get('profit_reserve', 0.0):.2f}")
    lines.append(f"  Last Action:    {state.get('last_action', '—')}")

    last_sell = state.get('last_sell_price')
    if last_sell and not state.get('is_active'):
        reentry = last_sell * (1 - 0.012)
        lines.append(f"  Re-entry At:    {reentry:.2f} (1.2% below sell {last_sell:.2f})")

    if state.get('is_active') and low:
        from config import GRID_PCT
        sell_t = low * (1 + GRID_PCT)
        dip_t  = low * (1 - GRID_PCT)
        lines.append(f"  Sell Trigger:   {sell_t:.2f}")
        lines.append(f"  Dip Trigger:    {dip_t:.2f}")

    lines.append("="*52 + "\n")

    with _print_lock:
        print("\n".join(lines), flush=True)


# ============================================================
# Reconciliation
# ============================================================

def reconcile_state_with_broker(state, broker):
    logger.info("Reconciling saved state with IBKR positions...")
    live_positions = broker.get_open_positions()

    saved_qty = state.get('total_qty', 0)
    live_qty  = sum(p['qty'] for p in live_positions) if live_positions else 0

    if saved_qty == live_qty:
        logger.info(f"Reconciliation OK: both show {live_qty} contracts held.")
        return state

    logger.warning(
        f"MISMATCH: state={saved_qty} contracts, IBKR={live_qty} contracts."
    )

    if live_qty == 0:
        logger.warning("IBKR is flat — resetting state.")
        return reset_state()

    if live_qty > 0:
        logger.warning(f"Reconstructing state from IBKR: {live_qty} contracts.")
        avg_cost = live_positions[0]['avg_cost'] if live_positions else None
        if avg_cost:
            price_in_points = avg_cost / 5.0
            state = reset_state()
            state = record_buy(state, price_in_points, live_qty)
            save_state(state)
            logger.info(f"Reconstructed: {live_qty} @ {price_in_points:.2f}")

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
        logger.info("="*52)
        logger.info("  MES Grid Bot Starting")
        logger.info("="*52)

        if not self.broker.connect():
            logger.error("Cannot start — failed to connect to IB Gateway.")
            sys.exit(1)

        self.state = reconcile_state_with_broker(self.state, self.broker)

        price = self.broker.get_current_price()
        if price:
            self._last_price = price

        print_status(self.state, price)

        # Immediate entry check on startup if flat
        if not self.state['is_active'] and price:
            logger.info("Flat on startup — checking entry conditions.")
            if self.state.get('weekend_closed', False):
                # Bot missed Sunday open — buy immediately on next startup
                self.state['weekend_closed'] = False
                save_state(self.state)
                self._action_queue.put((ACTION_BUY_INIT, INITIAL_QTY, "Post-weekend restart — entering immediately"))
                self._pending_action = True
                logger.info("Post-weekend restart — queued immediate BUY_INIT")
            else:
                action, qty, reason = evaluate(self.state, price)
                if action in (ACTION_BUY_INIT, ACTION_BUY_REENTER):
                    self._action_queue.put((action, qty, reason))
                    self._pending_action = True
                    logger.info(f"Startup entry queued: {action} x{qty}")

        self.broker.start_price_stream(self._on_price_tick)

        self.running = True
        logger.info("Bot running. Press Ctrl+C to stop.")

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

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Ctrl+C received — shutting down.")
            self._shutdown()

    # ----------------------------------------------------------
    # Price Callback — queue only, never trade directly
    # ----------------------------------------------------------

    def _on_price_tick(self, price):
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
    # Action Queue Processor
    # ----------------------------------------------------------

    def _process_action_queue(self):
        try:
            action, qty, reason = self._action_queue.get_nowait()
        except queue.Empty:
            return

        logger.info(f"Executing: {action} x{qty}")

        try:
            if action in (ACTION_BUY_INIT, ACTION_BUY_REENTER, ACTION_BUY_AVG):
                self._execute_buy(qty)
            elif action == ACTION_SELL_ALL:
                self._execute_sell(qty, self.state['grid_level'])
        except Exception as e:
            logger.error(f"Order execution error [{action}]: {e}")
        finally:
            self._pending_action = False

    # ----------------------------------------------------------
    # Order Execution
    # ----------------------------------------------------------

    def _execute_buy(self, qty):
        filled_price = self.broker.buy(qty)
        if filled_price:
            self.state = record_buy(self.state, filled_price, qty)
            save_state(self.state)
            logger.info(f"✅ BUY {qty} @ {filled_price:.2f}")
            print_status(self.state, self._last_price)
        else:
            logger.error(f"Buy failed for {qty} contracts — will retry on next tick")

    def _execute_sell(self, qty, grid_level):
        filled_price = self.broker.sell(qty)
        if filled_price:
            self.state = record_sell(
                self.state, filled_price, qty, PROFIT_RESERVE_PCT
            )
            save_state(self.state)
            logger.info(
                f"✅ SELL {qty} @ {filled_price:.2f} | "
                f"PnL: ${self.state['realized_pnl']:.2f} | "
                f"Reserve: ${self.state['profit_reserve']:.2f}"
            )
            print_status(self.state, self._last_price)

            if grid_level > 0:
                # Averaged-down position — rebuy 1 immediately
                time.sleep(1)
                logger.info("Averaged-down position closed — re-entering with 1 contract.")
                new_price = self.broker.buy(INITIAL_QTY)
                if new_price:
                    self.state = record_buy(self.state, new_price, INITIAL_QTY)
                    save_state(self.state)
                    logger.info(f"✅ RE-ENTRY 1 @ {new_price:.2f}")
                    print_status(self.state, self._last_price)
                else:
                    logger.error("Re-entry failed — flat, will re-enter on next tick")
            else:
                reentry = filled_price * (1 - 0.012)
                logger.info(
                    f"Single contract win — now flat. "
                    f"Re-entry trigger: {reentry:.2f}"
                )
        else:
            logger.error(f"Sell failed for {qty} contracts")

    # ----------------------------------------------------------
    # Weekly Schedule
    # ----------------------------------------------------------

    def _check_schedule(self):
        now = now_et()

        if should_close_for_weekend(now):
            if not self._weekend_closed:
                logger.info("WEEKLY CLOSE: Friday 3:59:55 PM ET")
                while not self._action_queue.empty():
                    self._action_queue.get_nowait()
                self._pending_action = False

                if self.state['is_active']:
                    filled = self.broker.close_all_positions()
                    if filled:
                        self.state = record_sell(
                            self.state, filled,
                            self.state['total_qty'], PROFIT_RESERVE_PCT
                        )
                        # FIX: Set last_sell_price to filled price instead of None.
                        # Previously set to None which caused strategy.py to treat
                        # the next price tick as a fresh start and immediately rebuy.
                        # Now set to filled price so the 1.2% drop wait is enforced
                        # after Friday close. Sunday open bypasses this via
                        # should_open_for_week() which buys directly at 5 PM ET.
                        self.state['last_sell_price'] = filled
                        self.state['weekend_closed'] = True
                        save_state(self.state)
                        logger.info(
                            f"Weekly close done. "
                            f"PnL: ${self.state['realized_pnl']:.2f} | "
                            f"Reserve: ${self.state['profit_reserve']:.2f}"
                        )
                        print_status(self.state, self._last_price)
                else:
                    logger.info("Friday close — no active position.")

                self._weekend_closed = True
                self._week_opened    = False

        elif should_open_for_week(now):
            if not self._week_opened:
                logger.info("WEEKLY OPEN: Sunday 5:00 PM ET")
                if not self.state['is_active']:
                    self.state['weekend_closed'] = False
                    price = self.broker.buy(INITIAL_QTY)
                    if price:
                        self.state = record_buy(self.state, price, INITIAL_QTY)
                        save_state(self.state)
                        logger.info(f"Weekly open: 1 @ {price:.2f}")
                        print_status(self.state, self._last_price)
                else:
                    logger.info("Sunday open — already holding, skipping.")

                self._week_opened    = True
                self._weekend_closed = False

    # ----------------------------------------------------------
    # Shutdown
    # ----------------------------------------------------------

    def _shutdown(self):
        logger.info("Shutting down — saving state...")
        save_state(self.state)
        print_status(self.state, self._last_price)
        self.broker.disconnect()
        logger.info("Bot stopped. Position held. Restart bot.py to resume.")


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