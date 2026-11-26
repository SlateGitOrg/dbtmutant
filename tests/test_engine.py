from __future__ import annotations

import json
from collections import Counter

import pytest
from test_operators import RenameColumn  # noqa: I001

from dbtmutant.engine import (
    EQUIVALENT,
    INVALID,
    KILLED,
    SURVIVED,
    Engine,
    TestOutcome,
    threshold_met,
)
from dbtmutant.loader import DataTest, GivenInput, UnitTest
from dbtmutant.mutants import find_mutant, generate_mutants
from dbtmutant.operators import get_operators, register_operator, unregister_operator
from dbtmutant.report import ranked_survivors, score
from dbtmutant.sqlutil import parse
from dbtmutant.unit_eval import run_unit_test


def test_baseline_has_no_failures(engine):
    base = engine.baseline_result
    assert base.clean
    assert (base.failed_tests, base.failed_unit_tests) == ([], [])
    assert (base.test_count, base.unit_test_count) == (32, 5)


def test_baseline_warehouse_has_the_seeded_shape(engine):
    con = engine.con
    assert con.execute("select count(*) from main.accounts").fetchone()[0] == 400
    # 3 segments x 24 month ends, minus the segment-months before that segment's first customer
    assert con.execute("select count(*) from main.mart_arr_by_segment").fetchone()[0] == 70
    assert (
        con.execute("select max(month_end) from main.mart_arr_by_segment").fetchone()[0].isoformat()
        == "2025-12-31"
    )


def test_threshold_expressions():
    assert threshold_met("!= 0", 3) and not threshold_met("!= 0", 0)
    assert threshold_met("> 10", 11) and not threshold_met("> 10", 10)
    assert threshold_met(">=2", 2)
    with pytest.raises(ValueError):
        threshold_met("between 1 and 2", 1)


def test_warn_severity_does_not_kill(engine):
    failing = "select 1 as x"
    warn = DataTest("t", "test.t", failing, (), None, "warn", "!= 0", "!= 0")
    error = DataTest("t", "test.t", failing, (), None, "error", "!= 0", "!= 0")
    broken = DataTest("t", "test.t", "select nope from nowhere", (), None, "error", "!= 0", "!= 0")
    assert engine._run_data_test(warn, {}) == TestOutcome("t", "warn", 1)
    assert not engine._run_data_test(warn, {}).kills
    assert engine._run_data_test(error, {}).kills
    assert engine._run_data_test(broken, {}).status == "error"


def test_planted_boundary_mutant_is_killed_by_the_reconcile_test(by_id, ids):
    result = by_id[ids["boundary"]]
    assert "c.day <= p.end_date" in result.mutant.summary
    assert result.verdict == KILLED
    assert result.killed_by == ["mrr_movements_reconcile"]


def test_planted_month_end_filter_in_the_mart_survives_with_arr_impact(by_id, ids, engine):
    result = by_id[ids["mart_boundary"]]
    assert "f.day < b.last_day_of_data" in result.mutant.summary
    assert result.verdict == SURVIVED
    assert result.killed_by == []
    december = engine.con.execute(
        "select sum(mrr) from main.mart_arr_by_segment where month_end = date '2025-12-31'"
    ).fetchone()[0]
    # Impact is measured at the latest month end, and the mutant removes exactly that month.
    assert result.impact["mrr"] == pytest.approx(-float(december), abs=0.01)
    assert result.impact["arr"] == pytest.approx(-12 * float(december), abs=0.05)
    assert result.changed_models == {"mart_arr_by_segment": 3}  # the three December rows disappear


def test_count_distinct_over_a_unique_column_is_equivalent_on_the_data(by_id, ids):
    result = by_id[ids["count_distinct"]]
    assert "COUNT(account_id)" in result.mutant.summary
    assert result.verdict == EQUIVALENT
    assert result.changed_models == {} and result.impact == {}


def test_dropped_coalesce_exposed_by_missing_credit_notes_is_killed(by_id, ids):
    result = by_id[ids["coalesce_credits"]]
    assert result.verdict == KILLED
    assert "not_null_int_account_credits_credit_rate" in result.killed_by


def test_left_to_inner_join_on_invoices_survives_because_both_derivations_drop_the_accounts(
    by_id, ids
):
    result = by_id[ids["inner_invoices"]]
    assert result.verdict == SURVIVED
    assert result.impact["mrr"] < 0


def test_a_mutant_the_database_rejects_is_invalid_not_killed(by_id, ids):
    result = by_id[ids["interval_zero"]]
    assert result.verdict == INVALID
    assert "interval cannot be 0" in result.error
    assert result.killed_by == []


def test_custom_operator_that_breaks_the_sql_gives_invalid_mutants(project, engine):
    register_operator(RenameColumn())
    try:
        found = generate_mutants(
            "int_account_credits",
            project.models["int_account_credits"].sql,
            get_operators(["RENAME_COL"]),
        )
    finally:
        unregister_operator("RENAME_COL")
    assert len(found) == 2
    outcomes = [engine.run_mutant(m) for m in found]
    assert [o.verdict for o in outcomes] == [INVALID, INVALID]
    assert all("no_such_column" in o.error for o in outcomes)


def test_verdicts_partition_the_mutants(results, mutants):
    assert len(results) == len(mutants)
    assert Counter(r.verdict for r in results).keys() <= {KILLED, SURVIVED, EQUIVALENT, INVALID}
    assert [r.mutant.id for r in results] == [m.id for m in mutants]
    for r in results:
        assert bool(r.killed_by) == (r.verdict == KILLED), r.mutant.id
        assert bool(r.changed_models) == (r.verdict == SURVIVED), r.mutant.id


