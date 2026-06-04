from __future__ import annotations

from collections.abc import Sequence

from .cascade_metadata import foreign_key_edges_by_child, foreign_key_edges_by_parent
from .models import (
    ConflictCheckContext,
    ConflictKind,
    LoggedStatement,
    LoggedTransaction,
)
from .static_analysis import (
    omitted_integer_primary_key_column,
    transaction_metadata_may_create_or_change_key,
    transaction_metadata_may_remove_or_change_key,
)
from .cascade_types import CascadeWriteEvent
from .sql_metadata import StatementMetadata, required_updated_table
from .utils import ALL_COLUMNS

ColumnCountMap = dict[str, dict[str, int]]


def remaining_individual_check_kinds(
    context: ConflictCheckContext,
    current: LoggedTransaction,
    remaining_other_index: "RemainingMetadataIndex",
) -> set[ConflictKind]:
    """Return individual check kinds needed by cached remaining metadata."""

    if remaining_other_index.is_empty:
        return set()

    needed_kinds: set[ConflictKind] = set()
    if _write_write_conflict_possible(current, remaining_other_index):
        needed_kinds.add("write_write")
    if _column_sets_overlap_counter(
        current.metadata.tables_updated_to_columns_updated,
        remaining_other_index.tables_referenced_to_column_counts,
    ):
        needed_kinds.add("write_read")
    if _implicit_insert_key_conflict_possible(
        context,
        current,
        remaining_other_index,
    ):
        needed_kinds.add("implicit_insert_key")
    if _constraint_conflict_possible_with_index(
        context,
        current,
        remaining_other_index,
    ):
        needed_kinds.add("integrity")
    return needed_kinds


