from __future__ import annotations

from collections.abc import Collection, Sequence

from .models import (
    BranchName,
    ConflictCheckContext,
    ConflictKind,
    ConflictCheckResult,
    LoggedTransaction,
    StatementConflict,
    transaction_label,
)
from .remaining_execution import OrderedRemainingExecutionScanner
from .static_analysis import static_analysis_matching

ConflictResolutionKey = tuple[object, ...]


def conflict_resolution_key(
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
    conflicts: tuple[StatementConflict, ...],
) -> ConflictResolutionKey:
    """Return a stable key for an accepted reviewable pair conflict."""

    return (
        "ours",
        ours_transaction.transaction_id,
        tuple(
            statement.branch_index
            for statement in ours_transaction.statements
        ),
        tuple(statement.sql_text for statement in ours_transaction.statements),
        "theirs",
        theirs_transaction.transaction_id,
        tuple(
            statement.branch_index
            for statement in theirs_transaction.statements
        ),
        tuple(statement.sql_text for statement in theirs_transaction.statements),
        tuple(
            (conflict.kind, conflict.scope, conflict.message)
            for conflict in conflicts
        ),
    )


class OrderedRemainingConflictScanner:
    """Stateful conflict scan for one current transaction and opposite suffix."""

    def __init__(
        self,
        context: ConflictCheckContext,
        current_transaction: LoggedTransaction,
        *,
        current_branch: BranchName,
        enabled_kinds: Collection[ConflictKind],
        accepted_pair_keys: Collection[ConflictResolutionKey] = (),
    ) -> None:
        self.context = context
        self.current_transaction = current_transaction
        self.current_branch: BranchName = current_branch
        self.enabled_kinds: set[ConflictKind] = set(enabled_kinds)
        # Reviewable conflicts can be accepted without editing either
        # transaction. If the same pair/result is seen again, advance the
        # scanner instead of prompting forever.
        self.accepted_pair_keys = set(accepted_pair_keys)
        self._scanner = OrderedRemainingExecutionScanner(
            context,
            current_transaction=current_transaction,
            current_branch=current_branch,
            enabled_kinds=enabled_kinds,
        )
        self._started = False
        self._next_other_index = 0

    def enable_kinds(
        self,
        enabled_kinds: Collection[ConflictKind],
    ) -> None:
        """Allow this rolling scan to check additional conflict kinds."""

        self.enabled_kinds.update(enabled_kinds)
        self._scanner.enable_kinds(enabled_kinds)

    def accept_resolution_key(self, key: ConflictResolutionKey | None) -> None:
        """Remember one reviewable pair resolution for this current scan."""

        if key is not None:
            self.accepted_pair_keys.add(key)

    def accept_current_conflict(
        self,
        remaining_other: Sequence[LoggedTransaction],
        other_index: int,
        result: ConflictCheckResult,
        resolution_key: ConflictResolutionKey | None,
    ) -> tuple[int | None, ConflictCheckResult, ConflictResolutionKey | None] | None:
        """Accept the conflict at one opposite index and advance once."""
        # can only accept the conflict the scanner is currently paused on.
        if other_index != self._next_other_index:
            raise RuntimeError("accepted conflict is not at the scanner position")
        self.accept_resolution_key(resolution_key)

        advance_result = self._scanner.accept_next(
            remaining_other[other_index],
            result,
        )
        if advance_result.has_conflict:
            return (other_index, advance_result, resolution_key)

        self._next_other_index += 1
        return None

    def next_conflict(
        self,
        remaining_other: Sequence[LoggedTransaction],
    ) -> tuple[int | None, ConflictCheckResult, ConflictResolutionKey | None] | None:
        """Return the next conflict without discarding the rolling scan state."""

        if not self._started:
            self._started = True
            start_result = self._scanner.start()
            if start_result.has_conflict:
                return (None, start_result, None)

        while self._next_other_index < len(remaining_other):
            other_index = self._next_other_index
            other_transaction = remaining_other[other_index]
            ours_transaction, theirs_transaction = (
                (self.current_transaction, other_transaction)
                if self.current_branch == "ours"
                else (other_transaction, self.current_transaction)
            )
            static_result = static_analysis_matching(
                self.context,
                ours_transaction.metadata,
                theirs_transaction.metadata,
                enabled_kinds=self.enabled_kinds,
                current_branch=self.current_branch,
                ours_label=transaction_label(ours_transaction),
                theirs_label=transaction_label(theirs_transaction),
            )
            result = self._scanner.check_next(
                ours_transaction,
                theirs_transaction,
                static_result,
            )
            if result.has_conflict:
                resolution_key = conflict_resolution_key(
                    ours_transaction,
                    theirs_transaction,
                    result.conflicts,
                )
                if resolution_key in self.accepted_pair_keys:
                    advance_result = self._scanner.accept_next(
                        other_transaction,
                        result,
                    )
                    if advance_result.has_conflict:
                        return (other_index, advance_result, resolution_key)
                    self._next_other_index += 1
                    continue
                return (other_index, result, resolution_key)
            self._next_other_index += 1

        return None

    def close(self) -> None:
        """Discard scan-only effects."""

        self._scanner.close()


def ordered_remaining_transactions_conflict(
    context: ConflictCheckContext,
    current_transaction: LoggedTransaction,
    remaining_other: Sequence[LoggedTransaction],
    *,
    current_branch: BranchName,
    enabled_kinds: Collection[ConflictKind],
    accepted_pair_keys: Collection[ConflictResolutionKey] = (),
) -> tuple[int | None, ConflictCheckResult, ConflictResolutionKey | None] | None:
    """Check current against the opposite suffix using one rolling replay scan."""

    scanner = OrderedRemainingConflictScanner(
        context,
        current_transaction,
        current_branch=current_branch,
        enabled_kinds=enabled_kinds,
        accepted_pair_keys=accepted_pair_keys,
    )
    try:
        return scanner.next_conflict(remaining_other)
    finally:
        scanner.close()
