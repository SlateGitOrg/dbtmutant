from __future__ import annotations

import pytest

from dbtmutant.loader import ProjectError, load_project


def test_models_and_materialisations(project):
    assert set(project.models) == {
        "stg_accounts",
        "stg_plans",
        "stg_plan_prices",
        "stg_subscription_events",
        "stg_invoices",
        "stg_credit_notes",
        "int_subscription_days",
        "int_account_credits",
        "fct_mrr_daily",
        "fct_mrr_movements",
        "mart_arr_by_segment",
    }
    assert project.models["stg_accounts"].materialized == "view"
    assert project.models["fct_mrr_daily"].materialized == "table"
    assert (
        project.models["int_subscription_days"].file
        == "models/intermediate/int_subscription_days.sql"
    )


def test_compiled_sql_is_normalised_and_keeps_raw_source(project):
    model = project.models["fct_mrr_daily"]
    assert "main.int_subscription_days" in model.sql
    assert '"' not in model.sql, "no database name or quoting leaks from dbt's compiled SQL"
    assert "{{ ref('int_subscription_days') }}" in model.raw_sql


def test_dependency_graph(project):
    order = project.topo_order()
    for name, model in project.models.items():
        for parent in model.depends_on:
            if parent in project.models:
                assert order.index(parent) < order.index(name)
    assert project.models["mart_arr_by_segment"].depends_on == ("fct_mrr_daily",)
    assert project.descendants("int_subscription_days") == {"fct_mrr_daily", "mart_arr_by_segment"}
    assert project.descendants("mart_arr_by_segment") == set()
    assert "fct_mrr_movements" in project.descendants("int_account_credits")


def test_data_tests_are_loaded_with_their_dependencies(project):
    assert len(project.tests) == 32
    reconcile = next(t for t in project.tests if t.name == "mrr_movements_reconcile")
    assert set(reconcile.depends_on) == {"fct_mrr_movements", "fct_mrr_daily"}
    assert reconcile.attached_node is None
    assert reconcile.severity == "error"
    unique = next(t for t in project.tests if t.name.startswith("unique_stg_accounts_account_id"))
    assert unique.attached_node == "stg_accounts"
    mart_tests = project.tests_for({"mart_arr_by_segment"})
    assert all("mart_arr_by_segment" in t.name for t in mart_tests)
    assert len(mart_tests) == 4, "the mart has only key tests"


def test_unit_tests_are_loaded_with_given_and_expected_rows(project):
    names = {(u.model, u.name) for u in project.unit_tests}
    assert ("fct_mrr_movements", "movements_cover_all_five_types") in names
    assert {u.model for u in project.unit_tests} == {"int_subscription_days", "fct_mrr_movements"}
    five = next(u for u in project.unit_tests if u.name == "movements_cover_all_five_types")
    assert [(g.kind, g.name) for g in five.given] == [
        ("ref", "stg_subscription_events"),
        ("ref", "stg_plan_prices"),
        ("ref", "int_account_credits"),
    ]
    assert len(five.given[0].rows) == 5
    assert [r["movement_type"] for r in five.expect_rows] == [
        "new",
        "expansion",
        "contraction",
        "churn",
        "reactivation",
    ]


def test_seeds_carry_their_column_types(project):
    prices = project.seeds["plan_prices"]
    assert prices.column_types["price"] == "decimal(10,2)"
    assert prices.path.exists()
    assert project.impact["mrr"] == {
        "model": "mart_arr_by_segment",
        "column": "mrr",
        "period_column": "month_end",
    }


def test_not_a_dbt_project(tmp_path):
    with pytest.raises(ProjectError):
        load_project(tmp_path)


def test_releasing_the_warehouse_degrades_when_adapter_internals_move(monkeypatch):
    import sys
    import types

    from dbtmutant.loader import _release_warehouse

    module = "dbt.adapters.duckdb.connections"
    monkeypatch.setitem(sys.modules, module, None)  # import fails
    _release_warehouse()

    class NoClose:
        pass

    renamed = types.ModuleType(module)
    renamed.DuckDBConnectionManager = types.SimpleNamespace(_ENV=NoClose())  # no close()
    monkeypatch.setitem(sys.modules, module, renamed)
    _release_warehouse()

    missing = types.ModuleType(module)
    missing.DuckDBConnectionManager = types.SimpleNamespace()  # no _ENV at all
    monkeypatch.setitem(sys.modules, module, missing)
    _release_warehouse()
