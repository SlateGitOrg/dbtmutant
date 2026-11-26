from __future__ import annotations

import json

import duckdb
import pytest

from dbtmutant.crosscheck import classify_run_results, copy_project, dbt_class, mutate_source
from dbtmutant.loader import run_dbt
from dbtmutant.mutants import find_mutant


def _result(unique_id: str, status: str) -> dict:
    return {"unique_id": unique_id, "status": status}


def test_classify_run_results():
    ok = [_result("model.p.a", "success"), _result("test.p.t.abc", "pass")]
    assert classify_run_results(ok).verdict == "survived"
    warn = [_result("test.p.t.abc", "warn")]
    assert classify_run_results(warn).verdict == "survived"
    failed = ok + [_result("test.p.reconcile", "fail")]
    run = classify_run_results(failed)
    assert (run.verdict, run.failed) == ("killed", ["reconcile"])
    unit = classify_run_results([_result("unit_test.p.m.my_case", "fail")])
    assert (unit.verdict, unit.failed) == ("killed", ["m::my_case"])
    assert classify_run_results([_result("model.p.a", "error")]).verdict == "invalid"
    assert classify_run_results([_result("unit_test.p.m.c", "error")]).verdict == "invalid"
    assert classify_run_results([_result("test.p.t.1", "error")]).verdict == "killed"
    assert dbt_class("equivalent_on_data") == "survived" and dbt_class("killed") == "killed"


def test_dbt_build_of_the_baseline_passes(checker, project):
    base = checker.baseline()
    assert base.verdict == "survived" and base.failed == [] and base.errors == []
    run_results = json.loads(
        (checker.work_dir / "baseline" / "target" / "run_results.json").read_text()
    )
    statuses = {r["unique_id"].split(".")[0]: set() for r in run_results["results"]}
    for r in run_results["results"]:
        statuses[r["unique_id"].split(".")[0]].add(r["status"])
    assert statuses == {
        "seed": {"success"},
        "model": {"success"},
        "test": {"pass"},
        "unit_test": {"pass"},
    }
    assert sum(r["unique_id"].startswith("test.") for r in run_results["results"]) == 32


def test_engine_unit_tests_agree_with_dbt_unit_tests(checker, project, engine):
    checker.baseline()
    run_results = json.loads(
        (checker.work_dir / "baseline" / "target" / "run_results.json").read_text()
    )
    dbt_units = {
        r["unique_id"].split(".", 2)[2]: r["status"]
        for r in run_results["results"]
        if r["unique_id"].startswith("unit_test.")
    }
    assert dbt_units == {f"{u.model}.{u.name}": "pass" for u in project.unit_tests}
    assert engine.baseline_result.failed_unit_tests == []


def test_engine_baseline_tables_equal_dbts(checker, engine, project):
    checker.baseline()
    db = checker.work_dir / "baseline" / "warehouse.duckdb"
    con = engine.con
    con.execute(f"ATTACH '{db}' AS dbtwh (READ_ONLY)")
    try:
        for name in [*project.seeds, *project.models]:
            for a, b in (("main", "dbtwh.main"), ("dbtwh.main", "main")):
                query = (
                    f'SELECT count(*) FROM (SELECT * FROM {a}."{name}" '
                    f'EXCEPT ALL SELECT * FROM {b}."{name}")'
                )
                extra = con.execute(query).fetchone()[0]
                assert extra == 0, f"{name} differs between engine and dbt"
    finally:
        con.execute("DETACH dbtwh")


def test_mutation_is_applied_to_the_source_file_with_refs_restored(project, tmp_path, ids):
    copy_project(project.root, tmp_path / "copy")
    mutant = find_mutant(project, ids["boundary"])
    mutate_source(project, mutant, tmp_path / "copy")
    text = (tmp_path / "copy" / project.models[mutant.model].file).read_text()
    assert "{{ ref('stg_subscription_events') }}" in text and "{{ ref('stg_plan_prices') }}" in text
    assert "c.day <= p.end_date" in text and "c.day < p.end_date" not in text
    original = (project.root / project.models[mutant.model].file).read_text()
    assert original != text and "c.day < p.end_date" in original, "the real project is untouched"


def test_three_mutants_get_the_same_verdict_from_dbt_build(crosschecks):
    assert {k: (v.engine_verdict, v.dbt_verdict) for k, v in crosschecks.items()} == {
        "boundary": ("killed", "killed"),
        "unit_killed": ("killed", "killed"),
        "mart_boundary": ("survived", "survived"),
    }
    assert all(c.agree for c in crosschecks.values())


def test_data_test_kill_names_the_same_test_in_dbt(crosschecks):
    boundary = crosschecks["boundary"]
    assert boundary.engine_killed_by == ["mrr_movements_reconcile"] == boundary.dbt_failed


def test_engine_unit_test_verdicts_equal_dbts_on_a_unit_test_kill(
    crosschecks, checker, project, ids
):
    unit = crosschecks["unit_killed"]
    engine_failed = {t for t in unit.engine_killed_by if "::" in t}
    assert engine_failed == {
        "fct_mrr_movements::movements_cover_all_five_types",
        "fct_mrr_movements::movements_skip_renewals_and_unchanged_mrr",
        "fct_mrr_movements::movements_use_annual_price_and_credit_rate",
    }
    # dbt build stops at the first failing unit test and skips its siblings, so ask dbt to run every
    # unit test of the mutated copy on its own and compare each one.
    ran = checker.runs[ids["unit_killed"]]
    assert (
        "reactivation" in (ran.project_dir / project.models["fct_mrr_movements"].file).read_text()
    )
    work = ran.work_dir.parent / "unit_tests_of_the_unit_killed_mutant"
    run_dbt(["test", "--select", "test_type:unit"], ran.project_dir, work, ran.warehouse)
    results = json.loads((work / "target" / "run_results.json").read_text())["results"]
    dbt_failed = {
        "::".join(r["unique_id"].split(".")[-2:]) for r in results if r["status"] == "fail"
    }
    dbt_passed = {
        "::".join(r["unique_id"].split(".")[-2:]) for r in results if r["status"] == "pass"
    }
    assert dbt_failed == engine_failed
    assert dbt_passed == {f"{u.model}::{u.name}" for u in project.unit_tests} - engine_failed


def test_surviving_mutant_impact_matches_dbt(crosschecks):
    mart = crosschecks["mart_boundary"]
    assert mart.impact_agrees is True
    assert mart.dbt_impact["arr"] == pytest.approx(mart.engine_impact["arr"], abs=0.01)
    assert mart.dbt_impact["mrr"] < 0


def test_top_survivor_impact_equals_an_independent_dbt_recomputation(checker, top_survivor):
    check = checker.check(top_survivor)
    assert check.engine_verdict == "survived" and check.agree
    baseline = checker.baseline()
    period = baseline.periods["mrr"]
    con = duckdb.connect(str(checker.runs[top_survivor.mutant.id].warehouse), read_only=True)
    try:
        mutated_total = con.execute(
            "SELECT SUM(mrr) FROM main.mart_arr_by_segment WHERE month_end = ?", [period]
        ).fetchone()[0]
    finally:
        con.close()
    expected = float(mutated_total or 0) - baseline.totals["mrr"]
    assert expected == pytest.approx(top_survivor.impact["mrr"], abs=0.01)
    assert check.impact_agrees is True
