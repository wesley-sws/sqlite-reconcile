#!/usr/bin/env python3
"""Run selected correctness-scenario tests and write JSON evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "evaluation" / "results"


@dataclass(frozen=True)
class CorrectnessScenario:
    name: str
    expected: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class CorrectnessResult:
    scenario: str
    expected: str
    actual: str
    evidence: tuple[str, ...]
    status: Literal["pass", "fail"]


CORRECTNESS_SCENARIOS: tuple[CorrectnessScenario, ...] = (
    CorrectnessScenario(
        "Clean non-overlapping transactions",
        "The fixed-order merge applies both branch heads without prompting.",
        (
            "tests/merge/test_terminal_mergetool.py::"
            "test_check_accept_current_applies_and_pops_fixed_order_heads",
        ),
    ),
    CorrectnessScenario(
        "Write-write conflict",
        "Overlapping updates/deletes are reported as write-write conflicts.",
        (
            "tests/merge/test_static_analysis.py::"
            "test_static_analysis_flags_update_same_column_write_write",
            "tests/merge/test_log_merge.py::"
            "test_remaining_conflict_uses_current_write_probe_before_suffix",
        ),
    ),
    CorrectnessScenario(
        "Write-read conflict",
        "A later statement reading values changed by the current transaction is reported.",
        (
            "tests/merge/test_static_analysis.py::test_static_analysis_flags_write_read",
            "tests/merge/test_log_merge.py::"
            "test_remaining_conflict_uses_rolling_control_state_for_later_write_read",
        ),
    ),
    CorrectnessScenario(
        "Integrity conflict",
        "Constraint failures that only appear after branch replay are reported.",
        (
            "tests/merge/test_log_merge.py::"
            "test_remaining_conflict_reports_later_remote_integrity_conflict",
        ),
    ),
    CorrectnessScenario(
        "Branch-local replay problem",
        "Unsafe or failing branch-local transactions are resolved before pair scanning.",
        (
            "tests/merge/test_log_merge.py::"
            "test_current_unsafe_replay_statement_is_resolved_before_pair_scan",
            "tests/merge/test_terminal_mergetool.py::"
            "test_branch_replay_safety_can_accept_nondeterministic_warning",
        ),
    ),
    CorrectnessScenario(
        "OR IGNORE / OR REPLACE reviewable conflict",
        "Strict replay reports conflict-resolution behavior without hard-failing original SQL.",
        (
            "tests/merge/test_log_merge.py::"
            "test_remaining_conflict_reports_constraint_resolution_during_integrity_scan",
            "tests/merge/test_log_merge.py::"
            "test_remaining_conflict_suppresses_already_active_constraint_resolution",
        ),
    ),
    CorrectnessScenario(
        "Omitted INTEGER PRIMARY KEY",
        "Implicit rowid assignment is reported as a direct conflict.",
        (
            "tests/merge/test_static_analysis.py::"
            "test_static_analysis_implicit_key_insert_conflicts_with_other_implicit_insert",
            "tests/merge/test_static_analysis.py::"
            "test_static_analysis_implicit_key_insert_conflicts_with_key_update",
        ),
    ),
    CorrectnessScenario(
        "UPDATE FROM duplicate source rows",
        "Duplicate source-row ambiguity is warned about and not used to clear conflicts.",
        (
            "tests/merge/test_terminal_mergetool.py::"
            "test_branch_replay_safety_warns_for_update_from_duplicates",
        ),
    ),
    CorrectnessScenario(
        "Foreign-key cascade static metadata",
        "Cascade-hidden reads/writes are included in static metadata and filters.",
        (
            "tests/merge/test_static_analysis.py::"
            "test_cascade_metadata_is_stored_on_statement_and_transaction_metadata",
            "tests/merge/test_static_analysis.py::"
            "test_static_analysis_flags_cascade_write_read_overlap",
        ),
    ),
    CorrectnessScenario(
        "Logged SQL parse/rewrite limitation",
        "Unparseable logged SQL or limited rewrites are handled conservatively.",
        (
            "tests/merge/test_log_merge.py::"
            "test_make_logged_statement_marks_unparseable_sql_unsafe",
            "tests/merge/test_terminal_mergetool.py::"
            "test_control_rewrite_uses_strict_insert_for_upsert_statement_metadata",
        ),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for generated JSON artifacts",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = run_correctness(args.output_dir)
    return 1 if any(result.status == "fail" for result in results) else 0


def run_correctness(output_dir: Path) -> list[CorrectnessResult]:
    results: list[CorrectnessResult] = []
    for scenario in CORRECTNESS_SCENARIOS:
        command = [sys.executable, "-m", "pytest", "-q", *scenario.evidence]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        status: Literal["pass", "fail"] = "pass" if completed.returncode == 0 else "fail"
        actual = (
            "Selected tests passed."
            if status == "pass"
            else _last_lines(completed.stdout, line_count=8)
        )
        results.append(
            CorrectnessResult(
                scenario=scenario.name,
                expected=scenario.expected,
                actual=actual,
                evidence=scenario.evidence,
                status=status,
            )
        )

    _write_json(output_dir / "correctness_scenarios.json", results)
    return results


def _write_json(path: Path, results: list[CorrectnessResult]) -> None:
    path.write_text(
        json.dumps([asdict(result) for result in results], indent=2) + "\n",
        encoding="utf-8",
    )


def _last_lines(text: str, *, line_count: int) -> str:
    lines = text.strip().splitlines()
    return "\n".join(lines[-line_count:])


if __name__ == "__main__":
    raise SystemExit(main())
