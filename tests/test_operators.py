from __future__ import annotations

import pytest
from sqlglot import exp

from dbtmutant.mutants import find_mutant, generate_mutants, mutate_tree, parse_model
from dbtmutant.operators import (
    Operator,
    get_operators,
    register_operator,
    unregister_operator,
)
from dbtmutant.sqlutil import parse, render, restore_refs, swap_refs


def mutated(sql: str, operator: str) -> list[str]:
    """Every mutant of ``sql`` for one operator, as compact SQL."""
    return [
        render(parse(m.mutated_sql), pretty=False)
        for m in generate_mutants("m", sql, get_operators([operator]))
    ]


def test_ror_swaps_each_comparison_pair():
    sql = "SELECT 1 FROM s JOIN d ON s.end_date < d.day"
    assert mutated(sql, "ROR") == ["SELECT 1 FROM s JOIN d ON s.end_date <= d.day"]
    assert mutated("SELECT 1 FROM t WHERE a <= 1", "ROR") == ["SELECT 1 FROM t WHERE a < 1"]
    assert mutated("SELECT 1 FROM t WHERE a > 1", "ROR") == ["SELECT 1 FROM t WHERE a >= 1"]
    assert mutated("SELECT 1 FROM t WHERE a >= 1", "ROR") == ["SELECT 1 FROM t WHERE a > 1"]
    assert mutated("SELECT 1 FROM t WHERE a = 1", "ROR") == ["SELECT 1 FROM t WHERE a <> 1"]
    assert mutated("SELECT 1 FROM t WHERE a <> 1", "ROR") == ["SELECT 1 FROM t WHERE a = 1"]


def test_join_swaps_left_and_inner():
    assert mutated("SELECT 1 FROM a LEFT JOIN b ON a.k = b.k", "JOIN") == [
        "SELECT 1 FROM a JOIN b ON a.k = b.k"
    ]
    assert mutated("SELECT 1 FROM a INNER JOIN b ON a.k = b.k", "JOIN") == [
        "SELECT 1 FROM a LEFT JOIN b ON a.k = b.k"
    ]
    assert mutated("SELECT 1 FROM a CROSS JOIN b", "JOIN") == []


def test_lcr_turns_and_into_or_and_keeps_precedence():
    assert mutated("SELECT 1 FROM t WHERE a = 1 AND b = 2", "LCR") == [
        "SELECT 1 FROM t WHERE a = 1 OR b = 2"
    ]
    nested = mutated("SELECT 1 FROM t WHERE a = 1 OR (b = 2 AND c = 3)", "LCR")
    assert nested == ["SELECT 1 FROM t WHERE a = 1 OR (b = 2 OR c = 3)"]
    chain = mutated("SELECT 1 FROM t WHERE a = 1 AND b = 2 AND c = 3", "LCR")
    assert chain == [
        "SELECT 1 FROM t WHERE a = 1 AND b = 2 OR c = 3",
        "SELECT 1 FROM t WHERE (a = 1 OR b = 2) AND c = 3",
    ]


def test_drop_pred_removes_one_conjunct_at_a_time():
    assert mutated("SELECT 1 FROM t WHERE a = 1 AND b = 2 AND c = 3", "DROP_PRED") == [
        "SELECT 1 FROM t WHERE b = 2 AND c = 3",
        "SELECT 1 FROM t WHERE a = 1 AND c = 3",
        "SELECT 1 FROM t WHERE a = 1 AND b = 2",
    ]
    assert mutated("SELECT 1 FROM a JOIN b ON a.k = b.k AND a.d < b.d", "DROP_PRED") == [
        "SELECT 1 FROM a JOIN b ON a.d < b.d",
        "SELECT 1 FROM a JOIN b ON a.k = b.k",
    ]
    # A lone WHERE condition can be dropped; a lone ON condition cannot (it would be a cross join).
    assert mutated("SELECT 1 FROM t WHERE a = 1", "DROP_PRED") == ["SELECT 1 FROM t"]
    assert mutated("SELECT 1 FROM a JOIN b ON a.k = b.k", "DROP_PRED") == []


