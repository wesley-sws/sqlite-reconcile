from __future__ import annotations

from .execution_based_analysis import (
    execution_based_matching,
    sqlite_replay_conflicts,
)
from .log_merge import (
    ConflictCheckContext,
    ConflictCheckResult,
    LoggedStatement,
)
from .static_analysis import static_analysis_matching


def statements_conflict(
    context: ConflictCheckContext,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Coordinate pairwise static and execution-based conflict checks."""

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
