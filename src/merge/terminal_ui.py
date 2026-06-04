from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .log_merge import make_logged_statement
from .models import (
    BranchName,
    ConflictCheckContext,
    ConflictPair,
    LoggedStatement,
    LoggedTransaction,
    transaction_label,
)
from sqlite_replay_preparation import prepare_logged_sql
from .sql_metadata import transaction_metadata

_printed_replay_sql_note = False


@dataclass(frozen=True)
class PairTransactionResolution:
    """User decision from resolving two conflicting transactions."""

    action: Literal["accept", "replace"]
    ours: LoggedTransaction | None
    theirs: LoggedTransaction | None
    changed_ours: bool = False
    changed_theirs: bool = False


def _group_title(label: str, statements: Sequence[LoggedStatement]) -> str:
    """Return a clear terminal heading for one side of a conflict."""

    branch = "Local" if statements and statements[0].branch == "ours" else "Remote"
    return f"{branch} transaction {label}"


def _print_indented_sql(sql_text: str, indent: str = "  ") -> None:
    """Print SQL with indentation preserved across multiline statements."""

    for line in sql_text.splitlines() or [""]:
        print(f"{indent}{line}")


def _print_statement_group(label: str, statements: Sequence[LoggedStatement]) -> None:
    """Print all statements in a conflict group."""

    _print_replay_sql_note_if_needed(statements)
    print(f"{_group_title(label, statements)}:")
    for index, statement in enumerate(statements):
        print(f"  {_transaction_statement_label(label, statement, index)}:")
        _print_indented_sql(statement.sql_text, indent="    ")


def _print_replay_sql_note_if_needed(statements: Sequence[LoggedStatement]) -> None:
    """Print a one-time note when replay SQL differs from originally logged SQL."""

    global _printed_replay_sql_note
    if _printed_replay_sql_note:
        return
    if not any(
        statement.sql_text != statement.original_sql_text for statement in statements
    ):
        return
    print(
        "Note: shown SQL is the deterministic replay form, "
        "not necessarily the original text."
    )
    _printed_replay_sql_note = True


def _conflict_messages(conflict: ConflictPair) -> str:
    return "\n".join(
        f"{statement_conflict.kind}: {statement_conflict.message}"
        for statement_conflict in conflict.conflicts
    )


def _standalone_conflict_message(conflict: ConflictPair, scope: BranchName) -> str:
    """Return replay messages for the standalone branch being resolved."""

    scoped_conflicts = [
        statement_conflict
        for statement_conflict in conflict.conflicts
        if statement_conflict.scope == scope
    ]
    if not scoped_conflicts:
        scoped_conflicts = [
            statement_conflict
            for statement_conflict in conflict.conflicts
            if statement_conflict.scope == "both"
        ]
    return "\n".join(
        f"{statement_conflict.kind}: {statement_conflict.message}"
        for statement_conflict in scoped_conflicts
    )


def _replace_statement_sql(
    statement: LoggedStatement,
    sql_text: str,
    replay_conn: sqlite3.Connection,
    table_columns,
    metadata_context: ConflictCheckContext | None = None,
) -> LoggedStatement:
    """Reparse edited SQL while preserving the statement's merge identity."""

    prepared = prepare_logged_sql(sql_text, replay_conn)
    return make_logged_statement(
        branch=statement.branch,
        branch_index=statement.branch_index,
        transaction_id=statement.transaction_id,
        committed_at=statement.committed_at,
        sql_text=prepared.to_replay_sql_text,
        original_sql_text=prepared.original_sql_text,
        is_replay_safe=prepared.is_replay_safe,
        replay_block_reason=prepared.replay_block_reason,
        table_columns=table_columns,
        metadata_context=metadata_context,
    )


def _new_statement_sql(
    transaction: LoggedTransaction,
    statements: Sequence[LoggedStatement],
    sql_text: str,
    replay_conn: sqlite3.Connection,
    table_columns,
    metadata_context: ConflictCheckContext | None = None,
) -> LoggedStatement:
    """Create a user-inserted statement inside an existing transaction."""

    branch_index = min(
        min((statement.branch_index for statement in statements), default=0),
        0,
    ) - 1
    prepared = prepare_logged_sql(sql_text, replay_conn)
    return make_logged_statement(
        branch=transaction.branch,
        branch_index=branch_index,
        transaction_id=transaction.transaction_id,
        committed_at=transaction.committed_at,
        sql_text=prepared.to_replay_sql_text,
        original_sql_text=prepared.original_sql_text,
        is_replay_safe=prepared.is_replay_safe,
        replay_block_reason=prepared.replay_block_reason,
        table_columns=table_columns,
        metadata_context=metadata_context,
    )