def test_agg_replaces_count_distinct_min_and_sum():
    sql = "SELECT COUNT(DISTINCT x), MIN(y), SUM(z), MAX(w) FROM t"
    assert mutated(sql, "AGG") == [
        "SELECT COUNT(x), MIN(y), SUM(z), MAX(w) FROM t",
        "SELECT COUNT(DISTINCT x), MAX(y), SUM(z), MAX(w) FROM t",
        "SELECT COUNT(DISTINCT x), MIN(y), AVG(z), MAX(w) FROM t",
    ]


def test_coalesce_with_constant_fallback_is_dropped():
    assert mutated("SELECT COALESCE(x, 0) FROM t", "COALESCE") == ["SELECT x FROM t"]
    assert mutated("SELECT COALESCE(x, 'n/a') FROM t", "COALESCE") == ["SELECT x FROM t"]
    assert mutated("SELECT COALESCE(x, y) FROM t", "COALESCE") == []
    assert mutated("SELECT COALESCE(x, y, 0) FROM t", "COALESCE") == []


def test_interval_shifts_integer_by_plus_and_minus_one():
    assert mutated("SELECT d + INTERVAL 3 DAY FROM t", "INTERVAL") == [
        "SELECT d + INTERVAL '4' DAY FROM t",
        "SELECT d + INTERVAL '2' DAY FROM t",
    ]
    # No negative intervals are generated.
    assert mutated("SELECT d + INTERVAL 0 DAY FROM t", "INTERVAL") == [
        "SELECT d + INTERVAL '1' DAY FROM t"
    ]


def test_aor_swaps_arithmetic_operators():
    assert mutated("SELECT a + b FROM t", "AOR") == ["SELECT a - b FROM t"]
    assert mutated("SELECT a - b FROM t", "AOR") == ["SELECT a + b FROM t"]
    assert mutated("SELECT a * b FROM t", "AOR") == ["SELECT a / b FROM t"]
    assert mutated("SELECT a / 12 FROM t", "AOR") == ["SELECT a * 12 FROM t"]
    assert mutated("SELECT a * (b - c) FROM t", "AOR") == [
        "SELECT a / (b - c) FROM t",
        "SELECT a * (b + c) FROM t",
    ]


def test_case_swap_exchanges_the_first_two_results():
    sql = "SELECT CASE WHEN a THEN 'x' WHEN b THEN 'y' ELSE 'z' END FROM t"
    assert mutated(sql, "CASE_SWAP") == [
        "SELECT CASE WHEN a THEN 'y' WHEN b THEN 'x' ELSE 'z' END FROM t"
    ]
    assert mutated("SELECT CASE WHEN a THEN 'x' ELSE 'z' END FROM t", "CASE_SWAP") == []
    assert mutated("SELECT CASE WHEN a THEN 'x' WHEN b THEN 'x' END FROM t", "CASE_SWAP") == []


def test_distinct_is_dropped_from_select_only():
    assert mutated("SELECT DISTINCT a FROM t", "DISTINCT") == ["SELECT a FROM t"]
    assert mutated("SELECT COUNT(DISTINCT a) FROM t", "DISTINCT") == []


HAND_COUNT_SQL = """
SELECT a.id, COALESCE(b.x, 0) AS x, COUNT(DISTINCT a.k) AS c, a.v + 1 AS v1
FROM a LEFT JOIN b ON a.id = b.id AND a.d < b.d
WHERE a.v > 3 AND a.w <> 0
GROUP BY 1, 2, 4
"""


