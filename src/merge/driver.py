from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from .execution_based_analysis import update_from_has_duplicate_target_rows
from .log_merge import (
    ConflictCheckContext,
    ConflictDetector,
    FrontierCandidate,
    LoggedStatement,
    MergePlan,
    MergeNotApplicableError,
    ReplayResult,
    UPDATE_FROM_DUPLICATE_TARGET_WARNING,
    acknowledge_replay_warning,
    acknowledgeable_replay_warning,
    build_merge_plan_from_connection,
    default_conflict_detector,
    load_merge_inputs,
    load_schema_metadata,
    replay_statement_plan,
    validate_database,
)
from .session import write_merge_session, write_not_applicable_session


@dataclass(frozen=True)
class MergeOutcome:
    plan: MergePlan
    replay: ReplayResult
    report_path: str | None


def default_session_path(ours_path: str, pathname: str | None) -> str:
    """Return the default resolver handoff path for a failed merge."""

    if pathname:
        return f"{pathname}.sqlite-reconcile-session.json"
    return f"{ours_path}.sqlite-reconcile-session.json"


def _has_unsafe_replay(statements: Sequence[LoggedStatement]) -> bool:
    """Return whether a branch has wrapper-marked statements to resolve first."""

    return any(
        not statement.is_replay_safe
        and acknowledgeable_replay_warning(statement) is None
        for statement in statements
    )


def _has_replay_warnings(statements: Sequence[LoggedStatement]) -> bool:
    """Return whether a branch has warnings that need user acknowledgement."""

    return any(statement.replay_warnings for statement in statements)


def _with_replay_warning(
    statement: LoggedStatement,
    warning: str,
) -> LoggedStatement:
    """Return statement with warning attached once."""

    if warning in statement.replay_warnings:
        return statement
    return replace(
        statement,
        replay_warnings=(*statement.replay_warnings, warning),
    )


def _update_from_warning(
    con: sqlite3.Connection,
    statement: LoggedStatement,
) -> str | None:
    """Return an UPDATE FROM warning for the branch-local current state."""

    table_columns, primary_key_columns, key_column_sets = load_schema_metadata(
        con.cursor(),
    )
    context = ConflictCheckContext(
        base_cursor=con.cursor(),
        base_db_path=":memory:",
        table_columns=table_columns,
        primary_key_columns=primary_key_columns,
        key_column_sets=key_column_sets,
    )
    if not update_from_has_duplicate_target_rows(context, statement.metadata):
        return None
    return UPDATE_FROM_DUPLICATE_TARGET_WARNING


def _annotate_branch_replay_warnings(
    base_db_path: str | Path,
    statements: list[LoggedStatement],
) -> list[LoggedStatement]:
    """Attach branch-local replay warnings before deciding auto-merge is clean."""

    updated = list(statements)
    with closing(sqlite3.connect(base_db_path)) as base_conn, \
         closing(sqlite3.connect(":memory:")) as branch_conn:
        base_conn.backup(branch_conn)
        branch_conn.row_factory = sqlite3.Row
        branch_conn.execute("PRAGMA foreign_keys = ON")

        for index, statement in enumerate(updated):
            if acknowledgeable_replay_warning(statement) is not None:
                updated[index] = statement = acknowledge_replay_warning(statement)

            if statement.is_replay_safe:
                warning = _update_from_warning(branch_conn, statement)
                if warning is not None:
                    updated[index] = statement = _with_replay_warning(
                        statement,
                        warning,
                    )

            if not statement.is_replay_safe:
                break

            try:
                branch_conn.execute(statement.sql_text)
            except sqlite3.Error:
                break

            if validate_database(branch_conn):
                break

            branch_conn.commit()

    return updated


def _branch_replay_plan(
    base_transaction_id: int,
    ours: list[LoggedStatement],
    theirs: list[LoggedStatement],
) -> MergePlan:
    """Return a handoff plan for branch-local replay cleanup."""

    return MergePlan(
        status="conflict",
        base_transaction_id=base_transaction_id,
        ours=ours,
        theirs=theirs,
        selected=FrontierCandidate(
            name="branch_replay",
            ours_count=0,
            theirs_count=0,
            next_conflict=None,
        ),
        statement_plan=[],
    )


