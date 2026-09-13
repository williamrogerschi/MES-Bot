# ============================================================
# bot.py — Main Bot Loop (v7 — three-branch Friday close + quarterly roll,
#          roll now flattens-and-waits for normal Sunday-open re-entry)
# ============================================================

import logging
import time
import sys
import queue
import threading
from datetime import datetime, timedelta
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
    ACTION_BUY_AVG, ACTION_SELL_ALL, ACTION_SELL_AND_REBUY,
    ACTION_SELL_SINGLE, ACTION_HOLD
)
from state import (
    load_state, save_state, reset_state,
    record_buy, record_sell, record_lot_sell_and_rebuy,
    record_lot_sell_single, record_partial_sell_fifo,
    get_unrealized_pnl
)
import contract_roll as cr

# ── Logging Setup ─────────────────────────────────────────────
def setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    fh = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8', delay=False)
    fh.setLevel(level)
    fh.setFormatter(fmt)
    fh.flush = lambda: fh.stream.flush()

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
    from config import GRID_PCT
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

    if state.get('is_active') and state.get('buys'):
        lines.append("  Lots:")
        for i, lot in enumerate(state['buys']):
            trigger = lot['price'] * (1 + GRID_PCT)
            action = "sell+rebuy" if lot['qty'] >= 2 else "sell only"
            lines.append(f"    [{i}] {lot['qty']} @ {lot['price']:.2f} → sell {trigger:.2f} ({action})")
        if low:
            dip_t = low * (1 - GRID_PCT)
            lines.append(f"  Dip Trigger:    {dip_t:.2f}")
    elif state.get('last_sell_price') and not state.get('is_active'):
        last_sell = state.get('last_sell_price')
        reentry = last_sell * (1 - GRID_PCT)
        lines.append(f"  Re-entry At:    {reentry:.2f} (1.2% below sell {last_sell:.2f})")

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

    logger.warning(f"MISMATCH: state={saved_qty} contracts, IBKR={live_qty} contracts.")

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

    def start(self):
        logger.info("="*52)
        logger.info("  MES Grid Bot Starting")
        logger.info("="*52)
        logger.info(cr.describe(now_et().date()))

        # Startup connect. For no-touch deployment, if IB Gateway isn't up yet
        # (e.g. machine just rebooted and Gateway is still launching), wait and
        # keep trying rather than exiting — exiting would just bounce the
        # supervisor in a fast restart loop. Backoff caps at 60s between rounds.
        startup_delay = 5.0
        while not self.broker.connect():
            logger.warning(
                f"IB Gateway not available at startup — retrying in {startup_delay:.0f}s. "
                f"(Is IB Gateway/TWS running with the API port open?)"
            )
            time.sleep(startup_delay)
            startup_delay = min(startup_delay * 2.0, 60.0)

        # Ensure the broker is on the correct front-month contract. If the
        # machine was off across a roll boundary, switch before reconciling.
        expected_expiry = cr.contract_expiry_str(now_et().date())
        if self.broker.current_expiry_str() != expected_expiry:
            logger.warning(
                f"Startup: broker on {self.broker.current_expiry_str()}, "
                f"resolver expects {expected_expiry} — switching contract."
            )
            self.broker.set_contract(expected_expiry)

        self.state = reconcile_state_with_broker(self.state, self.broker)

        price = self.broker.get_current_price()
        if price:
            self._last_price = price

        print_status(self.state, price)

        if not self.state['is_active'] and price:
            logger.info("Flat on startup — checking entry conditions.")

            if self.state.get('weekend_closed', False):
                # Post-weekend restart via Friday close (normal flatten OR roll)
                self.state['weekend_closed'] = False
                save_state(self.state)
                self._action_queue.put((ACTION_BUY_INIT, INITIAL_QTY, "Post-weekend restart — entering immediately"))
                self._pending_action = True
                logger.info("Post-weekend restart — queued immediate BUY_INIT")

            elif self.state.get('last_sell_price') and now_et().weekday() in (0, 1, 2, 3, 4):
                # Only override re-entry trigger if the sell happened in a PREVIOUS week.
                sell_from_previous_week = False
                last_action_time = self.state.get('last_action_time')
                if last_action_time:
                    try:
                        sell_dt = datetime.fromisoformat(last_action_time)
                        now = now_et()
                        sell_week = sell_dt.isocalendar()[1]
                        sell_year = sell_dt.isocalendar()[0]
                        now_week  = now.isocalendar()[1]
                        now_year  = now.isocalendar()[0]
                        sell_from_previous_week = (sell_year, sell_week) < (now_year, now_week)
                    except Exception as e:
                        logger.warning(f"Could not parse last_action_time: {e} — defaulting to wait for re-entry")

                if sell_from_previous_week:
                    logger.info(
                        f"Flat on weekday with stale re-entry trigger from previous week "
                        f"({self.state['last_sell_price']:.2f}) — buying at market and resetting"
                    )
                    self.state['last_sell_price'] = None
                    save_state(self.state)
                    self._action_queue.put((ACTION_BUY_INIT, INITIAL_QTY, "New week startup — missed re-entry, buying at market"))
                    self._pending_action = True
                else:
                    logger.info(
                        f"Flat with re-entry trigger from this week "
                        f"({self.state['last_sell_price']:.2f} → {self.state['last_sell_price'] * 0.988:.2f}) "
                        f"— waiting for dip"
                    )

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

    def _run_loop(self):
        while self.running:
            try:
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
                return

            except Exception as e:
                # A single bad cycle must NOT kill the bot. Log it and keep
                # looping — next cycle will reconnect/reconcile as needed.
                # (The circuit breaker in _execute_buy still halts on the one
                # condition where continuing would be dangerous.)
                logger.error(f"Loop cycle error (continuing): {type(e).__name__}: {e}")
                time.sleep(1)

    def _on_price_tick(self, price):
        try:
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
        except Exception as e:
            # Never let a bad tick propagate and kill the stream/process.
            logger.error(f"Price tick error (ignored): {type(e).__name__}: {e}")

    def _process_action_queue(self):
        try:
            action, qty, reason = self._action_queue.get_nowait()
        except queue.Empty:
            return

        logger.info(f"Executing: {action} x{qty}")

        try:
            if action in (ACTION_BUY_INIT, ACTION_BUY_REENTER, ACTION_BUY_AVG):
                self._execute_buy(qty)
            elif action == ACTION_SELL_AND_REBUY:
                self._execute_sell_and_rebuy(qty)
            elif action == ACTION_SELL_SINGLE:
                self._execute_sell_single(qty)
            elif action == ACTION_SELL_ALL:
                self._execute_sell(qty, self.state['grid_level'])
        except Exception as e:
            logger.error(f"Order execution error [{action}]: {e}")
        finally:
            self._pending_action = False

    def _execute_buy(self, qty):
        filled_price = self.broker.buy(qty)
        if filled_price:
            prev_qty = self.state.get('total_qty', 0)
            self.state = record_buy(self.state, filled_price, qty)
            save_state(self.state)

            # Circuit breaker: a buy filled, so the position MUST now be active
            # with a larger qty. If it isn't, the state write failed (e.g. a
            # corrupt lot) — halt rather than let the flat state trigger an
            # infinite re-buy loop on the next tick.
            if not self.state.get('is_active') or self.state.get('total_qty', 0) <= prev_qty:
                logger.error(
                    f"FATAL: buy filled @ {filled_price:.2f} but state did not register "
                    f"(is_active={self.state.get('is_active')}, total_qty={self.state.get('total_qty')}). "
                    f"Halting bot to prevent runaway buying. Check TWS position and state.json."
                )
                self.running = False
                return

            logger.info(f"✅ BUY {qty} @ {filled_price:.2f}")
            print_status(self.state, self._last_price)
        else:
            logger.error(f"Buy failed for {qty} contracts — will retry on next tick")

    def _execute_sell_and_rebuy(self, lot_index):
        if lot_index >= len(self.state['buys']):
            logger.error(f"Lot index {lot_index} out of range — skipping")
            return

        lot = self.state['buys'][lot_index]
        qty_to_sell = lot['qty']

        sell_price = self.broker.sell(qty_to_sell)
        if not sell_price:
            logger.error(f"Sell failed for lot {lot_index} ({qty_to_sell} contracts)")
            return

        time.sleep(1)
        rebuy_price = self.broker.buy(INITIAL_QTY)
        if not rebuy_price:
            logger.error("Rebuy failed after lot sell — position partially closed")
            rebuy_price = sell_price

        self.state = record_lot_sell_and_rebuy(
            self.state, lot_index, sell_price, rebuy_price, PROFIT_RESERVE_PCT
        )
        save_state(self.state)
        logger.info(
            f"✅ SELL {qty_to_sell} @ {sell_price:.2f} + REBUY 1 @ {rebuy_price:.2f} | "
            f"PnL: ${self.state['realized_pnl']:.2f} | Reserve: ${self.state['profit_reserve']:.2f}"
        )
        print_status(self.state, self._last_price)

    def _execute_sell_single(self, lot_index):
        if lot_index >= len(self.state['buys']):
            logger.error(f"Lot index {lot_index} out of range — skipping")
            return

        lot = self.state['buys'][lot_index]
        qty_to_sell = lot['qty']

        sell_price = self.broker.sell(qty_to_sell)
        if not sell_price:
            logger.error(f"Sell failed for lot {lot_index} ({qty_to_sell} contracts)")
            return

        self.state = record_lot_sell_single(
            self.state, lot_index, sell_price, PROFIT_RESERVE_PCT
        )
        save_state(self.state)
        logger.info(
            f"✅ SELL {qty_to_sell} @ {sell_price:.2f} (no rebuy) | "
            f"PnL: ${self.state['realized_pnl']:.2f} | Reserve: ${self.state['profit_reserve']:.2f}"
        )
        print_status(self.state, self._last_price)

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
        else:
            logger.error(f"Sell failed for {qty} contracts")

    # ----------------------------------------------------------------
    def _check_schedule(self):
        now = now_et()

        if should_close_for_weekend(now):
            if not self._weekend_closed:
                # Clear queued actions / pending flag first.
                while not self._action_queue.empty():
                    self._action_queue.get_nowait()
                self._pending_action = False

                # ROLL takes priority over the normal weekly close.
                if cr.is_roll_day(now.date()):
                    logger.info("ROLL DAY: Friday 3:59:55 PM ET — contract roll")
                    self._handle_contract_roll()
                else:
                    logger.info("WEEKLY CLOSE: Friday 3:59:55 PM ET")
                    if self.state['is_active']:
                        self._handle_friday_close()
                    else:
                        logger.info("Friday close — no active position.")

                self._weekend_closed = True
                self._week_opened    = False

        elif should_open_for_week(now):
            if not self._week_opened:
                logger.info("WEEKLY OPEN: Sunday 5:00 PM ET")
                if not self.state['is_active']:
                    # Flat (flattened green on Friday, rolled on Friday, or
                    # never held) — always buy 1 at Sunday open, clear any
                    # re-entry trigger.
                    self.state['weekend_closed'] = False
                    self.state['last_sell_price'] = None
                    price = self.broker.buy(INITIAL_QTY)
                    if price:
                        self.state = record_buy(self.state, price, INITIAL_QTY)
                        save_state(self.state)
                        logger.info(f"Weekly open: 1 @ {price:.2f} (re-entry trigger cleared)")
                        print_status(self.state, self._last_price)
                    else:
                        logger.error("Weekly open buy failed")
                else:
                    # Position held over the weekend (red Friday close).
                    # Resume grid — do NOT add a contract.
                    logger.info(
                        f"Sunday open — holding {self.state['total_qty']} contract(s) "
                        f"carried over from Friday. Resuming grid, no new buy."
                    )
                    self.state['weekend_closed'] = False
                    save_state(self.state)

                self._week_opened    = True
                self._weekend_closed = False

        else:
            # CATCH-UP: the Sunday-open window (should_open_for_week) is narrow,
            # so if the process was suspended across it — machine asleep, frozen,
            # or lagging — the loop never evaluated the window while it was True
            # and the weekly buy is silently skipped. This branch fires that buy
            # late, the moment the loop resumes, instead of waiting a full week.
            #
            # Conditions (all must hold):
            #   - flat (no active position)
            #   - not currently in the weekend-close window
            #   - haven't already opened this week (_week_opened False)
            #   - it's Sunday evening or later in the trading week (not mid-close)
            #   - the last sell (if any) was in a PREVIOUS week — so we don't
            #     stomp a this-week re-entry trigger the strategy is waiting on
            # Mirrors the startup post-weekend logic so wake-from-sleep behaves
            # like a restart for this one purpose.
            if (not self.state['is_active']
                    and not self._week_opened
                    and now.weekday() in (6, 0, 1, 2, 3, 4)  # Sun-Fri trading week
                    and self._is_new_trading_week(now)):
                logger.info("MISSED WEEKLY OPEN — catch-up: flat and past Sunday open, "
                            "buying base position at market.")
                self.state['weekend_closed'] = False
                self.state['last_sell_price'] = None
                price = self.broker.buy(INITIAL_QTY)
                if price:
                    self.state = record_buy(self.state, price, INITIAL_QTY)
                    save_state(self.state)
                    logger.info(f"Catch-up open: 1 @ {price:.2f} (re-entry trigger cleared)")
                    print_status(self.state, self._last_price)
                    self._week_opened = True
                else:
                    logger.error("Catch-up weekly open buy failed")

    def _is_new_trading_week(self, now):
        """True if we've entered a new trading week since the last recorded action.
        The trading week opens Sunday 5 PM ET, but ISO weeks start Monday — so a
        Sunday belongs to the ISO week that's ending, not the new trading week.
        Shift any Sunday forward one day before computing its ISO week so Sunday
        open groups with the coming Monday. Compares against last_action_time the
        same way, so a Thursday sell reads as 'previous week' by Sunday evening.
        No last_action_time → treat as new week (safe: flat and past open)."""
        def trading_week(dt):
            # Sunday (weekday 6) counts as the next trading week.
            shifted = dt + timedelta(days=1) if dt.weekday() == 6 else dt
            return (shifted.isocalendar()[0], shifted.isocalendar()[1])

        last_action_time = self.state.get('last_action_time')
        if not last_action_time:
            return True
        try:
            last_dt = datetime.fromisoformat(last_action_time)
            return trading_week(last_dt) < trading_week(now)
        except Exception as e:
            logger.warning(f"_is_new_trading_week parse error: {e} — treating as new week")
            return True

    def _handle_friday_close(self):
        """
        Normal Friday close (NON-roll weeks). Three branches:

          1. GREEN (uPnL >= 0)          → flatten all. Sunday rebuys 1.
          2. RED and 2+ contracts       → sell 1 FIFO (oldest/furthest under),
                                          hold the rest over the weekend.
          3. RED and exactly 1 contract → hold the single contract.
        """
        price = self._last_price
        if price is None:
            logger.error("Friday close — no price available. Retry next tick.")
            self._weekend_closed = False
            return

        upnl = get_unrealized_pnl(self.state, price)
        total_qty = self.state['total_qty']

        # Branch 1: GREEN — flatten
        if upnl >= 0:
            logger.info(f"Friday close — GREEN (uPnL ${upnl:.2f}) — flattening {total_qty} contract(s).")
            filled = self.broker.close_all_positions()
            if filled:
                self.state = record_sell(self.state, filled, total_qty, PROFIT_RESERVE_PCT)
                self.state['last_sell_price'] = filled
                self.state['weekend_closed'] = True
                save_state(self.state)
                logger.info(f"Friday GREEN close done. Realized ${self.state['realized_pnl']:.2f}")
                print_status(self.state, price)
            else:
                logger.error("Friday GREEN close FAILED — retry next tick")
                self._weekend_closed = False
            return

        # Branch 2: RED with 2+ — sell 1 FIFO, hold rest
        if total_qty >= 2:
            logger.info(
                f"Friday close — RED (uPnL ${upnl:.2f}) with {total_qty} — "
                f"selling 1 FIFO, holding {total_qty - 1} over weekend."
            )
            filled = self.broker.sell(1)
            if filled:
                self.state = record_partial_sell_fifo(self.state, filled, PROFIT_RESERVE_PCT)
                self.state['weekend_closed'] = False
                save_state(self.state)
                logger.info(f"Friday RED partial done. Holding {self.state['total_qty']} over weekend.")
                print_status(self.state, price)
            else:
                logger.error("Friday RED partial sell FAILED — retry next tick")
                self._weekend_closed = False
            return

        # Branch 3: RED with 1 — hold
        logger.info(f"Friday close — RED (uPnL ${upnl:.2f}) with 1 contract — holding over weekend.")
        self.state['weekend_closed'] = False
        save_state(self.state)
        print_status(self.state, price)

    # ----------------------------------------------------------------
    def _handle_contract_roll(self):
        """
        ROLL DAY (second Friday of expiry month, at the Friday close).

        Close ALL contracts in the expiring contract and switch the broker
        to the new front-month contract. Do NOT reopen here — the position
        stays FLAT over the weekend, exactly like a green Friday close.
        The normal Sunday-open path (should_open_for_week) — or the
        post-weekend startup path, via weekend_closed — buys INITIAL_QTY
        (1 contract) fresh in the new contract, and the grid ladder
        rebuilds from there like any other week. This happens regardless
        of red/green PnL, since holding into the new contract without a
        fresh entry decision doesn't make sense — the old price levels
        (lot prices, dip trigger, sell triggers) belong to the expiring
        contract and carry no meaning in the new one.
        """
        old_expiry = self.broker.current_expiry_str()
        new_expiry = cr.contract_expiry_str(now_et().date() + timedelta(days=1))

        was_active = self.state['is_active']
        qty_closed = self.state['total_qty'] if was_active else 0

        # 1) Close everything in the OLD contract.
        if was_active:
            filled = self.broker.close_all_positions()
            if not filled:
                logger.error("ROLL: close of old contract FAILED — will retry next tick")
                self._weekend_closed = False
                return
            self.state = record_sell(
                self.state, filled, qty_closed, PROFIT_RESERVE_PCT
            )
            logger.info(
                f"ROLL: closed {qty_closed} contract(s) of {old_expiry} @ {filled:.2f} | "
                f"Realized: ${self.state['realized_pnl']:.2f}"
            )

        # No re-entry trigger carries over — the old contract's price level
        # is meaningless in the new contract (different absolute price due
        # to calendar spread/carry).
        self.state['last_sell_price'] = None

        # 2) Point the broker at the NEW contract.
        if not self.broker.set_contract(new_expiry):
            logger.error(f"ROLL: failed to switch broker to {new_expiry} — position FLAT, manual check needed")
            self.state['weekend_closed'] = True
            save_state(self.state)
            return
        logger.info(f"ROLL: broker now on contract {new_expiry} (was {old_expiry})")

        # 3) Stay flat. Sunday-open (or post-weekend startup) logic buys
        # INITIAL_QTY fresh — same treatment as a normal green-Friday flatten.
        self.state['weekend_closed'] = True
        save_state(self.state)
        logger.info(
            f"ROLL COMPLETE: flattened {qty_closed} contract(s), switched to {new_expiry}. "
            f"Staying flat over weekend — Sunday open buys {INITIAL_QTY} fresh."
        )
        print_status(self.state, self._last_price)

    # ----------------------------------------------------------------
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
    bot.start()# ============================================================
