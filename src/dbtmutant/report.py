"""Aggregate mutant results into per-model scores and write Markdown and JSON reports."""

from __future__ import annotations

import json
from pathlib import Path

from .engine import EQUIVALENT, INVALID, KILLED, SURVIVED, MutantResult
from .loader import Project


def score(killed: int, survived: int) -> float | None:
    """Mutation score: killed / (killed + survived); equivalent and invalid mutants are excluded."""
    total = killed + survived
    return None if total == 0 else killed / total


def survivor_sort_key(result: MutantResult):
    """Rank by absolute MRR impact, then ARR impact, then rows changed, then id."""
    return (
        -abs(result.impact.get("mrr", 0.0)),
        -abs(result.impact.get("arr", 0.0)),
        -sum(result.changed_models.values()),
        result.mutant.id,
    )


def ranked_survivors(results: list[MutantResult]) -> list[MutantResult]:
    return sorted((r for r in results if r.verdict == SURVIVED), key=survivor_sort_key)


def _counts(results: list[MutantResult]) -> dict:
    counts = {
        v: sum(1 for r in results if r.verdict == v)
        for v in (KILLED, SURVIVED, EQUIVALENT, INVALID)
    }
    return {"mutants": len(results), **counts, "score": score(counts[KILLED], counts[SURVIVED])}


def build_report(project: Project, results: list[MutantResult]) -> dict:
    """Return the report as a JSON-serialisable dict."""
    order = project.topo_order()
    models = []
    for name in order:
        mine = [r for r in results if r.mutant.model == name]
        if mine:
            models.append({"model": name, **_counts(mine)})
    survivors = ranked_survivors(results)
    return {
        "project": project.name,
        "totals": _counts(results),
        "models": models,
        "survivors": [r.mutant.id for r in survivors],
        "mutants": [
            {
                "id": r.mutant.id,
                "model": r.mutant.model,
                "operator": r.mutant.operator,
                "n": r.mutant.n,
                "verdict": r.verdict,
                "killed_by": r.killed_by,
                "error": r.error,
                "changed_models": r.changed_models,
                "impact": r.impact,
                "summary": r.mutant.summary,
                "diff": r.mutant.diff,
            }
            for r in sorted(
                results, key=lambda r: (order.index(r.mutant.model), r.mutant.operator, r.mutant.n)
            )
        ],
    }


def _fmt_score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _money(value: float) -> str:
    return f"{value:+,.2f}"


def score_table(report: dict) -> str:
    rows = [
        "| Model | Mutants | Killed | Survived | Equivalent | Invalid | Score |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for m in report["models"]:
        rows.append(
            f"| {m['model']} | {m['mutants']} | {m['killed']} | {m['survived']} | "
            f"{m['equivalent_on_data']} | {m['invalid']} | {_fmt_score(m['score'])} |"
        )
    t = report["totals"]
    rows.append(
        f"| **all models** | {t['mutants']} | {t['killed']} | {t['survived']} | "
        f"{t['equivalent_on_data']} | {t['invalid']} | {_fmt_score(t['score'])} |"
    )
    return "\n".join(rows)


def to_markdown(report: dict, top: int | None = None) -> str:
    """Render the report: scores, survivors ranked by impact with diffs, equivalent, invalid."""
    by_id = {m["id"]: m for m in report["mutants"]}
    out = [f"# Mutation report: {report['project']}", ""]
    t = report["totals"]
    out.append(
        f"{t['mutants']} mutants: {t['killed']} killed, {t['survived']} survived, "
        f"{t['equivalent_on_data']} equivalent on the data, {t['invalid']} invalid. "
        f"Overall score {_fmt_score(t['score'])} (killed / (killed + survived))."
    )
    out += ["", "## Score by model", "", score_table(report), ""]
    survivors = report["survivors"] if top is None else report["survivors"][:top]
    out.append(f"## Survivors ranked by impact ({len(survivors)} of {len(report['survivors'])})")
    out.append("")
    out.append(
        "Impact: change in MRR and ARR at the latest baseline period (mutated minus baseline)"
    )
    for rank, mid in enumerate(survivors, start=1):
        m = by_id[mid]
        changed = ", ".join(f"{k} ({v} rows)" for k, v in m["changed_models"].items())
        out += [
            "",
            f"### {rank}. `{mid}`",
            "",
            f"MRR impact {_money(m['impact'].get('mrr', 0.0))}, ARR impact "
            f"{_money(m['impact'].get('arr', 0.0))}. Changed output: {changed}.",
            "",
            "```diff",
            m["diff"],
            "```",
        ]
    equivalent = [m for m in report["mutants"] if m["verdict"] == EQUIVALENT]
    out += ["", f"## Equivalent on the data ({len(equivalent)})", ""]
    out += [f"- `{m['id']}`: {m['summary']}" for m in equivalent] or ["None."]
    invalid = [m for m in report["mutants"] if m["verdict"] == INVALID]
    out += ["", f"## Invalid ({len(invalid)})", ""]
    out += [f"- `{m['id']}`: {m['error']}" for m in invalid] or ["None."]
    return "\n".join(out) + "\n"


def write_report(
    report: dict, out_dir: Path, fmt: str = "both", top: int | None = None
) -> list[Path]:
    """Write ``report.json`` and/or ``report.md`` into ``out_dir`` and return the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if fmt in ("json", "both"):
        path = out_dir / "report.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        written.append(path)
    if fmt in ("md", "both"):
        path = out_dir / "report.md"
        path.write_text(to_markdown(report, top))
        written.append(path)
    return written
