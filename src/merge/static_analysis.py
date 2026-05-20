from __future__ import annotations

from sqlglot import expressions as exp

from .log_merge import (
    ConflictCheckContext,
    ConflictCheckResult,
    LoggedStatement,
    StatementConflict,
)
from .statement_metadata import ALL_COLUMNS, UNQUALIFIED_TABLE, StatementMetadata
from .utils import (
    is_delete_statement,
    is_insert_statement,
    key_columns as schema_key_columns,
    key_column_sets as schema_key_column_sets,
    nondeterministic_default_columns,
)


def static_analysis_matching(
    context: ConflictCheckContext,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Return table/column based conflicts between two logged statements."""

    return _match_metadata(
        context,
        ours_statement.metadata,
        theirs_statement.metadata,
    )

def _with_unsafe_replay_conflicts(
    result: ConflictCheckResult,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Append blocking conflicts for statements unsafe for automatic replay."""

    conflicts = list(result.conflicts)
    for label, statement in (
        ("ours", ours_statement),
        ("theirs", theirs_statement),
    ):
        if statement.is_replay_safe:
            continue

        conflicts.append(
            StatementConflict(
                kind="unsafe_replay",
                message=f"{label} statement is unsafe for replay: "
                f"{statement.replay_block_reason or 'reason not recorded'}",
            )
        )

    if len(conflicts) == len(result.conflicts):
        return result

    return ConflictCheckResult(tuple(conflicts))


def _match_metadata(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> ConflictCheckResult:
    """Compare metadata in both write/write and write/read directions."""

    conflicts: list[StatementConflict] = []
    conflicts.extend(
        _implicit_insert_default_conflicts(context, ours_metadata, theirs_metadata)
    )
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
    return ConflictCheckResult(tuple(conflicts))


def _implicit_insert_default_conflicts(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> list[StatementConflict]:
    """Return conflicts for INSERTs that rely on nondeterministic defaults."""

    conflicts: list[StatementConflict] = []
    for label, metadata in (
        ("ours", ours_metadata),
        ("theirs", theirs_metadata),
    ):
        columns = _omitted_nondeterministic_default_columns(context, metadata)
        if not columns:
            continue

        conflicts.append(
            StatementConflict(
                kind="implicit_insert_default",
                message=(
                    f"{label} INSERT omits nondeterministic default columns on "
                    f"{metadata.table_updated}: {_format_columns(columns)}"
                ),
            )
        )

    return conflicts


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

    conflicts: list[StatementConflict] = []
    qualified_overlap, unqualified_overlap = _metadata_read_overlaps(
        reader,
        table,
        written_columns,
    )
    if qualified_overlap:
        conflicts.append(
            StatementConflict(
                kind="write_read",
                message=(
                    f"{writer_label} writes "
                    f"{table}.{_format_columns(qualified_overlap)}; "
                    f"{reader_label} reads it"
                ),
            )
        )

    if unqualified_overlap:
        conflicts.append(
            StatementConflict(
                kind="write_read",
                message=(
                    f"{writer_label} writes {_format_columns(unqualified_overlap)} "
                    f"on {table}; {reader_label} has unresolved reads of the same "
                    "columns"
                ),
            )
        )

    return conflicts


def _implicit_insert_key_conflicts(
    context: ConflictCheckContext,
    ours_metadata: StatementMetadata,
    theirs_metadata: StatementMetadata,
) -> list[StatementConflict]:
    """Return conflicts caused by INSERTs omitting explicit PK/UK values."""

    conflicts: list[StatementConflict] = []
    if (
        _is_insert_without_explicit_key(context, ours_metadata)
        and _same_written_table(ours_metadata, theirs_metadata)
        and is_insert_statement(theirs_metadata)
    ):
        conflicts.append(
            _implicit_insert_key_conflict("ours", "theirs", ours_metadata.table_updated)
        )
    elif (
        _is_insert_without_explicit_key(context, theirs_metadata)
        and _same_written_table(ours_metadata, theirs_metadata)
        and is_insert_statement(ours_metadata)
    ):
        conflicts.append(
            _implicit_insert_key_conflict("theirs", "ours", theirs_metadata.table_updated)
        )

    conflicts.extend(
        _implicit_insert_key_dml_conflicts(
            context,
            insert_label="ours",
            insert=ours_metadata,
            other_label="theirs",
            other=theirs_metadata,
        )
    )
    conflicts.extend(
        _implicit_insert_key_dml_conflicts(
            context,
            insert_label="theirs",
            insert=theirs_metadata,
            other_label="ours",
            other=ours_metadata,
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

    key_sets = schema_key_column_sets(context.base_cursor, metadata.table_updated)
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


def _omitted_nondeterministic_default_columns(
    context: ConflictCheckContext,
    metadata: StatementMetadata,
) -> set[str]:
    """Return nondeterministic default columns omitted by an INSERT."""

    if not is_insert_statement(metadata) or metadata.table_updated is None:
        return set()

    default_columns = nondeterministic_default_columns(
        context.base_cursor,
        metadata.table_updated,
    )
    if not default_columns:
        return set()

    explicit_columns = _insert_explicit_columns(context, metadata)
    if ALL_COLUMNS in explicit_columns:
        return set()

    return default_columns - explicit_columns


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
        | set().union(*_metadata_read_overlaps(metadata, table, columns))
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


def _metadata_read_overlaps(
    metadata: StatementMetadata,
    table: str | None,
    columns: set[str],
) -> tuple[set[str], set[str]]:
    """Return qualified and unresolved read overlaps for columns."""

    references = metadata.tables_referenced_to_columns_referenced
    return (
        _column_overlap(references.get(table, set()), columns),
        _column_overlap(references.get(UNQUALIFIED_TABLE, set()), columns),
    )


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
