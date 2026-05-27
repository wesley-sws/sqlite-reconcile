from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .log_merge import (
    BranchName,
    ConflictPair,
    LoggedStatement,
    LoggedTransaction,
    acknowledge_replay_warning,
    make_logged_statement,
)
from .models import (
    ConflictScope,
    StatementConflict,
    statement_group_label,
    statement_label,
    transaction_label,
)
from .sql_metadata import transaction_metadata

STANDALONE_RESOLUTION_SCOPE: dict[ConflictScope, BranchName] = {
    "ours": "ours",
    "theirs": "theirs",
    "both": "ours",
}


def _group_title(label: str, statements: Sequence[LoggedStatement]) -> str:
    """Return a clear terminal heading for one side of a conflict."""

    branch = "Local" if statements and statements[0].branch == "ours" else "Remote"
    unit = "transaction" if len(statements) > 1 else "statement"
    return f"{branch} {unit} {label}"


def _print_indented_sql(sql_text: str, indent: str = "  ") -> None:
    """Print SQL with indentation preserved across multiline statements."""

    for line in sql_text.splitlines() or [""]:
        print(f"{indent}{line}")


def _print_statement_group(label: str, statements: Sequence[LoggedStatement]) -> None:
    """Print all statements in a conflict group."""

    print(f"{_group_title(label, statements)}:")
    if len(statements) == 1:
        _print_indented_sql(statements[0].original_sql_text)
        return
    for statement in statements:
        print(f"  {statement_label(statement)}:")
        _print_indented_sql(statement.original_sql_text, indent="    ")


def _conflict_messages(conflict: ConflictPair) -> str:
    return "\n".join(
        f"{statement_conflict.kind}: {statement_conflict.message}"
        for statement_conflict in conflict.conflicts
    )


def _standalone_conflicts(conflict: ConflictPair) -> tuple[StatementConflict, ...]:
    """Return scoped replay failures from one conflict pair."""

    return tuple(
        statement_conflict
        for statement_conflict in conflict.conflicts
        if statement_conflict.scope in {"ours", "theirs", "both"}
    )


def _standalone_conflict_message(conflict: ConflictPair, scope: BranchName) -> str:
    """Return replay messages for the standalone branch being resolved."""

    scoped_conflicts = [
        statement_conflict
        for statement_conflict in _standalone_conflicts(conflict)
        if statement_conflict.scope == scope
    ]
    if not scoped_conflicts:
        scoped_conflicts = [
            statement_conflict
            for statement_conflict in _standalone_conflicts(conflict)
            if statement_conflict.scope == "both"
        ]
    return "\n".join(
        f"{statement_conflict.kind}: {statement_conflict.message}"
        for statement_conflict in scoped_conflicts
    )


def _edit_label_hint(
    lookup: dict[str, tuple[str, LoggedTransaction, LoggedStatement]],
) -> str:
    """Return a compact hint for the labels that can be edited."""

    if not lookup:
        return "none"

    hints: list[str] = []
    for branch, prefix in (("ours", "L"), ("theirs", "R")):
        original_indexes = sorted(
            statement.branch_index
            for _, _, statement in lookup.values()
            if statement.branch == branch and statement.branch_index >= 0
        )
        if original_indexes:
            first = f"{prefix}{original_indexes[0] + 1}"
            last = f"{prefix}{original_indexes[-1] + 1}"
            hints.append(first if first == last else f"{first}-{last}")

        new_indexes = sorted(
            abs(statement.branch_index)
            for _, _, statement in lookup.values()
            if statement.branch == branch and statement.branch_index < 0
        )
        if new_indexes:
            new_prefix = f"{prefix}N"
            first = f"{new_prefix}{new_indexes[0]}"
            last = f"{new_prefix}{new_indexes[-1]}"
            hints.append(first if first == last else f"{first}-{last}")
    return ", ".join(hints)


def _replace_statement_sql(
    statement: LoggedStatement,
    sql_text: str,
    table_columns,
) -> LoggedStatement:
    """Reparse edited SQL while preserving the original branch/log identity."""

    return make_logged_statement(
        branch=statement.branch,
        branch_index=statement.branch_index,
        log_id=statement.log_id,
        transaction_id=statement.transaction_id,
        committed_at=statement.committed_at,
        sql_text=sql_text,
        original_sql_text=sql_text,
        is_replay_safe=True,
        table_columns=table_columns,
    )


