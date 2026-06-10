"""Orchestrate EvalFix evaluation, comparison, diagnosis, and reporting."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import requests
import yaml

import errors
from baseline import BaselineExecution, resolve_base_ref, run_base_evaluation
from comment import GitHubCommentClient, Report, render_report
from compare import Comparison, compare_runs
from evalfix_adapter import (
    CliExecution,
    FixResult,
    RunResult,
    invoke_fix,
    invoke_run,
    parse_fix_output,
    parse_run_output,
)


@dataclass(frozen=True, slots=True)
class Config:
    """Validated action inputs and GitHub runtime metadata."""

    repository_root: Path
    project_dir: Path
    suggest_fix: bool
    fail_on_regression: bool
    baseline_mode: str
    max_cases: int
    timeout_seconds: float
    comment_enabled: bool
    anthropic_key: str | None
    github_token: str | None
    github_repository: str | None
    github_api_url: str
    pull_number: int | None
    base_branch: str | None
    fork_pull_request: bool
    step_summary_path: Path | None
    output_path: Path | None


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Final action result before process exit."""

    exit_code: int
    body: str | None
    summary: str
    passed: int = 0
    total: int = 0
    regressions: int = 0
    score_delta: float | None = None


class Dependencies(Protocol):
    """Injectable side effects for offline orchestration tests."""

    def run_head(
        self,
        project_dir: Path,
        *,
        timeout_seconds: float,
    ) -> CliExecution: ...

    def run_base(
        self,
        *,
        repository_root: Path,
        project_dir: Path,
        head_evals_path: Path,
        base_ref: str,
        timeout_seconds: float,
    ) -> BaselineExecution: ...

    def run_fix(
        self,
        project_dir: Path,
        *,
        timeout_seconds: float,
    ) -> CliExecution: ...


class DefaultDependencies:
    """Production side-effect implementation."""

    def run_head(
        self,
        project_dir: Path,
        *,
        timeout_seconds: float,
    ) -> CliExecution:
        return invoke_run(project_dir, timeout_seconds=timeout_seconds)

    def run_base(
        self,
        *,
        repository_root: Path,
        project_dir: Path,
        head_evals_path: Path,
        base_ref: str,
        timeout_seconds: float,
    ) -> BaselineExecution:
        return run_base_evaluation(
            repository_root=repository_root,
            project_dir=project_dir,
            head_evals_path=head_evals_path,
            base_ref=base_ref,
            timeout_seconds=timeout_seconds,
        )

    def run_fix(
        self,
        project_dir: Path,
        *,
        timeout_seconds: float,
    ) -> CliExecution:
        return invoke_fix(project_dir, timeout_seconds=timeout_seconds)


def main() -> int:
    """Load environment, run the action, and publish outputs."""

    try:
        config = load_config(os.environ)
    except ValueError as error:
        result = ActionResult(
            exit_code=1,
            body=None,
            summary=f"EvalFix configuration error: {error}",
        )
        _publish(result, None)
        return result.exit_code

    result = execute(config, DefaultDependencies())
    _publish(result, config)
    return result.exit_code


