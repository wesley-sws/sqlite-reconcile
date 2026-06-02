from __future__ import annotations

import argparse
import sqlite3
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .accepted_replay import apply_accepted_transaction
from .conflict_detection import ConflictResolutionKey
from .control_db import _load_working_base_copy, _open_merge_working_context
from .execution_based_analysis import update_from_has_duplicate_target_rows
from .log_merge import (
    MergeNotApplicableError,
    UPDATE_FROM_DUPLICATE_TARGET_WARNING,
    _RemainingCurrentConflictScan,
    accept_replay_warning,
    load_merge_inputs,
    pending_replay_warning,
    unresolved_replay_block_reason,
    validate_database,
)
from .models import (
    BranchName,
    ConflictCheckContext,
    ConflictPair,
    LoggedStatement,
    LoggedTransaction,
    StatementConflict,
    transaction_label,
)
from .remaining_metadata import RemainingMetadataIndex
from .terminal_ui import (
    _prompt_pair_transaction_resolution,
    _prompt_standalone_transaction_resolution,
    _transaction_with_statements,
)
from .utils import quote_identifier, rollback_savepoint

MetadataIndexes = dict[BranchName, RemainingMetadataIndex]
PairSideOutcome = Literal["unchanged", "edited", "deleted"]
StandaloneReplayOutcome = Literal["current_removed", "current_changed"]


@dataclass(frozen=True)
class PairConflictOutcome:
    """Describe what the user did to each transaction in a pair conflict."""

    action: Literal["accepted", "resolved"]
    ours: PairSideOutcome = "unchanged"
    theirs: PairSideOutcome = "unchanged"

    def side(self, branch: BranchName) -> PairSideOutcome:
        """Return the outcome for one branch."""

        return self.ours if branch == "ours" else self.theirs


@dataclass(frozen=True)
class BranchReplayIssue:
    """Reason one branch-local transaction cannot simply replay yet."""

    conflict: StatementConflict
    warning: tuple[LoggedStatement, str] | None = None

    @property
    def allow_accept(self) -> bool:
        """Return whether Enter should accept the original transaction."""

        return self.warning is not None


def _branch_transaction_replay_issue(
    con: sqlite3.Connection,
    update_from_context: ConflictCheckContext,
    transaction: LoggedTransaction,
) -> BranchReplayIssue | None:
    """Apply one branch-local transaction, returning why it cannot replay."""

    savepoint = quote_identifier(f"sqlite_merge_branch_{uuid.uuid4().hex}")
    con.execute(f"SAVEPOINT {savepoint}")
    last_statement: LoggedStatement | None = None
    try:
        for statement in transaction.statements:
            last_statement = statement
            replay_warning = _statement_replay_warning(statement)
            if replay_warning is not None:
                rollback_savepoint(con.cursor(), savepoint)
                return _branch_replay_warning_issue(
                    transaction.branch,
                    statement,
                    replay_warning,
                )

            replay_block_reason = unresolved_replay_block_reason(statement)
            if replay_block_reason is not None:
                rollback_savepoint(con.cursor(), savepoint)
                return BranchReplayIssue(
                    _branch_replay_conflict(
                        "replay_error",
                        replay_block_reason,
                        transaction.branch,
                        statement,
                    ),
                )
            if (
                UPDATE_FROM_DUPLICATE_TARGET_WARNING
                not in statement.accepted_replay_warnings
                and update_from_has_duplicate_target_rows(
                    update_from_context,
                    statement.metadata,
                )
            ):
                rollback_savepoint(con.cursor(), savepoint)
                return _branch_replay_warning_issue(
                    transaction.branch,
                    statement,
                    UPDATE_FROM_DUPLICATE_TARGET_WARNING,
                )
            con.execute(statement.sql_text)
        errors = validate_database(con)
        if errors:
            rollback_savepoint(con.cursor(), savepoint)
            return BranchReplayIssue(
                _branch_replay_conflict(
                    "integrity",
                    "\n".join(errors),
                    transaction.branch,
                ),
            )
        con.execute(f"RELEASE {savepoint}")
        con.commit()
        return None
    except sqlite3.Error as exc:
        rollback_savepoint(con.cursor(), savepoint)
        return BranchReplayIssue(
            _branch_replay_conflict(
                _sqlite_replay_conflict_kind(exc),
                str(exc),
                transaction.branch,
                last_statement,
            ),
        )


