from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from baseline import BaselineExecution
from evalfix_adapter import CliExecution
from main import Config, DefaultDependencies, _publish, execute, load_config


RUN_PASS = json.dumps(
    {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "score": 1.0,
        "results": [
            {"id": "stable", "pass": True},
            {"id": "changed", "pass": True},
        ],
    }
)
RUN_HEAD_REGRESSION = json.dumps(
    {
        "total": 3,
        "passed": 1,
        "failed": 2,
        "score": 1 / 3,
        "results": [
            {"id": "stable", "pass": True},
            {
                "id": "changed",
                "pass": False,
                "grader": "semantic",
                "reason": "Behavior changed.",
            },
            {
                "id": "new_case",
                "pass": False,
                "grader": "semantic",
                "reason": "New case is failing.",
            },
        ],
    }
)
RUN_NEW_FAILURE_ONLY = json.dumps(
    {
        "total": 3,
        "passed": 2,
        "failed": 1,
        "score": 2 / 3,
        "results": [
            {"id": "stable", "pass": True},
            {"id": "changed", "pass": True},
            {
                "id": "new_case",
                "pass": False,
                "grader": "semantic",
                "reason": "New case is failing.",
            },
        ],
    }
)
RUN_ALL_INFRA_ERRORS = json.dumps(
    {
        "pass_count": 0,
        "fail_count": 2,
        "total_count": 2,
        "avg_score": 0,
        "results": [
            {
                "test_id": "one",
                "passed": False,
                "actual": "",
                "judge_reasoning": None,
                "error": "Error code: 400 - model unavailable",
            },
            {
                "test_id": "two",
                "passed": False,
                "actual": "",
                "judge_reasoning": None,
                "error": "Error code: 400 - model unavailable",
            },
        ],
    }
)
FIX_PASS = json.dumps(
    {
        "diagnosis": "The changed behavior is caused by the prompt.",
        "root_cause": "prompt",
        "confidence": 0.9,
        "diff": "- old\n+ new",
        "before": {"passed": 1, "total": 3},
        "after": {"passed": 3, "total": 3},
        "previously_passing_intact": True,
    }
)


class FakeDependencies(DefaultDependencies):
    def __init__(
        self,
        *,
        head: CliExecution,
        base: BaselineExecution | None = None,
        fix: CliExecution | None = None,
    ) -> None:
        self.head = head
        self.base = base or BaselineExecution(
            CliExecution(("evalfix",), 0, RUN_PASS, "")
        )
        self.fix = fix or CliExecution(("evalfix",), 0, FIX_PASS, "")
        self.head_calls = 0
        self.base_calls = 0
        self.fix_calls = 0
        self.head_project: Path | None = None
        self.head_case_count: int | None = None
        self.fix_project: Path | None = None

    def run_head(
        self,
        project_dir: Path,
        *,
        timeout_seconds: float,
    ) -> CliExecution:
        self.head_calls += 1
        self.head_project = project_dir
        payload = project_dir.joinpath("evals.yaml").read_text(encoding="utf-8")
        self.head_case_count = payload.count("- id:")
        project_dir.joinpath(".evalfix-state").write_text("temporary", encoding="utf-8")
        return self.head

    def run_base(
        self,
        *,
        repository_root: Path,
        project_dir: Path,
        head_evals_path: Path,
        base_ref: str,
        timeout_seconds: float,
    ) -> BaselineExecution:
        self.base_calls += 1
        return self.base

    def run_fix(
        self,
        project_dir: Path,
        *,
        timeout_seconds: float,
    ) -> CliExecution:
        self.fix_calls += 1
        self.fix_project = project_dir
        project_dir.joinpath("prompt.txt").write_text("proposed", encoding="utf-8")
        return self.fix


