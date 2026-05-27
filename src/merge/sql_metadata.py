"""
SQL metadata used by merge analysis.

Statement metadata records one SQL statement's reads and writes. Transaction
metadata aggregates those statement-level facts across a transaction.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from .utils import ALL_COLUMNS, TableColumns, table_expression, table_name

IgnoredRelations = dict[str, set[str]]

DEFAULT_VALUES_INSERT_PATTERN = re.compile(
    r"\bDEFAULT\s+VALUES\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceMap:
    """Tables visible in one SQL scope, plus aliases intentionally ignored."""

    alias_to_table: dict[str, str]
    ignored_aliases: set[str]
    ignored_columns: dict[str, set[str]]
    output_aliases: set[str]


@dataclass(frozen=True)
class ResolutionFrame:
    """A lexical SQL scope used to resolve column references."""

    sources: SourceMap
    parent: "ResolutionFrame | None" = None


@dataclass(frozen=True)
class StatementMetadata:
    """Parsed SQL plus the statement-level read/write tables and columns."""

    parsed_sql_text: exp.Expression
    table_updated: str | None
    columns_updated: set[str]
    tables_referenced_to_columns_referenced: dict[str, set[str]]


@dataclass(frozen=True)
class TransactionMetadata:
    """Statement metadata plus aggregate transaction-level read/write sets."""

    statements: tuple[StatementMetadata, ...]
    tables_updated_to_columns_updated: dict[str, set[str]]
    tables_referenced_to_columns_referenced: dict[str, set[str]]


def transaction_metadata(
    statements: Sequence[StatementMetadata],
) -> TransactionMetadata:
    """Aggregate statement metadata into transaction-level metadata."""

    statement_tuple = tuple(statements)
    updated: defaultdict[str, set[str]] = defaultdict(set)
    referenced: defaultdict[str, set[str]] = defaultdict(set)
    for statement in statement_tuple:
        if statement.table_updated is not None:
            updated[statement.table_updated].update(statement.columns_updated)
        for table, columns in statement.tables_referenced_to_columns_referenced.items():
            referenced[table].update(columns)

    return TransactionMetadata(
        statements=statement_tuple,
        tables_updated_to_columns_updated=dict(updated),
        tables_referenced_to_columns_referenced=dict(referenced),
    )


def parse_statement_metadata(
    sql_text: str,
    table_columns: TableColumns | None = None,
) -> StatementMetadata:
    """Parse one SQL statement and extract metadata used by conflict checks."""

    parsed_sql_text = _parse_one(sql_text)
    return StatementMetadata(
        parsed_sql_text=parsed_sql_text,
        table_updated=_target_table_name(parsed_sql_text),
        columns_updated=_updated_columns(parsed_sql_text),
        tables_referenced_to_columns_referenced=_referenced_tables_to_columns(
            parsed_sql_text,
            table_columns,
        ),
    )


def unsupported_statement_metadata(sql_text: str) -> StatementMetadata:
    """Return empty metadata for SQL that cannot be statically analyzed."""

    return StatementMetadata(
        parsed_sql_text=exp.Command(
            this="UNSUPPORTED_SQL",
            expression=exp.Literal.string(sql_text),
        ),
        table_updated=None,
        columns_updated=set(),
        tables_referenced_to_columns_referenced={},
    )


def _parse_one(sql_text: str) -> exp.Expression:
    """Parse one SQLite statement, including INSERT ... DEFAULT VALUES."""

    try:
        return sqlglot.parse_one(sql_text, dialect="sqlite")
    except sqlglot.errors.ParseError:
        normalized = _normalize_default_values_insert(sql_text)
        if normalized == sql_text:
            raise
        return sqlglot.parse_one(normalized, dialect="sqlite")


def _normalize_default_values_insert(sql_text: str) -> str:
    """
    Rewrite DEFAULT VALUES into an equivalent metadata-only empty insert.
    This is necessary because sqlglot does not parse INSERT INTO ... DEFAULT VALUES
    """

    stripped = sql_text.lstrip().lower()
    if not (
        stripped.startswith("insert")
        or stripped.startswith("with")
    ):
        return sql_text

    return DEFAULT_VALUES_INSERT_PATTERN.sub("() VALUES ()", sql_text, count=1)


def _target_table_name(parsed_sql_text: exp.Expression) -> str | None:
    """Return the table directly written by INSERT, UPDATE, or DELETE."""

    if isinstance(parsed_sql_text, (exp.Insert, exp.Update, exp.Delete)):
        return table_name(parsed_sql_text.this)
    return None


def _updated_columns(parsed_sql_text: exp.Expression) -> set[str]:
    """Return UPDATE assignment names; other write statements return '*'."""

    if not isinstance(parsed_sql_text, exp.Update):
        return {ALL_COLUMNS}

    return {
        column.name
        for assignment in parsed_sql_text.expressions
        if isinstance(assignment, exp.EQ)
        for column in [assignment.left]
        if isinstance(column, exp.Column)
    }


def _referenced_tables_to_columns(
    parsed_sql_text: exp.Expression,
    table_columns: TableColumns | None,
) -> dict[str, set[str]]:
    """Collect real table columns read by the statement.

    CTE bodies are scanned directly. Later references to CTE/derived output
    columns are ignored instead of trying to perform full output-column lineage.
    """

    references: defaultdict[str, set[str]] = defaultdict(set)
    ignored_relations = _cte_output_columns(parsed_sql_text)
    statement_outer_frame = ResolutionFrame(
        _dml_outer_sources(parsed_sql_text, ignored_relations),
    )
    cte_definition_frame = ResolutionFrame(_new_source_map())

    for cte in _with_ctes(parsed_sql_text):
        _collect_select_scope_references(
            references,
            cte.this,
            cte_definition_frame,
            ignored_relations,
            table_columns,
        )
        _collect_non_select_column_references(
            references,
            cte.this,
            cte_definition_frame,
            table_columns,
        )

    for root in _read_roots(parsed_sql_text):
        _collect_select_scope_references(
            references,
            root,
            statement_outer_frame,
            ignored_relations,
            table_columns,
        )
        _collect_non_select_column_references(
            references,
            root,
            statement_outer_frame,
            table_columns,
        )

    return dict(references)


def _collect_select_scope_references(
    references: defaultdict[str, set[str]],
    expression: exp.Expression,
    outer_frame: ResolutionFrame,
    ignored_relations: IgnoredRelations,
    table_columns: TableColumns | None,
) -> None:
    """
    Resolve column reads inside SELECT-like scopes contained by expression.
    traverse_scope returns scopes inside-out, so the final scope is the root of
    that SELECT tree and contains direct child scopes such as CTEs and UNIONs.
    """
    for select_root in _outermost_select_roots(expression):
        scopes = traverse_scope(select_root)
        if scopes:
            _collect_scope_tree_references(
                references,
                scopes[-1],
                outer_frame,
                ignored_relations,
                table_columns,
            )


def _collect_scope_tree_references(
    references: defaultdict[str, set[str]],
    scope: Scope,
    outer_frame: ResolutionFrame,
    ignored_relations: IgnoredRelations,
    table_columns: TableColumns | None,
) -> None:
    """Walk a sqlglot SELECT scope tree and resolve columns frame by frame."""

    frame = ResolutionFrame(
        _scope_sources(scope, ignored_relations),
        outer_frame,
    )

    _collect_direct_references(references, scope.expression, frame, table_columns)

    for child_scope in _scope_children(scope):
        child_outer_frame = _outer_frame_for_child_scope(
            child_scope,
            frame,
            outer_frame,
        )
        _collect_scope_tree_references(
            references,
            child_scope,
            child_outer_frame,
            ignored_relations,
            table_columns,
        )


def _collect_non_select_column_references(
    references: defaultdict[str, set[str]],
    expression: exp.Expression,
    frame: ResolutionFrame,
    table_columns: TableColumns | None,
) -> None:
    """Resolve direct column reads in DML fragments outside nested SELECTs."""

    if isinstance(expression, exp.Subqueryable):
        return

    _collect_direct_references(references, expression, frame, table_columns)


def _collect_direct_references(
    references: defaultdict[str, set[str]],
    expression: exp.Expression,
    frame: ResolutionFrame,
    table_columns: TableColumns | None,
) -> None:
    """Record column/star reads directly under expression's current scope."""

    for column in _direct_columns(expression):
        _add_column_reference(references, column, frame, table_columns)

    if _has_direct_bare_star(expression):
        _add_star_reference(references, frame)