class RemainingMetadataIndex:
    """Mutable read/write counters for one branch's remaining transactions."""

    def __init__(self) -> None:
        self.tables_referenced_to_column_counts: ColumnCountMap = {}
        self.write_write_column_counts: ColumnCountMap = {}
        self.create_or_change_key_column_counts: ColumnCountMap = {}
        self.remove_or_change_key_column_counts: ColumnCountMap = {}
        self.update_column_counts: ColumnCountMap = {}
        self.omitted_integer_primary_key_counts: ColumnCountMap = {}
        self.transaction_count = 0

    @classmethod
    def from_transactions(
        cls,
        context: ConflictCheckContext,
        transactions: Sequence[LoggedTransaction],
    ) -> "RemainingMetadataIndex":
        index = cls()
        for transaction in transactions:
            index.add_transaction(context, transaction)
        return index

    @property
    def is_empty(self) -> bool:
        return self.transaction_count == 0

    def add_transaction(
        self,
        context: ConflictCheckContext,
        transaction: LoggedTransaction,
    ) -> None:
        self._update_transaction(context, transaction, delta=1)
        self.transaction_count += 1

    def remove_transaction(
        self,
        context: ConflictCheckContext,
        transaction: LoggedTransaction,
    ) -> None:
        self._update_transaction(context, transaction, delta=-1)
        self.transaction_count -= 1

    def has_create_or_change_key(
        self,
        table: str,
        columns: set[str],
    ) -> bool:
        return _column_counter_overlap(
            self.create_or_change_key_column_counts,
            table,
            columns,
        )

    def has_remove_or_change_key(
        self,
        table: str,
        columns: set[str],
    ) -> bool:
        return _column_counter_overlap(
            self.remove_or_change_key_column_counts,
            table,
            columns,
        )

    def has_update_write(
        self,
        table: str,
        columns: set[str],
    ) -> bool:
        return _column_counter_overlap(self.update_column_counts, table, columns)

    def has_omitted_integer_primary_key(
        self,
        table: str,
        column: str,
    ) -> bool:
        return _column_counter_overlap(
            self.omitted_integer_primary_key_counts,
            table,
            {column},
        )

    def _update_transaction(
        self,
        context: ConflictCheckContext,
        transaction: LoggedTransaction,
        *,
        delta: int,
    ) -> None:
        for statement in transaction.statements:
            self._update_statement(context, statement, delta=delta)

    def _update_statement(
        self,
        context: ConflictCheckContext,
        statement: LoggedStatement,
        *,
        delta: int,
    ) -> None:
        metadata = statement.metadata
        if metadata.statement_kind == "insert":
            table = _updated_table(metadata)
            _update_column_counts(
                self.create_or_change_key_column_counts,
                table,
                metadata.columns_updated,
                delta=delta,
            )
        elif metadata.statement_kind == "update":
            table = _updated_table(metadata)
            _update_column_counts(
                self.write_write_column_counts,
                table,
                metadata.columns_updated,
                delta=delta,
            )
            _update_column_counts(
                self.create_or_change_key_column_counts,
                table,
                metadata.columns_updated,
                delta=delta,
            )
            _update_column_counts(
                self.remove_or_change_key_column_counts,
                table,
                metadata.columns_updated,
                delta=delta,
            )
            _update_column_counts(
                self.update_column_counts,
                table,
                metadata.columns_updated,
                delta=delta,
            )
        elif metadata.statement_kind == "delete":
            table = _updated_table(metadata)
            _update_column_counts(
                self.write_write_column_counts,
                table,
                metadata.columns_updated,
                delta=delta,
            )
            _update_column_counts(
                self.remove_or_change_key_column_counts,
                table,
                metadata.columns_updated,
                delta=delta,
            )

        for table, columns in (
            metadata.tables_referenced_to_columns_referenced.items()
        ):
            _update_column_counts(
                self.tables_referenced_to_column_counts,
                table,
                columns,
                delta=delta,
            )

        for event in metadata.cascade_write_events:
            self._update_cascade_write_event(event, delta=delta)

        if metadata.statement_kind == "insert":
            table = _updated_table(metadata)
            omitted_key = omitted_integer_primary_key_column(context, metadata)
            if omitted_key is not None:
                _update_column_counts(
                    self.omitted_integer_primary_key_counts,
                    table,
                    {omitted_key},
                    delta=delta,
                )

    def _update_cascade_write_event(
        self,
        event: CascadeWriteEvent,
        *,
        delta: int,
    ) -> None:
        """Add or remove hidden child writes caused by FK actions."""

        columns = set(event.columns)
        _update_column_counts(
            self.write_write_column_counts,
            event.table,
            columns,
            delta=delta,
        )
        _update_column_counts(
            self.remove_or_change_key_column_counts,
            event.table,
            columns,
            delta=delta,
        )
        if event.kind == "update":
            _update_column_counts(
                self.create_or_change_key_column_counts,
                event.table,
                columns,
                delta=delta,
            )
            _update_column_counts(
                self.update_column_counts,
                event.table,
                columns,
                delta=delta,
            )


def _update_column_counts(
    counts_by_table: ColumnCountMap,
    table: str,
    columns: set[str],
    *,
    delta: int,
) -> None:
    """Increment or decrement per-column counters for a table."""

    table_counts = counts_by_table.setdefault(table, {})
    for column in columns:
        next_count = table_counts.get(column, 0) + delta
        if next_count <= 0:
            table_counts.pop(column, None)
        else:
            table_counts[column] = next_count
    if not table_counts:
        counts_by_table.pop(table, None)


def _column_sets_overlap_counter(
    columns_by_table: dict[str, set[str]],
    counts_by_table: ColumnCountMap,
) -> bool:
    """Return whether table/column sets overlap a counted metadata index."""

    return any(
        _column_counter_overlap(counts_by_table, table, columns)
        for table, columns in columns_by_table.items()
    )


def _column_counter_overlap(
    counts_by_table: ColumnCountMap,
    table: str,
    columns: set[str],
) -> bool:
    """Return whether a column set overlaps counted columns for one table."""

    table_counts = counts_by_table.get(table)
    if not table_counts or not columns:
        return False
    if ALL_COLUMNS in table_counts:
        return True
    if ALL_COLUMNS in columns:
        return bool(table_counts)
    return any(column in table_counts for column in columns)


def _updated_table(metadata: StatementMetadata) -> str:
    """Return the updated table for DML metadata."""

    return required_updated_table(metadata)


