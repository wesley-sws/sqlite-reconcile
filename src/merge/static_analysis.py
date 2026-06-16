from __future__ import annotations

from collections.abc import Collection

from sqlglot import expressions as exp

from .cascade_metadata import (
    CASCADE_CONFLICT_DETAIL,
    cascade_event_may_create_or_change_key,
    cascade_event_may_remove_or_change_key,
    foreign_key_edges_by_child,
    foreign_key_edges_by_parent,
)
from .conflict_kinds import kind_enabled
from .models import (
    BranchName,
    ConflictCheckContext,
    ConflictKind,
    ConflictCheckResult,
    StatementConflict,
)
from .sql_metadata import (
    StatementMetadata,
    TransactionMetadata,
    required_updated_table,
)
from .utils import (
    ALL_COLUMNS,
    TableKeyColumnSets,
    column_overlap,
    is_delete_statement,
    is_insert_statement,
    is_update_statement,
    integer_primary_key_column,
)


def static_analysis_matching(
    context: ConflictCheckContext,
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
    *,
    enabled_kinds: Collection[ConflictKind] | None = None,
    current_branch: BranchName | None = None,
    ours_label: str | None = None,
    theirs_label: str | None = None,
) -> ConflictCheckResult:
    """Return table/column conflicts, optionally ordered from current to other."""

    kinds = set(enabled_kinds) if enabled_kinds is not None else None
    result = ConflictCheckResult()
    if kind_enabled(kinds, "implicit_insert_key"):
        result.add_conflicts(
            *_transaction_implicit_insert_key_conflicts(
                context,
                ours_metadata,
                theirs_metadata,
            )
        )

    if kind_enabled(kinds, "write_write") and _transaction_write_write_overlap(
        ours_metadata,
        theirs_metadata,
    ):
        has_cascade = _transaction_has_cascade_effects(
            ours_metadata,
        ) or _transaction_has_cascade_effects(theirs_metadata)
        result.add_conflicts(
            StatementConflict(
                kind="write_write",
                message="transactions write overlapping table columns",
                details=_cascade_conflict_details(has_cascade),
            )
        )

    if kind_enabled(kinds, "write_read"):
        if (
            current_branch is None or current_branch == "ours"
        ) and _transaction_write_read_overlap(
            ours_metadata,
            theirs_metadata,
        ):
            has_cascade = _transaction_has_cascade_effects(
                ours_metadata,
            ) or _transaction_has_cascade_effects(theirs_metadata)
            result.add_conflicts(
                StatementConflict(
                    kind="write_read",
                    message=_write_read_message(
                        "ours",
                        "theirs",
                        ours_label=ours_label,
                        theirs_label=theirs_label,
                    ),
                    details=_write_read_details(
                        "ours",
                        "theirs",
                        is_cascade=has_cascade,
                    ),
                )
            )
        if (
            current_branch is None or current_branch == "theirs"
        ) and _transaction_write_read_overlap(
            theirs_metadata,
            ours_metadata,
        ):
            has_cascade = _transaction_has_cascade_effects(
                theirs_metadata,
            ) or _transaction_has_cascade_effects(ours_metadata)
            result.add_conflicts(
                StatementConflict(
                    kind="write_read",
                    message=_write_read_message(
                        "theirs",
                        "ours",
                        ours_label=ours_label,
                        theirs_label=theirs_label,
                    ),
                    details=_write_read_details(
                        "theirs",
                        "ours",
                        is_cascade=has_cascade,
                    ),
                )
            )
    return result


def _write_read_message(
    writer: BranchName,
    reader: BranchName,
    *,
    ours_label: str | None,
    theirs_label: str | None,
) -> str:
    """Return a write/read message, using transaction labels when available."""

    return (
        f"{_branch_display_label(writer, ours_label, theirs_label)} writes columns "
        f"read by {_branch_display_label(reader, ours_label, theirs_label)}"
    )


def _branch_display_label(
    branch: BranchName,
    ours_label: str | None,
    theirs_label: str | None,
) -> str:
    """Return a display label for a branch in static conflict messages."""

    if branch == "ours":
        return ours_label or "local transaction"
    return theirs_label or "remote transaction"


def _cascade_conflict_details(
    has_cascade: bool,
) -> tuple[tuple[str, str], ...]:
    """Return static-conflict details for cascade-derived metadata."""

    return (CASCADE_CONFLICT_DETAIL,) if has_cascade else ()


