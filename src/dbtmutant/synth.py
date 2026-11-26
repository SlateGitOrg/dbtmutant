"""Seeded generator for the synthetic SaaS billing seeds used by the example project.

The data is synthetic. 400 accounts in three segments subscribe to one of three plans (monthly or
annual billing) over 24 months. The generator plants the edge cases that make specific mutants
observable on the data:

* cancellations, starts and upgrades that land on the last day of a month (``<`` vs ``<=``);
* annual plans whose price does not divide evenly by 12 (``/ 12`` arithmetic);
* credit notes on a small share of invoices (a dropped ``COALESCE``);
* accounts with no invoices at all (``INNER`` vs ``LEFT`` join);
* a reactivation in the same month as the churn (``CASE`` ordering).

Run ``python -m dbtmutant.synth --out examples/saas_billing/seeds`` to rewrite the committed CSVs.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

WINDOW_START = date(2024, 1, 1)
WINDOW_END = date(2025, 12, 31)
N_ACCOUNTS = 400
DEFAULT_SEED = 7

SEGMENTS = ("smb", "mid_market", "enterprise")
SEGMENT_WEIGHTS = (0.60, 0.30, 0.10)
PLANS = {1: "starter", 2: "growth", 3: "scale"}
MONTHLY_PRICE = {1: Decimal("49.00"), 2: Decimal("149.00"), 3: Decimal("499.00")}
ANNUAL_PRICE = {1: Decimal("490.00"), 2: Decimal("1490.00"), 3: Decimal("4990.00")}
PLAN_WEIGHTS = {
    "smb": (0.75, 0.22, 0.03),
    "mid_market": (0.25, 0.55, 0.20),
    "enterprise": (0.02, 0.28, 0.70),
}
ANNUAL_SHARE = {"smb": 0.15, "mid_market": 0.35, "enterprise": 0.60}
CANCEL_RATE = {"smb": 0.030, "mid_market": 0.018, "enterprise": 0.008}
UPGRADE_RATE = 0.035
DOWNGRADE_RATE = 0.015
REACTIVATE_RATE = 0.12
MONTH_END_SNAP = 0.35  # share of cancellations/upgrades snapped to the last day of the month
START_SNAP = 0.06
SAME_MONTH_REACTIVATION = 0.25
NO_INVOICE_SHARE = 0.10
GOODWILL_CREDIT_SHARE = 0.03
DOWNGRADE_CREDIT_SHARE = 0.5

TABLES = ("accounts", "plans", "plan_prices", "subscription_events", "invoices", "credit_notes")


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def add_months(anchor: date, months: int) -> date:
    """Add whole months, clamping the day to the target month's length."""
    index = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _weighted(rng: random.Random, options, weights):
    return rng.choices(options, weights=weights, k=1)[0]


def _price(plan_id: int, billing: str) -> Decimal:
    return ANNUAL_PRICE[plan_id] if billing == "annual" else MONTHLY_PRICE[plan_id]