def merge_databases(
    base_db_path: str | Path,
    ours_db_path: str | Path,
    theirs_db_path: str | Path,
    session_path: str | Path | None = None,
    merged_db_path: str | Path | None = None,
    conflict_detector: ConflictDetector | None = None,
) -> MergeOutcome:
    """Run the merge-driver path: auto-merge if clean, otherwise write session."""

    conflict_detector = conflict_detector or default_conflict_detector()
    (
        base_transaction_id,
        ours,
        theirs,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) = load_merge_inputs(base_db_path, ours_db_path, theirs_db_path)

    ours = _annotate_branch_replay_warnings(base_db_path, ours)
    theirs = _annotate_branch_replay_warnings(base_db_path, theirs)

    if (
        _has_unsafe_replay(ours)
        or _has_unsafe_replay(theirs)
        or _has_replay_warnings(ours)
        or _has_replay_warnings(theirs)
    ):
        plan = _branch_replay_plan(base_transaction_id, ours, theirs)
    else:
        with closing(sqlite3.connect(base_db_path)) as base_conn:
            base_conn.row_factory = sqlite3.Row
            plan = build_merge_plan_from_connection(
                base_conn,
                str(base_db_path),
                base_transaction_id,
                ours,
                theirs,
                table_columns,
                primary_key_columns,
                key_column_sets,
                conflict_detector,
                search_frontier=False,
            )

    replay = ReplayResult(
        ok=True,
        output_path=str(ours_db_path),
        applied_count=0,
    )
    session: str | None = None

    if plan.status == "clean":
        replay = replay_statement_plan(
            base_db_path,
            ours_db_path,
            plan.statement_plan,
        )

    if plan.status == "conflict" or not replay.ok:
        session = str(
            session_path
            or default_session_path(str(ours_db_path), pathname=None)
        )
        write_merge_session(
            session,
            status=plan.status if plan.status == "conflict" else "replay_failed",
            base_db_path=base_db_path,
            merged_db_path=merged_db_path or ours_db_path,
            base_transaction_id=plan.base_transaction_id,
            ours=plan.ours,
            theirs=plan.theirs,
            replay=replay,
        )

    return MergeOutcome(plan=plan, replay=replay, report_path=session)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite statement-log merge driver")
    parser.add_argument("base", metavar="%O", help="Base file")
    parser.add_argument(
        "ours",
        metavar="%A",
        help="Ours file; overwritten with merge result on clean merge",
    )
    parser.add_argument("theirs", metavar="%B", help="Theirs file")
    parser.add_argument("conflict_marker_size", metavar="%L", nargs="?", type=int)
    parser.add_argument("pathname", metavar="%P", nargs="?")
    parser.add_argument(
        "--report",
        "--session",
        dest="session",
        help="Path for the merge-session JSON handoff file",
    )
    args = parser.parse_args(argv)

    session_path = args.session or default_session_path(args.ours, args.pathname)
    # In a real Git invocation, %A is the merge-driver work file while %P is
    # the repository path the later mergetool should update.
    merged_db_path = Path(args.pathname).resolve() if args.pathname else args.ours
    try:
        outcome = merge_databases(
            args.base,
            args.ours,
            args.theirs,
            session_path=session_path,
            merged_db_path=merged_db_path,
        )
    except MergeNotApplicableError as exc:
        write_not_applicable_session(session_path, exc)
        print(
            f"sqlite-reconcile: {exc}; session written to {session_path}",
            file=sys.stderr,
        )
        return 1

    if outcome.plan.status == "conflict" or not outcome.replay.ok:
        if outcome.report_path:
            print(
                "sqlite-reconcile: unresolved SQLite merge; "
                f"session written to {outcome.report_path}",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