def write_write_candidate_pairs(
    context: ConflictCheckContext,
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
    context: ConflictCheckContext,
    writer_metadata: TransactionMetadata,
    reader_metadata: TransactionMetadata,
) -> tuple[int, ...]:
    """Return reader statement indexes that may read writer output."""

    written_columns = _transaction_updated_columns(writer_metadata)
    return tuple(
        index
        for index, reader_statement in enumerate(reader_metadata.statements)
        if _metadata_reads_any(
            reader_statement,
            written_columns,
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


def _transaction_implicit_insert_key_conflicts(
    context: ConflictCheckContext,
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
) -> tuple[StatementConflict, ...]:
    """Return implicit-key conflicts across all statement pairs."""

    return tuple(
        conflict
        for ours_statement in ours_metadata.statements
        for theirs_statement in theirs_metadata.statements
        for conflict in _implicit_insert_key_conflicts(
            context,
            ours_statement,
            theirs_statement,
        )
    )


def _transaction_updated_columns(
    metadata: TransactionMetadata,
) -> dict[str, set[str]]:
    """Return explicit plus cascade-hidden transaction writes."""

    return metadata.tables_updated_to_columns_updated


def _transaction_referenced_columns(
    metadata: TransactionMetadata,
) -> dict[str, set[str]]:
    """Return explicit plus cascade-hidden transaction reads."""

    return metadata.tables_referenced_to_columns_referenced


def _statement_updated_columns(
    metadata: StatementMetadata,
) -> dict[str, set[str]]:
    """Return explicit plus cascade-hidden writes for one statement."""

    return metadata.tables_updated_to_columns_updated


def _transaction_has_cascade_effects(
    metadata: TransactionMetadata,
) -> bool:
    """Return whether any statement has cascade-hidden reads or writes."""

    return metadata.has_cascade_effects


def _statement_has_cascade_effects(
    metadata: StatementMetadata,
) -> bool:
    """Return whether one statement has cascade-hidden reads or writes."""

    return metadata.has_cascade_effects


def _transaction_write_write_overlap(
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
) -> bool:
    """Return whether the two transactions may write the same table columns."""

    return any(
        _metadata_write_write_overlap(ours_statement, theirs_statement)
        for ours_statement in ours_metadata.statements
        for theirs_statement in theirs_metadata.statements
    )


def _transaction_write_read_overlap(
    writer_metadata: TransactionMetadata,
    reader_metadata: TransactionMetadata,
) -> bool:
    """Return whether any reader statement may read writer transaction output."""

    return _columns_by_table_overlap(
        _transaction_updated_columns(writer_metadata),
        _transaction_referenced_columns(reader_metadata),
    )


def _columns_by_table_overlap(
    left: dict[str, set[str]],
    right: dict[str, set[str]],
) -> bool:
    """Return whether two table-to-column maps overlap."""

    for table, left_columns in left.items():
        right_columns = right.get(table)
        if right_columns is not None and column_overlap(left_columns, right_columns):
            return True
    return False


def _metadata_write_write_overlap(
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> bool:
    """Return whether two statements may write overlapping table columns."""

    if _direct_write_write_overlap(ours_metadata, theirs_metadata):
        return True

    if not (
        _statement_has_cascade_effects(ours_metadata)
        or _statement_has_cascade_effects(theirs_metadata)
    ):
        return False

    return _columns_by_table_overlap(
        _statement_updated_columns(ours_metadata),
        _statement_updated_columns(theirs_metadata),
    )


def _metadata_reads_any(
    metadata: StatementMetadata,
    columns_by_table: dict[str, set[str]],
) -> bool:
    """Return whether metadata reads any provided table columns."""

    return _columns_by_table_overlap(
        metadata.tables_referenced_to_columns_referenced,
        columns_by_table,
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
            if transaction_metadata_may_create_or_change_key(
                first_metadata,
                table,
                key_set,
            ) and transaction_metadata_may_create_or_change_key(
                second_metadata,
                table,
                key_set,
            ):
                return True
    return False


def transaction_metadata_may_create_or_change_key(
    metadata: TransactionMetadata,
    table: str,
    key_columns: set[str],
) -> bool:
    """Return whether a transaction may insert/update a key value."""

    return any(
        _statement_may_create_or_change_key(statement, table, key_columns)
        for statement in metadata.statements
    ) or any(
        cascade_event_may_create_or_change_key(event, table, key_columns)
        for event in metadata.cascade_write_events
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

    for table in first_metadata.tables_updated_to_columns_updated:
        for edge in foreign_key_edges_by_parent(context).get(table, ()):
            if transaction_metadata_may_remove_or_change_key(
                first_metadata,
                edge.parent_table,
                set(edge.parent_columns),
            ) and transaction_metadata_may_create_or_change_key(
                second_metadata,
                edge.child_table,
                set(edge.child_columns),
            ):
                return True

        for edge in foreign_key_edges_by_child(context).get(table, ()):
            if transaction_metadata_may_create_or_change_key(
                first_metadata,
                edge.child_table,
                set(edge.child_columns),
            ) and transaction_metadata_may_remove_or_change_key(
                second_metadata,
                edge.parent_table,
                set(edge.parent_columns),
            ):
                return True

    return False


def transaction_metadata_may_remove_or_change_key(
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
    ) or any(
        cascade_event_may_remove_or_change_key(event, table, key_columns)
        for event in metadata.cascade_write_events
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


def _direct_write_write_overlap(
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> bool:
    """Return whether explicit writes may update/delete overlapping rows."""

    if not _same_written_table(ours_metadata, theirs_metadata):
        return False
    table = required_updated_table(ours_metadata)

    if is_delete_statement(ours_metadata) and is_delete_statement(theirs_metadata):
        return False

    if is_insert_statement(ours_metadata) or is_insert_statement(theirs_metadata):
        return False

    ours_columns = _written_columns(ours_metadata)
    return bool(
        _metadata_write_overlap(
            theirs_metadata,
            table,
            ours_columns,
        )
    )


def _write_read_details(
    writer_label: str,
    reader_label: str,
    *,
    is_cascade: bool = False,
) -> tuple[tuple[str, str], ...]:
    """Return structured direction data for execution-based refinement."""

    details = [
        ("writer", writer_label),
        ("reader", reader_label),
    ]
    if is_cascade:
        details.append(CASCADE_CONFLICT_DETAIL)
    return tuple(details)


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
        table = required_updated_table(insert)

        other_omitted_key = _omitted_integer_primary_key_column(context, other)
        if other_omitted_key is not None:
            conflicts.append(
                _implicit_insert_key_conflict(
                    insert_label,
                    other_label,
                    table,
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
    table = required_updated_table(insert)

    written_columns = _metadata_write_overlap(
        other,
        table,
        {omitted_key},
    )
    if not written_columns:
        return None

    return StatementConflict(
        kind="implicit_insert_key",
        message=(
            f"{insert_label} INSERT omits {table}.{omitted_key}; "
            f"{other_label} UPDATE assigns to the same key column"
        ),
    )


def _implicit_insert_key_conflict(
    insert_label: str,
    other_label: str,
    table: str,
    key_column: str,
) -> StatementConflict:
    """Return implicit-key conflict for two omitted INTEGER PRIMARY KEY inserts."""

    return StatementConflict(
        kind="implicit_insert_key",
        message=(
            f"{insert_label} INSERT omits {table}.{key_column}; "
            f"{other_label} INSERT also omits the same key"
        ),
    )


def _omitted_integer_primary_key_column(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> str | None:
    """Return omitted INTEGER PRIMARY KEY column for replay-sensitive INSERTs."""

    if not is_insert_statement(metadata):
        return None
    table = required_updated_table(metadata)

    key_column = _integer_primary_key_column(context, table)
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

    integer_pk_cache = context.schema_cache.integer_primary_key_columns
    if table in integer_pk_cache:
        return integer_pk_cache[table]

    key_column = integer_primary_key_column(context.base_cursor, table)
    integer_pk_cache[table] = key_column
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

    if metadata.statement_kind == "insert":
        required_updated_table(metadata)
        return {ALL_COLUMNS}

    return set()


def _metadata_write_overlap(
    metadata: StatementMetadata,
    table: str,
    columns: set[str],
) -> set[str]:
    """Return columns metadata writes on table."""

    if metadata.table_updated != table:
        return set()

    return column_overlap(metadata.columns_updated, columns)


def _metadata_read_overlap(
    metadata: StatementMetadata,
    table: str,
    columns: set[str],
) -> set[str]:
    """Return columns metadata reads on table."""

    references = metadata.tables_referenced_to_columns_referenced
    return column_overlap(references.get(table, set()), columns)


def _written_columns(
    metadata: StatementMetadata,
) -> set[str]:
    """Return columns written by a statement, with INSERT/DELETE as all columns."""

    required_updated_table(metadata)
    return metadata.columns_updated


def _same_written_table(
    left: StatementMetadata,
    right: StatementMetadata,
) -> bool:
    """Return whether both statements write the same real table."""

    return left.table_updated is not None and left.table_updated == right.table_updated
