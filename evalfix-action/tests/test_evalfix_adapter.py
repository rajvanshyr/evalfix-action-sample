import subprocess
from pathlib import Path
from unittest.mock import patch

from evalfix_adapter import invoke_fix, invoke_run, parse_fix_output, parse_run_output


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_well_formed_run_output() -> None:
    result = parse_run_output(_fixture("run_well_formed.json"))

    assert result.total == 4
    assert result.passed == 2
    assert result.failed == 2
    assert result.score == 0.5
    assert result.warnings == ()
    assert result.cases[0].case_id == "regression"
    assert result.cases[0].passed is False
    assert result.cases[0].reason == "Approved an ineligible refund."


def test_parse_installed_cli_schema() -> None:
    result = parse_run_output(_fixture("run_installed_cli.json"))

    assert result.total == 5
    assert result.passed == 3
    assert result.failed == 2
    assert result.score == 0.72
    assert result.cases[0].case_id == "refund_recent_active_warranty"
    assert result.cases[0].passed is True
    assert result.cases[1].passed is False
    assert result.cases[1].reason == "The response approved an ineligible refund."
    assert result.cases[1].error is None
    assert result.warnings == ()


def test_parse_missing_fields_degrades_with_warnings() -> None:
    result = parse_run_output(_fixture("run_missing_fields.json"))

    assert result.total == 3
    assert result.passed == 1
    assert result.failed == 1
    assert result.score == 1 / 3
    assert result.cases[1].case_id == "unknown_case_2"
    assert result.cases[2].passed is None
    assert len(result.warnings) >= 6


def test_parse_garbage_output_returns_empty_result() -> None:
    result = parse_run_output(_fixture("run_garbage.txt"))

    assert result.total == 0
    assert result.passed == 0
    assert result.failed == 0
    assert result.score is None
    assert result.cases == ()
    assert result.warnings == ("EvalFix output was not valid JSON.",)


def test_parse_last_json_line_after_cli_logs() -> None:
    result = parse_run_output('progress message\n{"total": 1, "passed": 1, "failed": 0}')

    assert result.total == 1
    assert result.passed == 1
    assert result.score == 1.0


def test_max_cases_truncates_details_without_changing_reported_totals() -> None:
    result = parse_run_output(_fixture("run_well_formed.json"), max_cases=2)

    assert len(result.cases) == 2
    assert result.total == 4
    assert result.truncated is True
    assert "Case details truncated to max_cases=2." in result.warnings


def test_parse_well_formed_fix_output() -> None:
    result = parse_fix_output(_fixture("fix_well_formed.json"))

    assert result.root_cause == "prompt"
    assert result.confidence == 0.91
    assert result.before.passed == 38
    assert result.after.passed == 40
    assert result.previously_passing_intact is True
    assert result.diff is not None
    assert result.warnings == ()


def test_parse_incomplete_fix_output_degrades_with_warnings() -> None:
    result = parse_fix_output('{"diagnosis": "A pattern was found.", "before": null}')

    assert result.diagnosis == "A pattern was found."
    assert result.root_cause is None
    assert result.before.passed is None
    assert result.after.total is None
    assert result.diff is None
    assert "Missing proposed prompt diff." in result.warnings


def test_invoke_run_uses_expected_command_without_calling_real_cli() -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout='{"total": 1, "passed": 0, "failed": 1}',
        stderr="",
    )
    with patch("evalfix_adapter.subprocess.run", return_value=completed) as mocked_run:
        execution = invoke_run(Path("agents/support"), timeout_seconds=30)

    mocked_run.assert_called_once_with(
        ("evalfix", "run", "agents/support", "--json"),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert execution.return_code == 1
    assert execution.timed_out is False


def test_invoke_fix_normalizes_timeout_without_calling_real_cli() -> None:
    timeout = subprocess.TimeoutExpired(
        cmd=("evalfix", "fix"),
        timeout=15,
        output=b'{"partial": true}',
        stderr=b"still running",
    )
    with patch("evalfix_adapter.subprocess.run", side_effect=timeout):
        execution = invoke_fix(Path("/tmp/throwaway"), timeout_seconds=15)

    assert execution.command == (
        "evalfix",
        "fix",
        "/tmp/throwaway",
        "--yes",
        "--json",
    )
    assert execution.return_code is None
    assert execution.stdout == '{"partial": true}'
    assert execution.stderr == "still running"
    assert execution.timed_out is True


def test_invoke_run_normalizes_missing_executable() -> None:
    with patch(
        "evalfix_adapter.subprocess.run",
        side_effect=FileNotFoundError("evalfix was not found"),
    ):
        execution = invoke_run(Path("agent"), timeout_seconds=30)

    assert execution.return_code is None
    assert execution.stderr == "evalfix was not found"
    assert execution.timed_out is False


def test_invoke_fix_falls_back_for_legacy_cli_without_json(tmp_path: Path) -> None:
    project = tmp_path / "agent"
    project.mkdir()
    prompt = project / "prompt.txt"
    prompt.write_text("old prompt\n", encoding="utf-8")

    unsupported = subprocess.CompletedProcess(
        args=[],
        returncode=2,
        stdout="",
        stderr="Error: No such option: --json",
    )
    legacy = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="prompt.txt updated.",
        stderr="",
    )
    verification = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            '{"pass_count": 5, "fail_count": 0, "total_count": 5, '
            '"avg_score": 1.0, "results": [{"test_id": "case", "passed": true}]}'
        ),
        stderr="",
    )
    calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return unsupported
        if calls == 2:
            prompt.write_text("new prompt\n", encoding="utf-8")
            return legacy
        return verification

    with patch("evalfix_adapter.subprocess.run", side_effect=fake_run):
        execution = invoke_fix(project, timeout_seconds=30)

    result = parse_fix_output(execution.stdout)
    assert calls == 3
    assert execution.return_code == 0
    assert result.root_cause == "prompt"
    assert result.diff is not None
    assert "-old prompt" in result.diff
    assert "+new prompt" in result.diff
    assert result.after.passed == 5
    assert result.after.total == 5
