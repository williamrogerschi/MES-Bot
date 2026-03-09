# ============================================================
# config.py — MES Grid Trading Bot Configuration
# All tunable parameters live here. Never need to touch
# strategy.py or bot.py just to change a setting.
# ============================================================

# --- IBKR Connection ---
IB_HOST = '127.0.0.1'       # Localhost — bot and gateway on same machine
IB_PORT = 4002               # 4002 = IB Gateway paper | 7497 = TWS paper
IB_CLIENT_ID = 1             # Unique ID for this bot connection

# --- Contract ---
SYMBOL = 'MES'               # Micro E-mini S&P 500
EXCHANGE = 'CME'
CURRENCY = 'USD'
CONTRACT_TYPE = 'FUT'

# Expiry: format is YYYYMM
# March = 202503, June = 202506
# Update this when rolling to the next contract
CONTRACT_EXPIRY = '202506'   # Jump to June as soon as it opens

# --- Grid Parameters ---
GRID_PCT = 0.012             # 1.2% grid spacing (both up and down)

# How many contracts to buy on first entry
INITIAL_QTY = 1

# How many additional contracts to buy on each dip
AVERAGING_QTY = 2

# Max number of averaging-down levels allowed
# Level 0 = initial buy (1 contract)
# Level 1 = first avg down (3 total)
# Level 2 = second avg down (5 total)
# Level 3 = third avg down (7 total)
# Set this based on your account size / risk tolerance
MAX_GRID_LEVELS = 5          # Stops averaging down after 5 dips (11 contracts max)

# --- Account / Risk ---
ACCOUNT_SIZE = 75000         # Your simulated account size in USD
MAX_MARGIN_USAGE = 0.50      # Never use more than 50% of account on margin
MES_MARGIN_PER_CONTRACT = 1500  # Approximate margin per MES contract (adjust if IBKR shows different)

# --- Schedule ---
# All times are US Eastern Time (ET)
WEEKLY_CLOSE_DAY = 4         # Friday = 4 (Monday=0 ... Friday=4)
WEEKLY_CLOSE_HOUR = 15       # 3 PM
WEEKLY_CLOSE_MINUTE = 59
WEEKLY_CLOSE_SECOND = 55     # 3:59:55 PM ET — close all positions

WEEKLY_OPEN_DAY = 6          # Sunday = 6
WEEKLY_OPEN_HOUR = 17        # 5:00 PM ET — re-enter with 1 contract
WEEKLY_OPEN_MINUTE = 0

# --- Profit Sequestering ---
# The bot will set aside this % of each realized profit into a reserve
# Reserve is tracked in state.json and used for context/reporting
# Phase 2: reserve will fund larger dip buys automatically
PROFIT_RESERVE_PCT = 0.25    # Set aside 25% of each win into reserve

# --- State File ---
# Bot writes its full state here after every trade
# If bot restarts, it reads this file to resume
STATE_FILE = 'state.json'

# --- Logging ---
LOG_FILE = 'bot.log'
LOG_LEVEL = 'INFO'           # DEBUG for verbose, INFO for normal, WARNING for quiet