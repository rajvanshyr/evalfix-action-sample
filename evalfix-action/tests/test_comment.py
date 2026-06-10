from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import errors
from comment import GitHubCommentClient, MARKER, Report, render_report
from compare import compare_runs
from evalfix_adapter import (
    CaseResult,
    FixResult,
    RunResult,
    ScoreSummary,
)


SNAPSHOTS = Path(__file__).parent / "snapshots"


def _case(
    case_id: str,
    passed: bool,
    *,
    grader: str = "semantic",
    reason: str | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        passed=passed,
        grader=grader,
        reason=reason,
    )


def _run(*cases: CaseResult) -> RunResult:
    passed = sum(case.passed is True for case in cases)
    failed = sum(case.passed is False for case in cases)
    total = len(cases)
    return RunResult(
        total=total,
        passed=passed,
        failed=failed,
        score=passed / total if total else None,
        cases=cases,
    )


def _regression_report(*, include_fix: bool) -> Report:
    long_reason = (
        "The answer approves a refund even though the warranty expired, "
        "contradicting the expected policy and exposing the business to loss."
    )
    base = _run(
        _case("refund_expired_warranty_polite", True),
        _case("refund_expired_warranty_direct", True),
        _case("existing_failure", False),
    )
    head = _run(
        _case(
            "refund_expired_warranty_polite",
            False,
            reason=long_reason,
        ),
        _case(
            "refund_expired_warranty_direct",
            False,
            reason="Approved an expired-warranty refund.",
        ),
        _case("existing_failure", False),
        _case("new_refund_case", False, reason="The newly added case does not pass."),
    )
    comparison = compare_runs(base, head)
    fix = None
    if include_fix:
        fix = FixResult(
            diagnosis=(
                "Both failures are expired-warranty refund cases. "
                "The prompt mentions the purchase window but does not require "
                "an active warranty."
            ),
            root_cause="prompt",
            confidence=0.91,
            diff=(
                "--- prompt.txt\n"
                "+++ prompt.txt\n"
                "@@ -1 +1 @@\n"
                "- Refunds are available within 30 days of purchase.\n"
                "+ Refunds are available within 30 days of purchase, only while "
                "the warranty is active."
            ),
            before=ScoreSummary(passed=38, total=40),
            after=ScoreSummary(passed=40, total=40),
            previously_passing_intact=True,
        )
    return Report(
        head=head,
        base=base,
        comparison=comparison,
        fix=fix,
        pattern_summary=(
            "Both failures are expired-warranty refund cases. The agent treats "
            "the 30-day window as sufficient even when warranty coverage has ended."
        ),
        project_dir="agents/support",
    )


def _green_report() -> Report:
    base = _run(_case("greeting", True), _case("refund", False))
    head = _run(_case("greeting", True), _case("refund", True))
    return Report(
        head=head,
        base=base,
        comparison=compare_runs(base, head),
        project_dir="agents/support",
    )


def _assert_snapshot(name: str, actual: str) -> None:
    expected = (SNAPSHOTS / name).read_text(encoding="utf-8").rstrip("\n")
    assert actual == expected


def test_regression_with_fix_snapshot() -> None:
    _assert_snapshot(
        "regression_with_fix.md",
        render_report(_regression_report(include_fix=True)),
    )


def test_regression_without_fix_snapshot() -> None:
    _assert_snapshot(
        "regression_without_fix.md",
        render_report(_regression_report(include_fix=False)),
    )


def test_green_snapshot_is_at_most_three_lines() -> None:
    rendered = render_report(_green_report())

    _assert_snapshot("green.md", rendered)
    assert len(rendered.splitlines()) <= 3


def test_new_case_failure_is_warning_not_green_or_regression() -> None:
    base = _run(_case("stable", True))
    head = _run(_case("stable", True), _case("new_case", False))

    rendered = render_report(
        Report(
            head=head,
            base=base,
            comparison=compare_runs(base, head),
            project_dir="agent",
        )
    )

    assert "⚠️ 1/2 passed (no regressions)" in rendered
    assert "**New case, failing:** `new_case`" in rendered
    assert "✅" not in rendered


def test_missing_key_error_snapshot() -> None:
    _assert_snapshot("error_missing_key.md", errors.missing_anthropic_key())


def test_missing_files_error_snapshot() -> None:
    _assert_snapshot(
        "error_missing_files.md",
        errors.missing_project_files("agents/support"),
    )


def test_yaml_error_snapshot() -> None:
    _assert_snapshot(
        "error_invalid_yaml.md",
        errors.invalid_evals_yaml(
            'while parsing a block mapping\nexpected <block end>, but found "-"'
        ),
    )


def test_cli_crash_error_snapshot() -> None:
    stderr = "\n".join(f"line {index}" for index in range(1, 24))
    rendered = errors.cli_failure(stderr, timed_out=False)

    _assert_snapshot("error_cli_crash.md", rendered)
    assert "\nline 1\n" not in rendered
    assert "line 4" in rendered


def test_cli_timeout_error_snapshot() -> None:
    _assert_snapshot(
        "error_cli_timeout.md",
        errors.cli_failure("evaluation exceeded the deadline", timed_out=True),
    )


@dataclass
class FakeResponse:
    payload: object

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class MemorySession:
    def __init__(self) -> None:
        self.comments: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, object],
        timeout: float,
    ) -> FakeResponse:
        self.calls.append(("GET", url, {"headers": headers, "params": params}))
        return FakeResponse(list(self.comments))

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        comment = {
            "id": 101,
            "body": json["body"],
            "html_url": "https://github.test/comment/101",
        }
        self.comments.append(comment)
        self.calls.append(("POST", url, {"headers": headers, "json": json}))
        return FakeResponse(comment)

    def patch(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        self.comments[0]["body"] = json["body"]
        self.calls.append(("PATCH", url, {"headers": headers, "json": json}))
        return FakeResponse(
            {
                "id": 101,
                "body": json["body"],
                "html_url": "https://github.test/comment/101",
            }
        )


def test_sticky_comment_posts_then_patches_on_second_run() -> None:
    session = MemorySession()
    client = GitHubCommentClient(
        repository="acme/agent",
        token="secret",
        api_url="https://api.github.test",
        session=session,
    )

    first = client.sync(pull_number=42, body=f"{MARKER}\nfirst")
    second = client.sync(pull_number=42, body=f"{MARKER}\nsecond")

    assert first.action == "created"
    assert second.action == "updated"
    assert session.comments == [
        {
            "id": 101,
            "body": f"{MARKER}\nsecond",
            "html_url": "https://github.test/comment/101",
        }
    ]
    assert [method for method, _, _ in session.calls] == [
        "GET",
        "POST",
        "GET",
        "PATCH",
    ]
    assert session.calls[-1][1].endswith("/issues/42/comments/101")


def test_marker_must_be_first_line() -> None:
    session = MemorySession()
    session.comments = [
        {
            "id": 7,
            "body": f"intro\n{MARKER}\nold",
        }
    ]
    client = GitHubCommentClient(
        repository="acme/agent",
        token="secret",
        session=session,
    )

    result = client.sync(pull_number=5, body=f"{MARKER}\nnew")

    assert result.action == "created"
    assert len(session.comments) == 2


def test_client_sends_required_github_headers() -> None:
    session = MemorySession()
    client = GitHubCommentClient(
        repository="acme/agent",
        token="token-value",
        session=session,
    )

    client.sync(pull_number=9, body=f"{MARKER}\nbody")

    request_data: dict[str, Any] = session.calls[0][2]
    headers = request_data["headers"]
    assert headers["Authorization"] == "Bearer token-value"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
