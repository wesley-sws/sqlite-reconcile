from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from .execution_based_analysis import update_from_has_duplicate_target_rows
from .log_merge import (
    BranchName,
    ConflictCheckContext,
    ConflictPair,
    LoggedStatement,
    LoggedTransaction,
    UPDATE_FROM_DUPLICATE_TARGET_WARNING,
    acknowledgeable_replay_warning,
    build_merge_plan_from_connection,
    group_logged_transactions,
    load_schema_metadata_from_db,
    make_logged_statement,
    replay_transaction_plan,
    validate_database,
)
from .models import statement_label, transaction_label
from .session import read_merge_session
from .terminal_ui import (
    STANDALONE_RESOLUTION_SCOPE,
    _prompt_pair_resolution,
    _prompt_replay_warning,
    _prompt_replacement,
    _prompt_standalone_resolution,
    _prompt_update_from_warning,
    _transaction_with_statements,
)

DEBUG_MERGETOOL_TRACE = True


def _session_logged_statements(
    transactions: list[dict[str, object]],
    branch: BranchName,
    table_columns,
) -> list[LoggedStatement]:
    """Rebuild logged statements from the compact session JSON."""

    statements: list[LoggedStatement] = []
    for transaction in transactions:
        statement_payloads = transaction.get("statements", [])
        if not isinstance(statement_payloads, list):
            continue

        committed_at = str(transaction["committed_at"])
        for payload in statement_payloads:
            if not isinstance(payload, dict):
                continue

            to_replay_sql_text = str(payload["to_replay_sql_text"])
            statements.append(
                make_logged_statement(
                    branch=branch,
                    branch_index=int(payload["branch_index"]),
                    log_id=int(payload["log_id"]),
                    transaction_id=int(payload["transaction_id"]),
                    committed_at=committed_at,
                    sql_text=to_replay_sql_text,
                    original_sql_text=str(payload["original_sql_text"]),
                    is_replay_safe=bool(payload["is_replay_safe"]),
                    replay_block_reason=(
                        None
                        if payload.get("replay_block_reason") is None
                        else str(payload["replay_block_reason"])
                    ),
                    replay_warnings=tuple(
                        str(warning)
                        for warning in payload.get("replay_warnings", ())
                    ),
                    table_columns=table_columns,
                ),
            )

    return statements


def _apply_to_planning_db(
    con: sqlite3.Connection,
    transactions: Sequence[LoggedTransaction],
) -> str | None:
    """Apply transactions to the mutable planning database, returning an error."""

    try:
        for transaction in transactions:
            if DEBUG_MERGETOOL_TRACE:
                print(
                    "[mergetool-debug] applying "
                    f"{transaction_label(transaction)} "
                    f"({transaction.branch} tx {transaction.branch_index + 1})"
                )
            for statement in transaction.statements:
                if not statement.is_replay_safe:
                    con.rollback()
                    return (
                        statement.replay_block_reason
                        or "statement is unsafe for automatic replay"
                    )
                con.execute(statement.sql_text)
            errors = validate_database(con)
            if errors:
                con.rollback()
                return "\n".join(errors)
            if DEBUG_MERGETOOL_TRACE:
                print(
                    "[mergetool-debug] applied "
                    f"{transaction_label(transaction)}"
                )
    except sqlite3.Error as exc:
        con.rollback()
        return str(exc)

    con.commit()
    return None


def _load_planning_db(base_path: Path) -> sqlite3.Connection:
    """Return an open in-memory copy of base; the caller must close it."""

    planning_conn = sqlite3.connect(":memory:")
    try:
        # Only this source connection is closed here. The returned planning
        # connection stays open so callers can mutate it across a check loop.
        with closing(sqlite3.connect(base_path)) as base_conn:
            base_conn.backup(planning_conn)
        planning_conn.row_factory = sqlite3.Row
        planning_conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        planning_conn.close()
        raise
    return planning_conn


