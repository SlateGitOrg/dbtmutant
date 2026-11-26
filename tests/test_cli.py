from __future__ import annotations

import json

from conftest import EXAMPLE
from typer.testing import CliRunner

from dbtmutant.cli import app

runner = CliRunner()
PROJECT = ["--project", str(EXAMPLE)]


def test_list_mutants_json_matches_generation(mutants):
    result = runner.invoke(app, ["list-mutants", *PROJECT, "--json"])
    assert result.exit_code == 0, result.output
    listed = json.loads(result.output)
    assert [m["id"] for m in listed] == [m.id for m in mutants]


def test_list_mutants_filters():
    result = runner.invoke(
        app, ["list-mutants", *PROJECT, "--json", "-m", "mart_arr_by_segment", "-o", "AGG"]
    )
    ids = [m["id"] for m in json.loads(result.output)]
    assert ids == [
        "mart_arr_by_segment:AGG:0",
        "mart_arr_by_segment:AGG:1",
        "mart_arr_by_segment:AGG:2",
    ]


def test_list_mutants_rejects_unknown_model_and_operator():
    assert runner.invoke(app, ["list-mutants", *PROJECT, "-m", "nope"]).exit_code == 2
    assert runner.invoke(app, ["list-mutants", *PROJECT, "-o", "NOPE"]).exit_code == 2


def test_run_writes_reports_and_prints_scores(tmp_path):
    out = tmp_path / "report"
    result = runner.invoke(app, ["run", *PROJECT, "-m", "mart_arr_by_segment", "--out", str(out)])
    assert result.exit_code == 0, result.output
    report = json.loads((out / "report.json").read_text())
    assert report["totals"]["mutants"] == 9
    assert report["totals"]["survived"] == 7 and report["totals"]["equivalent_on_data"] == 2
    assert "mart_arr_by_segment" in result.output and "Top survivors" in result.output
    assert (out / "report.md").read_text().startswith("# Mutation report")


def test_show_reads_a_report_and_runs_a_single_mutant(tmp_path, ids):
    out = tmp_path / "report"
    runner.invoke(
        app, ["run", *PROJECT, "-m", "mart_arr_by_segment", "-o", "AGG", "--out", str(out)]
    )
    from_report = runner.invoke(
        app, ["show", ids["count_distinct"], "--report", str(out / "report.json")]
    )
    assert from_report.exit_code == 0 and "equivalent_on_data" in from_report.output
    live = runner.invoke(app, ["show", ids["mart_boundary"], *PROJECT])
    assert live.exit_code == 0, live.output
    assert "survived" in live.output and "MRR impact" in live.output and "<=" in live.output
    assert runner.invoke(app, ["show", "mart_arr_by_segment:ROR:99", *PROJECT]).exit_code == 2
