"""Typer command line: list-mutants, run, show, crosscheck."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from .crosscheck import CrossChecker
from .engine import SURVIVED, Engine
from .loader import Project, ProjectError
from .mutants import find_mutant, project_mutants
from .operators import get_operators
from .report import build_report, ranked_survivors, score_table, write_report

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Mutation testing for dbt projects: plant plausible bugs and report the ones tests miss.",
)
console = Console()
err = Console(stderr=True)

ProjectOpt = typer.Option(Path("examples/saas_billing"), "--project", "-p", help="dbt project dir.")
ModelOpt = typer.Option(None, "--model", "-m", help="Only this model (repeatable).")
OperatorOpt = typer.Option(None, "--operator", "-o", help="Only this operator (repeatable).")


def _load(project_dir: Path) -> Project:
    try:
        return Project.load(project_dir)
    except ProjectError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc


def _operators(names: list[str] | None):
    try:
        return get_operators(names or None)
    except KeyError as exc:
        err.print(f"[red]error:[/red] {exc.args[0]}")
        raise typer.Exit(2) from exc


def _baseline_engine(project: Project) -> Engine:
    engine = Engine(project)
    baseline = engine.build_baseline()
    if not baseline.clean:
        failed = baseline.failed_tests + baseline.failed_unit_tests
        err.print(
            f"[red]error:[/red] the unmutated project fails its own tests: {', '.join(failed)}"
        )
        raise typer.Exit(2)
    return engine


@app.command("list-mutants")
def list_mutants(
    project: Path = ProjectOpt,
    model: list[str] = ModelOpt,
    operator: list[str] = OperatorOpt,
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """List every mutant that would be generated, without running anything."""
    proj = _load(project)
    try:
        mutants = project_mutants(proj, model or None, _operators(operator))
    except KeyError as exc:
        err.print(f"[red]error:[/red] {exc.args[0]}")
        raise typer.Exit(2) from exc
    if as_json:
        typer.echo(
            json.dumps(
                [{"id": m.id, "summary": m.summary, "diff": m.diff} for m in mutants], indent=2
            )
        )
        return
    table = Table("mutant", "change", title=f"{len(mutants)} mutants")
    for m in mutants:
        table.add_row(m.id, m.summary)
    console.print(table)


@app.command()
def run(
    project: Path = ProjectOpt,
    model: list[str] = ModelOpt,
    operator: list[str] = OperatorOpt,
    out: Path = typer.Option(Path("dbtmutant-report"), "--out", help="Report directory."),
    fmt: str = typer.Option("both", "--format", help="md, json or both."),
    top: int = typer.Option(
        10, "--top", help="Survivors to list in the Markdown report (0 = all)."
    ),
    fail_fast: bool = typer.Option(
        False, "--fail-fast", help="Stop at a mutant's first failing test."
    ),
) -> None:
    """Run every mutant against the project's tests and write the report."""
    if fmt not in ("md", "json", "both"):
        err.print("[red]error:[/red] --format must be md, json or both")
        raise typer.Exit(2)
    proj = _load(project)
    try:
        mutants = project_mutants(proj, model or None, _operators(operator))
    except KeyError as exc:
        err.print(f"[red]error:[/red] {exc.args[0]}")
        raise typer.Exit(2) from exc
    engine = _baseline_engine(proj)
    with err.status("running mutants") as status:

        def tick(result) -> None:
            done.append(result)
            status.update(f"running mutants {len(done)}/{len(mutants)} ({result.mutant.id})")

        done: list = []
        results = engine.run_all(mutants, fail_fast=fail_fast, progress=tick)
    report = build_report(proj, results)
    paths = write_report(report, out, fmt, top or None)
    console.print(score_table(report))
    top_survivors = ranked_survivors(results)[:5]
    if top_survivors:
        table = Table(
            "mutant", "operator", "change", "MRR impact", title="Top survivors by MRR impact"
        )
        for r in top_survivors:
            table.add_row(
                r.mutant.id, r.mutant.operator, r.mutant.summary, f"{r.impact.get('mrr', 0):+,.2f}"
            )
        table.columns[2].overflow = "fold"
        console.print()
        console.print(table)
    console.print("\nwrote " + ", ".join(str(p) for p in paths))


@app.command()
def show(
    mutant_id: str = typer.Argument(..., help="<model>:<operator>:<n>"),
    project: Path = ProjectOpt,
    report: Path = typer.Option(None, "--report", help="Read the verdict from a report.json."),
) -> None:
    """Show one mutant's diff and verdict (runs it unless --report is given)."""
    if report is not None:
        data = json.loads(report.read_text())
        found = next((m for m in data["mutants"] if m["id"] == mutant_id), None)
        if found is None:
            err.print(f"[red]error:[/red] {mutant_id} is not in {report}")
            raise typer.Exit(2)
        diff, verdict = found["diff"], found["verdict"]
        killed_by, error = found["killed_by"], found["error"]
        changed, impact = found["changed_models"], found["impact"]
    else:
        proj = _load(project)
        try:
            mutant = find_mutant(proj, mutant_id)
        except KeyError as exc:
            err.print(f"[red]error:[/red] {exc.args[0]}")
            raise typer.Exit(2) from exc
        result = _baseline_engine(proj).run_mutant(mutant)
        diff, verdict = mutant.diff, result.verdict
        killed_by, error = result.killed_by, result.error
        changed, impact = result.changed_models, result.impact
    console.print(Syntax(diff, "diff", theme="ansi_dark"))
    console.print(f"verdict: [bold]{verdict}[/bold]")
    if killed_by:
        console.print("killed by: " + ", ".join(killed_by))
    if error:
        console.print(f"error: {error}")
    if changed:
        console.print("changed output: " + ", ".join(f"{k} ({v} rows)" for k, v in changed.items()))
    if verdict == SURVIVED:
        console.print(
            f"MRR impact {impact.get('mrr', 0):+,.2f}, ARR impact {impact.get('arr', 0):+,.2f}"
        )


@app.command()
def crosscheck(
    mutant_ids: list[str] = typer.Argument(..., help="One or more <model>:<operator>:<n>."),
    project: Path = ProjectOpt,
) -> None:
    """Compare the engine's verdict with a real dbt build of a mutated copy of the project."""
    proj = _load(project)
    engine = _baseline_engine(proj)
    checks = []
    with CrossChecker(proj) as checker:
        for mutant_id in mutant_ids:
            try:
                mutant = find_mutant(proj, mutant_id)
            except KeyError as exc:
                err.print(f"[red]error:[/red] {exc.args[0]}")
                raise typer.Exit(2) from exc
            checks.append(checker.check(engine.run_mutant(mutant)))
    table = Table("mutant", "engine", "dbt build", "agree", "dbt failures")
    for column in table.columns:
        column.overflow = "fold"
    for c in checks:
        table.add_row(
            c.mutant_id,
            c.engine_verdict,
            c.dbt_verdict,
            "yes" if c.agree else "NO",
            ", ".join(c.dbt_failed),
        )
    console.print(table)
    for c in checks:
        if c.dbt_impact is not None:
            console.print(
                f"{c.mutant_id}: MRR impact engine {c.engine_impact.get('mrr', 0):+,.2f}, "
                f"dbt {c.dbt_impact.get('mrr', 0):+,.2f}"
            )
    agreed = sum(c.agree for c in checks)
    console.print(f"Cross-check: {len(checks)} mutants checked against dbt build, {agreed} agree")
    if agreed != len(checks):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