def _sqlite_replay_conflict_kind(
    error: sqlite3.Error,
) -> Literal["integrity", "replay_error"]:
    """Return the merge conflict kind for a branch-local SQLite exception."""

    if isinstance(error, sqlite3.IntegrityError):
        return "integrity"
    return "replay_error"


def _branch_replay_conflict(
    kind: Literal["integrity", "replay_error"],
    message: str,
    scope: BranchName,
    statement: LoggedStatement | None = None,
) -> StatementConflict:
    """Return one branch-local replay conflict, scoped to a statement if known."""

    return StatementConflict(
        kind=kind,
        message=message,
        scope=scope,
        details=(
            ()
            if statement is None
            else (("statement_log_id", str(statement.log_id)),)
        ),
    )


def _branch_replay_warning_issue(
    scope: BranchName,
    statement: LoggedStatement,
    warning: str,
) -> BranchReplayIssue:
    """Return a reviewable branch replay warning."""

    return BranchReplayIssue(
        _branch_replay_conflict(
            "replay_error",
            f"warning: {warning}",
            scope,
            statement,
        ),
        warning=(statement, warning),
    )


def _statement_replay_warning(statement: LoggedStatement) -> str | None:
    """Return a stored/unsafe replay warning that still needs user acceptance."""

    warning = pending_replay_warning(statement)
    if warning is not None:
        return warning

    for warning in statement.replay_warnings:
        if (
            warning != UPDATE_FROM_DUPLICATE_TARGET_WARNING
            and warning not in statement.accepted_replay_warnings
        ):
            return warning
    return None


def _accept_warning_if_statement_still_present(
    statements: list[LoggedStatement],
    warning_statement: LoggedStatement,
    warning: str,
) -> None:
    """Record an accepted warning if the same statement survived the prompt."""

    for index, statement in enumerate(statements):
        if statement is warning_statement:
            statements[index] = accept_replay_warning(statement, warning)
            return


def _resolve_branch_transaction(
    con: sqlite3.Connection,
    update_from_context: ConflictCheckContext,
    branch: BranchName,
    transaction: LoggedTransaction,
    table_columns,
) -> LoggedTransaction | None:
    """Resolve and replay one branch-local transaction."""

    current = transaction
    while True:
        replay_issue = _branch_transaction_replay_issue(
            con,
            update_from_context,
            current,
        )
        if replay_issue is None:
            return current

        replacement_statements = _prompt_standalone_transaction_resolution(
            _standalone_head_conflict(replay_issue.conflict, branch),
            branch,
            current,
            table_columns,
            con,
            allow_accept=replay_issue.allow_accept,
            heading=(
                f"A replay warning came up while checking {transaction_label(current)}:"
                if replay_issue.allow_accept
                else None
            ),
        )
        if not replacement_statements:
            return None
        if replay_issue.warning is not None:
            warning_statement, warning = replay_issue.warning
            _accept_warning_if_statement_still_present(
                replacement_statements,
                warning_statement,
                warning,
            )
        current = _transaction_with_statements(current, replacement_statements)


def _resolve_branch_replay_safety(
    base_path: Path,
    branch: BranchName,
    transactions: list[LoggedTransaction],
    table_columns,
    primary_key_columns,
    key_column_sets,
) -> list[LoggedTransaction]:
    """Resolve wrapper-unsafe or branch-local replay failures before pair checks.

    Each branch is checked from a fresh base copy. Reusing the same connection
    across branches would make the second branch replay after the first branch's
    accepted prefix, which is not a branch-local safety check anymore.
    """

    resolved: list[LoggedTransaction] = []
    with closing(_load_working_base_copy(base_path)) as branch_conn:
        update_from_context = ConflictCheckContext(
            base_cursor=branch_conn.cursor(),
            base_db_path=":memory:",
            table_columns=table_columns,
            primary_key_columns=primary_key_columns,
            key_column_sets=key_column_sets,
        )
        for transaction in transactions:
            replayed = _resolve_branch_transaction(
                branch_conn,
                update_from_context,
                branch,
                transaction,
                table_columns,
            )
            if replayed is not None:
                resolved.append(replayed)

    return resolved


