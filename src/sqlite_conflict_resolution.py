"""
Temporary SQLite conflict-resolution compatibility helpers.

This module is an AI-assisted shim for SQLite syntax that the sqlglot version
used by the project does not parse or render cleanly yet, such as UPDATE OR ...,
REPLACE INTO, and INSERT ... ON CONFLICT UPSERT clauses. The code deliberately
does only shallow top-level rewriting so sqlglot can still handle the main SQL
AST. Once these SQLite forms are represented reliably in sqlglot's AST, this
module should be replaced by AST-based stripping/restoration instead of manual
top-level scanning.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


CONFLICT_ALGORITHMS = "ROLLBACK|ABORT|FAIL|IGNORE|REPLACE"
REVIEWABLE_ALGORITHMS = "IGNORE|REPLACE"


INSERT_OR_PATTERN = re.compile(
    rf"(INSERT)\s+OR\s+({CONFLICT_ALGORITHMS})\s+",
    flags=re.IGNORECASE,
)
UPDATE_OR_PATTERN = re.compile(
    rf"(UPDATE)\s+OR\s+({CONFLICT_ALGORITHMS})\s+",
    flags=re.IGNORECASE,
)
REVIEWABLE_INSERT_OR_PATTERN = re.compile(
    rf"(INSERT)\s+OR\s+({REVIEWABLE_ALGORITHMS})\s+",
    flags=re.IGNORECASE,
)
REVIEWABLE_UPDATE_OR_PATTERN = re.compile(
    rf"(UPDATE)\s+OR\s+({REVIEWABLE_ALGORITHMS})\s+",
    flags=re.IGNORECASE,
)
REPLACE_INTO_PATTERN = re.compile(
    r"(REPLACE)(\s+INTO\s+)",
    flags=re.IGNORECASE,
)
UPDATE_KEYWORD_PATTERN = re.compile(
    r"(UPDATE)\s+",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class SQLiteConflictResolution:
    """Top-level SQLite conflict-resolution syntax found before parsing."""

    statement_kind: Literal["insert", "update"]
    algorithm: str


@dataclass(frozen=True)
class CompatibleSQL:
    """SQL text that sqlglot can parse, plus syntax stripped to get there."""

    sql: str
    conflict_resolution: SQLiteConflictResolution | None = None
    stripped_upsert: bool = False


@dataclass(frozen=True)
class StrictReplayRewrite:
    """Strict SQL used only to check whether conflict resolution was needed."""

    sql: str
    label: str


def normalize_sql_for_sqlglot(sql: str) -> CompatibleSQL:
    """Return a sqlglot-compatible SQL form and the SQLite syntax it normalized."""

    resolution = _insert_or_resolution(sql)
    normalized = sql

    update_normalized, update_resolution = _remove_top_level_or_clause(
        normalized,
        UPDATE_OR_PATTERN,
    )
    if update_resolution is not None:
        normalized = update_normalized
        resolution = update_resolution

    if resolution is None:
        replace_normalized, replace_resolution = _normalize_top_level_replace_into(
            normalized,
        )
        if replace_resolution is not None:
            normalized = replace_normalized
            resolution = replace_resolution

    without_upsert = strip_top_level_upsert(normalized)
    stripped_upsert = False
    if without_upsert is not None and without_upsert != normalized:
        normalized = without_upsert
        stripped_upsert = True

    return CompatibleSQL(
        sql=normalized,
        conflict_resolution=resolution,
        stripped_upsert=stripped_upsert,
    )


def parse_compatible_sql(sql: str) -> str:
    """Return SQL normalized enough for sqlglot to parse SQLite conflict syntax."""

    return normalize_sql_for_sqlglot(sql).sql


def strict_conflict_resolution_rewrite(sql: str) -> StrictReplayRewrite | None:
    """Return stricter SQL when SQLite conflict resolution can hide a violation."""

    rewritten = sql
    labels: list[str] = []

    rewritten, label = _remove_top_level_or_clause(
        rewritten,
        REVIEWABLE_INSERT_OR_PATTERN,
    )
    if label is not None:
        labels.append(_resolution_label(label))

    rewritten, label = _remove_top_level_or_clause(
        rewritten,
        REVIEWABLE_UPDATE_OR_PATTERN,
    )
    if label is not None:
        labels.append(_resolution_label(label))

    rewritten, label = _strip_top_level_replace_into(rewritten)
    if label is not None:
        labels.append("REPLACE INTO")

    without_upsert = strip_top_level_upsert(rewritten)
    if without_upsert is not None and without_upsert != rewritten:
        rewritten = without_upsert
        labels.append("UPSERT")

    if labels:
        return StrictReplayRewrite(rewritten, " / ".join(labels))

    return None


def restore_update_conflict_resolution(
    sql: str,
    resolution: SQLiteConflictResolution | None,
) -> str:
    """Reinsert UPDATE OR ... into SQL rendered from a normalized sqlglot tree."""

    if resolution is None or resolution.statement_kind != "update":
        return sql

    match = _top_level_pattern_match(sql, UPDATE_KEYWORD_PATTERN)
    if match is None:
        return sql

    return (
        sql[: match.start()]
        + f"{match.group(1)} OR {resolution.algorithm} "
        + sql[match.end() :]
    )


def strip_top_level_upsert(sql: str) -> str | None:
    """Remove a top-level SQLite UPSERT clause from an INSERT statement."""

    if not _starts_with_insert_like_statement(sql):
        return None

    index = _find_top_level_on_conflict(sql)
    if index is None:
        return None

    stripped = sql[:index].rstrip()
    return stripped + ";" if sql.rstrip().endswith(";") else stripped


def _remove_top_level_or_clause(
    sql: str,
    pattern: re.Pattern[str],
) -> tuple[str, SQLiteConflictResolution | None]:
    match = _top_level_pattern_match(sql, pattern)
    if match is None:
        return sql, None

    rewritten = sql[: match.start()] + f"{match.group(1)} " + sql[match.end() :]
    statement_kind: Literal["insert", "update"] = (
        "insert" if match.group(1).upper() == "INSERT" else "update"
    )
    resolution = SQLiteConflictResolution(
        statement_kind=statement_kind,
        algorithm=match.group(2).upper(),
    )
    return rewritten, resolution


def _resolution_label(resolution: SQLiteConflictResolution) -> str:
    return f"{resolution.statement_kind.upper()} OR {resolution.algorithm}"


def _insert_or_resolution(sql: str) -> SQLiteConflictResolution | None:
    match = _top_level_pattern_match(sql, INSERT_OR_PATTERN)
    if match is None:
        return None
    return SQLiteConflictResolution(
        statement_kind="insert",
        algorithm=match.group(2).upper(),
    )


def _normalize_top_level_replace_into(
    sql: str,
) -> tuple[str, SQLiteConflictResolution | None]:
    match = _top_level_pattern_match(sql, REPLACE_INTO_PATTERN)
    if match is None:
        return sql, None

    rewritten = (
        sql[: match.start()]
        + f"INSERT OR REPLACE{match.group(2)}"
        + sql[match.end() :]
    )
    return rewritten, SQLiteConflictResolution(
        statement_kind="insert",
        algorithm="REPLACE",
    )


def _strip_top_level_replace_into(
    sql: str,
) -> tuple[str, SQLiteConflictResolution | None]:
    match = _top_level_pattern_match(sql, REPLACE_INTO_PATTERN)
    if match is None:
        return sql, None

    rewritten = sql[: match.start()] + f"INSERT{match.group(2)}" + sql[match.end() :]
    return rewritten, SQLiteConflictResolution(
        statement_kind="insert",
        algorithm="REPLACE",
    )


def _starts_with_insert_like_statement(sql: str) -> bool:
    stripped = sql.lstrip()
    upper = stripped.upper()
    return upper.startswith("INSERT") or upper.startswith("WITH")


def _find_top_level_on_conflict(sql: str) -> int | None:
    for index in _top_level_keyword_indexes(sql):
        if (
            _keyword_at(sql, index, "ON")
            and _next_keyword_after(sql, index + 2) == "CONFLICT"
        ):
            return index
    return None


def _top_level_pattern_match(
    sql: str,
    pattern: re.Pattern[str],
) -> re.Match[str] | None:
    for index in _top_level_keyword_indexes(sql):
        match = pattern.match(sql, index)
        if match is not None:
            return match
    return None


def _top_level_keyword_indexes(sql: str):
    depth = 0
    index = 0
    quote: str | None = None
    bracket_quote = False
    line_comment = False
    block_comment = False

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue

        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue

        if quote is not None:
            if bracket_quote and char == "]":
                quote = None
                bracket_quote = False
            elif not bracket_quote and char == quote:
                if next_char == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char == "-" and next_char == "-":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "[":
            quote = char
            bracket_quote = True
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(depth - 1, 0)
            index += 1
            continue

        if (
            depth == 0
            and (char.isalpha() or char == "_")
            and (index == 0 or not _identifier_char(sql[index - 1]))
        ):
            yield index
            while index < len(sql) and _identifier_char(sql[index]):
                index += 1
            continue

        index += 1


def _keyword_at(sql: str, index: int, keyword: str) -> bool:
    end = index + len(keyword)
    return (
        sql[index:end].upper() == keyword
        and (index == 0 or not _identifier_char(sql[index - 1]))
        and (end >= len(sql) or not _identifier_char(sql[end]))
    )


def _next_keyword_after(sql: str, index: int) -> str | None:
    while index < len(sql) and sql[index].isspace():
        index += 1

    end = index
    while end < len(sql) and _identifier_char(sql[end]):
        end += 1

    if end == index:
        return None
    return sql[index:end].upper()


def _identifier_char(char: str) -> bool:
    return char.isalnum() or char == "_"