def _branch_statement_replay_error(
    con: sqlite3.Connection,
    statement: LoggedStatement,
) -> str | None:
    """Apply one branch-local statement, returning why it cannot replay."""

    if not statement.is_replay_safe:
        return statement.replay_block_reason or "statement is unsafe for replay"

    try:
        con.execute(statement.sql_text)
        errors = validate_database(con)
        if errors:
            con.rollback()
            return "\n".join(errors)
        con.commit()
        return None
    except sqlite3.Error as exc:
        con.rollback()
        return str(exc)


def _statement_replay_warning(statement: LoggedStatement) -> str | None:
    """Return a stored/unsafe replay warning that needs user acknowledgement."""

    warning = acknowledgeable_replay_warning(statement)
    if warning is not None:
        return warning

    for warning in statement.replay_warnings:
        if warning != UPDATE_FROM_DUPLICATE_TARGET_WARNING:
            return warning
    return None


def _resolve_branch_statement(
    con: sqlite3.Connection,
    update_from_context: ConflictCheckContext,
    branch_label: str,
    statement: LoggedStatement,
    table_columns,
) -> LoggedStatement | None:
    """Resolve, run, and optionally retry one branch-local statement."""

    current = statement
    while True:
        replay_warning = _statement_replay_warning(current)
        if replay_warning is not None:
            replacement, changed = _prompt_replay_warning(
                current,
                table_columns,
                replay_warning,
            )
            if replacement is None:
                return None
            current = replacement
            if changed:
                continue

        if current.is_replay_safe and update_from_has_duplicate_target_rows(
            update_from_context,
            current.metadata,
        ):
            replacement = _prompt_update_from_warning(
                current,
                table_columns,
                UPDATE_FROM_DUPLICATE_TARGET_WARNING,
            )
            if replacement is None:
                return None
            if replacement is not current:
                current = replacement
                continue

        error = _branch_statement_replay_error(con, current)
        if error is None:
            return current

        print(
            f"\nChecking {branch_label} branch stopped at "
            f"{statement_label(current)}."
        )
        print(f"{statement_label(current)} cannot be replayed: {error}")
        replacement = _prompt_replacement(
            current,
            table_columns,
        )
        if replacement is None:
            return None
        current = replacement


