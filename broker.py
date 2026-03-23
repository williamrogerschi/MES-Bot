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
    SYMBOL, EXCHANGE, CURRENCY, CONTRACT_TYPE, CONTRACT_EXPIRY
)

logger = logging.getLogger(__name__)

# Suppress ib_insync's verbose internal logging
util.logToConsole(logging.WARNING)


class Broker:
    def __init__(self):
        self.ib = IB()
        self.contract = None
        self.ticker = None
        self._price_callbacks = []   # Functions to call when price updates arrive

    # ----------------------------------------------------------
    # Connection
    # ----------------------------------------------------------

    def connect(self):
        """
        Connects to IB Gateway. Retries up to 5 times on failure.
        """
        for attempt in range(1, 6):
            try:
                logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} (attempt {attempt}/5)...")
                self.ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
                self.ib.reqMarketDataType(1)  # 1 = live data
                logger.info("Connected to IB Gateway.")
                self._setup_contract()
                return True
            except Exception as e:
                logger.warning(f"Connection attempt {attempt} failed: {e}")
                time.sleep(5)

        logger.error("Could not connect to IB Gateway after 5 attempts. Is it running?")
        return False

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB Gateway.")

    def is_connected(self):
        return self.ib.isConnected()

    def reconnect_if_needed(self):
        """
        Called periodically in the main loop to ensure connection stays alive.
        """
        if not self.ib.isConnected():
            logger.warning("Connection lost — attempting reconnect...")
            self.connect()

    # ----------------------------------------------------------
    # Contract Setup
    # ----------------------------------------------------------

    def _setup_contract(self):
        """
        Defines the MES futures contract and qualifies it with IBKR.
        Qualification fills in missing fields (conId, tradingClass, etc.)
        """
        contract = Future(
            symbol=SYMBOL,
            lastTradeDateOrContractMonth=CONTRACT_EXPIRY,
            exchange=EXCHANGE,
            currency=CURRENCY
        )
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise Exception(f"Could not qualify contract {SYMBOL} {CONTRACT_EXPIRY}. "
                            f"Check that the contract expiry is correct and trading has opened.")
        self.contract = qualified[0]
        logger.info(f"Contract qualified: {self.contract.localSymbol} | "
                    f"Exchange: {self.contract.exchange} | "
                    f"Expiry: {self.contract.lastTradeDateOrContractMonth}")

    # ----------------------------------------------------------
    # Price Streaming
    # ----------------------------------------------------------

    def start_price_stream(self, callback):
        """
        Subscribes to real-time price ticks for the MES contract.
        Calls `callback(price)` every time a new trade price arrives.
        Also hooks into portfolio updates as a backup price source.
        """
        self._price_callbacks.append(callback)
        self.ticker = self.ib.reqMktData(self.contract, '233', False, False)
        self.ticker.updateEvent += self._on_price_update
        # Portfolio updates fire every ~3 min and serve as backup price source
        self.ib.updatePortfolioEvent += self._on_portfolio_update
        logger.info(f"Price stream started for {self.contract.localSymbol}")

    def _on_price_update(self, ticker):
        """
        Internal handler for price tick events.
        Fires registered callbacks with the latest trade price.
        """
        price = ticker.last
        if price and price > 0:
            for cb in self._price_callbacks:
                try:
                    cb(price)
                except Exception as e:
                    logger.error(f"Error in price callback: {e}")

    def _on_portfolio_update(self, item):
        """
        Price source from portfolio update events (~3 min intervals).
        Always fires callbacks for MES — duplicate updates are harmless.
        """
        if (item.contract.symbol == self.contract.symbol and
                item.marketPrice and item.marketPrice > 0):
            for cb in self._price_callbacks:
                try:
                    cb(item.marketPrice)
                except Exception as e:
                    logger.error(f"Error in portfolio price callback: {e}")

    def get_current_price(self):
        """
        Returns the latest known price synchronously.
        Used for startup reconciliation and order confirmation.
        """
        if self.ticker and self.ticker.last and self.ticker.last > 0:
            return self.ticker.last

        # Fallback: request a snapshot
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
        Returns the filled price, or None if order failed.
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

        logger.error(f"BUY order not filled after 30 seconds. Status: {trade.orderStatus.status}")
        return None

    def sell(self, qty):
        """
        Places a market sell order for `qty` contracts.
        Returns the filled price, or None if order failed.
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

        logger.error(f"SELL order not filled after 30 seconds. Status: {trade.orderStatus.status}")
        return None

    def close_all_positions(self):
        """
        Emergency close — sells all open MES positions at market.
        Used for Friday close and manual override.
        Returns filled price or None.
        """
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
        """
        Returns list of open MES positions from IBKR.
        Format: [{"symbol": "MES", "qty": 2, "avg_cost": 5400.0}]
        Used on restart to reconcile bot state with reality.
        """
        positions = []
        for pos in self.ib.positions():
            if (pos.contract.symbol == SYMBOL and
                    pos.contract.secType == 'FUT' and
                    pos.position != 0):
                positions.append({
                    "symbol": pos.contract.localSymbol,
                    "qty": int(pos.position),
                    "avg_cost": pos.avgCost
                })
        return positions

    def get_account_value(self):
        """
        Returns net liquidation value of the paper trading account.
        """
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
        """
        Runs the ib_insync event loop — call this to process
        incoming data and events. Used in the main bot loop.
        """
        self.ib.sleep(0)