def test_rerunning_a_mutant_gives_the_same_result(engine, project, by_id, ids):
    for mutant_id in (ids["aor_days"], ids["mart_lcr"]):
        again = engine.run_mutant(find_mutant(project, mutant_id))
        first = by_id[mutant_id]
        assert (again.verdict, again.killed_by, again.impact) == (
            first.verdict,
            first.killed_by,
            first.impact,
        )


def test_fail_fast_stops_at_the_first_failing_test(engine, project, ids):
    mutant = find_mutant(project, ids["start_boundary"])
    assert len(engine.run_mutant(mutant).killed_by) > 1
    assert len(engine.run_mutant(mutant, fail_fast=True).killed_by) == 1


def test_scores_order_and_match_the_json_report(report):
    loaded = json.loads(json.dumps(report))
    rows = {m["model"]: m for m in loaded["models"]}
    assert rows["fct_mrr_movements"]["score"] > rows["mart_arr_by_segment"]["score"]
    for row in loaded["models"]:
        verdicts = Counter(m["verdict"] for m in loaded["mutants"] if m["model"] == row["model"])
        killed, survived = verdicts[KILLED], verdicts[SURVIVED]
        assert (row["killed"], row["survived"]) == (killed, survived)
        if killed + survived:
            assert row["score"] == pytest.approx(killed / (killed + survived))
        else:
            assert row["score"] is None
    assert score(0, 0) is None and score(3, 1) == 0.75


def test_survivors_are_ordered_by_absolute_mrr_impact(results):
    ranked = ranked_survivors(results)
    impacts = [abs(r.impact["mrr"]) for r in ranked]
    assert impacts == sorted(impacts, reverse=True)
    assert len(ranked) >= 10
    assert ranked[0].impact["mrr"] != 0


def test_unit_evaluator_passes_every_baseline_unit_test(engine, project):
    for unit_test in project.unit_tests:
        outcome = run_unit_test(engine.con, unit_test, parse(project.models[unit_test.model].sql))
        assert outcome.passed, (outcome.label, outcome.detail)


def test_unit_evaluator_detects_wrong_expectations(engine, project):
    original = next(
        u for u in project.unit_tests if u.name == "movements_use_annual_price_and_credit_rate"
    )
    tree = parse(project.models["fct_mrr_movements"].sql)
    wrong_value = UnitTest(
        original.name,
        original.unique_id,
        original.model,
        original.given,
        ({**original.expect_rows[0], "mrr_after": 111.76},),
    )
    outcome = run_unit_test(engine.con, wrong_value, tree)
    assert not outcome.passed and outcome.detail == "1 unexpected row(s), 1 missing row(s)"
    assert not outcome.errored, "a wrong expectation is a failure, not an error"
    extra_row = UnitTest(original.name, original.unique_id, original.model, original.given, ())
    assert (
        run_unit_test(engine.con, extra_row, tree).detail == "1 unexpected row(s), 0 missing row(s)"
    )
    missing_column = UnitTest(
        original.name, original.unique_id, original.model, original.given, ({"nope": 1},)
    )
    outcome = run_unit_test(engine.con, missing_column, tree)
    assert "missing expected column" in outcome.detail and outcome.errored and not outcome.passed
    bad_fixture = UnitTest(
        original.name,
        original.unique_id,
        original.model,
        (GivenInput("ref", "stg_plan_prices", ({"not_a_column": 1},)),),
        original.expect_rows,
    )
    bad = run_unit_test(engine.con, bad_fixture, tree)
    assert bad.detail.startswith("error: ") and bad.errored


def test_unit_evaluator_compares_rows_as_a_multiset(engine, project):
    original = next(u for u in project.unit_tests if u.name == "movements_cover_all_five_types")
    tree = parse(project.models["fct_mrr_movements"].sql)
    reordered = UnitTest(
        original.name,
        original.unique_id,
        original.model,
        original.given,
        tuple(reversed(original.expect_rows)),
    )
    assert run_unit_test(engine.con, reordered, tree).passed


def test_run_mutant_requires_a_baseline(project, ids):
    fresh = Engine(project)
    with pytest.raises(RuntimeError):
        fresh.run_mutant(find_mutant(project, ids["count_distinct"]))


def test_a_unit_test_that_errors_makes_the_mutant_invalid_as_in_dbt(
    engine, project, ids, monkeypatch
):
    """The engine and crosscheck.classify_run_results use one rule: a unit-test error is invalid."""
    from dbtmutant.crosscheck import classify_run_results

    base = next(u for u in project.unit_tests if u.name == "movements_cover_all_five_types")
    broken = UnitTest(
        "needs_a_missing_column",
        "unit_test.broken",
        "fct_mrr_movements",
        base.given,
        ({"no_such_column": 1},),
    )
    original = list(project.unit_tests)
    monkeypatch.setattr(engine.project, "unit_tests", [*original, broken])
    mutant = find_mutant(project, ids["unit_killed"])
    result = engine.run_mutant(mutant)
    assert result.verdict == INVALID
    assert "needs_a_missing_column" in result.error and result.killed_by == []
    dbt_view = classify_run_results([{"unique_id": "unit_test.p.m.c", "status": "error"}])
    assert dbt_view.verdict == result.verdict
    # A unit test that evaluates and fails is still a kill.
    monkeypatch.setattr(engine.project, "unit_tests", original)
    assert engine.run_mutant(mutant).verdict == KILLED