def _replace_or_delete_transaction(
    context: ConflictCheckContext,
    transactions: deque[LoggedTransaction],
    metadata_index: RemainingMetadataIndex,
    index: int,
    replacement: LoggedTransaction | None,
) -> None:
    """Replace or delete one queued transaction in place."""

    metadata_index.remove_transaction(context, transactions[index])
    if replacement is None:
        del transactions[index]
    else:
        transactions[index] = replacement
        metadata_index.add_transaction(context, replacement)


def _resolve_standalone_replay(
    context: ConflictCheckContext,
    scope: BranchName,
    conflict: ConflictPair,
    remaining_ours: deque[LoggedTransaction],
    remaining_theirs: deque[LoggedTransaction],
    metadata_indexes: MetadataIndexes,
    table_columns,
) -> StandaloneReplayOutcome:
    """Edit/delete the current transaction that failed before pair comparison."""

    target_queue = remaining_ours if scope == "ours" else remaining_theirs
    target_transaction = target_queue[0]
    replacement_statements = _prompt_standalone_transaction_resolution(
        conflict,
        scope,
        target_transaction,
        table_columns,
        context.base_cursor.connection,
    )
    replacement = (
        None
        if not replacement_statements
        else _transaction_with_statements(
            target_transaction,
            replacement_statements,
        )
    )
    _replace_or_delete_transaction(
        context,
        target_queue,
        metadata_indexes[scope],
        0,
        replacement,
    )
    return "current_removed" if replacement is None else "current_changed"


def _resolve_remaining_pair_conflict(
    context: ConflictCheckContext,
    conflict: ConflictPair,
    remaining_ours: deque[LoggedTransaction],
    remaining_theirs: deque[LoggedTransaction],
    metadata_indexes: MetadataIndexes,
    table_columns,
    *,
    current_branch: BranchName,
) -> PairConflictOutcome:
    """Edit/delete a current-vs-remaining pair and describe what changed."""

    resolution = _prompt_pair_transaction_resolution(
        conflict,
        remaining_ours,
        remaining_theirs,
        table_columns,
        context.base_cursor.connection,
        allow_accept=_is_reviewable_pair_conflict(conflict),
    )
    if resolution.action == "accept":
        return PairConflictOutcome("accepted")

    if resolution.changed_ours:
        _replace_or_delete_transaction(
            context,
            remaining_ours,
            metadata_indexes["ours"],
            conflict.index_for_branch("ours"),
            resolution.ours,
        )
    if resolution.changed_theirs:
        _replace_or_delete_transaction(
            context,
            remaining_theirs,
            metadata_indexes["theirs"],
            conflict.index_for_branch("theirs"),
            resolution.theirs,
        )

    return PairConflictOutcome(
        "resolved",
        ours=_pair_side_outcome(resolution.changed_ours, resolution.ours),
        theirs=_pair_side_outcome(resolution.changed_theirs, resolution.theirs),
    )


def _pair_side_outcome(
    changed: bool,
    replacement: LoggedTransaction | None,
) -> PairSideOutcome:
    """Return whether one transaction was kept, edited, or deleted."""

    if not changed:
        return "unchanged"
    if replacement is None:
        return "deleted"
    return "edited"


def _is_reviewable_pair_conflict(conflict: ConflictPair) -> bool:
    """Return whether the user can accept original SQL and keep checking."""

    return bool(conflict.conflicts) and all(
        statement_conflict.kind not in {"integrity", "replay_error"}
        for statement_conflict in conflict.conflicts
    )


def _is_current_standalone_replay(
    conflict: ConflictPair,
    current_branch: BranchName,
) -> bool:
    """Return whether the scanner failed before comparing the opposite branch."""

    return conflict.is_standalone and conflict.current_branch == current_branch


