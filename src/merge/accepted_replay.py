from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import replace

from sqlite_conflict_resolution import strict_conflict_resolution_rewrite

from .control_db import clean_control_schema_references
from .execution_based_analysis import (
    SQLiteReplayFailure,
    _constraint_resolution_conflict,
    _foreign_key_check_error,
    _sqlite_error_conflict_kind,
)
from .models import (
    ConflictCheckContext,
    ConflictCheckResult,
    ConflictScope,
    LoggedStatement,
    LoggedTransaction,
    StatementConflict,
    transaction_label,
    statement_label,
)
from .sql_metadata import parse_statement_metadata_for_context
from .utils import quote_identifier, rollback_savepoint


def conflict_resolution_branch_warning(
    context: ConflictCheckContext,
    statement: LoggedStatement,
) -> str | None:
    """Return a warning when conflict-resolution syntax is already active."""

    strict_statement = _strict_conflict_resolution_statement(context, statement)
    if strict_statement is None:
        return None

    statement_without_resolution, label = strict_statement
    failure = _savepoint_statement_failure(
        context,
        statement_without_resolution,
        use_control=False,
        scope=statement.branch,
        order_label=f"strict {statement_label(statement)}",
    )
    if failure is None or failure.kind != "integrity":
        return None
    return _conflict_resolution_already_active_warning(
        label,
        failure.message,
    )


def apply_accepted_transaction(
    context: ConflictCheckContext,
    transaction: LoggedTransaction,
) -> StatementConflict | None:
    """Apply one transaction accepted by the fixed replay order."""

    cursor = context.base_cursor
    savepoint = quote_identifier(f"sqlite_merge_accept_{uuid.uuid4().hex}")
    cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        replay_result = _advance_transaction_on_main_and_control(
            context,
            transaction,
            transaction,
            order_label=transaction_label(transaction),
            scope=transaction.branch,
            check_constraint_resolution=False,
        )
        if replay_result.has_conflict:
            rollback_savepoint(cursor, savepoint)
            return _clean_control_conflict(context, replay_result.conflicts[0])

        _append_replayed_log(cursor.connection, transaction.statements)
        cursor.execute(f"RELEASE {savepoint}")
        return None
    except sqlite3.Error as exc:
        rollback_savepoint(cursor, savepoint)
        return _sqlite_exception_conflict(exc, transaction.branch)


def _sqlite_exception_conflict(
    error: sqlite3.Error,
    scope: ConflictScope,
) -> StatementConflict:
    """Return a scoped replay failure conflict from a SQLite exception."""

    return StatementConflict(
        kind="integrity" if isinstance(error, sqlite3.IntegrityError) else "replay_error",
        message=str(error),
        scope=scope,
    )


def _statement_conflict_details(
    statement: LoggedStatement,
) -> tuple[tuple[str, str], ...]:
    """Return conflict metadata that lets the UI find the failing statement."""

    return (("statement_branch_index", str(statement.branch_index)),)


def _advance_transaction_on_main_and_control(
    context: ConflictCheckContext,
    current_transaction: LoggedTransaction,
    transaction: LoggedTransaction,
    *,
    order_label: str,
    scope: ConflictScope,
    check_constraint_resolution: bool,
) -> ConflictCheckResult:
    """Replay one transaction on main and control, returning replay conflicts."""

    for statement in transaction.statements:
        strict_failure = None
        if check_constraint_resolution:
            strict_failure = _strict_constraint_resolution_failure(
                context,
                current_transaction,
                statement,
            )

        failure = _execute_statement_on_control(
            context,
            statement,
            scope=scope,
            order_label=order_label,
            check_deferred_foreign_keys=False,
        )
        if failure is not None:
            return ConflictCheckResult((_replay_failure_conflict(failure),))
        failure = _execute_statement_on_main(
            context,
            statement,
            scope=scope,
            order_label=order_label,
            check_deferred_foreign_keys=False,
        )
        if failure is not None:
            return ConflictCheckResult((_replay_failure_conflict(failure),))

        if strict_failure is not None:
            return ConflictCheckResult((
                _constraint_resolution_conflict(
                    transaction,
                    strict_failure,
                    scope="pair",
                    after_transaction=current_transaction,
                ),
            ))

    for failure in _transaction_foreign_key_failures(
        context,
        transaction,
        scope=scope,
        order_label=order_label,
    ):
        return ConflictCheckResult((_replay_failure_conflict(failure),))

    return ConflictCheckResult()


