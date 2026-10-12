"""
The partner ledger (partner_ledger.py, cogs/partners.py): money put in,
expenses paid personally, reimbursements, payouts, and an equal profit
split between active partners. A record only - nothing moves money.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import partner_ledger
import cogs.partners as partners_cog
from cogs.partners import Partners, build_summary_embed

FINANCE = SimpleNamespace(name=config.ROLE_FINANCE_MGMT)
ADMIN = SimpleNamespace(name=config.ROLE_ADMIN)


@pytest.fixture(autouse=True)
def roles(monkeypatch):
    def resolve(guild, name):
        return {config.ROLE_FINANCE_MGMT: FINANCE, config.ROLE_ADMIN: ADMIN}.get(name)
    monkeypatch.setattr(partners_cog.runtime_settings, "resolve_role", resolve)
    monkeypatch.setattr(partners_cog.finance_utils, "post_to_finance_audit_log", AsyncMock())


def _member(user_id, name):
    return SimpleNamespace(id=user_id, display_name=name)


def _interaction(user_id=1, roles=(FINANCE,)):
    return SimpleNamespace(
        guild=object(), user=SimpleNamespace(id=user_id, roles=list(roles), display_name=f"user{user_id}"),
        client=object(),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _cog():
    return Partners.__new__(Partners)


def _call(command, interaction, *args, **kwargs):
    asyncio.run(command.callback(_cog(), interaction, *args, **kwargs))
    return interaction


ALEX, BEN, CAM, DEV = (_member(11, "Alex"), _member(12, "Ben"), _member(13, "Cam"), _member(14, "Dev"))


@pytest.fixture
def four(fresh_db):
    for m in (ALEX, BEN, CAM, DEV):
        fresh_db.add_partner(m.id, m.display_name, added_by=1)
    return fresh_db


def _sold_pallet(db, cost, revenue):
    pallet_id = db.create_pallet(f"P{cost}-{revenue}", category_id=cost * 1000 + revenue, created_by=1)
    db.set_pallet_cost(pallet_id, cost, actor_id=1)
    item_id = db.create_item(pallet_id, "x", [], 1)
    db.record_item_sale(item_id, revenue, "eBay", actor_id=1)
    return pallet_id


# --------------------------------------------------------------- the math

def test_equal_split_equity_owed_and_behind(four):
    _sold_pallet(four, cost=400, revenue=1200)  # profit 800 -> 200 each
    ids = {m.display_name: four.get_partner_by_discord_id(m.id)["id"] for m in (ALEX, BEN, CAM, DEV)}
    four.add_partner_ledger_entry(ids["Alex"], "contribution", 400, "pallet money", "2026-10-01", 1)
    four.add_partner_ledger_entry(ids["Ben"], "fronted", 30, "tape + boxes", "2026-10-02", 1)
    four.add_partner_ledger_entry(ids["Ben"], "reimbursement", 10, None, "2026-10-03", 1)
    four.add_partner_ledger_entry(ids["Cam"], "payout", 150, None, "2026-10-04", 1)

    data = partner_ledger.summary()
    rows = {r["partner"]["display_name"]: r for r in data["partners"]}

    assert data["profit"]["profit"] == 800.0 and data["share"] == 200.0
    assert rows["Alex"]["equity"] == 600.0          # 400 in + 200 share
    assert rows["Ben"]["owed"] == 20.0              # 30 fronted - 10 back
    assert rows["Cam"]["equity"] == 50.0            # 200 share - 150 paid out
    assert rows["Alex"]["behind"] == 150.0 and rows["Cam"]["behind"] == 0.0
    assert data["total_owed"] == 20.0


def test_pallet_with_sales_but_no_cost_is_flagged(four):
    pallet_id = four.create_pallet("No Cost Yet", category_id=9, created_by=1)
    item_id = four.create_item(pallet_id, "x", [], 1)
    four.record_item_sale(item_id, 50.0, "eBay", actor_id=1)
    assert partner_ledger.business_profit()["pallets_missing_cost"] == ["No Cost Yet"]


def test_voided_entries_dont_count(four):
    alex = four.get_partner_by_discord_id(ALEX.id)["id"]
    entry = four.add_partner_ledger_entry(alex, "contribution", 999, None, "2026-10-01", 1)
    four.void_partner_ledger_entry(entry, "typo", voided_by=1)
    assert partner_ledger.partner_totals(alex)["contribution"] == 0.0


def test_removed_partner_leaves_the_split_but_keeps_history(four):
    _sold_pallet(four, cost=0, revenue=900)
    alex = four.get_partner_by_discord_id(ALEX.id)["id"]
    four.add_partner_ledger_entry(alex, "contribution", 100, None, "2026-10-01", 1)
    four.deactivate_partner(DEV.id)

    assert partner_ledger.summary()["share"] == 300.0  # 3 ways now
    four.add_partner(DEV.id, "Dev", added_by=1)        # re-adding restores, doesn't duplicate
    assert len(four.get_partners()) == 4 and partner_ledger.summary()["share"] == 225.0


def test_parse_entry_date():
    assert partner_ledger.parse_entry_date("2026-10-12") == "2026-10-12"
    assert len(partner_ledger.parse_entry_date(None)) == 10
    with pytest.raises(ValueError):
        partner_ledger.parse_entry_date("10/12/2026")


# ------------------------------------------------------------- commands

def test_put_in_defaults_to_yourself_and_hits_the_audit_log(four):
    event = _call(Partners.put_in, _interaction(user_id=ALEX.id), 250.0, None, "deposit", "2026-10-05")
    assert "Entry #" in event.response.send_message.call_args.args[0]
    alex = four.get_partner_by_discord_id(ALEX.id)["id"]
    assert partner_ledger.partner_totals(alex)["contribution"] == 250.0
    partners_cog.finance_utils.post_to_finance_audit_log.assert_awaited_once()


def test_recording_needs_finance_role(four):
    event = _call(Partners.payout, _interaction(roles=()), BEN, 50.0)
    assert "Finance Management" in event.response.send_message.call_args.args[0]
    assert four.get_partner_ledger() == []


def test_non_partner_and_bad_amounts_are_refused(four):
    stranger = _member(99, "Stranger")
    assert "isn't a partner" in _call(Partners.payout, _interaction(), stranger, 5.0).response.send_message.call_args.args[0]
    assert "more than $0" in _call(Partners.payout, _interaction(), BEN, 0.0).response.send_message.call_args.args[0]
    assert "2026-10-12" in _call(Partners.payout, _interaction(), BEN, 5.0, None, "Oct 12").response.send_message.call_args.args[0]
    assert four.get_partner_ledger() == []


def test_cannot_reimburse_more_than_is_owed(four):
    _call(Partners.paid_expense, _interaction(), 40.0, "shipping supplies", BEN)
    refused = _call(Partners.reimburse, _interaction(), BEN, 60.0)
    assert "only owes" in refused.response.send_message.call_args.args[0]
    _call(Partners.reimburse, _interaction(), BEN, 40.0)
    assert partner_ledger.owed_to_partner(four.get_partner_by_discord_id(BEN.id)["id"]) == 0.0


def test_void_command(four):
    _call(Partners.payout, _interaction(), BEN, 75.0)
    entry_id = four.get_partner_ledger()[0]["id"]
    _call(Partners.void, _interaction(), entry_id, "wrong partner")
    assert four.get_partner_ledger() == []
    again = _call(Partners.void, _interaction(), entry_id, "again")
    assert "already voided" in again.response.send_message.call_args.args[0]


def test_add_and_remove_need_admin(fresh_db):
    event = _call(Partners.add, _interaction(roles=(FINANCE,)), ALEX)
    assert "Pallet Admin" in event.response.send_message.call_args.args[0]
    _call(Partners.add, _interaction(roles=(ADMIN,)), ALEX)
    assert fresh_db.get_partner_by_discord_id(ALEX.id)["active"] == 1
    _call(Partners.remove, _interaction(roles=(ADMIN,)), ALEX)
    assert fresh_db.get_partner_by_discord_id(ALEX.id)["active"] == 0


def test_summary_visible_to_partners_but_not_others(four):
    ok = _call(Partners.summary, _interaction(user_id=CAM.id, roles=()))
    embed = ok.followup.send.call_args.kwargs["embed"]
    assert embed.title == "🤝 Partner Ledger" and len(embed.fields) == 4
    nope = _call(Partners.summary, _interaction(user_id=99, roles=()))
    assert "Only partners" in nope.response.send_message.call_args.args[0]


def test_history_shows_voided_entries_struck_through(four):
    _call(Partners.payout, _interaction(), BEN, 75.0, "venmo")
    entry_id = four.get_partner_ledger()[0]["id"]
    four.void_partner_ledger_entry(entry_id, "duplicate", voided_by=1)
    text = _call(Partners.history, _interaction()).response.send_message.call_args.args[0]
    assert text.startswith("~~") and "duplicate" in text and "venmo" in text


def test_summary_embed_fits_discord_limits(four):
    embed = build_summary_embed(partner_ledger.summary())
    assert len(embed.fields) <= 25 and all(len(f.value) <= 1024 for f in embed.fields)