def _transaction_with_statements(
    transaction: LoggedTransaction,
    statements: Sequence[LoggedStatement],
) -> LoggedTransaction:
    """Return one transaction with edited statements and refreshed metadata."""

    return replace(
        transaction,
        statements=tuple(statements),
        metadata=transaction_metadata(
            statement.metadata for statement in statements
        ),
    )


def _parse_edit_command(raw: str) -> str | None:
    """Return label from edit commands like ':edit L1.1' or 'edit L1.1;'."""

    text = raw.strip().rstrip(";").strip()
    upper_text = text.upper()
    for prefix in (":EDIT", "EDIT"):
        if upper_text == prefix:
            return ""
        if upper_text.startswith(prefix + " "):
            return text.split(maxsplit=1)[1].strip().upper()
    return None


def _parse_delete_command(raw: str) -> str | None:
    """Return label from delete commands like 'delete L1.1;'."""

    text = raw.strip().rstrip(";").strip()
    upper_text = text.upper()
    if upper_text == "DELETE":
        return ""
    if upper_text.startswith("DELETE "):
        return text.split(maxsplit=1)[1].strip().upper()
    return None


def _parse_insert_command(raw: str) -> tuple[str, str] | None:
    """Return insert position and anchor label from 'insert before/after L1.1;'."""

    text = raw.strip().rstrip(";").strip()
    parts = text.split()
    if len(parts) != 3 or parts[0].upper() != "INSERT":
        return None

    position = parts[1].lower()
    if position not in {"before", "after"}:
        return None
    return position, parts[2].upper()