def _transaction_foreign_key_failures(
    context: ConflictCheckContext,
    transaction: LoggedTransaction,
    *,
    scope: ConflictScope,
    order_label: str,
) -> tuple[SQLiteReplayFailure, ...]:
    """Return deferred FK failures after replaying a whole transaction."""

    failures: list[SQLiteReplayFailure] = []
    main_error = _foreign_key_check_error(context.base_cursor)
    if main_error is not None:
        failures.append(
            SQLiteReplayFailure(
                kind="integrity",
                scope=scope,
                message=main_error,
                order_label=order_label,
                statement=transaction.statements[-1],
            )
        )

    control_error = _foreign_key_check_error(
        context.base_cursor,
        schema=context.control_schema,
    )
    if control_error is not None:
        failures.append(
            SQLiteReplayFailure(
                kind="integrity",
                scope=scope,
                message=control_error,
                order_label=order_label,
                statement=transaction.statements[-1],
            )
        )
    return tuple(failures)


def _strict_constraint_resolution_failure(
    context: ConflictCheckContext,
    current_transaction: LoggedTransaction,
    statement: LoggedStatement,
) -> SQLiteReplayFailure | None:
    """Return the strict replay failure, if reviewable syntax was needed."""

    strict_statement = _strict_conflict_resolution_statement(context, statement)
    if strict_statement is None:
        return None

    statement_without_resolution, _ = strict_statement
    before_failure = _savepoint_statement_failure(
        context,
        statement_without_resolution,
        use_control=False,
        scope="pair",
        order_label=f"strict {statement_label(statement)} before current",
    )
    after_failure = _savepoint_statement_failure(
        context,
        statement_without_resolution,
        use_control=True,
        scope="pair",
        order_label=(
            f"{transaction_label(current_transaction)} then strict "
            f"{statement_label(statement)}"
        ),
    )
    if after_failure is None:
        return None

    # If strict replay already fails on the comparison state without the current
    # transaction, the OR/REPLACE behavior was active before this pair. Reporting
    # it here would blame an unrelated opposite-branch transaction.
    if before_failure is not None and before_failure.kind == "integrity":
        return None
    return after_failure


def _strict_conflict_resolution_statement(
    context: ConflictCheckContext,
    statement: LoggedStatement,
) -> tuple[LoggedStatement, str] | None:
    """Return stricter SQL for reviewable conflict-resolution syntax."""

    if not statement.metadata.has_reviewable_constraint_resolution:
        return None

    rewrite = strict_conflict_resolution_rewrite(statement.sql_text)
    if rewrite is None:
        return None

    return (
        replace(
            statement,
            to_replay_sql_text=rewrite.sql,
            metadata=parse_statement_metadata_for_context(rewrite.sql, context),
            replay_warnings=(
                *statement.replay_warnings,
                f"strict replay removed {rewrite.label}",
            ),
        ),
        rewrite.label,
    )


def _conflict_resolution_already_active_warning(
    label: str,
    message: str,
) -> str:
    """Return the user-facing branch-local strict replay warning."""

    return (
        "SQLite conflict-resolution syntax is already active here; "
        f"strict replay without {label} fails: {message}"
    )


def _savepoint_statement_failure(
    context: ConflictCheckContext,
    statement: LoggedStatement,
    *,
    use_control: bool,
    scope: ConflictScope,
    order_label: str,
) -> SQLiteReplayFailure | None:
    """Replay one strict statement in a nested savepoint and discard effects."""

    savepoint = quote_identifier(
        f"sqlite_merge_remaining_stmt_probe_{uuid.uuid4().hex}"
    )
    context.base_cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        execute = (
            _execute_statement_on_control
            if use_control
            else _execute_statement_on_main
        )
        return execute(
            context,
            statement,
            scope=scope,
            order_label=order_label,
        )
    finally:
        rollback_savepoint(context.base_cursor, savepoint)


def _execute_transaction_on_control(
    context: ConflictCheckContext,
    transaction: LoggedTransaction,
    *,
    scope: ConflictScope,
    order_label: str,
) -> SQLiteReplayFailure | None:
    """Replay a transaction against the attached control database."""

    for statement in transaction.statements:
        failure = _execute_statement_on_control(
            context,
            statement,
            scope=scope,
            order_label=order_label,
            check_deferred_foreign_keys=False,
        )
        if failure is not None:
            return failure

    foreign_key_error = _foreign_key_check_error(
        context.base_cursor,
        schema=context.control_schema,
    )
    if foreign_key_error is None:
        return None
    return SQLiteReplayFailure(
        kind="integrity",
        scope=scope,
        message=foreign_key_error,
        order_label=order_label,
        statement=transaction.statements[-1],
    )


