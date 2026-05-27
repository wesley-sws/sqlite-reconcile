from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .sql_metadata import StatementMetadata, TransactionMetadata
from .utils import TableColumns, TableKeyColumnSets, TablePrimaryKeyColumns

BranchName = Literal["ours", "theirs"]
ConflictScope = Literal["pair", "ours", "theirs", "both"]
ConflictKind = Literal[
    "write_write",
    "write_read",
    "implicit_insert_key",
    "integrity",
    "non_commutative",
    "replay_error",
]


@dataclass(frozen=True)
class LoggedStatement:
    branch: BranchName
    branch_index: int
    log_id: int
    transaction_id: int
    committed_at: str
    original_sql_text: str
    to_replay_sql_text: str
    is_replay_safe: bool
    replay_block_reason: str | None
    metadata: StatementMetadata
    replay_warnings: tuple[str, ...] = ()

    @property
    def sql_text(self) -> str:
        """Return the deterministic SQL used for analysis and replay."""

        return self.to_replay_sql_text


@dataclass(frozen=True)
class LoggedTransaction:
    branch: BranchName
    branch_index: int
    transaction_id: int
    committed_at: str
    statements: tuple[LoggedStatement, ...]
    metadata: TransactionMetadata

    @property
    def sql_text(self) -> str:
        """Return compact SQL text for tests and single-transaction labels."""

        return "; ".join(statement.sql_text for statement in self.statements)

    @property
    def original_sql_text(self) -> str:
        """Return display SQL for all statements in this transaction."""

        return ";\n".join(
            statement.original_sql_text for statement in self.statements
        )


@dataclass(frozen=True)
class ConflictPair:
    ours_index: int
    theirs_index: int
    ours_sql: str
    theirs_sql: str
    conflicts: tuple["StatementConflict", ...] = ()


@dataclass(frozen=True)
class ConflictCheckContext:
    base_cursor: sqlite3.Cursor
    base_db_path: str | Path
    table_columns: TableColumns
    primary_key_columns: TablePrimaryKeyColumns = field(default_factory=dict)
    key_column_sets: TableKeyColumnSets = field(default_factory=dict)


@dataclass(frozen=True)
class StatementConflict:
    kind: ConflictKind
    message: str
    scope: ConflictScope = "pair"
    details: tuple[tuple[str, str], ...] = ()


@dataclass(init=False)
class ConflictCheckResult:
    conflicts_by_kind: defaultdict[ConflictKind, list[StatementConflict]]

    def __init__(
        self,
        conflicts: Sequence[StatementConflict] = (),
        *,
        conflicts_by_kind: Mapping[
            ConflictKind,
            Sequence[StatementConflict],
        ] | None = None,
    ):
        grouped: defaultdict[ConflictKind, list[StatementConflict]] = defaultdict(list)
        if conflicts_by_kind is None:
            for conflict in conflicts:
                grouped[conflict.kind].append(conflict)
        else:
            for kind, kind_conflicts in conflicts_by_kind.items():
                if kind_conflicts:
                    grouped[kind] = list(kind_conflicts)

        self.conflicts_by_kind = grouped

    @property
    def conflicts(self) -> tuple[StatementConflict, ...]:
        """Return conflicts flattened in insertion order."""

        return tuple(
            conflict
            for kind_conflicts in self.conflicts_by_kind.values()
            for conflict in kind_conflicts
        )

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts_by_kind)

    def of_kind(self, kind: ConflictKind) -> tuple[StatementConflict, ...]:
        """Return conflicts of one kind."""

        return tuple(self.conflicts_by_kind.get(kind, ()))

    def has_kind(self, kind: ConflictKind) -> bool:
        """Return whether any conflicts of kind are present."""

        return kind in self.conflicts_by_kind

    def without_kind(self, kind: ConflictKind) -> ConflictCheckResult:
        """Remove conflicts of kind and return this result."""

        self.conflicts_by_kind.pop(kind, None)
        return self

    def replace_kind(
        self,
        kind: ConflictKind,
        conflicts: Sequence[StatementConflict],
    ) -> ConflictCheckResult:
        """Replace conflicts of kind and return this result."""

        self.conflicts_by_kind.pop(kind, None)
        if conflicts:
            self.conflicts_by_kind[kind] = list(conflicts)
        return self

    def add_conflicts(
        self,
        *conflicts: StatementConflict,
    ) -> ConflictCheckResult:
        """Append conflicts to their kind groups and return this result."""

        for conflict in conflicts:
            self.conflicts_by_kind[conflict.kind].append(conflict)
        return self


ConflictDetector = Callable[
    [ConflictCheckContext, LoggedTransaction, LoggedTransaction],
    ConflictCheckResult,
]


@dataclass(frozen=True)
class FrontierCandidate:
    name: str
    ours_count: int
    theirs_count: int
    next_conflict: ConflictPair | None
    scope: ConflictScope = "pair"

    @property
    def score(self) -> int:
        return self.ours_count + self.theirs_count


@dataclass(frozen=True)
class ReplayFailure:
    statement: dict[str, object] | None
    error: str


@dataclass(frozen=True)
class ReplayResult:
    ok: bool
    output_path: str
    applied_count: int
    failure: ReplayFailure | None = None
    integrity_errors: list[str] | None = None


@dataclass(frozen=True)
class MergePlan:
    status: Literal["clean", "conflict"]
    base_transaction_id: int
    ours: list[LoggedTransaction]
    theirs: list[LoggedTransaction]
    selected: FrontierCandidate
    transaction_plan: list[LoggedTransaction]
    first_conflict: ConflictPair | None = None
    candidates: list[FrontierCandidate] | None = None

    @property
    def statement_plan(self) -> list[LoggedStatement]:
        """Return the plan flattened to statements for replay helpers/UI code."""

        return [
            statement
            for transaction in self.transaction_plan
            for statement in transaction.statements
        ]


class MergeNotApplicableError(Exception):
    """Raised when a database was not prepared with sqlite-reconcile logging."""

    def __init__(self, db_path: str | Path, role: str, missing_tables: Sequence[str]):
        self.db_path = str(db_path)
        self.role = role
        self.missing_tables = list(missing_tables)
        missing = ", ".join(self.missing_tables)
        super().__init__(
            f"{role} database is not applicable for log-based SQLite merge: "
            f"{self.db_path} is missing {missing}"
        )


def statement_label(statement: LoggedStatement) -> str:
    """Return a user-facing branch label for one statement."""

    if statement.branch_index < 0:
        prefix = "LN" if statement.branch == "ours" else "RN"
        return f"{prefix}{abs(statement.branch_index)}"

    prefix = "L" if statement.branch == "ours" else "R"
    return f"{prefix}{statement.branch_index + 1}"


def statement_group_label(statements: Sequence[LoggedStatement]) -> str:
    """Return a compact label for one statement or a transaction range."""

    if not statements:
        return "?"
    if len(statements) == 1:
        return statement_label(statements[0])
    return f"{statement_label(statements[0])}-{statement_label(statements[-1])}"


def transaction_label(transaction: LoggedTransaction) -> str:
    """Return a compact label for a logged transaction."""

    return statement_group_label(transaction.statements)