def execute(config: Config, dependencies: Dependencies) -> ActionResult:
    """Execute one action run without directly terminating the process."""

    if config.fork_pull_request:
        return ActionResult(
            exit_code=0,
            body=None,
            summary=(
                "EvalFix skipped this fork pull request because repository secrets "
                "are intentionally unavailable."
            ),
        )

    if not config.anthropic_key:
        return _error_result(config, errors.missing_anthropic_key())

    project_root = config.repository_root / config.project_dir
    prompt_path = project_root / "prompt.txt"
    evals_path = project_root / "evals.yaml"
    if not prompt_path.is_file() or not evals_path.is_file():
        return _error_result(
            config,
            errors.missing_project_files(str(config.project_dir)),
        )

    try:
        eval_count = _validate_evals(evals_path)
    except yaml.YAMLError as error:
        return _error_result(config, errors.invalid_evals_yaml(str(error)))
    except OSError:
        return _error_result(
            config,
            errors.missing_project_files(str(config.project_dir)),
        )

    truncation_note: str | None = None
    with tempfile.TemporaryDirectory(prefix="evalfix-head-") as temp_root:
        evaluation_root = Path(temp_root) / "project"
        shutil.copytree(project_root, evaluation_root)
        head_evals = evaluation_root / "evals.yaml"
        if eval_count > config.max_cases:
            _truncate_evals(head_evals, config.max_cases)
            truncation_note = (
                f"Run limited to the first {config.max_cases} of {eval_count} cases "
                "by `max_cases`."
            )

        head_execution = dependencies.run_head(
            evaluation_root,
            timeout_seconds=config.timeout_seconds,
        )
        head_error = _execution_error(head_execution)
        if head_error is not None:
            return _error_result(config, head_error)
        head = parse_run_output(head_execution.stdout, max_cases=config.max_cases)
        if _unusable_output(head):
            return _error_result(
                config,
                errors.cli_failure(
                    head_execution.stderr or "\n".join(head.warnings),
                    timed_out=False,
                ),
            )
        if _all_cases_errored(head):
            details = "\n".join(
                case.error or "Unknown evaluation error." for case in head.cases
            )
            return _error_result(
                config,
                errors.cli_failure(details, timed_out=False),
            )

        base: RunResult | None = None
        baseline_note: str | None = None
        comparison: Comparison
        if config.baseline_mode == "base-branch":
            base_ref = _base_ref(config)
            if base_ref is None:
                baseline_note = "Baseline unavailable: pull-request base ref was not provided."
            else:
                base_execution = dependencies.run_base(
                    repository_root=config.repository_root,
                    project_dir=config.project_dir,
                    head_evals_path=head_evals,
                    base_ref=base_ref,
                    timeout_seconds=config.timeout_seconds,
                )
                if base_execution.execution is None:
                    baseline_note = (
                        "Baseline unavailable; reporting absolute results only. "
                        f"{base_execution.warning or 'Unknown baseline preparation error.'}"
                    )
                elif _execution_error(base_execution.execution) is not None:
                    baseline_note = (
                        "Baseline evaluation failed; reporting absolute results only."
                    )
                else:
                    base = parse_run_output(
                        base_execution.execution.stdout,
                        max_cases=config.max_cases,
                    )
                    if _unusable_output(base):
                        base = None
                        baseline_note = (
                            "Baseline output could not be parsed; reporting absolute "
                            "results only."
                        )

        comparison = (
            compare_runs(base, head)
            if base is not None
            else _absolute_comparison(head)
        )

        fix: FixResult | None = None
        if comparison.regressions and config.suggest_fix:
            fix, fix_error = _run_fix_safely(
                project_root=evaluation_root,
                timeout_seconds=config.timeout_seconds,
                dependencies=dependencies,
            )
            if fix_error is not None:
                return _error_result(config, fix_error)

        note = " ".join(
            item for item in (truncation_note, baseline_note) if item
        ) or None
        body = render_report(
            Report(
                head=head,
                base=base,
                comparison=comparison,
                fix=fix,
                project_dir=str(config.project_dir),
                note=note,
            )
        )
        regression_count = len(comparison.regressions)
        exit_code = (
            1 if regression_count and config.fail_on_regression else 0
        )
        summary = (
            f"EvalFix: {head.passed}/{head.total} passed; "
            f"{regression_count} regression"
            f"{'' if regression_count == 1 else 's'}."
        )
        result = ActionResult(
            exit_code=exit_code,
            body=body,
            summary=summary,
            passed=head.passed,
            total=head.total,
            regressions=regression_count,
            score_delta=comparison.score_delta,
        )
        return _maybe_comment(config, result)