def _simulate_account(rng: random.Random, segment: str, first_month: int) -> list[dict]:
    """Return change events (start/upgrade/downgrade/cancel/reactivate) for one account."""
    plan = _weighted(rng, (1, 2, 3), PLAN_WEIGHTS[segment])
    billing = "annual" if rng.random() < ANNUAL_SHARE[segment] else "monthly"
    year, month = divmod(first_month, 12)
    year += WINDOW_START.year
    month += 1
    last = month_end(year, month)
    if rng.random() < START_SNAP:
        start = last
    else:
        start = date(year, month, rng.randint(1, last.day))
    events = [
        {"date": start, "type": "start", "plan": plan, "billing": billing},
    ]
    active = True
    cancelled_idx = -1
    for idx in range(first_month + 1, 24):
        y, m = divmod(idx, 12)
        y += WINDOW_START.year
        m += 1
        last = month_end(y, m)
        if active:
            roll = rng.random()
            if roll < CANCEL_RATE[segment]:
                when = (
                    last if rng.random() < MONTH_END_SNAP else date(y, m, rng.randint(1, last.day))
                )
                events.append({"date": when, "type": "cancel", "plan": None, "billing": None})
                active = False
                cancelled_idx = idx
                if rng.random() < SAME_MONTH_REACTIVATION and when.day < last.day:
                    back = date(y, m, rng.randint(when.day + 1, last.day))
                    events.append(
                        {"date": back, "type": "reactivate", "plan": plan, "billing": billing}
                    )
                    active = True
            elif roll < CANCEL_RATE[segment] + UPGRADE_RATE and plan < 3:
                when = (
                    last if rng.random() < MONTH_END_SNAP else date(y, m, rng.randint(1, last.day))
                )
                plan += 1
                events.append({"date": when, "type": "upgrade", "plan": plan, "billing": billing})
            elif roll < CANCEL_RATE[segment] + UPGRADE_RATE + DOWNGRADE_RATE and plan > 1:
                when = date(y, m, rng.randint(1, last.day))
                plan -= 1
                events.append({"date": when, "type": "downgrade", "plan": plan, "billing": billing})
        elif idx - cancelled_idx <= 8:
            if rng.random() < REACTIVATE_RATE:
                when = date(y, m, rng.randint(1, last.day))
                events.append(
                    {"date": when, "type": "reactivate", "plan": plan, "billing": billing}
                )
                active = True
    return events


def _segments_of(events: list[dict]):
    """Yield (index, start, end, plan, billing, kind) for each billed segment of an account."""
    for i, ev in enumerate(events):
        if ev["type"] == "cancel":
            continue
        end = events[i + 1]["date"] if i + 1 < len(events) else WINDOW_END + timedelta(days=1)
        yield i, ev["date"], end, ev["plan"], ev["billing"], ev["type"]


