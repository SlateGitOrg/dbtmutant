from __future__ import annotations

import json

from dbtmutant.report import score_table, to_markdown, write_report


def test_report_structure(report, results):
    assert report["project"] == "saas_billing"
    totals = report["totals"]
    assert totals["mutants"] == len(results) == len(report["mutants"])
    assert totals["killed"] + totals["survived"] + totals["equivalent_on_data"] + totals[
        "invalid"
    ] == len(results)
    assert sum(m["mutants"] for m in report["models"]) == len(results)
    assert [m["model"] for m in report["models"]][-1] == "mart_arr_by_segment"
    assert {m["model"] for m in report["models"]}.isdisjoint({"stg_accounts", "stg_plans"})


def test_survivor_list_is_ranked_and_complete(report):
    by_id = {m["id"]: m for m in report["mutants"]}
    ids = report["survivors"]
    assert all(by_id[i]["verdict"] == "survived" for i in ids)
    assert len(ids) == report["totals"]["survived"]
    impacts = [abs(by_id[i]["impact"]["mrr"]) for i in ids]
    assert impacts == sorted(impacts, reverse=True)


def test_markdown_sections(report, ids):
    text = to_markdown(report, top=3)
    assert text.startswith("# Mutation report: saas_billing")
    assert "## Score by model" in text and "## Survivors ranked by impact (3 of 13)" in text
    assert "## Equivalent on the data (14)" in text and "## Invalid (2)" in text
    assert "```diff" in text and ids["count_distinct"] in text
    assert text.count("### ") == 3
    assert "| fct_mrr_movements | 25 | 22 | 0 | 3 | 0 | 1.00 |" in score_table(report)


def test_write_report_files_round_trip(report, tmp_path):
    paths = write_report(report, tmp_path / "out")
    assert sorted(p.name for p in paths) == ["report.json", "report.md"]
    assert json.loads((tmp_path / "out" / "report.json").read_text()) == json.loads(
        json.dumps(report)
    )
    only_md = write_report(report, tmp_path / "md", fmt="md", top=1)
    assert [p.name for p in only_md] == ["report.md"]
