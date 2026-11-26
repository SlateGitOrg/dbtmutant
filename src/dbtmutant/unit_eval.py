"""Evaluate dbt unit tests inside DuckDB without dbt.

Given rows become tables typed from the baseline relation, the model's references are pointed at
them, and the model SQL runs on top.  The result is compared with the expected rows on the
expected columns as a multiset (``EXCEPT ALL`` both ways), the way dbt compares them.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
from sqlglot import exp

from .loader import ProjectError, UnitTest
from .sqlutil import DIALECT, rewrite_relations

UNIT_SCHEMA = "dbtmutant_unit"
BASELINE_SCHEMA = "main"


@dataclass(frozen=True)
class UnitOutcome:
    name: str
    model: str
    passed: bool
    detail: str = ""
    errored: bool = False  # the test could not be evaluated (as opposed to evaluated and failed)

    @property
    def label(self) -> str:
        return f"{self.model}::{self.name}"


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _describe(con: duckdb.DuckDBPyConnection, relation: str) -> list[tuple[str, str]]:
    return [(row[0], row[1]) for row in con.execute(f"DESCRIBE {relation}").fetchall()]


def _insert(
    con: duckdb.DuckDBPyConnection,
    relation: str,
    columns: list[tuple[str, str]],
    rows: list[dict],
) -> None:
    if not rows:
        return
    known = {name for name, _ in columns}
    for row in rows:
        extra = set(row) - known
        if extra:
            raise duckdb.Error(f"unknown column(s) in fixture: {', '.join(sorted(extra))}")
    placeholders = ", ".join(f"CAST(? AS {ctype})" for _, ctype in columns)
    sql = f"INSERT INTO {relation} VALUES ({placeholders})"
    values = [
        [None if row.get(name) is None else str(row[name]) for name, _ in columns] for row in rows
    ]
    con.executemany(sql, values)


def _create(con: duckdb.DuckDBPyConnection, relation: str, columns: list[tuple[str, str]]) -> None:
    body = ", ".join(f"{_q(name)} {ctype}" for name, ctype in columns)
    con.execute(f"CREATE TABLE {relation} ({body})")


def run_unit_test(
    con: duckdb.DuckDBPyConnection, unit_test: UnitTest, model_tree: exp.Expression
) -> UnitOutcome:
    """Run one unit test against ``model_tree`` (the model's SQL, possibly mutated)."""

    def outcome(passed: bool, detail: str = "", errored: bool = False) -> UnitOutcome:
        return UnitOutcome(unit_test.name, unit_test.model, passed, detail, errored)

    con.execute(f"DROP SCHEMA IF EXISTS {UNIT_SCHEMA} CASCADE")
    con.execute(f"CREATE SCHEMA {UNIT_SCHEMA}")
    try:
        mapping: dict[str, str] = {}
        for given in unit_test.given:
            if given.kind != "ref":
                raise ProjectError(f"unit test {unit_test.name}: only ref() inputs are supported")
            columns = _describe(con, f"{BASELINE_SCHEMA}.{_q(given.name)}")
            relation = f"{UNIT_SCHEMA}.{_q(given.name)}"
            _create(con, relation, columns)
            _insert(con, relation, columns, list(given.rows))
            mapping[given.name] = UNIT_SCHEMA

        sql = rewrite_relations(model_tree, mapping, BASELINE_SCHEMA).sql(dialect=DIALECT)
        con.execute(f"CREATE TABLE {UNIT_SCHEMA}.__actual AS {sql}")
        actual = _describe(con, f"{UNIT_SCHEMA}.__actual")
        actual_types = dict(actual)

        expected_names: list[str] = []
        for row in unit_test.expect_rows:
            for key in row:
                if key not in expected_names:
                    expected_names.append(key)
        if not expected_names:
            expected_names = [name for name, _ in actual]
        missing = [n for n in expected_names if n not in actual_types]
        if missing:
            return outcome(False, f"missing expected column(s): {', '.join(missing)}", errored=True)

        columns = [(name, actual_types[name]) for name in expected_names]
        _create(con, f"{UNIT_SCHEMA}.__expected", columns)
        _insert(con, f"{UNIT_SCHEMA}.__expected", columns, list(unit_test.expect_rows))
        cols = ", ".join(_q(n) for n in expected_names)
        a = f"SELECT {cols} FROM {UNIT_SCHEMA}.__actual"
        e = f"SELECT {cols} FROM {UNIT_SCHEMA}.__expected"
        extra = con.execute(f"SELECT count(*) FROM ({a} EXCEPT ALL {e})").fetchone()[0]
        lost = con.execute(f"SELECT count(*) FROM ({e} EXCEPT ALL {a})").fetchone()[0]
        if extra or lost:
            return outcome(False, f"{extra} unexpected row(s), {lost} missing row(s)")
        return outcome(True)
    except duckdb.Error as exc:
        return outcome(False, f"error: {str(exc).splitlines()[0]}", errored=True)
    finally:
        con.execute(f"DROP SCHEMA IF EXISTS {UNIT_SCHEMA} CASCADE")