def _standalone_head_conflict(
    conflict: StatementConflict,
    current_branch: BranchName,
) -> ConflictPair:
    """Build a queue-relative conflict for the current transaction head."""

    return ConflictPair(
        current_branch=current_branch,
        other_index=None,
        ours_sql="",
        theirs_sql="",
        conflicts=(conflict,),
        is_standalone=True,
    )


def _unresolved_replay_safety_conflict(
    transaction: LoggedTransaction,
) -> StatementConflict | None:
    """Return a standalone conflict for an unsafe statement in a transaction."""

    for statement in transaction.statements:
        replay_block_reason = unresolved_replay_block_reason(statement)
        if replay_block_reason is None:
            continue
        return StatementConflict(
            kind="replay_error",
            message=replay_block_reason,
            scope=transaction.branch,
            details=(("statement_log_id", str(statement.log_id)),),
        )
    return None


def _check_accept_current(
    current_branch: BranchName,
    remaining_ours: deque[LoggedTransaction],
    remaining_theirs: deque[LoggedTransaction],
    metadata_indexes: MetadataIndexes,
    context: ConflictCheckContext,
    table_columns,
) -> bool:
    """Resolve at most one fixed-order queue head for this branch."""

    accepted_pair_keys: set[ConflictResolutionKey] = set()
    current_queue = remaining_ours if current_branch == "ours" else remaining_theirs
    other_queue = remaining_theirs if current_branch == "ours" else remaining_ours
    other_branch: BranchName = "theirs" if current_branch == "ours" else "ours"
    scan: _RemainingCurrentConflictScan | None = None
    accepted_followup_conflict: ConflictPair | None = None

    # Resolution helpers mutate the queues immediately; outcomes below only
    # tell this turn whether to retry, continue the scan, or stop.
    while current_queue:
        current = current_queue[0]
        safety_conflict = _unresolved_replay_safety_conflict(current)
        if safety_conflict is not None:
            if scan is not None:
                scan.close()
                scan = None
            outcome = _resolve_standalone_replay(
                context,
                current_branch,
                _standalone_head_conflict(safety_conflict, current_branch),
                remaining_ours,
                remaining_theirs,
                metadata_indexes,
                table_columns,
            )
            if outcome == "current_removed":
                return True
            continue

        if scan is None:
            scan = _RemainingCurrentConflictScan(
                current,
                current_branch=current_branch,
                context=context,
                remaining_other_index=metadata_indexes[other_branch],
                accepted_pair_keys=accepted_pair_keys,
            )
        if accepted_followup_conflict is not None:
            conflict = accepted_followup_conflict
            accepted_followup_conflict = None
        else:
            conflict = scan.next_conflict(other_queue)
        if conflict is not None:
            # scanner.start() may fail while replaying only the current head on
            # control; that is a standalone prefix problem, not a pair conflict.
            if _is_current_standalone_replay(conflict, current_branch):
                outcome = _resolve_standalone_replay(
                    context,
                    current_branch,
                    conflict,
                    remaining_ours,
                    remaining_theirs,
                    metadata_indexes,
                    table_columns,
                )
                scan.close()
                scan = None
                if outcome == "current_removed":
                    return True
                if outcome == "current_changed":
                    continue
            else:
                pair_outcome = _resolve_remaining_pair_conflict(
                    context,
                    conflict,
                    remaining_ours,
                    remaining_theirs,
                    metadata_indexes,
                    table_columns,
                    current_branch=current_branch,
                )
                if pair_outcome.action == "accepted":
                    if conflict.resolution_key is not None:
                        accepted_pair_keys.add(conflict.resolution_key)
                    accepted_followup_conflict = scan.accept_current_conflict(
                        conflict,
                        other_queue,
                    )
                    continue

                current_outcome = pair_outcome.side(current_branch)
                other_outcome = pair_outcome.side(other_branch)
                if current_outcome == "deleted":
                    scan.close()
                    return True
                if current_outcome == "edited":
                    scan.close()
                    scan = None
                    continue
                if other_outcome == "edited":
                    scan.enable_checks_after_other_edit(
                        metadata_indexes[other_branch],
                    )
                continue
            continue

        # If no pair checks were needed, current may not have been trial-run by
        # the scanner. The final apply can still fail and needs user resolution.
        scan.close()
        scan = None
        replay_error = apply_accepted_transaction(context, current)
        if replay_error is not None:
            outcome = _resolve_standalone_replay(
                context,
                current_branch,
                _standalone_head_conflict(replay_error, current_branch),
                remaining_ours,
                remaining_theirs,
                metadata_indexes,
                table_columns,
            )
            if outcome == "current_removed":
                return True
            if outcome == "current_changed":
                continue
            continue

        metadata_indexes[current_branch].remove_transaction(context, current)
        current_queue.popleft()
        return True
    return True


