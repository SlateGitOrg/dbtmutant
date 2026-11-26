"""Cross-check engine verdicts against real ``dbt build`` runs of mutated project copies."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .engine import EQUIVALENT, INVALID, KILLED, SURVIVED, MutantResult
from .loader import Project, ProjectError, run_dbt
from .mutants import Mutant, generate_mutants
from .operators import get_operators
from .sqlutil import restore_refs, swap_refs


@dataclass
class DbtRun:
    """Outcome of one ``dbt build``."""

    verdict: str  # killed | survived | invalid
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)  # impact columns at the latest period
    periods: dict[str, object] = field(default_factory=dict)  # latest period per impact metric


@dataclass(frozen=True)
class MutantRun:
    """Where a mutant's dbt build ran."""

    project_dir: Path
    work_dir: Path
    warehouse: Path


@dataclass
class CrossCheck:
    mutant_id: str
    engine_verdict: str
    dbt_verdict: str
    agree: bool
    engine_killed_by: list[str]
    dbt_failed: list[str]
    engine_impact: dict[str, float]
    dbt_impact: dict[str, float] | None
    impact_agrees: bool | None


def dbt_class(engine_verdict: str) -> str:
    """The dbt-visible class of an engine verdict (dbt cannot tell survived from equivalent)."""
    return SURVIVED if engine_verdict == EQUIVALENT else engine_verdict


def classify_run_results(results: list[dict]) -> DbtRun:
    """Turn ``run_results.json`` entries of a ``dbt build`` into a verdict."""
    failed: list[str] = []
    errors: list[str] = []
    invalid = False
    for item in results:
        kind = item["unique_id"].split(".", 1)[0]
        name = item["unique_id"].split(".")[-1]
        status = item["status"]
        if kind == "unit_test":
            parts = item["unique_id"].split(".")
            name = f"{parts[-2]}::{parts[-1]}"
        if status == "error":
            errors.append(name)
            if kind in ("model", "seed", "unit_test"):
                invalid = True
            else:
                failed.append(name)
        elif status == "fail":
            failed.append(name)
    if invalid:
        return DbtRun(INVALID, failed, errors)
    return DbtRun(KILLED if failed else SURVIVED, failed, errors)


def copy_project(root: Path, dest: Path) -> None:
    shutil.copytree(
        root,
        dest,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal"),
    )


def mutate_source(project: Project, mutant: Mutant, copy_root: Path) -> None:
    """Apply the mutant's operator at the same occurrence to the model's source file."""
    model = project.models[mutant.model]
    operators = get_operators([mutant.operator])
    expected = generate_mutants(mutant.model, model.sql, operators)
    path = copy_root / model.file
    source, placeholders = swap_refs(path.read_text())
    candidates = generate_mutants(mutant.model, source, operators)
    if len(candidates) != len(expected) or mutant.n >= len(candidates):
        raise ProjectError(
            f"{mutant.id}: the source file and the compiled SQL give different mutants "
            f"({len(candidates)} vs {len(expected)}); models may use no Jinja except ref and source"
        )
    path.write_text(restore_refs(candidates[mutant.n].mutated_sql, placeholders) + "\n")


class CrossChecker:
    """Runs ``dbt build`` on the baseline and on mutated copies of a project."""

    def __init__(self, project: Project, work_dir: Path | None = None):
        self.project = project
        self._owned = work_dir is None
        self.work_dir = (
            Path(tempfile.mkdtemp(prefix="dbtmutant-xc-")) if work_dir is None else work_dir
        )
        self._baseline: DbtRun | None = None
        self.runs: dict[str, MutantRun] = {}
        self._count = 0

    def close(self) -> None:
        if self._owned:
            shutil.rmtree(self.work_dir, ignore_errors=True)

    def __enter__(self) -> CrossChecker:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _build(self, root: Path, label: str, periods: dict | None = None) -> DbtRun:
        work = self.work_dir / label
        work.mkdir(parents=True, exist_ok=True)
        db = work / "warehouse.duckdb"
        result = run_dbt(["build"], root, work, db)
        run_results = work / "target" / "run_results.json"
        if not run_results.exists():
            return DbtRun(INVALID, errors=[str(getattr(result, "exception", "dbt build failed"))])
        run = classify_run_results(json.loads(run_results.read_text())["results"])
        if db.exists():
            run.totals, run.periods = self._totals(db, periods)
        return run

    def _totals(self, db: Path, periods: dict | None) -> tuple[dict[str, float], dict]:
        """Impact columns of a dbt warehouse at the latest period (or the baseline's period)."""
        totals: dict[str, float] = {}
        found: dict[str, object] = {}
        try:
            con = duckdb.connect(str(db), read_only=True)
        except duckdb.Error:
            return totals, found  # warehouse still locked by dbt: no impact figures
        try:
            for metric, cfg in self.project.impact.items():
                table = f'main."{cfg["model"]}"'
                period_col = cfg.get("period_column")
                try:
                    if period_col:
                        period = (periods or {}).get(metric)
                        if period is None:
                            period = con.execute(
                                f'SELECT MAX("{period_col}") FROM {table}'
                            ).fetchone()[0]
                        value = con.execute(
                            f'SELECT SUM("{cfg["column"]}") FROM {table} WHERE "{period_col}" = ?',
                            [period],
                        ).fetchone()[0]
                        found[metric] = period
                    else:
                        value = con.execute(
                            f'SELECT SUM("{cfg["column"]}") FROM {table}'
                        ).fetchone()[0]
                except duckdb.Error:
                    continue
                totals[metric] = float(value or 0)
        finally:
            con.close()
        return totals, found

    def baseline(self) -> DbtRun:
        """``dbt build`` of the unmutated project (cached)."""
        if self._baseline is None:
            root = self.work_dir / "baseline_project"
            copy_project(self.project.root, root)
            self._baseline = self._build(root, "baseline")
        return self._baseline

    def run_mutant(self, mutant: Mutant) -> DbtRun:
        """``dbt build`` of a copy of the project with the mutant applied to its source file."""
        self._count += 1
        label = f"mutant_{self._count}"
        root = self.work_dir / f"{label}_project"
        copy_project(self.project.root, root)
        mutate_source(self.project, mutant, root)
        base_periods = self.baseline().periods
        self.runs[mutant.id] = MutantRun(
            root, self.work_dir / label, self.work_dir / label / "warehouse.duckdb"
        )
        return self._build(root, label, base_periods)

    def check(self, result: MutantResult) -> CrossCheck:
        """Compare an engine result with dbt's verdict on the same mutant."""
        run = self.run_mutant(result.mutant)
        dbt_impact = None
        impact_agrees = None
        if run.verdict == SURVIVED and run.totals:
            base = self.baseline().totals
            dbt_impact = {m: round(run.totals[m] - base[m], 2) for m in run.totals if m in base}
            impact_agrees = all(
                abs(dbt_impact.get(m, 0.0) - result.impact.get(m, 0.0)) < 0.01 for m in dbt_impact
            )
        return CrossCheck(
            mutant_id=result.mutant.id,
            engine_verdict=result.verdict,
            dbt_verdict=run.verdict,
            agree=dbt_class(result.verdict) == run.verdict,
            engine_killed_by=result.killed_by,
            dbt_failed=run.failed + [e for e in run.errors if e not in run.failed],
            engine_impact=result.impact,
            dbt_impact=dbt_impact,
            impact_agrees=impact_agrees,
        )
