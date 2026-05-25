from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from .execution_based_analysis import update_from_has_duplicate_target_rows
from .log_merge import (
    BranchName,
    ConflictCheckContext,
    ConflictPair,
    LoggedStatement,
    UPDATE_FROM_DUPLICATE_TARGET_WARNING,
    acknowledge_replay_warning,
    acknowledgeable_replay_warning,
    build_merge_plan_from_connection,
    load_schema_metadata,
    load_schema_metadata_from_db,
    make_logged_statement,
    replay_statement_plan,
    validate_database,
)
from .session import read_merge_session


def _label(statement: LoggedStatement) -> str:
    prefix = "L" if statement.branch == "ours" else "R"
    return f"{prefix}{statement.branch_index + 1}"


def _conflict_messages(conflict: ConflictPair) -> str:
    return "\n".join(
        f"{statement_conflict.kind}: {statement_conflict.message}"
        for statement_conflict in conflict.conflicts
    )


def _session_logged_statements(
    transactions: list[dict[str, object]],
    branch: BranchName,
    table_columns,
) -> list[LoggedStatement]:
    """Rebuild logged statements from the compact session JSON."""

    statements: list[LoggedStatement] = []
    for transaction in transactions:
        statement_payloads = transaction.get("statements", [])
        if not isinstance(statement_payloads, list):
            continue

        for payload in statement_payloads:
            if not isinstance(payload, dict):
                continue

            to_replay_sql_text = str(payload["to_replay_sql_text"])
            statements.append(
                make_logged_statement(
                    branch=branch,
                    branch_index=int(payload["branch_index"]),
                    log_id=int(payload["log_id"]),
                    transaction_id=int(payload["transaction_id"]),
                    committed_at=str(payload["committed_at"]),
                    sql_text=to_replay_sql_text,
                    original_sql_text=str(payload["original_sql_text"]),
                    is_replay_safe=bool(payload["is_replay_safe"]),
                    replay_block_reason=(
                        None
                        if payload.get("replay_block_reason") is None
                        else str(payload["replay_block_reason"])
                    ),
                    replay_warnings=tuple(
                        str(warning)
                        for warning in payload.get("replay_warnings", ())
                    ),
                    table_columns=table_columns,
                ),
            )

    return statements


def _replace_statement_sql(
    statement: LoggedStatement,
    sql_text: str,
    table_columns,
) -> LoggedStatement:
    """Return a statement with user-edited SQL but the same branch identity."""

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


def _apply_to_planning_db(
    con: sqlite3.Connection,
    statements: Sequence[LoggedStatement],
) -> str | None:
    """Apply statements to the mutable planning database, returning an error."""

    try:
        for statement in statements:
            if not statement.is_replay_safe:
                con.rollback()
                return (
                    statement.replay_block_reason
                    or "statement is unsafe for automatic replay"
                )
            con.execute(statement.sql_text)
            errors = validate_database(con)
            if errors:
                con.rollback()
                return "\n".join(errors)
    except sqlite3.Error as exc:
        con.rollback()
        return str(exc)

    con.commit()
    return None


def _load_planning_db(base_path: Path) -> sqlite3.Connection:
    """Return an in-memory copy of the base database for interactive planning."""

    planning_conn = sqlite3.connect(":memory:")
    try:
        with closing(sqlite3.connect(base_path)) as base_conn:
            base_conn.backup(planning_conn)
        planning_conn.row_factory = sqlite3.Row
        planning_conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        planning_conn.close()
        raise
    return planning_conn


def _branch_statement_replay_error(
    con: sqlite3.Connection,
    statement: LoggedStatement,
) -> str | None:
    """Apply one branch-local statement, returning why it cannot replay."""

    if not statement.is_replay_safe:
        return statement.replay_block_reason or "statement is unsafe for replay"

    try:
        con.execute(statement.sql_text)
        errors = validate_database(con)
        if errors:
            con.rollback()
            return "\n".join(errors)
        con.commit()
        return None
    except sqlite3.Error as exc:
        con.rollback()
        return str(exc)


def _prompt_branch_replay_resolution(
    statement: LoggedStatement,
    table_columns,
    error: str,
) -> LoggedStatement | None:
    """Prompt for one branch-local replay fix."""

    print()
    print(f"{_label(statement)} cannot be replayed automatically: {error}")
    print(f"{_label(statement)}: {statement.original_sql_text}")
    return _prompt_replacement(statement, table_columns, standalone=True)


def _update_from_warning(
    con: sqlite3.Connection,
    statement: LoggedStatement,
    table_columns,
    primary_key_columns,
    key_column_sets,
) -> str | None:
    """Return a warning for nondeterministic UPDATE FROM replay, if present."""

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


