from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dbtmutant import synth
from dbtmutant.synth import WINDOW_END, WINDOW_START, add_months, generate, write_seeds

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "saas_billing"


def test_generation_is_deterministic():
    assert generate(7) == generate(7)
    assert generate(7) != generate(8)


def test_table_shapes():
    data = generate()
    assert len(data["accounts"]) == 400
    assert Counter(a["segment"] for a in data["accounts"]).keys() == set(synth.SEGMENTS)
    assert len(data["plans"]) == 3
    assert len(data["plan_prices"]) == 6
    assert 4000 <= len(data["subscription_events"]) <= 6000
    dates = [e["event_date"] for e in data["subscription_events"]]
    assert WINDOW_START <= min(dates) and max(dates) <= WINDOW_END
    ids = [e["event_id"] for e in data["subscription_events"]]
    assert ids == list(range(1, len(ids) + 1))


def test_committed_seeds_match_the_generator(tmp_path):
    write_seeds(tmp_path)
    for name in synth.TABLES:
        assert (tmp_path / f"{name}.csv").read_bytes() == (
            EXAMPLE / "seeds" / f"{name}.csv"
        ).read_bytes()


def test_add_months_clamps_to_month_length():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 1, 31), 2) == date(2024, 3, 31)
    assert add_months(date(2024, 11, 15), 3) == date(2025, 2, 15)


def test_event_histories_are_consistent():
    events = defaultdict(list)
    for e in generate()["subscription_events"]:
        events[e["account_id"]].append(e)
    for history in events.values():
        assert history[0]["event_type"] == "start"
        active = True
        for e in history[1:]:
            if e["event_type"] == "cancel":
                assert active
                active = False
            elif e["event_type"] == "reactivate":
                assert not active
                active = True
            else:
                assert active, e
        assert [e["event_date"] for e in history] == sorted(e["event_date"] for e in history)


def test_planted_month_boundary_events():
    events = generate()["subscription_events"]
    month_end = [e for e in events if (e["event_date"] + timedelta(days=1)).day == 1]
    assert sum(e["event_type"] == "cancel" for e in month_end) >= 20
    assert sum(e["event_type"] == "start" for e in month_end) >= 5
    assert sum(e["event_type"] == "upgrade" for e in month_end) >= 5


def test_planted_same_month_churn_and_reactivation():
    by_account = defaultdict(list)
    for e in generate()["subscription_events"]:
        if e["event_type"] in ("cancel", "reactivate"):
            by_account[e["account_id"]].append(e)
    pairs = 0
    for history in by_account.values():
        for first, second in zip(history, history[1:], strict=False):
            same_month = (first["event_date"].year, first["event_date"].month) == (
                second["event_date"].year,
                second["event_date"].month,
            )
            pairs += (
                first["event_type"] == "cancel"
                and second["event_type"] == "reactivate"
                and same_month
            )
    assert pairs >= 5


def test_planted_annual_plans_do_not_divide_evenly():
    data = generate()
    annual = [e for e in data["subscription_events"] if e["billing_period"] == "annual"]
    assert len(annual) > 50
    assert all(price % 12 != 0 for price in synth.ANNUAL_PRICE.values())
    assert {p["price"] for p in data["plan_prices"] if p["billing_period"] == "annual"} == set(
        synth.ANNUAL_PRICE.values()
    )


def test_planted_accounts_without_invoices_and_sparse_credit_notes():
    data = generate()
    invoiced = {i["account_id"] for i in data["invoices"]}
    assert 20 <= 400 - len(invoiced) <= 60
    share = len(data["credit_notes"]) / len(data["invoices"])
    assert 0.01 < share < 0.10
    invoice_ids = {i["invoice_id"] for i in data["invoices"]}
    notes = [c["invoice_id"] for c in data["credit_notes"]]
    assert set(notes) <= invoice_ids
    assert len(notes) == len(set(notes)), "at most one credit note per invoice"
    gross = {i["invoice_id"]: i["gross_amount"] for i in data["invoices"]}
    assert all(
        Decimal(0) < c["credit_amount"] <= gross[c["invoice_id"]] for c in data["credit_notes"]
    )


def test_seed_csv_has_blank_plan_for_cancel_events():
    with (EXAMPLE / "seeds" / "subscription_events.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    cancels = [r for r in rows if r["event_type"] == "cancel"]
    assert cancels and all(r["plan_id"] == "" and r["billing_period"] == "" for r in cancels)
