"""Invoke EvalFix and normalize its JSON output.

Expected CLI schemas
--------------------
``evalfix run <dir> --json``::

    {
      "total": 40,
      "passed": 38,
      "failed": 2,
      "score": 0.95,
      "results": [
        {
          "id": "refund_expired_warranty",
          "pass": false,
          "grader": "semantic",
          "input": "...",
          "expected": "...",
          "actual": "...",
          "reason": "..."
        }
      ]
    }

``evalfix fix <dir> --yes --json``::

    {
      "diagnosis": "...",
      "root_cause": "prompt",
      "confidence": 0.91,
      "diff": "--- prompt.txt\\n+++ prompt.txt\\n...",
      "before": {"passed": 38, "total": 40},
      "after": {"passed": 40, "total": 40},
      "previously_passing_intact": true
    }

The upstream CLI is treated as a black box and may evolve. This is the only
module allowed to inspect raw CLI output. Parsing is deliberately defensive:
unknown or missing fields become ``None``/empty values plus warnings instead
of propagating parsing exceptions into action orchestration.

Future adapter note: a promptfoo implementation can expose the same
``RunResult`` and ``FixResult`` types without changing comparison or comments.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import TypeGuard


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Normalized result for one eval case."""

    case_id: str
    passed: bool | None
    grader: str | None = None
    input_text: str | None = None
    expected: str | None = None
    actual: str | None = None
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    """Normalized output from ``evalfix run``."""

    total: int
    passed: int
    failed: int
    score: float | None
    cases: tuple[CaseResult, ...]
    warnings: tuple[str, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ScoreSummary:
    """Pass counts reported before or after a proposed fix."""

    passed: int | None
    total: int | None


@dataclass(frozen=True, slots=True)
class FixResult:
    """Normalized output from ``evalfix fix``."""

    diagnosis: str | None
    root_cause: str | None
    confidence: float | None
    diff: str | None
    before: ScoreSummary
    after: ScoreSummary
    previously_passing_intact: bool | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CliExecution:
    """Raw process result retained only inside the adapter boundary."""

    command: tuple[str, ...]
    return_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


def invoke_run(
    project_dir: Path,
    *,
    timeout_seconds: float,
) -> CliExecution:
    """Execute ``evalfix run`` without interpreting its exit status."""

    return _invoke(
        ("evalfix", "run", str(project_dir), "--json"),
        timeout_seconds=timeout_seconds,
    )


def invoke_fix(
    project_dir: Path,
    *,
    timeout_seconds: float,
) -> CliExecution:
    """Execute ``evalfix fix`` on a caller-provided throwaway directory.

    Older EvalFix releases do not expose ``fix --json``. When that exact
    option is unsupported, run the legacy command, compute the prompt diff,
    and verify the resulting prompt with ``evalfix run --json``. The fallback
    still returns the normalized JSON shape consumed by ``parse_fix_output``.
    """

    json_execution = _invoke(
        ("evalfix", "fix", str(project_dir), "--yes", "--json"),
        timeout_seconds=timeout_seconds,
    )
    if not _json_option_unsupported(json_execution):
        return json_execution
    return _invoke_legacy_fix(project_dir, timeout_seconds=timeout_seconds)


def parse_run_output(raw_output: str, *, max_cases: int | None = None) -> RunResult:
    """Parse EvalFix run output, returning warnings for every degraded field."""

    payload, decode_warning = _decode_object(raw_output)
    warnings: list[str] = []
    if decode_warning:
        warnings.append(decode_warning)

    raw_results = payload.get("results")
    result_items: Sequence[object]
    if _is_sequence(raw_results):
        result_items = raw_results
    else:
        result_items = ()
        if payload and raw_results is not None:
            warnings.append("'results' was not a list; no case details were parsed.")
        elif payload:
            warnings.append("Missing 'results'; no case details were parsed.")

    cases: list[CaseResult] = []
    for index, item in enumerate(result_items):
        if not isinstance(item, Mapping):
            warnings.append(f"Result at index {index} was not an object and was skipped.")
            continue
        normalized = _string_key_mapping(item)
        case_id = _first_text(normalized, "id", "test_id")
        if not case_id:
            case_id = f"unknown_case_{index + 1}"
            warnings.append(f"Result at index {index} had no case id; using '{case_id}'.")
        case_passed = _first_bool(normalized, "pass", "passed")
        if case_passed is None:
            warnings.append(f"Case '{case_id}' had no boolean pass value.")
        cases.append(
            CaseResult(
                case_id=case_id,
                passed=case_passed,
                grader=_as_text(normalized.get("grader")),
                input_text=_as_text(normalized.get("input")),
                expected=_as_text(normalized.get("expected")),
                actual=_as_text(normalized.get("actual")),
                reason=_first_text(normalized, "reason", "judge_reasoning", "error"),
                error=_as_text(normalized.get("error")),
            )
        )

    truncated = False
    if max_cases is not None and max_cases >= 0 and len(cases) > max_cases:
        cases = cases[:max_cases]
        truncated = True
        warnings.append(f"Case details truncated to max_cases={max_cases}.")

    known_passes = sum(case.passed is True for case in cases)
    known_failures = sum(case.passed is False for case in cases)
    reported_total = _first_nonnegative_int(payload, "total", "total_count")
    reported_passed = _first_nonnegative_int(payload, "passed", "pass_count")
    reported_failed = _first_nonnegative_int(payload, "failed", "fail_count")

    total = reported_total if reported_total is not None else len(cases)
    passed = reported_passed if reported_passed is not None else known_passes
    failed = reported_failed if reported_failed is not None else known_failures

    if payload and reported_total is None:
        warnings.append("Missing or invalid 'total'; derived it from parsed cases.")
    if payload and reported_passed is None:
        warnings.append("Missing or invalid 'passed'; derived it from parsed cases.")
    if payload and reported_failed is None:
        warnings.append("Missing or invalid 'failed'; derived it from parsed cases.")

    score = _first_float(payload, "score", "avg_score")
    if score is None and total > 0:
        score = passed / total
        if payload:
            warnings.append("Missing or invalid 'score'; derived it from pass counts.")

    return RunResult(
        total=total,
        passed=passed,
        failed=failed,
        score=score,
        cases=tuple(cases),
        warnings=tuple(warnings),
        truncated=truncated,
    )


def parse_fix_output(raw_output: str) -> FixResult:
    """Parse EvalFix fix output without assuming every field is present."""

    payload, decode_warning = _decode_object(raw_output)
    warnings: list[str] = []
    if decode_warning:
        warnings.append(decode_warning)

    before = _parse_score_summary(payload.get("before"), "before", warnings)
    after = _parse_score_summary(payload.get("after"), "after", warnings)

    diff = _as_text(payload.get("diff"))
    if payload and diff is None:
        warnings.append("Missing proposed prompt diff.")

    return FixResult(
        diagnosis=_as_text(payload.get("diagnosis")),
        root_cause=_as_text(payload.get("root_cause")),
        confidence=_as_float(payload.get("confidence")),
        diff=diff,
        before=before,
        after=after,
        previously_passing_intact=_as_bool(payload.get("previously_passing_intact")),
        warnings=tuple(warnings),
    )


def _invoke(command: tuple[str, ...], *, timeout_seconds: float) -> CliExecution:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        return CliExecution(
            command=command,
            return_code=None,
            stdout=_decode_stream(error.stdout),
            stderr=_decode_stream(error.stderr),
            timed_out=True,
        )
    except OSError as error:
        return CliExecution(
            command=command,
            return_code=None,
            stdout="",
            stderr=str(error),
        )

    return CliExecution(
        command=command,
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _invoke_legacy_fix(
    project_dir: Path,
    *,
    timeout_seconds: float,
) -> CliExecution:
    prompt_path = project_dir / "prompt.txt"
    try:
        before_prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as error:
        return CliExecution(
            command=("evalfix", "fix", str(project_dir), "--yes"),
            return_code=None,
            stdout="",
            stderr=str(error),
        )

    legacy_execution = _invoke(
        ("evalfix", "fix", str(project_dir), "--yes"),
        timeout_seconds=timeout_seconds,
    )
    if legacy_execution.timed_out or legacy_execution.return_code not in {0, 1}:
        return legacy_execution

    try:
        after_prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as error:
        return CliExecution(
            command=legacy_execution.command,
            return_code=None,
            stdout=legacy_execution.stdout,
            stderr=str(error),
        )
    if before_prompt == after_prompt:
        return legacy_execution

    verification = invoke_run(project_dir, timeout_seconds=timeout_seconds)
    if verification.timed_out or verification.return_code not in {0, 1}:
        return verification
    after_result = parse_run_output(verification.stdout)
    if not after_result.cases and after_result.total == 0:
        return verification

    diff = "".join(
        unified_diff(
            before_prompt.splitlines(keepends=True),
            after_prompt.splitlines(keepends=True),
            fromfile="prompt.txt",
            tofile="prompt.txt",
        )
    )
    payload = {
        "diagnosis": (
            "EvalFix's legacy optimizer proposed a prompt change. "
            "Structured diagnosis details were unavailable in this CLI version."
        ),
        "root_cause": "prompt",
        "confidence": None,
        "diff": diff,
        "before": {"passed": None, "total": None},
        "after": {
            "passed": after_result.passed,
            "total": after_result.total,
        },
        "previously_passing_intact": None,
    }
    return CliExecution(
        command=legacy_execution.command,
        return_code=legacy_execution.return_code,
        stdout=json.dumps(payload),
        stderr=legacy_execution.stderr,
    )


def _json_option_unsupported(execution: CliExecution) -> bool:
    combined = f"{execution.stdout}\n{execution.stderr}".lower()
    return (
        execution.return_code == 2
        and "--json" in combined
        and ("no such option" in combined or "unknown option" in combined)
    )


def _decode_object(raw_output: str) -> tuple[dict[str, object], str | None]:
    stripped = raw_output.strip()
    if not stripped:
        return {}, "EvalFix produced empty JSON output."

    candidates = [stripped]
    candidates.extend(line.strip() for line in reversed(stripped.splitlines()) if line.strip())
    for candidate in candidates:
        try:
            decoded: object = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, Mapping):
            return _string_key_mapping(decoded), None
        return {}, "EvalFix JSON output was not an object."

    return {}, "EvalFix output was not valid JSON."


def _parse_score_summary(
    value: object,
    field_name: str,
    warnings: list[str],
) -> ScoreSummary:
    if not isinstance(value, Mapping):
        if value is not None:
            warnings.append(f"'{field_name}' was not an object.")
        elif field_name:
            warnings.append(f"Missing '{field_name}' score summary.")
        return ScoreSummary(passed=None, total=None)

    normalized = _string_key_mapping(value)
    return ScoreSummary(
        passed=_as_nonnegative_int(normalized.get("passed")),
        total=_as_nonnegative_int(normalized.get("total")),
    )


def _string_key_mapping(value: Mapping[object, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


def _is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _as_text(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _first_text(value: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        parsed = _as_text(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _first_bool(value: Mapping[str, object], *keys: str) -> bool | None:
    for key in keys:
        parsed = _as_bool(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _as_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _first_nonnegative_int(
    value: Mapping[str, object],
    *keys: str,
) -> int | None:
    for key in keys:
        parsed = _as_nonnegative_int(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first_float(value: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        parsed = _as_float(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _decode_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