def load_config(environment: Mapping[str, str]) -> Config:
    """Parse action inputs and GitHub event metadata from the environment."""

    repository_root = Path(environment.get("GITHUB_WORKSPACE", os.getcwd())).resolve()
    project_value = environment.get("INPUT_PROJECT_DIR", "").strip()
    if not project_value:
        raise ValueError("INPUT_PROJECT_DIR is required.")
    project_dir = Path(project_value)
    if project_dir.is_absolute() or ".." in project_dir.parts:
        raise ValueError("project_dir must be a relative path inside the repository.")

    baseline_mode = environment.get("INPUT_BASELINE", "base-branch").strip()
    if baseline_mode not in {"base-branch", "none"}:
        raise ValueError("baseline must be 'base-branch' or 'none'.")

    event = _load_event(environment.get("GITHUB_EVENT_PATH"))
    return Config(
        repository_root=repository_root,
        project_dir=project_dir,
        suggest_fix=_boolean(environment, "INPUT_SUGGEST_FIX", False),
        fail_on_regression=_boolean(
            environment,
            "INPUT_FAIL_ON_REGRESSION",
            True,
        ),
        baseline_mode=baseline_mode,
        max_cases=_positive_int(environment, "INPUT_MAX_CASES", 100),
        timeout_seconds=60
        * _positive_int(environment, "INPUT_TIMEOUT_MINUTES", 15),
        comment_enabled=_boolean(environment, "INPUT_COMMENT", True),
        anthropic_key=environment.get("ANTHROPIC_API_KEY"),
        github_token=environment.get("GITHUB_TOKEN"),
        github_repository=environment.get("GITHUB_REPOSITORY"),
        github_api_url=environment.get("GITHUB_API_URL", "https://api.github.com"),
        pull_number=_pull_number(event),
        base_branch=_base_branch(event, environment),
        fork_pull_request=_is_fork(event),
        step_summary_path=_optional_path(environment.get("GITHUB_STEP_SUMMARY")),
        output_path=_optional_path(environment.get("GITHUB_OUTPUT")),
    )


def _validate_evals(path: Path) -> int:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tests"), list):
        raise yaml.YAMLError("evals.yaml must contain a top-level 'tests' list.")
    return len(payload["tests"])


def _truncate_evals(path: Path, max_cases: int) -> None:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tests"), list):
        raise yaml.YAMLError("evals.yaml must contain a top-level 'tests' list.")
    payload["tests"] = payload["tests"][:max_cases]
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _execution_error(execution: CliExecution) -> str | None:
    if execution.timed_out:
        return errors.cli_failure(execution.stderr, timed_out=True)
    if execution.return_code is None or execution.return_code not in {0, 1}:
        return errors.cli_failure(execution.stderr, timed_out=False)
    if not execution.stdout.strip():
        return errors.cli_failure(
            execution.stderr or "EvalFix produced no JSON output.",
            timed_out=False,
        )
    return None


def _run_fix_safely(
    *,
    project_root: Path,
    timeout_seconds: float,
    dependencies: Dependencies,
) -> tuple[FixResult | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="evalfix-fix-") as temp_root:
        throwaway = Path(temp_root) / "project"
        shutil.copytree(project_root, throwaway)
        execution = dependencies.run_fix(
            throwaway,
            timeout_seconds=timeout_seconds,
        )
        execution_error = _execution_error(execution)
        if execution_error is not None:
            return None, execution_error
        fix = parse_fix_output(execution.stdout)
        if fix.diff is None and fix.diagnosis is None:
            return (
                None,
                errors.cli_failure(
                    execution.stderr or "\n".join(fix.warnings),
                    timed_out=False,
                ),
            )
        return fix, None


def _absolute_comparison(head: RunResult) -> Comparison:
    return Comparison(
        regressions=(),
        pre_existing_failures=(),
        new_case_failures=(),
        fixed=(),
        unchanged_passing=(),
        unknown=(),
        score_delta=None,
    )


