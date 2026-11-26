"""The mutation engine: run each mutant against the project's tests in a scratch DuckDB schema."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

import duckdb
from sqlglot import exp

from .loader import DataTest, Project
from .mutants import Mutant
from .sqlutil import DIALECT, parse, rewrite_relations
from .unit_eval import BASELINE_SCHEMA, UnitOutcome, run_unit_test

SCRATCH_SCHEMA = "dbtmutant_mut"

KILLED = "killed"
SURVIVED = "survived"
EQUIVALENT = "equivalent_on_data"
INVALID = "invalid"
VERDICTS = (KILLED, SURVIVED, EQUIVALENT, INVALID)

_THRESHOLD = re.compile(r"^\s*(>=|<=|!=|==|=|>|<)\s*(\d+)\s*$")
_COMPARE = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "!=": lambda a, b: a != b,
    "==": lambda a, b: a == b,
    "=": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def threshold_met(expression: str, failures: int) -> bool:
    """Evaluate a dbt ``error_if`` / ``warn_if`` expression such as ``!= 0`` or ``> 10``."""
    match = _THRESHOLD.match(expression)
    if match is None:
        raise ValueError(f"unsupported test threshold {expression!r}")
    return _COMPARE[match.group(1)](failures, int(match.group(2)))


@dataclass(frozen=True)
class TestOutcome:
    __test__ = False  # not a pytest class

    name: str
    status: str  # pass | warn | fail | error
    failures: int = 0
    detail: str = ""

    @property
    def kills(self) -> bool:
        return self.status in ("fail", "error")


@dataclass
class BaselineResult:
    failed_tests: list[str]
    failed_unit_tests: list[str]
    warned_tests: list[str]
    test_count: int
    unit_test_count: int
    seconds: float

    @property
    def clean(self) -> bool:
        return not self.failed_tests and not self.failed_unit_tests


@dataclass
class MutantResult:
    mutant: Mutant
    verdict: str
    killed_by: list[str] = field(default_factory=list)
    error: str = ""
    changed_models: dict[str, int] = field(default_factory=dict)
    impact: dict[str, float] = field(default_factory=dict)
    seconds: float = 0.0


class Engine:
    """Holds the baseline warehouse and evaluates mutants against it."""

    def __init__(self, project: Project, threads: int | None = None):
        self.project = project
        self.con = duckdb.connect(":memory:")
        if threads:
            self.con.execute(f"PRAGMA threads={int(threads)}")
        self._trees: dict[str, exp.Expression] = {
            n: parse(m.sql) for n, m in project.models.items()
        }
        self._order = project.topo_order()
        self._baseline_totals: dict[str, Decimal] = {}
        self._periods: dict[str, object] = {}
        self._built = False

    # -- baseline ---------------------------------------------------------------------------

    def build_baseline(self) -> BaselineResult:
        """Load seeds, build every model, and run every test once on the unmutated project."""
        start = time.perf_counter()
        con = self.con
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {BASELINE_SCHEMA}")
        for seed in self.project.seeds.values():
            path = str(seed.path).replace("'", "''")
            types = ", ".join(f"'{c}': '{t}'" for c, t in seed.column_types.items())
            options = f", types={{{types}}}" if types else ""
            con.execute(
                f"CREATE OR REPLACE TABLE {BASELINE_SCHEMA}.{_q(seed.name)} AS "
                f"SELECT * FROM read_csv('{path}', header=true{options})"
            )
        for name in self._order:
            self._create_model(BASELINE_SCHEMA, name, self._trees[name], {})
        self._periods = {}
        for metric, cfg in self.project.impact.items():
            if cfg.get("period_column"):
                self._periods[metric] = con.execute(
                    f"SELECT MAX({_q(cfg['period_column'])}) FROM "
                    f"{BASELINE_SCHEMA}.{_q(cfg['model'])}"
                ).fetchone()[0]
        self._baseline_totals = {
            metric: self._total(BASELINE_SCHEMA, metric, cfg)
            for metric, cfg in self.project.impact.items()
        }
        self._built = True

        everything = set(self.project.models) | set(self.project.seeds)
        outcomes = [self._run_data_test(t, {}) for t in self.project.tests_for(everything)]
        units = [run_unit_test(con, ut, self._trees[ut.model]) for ut in self.project.unit_tests]
        return BaselineResult(
            failed_tests=[o.name for o in outcomes if o.kills],
            failed_unit_tests=[u.label for u in units if not u.passed],
            warned_tests=[o.name for o in outcomes if o.status == "warn"],
            test_count=len(outcomes),
            unit_test_count=len(units),
            seconds=time.perf_counter() - start,
        )

    def _create_model(
        self, schema: str, name: str, tree: exp.Expression, mapping: dict[str, str]
    ) -> None:
        model = self.project.models[name]
        sql = rewrite_relations(tree, mapping, BASELINE_SCHEMA).sql(dialect=DIALECT)
        kind = "VIEW" if model.materialized == "view" else "TABLE"
        self.con.execute(f"CREATE OR REPLACE {kind} {schema}.{_q(name)} AS {sql}")

    def _total(self, schema: str, metric: str, cfg: dict[str, str]) -> Decimal:
        """Summed impact column, at the baseline's latest period when ``period_column`` is set."""
        sql = f"SELECT SUM({_q(cfg['column'])}) FROM {schema}.{_q(cfg['model'])}"
        params: list = []
        if cfg.get("period_column"):
            sql += f" WHERE {_q(cfg['period_column'])} = ?"
            params = [self._periods[metric]]
        row = self.con.execute(sql, params).fetchone()
        return Decimal(str(row[0])) if row[0] is not None else Decimal(0)

    # -- tests ------------------------------------------------------------------------------

    def _run_data_test(self, test: DataTest, mapping: dict[str, str]) -> TestOutcome:
        sql = rewrite_relations(parse(test.sql), mapping, BASELINE_SCHEMA).sql(dialect=DIALECT)
        try:
            failures = self.con.execute(
                f"SELECT count(*) FROM ({sql}) AS dbtmutant_test"
            ).fetchone()[0]
        except duckdb.Error as exc:
            return TestOutcome(test.name, "error", 0, str(exc).splitlines()[0])
        if test.severity == "error" and threshold_met(test.error_if, failures):
            return TestOutcome(test.name, "fail", failures)
        if threshold_met(test.warn_if, failures):
            return TestOutcome(test.name, "warn", failures)
        return TestOutcome(test.name, "pass", failures)

    # -- mutants ----------------------------------------------------------------------------

    def run_mutant(self, mutant: Mutant, fail_fast: bool = False) -> MutantResult:
        """Evaluate one mutant and classify it."""
        if not self._built:
            raise RuntimeError("build_baseline() must run before run_mutant()")
        start = time.perf_counter()
        affected = {mutant.model} | self.project.descendants(mutant.model)
        mapping = dict.fromkeys(affected, SCRATCH_SCHEMA)
        con = self.con
        con.execute(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE")
        con.execute(f"CREATE SCHEMA {SCRATCH_SCHEMA}")
        try:
            return self._evaluate(mutant, affected, mapping, fail_fast, start)
        finally:
            con.execute(f"DROP SCHEMA IF EXISTS {SCRATCH_SCHEMA} CASCADE")

    def _evaluate(self, mutant, affected, mapping, fail_fast, start) -> MutantResult:
        mutated_tree = parse(mutant.mutated_sql)

        def result(verdict: str, **kwargs) -> MutantResult:
            return MutantResult(mutant, verdict, seconds=time.perf_counter() - start, **kwargs)

        try:
            for name in self._order:
                if name in affected:
                    tree = mutated_tree if name == mutant.model else self._trees[name]
                    self._create_model(SCRATCH_SCHEMA, name, tree, mapping)
        except duckdb.Error as exc:
            return result(INVALID, error=str(exc).splitlines()[0])

        killed_by: list[str] = []
        for unit_test in self.project.unit_tests:
            if unit_test.model != mutant.model:
                continue
            outcome: UnitOutcome = run_unit_test(self.con, unit_test, mutated_tree)
            if outcome.errored:
                # dbt reports an erroring unit test as an error, not a failure: same rule as
                # crosscheck.classify_run_results, so the mutant is invalid, not killed.
                return result(INVALID, error=f"unit test {outcome.label}: {outcome.detail}")
            if not outcome.passed:
                killed_by.append(outcome.label)
                if fail_fast:
                    return result(KILLED, killed_by=killed_by)
        for test in self.project.tests_for(affected):
            if self._run_data_test(test, mapping).kills:
                killed_by.append(test.name)
                if fail_fast:
                    return result(KILLED, killed_by=killed_by)
        if killed_by:
            return result(KILLED, killed_by=killed_by)

        changed = {}
        for name in self._order:
            if name in affected:
                diff = self._rows_changed(name)
                if diff:
                    changed[name] = diff
        if not changed:
            return result(EQUIVALENT)
        return result(SURVIVED, changed_models=changed, impact=self._impact(affected))

    def _rows_changed(self, name: str) -> int:
        """Rows that differ between baseline and scratch output (multiset, both directions)."""
        base = f"{BASELINE_SCHEMA}.{_q(name)}"
        mut = f"{SCRATCH_SCHEMA}.{_q(name)}"
        try:
            lost = self.con.execute(
                f"SELECT count(*) FROM (SELECT * FROM {base} EXCEPT ALL SELECT * FROM {mut})"
            ).fetchone()[0]
            gained = self.con.execute(
                f"SELECT count(*) FROM (SELECT * FROM {mut} EXCEPT ALL SELECT * FROM {base})"
            ).fetchone()[0]
        except duckdb.Error:
            # The columns no longer line up, so the output certainly changed.
            return max(
                self.con.execute(f"SELECT count(*) FROM {base}").fetchone()[0],
                self.con.execute(f"SELECT count(*) FROM {mut}").fetchone()[0],
                1,
            )
        return lost + gained

    def _impact(self, affected: set[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for metric, cfg in self.project.impact.items():
            if cfg["model"] in affected:
                delta = self._total(SCRATCH_SCHEMA, metric, cfg) - self._baseline_totals[metric]
            else:
                delta = Decimal(0)
            out[metric] = float(round(delta, 2))
        return out

    def run_all(
        self,
        mutants: list[Mutant],
        fail_fast: bool = False,
        progress: Callable[[MutantResult], None] | None = None,
    ) -> list[MutantResult]:
        results = []
        for mutant in mutants:
            res = self.run_mutant(mutant, fail_fast=fail_fast)
            results.append(res)
            if progress:
                progress(res)
        return results