# bot.py — Main Bot Loop (v7 — three-branch Friday close + quarterly roll,
#          roll now flattens-and-waits for normal Sunday-open re-entry)
# ============================================================

import logging
import time
import sys
import queue
import threading
from datetime import datetime, timedelta
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
    ACTION_BUY_AVG, ACTION_SELL_ALL, ACTION_SELL_AND_REBUY,
    ACTION_SELL_SINGLE, ACTION_HOLD
)
from state import (
    load_state, save_state, reset_state, reset_position_only,
    record_buy, record_sell, record_lot_sell_and_rebuy,
    record_lot_sell_single, record_partial_sell_fifo,
    get_unrealized_pnl
)
import contract_roll as cr

# ── Logging Setup ─────────────────────────────────────────────
def setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    fh = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8', delay=False)
    fh.setLevel(level)
    fh.setFormatter(fmt)
    fh.flush = lambda: fh.stream.flush()

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
    from config import GRID_PCT
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

    if state.get('is_active') and state.get('buys'):
        lines.append("  Lots:")
        for i, lot in enumerate(state['buys']):
            trigger = lot['price'] * (1 + GRID_PCT)
            action = "sell+rebuy" if lot['qty'] >= 2 else "sell only"
            lines.append(f"    [{i}] {lot['qty']} @ {lot['price']:.2f} → sell {trigger:.2f} ({action})")
        if low:
            dip_t = low * (1 - GRID_PCT)
            lines.append(f"  Dip Trigger:    {dip_t:.2f}")
    elif state.get('last_sell_price') and not state.get('is_active'):
        last_sell = state.get('last_sell_price')
        reentry = last_sell * (1 - GRID_PCT)
        lines.append(f"  Re-entry At:    {reentry:.2f} (1.2% below sell {last_sell:.2f})")

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

    logger.warning(f"MISMATCH: state={saved_qty} contracts, IBKR={live_qty} contracts.")

    # NOTE: a position-count mismatch is a bookkeeping issue about the
    # CURRENT position only (e.g. a roll or restart landed state.json in a
    # stale spot). It must never touch lifetime stats like realized_pnl or
    # profit_reserve — reset_position_only() clears position fields and
    # explicitly preserves those. reset_state() (full wipe) is reserved for
    # a deliberate, explicit full reset and is never called from here.
    if live_qty == 0:
        logger.warning("IBKR is flat — resetting position fields (history preserved).")
        return reset_position_only(state)

    if live_qty > 0:
        logger.warning(f"Reconstructing state from IBKR: {live_qty} contracts.")
        avg_cost = live_positions[0]['avg_cost'] if live_positions else None
        if avg_cost:
            price_in_points = avg_cost / 5.0
            state = reset_position_only(state)
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

    def start(self):
        logger.info("="*52)
        logger.info("  MES Grid Bot Starting")
        logger.info("="*52)
        logger.info(cr.describe(now_et().date()))

        # Startup connect. For no-touch deployment, if IB Gateway isn't up yet
        # (e.g. machine just rebooted and Gateway is still launching), wait and
        # keep trying rather than exiting — exiting would just bounce the
        # supervisor in a fast restart loop. Backoff caps at 60s between rounds.
        startup_delay = 5.0
        while not self.broker.connect():
            logger.warning(
                f"IB Gateway not available at startup — retrying in {startup_delay:.0f}s. "
                f"(Is IB Gateway/TWS running with the API port open?)"
            )
            time.sleep(startup_delay)
            startup_delay = min(startup_delay * 2.0, 60.0)

        # Ensure the broker is on the correct front-month contract. If the
        # machine was off across a roll boundary, switch before reconciling.
        expected_expiry = cr.contract_expiry_str(now_et().date())
        if self.broker.current_expiry_str() != expected_expiry:
            logger.warning(
                f"Startup: broker on {self.broker.current_expiry_str()}, "
                f"resolver expects {expected_expiry} — switching contract."
            )
            self.broker.set_contract(expected_expiry)

        self.state = reconcile_state_with_broker(self.state, self.broker)

        price = self.broker.get_current_price()
        if price:
            self._last_price = price

        print_status(self.state, price)

        if not self.state['is_active'] and price:
            logger.info("Flat on startup — checking entry conditions.")

            if self.state.get('weekend_closed', False):
                # Post-weekend restart via Friday close (normal flatten OR roll)
                self.state['weekend_closed'] = False
                save_state(self.state)
                self._action_queue.put((ACTION_BUY_INIT, INITIAL_QTY, "Post-weekend restart — entering immediately"))
                self._pending_action = True
                logger.info("Post-weekend restart — queued immediate BUY_INIT")

            elif self.state.get('last_sell_price') and now_et().weekday() in (0, 1, 2, 3, 4):
                # Only override re-entry trigger if the sell happened in a PREVIOUS week.
                sell_from_previous_week = False
                last_action_time = self.state.get('last_action_time')
                if last_action_time:
                    try:
                        sell_dt = datetime.fromisoformat(last_action_time)
                        now = now_et()
                        sell_week = sell_dt.isocalendar()[1]
                        sell_year = sell_dt.isocalendar()[0]
                        now_week  = now.isocalendar()[1]
                        now_year  = now.isocalendar()[0]
                        sell_from_previous_week = (sell_year, sell_week) < (now_year, now_week)
                    except Exception as e:
                        logger.warning(f"Could not parse last_action_time: {e} — defaulting to wait for re-entry")

                if sell_from_previous_week:
                    logger.info(
                        f"Flat on weekday with stale re-entry trigger from previous week "
                        f"({self.state['last_sell_price']:.2f}) — buying at market and resetting"
                    )
                    self.state['last_sell_price'] = None
                    save_state(self.state)
                    self._action_queue.put((ACTION_BUY_INIT, INITIAL_QTY, "New week startup — missed re-entry, buying at market"))
                    self._pending_action = True
                else:
                    logger.info(
                        f"Flat with re-entry trigger from this week "
                        f"({self.state['last_sell_price']:.2f} → {self.state['last_sell_price'] * 0.988:.2f}) "
                        f"— waiting for dip"
                    )

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

    def _run_loop(self):
        while self.running:
            try:
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
                return

            except Exception as e:
                # A single bad cycle must NOT kill the bot. Log it and keep
                # looping — next cycle will reconnect/reconcile as needed.
                # (The circuit breaker in _execute_buy still halts on the one
                # condition where continuing would be dangerous.)
                logger.error(f"Loop cycle error (continuing): {type(e).__name__}: {e}")
                time.sleep(1)

    def _on_price_tick(self, price):
        try:
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
        except Exception as e:
            # Never let a bad tick propagate and kill the stream/process.
            logger.error(f"Price tick error (ignored): {type(e).__name__}: {e}")

    def _process_action_queue(self):
        try:
            action, qty, reason = self._action_queue.get_nowait()
        except queue.Empty:
            return

        logger.info(f"Executing: {action} x{qty}")

        try:
            if action in (ACTION_BUY_INIT, ACTION_BUY_REENTER, ACTION_BUY_AVG):
                self._execute_buy(qty)
            elif action == ACTION_SELL_AND_REBUY:
                self._execute_sell_and_rebuy(qty)
            elif action == ACTION_SELL_SINGLE:
                self._execute_sell_single(qty)
            elif action == ACTION_SELL_ALL:
                self._execute_sell(qty, self.state['grid_level'])
        except Exception as e:
            logger.error(f"Order execution error [{action}]: {e}")
        finally:
            self._pending_action = False

    def _execute_buy(self, qty):
        filled_price = self.broker.buy(qty)
        if filled_price:
            prev_qty = self.state.get('total_qty', 0)
            self.state = record_buy(self.state, filled_price, qty)
            save_state(self.state)

            # Circuit breaker: a buy filled, so the position MUST now be active
            # with a larger qty. If it isn't, the state write failed (e.g. a
            # corrupt lot) — halt rather than let the flat state trigger an
            # infinite re-buy loop on the next tick.
            if not self.state.get('is_active') or self.state.get('total_qty', 0) <= prev_qty:
                logger.error(
                    f"FATAL: buy filled @ {filled_price:.2f} but state did not register "
                    f"(is_active={self.state.get('is_active')}, total_qty={self.state.get('total_qty')}). "
                    f"Halting bot to prevent runaway buying. Check TWS position and state.json."
                )
                self.running = False
                return

            logger.info(f"✅ BUY {qty} @ {filled_price:.2f}")
            print_status(self.state, self._last_price)
        else:
            logger.error(f"Buy failed for {qty} contracts — will retry on next tick")

    def _execute_sell_and_rebuy(self, lot_index):
        if lot_index >= len(self.state['buys']):
            logger.error(f"Lot index {lot_index} out of range — skipping")
            return

        lot = self.state['buys'][lot_index]
        qty_to_sell = lot['qty']

        sell_price = self.broker.sell(qty_to_sell)
        if not sell_price:
            logger.error(f"Sell failed for lot {lot_index} ({qty_to_sell} contracts)")
            return

        time.sleep(1)
        rebuy_price = self.broker.buy(INITIAL_QTY)
        if not rebuy_price:
            logger.error("Rebuy failed after lot sell — position partially closed")
            rebuy_price = sell_price

        self.state = record_lot_sell_and_rebuy(
            self.state, lot_index, sell_price, rebuy_price, PROFIT_RESERVE_PCT
        )
        save_state(self.state)
        logger.info(
            f"✅ SELL {qty_to_sell} @ {sell_price:.2f} + REBUY 1 @ {rebuy_price:.2f} | "
            f"PnL: ${self.state['realized_pnl']:.2f} | Reserve: ${self.state['profit_reserve']:.2f}"
        )
        print_status(self.state, self._last_price)

    def _execute_sell_single(self, lot_index):
        if lot_index >= len(self.state['buys']):
            logger.error(f"Lot index {lot_index} out of range — skipping")
            return

        lot = self.state['buys'][lot_index]
        qty_to_sell = lot['qty']

        sell_price = self.broker.sell(qty_to_sell)
        if not sell_price:
            logger.error(f"Sell failed for lot {lot_index} ({qty_to_sell} contracts)")
            return

        self.state = record_lot_sell_single(
            self.state, lot_index, sell_price, PROFIT_RESERVE_PCT
        )
        save_state(self.state)
        logger.info(
            f"✅ SELL {qty_to_sell} @ {sell_price:.2f} (no rebuy) | "
            f"PnL: ${self.state['realized_pnl']:.2f} | Reserve: ${self.state['profit_reserve']:.2f}"
        )
        print_status(self.state, self._last_price)

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
        else:
            logger.error(f"Sell failed for {qty} contracts")

    # ----------------------------------------------------------------
    def _check_schedule(self):
        now = now_et()

        if should_close_for_weekend(now):
            if not self._weekend_closed:
                # Clear queued actions / pending flag first.
                while not self._action_queue.empty():
                    self._action_queue.get_nowait()
                self._pending_action = False

                # ROLL takes priority over the normal weekly close.
                if cr.is_roll_day(now.date()):
                    logger.info("ROLL DAY: Friday 3:59:55 PM ET — contract roll")
                    self._handle_contract_roll()
                else:
                    logger.info("WEEKLY CLOSE: Friday 3:59:55 PM ET")
                    if self.state['is_active']:
                        self._handle_friday_close()
                    else:
                        logger.info("Friday close — no active position.")

                self._weekend_closed = True
                self._week_opened    = False

        elif should_open_for_week(now):
            if not self._week_opened:
                logger.info("WEEKLY OPEN: Sunday 5:00 PM ET")
                if not self.state['is_active']:
                    # Flat (flattened green on Friday, rolled on Friday, or
                    # never held) — always buy 1 at Sunday open, clear any
                    # re-entry trigger.
                    self.state['weekend_closed'] = False
                    self.state['last_sell_price'] = None
                    price = self.broker.buy(INITIAL_QTY)
                    if price:
                        self.state = record_buy(self.state, price, INITIAL_QTY)
                        save_state(self.state)
                        logger.info(f"Weekly open: 1 @ {price:.2f} (re-entry trigger cleared)")
                        print_status(self.state, self._last_price)
                    else:
                        logger.error("Weekly open buy failed")
                else:
                    # Position held over the weekend (red Friday close).
                    # Resume grid — do NOT add a contract.
                    logger.info(
                        f"Sunday open — holding {self.state['total_qty']} contract(s) "
                        f"carried over from Friday. Resuming grid, no new buy."
                    )
                    self.state['weekend_closed'] = False
                    save_state(self.state)

                self._week_opened    = True
                self._weekend_closed = False

        else:
            # CATCH-UP: the Sunday-open window (should_open_for_week) is narrow,
            # so if the process was suspended across it — machine asleep, frozen,
            # or lagging — the loop never evaluated the window while it was True
            # and the weekly buy is silently skipped. This branch fires that buy
            # late, the moment the loop resumes, instead of waiting a full week.
            #
            # Conditions (all must hold):
            #   - flat (no active position)
            #   - not currently in the weekend-close window
            #   - haven't already opened this week (_week_opened False)
            #   - it's Sunday evening or later in the trading week (not mid-close)
            #   - the last sell (if any) was in a PREVIOUS week — so we don't
            #     stomp a this-week re-entry trigger the strategy is waiting on
            # Mirrors the startup post-weekend logic so wake-from-sleep behaves
            # like a restart for this one purpose.
            if (not self.state['is_active']
                    and not self._week_opened
                    and now.weekday() in (6, 0, 1, 2, 3, 4)  # Sun-Fri trading week
                    and self._is_new_trading_week(now)):
                logger.info("MISSED WEEKLY OPEN — catch-up: flat and past Sunday open, "
                            "buying base position at market.")
                self.state['weekend_closed'] = False
                self.state['last_sell_price'] = None
                price = self.broker.buy(INITIAL_QTY)
                if price:
                    self.state = record_buy(self.state, price, INITIAL_QTY)
                    save_state(self.state)
                    logger.info(f"Catch-up open: 1 @ {price:.2f} (re-entry trigger cleared)")
                    print_status(self.state, self._last_price)
                    self._week_opened = True
                else:
                    logger.error("Catch-up weekly open buy failed")

    def _is_new_trading_week(self, now):
        """True if we've entered a new trading week since the last recorded action.
        The trading week opens Sunday 5 PM ET, but ISO weeks start Monday — so a
        Sunday belongs to the ISO week that's ending, not the new trading week.
        Shift any Sunday forward one day before computing its ISO week so Sunday
        open groups with the coming Monday. Compares against last_action_time the
        same way, so a Thursday sell reads as 'previous week' by Sunday evening.
        No last_action_time → treat as new week (safe: flat and past open)."""
        def trading_week(dt):
            # Sunday (weekday 6) counts as the next trading week.
            shifted = dt + timedelta(days=1) if dt.weekday() == 6 else dt
            return (shifted.isocalendar()[0], shifted.isocalendar()[1])

        last_action_time = self.state.get('last_action_time')
        if not last_action_time:
            return True
        try:
            last_dt = datetime.fromisoformat(last_action_time)
            return trading_week(last_dt) < trading_week(now)
        except Exception as e:
            logger.warning(f"_is_new_trading_week parse error: {e} — treating as new week")
            return True

    def _handle_friday_close(self):
        """
        Normal Friday close (NON-roll weeks). Three branches:

          1. GREEN (uPnL >= 0)          → flatten all. Sunday rebuys 1.
          2. RED and 2+ contracts       → sell 1 FIFO (oldest/furthest under),
                                          hold the rest over the weekend.
          3. RED and exactly 1 contract → hold the single contract.
        """
        price = self._last_price
        if price is None:
            logger.error("Friday close — no price available. Retry next tick.")
            self._weekend_closed = False
            return

        upnl = get_unrealized_pnl(self.state, price)
        total_qty = self.state['total_qty']

        # Branch 1: GREEN — flatten
        if upnl >= 0:
            logger.info(f"Friday close — GREEN (uPnL ${upnl:.2f}) — flattening {total_qty} contract(s).")
            filled = self.broker.close_all_positions()
            if filled:
                self.state = record_sell(self.state, filled, total_qty, PROFIT_RESERVE_PCT)
                self.state['last_sell_price'] = filled
                self.state['weekend_closed'] = True
                save_state(self.state)
                logger.info(f"Friday GREEN close done. Realized ${self.state['realized_pnl']:.2f}")
                print_status(self.state, price)
            else:
                logger.error("Friday GREEN close FAILED — retry next tick")
                self._weekend_closed = False
            return

        # Branch 2: RED with 2+ — sell 1 FIFO, hold rest
        if total_qty >= 2:
            logger.info(
                f"Friday close — RED (uPnL ${upnl:.2f}) with {total_qty} — "
                f"selling 1 FIFO, holding {total_qty - 1} over weekend."
            )
            filled = self.broker.sell(1)
            if filled:
                self.state = record_partial_sell_fifo(self.state, filled, PROFIT_RESERVE_PCT)
                self.state['weekend_closed'] = False
                save_state(self.state)
                logger.info(f"Friday RED partial done. Holding {self.state['total_qty']} over weekend.")
                print_status(self.state, price)
            else:
                logger.error("Friday RED partial sell FAILED — retry next tick")
                self._weekend_closed = False
            return

        # Branch 3: RED with 1 — hold
        logger.info(f"Friday close — RED (uPnL ${upnl:.2f}) with 1 contract — holding over weekend.")
        self.state['weekend_closed'] = False
        save_state(self.state)
        print_status(self.state, price)

    # ----------------------------------------------------------------
    def _handle_contract_roll(self):
        """
        ROLL DAY (second Friday of expiry month, at the Friday close).

        Close ALL contracts in the expiring contract and switch the broker
        to the new front-month contract. Do NOT reopen here — the position
        stays FLAT over the weekend, exactly like a green Friday close.
        The normal Sunday-open path (should_open_for_week) — or the
        post-weekend startup path, via weekend_closed — buys INITIAL_QTY
        (1 contract) fresh in the new contract, and the grid ladder
        rebuilds from there like any other week. This happens regardless
        of red/green PnL, since holding into the new contract without a
        fresh entry decision doesn't make sense — the old price levels
        (lot prices, dip trigger, sell triggers) belong to the expiring
        contract and carry no meaning in the new one.
        """
        old_expiry = self.broker.current_expiry_str()
        new_expiry = cr.contract_expiry_str(now_et().date() + timedelta(days=1))

        was_active = self.state['is_active']
        qty_closed = self.state['total_qty'] if was_active else 0

        # 1) Close everything in the OLD contract.
        if was_active:
            filled = self.broker.close_all_positions()
            if not filled:
                logger.error("ROLL: close of old contract FAILED — will retry next tick")
                self._weekend_closed = False
                return
            self.state = record_sell(
                self.state, filled, qty_closed, PROFIT_RESERVE_PCT
            )
            logger.info(
                f"ROLL: closed {qty_closed} contract(s) of {old_expiry} @ {filled:.2f} | "
                f"Realized: ${self.state['realized_pnl']:.2f}"
            )

        # No re-entry trigger carries over — the old contract's price level
        # is meaningless in the new contract (different absolute price due
        # to calendar spread/carry).
        self.state['last_sell_price'] = None

        # 2) Point the broker at the NEW contract.
        if not self.broker.set_contract(new_expiry):
            logger.error(f"ROLL: failed to switch broker to {new_expiry} — position FLAT, manual check needed")
            self.state['weekend_closed'] = True
            save_state(self.state)
            return
        logger.info(f"ROLL: broker now on contract {new_expiry} (was {old_expiry})")

        # 3) Stay flat. Sunday-open (or post-weekend startup) logic buys
        # INITIAL_QTY fresh — same treatment as a normal green-Friday flatten.
        self.state['weekend_closed'] = True
        save_state(self.state)
        logger.info(
            f"ROLL COMPLETE: flattened {qty_closed} contract(s), switched to {new_expiry}. "
            f"Staying flat over weekend — Sunday open buys {INITIAL_QTY} fresh."
        )
        print_status(self.state, self._last_price)

    # ----------------------------------------------------------------
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