def _new_statement_sql(
    transaction: LoggedTransaction,
    statements: Sequence[LoggedStatement],
    sql_text: str,
    table_columns,
) -> LoggedStatement:
    """Create a user-inserted statement inside an existing transaction."""

    branch_index = min(
        (
            statement.branch_index
            for statement in statements
            if statement.branch_index < 0
        ),
        default=0,
    ) - 1
    log_id = min((statement.log_id for statement in statements), default=0)
    return make_logged_statement(
        branch=transaction.branch,
        branch_index=branch_index,
        log_id=min(log_id, 0) - 1,
        transaction_id=transaction.transaction_id,
        committed_at=transaction.committed_at,
        sql_text=sql_text,
        original_sql_text=sql_text,
        is_replay_safe=True,
        table_columns=table_columns,
    )



def _prompt_update_from_warning(
    statement: LoggedStatement,
    table_columns,
    warning: str,
) -> LoggedStatement | None:
    """Prompt after an UPDATE FROM warning; Enter keeps and runs the statement."""

    label = statement_label(statement)
    current = statement
    print()
    print(f"Warning for {label}: {warning}. SQLite may choose any matching row.")
    print(f"{label}: {current.original_sql_text}")
    while True:
        replacement = input(
            f"{label} action [Enter to run shown SQL, :edit for editor, "
            "DELETE or ; to skip]: "
        ).strip()
        if not replacement:
            return current
        if replacement.upper() == "DELETE" or replacement == ";":
            return None
        if replacement == ":edit":
            replacement = _edit_sql_in_editor(label, current.sql_text) or ""
        if replacement:
            current = _replace_statement_sql(current, replacement, table_columns)
            print(f"{label}: {current.original_sql_text}")


def _prompt_replay_warning(
    statement: LoggedStatement,
    table_columns,
    warning: str,
) -> tuple[LoggedStatement | None, bool]:
    """Prompt after a replay warning; Enter keeps and runs the statement."""

    label = statement_label(statement)
    current = statement
    changed = False
    print()
    print(f"Warning for {label}: {warning}. Replaying may produce different values.")
    print(f"{label}: {current.original_sql_text}")
    while True:
        replacement = input(
            f"{label} action [Enter to run shown SQL, :edit for editor, "
            "DELETE or ; to skip]: "
        ).strip()
        if not replacement:
            return acknowledge_replay_warning(current), changed
        if replacement.upper() == "DELETE" or replacement == ";":
            return None, True
        if replacement == ":edit":
            replacement = _edit_sql_in_editor(label, current.sql_text) or ""
        if replacement:
            current = _replace_statement_sql(current, replacement, table_columns)
            changed = True
            print(f"{label}: {current.original_sql_text}")



def _transaction_with_statements(
    transaction: LoggedTransaction,
    statements: Sequence[LoggedStatement],
) -> LoggedTransaction:
    """Return one transaction with edited statements and refreshed metadata."""

    return replace(
        transaction,
        statements=tuple(statements),
        metadata=transaction_metadata(
            tuple(statement.metadata for statement in statements),
        ),
    )



def _parse_order(
    raw: str,
    labels: dict[str, LoggedTransaction],
) -> list[LoggedTransaction] | None:
    """Parse semicolon-separated transaction labels like A; B;."""

    text = raw.strip()
    if not text:
        return [transaction for transaction in labels.values() if transaction.statements]
    tokens = [token.strip().upper() for token in text.split(";") if token.strip()]
    if not tokens:
        return []

    selected: list[LoggedTransaction] = []
    for token in tokens:
        transaction = labels.get(token)
        if transaction is None:
            print(f"Unknown label {token}. Valid labels: {', '.join(labels)}")
            return None
        if transaction.statements:
            selected.append(transaction)
    return selected


def _parse_edit_command(raw: str) -> str | None:
    """Return label from edit commands like ':edit L1' or 'edit L1;'."""

    text = raw.strip().rstrip(";").strip()
    upper_text = text.upper()
    for prefix in (":EDIT", "EDIT"):
        if upper_text == prefix:
            return ""
        if upper_text.startswith(prefix + " "):
            return text.split(maxsplit=1)[1].strip().upper()
    return None


