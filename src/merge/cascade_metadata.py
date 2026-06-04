from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import sqlite3
from typing import TYPE_CHECKING, Literal, cast, get_args

from .cascade_types import (
    CascadeEventKind,
    CascadeWriteEvent,
    ForeignKeyAction,
    ForeignKeyEdge,
    ForeignKeyEdgeMap,
    ForeignKeyEdges,
)
from .utils import (
    ALL_COLUMNS,
    TableColumns,
    TablePrimaryKeyColumns,
    add_columns_to_column_map,
    column_overlap,
    quote_identifier,
    row_value,
)

if TYPE_CHECKING:
    from .models import ConflictCheckContext

VALID_FOREIGN_KEY_ACTIONS: frozenset[str] = frozenset(get_args(ForeignKeyAction))
CASCADE_CONFLICT_DETAIL = ("metadata_source", "cascade")
MAX_CASCADE_METADATA_DEPTH = 64


@dataclass(frozen=True)
class CascadeEffects:
    """Hidden reads/writes caused by recursive foreign-key actions."""

    tables_updated_to_columns_updated: dict[str, set[str]]
    tables_referenced_to_columns_referenced: dict[str, set[str]]
    write_events: tuple[CascadeWriteEvent, ...]

    @property
    def has_effects(self) -> bool:
        return bool(
            self.tables_updated_to_columns_updated
            or self.tables_referenced_to_columns_referenced
        )


def cascade_effects_for_parsed_statement(
    context: ConflictCheckContext,
    *,
    statement_kind: CascadeEventKind,
    table_updated: str,
    columns_updated: set[str],
) -> CascadeEffects:
    """Return hidden FK reads/writes for one parsed UPDATE/DELETE statement."""

    if statement_kind == "delete":
        initial_event = CascadeWriteEvent(
            table=table_updated,
            columns=frozenset({ALL_COLUMNS}),
            kind="delete",
        )
    else:
        initial_event = CascadeWriteEvent(
            table=table_updated,
            columns=frozenset(columns_updated),
            kind="update",
        )

    return _cascade_effects_for_event(context, initial_event)


def foreign_key_edges(
    context: ConflictCheckContext,
) -> ForeignKeyEdges:
    """Return child columns, parent columns, and actions for each FK edge."""

    edges = context.schema_cache.foreign_key_edges
    if edges is None:
        edges = _foreign_key_edges(context)
        context.schema_cache.foreign_key_edges = edges
    return edges


def foreign_key_edges_by_parent(
    context: ConflictCheckContext,
) -> ForeignKeyEdgeMap:
    """Return FK edges grouped by referenced parent table."""

    edges_by_parent = context.schema_cache.foreign_key_edges_by_parent
    if edges_by_parent is None:
        edges_by_parent = group_foreign_key_edges_by_table(
            foreign_key_edges(context),
            table_role="parent",
        )
        context.schema_cache.foreign_key_edges_by_parent = edges_by_parent
    return edges_by_parent


def foreign_key_edges_by_child(
    context: ConflictCheckContext,
) -> ForeignKeyEdgeMap:
    """Return FK edges grouped by referencing child table."""

    edges_by_child = context.schema_cache.foreign_key_edges_by_child
    if edges_by_child is None:
        edges_by_child = group_foreign_key_edges_by_table(
            foreign_key_edges(context),
            table_role="child",
        )
        context.schema_cache.foreign_key_edges_by_child = edges_by_child
    return edges_by_child


def _cascade_effects_for_event(
    context: ConflictCheckContext,
    initial_event: CascadeWriteEvent,
) -> CascadeEffects:
    """Return hidden FK reads/writes caused by one parent-table event."""

    updated: dict[str, set[str]] = {}
    referenced: dict[str, set[str]] = {}
    write_events: list[CascadeWriteEvent] = []
    queue: deque[tuple[CascadeWriteEvent, int]] = deque([(initial_event, 0)])
    visited: set[tuple[str, CascadeEventKind, frozenset[str]]] = set()

    while queue:
        event, depth = queue.popleft()
        if depth > MAX_CASCADE_METADATA_DEPTH:
            _mark_unknown_cascade_effects(
                context,
                updated,
                referenced,
                write_events,
            )
            break

        # Cascade metadata is schema-level: once a table/kind/column set has
        # been expanded, different row values cannot add new table/column facts.
        event_key = (event.table, event.kind, event.columns)
        if event_key in visited:
            continue
        visited.add(event_key)

        for edge in foreign_key_edges_by_parent(context).get(event.table, ()):
            action = _action_for_parent_event(edge, event)
            if action is None:
                continue

            add_columns_to_column_map(
                referenced,
                edge.child_table,
                set(edge.child_columns),
            )
            child_event = _cascade_child_event(edge, action, event.kind)
            if child_event is None:
                continue

            add_columns_to_column_map(
                updated,
                child_event.table,
                set(child_event.columns),
            )
            write_events.append(child_event)
            queue.append((child_event, depth + 1))

    return CascadeEffects(
        tables_updated_to_columns_updated=updated,
        tables_referenced_to_columns_referenced=referenced,
        write_events=tuple(write_events),
    )


def _action_for_parent_event(
    edge: ForeignKeyEdge,
    event: CascadeWriteEvent,
) -> ForeignKeyAction | None:
    """Return the FK action triggered by a parent delete/update event."""

    if event.kind == "delete":
        return edge.on_delete
    if column_overlap(set(event.columns), set(edge.parent_columns)):
        return edge.on_update
    return None


