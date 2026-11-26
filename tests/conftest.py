from __future__ import annotations

from pathlib import Path

import pytest

from dbtmutant.crosscheck import CrossChecker
from dbtmutant.engine import Engine
from dbtmutant.loader import Project, load_project
from dbtmutant.mutants import find_mutant, project_mutants
from dbtmutant.report import build_report, ranked_survivors

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "saas_billing"


def locate(mutants, model: str, operator: str, added: str) -> str:
    """Id of the one ``model``/``operator`` mutant whose diff adds a line containing ``added``.

    Locating mutants by what they change, not by their position, keeps the tests valid when the
    SQL printer lays a model out differently or an operator gains a site.
    """
    found = []
    for m in mutants:
        if m.model != model or m.operator != operator:
            continue
        plus = " ".join(
            ln[1:].strip()
            for ln in m.diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        )
        if added in " ".join(plus.split()):
            found.append(m.id)
    assert len(found) == 1, f"{model} {operator} {added!r} matched {found}"
    return found[0]


@pytest.fixture(scope="session")
def project() -> Project:
    return load_project(EXAMPLE)


@pytest.fixture(scope="session")
def engine(project):
    eng = Engine(project)
    eng.baseline_result = eng.build_baseline()
    return eng


@pytest.fixture(scope="session")
def mutants(project):
    return project_mutants(project)


@pytest.fixture(scope="session")
def ids(mutants):
    """Ids of the mutants the tests talk about, found by what they change."""
    return {
        # c.day < p.end_date -> c.day <= p.end_date, the planted day-boundary bug
        "boundary": locate(mutants, "int_subscription_days", "ROR", "c.day <= p.end_date"),
        "start_boundary": locate(mutants, "int_subscription_days", "ROR", "c.day > p.start_date"),
        # f.day <= last day of data -> f.day < last day of data, the planted month-end filter
        "mart_boundary": locate(
            mutants, "mart_arr_by_segment", "ROR", "f.day < b.last_day_of_data"
        ),
        "mart_lcr": locate(mutants, "mart_arr_by_segment", "LCR", "OR f.day <= b.last_day_of_data"),
        "count_distinct": locate(mutants, "mart_arr_by_segment", "AGG", "COUNT(account_id)"),
        # swaps the 'new' and 'reactivation' labels: only a unit test can see it
        "unit_killed": locate(mutants, "fct_mrr_movements", "CASE_SWAP", "THEN 'reactivation'"),
        "interval_zero": locate(
            mutants, "int_subscription_days", "INTERVAL", "INTERVAL '0' DAY) AS t(day)"
        ),
        "aor_days": locate(mutants, "int_subscription_days", "AOR", "price * 12"),
        "coalesce_credits": locate(mutants, "int_account_credits", "COALESCE", "CAST("),
        "inner_invoices": locate(mutants, "int_account_credits", "JOIN", "JOIN main.stg_invoices"),
    }


@pytest.fixture(scope="session")
def results(engine, mutants):
    return engine.run_all(mutants)


@pytest.fixture(scope="session")
def by_id(results):
    return {r.mutant.id: r for r in results}


@pytest.fixture(scope="session")
def report(project, results):
    return build_report(project, results)


@pytest.fixture(scope="session")
def top_survivor(results):
    return ranked_survivors(results)[0]


@pytest.fixture(scope="session")
def checker(project, tmp_path_factory):
    with CrossChecker(project, tmp_path_factory.mktemp("crosscheck")) as xc:
        yield xc


@pytest.fixture(scope="session")
def crosschecks(project, engine, checker, ids):
    """Cross-check of three fixed mutants: a data-test kill, a unit-test kill and a survivor."""
    out = {}
    for key in ("boundary", "unit_killed", "mart_boundary"):
        out[key] = checker.check(engine.run_mutant(find_mutant(project, ids[key])))
    return out