def _parse_delete_command(raw: str) -> str | None:
    """Return label from delete commands like 'delete L1;'."""

    text = raw.strip().rstrip(";").strip()
    upper_text = text.upper()
    if upper_text == "DELETE":
        return ""
    if upper_text.startswith("DELETE "):
        return text.split(maxsplit=1)[1].strip().upper()
    return None


def _parse_insert_command(raw: str) -> tuple[str, str] | None:
    """Return insert position and anchor label from 'insert before/after L1;'."""

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


def _one_line_sql(sql_text: str) -> str:
    """Return SQL compacted for an editable single-line terminal prompt."""

    return " ".join(line.strip() for line in sql_text.splitlines() if line.strip())


def _input_with_prefill(prompt: str, initial_text: str) -> str:
    """Read one line with readline prefilled text when the terminal supports it."""

    try:
        import readline
    except ImportError:
        return input(prompt)

    def prefill() -> None:
        readline.insert_text(initial_text)
        readline.redisplay()

    readline.set_startup_hook(prefill)
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook(None)


def _edit_sql_inline(label: str, sql_text: str) -> str | None:
    """Read replacement SQL from an editable prefilled terminal prompt."""

    initial_sql = _one_line_sql(sql_text)
    print(f"Editing {label}. Edit the prefilled SQL, or press Enter to keep it.")
    try:
        edited = _input_with_prefill(f"{label} SQL> ", initial_sql).strip()
    except EOFError:
        return None

    if not edited or edited == initial_sql:
        return None
    return edited


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


def _prompt_replacement(
    statement: LoggedStatement,
    table_columns,
    *,
    show_statement: bool = True,
) -> LoggedStatement | None:
    """Prompt for optional SQL replacement; return None when skipped."""

    label = statement_label(statement)
    current = statement
    changed = False
    if show_statement:
        print(f"{label}: {current.original_sql_text}")

    while True:
        enter_action = "apply shown SQL" if changed else "delete/skip"
        prompt = (
            f"{label} action [Enter to {enter_action}, :edit for editor, "
            "DELETE or ; to skip]: "
        )
        replacement = input(prompt).strip()

        if not replacement:
            if not changed:
                return None
            return current
        if replacement.upper() == "DELETE" or replacement == ";":
            return None
        if replacement == ":edit":
            replacement = _edit_sql_in_editor(label, current.sql_text) or ""
        if replacement:
            current = _replace_statement_sql(current, replacement, table_columns)
            changed = True
            print(f"{label}: {current.original_sql_text}")


def _statement_labels(
    transactions_by_label: dict[str, LoggedTransaction],
) -> list[str]:
    """Return editable statement labels from transaction values."""

    return list(_statement_lookup(transactions_by_label))


def _statement_lookup(
    transactions_by_label: dict[str, LoggedTransaction],
) -> dict[str, tuple[str, LoggedTransaction, LoggedStatement]]:
    """Return editable statement labels mapped to their transaction context."""

    lookup: dict[str, tuple[str, LoggedTransaction, LoggedStatement]] = {}
    for transaction_label_text, transaction in transactions_by_label.items():
        for statement in transaction.statements:
            lookup[statement_label(statement)] = (
                transaction_label_text,
                transaction,
                statement,
            )
    return lookup


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
) -> None:
    """Show transaction edit commands for the currently visible labels."""

    edit_hint = " or ".join(f"'edit {label};'" for label in labels)
    print(
        f"Use {edit_hint}, 'delete {labels[0]};', "
        f"'insert before {labels[0]};', or 'insert after {labels[0]};' "
        "to edit first."
    )


def _handle_transaction_edit_command(
    raw: str,
    transactions_by_label: dict[str, LoggedTransaction],
    table_columns,
) -> str:
    """Apply edit/delete/insert commands to editable transactions."""

    lookup = _statement_lookup(transactions_by_label)
    first_label = next(iter(lookup), None)
    label_hint = _edit_label_hint(lookup)

    delete_label = _parse_delete_command(raw)
    if delete_label is not None:
        if first_label is None:
            print("No statements left to delete.")
            return "handled"
        if not delete_label:
            print(f"Choose a statement to delete, e.g. 'delete {first_label}'.")
            return "handled"
        found = lookup.get(delete_label)
        if found is None:
            print(f"Unknown label {delete_label}. Valid labels: {label_hint}")
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
            print(f"Unknown label {anchor_label}. Valid labels: {label_hint}")
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
            table_columns,
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
            print(f"Unknown label {edit_label}. Valid labels: {label_hint}")
            return "handled"
        transaction_label_text, transaction, statement = found
        edited_sql = _edit_sql_in_editor(edit_label, statement.original_sql_text)
        if edited_sql is None:
            print(f"Keeping {edit_label} unchanged.")
            return "handled"
        replacement = None
        if edited_sql.strip():
            replacement = _replace_statement_sql(
                statement,
                edited_sql,
                table_columns,
            )
        updated = _replace_transaction_statement(transaction, statement, replacement)
        transactions_by_label[transaction_label_text] = updated
        if replacement is None:
            print(f"{edit_label} deleted.")
        _print_transaction_edit_result(transaction_label_text, updated)
        return "changed"

    return "not_command"