def _cascade_child_event(
    edge: ForeignKeyEdge,
    action: ForeignKeyAction,
    parent_event_kind: CascadeEventKind,
) -> CascadeWriteEvent | None:
    """Return the hidden child write event caused by one FK action."""

    if action == "CASCADE" and parent_event_kind == "delete":
        return CascadeWriteEvent(
            table=edge.child_table,
            columns=frozenset({ALL_COLUMNS}),
            kind="delete",
        )
    if action in {"CASCADE", "SET NULL", "SET DEFAULT"}:
        return CascadeWriteEvent(
            table=edge.child_table,
            columns=frozenset(edge.child_columns),
            kind="update",
        )
    return None


def cascade_event_may_create_or_change_key(
    event: CascadeWriteEvent,
    table: str,
    key_columns: set[str],
) -> bool:
    """Return whether a cascade update may introduce/change a key value."""

    return (
        event.kind == "update"
        and event.table == table
        and bool(column_overlap(set(event.columns), key_columns))
    )


def cascade_event_may_remove_or_change_key(
    event: CascadeWriteEvent,
    table: str,
    key_columns: set[str],
) -> bool:
    """Return whether a cascade delete/update may remove/change a key value."""

    if event.table != table:
        return False
    if event.kind == "delete":
        return True
    return bool(column_overlap(set(event.columns), key_columns))


def _mark_unknown_cascade_effects(
    context: ConflictCheckContext,
    updated: dict[str, set[str]],
    referenced: dict[str, set[str]],
    write_events: list[CascadeWriteEvent],
) -> None:
    """Conservatively mark all tables when cascade recursion is too deep."""

    for table in context.table_columns:
        add_columns_to_column_map(updated, table, {ALL_COLUMNS})
        add_columns_to_column_map(referenced, table, {ALL_COLUMNS})
        write_events.append(
            CascadeWriteEvent(
                table=table,
                columns=frozenset({ALL_COLUMNS}),
                kind="update",
            )
        )


def _foreign_key_edges(
    context: ConflictCheckContext,
) -> ForeignKeyEdges:
    """Return child/parent columns and actions for each foreign-key edge."""

    return load_foreign_key_edges(
        context.base_cursor,
        context.table_columns,
        context.primary_key_columns,
    )


def load_foreign_key_edges(
    cursor: sqlite3.Cursor,
    table_columns: TableColumns,
    primary_key_columns: TablePrimaryKeyColumns,
) -> ForeignKeyEdges:
    """Load child/parent columns and actions for each foreign-key edge."""

    edges: list[ForeignKeyEdge] = []
    for child_table in table_columns:
        rows = cursor.execute(
            f"PRAGMA foreign_key_list({quote_identifier(child_table)})"
        ).fetchall()

        # PRAGMA foreign_key_list returns one row per child column. Rows that
        # belong to the same FK constraint share an "id"; composite FKs have
        # multiple rows with that same id and ordered "seq" values.
        grouped: dict[int, list[sqlite3.Row | tuple]] = {}
        for row in rows:
            grouped.setdefault(int(row_value(row, "id", 0)), []).append(row)

        for edge_rows in grouped.values():
            ordered_rows = sorted(
                edge_rows,
                key=lambda row: int(row_value(row, "seq", 1)),
            )
            parent_table = str(row_value(ordered_rows[0], "table", 2))
            on_update = _foreign_key_action(ordered_rows[0], "on_update", 5)
            on_delete = _foreign_key_action(ordered_rows[0], "on_delete", 6)
            parent_pk_columns = primary_key_columns.get(parent_table, ())

            # A NULL "to" column means the FK used SQLite's shorthand:
            # REFERENCES parent_table, so the parent columns are the parent's
            # primary-key columns in PK order. If the child column count does
            # not match the parent PK count, SQLite will reject the schema at
            # DML/foreign-key-check time; skip because we cannot map it safely.
            uses_parent_pk_shorthand = any(
                row_value(row, "to", 4) is None
                for row in ordered_rows
            )
            if uses_parent_pk_shorthand and len(ordered_rows) != len(parent_pk_columns):
                continue

            child_columns: list[str] = []
            parent_columns: list[str] = []
            for index, row in enumerate(ordered_rows):
                child_columns.append(str(row_value(row, "from", 3)))
                parent_column = row_value(row, "to", 4)
                # Explicit REFERENCES parent(col) rows store col in "to".
                # Shorthand rows store NULL, handled with parent_pk_columns.
                parent_columns.append(
                    parent_pk_columns[index]
                    if parent_column is None
                    else str(parent_column)
                )

            if parent_columns:
                edges.append(
                    ForeignKeyEdge(
                        child_table=child_table,
                        child_columns=tuple(child_columns),
                        parent_table=parent_table,
                        parent_columns=tuple(parent_columns),
                        on_update=on_update,
                        on_delete=on_delete,
                    )
                )
    return tuple(edges)


def _foreign_key_action(
    row: sqlite3.Row | tuple,
    key: str,
    index: int,
) -> ForeignKeyAction:
    """Return one SQLite FK action from PRAGMA foreign_key_list."""

    action = str(row_value(row, key, index)).upper()
    if action in VALID_FOREIGN_KEY_ACTIONS:
        return cast(ForeignKeyAction, action)
    return "NO ACTION"


def group_foreign_key_edges_by_table(
    edges: ForeignKeyEdges,
    *,
    table_role: Literal["parent", "child"],
) -> ForeignKeyEdgeMap:
    """Return FK edges keyed by either their parent or child table."""

    grouped: dict[str, list[ForeignKeyEdge]] = {}
    for edge in edges:
        table = edge.parent_table if table_role == "parent" else edge.child_table
        grouped.setdefault(table, []).append(edge)
    return {
        table: tuple(table_edges)
        for table, table_edges in grouped.items()
    }