def generate(seed: int = DEFAULT_SEED) -> dict[str, list[dict]]:
    """Generate all six seed tables as lists of dict rows."""
    rng = random.Random(seed)
    accounts: list[dict] = []
    raw_events: dict[int, list[dict]] = {}
    for account_id in range(1, N_ACCOUNTS + 1):
        segment = _weighted(rng, SEGMENTS, SEGMENT_WEIGHTS)
        first_month = min(23, int(rng.triangular(0, 22, 4)))
        events = _simulate_account(rng, segment, first_month)
        events.sort(key=lambda e: e["date"])
        accounts.append(
            {"account_id": account_id, "segment": segment, "signup_date": events[0]["date"]}
        )
        raw_events[account_id] = events

    no_invoice = set(rng.sample(range(1, N_ACCOUNTS + 1), int(N_ACCOUNTS * NO_INVOICE_SHARE)))

    event_rows: list[tuple] = []  # (date, account, seq, row-without-id)
    invoice_rows: list[tuple] = []  # (date, account, seq, dict)
    credit_plan: list[tuple] = []  # (account, invoice key, amount, issued date)
    for account_id, events in raw_events.items():
        seq = 0
        invoices_for_account: list[tuple[date, int]] = []
        for ev in events:
            event_rows.append((ev["date"], account_id, seq, ev["type"], ev["plan"], ev["billing"]))
            seq += 1
        for pos, start, end, plan, billing, kind in _segments_of(events):
            period_months = 12 if billing == "annual" else 1
            k = 0
            while True:
                when = add_months(start, k * period_months)
                if when >= end or when > WINDOW_END:
                    break
                if k > 0:
                    event_rows.append((when, account_id, seq, "renewal", plan, billing))
                    seq += 1
                if account_id not in no_invoice:
                    amount = _price(plan, billing)
                    invoice_rows.append(
                        (when, account_id, seq, {"amount": amount, "plan": plan, "kind": "cycle"})
                    )
                    invoices_for_account.append((when, len(invoice_rows) - 1))
                k += 1
            if kind == "upgrade" and account_id not in no_invoice:
                # Mid-cycle upgrade: prorated difference for the rest of the current cycle.
                old_plan = events[pos - 1]["plan"]
                cycle = 365 if billing == "annual" else 30
                remaining = Decimal(min(cycle, (end - start).days)) / Decimal(cycle)
                delta = (_price(plan, billing) - _price(old_plan, billing)) * remaining
                invoice_rows.append(
                    (start, account_id, seq, {"amount": _q(delta), "plan": plan, "kind": "pro"})
                )
            if (
                kind == "downgrade"
                and account_id not in no_invoice
                and rng.random() < DOWNGRADE_CREDIT_SHARE
            ):
                prior = [t for t in invoices_for_account if t[0] <= start]
                if prior:
                    credit_plan.append((account_id, prior[-1][1], None, start))

    event_rows.sort(key=lambda r: (r[0], r[1], r[2]))
    events_out = [
        {
            "event_id": i,
            "account_id": r[1],
            "event_date": r[0],
            "event_type": r[3],
            "plan_id": r[4],
            "billing_period": r[5],
        }
        for i, r in enumerate(event_rows, start=1)
    ]

    order = sorted(
        range(len(invoice_rows)), key=lambda i: (invoice_rows[i][0], invoice_rows[i][1], i)
    )
    new_id = {old: n for n, old in enumerate(order, start=1)}
    invoices_out = [
        {
            "invoice_id": new_id[i],
            "account_id": invoice_rows[i][1],
            "invoice_date": invoice_rows[i][0],
            "gross_amount": invoice_rows[i][3]["amount"],
        }
        for i in order
    ]

    credits: dict[int, dict] = {}
    for _account_id, idx, _, issued in credit_plan:
        gross = invoice_rows[idx][3]["amount"]
        credits[new_id[idx]] = {
            "invoice_id": new_id[idx],
            "credit_amount": _q(gross * Decimal(str(round(rng.uniform(0.10, 0.40), 2)))),
            "issued_date": issued,
        }
    for inv in invoices_out:
        if inv["invoice_id"] in credits or inv["gross_amount"] <= 0:
            continue
        if rng.random() < GOODWILL_CREDIT_SHARE:
            credits[inv["invoice_id"]] = {
                "invoice_id": inv["invoice_id"],
                "credit_amount": _q(
                    inv["gross_amount"] * Decimal(str(round(rng.uniform(0.10, 0.50), 2)))
                ),
                "issued_date": min(
                    WINDOW_END, inv["invoice_date"] + timedelta(days=rng.randint(1, 20))
                ),
            }
    credit_rows = sorted(credits.values(), key=lambda c: (c["issued_date"], c["invoice_id"]))
    credit_notes = [{"credit_note_id": i, **c} for i, c in enumerate(credit_rows, start=1)]

    plans = [{"plan_id": pid, "plan_name": name} for pid, name in PLANS.items()]
    prices = [
        {"plan_id": pid, "billing_period": bp, "price": _price(pid, bp)}
        for pid in PLANS
        for bp in ("monthly", "annual")
    ]
    return {
        "accounts": accounts,
        "plans": plans,
        "plan_prices": prices,
        "subscription_events": events_out,
        "invoices": invoices_out,
        "credit_notes": credit_notes,
    }


def write_seeds(out_dir: Path, seed: int = DEFAULT_SEED) -> dict[str, int]:
    """Write the seed CSVs and return the row count per table."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, rows in generate(seed).items():
        with (out_dir / f"{name}.csv").open("w", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            columns = list(rows[0])
            writer.writerow(columns)
            for row in rows:
                writer.writerow(["" if row[c] is None else row[c] for c in columns])
        counts[name] = len(rows)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic SaaS billing seeds.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    for name, count in write_seeds(args.out, args.seed).items():
        print(f"{name}: {count} rows")


if __name__ == "__main__":
    main()