def _write_working_result(
    context: ConflictCheckContext,
    merged_path: Path,
) -> None:
    """Write the accepted working database to Git's merged path."""

    source_conn = context.base_cursor.connection
    source_conn.commit()
    with closing(sqlite3.connect(merged_path)) as merged_conn:
        source_conn.backup(merged_conn, name="main")


def _resolve_merge_transactions(
    *,
    base_path: Path,
    merged_path: Path,
    ours_transactions: list[LoggedTransaction],
    theirs_transactions: list[LoggedTransaction],
    table_columns,
    primary_key_columns,
    key_column_sets,
) -> int:
    """Resolve loaded branch transactions and write the merged database."""

    ours_transactions = _resolve_branch_replay_safety(
        base_path,
        "ours",
        ours_transactions,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )
    theirs_transactions = _resolve_branch_replay_safety(
        base_path,
        "theirs",
        theirs_transactions,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )

    remaining_ours: deque[LoggedTransaction] = deque(ours_transactions)
    remaining_theirs: deque[LoggedTransaction] = deque(theirs_transactions)
    with _open_merge_working_context(
        base_path,
        table_columns,
        primary_key_columns,
        key_column_sets,
    ) as context:
        metadata_indexes: MetadataIndexes = {
            "ours": RemainingMetadataIndex.from_transactions(
                context,
                remaining_ours,
            ),
            "theirs": RemainingMetadataIndex.from_transactions(
                context,
                remaining_theirs,
            ),
        }
        while remaining_ours or remaining_theirs:
            for branch in ("ours", "theirs"):
                if not _check_accept_current(
                    branch,
                    remaining_ours,
                    remaining_theirs,
                    metadata_indexes,
                    context,
                    table_columns,
                ):
                    return 1
        try:
            _write_working_result(context, merged_path)
        except (OSError, sqlite3.Error) as exc:
            print(f"Failed to write resolved database: {exc}")
            return 1

    print(f"Resolved SQLite merge written to {merged_path}")
    return 0


def resolve_direct_merge(
    base_db_path: str | Path,
    ours_db_path: str | Path,
    theirs_db_path: str | Path,
    merged_db_path: str | Path,
) -> int:
    """Run the terminal mergetool directly from Git's base/local/remote files."""

    try:
        (
            ours_transactions,
            theirs_transactions,
            table_columns,
            primary_key_columns,
            key_column_sets,
        ) = load_merge_inputs(base_db_path, ours_db_path, theirs_db_path)
    except MergeNotApplicableError as exc:
        print(exc)
        return 1

    return _resolve_merge_transactions(
        base_path=Path(base_db_path),
        merged_path=Path(merged_db_path),
        ours_transactions=ours_transactions,
        theirs_transactions=theirs_transactions,
        table_columns=table_columns,
        primary_key_columns=primary_key_columns,
        key_column_sets=key_column_sets,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite reconcile terminal mergetool")
    parser.add_argument(
        "paths",
        nargs="+",
        help="Git mergetool paths: BASE LOCAL REMOTE [MERGED]",
    )
    args = parser.parse_args(argv)
    if len(args.paths) in {3, 4}:
        base, ours, theirs = args.paths[:3]
        merged = args.paths[3] if len(args.paths) == 4 else ours
        return resolve_direct_merge(base, ours, theirs, merged)

    parser.error("expected BASE LOCAL REMOTE [MERGED]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