def _statement_update_from_warning(
    con: sqlite3.Connection,
    statement: LoggedStatement,
    table_columns,
    primary_key_columns,
    key_column_sets,
    *,
    use_stored_warning: bool,
) -> str | None:
    """Return a stored or freshly computed UPDATE FROM warning."""

    if use_stored_warning and statement.replay_warnings:
        for warning in statement.replay_warnings:
            if warning == UPDATE_FROM_DUPLICATE_TARGET_WARNING:
                return warning
    return _update_from_warning(
        con,
        statement,
        table_columns,
        primary_key_columns,
        key_column_sets,
    )


def _statement_replay_warning(statement: LoggedStatement) -> str | None:
    """Return a stored/unsafe replay warning that needs user acknowledgement."""

    warning = acknowledgeable_replay_warning(statement)
    if warning is not None:
        return warning

    for warning in statement.replay_warnings:
        if warning != UPDATE_FROM_DUPLICATE_TARGET_WARNING:
            return warning
    return None


def _prompt_update_from_warning(
    statement: LoggedStatement,
    table_columns,
    warning: str,
) -> LoggedStatement | None:
    """Prompt after an UPDATE FROM warning; Enter keeps and runs the statement."""

    label = _label(statement)
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

    label = _label(statement)
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


def _resolve_branch_replay_safety(
    base_path: Path,
    branch: BranchName,
    statements: list[LoggedStatement],
) -> list[LoggedStatement]:
    """Resolve wrapper-unsafe or branch-local replay failures before pair checks."""

    branch_label = "local" if branch == "ours" else "remote"
    updated = list(statements)
    accepted_replay_warnings: set[tuple[int, str, str]] = set()
    accepted_update_from_warnings: set[tuple[int, str]] = set()
    branch_changed = False
    while True:
        with closing(_load_planning_db(base_path)) as branch_conn:
            for index, statement in enumerate(updated):
                table_columns, primary_key_columns, key_column_sets = (
                    load_schema_metadata(branch_conn.cursor())
                )
                replay_warning = _statement_replay_warning(statement)
                replay_warning_key = (
                    statement.log_id,
                    statement.sql_text,
                    replay_warning or "",
                )
                if (
                    replay_warning is not None
                    and replay_warning_key not in accepted_replay_warnings
                ):
                    replacement, should_restart = _prompt_replay_warning(
                        statement,
                        table_columns,
                        replay_warning,
                    )
                    if replacement is None:
                        del updated[index]
                        branch_changed = True
                        break
                    updated[index] = statement = replacement
                    accepted_replay_warnings.add(replay_warning_key)
                    if should_restart:
                        branch_changed = True
                        break

                warning_key = (statement.log_id, statement.sql_text)
                if (
                    statement.is_replay_safe
                    and warning_key not in accepted_update_from_warnings
                ):
                    warning = _statement_update_from_warning(
                        branch_conn,
                        statement,
                        table_columns,
                        primary_key_columns,
                        key_column_sets,
                        use_stored_warning=not branch_changed,
                    )
                    if warning is not None:
                        replacement = _prompt_update_from_warning(
                            statement,
                            table_columns,
                            warning,
                        )
                        if replacement is statement:
                            accepted_update_from_warnings.add(warning_key)
                        elif replacement is None:
                            del updated[index]
                            branch_changed = True
                            break
                        else:
                            updated[index] = replacement
                            branch_changed = True
                            break

                error = _branch_statement_replay_error(branch_conn, statement)
                if error is None:
                    continue

                print(f"\nChecking {branch_label} branch stopped at {_label(statement)}.")
                replacement = _prompt_branch_replay_resolution(
                    statement,
                    table_columns,
                    error,
                )
                if replacement is None:
                    del updated[index]
                else:
                    updated[index] = replacement
                branch_changed = True

                # Restart from base so later statements are validated under the
                # edited/deleted branch history.
                break
            else:
                return updated


def _replace_remaining_standalone(
    conflict: ConflictPair,
    scope: BranchName,
    replacement: LoggedStatement | None,
    ours: list[LoggedStatement],
    theirs: list[LoggedStatement],
    frontier_ours_count: int,
    frontier_theirs_count: int,
) -> tuple[list[LoggedStatement], list[LoggedStatement]]:
    """Remove accepted prefix and replace/delete one standalone statement."""

    if scope == "ours":
        next_ours = ours[conflict.ours_index + 1:]
        if replacement is None:
            return next_ours, theirs[frontier_theirs_count:]
        return [replacement, *next_ours], theirs[frontier_theirs_count:]

    next_theirs = theirs[conflict.theirs_index + 1:]
    if replacement is None:
        return ours[frontier_ours_count:], next_theirs
    return ours[frontier_ours_count:], [replacement, *next_theirs]


