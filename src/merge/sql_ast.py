from __future__ import annotations

from sqlglot import expressions as exp


def with_ctes(expression: exp.Expression) -> list[exp.CTE]:
    """Return CTE definitions attached to an expression, if any."""

    with_expression = expression.args.get("with")
    if with_expression is None:
        return []

    return list(with_expression.expressions or [])


def cte_aliases(expression: exp.Expression) -> set[str]:
    """Return names introduced by CTEs attached to an expression."""

    return {
        cte.alias
        for cte in with_ctes(expression)
        if cte.alias
    }


def child_expressions(expression: exp.Expression) -> tuple[exp.Expression, ...]:
    """Return direct expression children."""

    return tuple(child for _, child in expression.iter_expressions())


def has_ancestor_before_root(
    node: exp.Expression,
    ancestor_type: type[exp.Expression],
    root: exp.Expression,
) -> bool:
    """Return whether node has an ancestor of type before reaching root."""

    parent = node.parent
    while parent is not None and parent is not root:
        if isinstance(parent, ancestor_type):
            return True
        parent = parent.parent

    return False
