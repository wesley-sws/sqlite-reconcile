from __future__ import annotations

from sqlglot import expressions as exp

from .log_merge import (
    ConflictCheckContext,
    ConflictCheckResult,
    StatementConflict,
)
from .sql_metadata import StatementMetadata, TransactionMetadata
from .utils import (
    ALL_COLUMNS,
    is_delete_statement,
    is_insert_statement,
    key_column_sets as schema_key_column_sets,
)


def static_analysis_matching(
    context: ConflictCheckContext,
    ours_metadata: TransactionMetadata,
    theirs_metadata: TransactionMetadata,
) -> ConflictCheckResult:
    """Return table/column based conflicts between two transactions."""

    if len(ours_metadata.statements) == 1 and len(theirs_metadata.statements) == 1:
        return _match_statement_metadata(
            context,
            ours_metadata.statements[0],
            theirs_metadata.statements[0],
        )

    result = ConflictCheckResult()
    if _transaction_has_implicit_insert_key_conflict(
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

    if _transaction_write_write_overlap(ours_metadata, theirs_metadata):
        result.add_conflicts(
            StatementConflict(
                kind="write_write",
                message="transactions write overlapping table columns",
            )
        )

    for writer_label, writer, reader_label, reader in (
        ("ours", ours_metadata, "theirs", theirs_metadata),
        ("theirs", theirs_metadata, "ours", ours_metadata),
    ):
        if _transaction_write_read_overlap(writer, reader):
            result.add_conflicts(
                StatementConflict(
                    kind="write_read",
                    message=(
                        f"{writer_label} transaction writes columns read by "
                        f"{reader_label} transaction"
                    ),
                    details=_write_read_details(
                        writer_label,
                        reader_label,
                    ),
                )
            )
    return result


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


def _match_statement_metadata(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> ConflictCheckResult:
    """Compare metadata in both write/write and write/read directions."""

    conflicts: list[StatementConflict] = []
    conflicts.extend(
        _implicit_insert_key_conflicts(context, ours_metadata, theirs_metadata)
    )

    write_write = _write_write_conflict(ours_metadata, theirs_metadata)
    if write_write is not None:
        conflicts.append(write_write)

    conflicts.extend(
        _write_read_conflicts(
            writer_label="ours",
            writer=ours_metadata,
            reader_label="theirs",
            reader=theirs_metadata,
        )
    )
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
    """Return conflicts caused by INSERTs omitting explicit PK/UK values."""

    conflicts: list[StatementConflict] = []
    for insert_label, insert, other_label, other in (
        ("ours", ours_metadata, "theirs", theirs_metadata),
        ("theirs", theirs_metadata, "ours", ours_metadata),
    ):
        if (
            not _is_insert_without_explicit_key(context, insert)
            or not _same_written_table(insert, other)
        ):
            continue

        if is_insert_statement(other):
            conflicts.append(
                _implicit_insert_key_conflict(
                    insert_label,
                    other_label,
                    insert.table_updated,
                )
            )
            break

        conflicts.extend(
            _implicit_insert_key_dml_conflicts(
                context,
                insert_label=insert_label,
                insert=insert,
                other_label=other_label,
                other=other,
            )
        )
    return conflicts


def _implicit_insert_key_dml_conflicts(
    context: ConflictCheckContext,
    insert_label: str,
    insert: StatementMetadata,
    other_label: str,
    other: StatementMetadata,
) -> list[StatementConflict]:
    """Return implicit-key conflicts between an INSERT and UPDATE/DELETE."""

    if (
        not _same_written_table(insert, other)
        or is_insert_statement(other)
    ):
        return []

    omitted_key_columns = _omitted_insert_key_columns(context, insert)
    touched_columns = _metadata_touch_overlap(
        other,
        insert.table_updated,
        omitted_key_columns,
    )
    if not touched_columns:
        return []

    return [
        StatementConflict(
            kind="implicit_insert_key",
            message=(
                f"{insert_label} INSERT omits explicit key values on "
                f"{insert.table_updated}; {other_label} references or writes "
                f"{_format_columns(touched_columns)}"
            ),
        )
    ]


def _implicit_insert_key_conflict(
    insert_label: str,
    other_label: str,
    table: str | None,
) -> StatementConflict:
    """Build a conflict for an implicit-key INSERT against another INSERT."""

    return StatementConflict(
        kind="implicit_insert_key",
        message=(
            f"{insert_label} INSERT omits explicit key values on {table}; "
            f"{other_label} also inserts into {table}"
        ),
    )


def _is_insert_without_explicit_key(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> bool:
    """Return whether an INSERT lacks a complete explicit PK or unique key."""

    return bool(_omitted_insert_key_columns(context, metadata))


def _omitted_insert_key_columns(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> set[str]:
    """Return omitted key columns when an INSERT has no complete key."""

    if not is_insert_statement(metadata) or metadata.table_updated is None:
        return set()

    key_sets = _key_column_sets(context, metadata.table_updated)
    if not key_sets:
        return set()

    explicit_columns = _insert_explicit_columns(context, metadata)
    if ALL_COLUMNS in explicit_columns:
        return set()

    omitted_columns: set[str] = set()
    for key_set in key_sets:
        if not key_set <= explicit_columns:
            omitted_columns.update(key_set - explicit_columns)
    return omitted_columns


def _key_column_sets(
    context: ConflictCheckContext,
    table: str | None,
) -> tuple[set[str], ...]:
    """Return cached PK/unique-key column sets, falling back to schema lookup."""

    if table is None:
        return ()

    key_sets = context.key_column_sets.get(table)
    if key_sets is not None:
        return key_sets

    return schema_key_column_sets(context.base_cursor, table)


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


def _metadata_touch_overlap(
    metadata: StatementMetadata,
    table: str | None,
    columns: set[str],
) -> set[str]:
    """Return columns metadata writes or reads on table."""

    return (
        _metadata_write_overlap(metadata, table, columns)
        | _metadata_read_overlap(metadata, table, columns)
    )


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