def _consume_pair_indexes(
    conflict: ConflictPair,
    frontier_ours_count: int,
    frontier_theirs_count: int,
) -> tuple[int, int]:
    """Return remaining-list starts after the user resolves a pair conflict."""

    return (
        max(conflict.ours_index + 1, frontier_ours_count),
        max(conflict.theirs_index + 1, frontier_theirs_count),
    )


def _consume_frontier_counts(
    ours: list[LoggedStatement],
    theirs: list[LoggedStatement],
    frontier_ours_count: int,
    frontier_theirs_count: int,
) -> tuple[list[LoggedStatement], list[LoggedStatement]]:
    """Return remaining statements after accepting a no-conflict frontier."""

    return ours[frontier_ours_count:], theirs[frontier_theirs_count:]


def _parse_order(
    raw: str,
    labels: dict[str, LoggedStatement],
) -> list[LoggedStatement] | None:
    """Parse semicolon-separated labels like L1; R2;."""

    text = raw.strip()
    if not text:
        return list(labels.values())
    tokens = [token.strip().upper() for token in text.split(";") if token.strip()]
    if not tokens:
        return []

    selected: list[LoggedStatement] = []
    for token in tokens:
        statement = labels.get(token)
        if statement is None:
            print(f"Unknown label {token}. Valid labels: {', '.join(labels)}")
            return None
        selected.append(statement)
    return selected


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

    if not edited:
        print("Editor returned empty SQL; keeping the original statement.")
        return None
    return edited


def _prompt_replacement(
    statement: LoggedStatement,
    table_columns,
    *,
    standalone: bool = False,
    show_statement: bool = True,
) -> LoggedStatement | None:
    """Prompt for optional SQL replacement; return None when skipped."""

    label = _label(statement)
    current = statement
    changed = False
    if show_statement:
        print(f"{label}: {current.original_sql_text}")

    while True:
        if standalone:
            enter_action = "apply shown SQL" if changed else "delete/skip"
            prompt = (
                f"{label} action [Enter to {enter_action}, :edit for editor, "
                "DELETE or ; to skip]: "
            )
        else:
            prompt = f"{label} action [Enter to keep shown SQL, :edit for editor]: "
        replacement = input(prompt).strip()

        if not replacement:
            if standalone and not changed:
                return None
            return current
        if standalone and (replacement.upper() == "DELETE" or replacement == ";"):
            return None
        if replacement == ":edit":
            replacement = _edit_sql_in_editor(label, current.sql_text) or ""
        if replacement:
            current = _replace_statement_sql(current, replacement, table_columns)
            changed = True
            print(f"{label}: {current.original_sql_text}")


def _prompt_pair_resolution(
    conflict: ConflictPair,
    ours: list[LoggedStatement],
    theirs: list[LoggedStatement],
    table_columns,
) -> list[LoggedStatement] | None:
    """Prompt for pair conflict resolution."""

    ours_statement = ours[conflict.ours_index]
    theirs_statement = theirs[conflict.theirs_index]
    print()
    print(f"{_label(ours_statement)} and {_label(theirs_statement)} conflict:")
    print(_conflict_messages(conflict))
    print(f"{_label(ours_statement)}: {ours_statement.original_sql_text}")
    print(f"{_label(theirs_statement)}: {theirs_statement.original_sql_text}")
    ours_label = _label(ours_statement)
    theirs_label = _label(theirs_statement)
    resolved_labels = {
        ours_label: ours_statement,
        theirs_label: theirs_statement,
    }
    print(
        f"Enter replay order such as '{ours_label}; {theirs_label};', "
        f"'{theirs_label};', or ';' for neither. "
        f"Use ':edit {ours_label}' or ':edit {theirs_label}' to edit first."
    )
    while True:
        raw = input("Resolution [Enter for shown order]: ").strip()
        if raw.startswith(":edit"):
            edit_parts = raw.split(maxsplit=1)
            if len(edit_parts) != 2:
                print(f"Choose a statement to edit, e.g. ':edit {ours_label}'.")
                continue
            edit_label = edit_parts[1].strip().upper()
            statement = resolved_labels.get(edit_label)
            if statement is None:
                print(f"Unknown label {edit_label}. Valid labels: {', '.join(resolved_labels)}")
                continue
            edited_sql = _edit_sql_in_editor(edit_label, statement.original_sql_text)
            if edited_sql is None:
                print(f"Keeping {edit_label} unchanged.")
                continue
            resolved_labels[edit_label] = _replace_statement_sql(
                statement,
                edited_sql,
                table_columns,
            )
            print(f"{edit_label}: {edited_sql}")
            continue

        order = _parse_order(raw, resolved_labels)
        if order is not None:
            return order


