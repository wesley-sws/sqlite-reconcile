#!/usr/bin/env python3
"""Run the end-to-end Git mergetool smoke test."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "evaluation" / "results"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sqlite_wrapper import SQLiteWrapper  # noqa: E402


@dataclass(frozen=True)
class SmokeResult:
    status: Literal["pass", "fail"]
    final_name: str | None
    git_status: str
    merge_output_excerpt: str
    mergetool_output_excerpt: str
    temp_repo: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for generated JSON artifacts",
    )
    parser.add_argument(
        "--keep-smoke-repo",
        action="store_true",
        help="keep the temporary Git smoke-test repository after the script exits",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = run_smoke(args.output_dir, keep_repo=args.keep_smoke_repo)
    return 0 if result.status == "pass" else 1


def run_smoke(output_dir: Path, *, keep_repo: bool) -> SmokeResult:
    temp_dir = tempfile.TemporaryDirectory(prefix="sqlite_reconcile_smoke_")
    repo_path = Path(temp_dir.name)
    try:
        result = _run_smoke_in_repo(repo_path)
        if keep_repo:
            kept_path = output_dir / "smoke_repo"
            if kept_path.exists():
                shutil.rmtree(kept_path)
            shutil.copytree(repo_path, kept_path)
            result = SmokeResult(
                status=result.status,
                final_name=result.final_name,
                git_status=result.git_status,
                merge_output_excerpt=result.merge_output_excerpt,
                mergetool_output_excerpt=result.mergetool_output_excerpt,
                temp_repo=str(kept_path),
            )
        else:
            result = SmokeResult(
                status=result.status,
                final_name=result.final_name,
                git_status=result.git_status,
                merge_output_excerpt=result.merge_output_excerpt,
                mergetool_output_excerpt=result.mergetool_output_excerpt,
                temp_repo="removed after run; pass --keep-smoke-repo to keep it",
            )
    finally:
        temp_dir.cleanup()

    _write_json(output_dir / "git_smoke.json", result)
    return result


def _run_smoke_in_repo(repo_path: Path) -> SmokeResult:
    app_db = repo_path / "app.db"
    run = lambda *args, input_text=None, check=True: _run(  # noqa: E731
        list(args),
        cwd=repo_path,
        input_text=input_text,
        check=check,
    )

    run("git", "init", "-b", "main")
    run("git", "config", "user.name", "SQLite Reconcile Smoke")
    run("git", "config", "user.email", "smoke@example.com")
    run(
        "git",
        "config",
        "mergetool.sqlite-reconcile.cmd",
        (
            f'"{sys.executable}" "{SRC_ROOT / "sqlite-reconcile-mergetool"}" '
            '"$BASE" "$LOCAL" "$REMOTE" "$MERGED"'
        ),
    )
    run("git", "config", "mergetool.sqlite-reconcile.trustExitCode", "true")
    run("git", "config", "mergetool.prompt", "false")
    run("git", "config", "mergetool.keepBackup", "false")
    (repo_path / ".gitattributes").write_text("*.db binary\n", encoding="utf-8")

    _create_smoke_db(app_db)
    run("git", "add", ".gitattributes", "app.db")
    run("git", "commit", "-m", "base")

    run("git", "checkout", "-b", "remote")
    _wrapper_execute(app_db, "UPDATE users SET name = 'Alice Remote' WHERE id = 1")
    run("git", "add", "app.db")
    run("git", "commit", "-m", "remote update")

    run("git", "checkout", "main")
    _wrapper_execute(app_db, "UPDATE users SET name = 'Alice Local' WHERE id = 1")
    run("git", "add", "app.db")
    run("git", "commit", "-m", "local update")

    merge = run("git", "merge", "remote", check=False)
    mergetool = run(
        "git",
        "mergetool",
        "--tool=sqlite-reconcile",
        "--",
        "app.db",
        input_text="delete R1;\n",
        check=False,
    )
    final_name = _read_smoke_user_name(app_db)
    status = run("git", "status", "--porcelain", check=False).stdout.strip()
    passed = (
        merge.returncode != 0
        and mergetool.returncode == 0
        and final_name == "Alice Local"
        and "UU app.db" not in status
    )
    return SmokeResult(
        status="pass" if passed else "fail",
        final_name=final_name,
        git_status=status,
        merge_output_excerpt=_last_lines(merge.stdout, line_count=8),
        mergetool_output_excerpt=_last_lines(mergetool.stdout, line_count=12),
        temp_repo=str(repo_path),
    )


def _create_smoke_db(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        con.commit()
    _wrapper_execute(path, "INSERT INTO users (id, name) VALUES (1, 'Alice Base')")


def _wrapper_execute(path: Path, sql: str) -> None:
    wrapper = SQLiteWrapper(path)
    try:
        wrapper.execute(sql)
    finally:
        wrapper.close()


def _read_smoke_user_name(path: Path) -> str | None:
    with sqlite3.connect(path) as con:
        row = con.execute("SELECT name FROM users WHERE id = 1").fetchone()
    return None if row is None else str(row[0])


def _run(
    command: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stdout}"
        )
    return completed


def _write_json(path: Path, result: SmokeResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")


def _last_lines(text: str, *, line_count: int) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-line_count:])


if __name__ == "__main__":
    raise SystemExit(main())