def _config(tmp_path: Path) -> Config:
    project = tmp_path / "agent"
    project.mkdir()
    project.joinpath("prompt.txt").write_text("original", encoding="utf-8")
    project.joinpath("evals.yaml").write_text(
        (
            "tests:\n"
            "  - id: stable\n"
            '    input: "one"\n'
            '    expected: "one"\n'
            "    grader: semantic\n"
            "  - id: changed\n"
            '    input: "two"\n'
            '    expected: "two"\n'
            "    grader: semantic\n"
            "  - id: new_case\n"
            '    input: "three"\n'
            '    expected: "three"\n'
            "    grader: semantic\n"
        ),
        encoding="utf-8",
    )
    return Config(
        repository_root=tmp_path,
        project_dir=Path("agent"),
        suggest_fix=False,
        fail_on_regression=True,
        baseline_mode="base-branch",
        max_cases=100,
        timeout_seconds=60,
        comment_enabled=False,
        anthropic_key="key",
        github_token=None,
        github_repository=None,
        github_api_url="https://api.github.com",
        pull_number=None,
        base_branch="main",
        fork_pull_request=False,
        step_summary_path=None,
        output_path=None,
    )


def test_fork_pr_exits_zero_without_touching_secrets_or_cli(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        fork_pull_request=True,
        anthropic_key=None,
    )
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 0, RUN_HEAD_REGRESSION, "")
    )

    result = execute(config, dependencies)

    assert result.exit_code == 0
    assert result.body is None
    assert dependencies.head_calls == 0
    assert dependencies.base_calls == 0
    assert dependencies.fix_calls == 0


def test_missing_key_returns_friendly_error_without_cli(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), anthropic_key=None)
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 0, RUN_HEAD_REGRESSION, "")
    )

    result = execute(config, dependencies)

    assert result.exit_code == 1
    assert result.body is not None
    assert "ANTHROPIC_API_KEY is not configured" in result.body
    assert dependencies.head_calls == 0


def test_missing_files_and_invalid_yaml_are_onboarding_errors(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.repository_root.joinpath("agent/prompt.txt").unlink()
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 0, RUN_HEAD_REGRESSION, "")
    )

    missing = execute(config, dependencies)

    assert missing.body is not None
    assert "Agent eval files were not found" in missing.body

    config.repository_root.joinpath("agent/prompt.txt").write_text(
        "prompt",
        encoding="utf-8",
    )
    config.repository_root.joinpath("agent/evals.yaml").write_text(
        "tests:\n - id: broken\n  input: nope",
        encoding="utf-8",
    )
    invalid = execute(config, dependencies)

    assert invalid.body is not None
    assert "evals.yaml could not be parsed" in invalid.body


def test_regression_exit_and_pre_existing_distinction(tmp_path: Path) -> None:
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 1, RUN_HEAD_REGRESSION, "")
    )

    result = execute(_config(tmp_path), dependencies)

    assert result.exit_code == 1
    assert result.regressions == 1
    assert result.passed == 1
    assert result.body is not None
    assert "1 regression" in result.body
    assert "new_case" in result.body


def test_fail_on_regression_false_keeps_success_exit(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), fail_on_regression=False)
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 1, RUN_HEAD_REGRESSION, "")
    )

    result = execute(config, dependencies)

    assert result.exit_code == 0
    assert result.regressions == 1


def test_new_failing_case_is_visible_but_does_not_fail_job(tmp_path: Path) -> None:
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 1, RUN_NEW_FAILURE_ONLY, "")
    )

    result = execute(_config(tmp_path), dependencies)

    assert result.exit_code == 0
    assert result.regressions == 0
    assert result.body is not None
    assert "⚠️ 2/3 passed (no regressions)" in result.body
    assert "New case, failing" in result.body


def test_baseline_failure_degrades_to_absolute_report(tmp_path: Path) -> None:
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 1, RUN_HEAD_REGRESSION, ""),
        base=BaselineExecution(None, "merge-base unavailable"),
    )

    result = execute(_config(tmp_path), dependencies)

    assert result.exit_code == 0
    assert result.regressions == 0
    assert result.body is not None
    assert "no baseline comparison" in result.body
    assert "merge-base unavailable" in result.body