def _execute_statement_on_main(
    context: ConflictCheckContext,
    statement: LoggedStatement,
    *,
    scope: ConflictScope,
    order_label: str,
    check_deferred_foreign_keys: bool = True,
) -> SQLiteReplayFailure | None:
    """Replay one statement against the main working database."""

    return _execute_sql_for_replay(
        context.base_cursor,
        statement,
        statement.sql_text,
        scope=scope,
        order_label=order_label,
        foreign_key_schema=None,
        check_deferred_foreign_keys=check_deferred_foreign_keys,
    )


def _execute_statement_on_control(
    context: ConflictCheckContext,
    statement: LoggedStatement,
    *,
    scope: ConflictScope,
    order_label: str,
    check_deferred_foreign_keys: bool = True,
) -> SQLiteReplayFailure | None:
    """Replay one statement against the attached control database."""

    control_sql = _control_sql_for(context, statement)
    if control_sql is None:
        return SQLiteReplayFailure(
            kind="replay_error",
            scope=scope,
            message="statement cannot be rewritten for control database",
            order_label=order_label,
            statement=statement,
        )
    failure = _execute_sql_for_replay(
        context.base_cursor,
        statement,
        control_sql,
        scope=scope,
        order_label=order_label,
        foreign_key_schema=context.control_schema,
        check_deferred_foreign_keys=check_deferred_foreign_keys,
    )
    return _clean_control_failure(context, failure)


def _execute_sql_for_replay(
    cursor: sqlite3.Cursor,
    statement: LoggedStatement,
    sql_text: str,
    *,
    scope: ConflictScope,
    order_label: str,
    foreign_key_schema: str | None,
    check_deferred_foreign_keys: bool,
) -> SQLiteReplayFailure | None:
    """Execute SQL and convert SQLite failures into replay failure objects."""

    try:
        cursor.execute(sql_text)
    except sqlite3.Error as exc:
        return SQLiteReplayFailure(
            kind=_sqlite_error_conflict_kind(exc),
            scope=scope,
            message=str(exc),
            order_label=order_label,
            statement=statement,
        )

    if not check_deferred_foreign_keys:
        return None

    deferred_error = _foreign_key_check_error(cursor, schema=foreign_key_schema)
    if deferred_error is not None:
        return SQLiteReplayFailure(
            kind="integrity",
            scope=scope,
            message=deferred_error,
            order_label=order_label,
            statement=statement,
        )
    return None


def _control_sql_for(
    context: ConflictCheckContext,
    sql: str | LoggedStatement,
) -> str | None:
    """Return SQL rewritten to use the attached control database."""

    if context.control_sql_rewriter is None:
        return None
    return context.control_sql_rewriter(sql)


def _clean_control_conflict(
    context: ConflictCheckContext,
    conflict: StatementConflict,
) -> StatementConflict:
    """Hide attached-control schema names in accepted-replay errors."""

    message = clean_control_schema_references(conflict.message, context.control_schema)
    if message == conflict.message:
        return conflict
    return replace(conflict, message=message)


def _clean_control_failure(
    context: ConflictCheckContext,
    failure: SQLiteReplayFailure | None,
) -> SQLiteReplayFailure | None:
    """Hide attached-control schema names in user-facing replay errors."""

    if failure is None or context.control_schema is None:
        return failure

    message = clean_control_schema_references(
        failure.message,
        context.control_schema,
    )
    if message == failure.message:
        return failure
    return replace(failure, message=message)


def _replay_failure_conflict(
    failure: SQLiteReplayFailure,
) -> StatementConflict:
    """Convert a replay failure into a UI conflict."""

    details = ()
    if failure.statement is not None:
        details = _statement_conflict_details(failure.statement)
    return StatementConflict(
        kind=failure.kind,
        scope=failure.scope,
        message=f"{failure.order_label}: {failure.message}",
        details=details,
    )


def _append_replayed_log(
    con: sqlite3.Connection,
    statements: Sequence[LoggedStatement],
) -> None:
    """Record one accepted transaction group in the working merge log."""

    from .log_merge import LOG_TABLE, TX_TABLE

    cursor = con.execute(
        f"INSERT INTO {TX_TABLE} DEFAULT VALUES",
    )
    con.executemany(
        f"""
        INSERT INTO {LOG_TABLE} (
            transaction_id,
            original_sql_text,
            to_replay_sql_text,
            is_replay_safe,
            replay_block_reason
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                cursor.lastrowid,
                statement.original_sql_text,
                statement.to_replay_sql_text,
                int(statement.is_replay_safe),
                statement.replay_block_reason,
            )
            for statement in statements
        ],
    )