def _resolve_branch_replay_safety(
    base_path: Path,
    branch: BranchName,
    statements: list[LoggedStatement],
    table_columns,
    primary_key_columns,
    key_column_sets,
) -> list[LoggedStatement]:
    """Resolve wrapper-unsafe or branch-local replay failures before pair checks."""

    branch_label = "local" if branch == "ours" else "remote"
    resolved: list[LoggedStatement] = []
    with closing(_load_planning_db(base_path)) as branch_conn:
        update_from_context = ConflictCheckContext(
            base_cursor=branch_conn.cursor(),
            base_db_path=":memory:",
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        for statement in statements:
            replayed = _resolve_branch_statement(
                branch_conn,
                update_from_context,
                branch_label,
                statement,
                table_columns,
            )
            if replayed is not None:
                resolved.append(replayed)

    return resolved


def _replace_remaining_standalone(
    conflict: ConflictPair,
    scope: BranchName,
    replacement: Sequence[LoggedStatement] | None,
    ours: list[LoggedTransaction],
    theirs: list[LoggedTransaction],
    frontier_ours_count: int,
    frontier_theirs_count: int,
) -> tuple[list[LoggedTransaction], list[LoggedTransaction]]:
    """Remove accepted prefix and replace/delete one standalone transaction."""

    if scope == "ours":
        next_ours = list(ours[conflict.ours_index:])
        if replacement:
            next_ours[0] = _transaction_with_statements(
                next_ours[0],
                replacement,
            )
        else:
            next_ours = next_ours[1:]
        return (
            next_ours,
            list(theirs[frontier_theirs_count:]),
        )

    next_theirs = list(theirs[conflict.theirs_index:])
    if replacement:
        next_theirs[0] = _transaction_with_statements(
            next_theirs[0],
            replacement,
        )
    else:
        next_theirs = next_theirs[1:]
    return (
        list(ours[frontier_ours_count:]),
        next_theirs,
    )


def resolve_session(session_path: str | Path) -> int:
    """Run a simple terminal resolver for one merge-session JSON file."""

    session = read_merge_session(session_path)
    if session.get("status") == "not_applicable":
        print(session.get("message", "database is not applicable"))
        return 1

    paths = session["paths"]
    base_path = Path(paths["base"])
    merged_path = Path(paths["merged"])

    base_transaction_id = int(session["base_transaction_id"])
    table_columns, primary_key_columns, key_column_sets = load_schema_metadata_from_db(
        base_path,
    )
    ours = _session_logged_statements(
        session.get("ours_transactions", []),
        "ours",
        table_columns,
    )
    theirs = _session_logged_statements(
        session.get("theirs_transactions", []),
        "theirs",
        table_columns,
    )
    ours = _resolve_branch_replay_safety(
        base_path,
        "ours",
        ours,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )
    theirs = _resolve_branch_replay_safety(
        base_path,
        "theirs",
        theirs,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )
    ours_transactions = group_logged_transactions(ours)
    theirs_transactions = group_logged_transactions(theirs)

    resolved_plan: list[LoggedTransaction] = []
    with closing(_load_planning_db(base_path)) as planning_conn:
        while True:
            plan = build_merge_plan_from_connection(
                planning_conn,
                ":memory:",
                base_transaction_id,
                ours_transactions,
                theirs_transactions,
                table_columns,
                primary_key_columns,
                key_column_sets,
                search_frontier = False,
            )

            prefix = plan.transaction_plan
            error = _apply_to_planning_db(planning_conn, prefix)
            if error is not None:
                # The planner already checked this prefix on the same in-memory
                # database shape; reaching this guard means a missed replay
                # failure or an unexpected side effect from edited SQL.
                print(f"Failed to apply accepted prefix: {error}")
                return 1
            resolved_plan.extend(prefix)

            if plan.status == "clean":
                break

            conflict = plan.selected.next_conflict
            if conflict is None:
                ours_transactions, theirs_transactions = (
                    list(ours_transactions[plan.selected.ours_count:]),
                    list(theirs_transactions[plan.selected.theirs_count:]),
                )
                if not prefix and (ours_transactions or theirs_transactions):
                    # Avoid spinning if a no-conflict frontier consumes no
                    # transactions but there is still work left.
                    print("Conflict search stopped without making progress.")
                    return 1
                if not ours_transactions and not theirs_transactions:
                    break
                continue

            # For a "both" scoped standalone failure, resolve one side first;
            # the next planning pass will surface the other side if needed.
            standalone_scope = STANDALONE_RESOLUTION_SCOPE.get(plan.selected.scope)

            if standalone_scope is not None:
                replacement = _prompt_standalone_resolution(
                    conflict,
                    standalone_scope,
                    ours_transactions,
                    theirs_transactions,
                    table_columns,
                )
                ours_transactions, theirs_transactions = _replace_remaining_standalone(
                    conflict,
                    standalone_scope,
                    replacement,
                    ours_transactions,
                    theirs_transactions,
                    plan.selected.ours_count,
                    plan.selected.theirs_count,
                )
            else:
                while True:
                    resolution = _prompt_pair_resolution(
                        conflict,
                        ours_transactions,
                        theirs_transactions,
                        table_columns,
                    )

                    error = _apply_to_planning_db(planning_conn, resolution)
                    if error is None:
                        resolved_plan.extend(resolution)
                        ours_transactions = list(
                            ours_transactions[conflict.ours_index + 1:]
                        )
                        theirs_transactions = list(
                            theirs_transactions[conflict.theirs_index + 1:]
                        )
                        break
                    print(f"Resolution failed: {error}")

        replay = replay_transaction_plan(base_path, merged_path, resolved_plan)
        if not replay.ok:
            error = replay.failure.error if replay.failure else "unknown replay failure"
            print(f"Failed to write resolved database: {error}")
            return 1
    print(f"Resolved SQLite merge written to {merged_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite reconcile terminal mergetool")
    parser.add_argument("session", help="Path to sqlite-reconcile session JSON")
    args = parser.parse_args(argv)
    return resolve_session(args.session)


if __name__ == "__main__":
    raise SystemExit(main())