def test_head_and_fix_run_only_on_throwaway_copies(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), suggest_fix=True)
    original_project = config.repository_root / config.project_dir
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 1, RUN_HEAD_REGRESSION, "")
    )

    result = execute(config, dependencies)

    assert result.body is not None
    assert "Proposed fix (not applied)" in result.body
    assert dependencies.head_project != original_project
    assert dependencies.fix_project != original_project
    assert original_project.joinpath("prompt.txt").read_text(encoding="utf-8") == "original"
    assert not original_project.joinpath(".evalfix-state").exists()


def test_max_cases_truncates_temporary_eval_file(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), max_cases=2, baseline_mode="none")
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 0, RUN_PASS, "")
    )

    result = execute(config, dependencies)

    assert result.exit_code == 0
    assert dependencies.head_case_count == 2
    assert result.body is not None


def test_head_timeout_returns_friendly_error(tmp_path: Path) -> None:
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), None, "", "deadline", timed_out=True)
    )

    result = execute(_config(tmp_path), dependencies)

    assert result.exit_code == 1
    assert result.body is not None
    assert "EvalFix timed out" in result.body


def test_fix_failure_returns_friendly_error(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), suggest_fix=True)
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 1, RUN_HEAD_REGRESSION, ""),
        fix=CliExecution(("evalfix",), 2, "", "optimizer crashed"),
    )

    result = execute(config, dependencies)

    assert result.exit_code == 1
    assert result.body is not None
    assert "EvalFix CLI failed" in result.body
    assert "optimizer crashed" in result.body


def test_garbage_cli_output_is_not_reported_as_green(tmp_path: Path) -> None:
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 0, "not json", "")
    )

    result = execute(_config(tmp_path), dependencies)

    assert result.exit_code == 1
    assert result.body is not None
    assert "EvalFix CLI failed" in result.body


def test_all_case_infrastructure_errors_are_not_behavioral_failures(
    tmp_path: Path,
) -> None:
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 1, RUN_ALL_INFRA_ERRORS, "")
    )

    result = execute(_config(tmp_path), dependencies)

    assert result.exit_code == 1
    assert result.body is not None
    assert "EvalFix CLI failed" in result.body
    assert "model unavailable" in result.body
    assert "no regressions" not in result.body


def test_missing_comment_credentials_fall_back_to_permissions_summary(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), comment_enabled=True)
    dependencies = FakeDependencies(
        head=CliExecution(("evalfix",), 0, RUN_PASS, "")
    )

    result = execute(config, dependencies)

    assert result.exit_code == 0
    assert "pull-requests: write" in result.summary


def test_publish_writes_action_outputs_and_step_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.md"
    output_path = tmp_path / "outputs.txt"
    config = replace(
        _config(tmp_path),
        step_summary_path=summary_path,
        output_path=output_path,
    )

    _publish(
        result=execute(
            replace(config, baseline_mode="none"),
            FakeDependencies(head=CliExecution(("evalfix",), 0, RUN_PASS, "")),
        ),
        config=config,
    )

    assert "EvalFix: 2/2 passed; 0 regressions." in summary_path.read_text(
        encoding="utf-8"
    )
    outputs = output_path.read_text(encoding="utf-8")
    assert "passed=2" in outputs
    assert "total=2" in outputs
    assert "regressions=0" in outputs
    assert "score_delta=" in outputs


def test_load_config_detects_fork_event(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "number": 12,
                "pull_request": {
                    "base": {"ref": "main"},
                    "head": {"repo": {"fork": True}},
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(
        {
            "GITHUB_WORKSPACE": str(tmp_path),
            "GITHUB_EVENT_PATH": str(event_path),
            "INPUT_PROJECT_DIR": "agent",
        }
    )

    assert config.fork_pull_request is True
    assert config.pull_number == 12
    assert config.base_branch == "main"


def test_load_config_rejects_path_traversal(tmp_path: Path) -> None:
    try:
        load_config(
            {
                "GITHUB_WORKSPACE": str(tmp_path),
                "INPUT_PROJECT_DIR": "../secret",
            }
        )
    except ValueError as error:
        assert "relative path" in str(error)
    else:
        raise AssertionError("Expected path traversal to be rejected.")