def _git_config_editor() -> str | None:
    """Return Git's configured editor, if available."""

    try:
        completed = subprocess.run(
            ["git", "config", "--get", "core.editor"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None

    editor = completed.stdout.strip()
    return editor or None


def _configured_editor() -> str:
    """Return an external editor, using nano/vi as demo-friendly fallbacks."""

    return (
        os.environ.get("GIT_EDITOR")
        or _git_config_editor()
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
        or ("nano" if shutil.which("nano") else "vi")
    )


def _edit_sql_in_editor(label: str, sql_text: str) -> str | None:
    """Open prefilled SQL in an external editor and return the edited text."""

    editor = _configured_editor()

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=f"-{label}.sql",
        delete=False,
    ) as temp_file:
        temp_file.write(sql_text.rstrip() + "\n")
        temp_path = Path(temp_file.name)

    try:
        subprocess.run(
            [*shlex.split(editor), str(temp_path)],
            check=True,
        )
        edited = temp_path.read_text(encoding="utf-8").strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Editor failed: {exc}")
        return None
    finally:
        temp_path.unlink(missing_ok=True)

    return edited


def _statement_lookup(
    transactions_by_label: dict[str, LoggedTransaction],
) -> dict[str, tuple[str, LoggedTransaction, LoggedStatement]]:
    """Return editable statement labels mapped to their transaction context."""

    lookup: dict[str, tuple[str, LoggedTransaction, LoggedStatement]] = {}
    for transaction_label_text, transaction in transactions_by_label.items():
        for index, statement in enumerate(transaction.statements):
            statement_label_text = _transaction_statement_label(
                transaction_label_text,
                statement,
                index,
            )
            lookup[statement_label_text] = (
                transaction_label_text,
                transaction,
                statement,
            )
    return lookup


def _transaction_statement_label(
    transaction_label_text: str,
    statement: LoggedStatement,
    statement_index: int,
) -> str:
    """Return a label for one statement inside a shown transaction."""

    if statement.branch_index < 0:
        return f"{transaction_label_text}.N{abs(statement.branch_index)}"
    return f"{transaction_label_text}.{statement_index + 1}"


def _replace_transaction_statement(
    transaction: LoggedTransaction,
    target: LoggedStatement,
    replacement: LoggedStatement | None,
) -> LoggedTransaction:
    """Return transaction after replacing or deleting one statement."""

    statements: list[LoggedStatement] = []
    for statement in transaction.statements:
        if statement is target:
            if replacement is not None:
                statements.append(replacement)
        else:
            statements.append(statement)
    return _transaction_with_statements(transaction, statements)


def _insert_transaction_statement(
    transaction: LoggedTransaction,
    anchor: LoggedStatement,
    position: str,
    inserted: LoggedStatement,
) -> LoggedTransaction:
    """Return transaction after inserting a statement before/after anchor."""

    statements: list[LoggedStatement] = []
    for statement in transaction.statements:
        if statement is anchor and position == "before":
            statements.append(inserted)
        statements.append(statement)
        if statement is anchor and position == "after":
            statements.append(inserted)
    return _transaction_with_statements(transaction, statements)


def _print_transaction_edit_result(
    label: str,
    transaction: LoggedTransaction,
) -> None:
    """Show the updated transaction, including the empty-transaction case."""

    if transaction.statements:
        _print_statement_group(label, transaction.statements)
    else:
        print(f"{label}: all statements deleted; this transaction will be skipped.")


def _print_transaction_edit_help(
    labels: Sequence[str],
    transaction_labels: Sequence[str] = (),
) -> None:
    """Show transaction edit commands for the currently visible labels."""

    examples = [
        f"edit {labels[0]};",
        f"delete {labels[0]};",
        f"insert after {labels[0]};",
    ]
    if transaction_labels:
        examples.insert(2, f"delete {transaction_labels[0]};")
    print("Commands: edit <statement>; delete <statement>; delete <transaction>;")
    print("          insert before <statement>; insert after <statement>;")
    print(f"Examples: {', '.join(examples)}")


def _handle_transaction_edit_command(
    raw: str,
    transactions_by_label: dict[str, LoggedTransaction],
    table_columns,
    replay_conn: sqlite3.Connection,
    metadata_context: ConflictCheckContext | None = None,
) -> str:
    """Apply edit/delete/insert commands to editable transactions."""

    lookup = _statement_lookup(transactions_by_label)
    first_label = next(iter(lookup), None)

    delete_label = _parse_delete_command(raw)
    if delete_label is not None:
        if first_label is None:
            print("No statements left to delete.")
            return "handled"
        if not delete_label:
            print(f"Choose a statement to delete, e.g. 'delete {first_label}'.")
            return "handled"
        if delete_label in transactions_by_label:
            transaction = transactions_by_label[delete_label]
            updated = _transaction_with_statements(transaction, ())
            transactions_by_label[delete_label] = updated
            print(f"{delete_label} transaction deleted.")
            _print_transaction_edit_result(delete_label, updated)
            return "changed"
        found = lookup.get(delete_label)
        if found is None:
            print(
                f"Unknown label {delete_label}. "
                "Use a shown statement or transaction label."
            )
            return "handled"
        transaction_label_text, transaction, statement = found
        updated = _replace_transaction_statement(transaction, statement, None)
        transactions_by_label[transaction_label_text] = updated
        print(f"{delete_label} deleted.")
        _print_transaction_edit_result(transaction_label_text, updated)
        return "changed"

    insert_command = _parse_insert_command(raw)
    if insert_command is not None:
        if first_label is None:
            print("No statements left to use as an insert position.")
            return "handled"
        position, anchor_label = insert_command
        found = lookup.get(anchor_label)
        if found is None:
            print(
                f"Unknown label {anchor_label}. "
                "Use a shown statement label."
            )
            return "handled"
        transaction_label_text, transaction, anchor = found
        edited_sql = _edit_sql_in_editor(f"insert {position} {anchor_label}", "")
        if edited_sql is None or not edited_sql.strip():
            print("No statement inserted.")
            return "handled"
        inserted = _new_statement_sql(
            transaction,
            transaction.statements,
            edited_sql,
            replay_conn,
            table_columns,
            metadata_context,
        )
        updated = _insert_transaction_statement(
            transaction,
            anchor,
            position,
            inserted,
        )
        transactions_by_label[transaction_label_text] = updated
        _print_transaction_edit_result(transaction_label_text, updated)
        return "changed"

    edit_label = _parse_edit_command(raw)
    if edit_label is not None:
        if first_label is None:
            print("No statements left to edit.")
            return "handled"
        if not edit_label:
            print(f"Choose a statement to edit, e.g. 'edit {first_label}'.")
            return "handled"
        found = lookup.get(edit_label)
        if found is None:
            print(
                f"Unknown label {edit_label}. "
                "Use a shown statement label."
            )
            return "handled"
        transaction_label_text, transaction, statement = found
        edited_sql = _edit_sql_in_editor(edit_label, statement.sql_text)
        if edited_sql is None:
            print(f"Keeping {edit_label} unchanged.")
            return "handled"
        replacement = None
        if edited_sql.strip():
            replacement = _replace_statement_sql(
                statement,
                edited_sql,
                replay_conn,
                table_columns,
                metadata_context,
            )
        updated = _replace_transaction_statement(transaction, statement, replacement)
        transactions_by_label[transaction_label_text] = updated
        if replacement is None:
            print(f"{edit_label} deleted.")
        _print_transaction_edit_result(transaction_label_text, updated)
        return "changed"

    return "not_command"


def _prompt_pair_transaction_resolution(
    conflict: ConflictPair,
    ours: Sequence[LoggedTransaction],
    theirs: Sequence[LoggedTransaction],
    table_columns,
    replay_conn: sqlite3.Connection,
    metadata_context: ConflictCheckContext | None = None,
    *,
    allow_accept: bool = False,
) -> PairTransactionResolution:
    """Prompt for resolving two transactions, then let the merge loop revalidate."""

    ours_transaction = ours[conflict.index_for_branch("ours")]
    theirs_transaction = theirs[conflict.index_for_branch("theirs")]
    ours_label = transaction_label(ours_transaction)
    theirs_label = transaction_label(theirs_transaction)
    resolved_labels = {
        ours_label: _transaction_with_statements(
            ours_transaction,
            ours_transaction.statements,
        ),
        theirs_label: _transaction_with_statements(
            theirs_transaction,
            theirs_transaction.statements,
        ),
    }

    print()
    print(
        f"{_group_title(ours_label, ours_transaction.statements)} and "
        f"{_group_title(theirs_label, theirs_transaction.statements)} conflict:"
    )
    print(_conflict_messages(conflict))
    _print_statement_group(ours_label, ours_transaction.statements)
    _print_statement_group(theirs_label, theirs_transaction.statements)
    print(f"A = local transaction {ours_label}")
    print(f"B = remote transaction {theirs_label}")

    while True:
        editable_labels = list(_statement_lookup(resolved_labels))
        if not editable_labels:
            return PairTransactionResolution(
                action="replace",
                ours=None,
                theirs=None,
                changed_ours=True,
                changed_theirs=True,
            )

        if allow_accept:
            print("Press Enter to accept this reviewable conflict and keep checking.")
        else:
            print("Edit or delete at least one shown statement before retrying.")
        _print_transaction_edit_help(
            editable_labels,
            transaction_labels=list(resolved_labels),
        )
        raw = input("Resolution: ").strip()

        if not raw:
            if allow_accept:
                return PairTransactionResolution(
                    action="accept",
                    ours=ours_transaction,
                    theirs=theirs_transaction,
                )
            print("No change made yet.")
            continue
        if raw.upper() == "DELETE" or raw == ";":
            return PairTransactionResolution(
                action="replace",
                ours=None,
                theirs=None,
                changed_ours=True,
                changed_theirs=True,
            )

        edit_result = _handle_transaction_edit_command(
            raw,
            resolved_labels,
            table_columns,
            replay_conn,
            metadata_context,
        )
        if edit_result == "changed":
            return _pair_resolution_from_labels(
                resolved_labels,
                ours_label,
                theirs_label,
                ours_transaction,
                theirs_transaction,
            )
        if edit_result == "handled":
            continue
        print(f"Unknown action. Use 'edit {editable_labels[0]};', DELETE, or ;.")


def _pair_resolution_from_labels(
    resolved_labels: dict[str, LoggedTransaction],
    ours_label: str,
    theirs_label: str,
    ours_transaction: LoggedTransaction,
    theirs_transaction: LoggedTransaction,
) -> PairTransactionResolution:
    """Return edited pair transactions for immediate revalidation."""

    ours_updated = resolved_labels[ours_label]
    theirs_updated = resolved_labels[theirs_label]
    return PairTransactionResolution(
        action="replace",
        ours=ours_updated if ours_updated.statements else None,
        theirs=theirs_updated if theirs_updated.statements else None,
        changed_ours=ours_updated != ours_transaction,
        changed_theirs=theirs_updated != theirs_transaction,
    )


def _prompt_standalone_transaction_resolution(
    conflict: ConflictPair,
    scope: BranchName,
    transaction: LoggedTransaction,
    table_columns,
    replay_conn: sqlite3.Connection,
    metadata_context: ConflictCheckContext | None = None,
    *,
    allow_accept: bool = False,
    heading: str | None = None,
) -> list[LoggedStatement] | None:
    """Prompt for resolving one transaction that failed on its own."""

    label = transaction_label(transaction)
    resolved_transaction = {
        label: _transaction_with_statements(transaction, transaction.statements),
    }
    message = _standalone_conflict_message(conflict, scope)
    print()
    print(heading or f"A standalone replay problem came up while applying {label}:")
    print(message)
    _print_statement_group(label, transaction.statements)

    while True:
        current_transaction = resolved_transaction[label]
        editable_labels = list(_statement_lookup(resolved_transaction))
        if not editable_labels:
            print(f"{label}: all statements deleted; this transaction will be skipped.")
            return []

        if allow_accept:
            print("Press Enter to run the shown transaction, or edit/delete it.")
        else:
            print("Edit or delete at least one shown statement before retrying.")
        _print_transaction_edit_help(
            editable_labels,
            transaction_labels=list(resolved_transaction),
        )
        raw = input("Resolution: ").strip()

        if not raw:
            if allow_accept:
                return list(current_transaction.statements)
            print("No change made yet.")
            continue
        if raw.upper() == "DELETE" or raw == ";":
            return None

        edit_result = _handle_transaction_edit_command(
            raw,
            resolved_transaction,
            table_columns,
            replay_conn,
            metadata_context,
        )
        if edit_result == "changed":
            return list(resolved_transaction[label].statements)
        if edit_result == "handled":
            continue
        print(f"Unknown action. Use 'edit {editable_labels[0]};', DELETE, or ;.")