def _write_write_conflict_possible(
    current: LoggedTransaction,
    remaining_other_index: RemainingMetadataIndex,
) -> bool:
    """Return whether the remaining side has possible update/delete overlap."""

    for statement in current.statements:
        metadata = statement.metadata
        if metadata.statement_kind == "update":
            table = _updated_table(metadata)
            if _column_counter_overlap(
                remaining_other_index.write_write_column_counts,
                table,
                metadata.columns_updated,
            ):
                return True
        elif metadata.statement_kind == "delete":
            table = _updated_table(metadata)
            if _column_counter_overlap(
                remaining_other_index.update_column_counts,
                table,
                {ALL_COLUMNS},
            ):
                return True
        for event in metadata.cascade_write_events:
            if event.kind == "update" and _column_counter_overlap(
                remaining_other_index.write_write_column_counts,
                event.table,
                set(event.columns),
            ):
                return True
            if event.kind == "delete" and _column_counter_overlap(
                remaining_other_index.update_column_counts,
                event.table,
                {ALL_COLUMNS},
            ):
                return True
    return False


def _implicit_insert_key_conflict_possible(
    context: ConflictCheckContext,
    current: LoggedTransaction,
    remaining_other_index: RemainingMetadataIndex,
) -> bool:
    """Return whether hidden INTEGER PRIMARY KEY assignment needs pair checks."""

    for statement in current.statements:
        metadata = statement.metadata
        if metadata.statement_kind == "update":
            table = _updated_table(metadata)
            if _column_counter_overlap(
                remaining_other_index.omitted_integer_primary_key_counts,
                table,
                metadata.columns_updated,
            ):
                return True

        if metadata.statement_kind != "insert":
            continue

        table = _updated_table(metadata)
        if (
            table not in remaining_other_index.omitted_integer_primary_key_counts
            and table not in remaining_other_index.update_column_counts
        ):
            continue

        omitted_key = omitted_integer_primary_key_column(context, metadata)
        if omitted_key is not None and (
            remaining_other_index.has_omitted_integer_primary_key(table, omitted_key)
            or remaining_other_index.has_update_write(table, {omitted_key})
        ):
            return True
    return False


def _constraint_conflict_possible_with_index(
    context: ConflictCheckContext,
    current: LoggedTransaction,
    remaining_other_index: RemainingMetadataIndex,
) -> bool:
    """Return whether remaining constraints require individual replay checks."""

    return _key_constraint_conflict_possible_with_index(
        context,
        current,
        remaining_other_index,
    ) or _foreign_key_constraint_conflict_possible_with_index(
        context,
        current,
        remaining_other_index,
    )


def _key_constraint_conflict_possible_with_index(
    context: ConflictCheckContext,
    current: LoggedTransaction,
    remaining_other_index: RemainingMetadataIndex,
) -> bool:
    """Return whether both sides may create/change the same PK/unique key."""

    for table in current.metadata.tables_updated_to_columns_updated:
        key_sets = context.key_column_sets.get(table, ())
        for key_set in key_sets:
            key_columns = set(key_set)
            if transaction_metadata_may_create_or_change_key(
                current.metadata,
                table,
                key_columns,
            ) and remaining_other_index.has_create_or_change_key(
                table,
                key_columns,
            ):
                return True
    return False


def _foreign_key_constraint_conflict_possible_with_index(
    context: ConflictCheckContext,
    current: LoggedTransaction,
    remaining_other_index: RemainingMetadataIndex,
) -> bool:
    """Return whether parent-key and child-FK writes may violate an FK edge."""

    for table in current.metadata.tables_updated_to_columns_updated:
        for edge in foreign_key_edges_by_parent(context).get(table, ()):
            child_column_set = set(edge.child_columns)
            parent_column_set = set(edge.parent_columns)
            if transaction_metadata_may_remove_or_change_key(
                current.metadata,
                edge.parent_table,
                parent_column_set,
            ) and remaining_other_index.has_create_or_change_key(
                edge.child_table,
                child_column_set,
            ):
                return True

        for edge in foreign_key_edges_by_child(context).get(table, ()):
            child_column_set = set(edge.child_columns)
            parent_column_set = set(edge.parent_columns)
            if transaction_metadata_may_create_or_change_key(
                current.metadata,
                edge.child_table,
                child_column_set,
            ) and remaining_other_index.has_remove_or_change_key(
                edge.parent_table,
                parent_column_set,
            ):
                return True
    return False
