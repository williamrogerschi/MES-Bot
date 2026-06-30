# ============================================================
# broker.py — IBKR Connection Layer
# Handles all communication with IB Gateway via ib_insync.
# Strategy logic never touches this directly — only bot.py does.
# ============================================================

import logging
import time
from ib_insync import IB, Future, MarketOrder, util
from config import (
    IB_HOST, IB_PORT, IB_CLIENT_ID,
    SYMBOL, EXCHANGE, CURRENCY, CONTRACT_TYPE
)
import contract_roll as cr

logger = logging.getLogger(__name__)

# Suppress ib_insync's verbose internal logging
util.logToConsole(logging.WARNING)


class Broker:
    def __init__(self):
        self.ib = IB()
        self.contract = None
        self.ticker = None
        self._price_callbacks = []
        # Active expiry is date-driven, not hardcoded. Source of truth is
        # contract_roll; config no longer carries the expiry.
        self.expiry_str = cr.contract_expiry_str()
        # Reconnect backoff state. When disconnected, attempts are spaced out
        # with exponential backoff (capped) instead of spinning every loop.
        self._reconnect_next_time = 0.0      # epoch seconds; 0 = attempt allowed now
        self._reconnect_delay = 0.0          # current backoff delay in seconds

    # ----------------------------------------------------------
    # Connection
    # ----------------------------------------------------------

    def connect(self, max_attempts=5):
        """
        Connect to IB Gateway. Tries up to max_attempts times with a short
        gap between tries. Returns True on success, False if all fail.
        Used both for the initial startup connect (max_attempts=5) and, with
        max_attempts=1, for the spaced-out reconnect path.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} (attempt {attempt}/{max_attempts})...")
                self.ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
                self.ib.reqMarketDataType(1)
                logger.info("Connected to IB Gateway.")
                self._setup_contract()
                # Reset backoff on any successful connect.
                self._reconnect_delay = 0.0
                self._reconnect_next_time = 0.0
                return True
            except Exception as e:
                logger.warning(f"Connection attempt {attempt} failed: {e}")
                if attempt < max_attempts:
                    time.sleep(5)

        logger.error(f"Could not connect to IB Gateway after {max_attempts} attempt(s).")
        return False

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway.")

    def is_connected(self):
        return self.ib.isConnected()

    def reconnect_if_needed(self):
        """
        Called every main-loop cycle. If disconnected, attempts ONE reconnect
        but only after the current backoff delay has elapsed — so an extended
        broker outage results in calm, spaced retries (5s, 10s, 20s, 40s, then
        capped at 60s) instead of spinning connect() continuously and starving
        the machine of resources.

        On a successful reconnect it re-arms the price stream so ticks resume.
        Returns True if connected (already or freshly), False if still down.
        """
        if self.ib.isConnected():
            return True

        now = time.time()
        if now < self._reconnect_next_time:
            # Still inside the backoff window — do nothing this cycle.
            return False

        logger.warning(
            f"Connection lost — reconnect attempt (backoff {self._reconnect_delay:.0f}s)..."
        )
        ok = self.connect(max_attempts=1)

        if ok:
            logger.info("Reconnected to IB Gateway.")
            # Re-arm BOTH feeds, matching start_price_stream: the ticker stream
            # and the portfolio update event. Clean up the OLD subscriptions
            # first so they don't pile up across reconnects (IBKR caps active
            # subscriptions; leaking them eventually throws Error 322 and can
            # multiply tick callbacks).
            try:
                if self._price_callbacks:
                    # Cancel any stale market-data line before re-requesting.
                    try:
                        self.ib.cancelMktData(self.contract)
                    except Exception:
                        pass
                    # Detach the portfolio handler if already attached, so we
                    # never stack duplicates (handler firing N times per update).
                    try:
                        self.ib.updatePortfolioEvent -= self._on_portfolio_update
                    except Exception:
                        pass

                    self.ticker = self.ib.reqMktData(self.contract, '233', False, False)
                    self.ticker.updateEvent += self._on_price_update
                    self.ib.updatePortfolioEvent += self._on_portfolio_update
                    logger.info(f"Price stream re-armed for {self.contract.localSymbol}")
            except Exception as e:
                logger.error(f"Reconnect: failed to re-arm price stream: {e}")
            return True

        # Failed — grow the backoff (5 → 10 → 20 → 40 → cap 60) and schedule next.
        RECONNECT_BACKOFF_CAP = 60.0
        if self._reconnect_delay <= 0.0:
            self._reconnect_delay = 5.0
        else:
            self._reconnect_delay = min(self._reconnect_delay * 2.0, RECONNECT_BACKOFF_CAP)
        self._reconnect_next_time = time.time() + self._reconnect_delay
        logger.warning(f"Reconnect failed — next attempt in {self._reconnect_delay:.0f}s.")
        return False

    # ----------------------------------------------------------
    # Contract Setup
    # ----------------------------------------------------------

    def _build_contract(self, expiry_str):
        """Construct and qualify the IBKR Future for a given YYYYMMDD expiry."""
        contract = Future(
            symbol=SYMBOL,
            lastTradeDateOrContractMonth=expiry_str,
            exchange=EXCHANGE,
            currency=CURRENCY
        )
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            logger.error(f"Could not qualify contract {SYMBOL} {expiry_str}. "
                         f"Check the expiry is correct and trading has opened.")
            return None
        return qualified[0]

    def _setup_contract(self):
        contract = self._build_contract(self.expiry_str)
        if contract is None:
            raise Exception(f"Could not qualify contract {SYMBOL} {self.expiry_str}. "
                            f"Check that the contract expiry is correct and trading has opened.")
        self.contract = contract
        logger.info(f"Contract qualified: {self.contract.localSymbol} | "
                    f"Exchange: {self.contract.exchange} | "
                    f"Expiry: {self.contract.lastTradeDateOrContractMonth}")

    def current_expiry_str(self):
        """Return the expiry string the broker is currently trading."""
        return self.expiry_str

    def set_contract(self, expiry_str):
        """
        Switch the broker to a new contract expiry (used at quarterly roll).
        Cancels the old price subscription, qualifies and arms the new
        contract, and re-subscribes the existing price callbacks.

        Caller MUST have closed any position in the old contract first —
        set_contract does not move positions.

        Returns True on success.
        """
        if expiry_str == self.expiry_str:
            logger.info(f"set_contract: already on {expiry_str}, no change.")
            return True

        new_contract = self._build_contract(expiry_str)
        if new_contract is None:
            return False

        # Tear down the old market-data subscription.
        try:
            if self.ticker is not None:
                self.ticker.updateEvent -= self._on_price_update
                self.ib.cancelMktData(self.contract)
        except Exception as e:
            logger.warning(f"set_contract: could not cancel old mkt data cleanly: {e}")

        old = self.expiry_str
        self.contract = new_contract
        self.expiry_str = expiry_str
        logger.info(f"set_contract: switched {old} -> {expiry_str} ({self.contract.localSymbol})")

        # Re-arm the price stream on the new contract if callbacks are present.
        if self._price_callbacks:
            self.ticker = self.ib.reqMktData(self.contract, '233', False, False)
            self.ticker.updateEvent += self._on_price_update
            logger.info(f"set_contract: price stream re-armed for {self.contract.localSymbol}")

        return True

    # ----------------------------------------------------------
    # Price Streaming
    # ----------------------------------------------------------

    def start_price_stream(self, callback):
        self._price_callbacks.append(callback)
        self.ticker = self.ib.reqMktData(self.contract, '233', False, False)
        self.ticker.updateEvent += self._on_price_update
        self.ib.updatePortfolioEvent += self._on_portfolio_update
        logger.info(f"Price stream started for {self.contract.localSymbol}")

    def _on_price_update(self, ticker):
        price = ticker.last
        if price and price > 0:
            for cb in self._price_callbacks:
                try:
                    cb(price)
                except Exception as e:
                    logger.error(f"Error in price callback: {e}")

    def _on_portfolio_update(self, item):
        # Match on the ACTIVE contract's expiry, not just the symbol — during
        # a roll there can be two MES contracts in the portfolio at once, and
        # we only want price ticks from the one we're currently trading.
        if (item.contract.symbol == self.contract.symbol and
                item.contract.lastTradeDateOrContractMonth == self.contract.lastTradeDateOrContractMonth and
                item.marketPrice and item.marketPrice > 0):
            for cb in self._price_callbacks:
                try:
                    cb(item.marketPrice)
                except Exception as e:
                    logger.error(f"Error in portfolio price callback: {e}")

    def get_current_price(self):
        if self.ticker and self.ticker.last and self.ticker.last > 0:
            return self.ticker.last

        ticker = self.ib.reqMktData(self.contract, '', True, False)
        self.ib.sleep(2)
        price = ticker.last or ticker.close
        if price and price > 0:
            return price

        logger.warning("Could not get current price — market may be closed.")
        return None

    # ----------------------------------------------------------
    # Order Execution
    # ----------------------------------------------------------

    def buy(self, qty):
        """
        Places a market buy order for `qty` contracts.
        Returns the filled price, or None if order failed/timed out.

        IMPORTANT: Always checks fill status before cancelling.
        A market order that appears to time out may already be filled —
        cancelling after a fill causes a double-buy on the next tick.
        """
        if not self.contract:
            logger.error("Cannot place buy — contract not set up.")
            return None

        order = MarketOrder('BUY', qty, tif='GTC', outsideRth=True)
        logger.info(f"Placing BUY order: {qty} x {self.contract.localSymbol}")

        trade = self.ib.placeOrder(self.contract, order)

        # Wait for fill (up to 30 seconds)
        for _ in range(30):
            self.ib.sleep(1)
            if trade.orderStatus.status == 'Filled':
                filled_price = trade.orderStatus.avgFillPrice
                logger.info(f"BUY filled: {qty} @ {filled_price:.2f}")
                return filled_price

        # Timeout — check fill status one final time before cancelling.
        # Market orders frequently fill during the cancel window; returning
        # None here would cause a duplicate buy on the next price tick.
        if trade.orderStatus.status == 'Filled':
            filled_price = trade.orderStatus.avgFillPrice
            logger.info(f"BUY filled (detected at timeout): {qty} @ {filled_price:.2f}")
            return filled_price

        if trade.orderStatus.filled > 0:
            filled_price = trade.orderStatus.avgFillPrice
            logger.warning(f"BUY partially filled at timeout: {trade.orderStatus.filled} @ {filled_price:.2f} — treating as filled")
            return filled_price

        # Genuinely unfilled — cancel to prevent GTC orphan
        try:
            self.ib.cancelOrder(trade.order)
            logger.warning("BUY order timed out unfilled — cancelled to prevent GTC orphan.")
        except Exception as e:
            logger.error(f"Failed to cancel timed-out BUY order: {e}")
        return None

    def sell(self, qty):
        """
        Places a market sell order for `qty` contracts.
        Returns the filled price, or None if order failed/timed out.

        IMPORTANT: Always checks fill status before cancelling.
        """
        if not self.contract:
            logger.error("Cannot place sell — contract not set up.")
            return None

        order = MarketOrder('SELL', qty, tif='GTC', outsideRth=True)
        logger.info(f"Placing SELL order: {qty} x {self.contract.localSymbol}")

        trade = self.ib.placeOrder(self.contract, order)

        # Wait for fill (up to 30 seconds)
        for _ in range(30):
            self.ib.sleep(1)
            if trade.orderStatus.status == 'Filled':
                filled_price = trade.orderStatus.avgFillPrice
                logger.info(f"SELL filled: {qty} @ {filled_price:.2f}")
                return filled_price

        # Timeout — check fill status one final time before cancelling
        if trade.orderStatus.status == 'Filled':
            filled_price = trade.orderStatus.avgFillPrice
            logger.info(f"SELL filled (detected at timeout): {qty} @ {filled_price:.2f}")
            return filled_price

        if trade.orderStatus.filled > 0:
            filled_price = trade.orderStatus.avgFillPrice
            logger.warning(f"SELL partially filled at timeout: {trade.orderStatus.filled} @ {filled_price:.2f} — treating as filled")
            return filled_price

        # Genuinely unfilled — cancel to prevent GTC orphan
        try:
            self.ib.cancelOrder(trade.order)
            logger.warning("SELL order timed out unfilled — cancelled to prevent GTC orphan.")
        except Exception as e:
            logger.error(f"Failed to cancel timed-out SELL order: {e}")
        return None

    def close_all_positions(self):
        positions = self.get_open_positions()
        if not positions:
            logger.info("close_all_positions called but no open positions found.")
            return None

        total_qty = sum(p['qty'] for p in positions)
        logger.info(f"Closing all positions: {total_qty} contracts")
        return self.sell(total_qty)

    # ----------------------------------------------------------
    # Position Reconciliation
    # ----------------------------------------------------------

    def get_open_positions(self):
        # Filter to the ACTIVE contract expiry so a freshly-rolled-out old
        # contract position (if any lingered) doesn't get mixed in.
        positions = []
        for pos in self.ib.positions():
            if (pos.contract.symbol == SYMBOL and
                    pos.contract.secType == 'FUT' and
                    pos.contract.lastTradeDateOrContractMonth == self.expiry_str and
                    pos.position != 0):
                positions.append({
                    "symbol": pos.contract.localSymbol,
                    "qty": int(pos.position),
                    "avg_cost": pos.avgCost
                })
        return positions

    def get_account_value(self):
        for av in self.ib.accountValues():
            if av.tag == 'NetLiquidation' and av.currency == 'USD':
                try:
                    return float(av.value)
                except:
                    pass
        return None

    # ----------------------------------------------------------
    # Utility
    # ----------------------------------------------------------

    def run_loop(self):
        self.ib.sleep(0)