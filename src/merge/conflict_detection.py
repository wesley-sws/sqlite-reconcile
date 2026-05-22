from __future__ import annotations

from .execution_based_analysis import (
    execution_based_matching,
    sqlite_replay_conflicts,
    update_from_has_duplicate_target_rows,
)
from .log_merge import (
    ConflictCheckContext,
    ConflictCheckResult,
    LoggedStatement,
    StatementConflict,
)
from .static_analysis import static_analysis_matching


def statements_conflict(
    context: ConflictCheckContext,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Coordinate static checks, replay safety checks, and execution checks."""

    unsafe_result = _with_unsafe_replay_conflicts(
        ConflictCheckResult(),
        ours_statement,
        theirs_statement,
    )
    if unsafe_result.has_conflict:
        return unsafe_result

    replay_result = _with_update_from_replay_conflicts(
        context,
        unsafe_result,
        ours_statement,
        theirs_statement,
    )
    if replay_result.has_conflict:
        return replay_result

    static_result = static_analysis_matching(context, ours_statement, theirs_statement)
    conflicts = sqlite_replay_conflicts(context, ours_statement, theirs_statement)
    if conflicts:
        return static_result.add_conflicts(*conflicts)

    return execution_based_matching(
        context,
        ours_statement,
        theirs_statement,
        static_result,
        check_integrity=False,
    )


def _with_unsafe_replay_conflicts(
    result: ConflictCheckResult,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Append blocking conflicts for statements unsafe for automatic replay."""

    conflicts: list[StatementConflict] = []
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
                scope=label,
            )
        )

    if not conflicts:
        return result

    return result.add_conflicts(*conflicts)


def _with_update_from_replay_conflicts(
    context: ConflictCheckContext,
    result: ConflictCheckResult,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Append replay-safety conflicts for nondeterministic UPDATE FROM."""

    conflicts: list[StatementConflict] = []
    for label, statement in (
        ("ours", ours_statement),
        ("theirs", theirs_statement),
    ):
        has_duplicates = update_from_has_duplicate_target_rows(
            context,
            statement.metadata,
        )
        if not has_duplicates:
            continue

        conflicts.append(
            StatementConflict(
                kind="unsafe_replay",
                message=(
                    f"{label} UPDATE FROM has multiple source rows for the same "
                    "target row"
                ),
                scope=label,
            )
        )

    if not conflicts:
        return result

    return result.add_conflicts(*conflicts)
