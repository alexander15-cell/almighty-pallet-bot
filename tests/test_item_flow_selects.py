"""
Regression guard: Discord's mobile client silently swallows a tap on a
discord.ui.SelectOption that's already marked default=True - the row shows
checked but the select's callback never fires, so a reviewer could get
stuck unable to pick (or re-confirm) whichever option came pre-highlighted.
This bit the eBay approval flow's condition/category/format selects once
already (see the fix in item_flow.py); this test just makes sure nobody
reintroduces default=True on a SelectOption in that file without meaning
to change this deliberate tradeoff.
"""
import re
from pathlib import Path

ITEM_FLOW_PATH = Path(__file__).resolve().parent.parent / "cogs" / "item_flow.py"


def test_no_selectoption_sets_default_true():
    source = ITEM_FLOW_PATH.read_text(encoding="utf-8")
    # A SelectOption(...) call can span multiple lines, so scan each call's
    # full argument text rather than doing a plain line-by-line grep.
    for match in re.finditer(r"discord\.ui\.SelectOption\(([^)]*)\)", source, re.DOTALL):
        args = match.group(1)
        assert "default=True" not in args, (
            "Found default=True on a discord.ui.SelectOption in item_flow.py - "
            "this re-triggers the mobile tap-bug the fix in this file removed. "
            "See EbayConditionSelectView's docstring before reverting this."
        )
