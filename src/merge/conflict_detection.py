from __future__ import annotations

from .execution_based_analysis import (
    execution_based_matching,
    sqlite_replay_conflicts,
)
from .log_merge import (
    ConflictCheckContext,
    ConflictCheckResult,
    LoggedTransaction,
    LoggedStatement,
)
from .sql_metadata import transaction_metadata
from .static_analysis import static_analysis_matching


def statements_conflict(
    context: ConflictCheckContext,
    ours_statement: LoggedStatement,
    theirs_statement: LoggedStatement,
) -> ConflictCheckResult:
    """Coordinate pairwise static and execution-based conflict checks."""

    return transactions_conflict(
        context,
        LoggedTransaction(
            branch=ours_statement.branch,
            branch_index=ours_statement.branch_index,
            transaction_id=ours_statement.transaction_id,
            committed_at=ours_statement.committed_at,
            statements=(ours_statement,),
            metadata=transaction_metadata((ours_statement.metadata,)),
        ),
        LoggedTransaction(
            branch=theirs_statement.branch,
            branch_index=theirs_statement.branch_index,
            transaction_id=theirs_statement.transaction_id,
            committed_at=theirs_statement.committed_at,
            statements=(theirs_statement,),
            metadata=transaction_metadata((theirs_statement.metadata,)),
        ),
    )


def transactions_conflict(
    context: ConflictCheckContext,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
) -> ConflictCheckResult:
    """Coordinate static and execution-based checks for two transactions."""

    static_result = static_analysis_matching(
        context,
        ours_transaction.metadata,
        theirs_transaction.metadata,
    )
    conflicts = sqlite_replay_conflicts(context, ours_transaction, theirs_transaction)
    if conflicts:
        return static_result.add_conflicts(*conflicts)

    return execution_based_matching(
        context,
        ours_transaction,
        theirs_transaction,
        static_result,
        check_integrity=False,
    )