def _add_column_reference(
    references: defaultdict[str, set[str]],
    column: exp.Column,
    frame: ResolutionFrame,
    table_columns: TableColumns | None,
) -> None:
    """Resolve one column expression and add it to the reference map."""

    if column.name == ALL_COLUMNS:
        _add_star_reference(references, frame, column.table or None)
        return

    if column.table:
        table = _resolve_qualified_table(frame, column.table)
    else:
        table = _resolve_unqualified_table(frame, column.name, table_columns)

    if table is not None:
        _add_reference(references, table, column.name)


def _add_star_reference(
    references: defaultdict[str, set[str]],
    frame: ResolutionFrame,
    table_or_alias: str | None = None,
) -> None:
    """Resolve SELECT * or table.* and mark affected real tables as all-read."""

    if table_or_alias:
        table = _resolve_qualified_table(frame, table_or_alias)
        if table is not None:
            _add_reference(references, table, ALL_COLUMNS)
        return

    for table in set(frame.sources.alias_to_table.values()):
        _add_reference(references, table, ALL_COLUMNS)


def _add_reference(
    references: defaultdict[str, set[str]],
    table: str,
    column: str,
) -> None:
    """Add a table/column read, with '*' taking precedence over specifics."""

    if column == ALL_COLUMNS:
        references[table] = {ALL_COLUMNS}
    elif ALL_COLUMNS not in references[table]:
        references[table].add(column)