def _prompt_standalone_resolution(
    conflict: ConflictPair,
    ours: list[LoggedStatement],
    theirs: list[LoggedStatement],
    table_columns,
) -> LoggedStatement | None:
    """Prompt for standalone replay failure resolution."""

    scope = conflict.conflicts[0].scope
    if scope not in {"ours", "theirs"}:
        return None

    statement = (
        ours[conflict.ours_index]
        if scope == "ours"
        else theirs[conflict.theirs_index]
    )
    message = conflict.conflicts[0].message
    print()
    print(
        f"An {conflict.conflicts[0].kind} error came up while applying "
        f"{_label(statement)}: {message}"
    )
    print(f"{_label(statement)}: {statement.original_sql_text}")
    return _prompt_replacement(
        statement,
        table_columns,
        standalone=True,
        show_statement=False,
    )


def resolve_session(session_path: str | Path) -> int:
    """Run a simple terminal resolver for one merge-session JSON file."""

    session = read_merge_session(session_path)
    if session.get("status") == "not_applicable":
        print(session.get("message", "database is not applicable"))
        return 1

    paths = session["paths"]
    base_path = Path(paths["base"])
    merged_path = Path(paths["merged"])

    base_transaction_id = int(session["base_transaction_id"])
    table_columns, primary_key_columns, key_column_sets = load_schema_metadata_from_db(
        base_path,
    )
    ours = _session_logged_statements(
        session.get("ours_transactions", []),
        "ours",
        table_columns,
    )
    theirs = _session_logged_statements(
        session.get("theirs_transactions", []),
        "theirs",
        table_columns,
    )
    ours = _resolve_branch_replay_safety(base_path, "ours", ours)
    theirs = _resolve_branch_replay_safety(base_path, "theirs", theirs)

    resolved_plan: list[LoggedStatement] = []
    with closing(_load_planning_db(base_path)) as planning_conn:
        while True:
            table_columns, primary_key_columns, key_column_sets = (
                load_schema_metadata(planning_conn.cursor())
            )
            plan = build_merge_plan_from_connection(
                planning_conn,
                ":memory:",
                base_transaction_id,
                ours,
                theirs,
                table_columns,
                primary_key_columns,
                key_column_sets,
            )

            prefix = plan.statement_plan
            error = _apply_to_planning_db(planning_conn, prefix)
            if error is not None:
                print(f"Failed to apply accepted prefix: {error}")
                return 1
            resolved_plan.extend(prefix)

            if plan.status == "clean":
                break

            conflict = plan.selected.next_conflict
            if conflict is None:
                ours, theirs = _consume_frontier_counts(
                    ours,
                    theirs,
                    plan.selected.ours_count,
                    plan.selected.theirs_count,
                )
                if not prefix and (ours or theirs):
                    print("Conflict search stopped without making progress.")
                    return 1
                if not ours and not theirs:
                    break
                continue

            if plan.selected.scope in {"ours", "theirs"}:
                replacement = _prompt_standalone_resolution(
                    conflict,
                    ours,
                    theirs,
                    table_columns,
                )
                ours, theirs = _replace_remaining_standalone(
                    conflict,
                    plan.selected.scope,
                    replacement,
                    ours,
                    theirs,
                    plan.selected.ours_count,
                    plan.selected.theirs_count,
                )
            else:
                while True:
                    resolution = None
                    while resolution is None:
                        resolution = _prompt_pair_resolution(
                            conflict,
                            ours,
                            theirs,
                            table_columns,
                        )

                    error = _apply_to_planning_db(planning_conn, resolution)
                    if error is None:
                        resolved_plan.extend(resolution)
                        ours_start, theirs_start = _consume_pair_indexes(
                            conflict,
                            plan.selected.ours_count,
                            plan.selected.theirs_count,
                        )
                        ours = ours[ours_start:]
                        theirs = theirs[theirs_start:]
                        break
                    print(f"Resolution failed: {error}")

        replay = replay_statement_plan(base_path, merged_path, resolved_plan)
        if not replay.ok:
            error = replay.failure.error if replay.failure else "unknown replay failure"
            print(f"Failed to write resolved database: {error}")
            return 1
    print(f"Resolved SQLite merge written to {merged_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite reconcile terminal mergetool")
    parser.add_argument("session", help="Path to sqlite-reconcile session JSON")
    args = parser.parse_args(argv)
    return resolve_session(args.session)


if __name__ == "__main__":
    raise SystemExit(main())
