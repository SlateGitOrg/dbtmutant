"""Mutation operators on the sqlglot AST (DuckDB dialect).

An operator finds *sites* in a parsed tree (pre-order) and knows how to rewrite one site, in
place, into each of its variants.  The mutant id of a mutation is ``<model>:<operator>:<n>``
where ``n`` counts the operator's mutants (sites in pre-order, variants in order) from zero.

Operators are pluggable: subclass :class:`Operator` and call :func:`register_operator`.
"""

from __future__ import annotations

from sqlglot import exp


class Operator:
    """Base class for a mutation operator."""

    name: str = ""
    description: str = ""

    def sites(self, tree: exp.Expression) -> list[exp.Expression]:
        """Return the nodes this operator can mutate, in pre-order."""
        raise NotImplementedError

    def variants(self, node: exp.Expression) -> int:
        """Number of distinct mutations available at ``node``."""
        return 1

    def apply(self, node: exp.Expression, variant: int) -> None:
        """Mutate ``node`` (a node of a private copy of the tree) in place."""
        raise NotImplementedError

    def describe(self, node: exp.Expression, variant: int) -> str:
        """Optional one-line description of the mutation at ``node`` (before it is applied).

        When empty, the mutant summary is derived from the text diff.
        """
        return ""


def _wrap(node: exp.Expression, parent: exp.Expression | None) -> exp.Expression:
    """Parenthesise a boolean node that is being placed under another operator."""
    if isinstance(parent, (exp.Connector, exp.Not, exp.Binary)):
        return exp.Paren(this=node)
    return node


class RelationalOperatorReplacement(Operator):
    name = "ROR"
    description = "swap a comparison: < <=, > >=, = !="

    _PAIRS = {
        exp.LT: exp.LTE,
        exp.LTE: exp.LT,
        exp.GT: exp.GTE,
        exp.GTE: exp.GT,
        exp.EQ: exp.NEQ,
        exp.NEQ: exp.EQ,
    }

    def sites(self, tree):
        return [n for n in tree.dfs() if type(n) in self._PAIRS]

    def apply(self, node, variant):
        new = self._PAIRS[type(node)](this=node.this.copy(), expression=node.expression.copy())
        node.replace(new)


class JoinTypeReplacement(Operator):
    name = "JOIN"
    description = "LEFT JOIN becomes INNER JOIN and INNER JOIN becomes LEFT JOIN"

    @staticmethod
    def _is_left(node: exp.Join) -> bool:
        return node.side == "LEFT" and node.kind in ("", "OUTER")

    @staticmethod
    def _is_inner(node: exp.Join) -> bool:
        return not node.side and node.kind in ("", "INNER") and node.args.get("on") is not None

    def sites(self, tree):
        return [
            n
            for n in tree.dfs()
            if isinstance(n, exp.Join) and (self._is_left(n) or self._is_inner(n))
        ]

    def apply(self, node, variant):
        if self._is_left(node):
            node.set("side", None)
            node.set("kind", None)
        else:
            node.set("side", "LEFT")
            node.set("kind", None)


class LogicalConnectorReplacement(Operator):
    name = "LCR"
    description = "AND becomes OR"

    def sites(self, tree):
        return [n for n in tree.dfs() if isinstance(n, exp.And)]

    def apply(self, node, variant):
        new = exp.Or(this=node.this.copy(), expression=node.expression.copy())
        node.replace(_wrap(new, node.parent))


class DropPredicate(Operator):
    name = "DROP_PRED"
    description = "remove one conjunct from a WHERE or ON condition"

    @staticmethod
    def _conjuncts(cond: exp.Expression) -> list[exp.Expression]:
        return list(cond.flatten()) if isinstance(cond, exp.And) else [cond]

    def sites(self, tree):
        out = []
        for node in tree.dfs():
            if isinstance(node, exp.Where):
                out.extend(self._conjuncts(node.this))
            elif isinstance(node, exp.Join) and node.args.get("on") is not None:
                parts = self._conjuncts(node.args["on"])
                if len(parts) > 1:
                    out.extend(parts)
        return out

    def describe(self, node, variant):
        container = node.find_ancestor(exp.Where, exp.Join)
        clause = "WHERE" if isinstance(container, exp.Where) else "ON"
        return f"dropped from {clause}: {node.sql(dialect='duckdb')}"

    def apply(self, node, variant):
        container = node.find_ancestor(exp.Where, exp.Join)
        cond = container.this if isinstance(container, exp.Where) else container.args["on"]
        parts = [p for p in self._conjuncts(cond) if p is not node]
        if not parts:
            container.pop()
            return
        rest = exp.and_(*[p.copy() for p in parts], copy=False)
        if isinstance(container, exp.Where):
            container.set("this", rest)
        else:
            container.set("on", rest)