def _resolve_qualified_table(
    frame: ResolutionFrame | None,
    qualifier: str,
) -> str | None:
    """Resolve table.column or alias.column through nested frames."""

    while frame is not None:
        if qualifier in frame.sources.ignored_aliases:
            return None

        table = frame.sources.alias_to_table.get(qualifier)
        if table is not None:
            return table

        frame = frame.parent

    return None


def _resolve_unqualified_table(
    frame: ResolutionFrame | None,
    column_name: str,
    table_columns: TableColumns | None,
) -> str | None:
    """Resolve an unqualified column, preferring inner scopes over parents."""

    while frame is not None:
        candidates = _real_table_candidates(frame.sources, column_name, table_columns)
        if len(candidates) == 1:
            return next(iter(candidates))

        if column_name in frame.sources.output_aliases:
            return None

        if _ignored_source_may_provide_column(frame.sources, column_name):
            return None

        frame = frame.parent

    return None


def _ignored_source_may_provide_column(
    sources: SourceMap,
    column_name: str,
) -> bool:
    """Return whether an ignored CTE/derived source could own column_name."""
    return any(
        ALL_COLUMNS in columns or column_name in columns
        for columns in sources.ignored_columns.values()
    )


def _real_table_candidates(
    sources: SourceMap,
    column_name: str,
    table_columns: TableColumns | None,
) -> set[str]:
    """Return visible real tables that could provide an unqualified column."""

    real_tables = set(sources.alias_to_table.values())
    if table_columns is None:
        return real_tables

    return {
        table
        for table in real_tables
        if column_name in table_columns.get(table, set())
    }


def _scope_children(scope: Scope) -> list[Scope]:
    """Return direct child scopes created by CTEs, set ops, and subqueries."""

    return [
        *scope.cte_scopes,
        *scope.union_scopes,
        *scope.table_scopes,
        *scope.subquery_scopes,
    ]


def _outer_frame_for_child_scope(
    child_scope: Scope,
    current_frame: ResolutionFrame,
    outer_frame: ResolutionFrame,
) -> ResolutionFrame:
    """Return the semantic outer frame used for a child scope lookup."""

    if child_scope.is_cte or child_scope.is_derived_table:
        return outer_frame

    return current_frame


def _scope_sources(scope: Scope, ignored_relations: IgnoredRelations) -> SourceMap:
    """Build table aliases visible from one sqlglot SELECT scope."""

    sources = _new_source_map(output_aliases=_explicit_output_aliases(scope.expression))
    for alias, (_, source) in scope.selected_sources.items():
        if isinstance(source, exp.Table):
            _add_table_source(sources, source, ignored_relations)
        elif isinstance(source, Scope):
            sources.ignored_aliases.add(alias)
            sources.ignored_columns[alias] = _scope_output_columns(source)

    return sources


def _dml_outer_sources(
    parsed_sql_text: exp.Expression,
    ignored_relations: IgnoredRelations,
) -> SourceMap:
    """Build DML sources visible to nested reads outside their own scope."""

    sources = _new_source_map()

    if isinstance(parsed_sql_text, (exp.Update, exp.Delete)):
        target_table = table_expression(parsed_sql_text.this)
        if target_table is not None:
            _add_table_source(sources, target_table, ignored_relations)

    if isinstance(parsed_sql_text, exp.Update):
        for table in _from_table_sources(parsed_sql_text.args.get("from")):
            _add_table_source(sources, table, ignored_relations)

    return sources


