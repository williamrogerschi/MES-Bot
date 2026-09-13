"""
patch_state.py — ONE-TIME fix for the reset_state() reconciliation bug.

Restores realized_pnl and profit_reserve to their correct values after the
9/13 restart wiped them to $0.00. Does NOT touch your current position
(the 1 contract @ 7675.25 bought fresh after the wipe is left alone).

Run once from the same directory as state.json:
    python patch_state.py
"""

import json
import shutil
from datetime import datetime

STATE_FILE = "state.json"

CORRECT_REALIZED_PNL = 16258.75
CORRECT_PROFIT_RESERVE = 4492.19

# Back up the current (wiped) file first, just in case.
backup_name = f"state.json.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
shutil.copy(STATE_FILE, backup_name)
print(f"Backed up current state.json to {backup_name}")

with open(STATE_FILE, "r") as f:
    state = json.load(f)

print("\nBEFORE patch:")
print(f"  realized_pnl:   {state.get('realized_pnl')}")
print(f"  profit_reserve: {state.get('profit_reserve')}")

state["realized_pnl"] = CORRECT_REALIZED_PNL
state["profit_reserve"] = CORRECT_PROFIT_RESERVE

with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)

print("\nAFTER patch:")
print(f"  realized_pnl:   {state['realized_pnl']}")
print(f"  profit_reserve: {state['profit_reserve']}")
print("\nDone. Position/grid fields were left untouched.")