class AggregateReplacement(Operator):
    name = "AGG"
    description = "COUNT(DISTINCT x) becomes COUNT(x), MIN becomes MAX, SUM becomes AVG"

    @staticmethod
    def _matches(node: exp.Expression) -> bool:
        if isinstance(node, exp.Count):
            inner = node.this
            return isinstance(inner, exp.Distinct) and len(inner.expressions) == 1
        return isinstance(node, (exp.Min, exp.Sum))

    def sites(self, tree):
        return [n for n in tree.dfs() if self._matches(n)]

    def apply(self, node, variant):
        if isinstance(node, exp.Count):
            node.replace(exp.Count(this=node.this.expressions[0].copy()))
        elif isinstance(node, exp.Min):
            node.replace(exp.Max(this=node.this.copy()))
        else:
            node.replace(exp.Avg(this=node.this.copy()))


class CoalesceDrop(Operator):
    name = "COALESCE"
    description = "COALESCE(x, <constant>) becomes x"

    @staticmethod
    def _matches(node: exp.Expression) -> bool:
        if not isinstance(node, exp.Coalesce) or len(node.expressions) != 1:
            return False
        fallback = node.expressions[0]
        return not any(True for _ in fallback.find_all(exp.Column, exp.Select, exp.Subquery))

    def sites(self, tree):
        return [n for n in tree.dfs() if self._matches(n)]

    def apply(self, node, variant):
        node.replace(node.this.copy())


class IntervalShift(Operator):
    name = "INTERVAL"
    description = "an integer interval or day offset changes by +1 or -1"

    @staticmethod
    def _literal(node: exp.Expression) -> exp.Literal | None:
        target = None
        if isinstance(node, exp.Interval):
            target = node.this
        elif isinstance(node, (exp.DateAdd, exp.DateSub)):
            target = node.expression
        if isinstance(target, exp.Literal) and target.name.isdigit():
            return target
        return None

    def sites(self, tree):
        return [n for n in tree.dfs() if self._literal(n) is not None]

    def _deltas(self, node):
        value = int(self._literal(node).name)
        return [d for d in (1, -1) if value + d >= 0]

    def variants(self, node):
        return len(self._deltas(node))

    def apply(self, node, variant):
        lit = self._literal(node)
        delta = self._deltas(node)[variant]
        lit.replace(exp.Literal(this=str(int(lit.name) + delta), is_string=lit.is_string))


class ArithmeticOperatorReplacement(Operator):
    name = "AOR"
    description = "swap an arithmetic operator: + -, * /"

    _PAIRS = {exp.Add: exp.Sub, exp.Sub: exp.Add, exp.Mul: exp.Div, exp.Div: exp.Mul}

    def sites(self, tree):
        return [n for n in tree.dfs() if type(n) in self._PAIRS]

    def apply(self, node, variant):
        new = self._PAIRS[type(node)](this=node.this.copy(), expression=node.expression.copy())
        node.replace(_wrap(new, node.parent) if isinstance(node.parent, exp.Binary) else new)


class CaseSwap(Operator):
    name = "CASE_SWAP"
    description = "swap the first two THEN results of a CASE"

    @staticmethod
    def _matches(node: exp.Expression) -> bool:
        if not isinstance(node, exp.Case):
            return False
        ifs = node.args.get("ifs") or []
        return len(ifs) >= 2 and ifs[0].args["true"] != ifs[1].args["true"]

    def sites(self, tree):
        return [n for n in tree.dfs() if self._matches(n)]

    def apply(self, node, variant):
        first, second = node.args["ifs"][:2]
        a, b = first.args["true"].copy(), second.args["true"].copy()
        first.set("true", b)
        second.set("true", a)


class DistinctDrop(Operator):
    name = "DISTINCT"
    description = "remove DISTINCT from a SELECT"

    def sites(self, tree):
        return [
            n
            for n in tree.dfs()
            if isinstance(n, exp.Select) and isinstance(n.args.get("distinct"), exp.Distinct)
        ]

    def apply(self, node, variant):
        node.set("distinct", None)


_REGISTRY: dict[str, Operator] = {}


def register_operator(operator: Operator) -> Operator:
    """Add (or replace) an operator in the registry."""
    if not operator.name:
        raise ValueError("an operator needs a name")
    _REGISTRY[operator.name] = operator
    return operator


def unregister_operator(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_operators(names: list[str] | None = None) -> list[Operator]:
    """Return registered operators (all of them, in registration order, or the named ones)."""
    if names is None:
        return list(_REGISTRY.values())
    missing = [n for n in names if n not in _REGISTRY]
    if missing:
        raise KeyError(f"unknown operator(s): {', '.join(missing)}")
    return [_REGISTRY[n] for n in names]


for _op in (
    RelationalOperatorReplacement(),
    JoinTypeReplacement(),
    LogicalConnectorReplacement(),
    DropPredicate(),
    AggregateReplacement(),
    CoalesceDrop(),
    IntervalShift(),
    ArithmeticOperatorReplacement(),
    CaseSwap(),
    DistinctDrop(),
):
    register_operator(_op)