def _from_table_sources(from_expression: exp.Expression | None) -> list[exp.Table]:
    """Return table nodes directly named by a FROM clause and its joins."""

    if from_expression is None:
        return []

    return [
        table
        for table in from_expression.find_all(exp.Table)
        if not _has_ancestor_before_root(table, exp.Select, from_expression)
    ]


def _add_table_source(
    sources: SourceMap,
    table: exp.Table,
    ignored_relations: IgnoredRelations,
) -> None:
    """Add a real table source, or ignore a source known to be a CTE."""

    table_name = table.name
    if not table_name:
        return

    alias = table.alias_or_name
    if not table.db and table_name in ignored_relations:
        ignored_columns = ignored_relations[table_name]
        sources.ignored_aliases.add(alias)
        sources.ignored_aliases.add(table_name)
        sources.ignored_columns[alias] = ignored_columns
        sources.ignored_columns[table_name] = ignored_columns
        return

    sources.alias_to_table[alias] = table_name
    sources.alias_to_table[table_name] = table_name


def _new_source_map(output_aliases: set[str] | None = None) -> SourceMap:
    """Return an empty source map."""

    return SourceMap(
        alias_to_table={},
        ignored_aliases=set(),
        ignored_columns={},
        output_aliases=set(output_aliases or set()),
    )


def _explicit_output_aliases(expression: exp.Expression) -> set[str]:
    """Return explicit SELECT aliases visible to clauses such as HAVING."""

    if not isinstance(expression, exp.Select):
        return set()

    return {
        select_expression.alias
        for select_expression in expression.expressions
        if isinstance(select_expression, exp.Alias) and select_expression.alias
    }


def _with_ctes(expression: exp.Expression) -> list[exp.CTE]:
    """Return CTE definitions attached to expression, if any."""

    with_expression = expression.args.get("with")
    if with_expression is None:
        return []

    return list(with_expression.expressions or [])


def _cte_output_columns(expression: exp.Expression) -> IgnoredRelations:
    """Return top-level CTE names mapped to their output column names."""

    return {
        cte.alias: set(cte.alias_column_names) or _expression_output_columns(cte.this)
        for cte in _with_ctes(expression)
        if cte.alias
    }


def _scope_output_columns(scope: Scope) -> set[str]:
    """Return output names for a CTE/derived scope, if sqlglot exposes them."""

    outer_columns = set(scope.outer_column_list or [])
    if outer_columns:
        return outer_columns

    return _expression_output_columns(scope.expression)


def _expression_output_columns(expression: exp.Expression) -> set[str]:
    """Return output names for SELECT-like expressions, or '*' if unknown."""

    columns = set(expression.named_selects)
    return columns or {ALL_COLUMNS}


def _read_roots(parsed_sql_text: exp.Expression) -> list[exp.Expression]:
    """Return statement fragments where read expressions can appear."""

    if isinstance(parsed_sql_text, exp.Insert):
        roots: list[exp.Expression] = []
        if parsed_sql_text.expression is not None:
            roots.append(parsed_sql_text.expression)
        return roots

    if isinstance(parsed_sql_text, exp.Update):
        roots = [
            assignment.right
            if isinstance(assignment, exp.EQ)
            else assignment
            for assignment in parsed_sql_text.expressions
        ]
        for key in ("from", "where"):
            value = parsed_sql_text.args.get(key)
            if value is not None:
                roots.append(value)
        return roots

    if isinstance(parsed_sql_text, exp.Delete):
        roots = []
        value = parsed_sql_text.args.get("where")
        if value is not None:
            roots.append(value)
        return roots

    return [parsed_sql_text]


def _outermost_select_roots(expression: exp.Expression) -> list[exp.Expression]:
    """Return SELECT expressions not nested inside another SELECT under root."""

    return [
        select
        for select in expression.find_all(exp.Select)
        if not _has_ancestor_before_root(select, exp.Select, expression)
    ]


def _direct_columns(expression: exp.Expression) -> list[exp.Column]:
    """Return columns directly belonging to expression's current SQL scope."""

    columns = [expression] if isinstance(expression, exp.Column) else []
    columns.extend(expression.find_all(exp.Column))
    return [
        column
        for column in columns
        if not _has_ancestor_before_root(column, exp.Select, expression)
    ]


def _has_direct_bare_star(expression: exp.Expression) -> bool:
    """Return whether the current scope has a row-set star such as * or COUNT(*)."""

    return any(
        not isinstance(star.parent, exp.Column)
        and not _has_ancestor_before_root(star, exp.Select, expression)
        for star in expression.find_all(exp.Star)
    )


def _has_ancestor_before_root(
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