def _unusable_output(result: RunResult) -> bool:
    fatal_warnings = {
        "EvalFix produced empty JSON output.",
        "EvalFix output was not valid JSON.",
        "EvalFix JSON output was not an object.",
    }
    return any(warning in fatal_warnings for warning in result.warnings)


def _all_cases_errored(result: RunResult) -> bool:
    return bool(result.cases) and all(case.error is not None for case in result.cases)


def _maybe_comment(config: Config, result: ActionResult) -> ActionResult:
    if not config.comment_enabled or result.body is None:
        return result
    if (
        not config.github_token
        or not config.github_repository
        or config.pull_number is None
    ):
        permissions = _permissions_guidance()
        return replace(result, summary=f"{result.summary}\n\n{permissions}")
    try:
        GitHubCommentClient(
            repository=config.github_repository,
            token=config.github_token,
            api_url=config.github_api_url,
        ).sync(pull_number=config.pull_number, body=result.body)
    except (requests.RequestException, ValueError) as error:
        return replace(
            result,
            summary=(
                f"{result.summary}\n\nCommenting failed: {error}\n\n"
                f"{_permissions_guidance()}"
            ),
        )
    return result


def _error_result(config: Config, body: str) -> ActionResult:
    result = ActionResult(
        exit_code=1,
        body=body,
        summary=_plain_summary(body),
    )
    return _maybe_comment(config, result)


def _publish(result: ActionResult, config: Config | None) -> None:
    print(result.body or result.summary)
    if config is not None and config.step_summary_path is not None:
        _append(config.step_summary_path, result.summary + "\n")
    if config is not None and config.output_path is not None:
        score_delta = (
            "" if result.score_delta is None else f"{result.score_delta:.6g}"
        )
        _append(
            config.output_path,
            "\n".join(
                [
                    f"passed={result.passed}",
                    f"total={result.total}",
                    f"regressions={result.regressions}",
                    f"score_delta={score_delta}",
                    "",
                ]
            ),
        )


def _base_ref(config: Config) -> str | None:
    local_override = os.environ.get("EVALFIX_BASE_REF")
    if local_override:
        return local_override
    if not config.base_branch:
        return None
    return resolve_base_ref(config.repository_root, config.base_branch)


def _load_event(path_value: str | None) -> dict[str, object]:
    if not path_value:
        return {}
    try:
        payload: object = json.loads(Path(path_value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items()}


def _pull_request(event: Mapping[str, object]) -> Mapping[str, object]:
    value = event.get("pull_request")
    return value if isinstance(value, dict) else {}


def _pull_number(event: Mapping[str, object]) -> int | None:
    value = event.get("number")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _base_branch(
    event: Mapping[str, object],
    environment: Mapping[str, str],
) -> str | None:
    pull_request = _pull_request(event)
    base = pull_request.get("base")
    if isinstance(base, dict):
        base_ref = base.get("ref")
        if isinstance(base_ref, str):
            return base_ref
    value = environment.get("GITHUB_BASE_REF")
    return value or None


def _is_fork(event: Mapping[str, object]) -> bool:
    pull_request = _pull_request(event)
    head = pull_request.get("head")
    if not isinstance(head, dict):
        return False
    repository = head.get("repo")
    return isinstance(repository, dict) and repository.get("fork") is True


def _boolean(
    environment: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{name} must be 'true' or 'false'.")
    return normalized == "true"


def _positive_int(
    environment: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw = environment.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer.") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _append(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(value)


def _plain_summary(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("### "):
            return f"EvalFix: {line[4:]}"
    return "EvalFix could not complete."


def _permissions_guidance() -> str:
    return (
        "To enable the sticky PR comment, add:\n\n"
        "permissions:\n"
        "  contents: read\n"
        "  pull-requests: write"
    )


if __name__ == "__main__":
    sys.exit(main())