def _prompt_pair_resolution(
    conflict: ConflictPair,
    ours: list[LoggedTransaction],
    theirs: list[LoggedTransaction],
    table_columns,
) -> list[LoggedTransaction]:
    """Prompt for pair conflict resolution."""

    ours_statements = list(ours[conflict.ours_index].statements)
    theirs_statements = list(theirs[conflict.theirs_index].statements)
    ours_label = statement_group_label(ours_statements)
    theirs_label = statement_group_label(theirs_statements)
    ours_order_label = "A"
    theirs_order_label = "B"
    print()
    print(
        f"{_group_title(ours_label, ours_statements)} and "
        f"{_group_title(theirs_label, theirs_statements)} conflict:"
    )
    print(_conflict_messages(conflict))
    _print_statement_group(ours_label, ours_statements)
    _print_statement_group(theirs_label, theirs_statements)
    print(
        f"{ours_order_label} = {ours_label} "
        f"({_group_title(ours_label, ours_statements)})"
    )
    print(
        f"{theirs_order_label} = {theirs_label} "
        f"({_group_title(theirs_label, theirs_statements)})"
    )
    resolved_labels = {
        ours_order_label: _transaction_with_statements(
            ours[conflict.ours_index],
            ours_statements,
        ),
        theirs_order_label: _transaction_with_statements(
            theirs[conflict.theirs_index],
            theirs_statements,
        ),
    }
    valid_edit_labels = _statement_labels(resolved_labels)
    print(
        f"Enter replay order such as '{ours_order_label}; {theirs_order_label};', "
        f"'{theirs_order_label};', or ';' for neither."
    )
    _print_transaction_edit_help(valid_edit_labels)
    while True:
        raw = input("Resolution [Enter for shown order]: ").strip()
        edit_result = _handle_transaction_edit_command(
            raw,
            resolved_labels,
            table_columns,
        )
        if edit_result != "not_command":
            continue

        order = _parse_order(raw, resolved_labels)
        if order is not None:
            return order


def _prompt_standalone_resolution(
    conflict: ConflictPair,
    scope: BranchName,
    ours: list[LoggedTransaction],
    theirs: list[LoggedTransaction],
    table_columns,
) -> list[LoggedStatement] | None:
    """Prompt for standalone transaction failure resolution."""

    transaction = (
        ours[conflict.ours_index]
        if scope == "ours"
        else theirs[conflict.theirs_index]
    )
    label = transaction_label(transaction)
    resolved_transaction = {
        label: _transaction_with_statements(transaction, transaction.statements),
    }
    changed = False
    message = _standalone_conflict_message(conflict, scope)
    print()
    print(f"A standalone replay problem came up while applying {label}:")
    print(message)
    _print_statement_group(label, transaction.statements)

    while True:
        current_transaction = resolved_transaction[label]
        valid_labels = _statement_labels(resolved_transaction)
        if not valid_labels:
            print(f"{label}: all statements deleted; this transaction will be skipped.")
            return []

        if changed:
            print("Press Enter to retry the shown transaction, or ';' to skip it.")
        else:
            print("Press Enter or ';' to skip this transaction.")
        _print_transaction_edit_help(valid_labels)
        raw = input("Resolution: ").strip()

        if not raw:
            return list(current_transaction.statements) if changed else None
        if raw.upper() == "DELETE" or raw == ";":
            return None

        edit_result = _handle_transaction_edit_command(
            raw,
            resolved_transaction,
            table_columns,
        )
        if edit_result == "changed":
            changed = True
            continue
        if edit_result == "handled":
            continue
        print(f"Unknown action. Use 'edit {valid_labels[0]};', DELETE, or ;.")
