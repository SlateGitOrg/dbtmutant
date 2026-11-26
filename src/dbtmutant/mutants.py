"""Mutant generation: apply operators to a model's parsed SQL and describe the result."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from sqlglot import exp

from .operators import Operator, get_operators
from .sqlutil import parse, render


@dataclass(frozen=True)
class Mutant:
    """One mutation of one model."""

    model: str
    operator: str
    n: int
    original_sql: str
    mutated_sql: str
    note: str = ""  # operator-provided description; empty means derive it from the diff

    @property
    def id(self) -> str:
        return f"{self.model}:{self.operator}:{self.n}"

    @property
    def diff(self) -> str:
        """Unified diff of the pretty-printed SQL, one line of context."""
        lines = difflib.unified_diff(
            self.original_sql.splitlines(),
            self.mutated_sql.splitlines(),
            fromfile=f"{self.model} (original)",
            tofile=f"{self.id}",
            lineterm="",
            n=1,
        )
        return "\n".join(lines)

    @property
    def summary(self) -> str:
        """One line naming what changed: the operator's note, else the differing tokens."""
        if self.note:
            return self.note
        lines = self.diff.splitlines()
        removed = " ".join(
            ln[1:].strip() for ln in lines if ln.startswith("-") and not ln.startswith("---")
        )
        added = " ".join(
            ln[1:].strip() for ln in lines if ln.startswith("+") and not ln.startswith("+++")
        )
        old, new = removed.split(), added.split()
        if not new:
            return f"dropped: {removed}"
        if not old:
            return f"added: {added}"
        head = 0
        while head < min(len(old), len(new)) and old[head] == new[head]:
            head += 1
        tail = 0
        while (
            tail < min(len(old), len(new)) - head
            and old[len(old) - 1 - tail] == new[len(new) - 1 - tail]
        ):
            tail += 1
        lo, hi = max(0, head - 1), min(len(old), len(old) - tail + 1)
        lo2, hi2 = max(0, head - 1), min(len(new), len(new) - tail + 1)
        return f"{' '.join(old[lo:hi])}  ->  {' '.join(new[lo2:hi2])}"


def parse_model(sql: str) -> exp.Expression:
    return parse(sql)


def mutate_tree(
    tree: exp.Expression, operator: Operator, site: int, variant: int
) -> exp.Expression:
    """Return a mutated copy of ``tree`` (the original is untouched)."""
    copy = tree.copy()
    operator.apply(operator.sites(copy)[site], variant)
    return copy


def generate_mutants(
    model: str, sql: str | exp.Expression, operators: list[Operator] | None = None
) -> list[Mutant]:
    """All mutants of ``model``, ordered by operator then by position in the tree.

    Ids are deterministic: the same SQL and operators always give the same ids in the same order.
    """
    tree = parse(sql) if isinstance(sql, str) else sql
    original = render(tree)
    mutants: list[Mutant] = []
    for operator in operators if operators is not None else get_operators():
        n = 0
        for site_index, node in enumerate(operator.sites(tree)):
            for variant in range(operator.variants(node)):
                mutated = render(mutate_tree(tree, operator, site_index, variant))
                mutants.append(
                    Mutant(
                        model, operator.name, n, original, mutated, operator.describe(node, variant)
                    )
                )
                n += 1
    return mutants


def project_mutants(
    project, models: list[str] | None = None, operators: list[Operator] | None = None
) -> list[Mutant]:
    """Mutants of every model (or the named ones) in dependency order."""
    unknown = [m for m in models or [] if m not in project.models]
    if unknown:
        raise KeyError(f"unknown model(s): {', '.join(unknown)}")
    out: list[Mutant] = []
    for name in project.topo_order():
        if models and name not in models:
            continue
        out.extend(generate_mutants(name, project.models[name].sql, operators))
    return out


def find_mutant(project, mutant_id: str, operators: list[Operator] | None = None) -> Mutant:
    """Look a mutant up by ``<model>:<operator>:<n>``."""
    try:
        model, operator, n = mutant_id.rsplit(":", 2)
        index = int(n)
    except ValueError:
        raise KeyError(
            f"malformed mutant id {mutant_id!r}; expected <model>:<operator>:<n>"
        ) from None
    if model not in project.models:
        raise KeyError(f"unknown model {model!r} in mutant id {mutant_id!r}")
    for mutant in generate_mutants(model, project.models[model].sql, operators):
        if mutant.operator == operator and mutant.n == index:
            return mutant
    raise KeyError(f"no mutant {mutant_id!r}")
