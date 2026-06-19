# ============================================================
# contract_roll.py — NEW FILE
# Date-based front-month resolution for MES/ES quarterly contracts.
# No hardcoded expiry — the active contract is computed from the date.
# ============================================================

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# ES/MES expire quarterly: March, June, September, December.
QUARTERLY_MONTHS = [3, 6, 9, 12]

# Option B: roll on the SECOND Friday of the expiry month — exactly one
# Friday before the third-Friday settlement, when liquidity has moved to
# the next contract. Anchored to a Friday so it lines up with the weekly
# close. (Defined via second_friday() below, not a raw day offset.)


def third_friday(year, month):
    """Return the date of the third Friday of the given month/year.
    ES/MES settle on the third Friday of the contract month."""
    d = date(year, month, 1)
    # weekday(): Mon=0 ... Fri=4. Days until first Friday:
    offset = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=offset)
    return first_friday + timedelta(days=14)   # +2 weeks = third Friday


def second_friday(year, month):
    """Second Friday of the month — our roll day (one Friday before expiry)."""
    d = date(year, month, 1)
    offset = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=offset)
    return first_friday + timedelta(days=7)    # +1 week = second Friday


def expiry_for(year, month):
    """Third-Friday settlement date for a quarterly contract."""
    return third_friday(year, month)


def roll_date_for(year, month):
    """The date on which we roll OUT of the (year, month) contract —
    the SECOND Friday of the expiry month (Option B, ~1 week early)."""
    return second_friday(year, month)


def _quarterly_contracts_around(today):
    """Yield (year, month) quarterly contracts from the most recent past
    one through several into the future, so we can pick the active one."""
    results = []
    # Start a quarter or two back to be safe, go ~1.5 years forward.
    for year in (today.year - 1, today.year, today.year + 1, today.year + 2):
        for month in QUARTERLY_MONTHS:
            results.append((year, month))
    results.sort()
    return results


def active_contract(today=None):
    """
    Return (year, month) of the contract that should be ACTIVE today.

    We remain in a contract until (and including) its roll Friday — the
    roll execution happens at that Friday's 3:59:55 close, after which the
    Sunday/next session is in the new contract. So during the roll Friday
    itself we still report the OLD contract (we're still holding/trading it
    until the close fires).

    Returns the first quarterly contract whose roll date is today or later.
    """
    if today is None:
        today = date.today()

    for (year, month) in _quarterly_contracts_around(today):
        if today <= roll_date_for(year, month):
            return (year, month)

    logger.error("active_contract: no future contract found — check calendar logic")
    last = _quarterly_contracts_around(today)[-1]
    return last


def contract_expiry_str(today=None):
    """
    Return the IBKR lastTradeDateOrContractMonth string for the active
    contract, in YYYYMMDD form (the third-Friday settlement date).

    This is what gets passed to the contract definition in broker.py.
    """
    year, month = active_contract(today)
    exp = expiry_for(year, month)
    return exp.strftime("%Y%m%d")


def is_roll_day(today):
    """
    True if `today` is a roll day — the second Friday of an expiry month,
    on which the Friday close should CLOSE-ALL in the expiring contract and
    immediately REOPEN the same size in the new contract.

    This is checked at the Friday close. Because active_contract() reports
    the old contract through the roll day and the *next* quarterly contract
    starting the following day, comparing today's contract to next-day's
    contract cleanly identifies the roll boundary.
    """
    if today.weekday() != 4:          # must be Friday
        return False
    this_contract = active_contract(today)
    tomorrow      = active_contract(today + timedelta(days=1))
    return this_contract != tomorrow


def describe(today=None):
    """Human-readable summary for logging/startup."""
    if today is None:
        today = date.today()
    y, m = active_contract(today)
    return (f"Active MES contract: {y}-{m:02d} "
            f"(expiry {expiry_for(y, m).strftime('%Y-%m-%d')}, "
            f"roll-out {roll_date_for(y, m).strftime('%Y-%m-%d')}) "
            f"| expiry_str={contract_expiry_str(today)}")