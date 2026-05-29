from __future__ import annotations

import json
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .log_merge import (
    LoggedStatement,
    LoggedTransaction,
    MergeNotApplicableError,
    ReplayResult,
)


SESSION_VERSION = 1


def session_artifact_dir(session_path: str | Path) -> Path:
    """Return the directory used for stable merge-session artifacts."""

    path = Path(session_path)
    return path.with_name(f"{path.name}.files")


def _statement_payload(statement: LoggedStatement) -> dict[str, object]:
    """Return compact statement data for the terminal resolver."""

    return {
        "branch_index": statement.branch_index,
        "log_id": statement.log_id,
        "transaction_id": statement.transaction_id,
        "original_sql_text": statement.original_sql_text,
        "to_replay_sql_text": statement.to_replay_sql_text,
        "is_replay_safe": statement.is_replay_safe,
        "replay_block_reason": statement.replay_block_reason,
        "replay_warnings": list(statement.replay_warnings),
    }


def _transactions_payload(
    transactions: list[LoggedTransaction],
) -> list[dict[str, object]]:
    """Return compact transaction data for UI display."""

    return [
        {
            "transaction_id": transaction.transaction_id,
            "committed_at": transaction.committed_at,
            "statements": [
                _statement_payload(statement)
                for statement in transaction.statements
            ],
        }
        for transaction in transactions
    ]


def _json_default(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_merge_session(
    session_path: str | Path,
    *,
    status: str,
    base_db_path: str | Path,
    merged_db_path: str | Path,
    base_transaction_id: int,
    ours: list[LoggedTransaction],
    theirs: list[LoggedTransaction],
    replay: ReplayResult | None = None,
) -> None:
    """Write a compact resolver handoff file plus a stable base snapshot."""

    path = Path(session_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)

    artifacts = session_artifact_dir(path)
    if artifacts.exists():
        shutil.rmtree(artifacts)
    artifacts.mkdir(parents=True)

    base_snapshot_path = artifacts / "base.db"
    shutil.copy2(base_db_path, base_snapshot_path)

    payload: dict[str, Any] = {
        "version": SESSION_VERSION,
        "status": status,
        "paths": {
            "base": str(base_snapshot_path),
            "merged": str(merged_db_path),
        },
        "base_transaction_id": base_transaction_id,
        "replay": replay,
        "ours_transactions": _transactions_payload(ours),
        "theirs_transactions": _transactions_payload(theirs),
    }
    path.write_text(
        json.dumps(payload, default=_json_default, indent=2),
        encoding="utf-8",
    )


def write_not_applicable_session(
    session_path: str | Path,
    error: MergeNotApplicableError,
) -> None:
    """Write a resolver handoff file for databases without merge logs."""

    path = Path(session_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": SESSION_VERSION,
        "status": "not_applicable",
        "message": str(error),
        "database": error.db_path,
        "role": error.role,
        "missing_tables": error.missing_tables,
        "details": error.details,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_merge_session(session_path: str | Path) -> dict[str, Any]:
    """Read a merge session JSON file."""

    return json.loads(Path(session_path).read_text(encoding="utf-8"))
