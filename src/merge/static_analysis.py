from __future__ import annotations

from collections.abc import Collection
import sqlite3

from sqlglot import expressions as exp

from .models import (
    BranchName,
    ConflictCheckContext,
    ConflictKind,
    ConflictCheckResult,
    StatementConflict,
)
from .sql_metadata import StatementMetadata, TransactionMetadata
from .utils import (
    ALL_COLUMNS,
    TableKeyColumnSets,
    is_delete_statement,
    is_insert_statement,
    is_update_statement,
    quote_identifier,
    row_value,
)


def static_analysis_matching(
    context: ConflictCheckContext,
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
    *,
    enabled_kinds: Collection[ConflictKind] | None = None,
    current_branch: BranchName | None = None,
) -> ConflictCheckResult:
    """Return table/column conflicts, optionally ordered from current to other."""

    kinds = set(enabled_kinds) if enabled_kinds is not None else None
    if len(ours_metadata.statements) == 1 and len(theirs_metadata.statements) == 1:
        return _match_statement_metadata(
            context,
            ours_metadata.statements[0],
            theirs_metadata.statements[0],
            enabled_kinds=kinds,
            current_branch=current_branch,
        )

    result = ConflictCheckResult()
    if _kind_enabled(
        kinds,
        "implicit_insert_key",
    ) and _transaction_has_implicit_insert_key_conflict(
        context,
        ours_metadata,
        theirs_metadata,
    ):
        result.add_conflicts(
            StatementConflict(
                kind="implicit_insert_key",
                message="transactions may conflict through implicit insert keys",
            )
        )

    if _kind_enabled(kinds, "write_write") and _transaction_write_write_overlap(
        ours_metadata,
        theirs_metadata,
    ):
        result.add_conflicts(
            StatementConflict(
                kind="write_write",
                message="transactions write overlapping table columns",
            )
        )

    if _kind_enabled(kinds, "write_read"):
        if (
            current_branch is None or current_branch == "ours"
        ) and _transaction_write_read_overlap(ours_metadata, theirs_metadata):
            result.add_conflicts(
                StatementConflict(
                    kind="write_read",
                    message=(
                        "ours transaction writes columns read by "
                        "theirs transaction"
                    ),
                    details=_write_read_details("ours", "theirs"),
                )
            )
        if (
            current_branch is None or current_branch == "theirs"
        ) and _transaction_write_read_overlap(theirs_metadata, ours_metadata):
            result.add_conflicts(
                StatementConflict(
                    kind="write_read",
                    message=(
                        "theirs transaction writes columns read by "
                        "ours transaction"
                    ),
                    details=_write_read_details("theirs", "ours"),
                )
            )
    return result


def _kind_enabled(
    enabled_kinds: set[ConflictKind] | None,
    kind: ConflictKind,
) -> bool:
    """Return whether a static conflict kind should be checked."""

    return enabled_kinds is None or kind in enabled_kinds


def write_write_candidate_pairs(
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
) -> tuple[tuple[int, int], ...]:
    """Return statement pairs that may have a write/write conflict."""

    return tuple(
        (ours_index, theirs_index)
        for ours_index, ours_statement in enumerate(ours_metadata.statements)
        for theirs_index, theirs_statement in enumerate(theirs_metadata.statements)
        if _metadata_write_write_overlap(ours_statement, theirs_statement)
    )


def write_read_candidate_indexes(
    writer_metadata: TransactionMetadata,
    reader_metadata: TransactionMetadata,
) -> tuple[int, ...]:
    """Return reader statement indexes that may read writer output."""

    return tuple(
        index
        for index, reader_statement in enumerate(reader_metadata.statements)
        if _metadata_reads_any(
            reader_statement,
            writer_metadata.tables_updated_to_columns_updated,
        )
    )


