"""Small helpers around sqlglot: parsing, relation rewriting and dbt reference placeholders."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

DIALECT = "duckdb"

_REF = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_PLACEHOLDER = re.compile(r"dbtmutant_ref_(\d+)")


def parse(sql: str) -> exp.Expression:
    """Parse one statement in the DuckDB dialect."""
    return sqlglot.parse_one(sql, read=DIALECT)


def render(tree: exp.Expression, pretty: bool = True) -> str:
    return tree.sql(dialect=DIALECT, pretty=pretty)


def normalize_relations(tree: exp.Expression, known: set[str], schema: str = "main") -> None:
    """Rewrite qualified references to known relations as ``schema.name``, in place.

    The catalog that dbt put in compiled SQL (a database file name) is dropped so that diffs and
    mutated SQL do not depend on where the project was compiled.
    """
    for table in tree.find_all(exp.Table):
        if table.args.get("db") and table.name in known:
            table.set("catalog", None)
            table.set("db", exp.to_identifier(schema))
            table.set("this", exp.to_identifier(table.name))


def rewrite_relations(
    tree: exp.Expression, mapping: dict[str, str], default_schema: str = "main"
) -> exp.Expression:
    """Return a copy where each qualified reference to a mapped relation points at its schema.

    ``mapping`` maps relation name to schema; qualified references to relations not in the
    mapping are pointed at ``default_schema``.  Unqualified names (CTEs) are left alone.
    """
    out = tree.copy()
    for table in out.find_all(exp.Table):
        if table.args.get("db"):
            table.set("catalog", None)
            table.set("db", exp.to_identifier(mapping.get(table.name, default_schema)))
    return out


def swap_refs(source: str) -> tuple[str, dict[str, str]]:
    """Replace ``{{ ref() }}``/``{{ source() }}`` by placeholder identifiers.

    Returns the SQL that sqlglot can parse and the placeholder -> original Jinja mapping.
    """
    mapping: dict[str, str] = {}

    def _sub(match: re.Match) -> str:
        key = f"dbtmutant_ref_{len(mapping)}"
        mapping[key] = match.group(0)
        return key

    text = _REF.sub(_sub, source)
    text = _SOURCE.sub(_sub, text)
    return text, mapping


def restore_refs(sql: str, mapping: dict[str, str]) -> str:
    """Inverse of :func:`swap_refs`."""
    return _PLACEHOLDER.sub(lambda m: mapping[m.group(0)], sql)


def ref_names(raw_input: str) -> tuple[str, str] | None:
    """Parse a unit-test input such as ``ref('x')`` or ``source('a', 'b')``.

    Returns ``(kind, name)`` where name is the model or ``source.table`` identifier.
    """
    match = re.fullmatch(r"\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*", raw_input)
    if match:
        return "ref", match.group(1)
    match = re.fullmatch(
        r"\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*", raw_input
    )
    if match:
        return "source", f"{match.group(1)}.{match.group(2)}"
    return None