def test_mutant_counts_equal_a_hand_count():
    mutants = generate_mutants("fixture", HAND_COUNT_SQL)
    counts = {}
    for m in mutants:
        counts[m.operator] = counts.get(m.operator, 0) + 1
    assert counts == {
        "ROR": 4,  # =, <, >, <>
        "JOIN": 1,  # the LEFT JOIN
        "LCR": 2,  # AND in ON, AND in WHERE
        "DROP_PRED": 4,  # two conjuncts in ON, two in WHERE
        "AGG": 1,  # COUNT(DISTINCT)
        "COALESCE": 1,
        "AOR": 1,  # a.v + 1
    }
    assert len(mutants) == 14


def test_ids_are_deterministic_and_ordered():
    first = [m.id for m in generate_mutants("fixture", HAND_COUNT_SQL)]
    second = [m.id for m in generate_mutants("fixture", HAND_COUNT_SQL)]
    assert first == second
    assert first[:4] == ["fixture:ROR:0", "fixture:ROR:1", "fixture:ROR:2", "fixture:ROR:3"]
    assert len(set(first)) == len(first)


def test_every_project_mutant_reparses_and_differs(mutants):
    assert len(mutants) > 50
    for m in mutants:
        reparsed = parse(m.mutated_sql)
        assert isinstance(reparsed, exp.Select)
        assert m.mutated_sql != m.original_sql, m.id
        assert m.diff.startswith("---"), m.id


def test_mutation_never_changes_the_original_tree():
    tree = parse_model(HAND_COUNT_SQL)
    before = render(tree)
    op = get_operators(["ROR"])[0]
    mutate_tree(tree, op, 0, 0)
    assert render(tree) == before


def test_planted_boundary_mutant_has_the_expected_diff(project, ids):
    mutant = find_mutant(project, ids["boundary"])
    assert mutant.summary.endswith("c.day <= p.end_date")
    assert "-  ON c.day >= p.start_date AND c.day < p.end_date" in mutant.diff
    assert "+  ON c.day >= p.start_date AND c.day <= p.end_date" in mutant.diff


def test_ref_placeholders_round_trip():
    source = "select * from {{ ref('a') }} x join {{ source('s', 't') }} y on x.k = y.k"
    text, mapping = swap_refs(source)
    assert "{{" not in text
    assert len(mapping) == 2
    assert restore_refs(text, mapping) == source


class RenameColumn(Operator):
    name = "RENAME_COL"

    def sites(self, tree):
        return [c for c in tree.dfs() if isinstance(c, exp.Column) and c.name == "account_id"][:2]

    def apply(self, node, variant):
        node.set("this", exp.to_identifier("no_such_column"))


def test_operators_are_pluggable():
    register_operator(RenameColumn())
    try:
        assert "RENAME_COL" in [o.name for o in get_operators()]
        found = generate_mutants("m", "SELECT account_id FROM t", get_operators(["RENAME_COL"]))
        assert [m.id for m in found] == ["m:RENAME_COL:0"]
        assert "no_such_column" in found[0].mutated_sql
    finally:
        unregister_operator("RENAME_COL")
    assert "RENAME_COL" not in [o.name for o in get_operators()]
    with pytest.raises(KeyError):
        get_operators(["RENAME_COL"])


def test_find_mutant_rejects_bad_ids(project):
    with pytest.raises(KeyError):
        find_mutant(project, "nonsense")
    with pytest.raises(KeyError):
        find_mutant(project, "no_such_model:ROR:0")
    with pytest.raises(KeyError):
        find_mutant(project, "mart_arr_by_segment:ROR:99")


def test_drop_pred_summary_says_what_was_dropped(project):
    where = generate_mutants(
        "m", "SELECT 1 FROM t WHERE a = 1 AND b > 2", get_operators(["DROP_PRED"])
    )
    assert [m.summary for m in where] == ["dropped from WHERE: a = 1", "dropped from WHERE: b > 2"]
    on = generate_mutants(
        "m", "SELECT 1 FROM a JOIN b ON a.k = b.k AND a.d < b.d", get_operators(["DROP_PRED"])
    )
    assert [m.summary for m in on] == ["dropped from ON: a.k = b.k", "dropped from ON: a.d < b.d"]