def constraint_conflict_possible(
    context: ConflictCheckContext,
    first_metadata: TransactionMetadata,
    second_metadata: TransactionMetadata,
) -> bool:
    """Return whether SQLite constraints may fail only when both sides replay."""

    return (
        _key_constraint_conflict_possible(context, first_metadata, second_metadata)
        or _foreign_key_constraint_conflict_possible(
            context,
            first_metadata,
            second_metadata,
        )
    )


def omitted_integer_primary_key_column(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> str | None:
    """Return omitted INTEGER PRIMARY KEY column for replay-sensitive INSERTs."""

    return _omitted_integer_primary_key_column(context, metadata)


def foreign_key_edges(
    context: ConflictCheckContext,
) -> tuple[tuple[str, tuple[str, ...], str, tuple[str, ...]], ...]:
    """Return child columns and parent columns for each foreign-key edge."""

    cache_key = "base"
    if cache_key not in context.foreign_key_edges_cache:
        context.foreign_key_edges_cache[cache_key] = _foreign_key_edges(context)
    return context.foreign_key_edges_cache[cache_key]


def _transaction_has_implicit_insert_key_conflict(
    context: ConflictCheckContext,
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
) -> bool:
    """Return whether any statement pair has an implicit-key risk."""

    return any(
        _implicit_insert_key_conflicts(context, ours_statement, theirs_statement)
        for ours_statement in ours_metadata.statements
        for theirs_statement in theirs_metadata.statements
    )


def _transaction_write_write_overlap(
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
) -> bool:
    """Return whether the two transactions may write the same table columns."""

    return _columns_by_table_overlap(
        ours_metadata.tables_updated_to_columns_updated,
        theirs_metadata.tables_updated_to_columns_updated,
    )


def _transaction_write_read_overlap(
    writer_metadata: TransactionMetadata,
    reader_metadata: TransactionMetadata,
) -> bool:
    """Return whether any reader statement may read writer transaction output."""

    return _columns_by_table_overlap(
        writer_metadata.tables_updated_to_columns_updated,
        reader_metadata.tables_referenced_to_columns_referenced,
    )


def _columns_by_table_overlap(
    left: dict[str, set[str]],
    right: dict[str, set[str]],
) -> bool:
    """Return whether two table-to-column maps overlap."""

    for table, left_columns in left.items():
        right_columns = right.get(table)
        if right_columns is not None and _column_overlap(left_columns, right_columns):
            return True
    return False


def _metadata_write_write_overlap(
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> bool:
    """Return whether two statements may write overlapping table columns."""

    return _write_write_conflict(ours_metadata, theirs_metadata) is not None


def _metadata_reads_any(
    metadata: StatementMetadata,
    columns_by_table: dict[str, set[str]],
) -> bool:
    """Return whether metadata reads any provided table columns."""

    return any(
        _metadata_read_overlap(metadata, table, columns)
        for table, columns in columns_by_table.items()
    )


def _key_constraint_conflict_possible(
    context: ConflictCheckContext,
    first_metadata: TransactionMetadata,
    second_metadata: TransactionMetadata,
) -> bool:
    """Return whether both sides may create/change the same PK/unique key."""

    key_sets_by_table = _key_column_sets_by_table(context)
    for table, key_sets in key_sets_by_table.items():
        for key_set in key_sets:
            if _transaction_may_create_or_change_key(
                first_metadata,
                table,
                key_set,
            ) and _transaction_may_create_or_change_key(
                second_metadata,
                table,
                key_set,
            ):
                return True
    return False


def _transaction_may_create_or_change_key(
    metadata: TransactionMetadata,
    table: str,
    key_columns: set[str],
) -> bool:
    """Return whether a transaction may insert/update a key value."""

    return any(
        _statement_may_create_or_change_key(statement, table, key_columns)
        for statement in metadata.statements
    )


def _statement_may_create_or_change_key(
    metadata: StatementMetadata,
    table: str,
    key_columns: set[str],
) -> bool:
    """Return whether one statement can introduce a duplicate key value."""

    if metadata.table_updated != table:
        return False
    if is_insert_statement(metadata):
        return True
    if is_update_statement(metadata):
        return bool(_metadata_write_overlap(metadata, table, key_columns))
    return False


def _foreign_key_constraint_conflict_possible(
    context: ConflictCheckContext,
    first_metadata: TransactionMetadata,
    second_metadata: TransactionMetadata,
) -> bool:
    """Return whether parent-key and child-FK writes may violate an FK edge."""

    for child_table, child_columns, parent_table, parent_columns in (
        foreign_key_edges(context)
    ):
        first_changes_parent = _transaction_may_remove_or_change_key(
            first_metadata,
            parent_table,
            set(parent_columns),
        )
        second_changes_parent = _transaction_may_remove_or_change_key(
            second_metadata,
            parent_table,
            set(parent_columns),
        )
        first_writes_child = _transaction_may_create_or_change_key(
            first_metadata,
            child_table,
            set(child_columns),
        )
        second_writes_child = _transaction_may_create_or_change_key(
            second_metadata,
            child_table,
            set(child_columns),
        )
        if (
            first_changes_parent
            and second_writes_child
            or second_changes_parent
            and first_writes_child
        ):
            return True
    return False


def _transaction_may_remove_or_change_key(
    metadata: TransactionMetadata,
    table: str,
    key_columns: set[str],
) -> bool:
    """Return whether a transaction may remove/update a referenced key."""

    return any(
        statement.table_updated == table
        and (
            is_delete_statement(statement)
            or (
                is_update_statement(statement)
                and bool(_metadata_write_overlap(statement, table, key_columns))
            )
        )
        for statement in metadata.statements
    )


def _key_column_sets_by_table(
    context: ConflictCheckContext,
) -> TableKeyColumnSets:
    """Return cached key sets, dropping tables that have no key metadata."""

    return {
        table: key_sets
        for table, key_sets in context.key_column_sets.items()
        if key_sets
    }


def _foreign_key_edges(
    context: ConflictCheckContext,
) -> tuple[tuple[str, tuple[str, ...], str, tuple[str, ...]], ...]:
    """Return child columns and parent columns for each foreign-key edge."""

    edges: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = []
    for child_table in context.table_columns:
        rows = context.base_cursor.execute(
            f"PRAGMA foreign_key_list({quote_identifier(child_table)})"
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row | tuple]] = {}
        for row in rows:
            grouped.setdefault(int(row_value(row, "id", 0)), []).append(row)

        for edge_rows in grouped.values():
            ordered_rows = sorted(edge_rows, key=lambda row: int(row_value(row, "seq", 1)))
            parent_table = str(row_value(ordered_rows[0], "table", 2))
            parent_pk_columns = context.primary_key_columns.get(parent_table, ())
            # if parent key reference is not specified by SQLite it should refer to its PK columns
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
                parent_columns.append(
                    parent_pk_columns[index]
                    if parent_column is None
                    else str(parent_column)
                )

            if parent_columns:
                edges.append((
                    child_table,
                    tuple(child_columns),
                    parent_table,
                    tuple(parent_columns),
                ))
    return tuple(edges)


def _match_statement_metadata(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
    *,
    enabled_kinds: set[ConflictKind] | None = None,
    current_branch: BranchName | None = None,
) -> ConflictCheckResult:
    """Compare metadata for write/write and selected write/read directions."""

    conflicts: list[StatementConflict] = []
    if _kind_enabled(enabled_kinds, "implicit_insert_key"):
        conflicts.extend(
            _implicit_insert_key_conflicts(context, ours_metadata, theirs_metadata)
        )

    if _kind_enabled(enabled_kinds, "write_write"):
        write_write = _write_write_conflict(ours_metadata, theirs_metadata)
        if write_write is not None:
            conflicts.append(write_write)

    if _kind_enabled(enabled_kinds, "write_read"):
        if current_branch is None or current_branch == "ours":
            conflicts.extend(
                _write_read_conflicts(
                    writer_label="ours",
                    writer=ours_metadata,
                    reader_label="theirs",
                    reader=theirs_metadata,
                )
            )
        if current_branch is None or current_branch == "theirs":
            conflicts.extend(
                _write_read_conflicts(
                    writer_label="theirs",
                    writer=theirs_metadata,
                    reader_label="ours",
                    reader=ours_metadata,
                )
            )
    return ConflictCheckResult(conflicts)


def _write_write_conflict(
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> StatementConflict | None:
    """Return a conflict when both statements write overlapping table columns."""

    if not _same_written_table(ours_metadata, theirs_metadata):
        return None

    if is_delete_statement(ours_metadata) and is_delete_statement(theirs_metadata):
        return None

    if is_insert_statement(ours_metadata) or is_insert_statement(theirs_metadata):
        return None

    ours_columns = _written_columns(ours_metadata)
    overlap = _metadata_write_overlap(
        theirs_metadata,
        ours_metadata.table_updated,
        ours_columns,
    )
    if not overlap:
        return None

    return StatementConflict(
        kind="write_write",
        message=(
            "Both statements write "
            f"{ours_metadata.table_updated}.{_format_columns(overlap)}"
        ),
    )


def _write_read_conflicts(
    writer_label: str,
    writer: StatementMetadata,
    reader_label: str,
    reader: StatementMetadata,
) -> list[StatementConflict]:
    """Return conflicts where one statement writes columns the other reads."""

    table = writer.table_updated
    if table is None:
        return []

    written_columns = _written_columns(writer)
    if not written_columns:
        return []

    overlap = _metadata_read_overlap(
        reader,
        table,
        written_columns,
    )
    if not overlap:
        return []

    return [
        StatementConflict(
            kind="write_read",
            message=(
                f"{writer_label} writes "
                f"{table}.{_format_columns(overlap)}; "
                f"{reader_label} reads it"
            ),
            details=_write_read_details(writer_label, reader_label),
        )
    ]


def _write_read_details(
    writer_label: str,
    reader_label: str,
) -> tuple[tuple[str, str], ...]:
    """Return structured direction data for execution-based refinement."""

    return (
        ("writer", writer_label),
        ("reader", reader_label),
    )


def _implicit_insert_key_conflicts(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> list[StatementConflict]:
    """Return conflicts caused by hidden INTEGER PRIMARY KEY assignment."""

    conflicts: list[StatementConflict] = []
    for insert_label, insert, other_label, other in (
        ("ours", ours_metadata, "theirs", theirs_metadata),
        ("theirs", theirs_metadata, "ours", ours_metadata),
    ):
        omitted_key = _omitted_integer_primary_key_column(context, insert)
        if omitted_key is None or not _same_written_table(insert, other):
            continue

        other_omitted_key = _omitted_integer_primary_key_column(context, other)
        if other_omitted_key is not None:
            conflicts.append(
                _implicit_insert_key_conflict(
                    insert_label,
                    other_label,
                    insert.table_updated,
                    omitted_key,
                )
            )
            break

        update_conflict = _implicit_insert_key_update_conflict(
            insert_label=insert_label,
            insert=insert,
            other_label=other_label,
            other=other,
            omitted_key=omitted_key,
        )
        if update_conflict is not None:
            conflicts.append(update_conflict)
    return conflicts


def _implicit_insert_key_update_conflict(
    insert_label: str,
    insert: StatementMetadata,
    other_label: str,
    other: StatementMetadata,
    omitted_key: str,
) -> StatementConflict | None:
    """Return an implicit-key conflict between an INSERT and key UPDATE."""

    if not _same_written_table(insert, other) or not is_update_statement(other):
        return None

    written_columns = _metadata_write_overlap(
        other,
        insert.table_updated,
        {omitted_key},
    )
    if not written_columns:
        return None

    return StatementConflict(
        kind="implicit_insert_key",
        message=(
            f"{insert_label} INSERT omits {insert.table_updated}.{omitted_key}; "
            f"{other_label} UPDATE assigns to the same key column"
        ),
    )


def _implicit_insert_key_conflict(
    insert_label: str,
    other_label: str,
    table: str | None,
    key_column: str,
) -> StatementConflict:
    """Build a conflict for an implicit-key INSERT against another INSERT."""

    return StatementConflict(
        kind="implicit_insert_key",
        message=(
            f"{insert_label} INSERT omits {table}.{key_column}; "
            f"{other_label} INSERT omits the same key"
        ),
    )


def _omitted_integer_primary_key_column(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> str | None:
    """Return omitted INTEGER PRIMARY KEY column for replay-sensitive INSERTs."""

    if not is_insert_statement(metadata) or metadata.table_updated is None:
        return None

    key_column = _integer_primary_key_column(context, metadata.table_updated)
    if key_column is None:
        return None

    explicit_columns = _insert_explicit_columns(context, metadata)
    if ALL_COLUMNS in explicit_columns or key_column in explicit_columns:
        return None
    return key_column


def _integer_primary_key_column(
    context: ConflictCheckContext,
    table: str,
) -> str | None:
    """Return the rowid-alias INTEGER PRIMARY KEY column, if the table has one."""

    if table in context.integer_primary_key_columns:
        return context.integer_primary_key_columns[table]

    rows = context.base_cursor.execute(
        f"PRAGMA table_info({quote_identifier(table)})"
    ).fetchall()
    primary_key_rows = [
        row
        for row in rows
        if int(row_value(row, "pk", 5) or 0) > 0
    ]
    if len(primary_key_rows) != 1:
        context.integer_primary_key_columns[table] = None
        return None

    row = primary_key_rows[0]
    declared_type = str(row_value(row, "type", 2) or "").upper()
    if declared_type != "INTEGER":
        context.integer_primary_key_columns[table] = None
        return None
    key_column = str(row_value(row, "name", 1))
    context.integer_primary_key_columns[table] = key_column
    return key_column


def _insert_explicit_columns(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> set[str]:
    """Return INSERT target columns explicitly provided by the statement."""

    target = metadata.parsed_sql_text.this
    if isinstance(target, exp.Schema):
        return {
            expression.name
            for expression in target.expressions
            if expression.name
        }

    if metadata.table_updated is not None:
        return {ALL_COLUMNS}

    return set()


def _metadata_write_overlap(
    metadata: StatementMetadata,
    table: str | None,
    columns: set[str],
) -> set[str]:
    """Return columns metadata writes on table."""

    if table is None or metadata.table_updated != table:
        return set()

    return _column_overlap(metadata.columns_updated, columns)


def _metadata_read_overlap(
    metadata: StatementMetadata,
    table: str | None,
    columns: set[str],
) -> set[str]:
    """Return columns metadata reads on table."""

    if table is None:
        return set()

    references = metadata.tables_referenced_to_columns_referenced
    return _column_overlap(references.get(table, set()), columns)


def _written_columns(
    metadata: StatementMetadata,
) -> set[str]:
    """Return columns written by a statement, with INSERT/DELETE as all columns."""

    table = metadata.table_updated
    if table is None:
        return set()
    return metadata.columns_updated


def _column_overlap(left: set[str], right: set[str]) -> set[str]:
    """Return overlapping columns, treating '*' as all columns."""

    if not left or not right:
        return set()

    if ALL_COLUMNS in left and ALL_COLUMNS in right:
        return {ALL_COLUMNS}
    if ALL_COLUMNS in left:
        return set(right)
    if ALL_COLUMNS in right:
        return set(left)
    return left & right


def _same_written_table(
    left: StatementMetadata,
    right: StatementMetadata,
) -> bool:
    """Return whether both statements write the same real table."""

    return left.table_updated is not None and left.table_updated == right.table_updated


def _format_columns(columns: set[str]) -> str:
    """Format a column set for a human-readable conflict message."""

    if columns == {ALL_COLUMNS}:
        return ALL_COLUMNS
    return ", ".join(sorted(columns))
