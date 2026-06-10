"""Render EvalFix markdown and create or update its sticky PR comment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, cast

import requests

from compare import CaseChange, Comparison
from evalfix_adapter import FixResult, RunResult

MARKER: Final = "<!-- evalfix-action -->"
DOCS_URL: Final = "https://github.com/evalfix/evalfix-action#readme"
API_VERSION: Final = "2022-11-28"


@dataclass(frozen=True, slots=True)
class Report:
    """Inputs required to render a success or regression comment."""

    head: RunResult
    comparison: Comparison
    base: RunResult | None = None
    fix: FixResult | None = None
    pattern_summary: str | None = None
    project_dir: str = "."
    note: str | None = None


@dataclass(frozen=True, slots=True)
class StickyCommentResult:
    """Outcome of a sticky-comment synchronization."""

    action: str
    comment_id: int
    url: str | None


class ResponseLike(Protocol):
    """Subset of ``requests.Response`` used by the GitHub client."""

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class SessionLike(Protocol):
    """Injectable HTTP surface used to keep tests offline."""

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, object],
        timeout: float,
    ) -> ResponseLike: ...

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str],
        timeout: float,
    ) -> ResponseLike: ...

    def patch(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, str],
        timeout: float,
    ) -> ResponseLike: ...


class GitHubCommentClient:
    """Minimal GitHub REST client for one sticky pull-request comment."""

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        api_url: str = "https://api.github.com",
        session: SessionLike | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        self._repository = repository
        self._api_url = api_url.rstrip("/")
        self._session = session or cast(SessionLike, requests.Session())
        self._timeout_seconds = timeout_seconds
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def sync(self, *, pull_number: int, body: str) -> StickyCommentResult:
        """PATCH the existing marker comment or POST a new one."""

        existing = self.find(pull_number=pull_number)
        if existing is not None:
            comment_id, _ = existing
            response = self._session.patch(
                f"{self._comments_url(pull_number)}/{comment_id}",
                headers=self._headers,
                json={"body": body},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = _object_payload(response.json())
            return StickyCommentResult(
                action="updated",
                comment_id=comment_id,
                url=_optional_text(payload.get("html_url")),
            )

        response = self._session.post(
            self._comments_url(pull_number),
            headers=self._headers,
            json={"body": body},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = _object_payload(response.json())
        return StickyCommentResult(
            action="created",
            comment_id=_required_int(payload.get("id"), "GitHub create-comment response"),
            url=_optional_text(payload.get("html_url")),
        )

    def find(self, *, pull_number: int) -> tuple[int, str] | None:
        """Return the first comment whose first line is the EvalFix marker."""

        page = 1
        while True:
            response = self._session.get(
                self._comments_url(pull_number),
                headers=self._headers,
                params={"per_page": 100, "page": page},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("GitHub list-comments response was not a list.")

            for raw_comment in payload:
                comment = _object_payload(raw_comment)
                body = _optional_text(comment.get("body"))
                if body is not None and _has_marker_first(body):
                    return (
                        _required_int(comment.get("id"), "GitHub comment"),
                        body,
                    )

            if len(payload) < 100:
                return None
            page += 1

    def _comments_url(self, pull_number: int) -> str:
        return (
            f"{self._api_url}/repos/{self._repository}/issues/"
            f"{pull_number}/comments"
        )


def render_report(report: Report) -> str:
    """Render the exact green or regression-oriented sticky comment."""

    if report.base is None and report.head.failed:
        return _render_absolute_failures(report)
    if not report.comparison.regressions and report.head.failed:
        return _render_non_regression_failures(report)
    if not report.comparison.regressions:
        return _render_green(report)
    return _render_regressions(report)


def _render_green(report: Report) -> str:
    lines = [
        MARKER,
        f"## EvalFix — ✅ {report.head.passed}/{report.head.total} passed (no regressions)",
    ]
    details: list[str] = []
    if report.comparison.fixed:
        fixed = ", ".join(f"`{item.case_id}`" for item in report.comparison.fixed)
        details.append(f"Fixed in this PR: 🎉 {fixed}")
    if report.note:
        details.append(report.note)
    if details:
        lines.append(" · ".join(details))
    return "\n".join(lines)


def _render_absolute_failures(report: Report) -> str:
    failures = tuple(
        CaseChange(case_id=case.case_id, base=None, head=case)
        for case in report.head.cases
        if case.passed is False
    )
    lines = [
        MARKER,
        (
            f"## EvalFix — ⚠️ {report.head.passed}/{report.head.total} passed "
            "(no baseline comparison)"
        ),
        "",
        (
            "**What broke:** EvalFix found failing cases, but no base result was "
            "available, so they are not classified as pull-request regressions."
        ),
        "",
        _render_failing_cases(failures),
    ]
    if report.note:
        lines.extend(["", f"> {report.note}"])
    lines.extend(
        [
            "",
            "---",
            (
                f"Apply locally with `evalfix fix {_inline_code_text(report.project_dir)}`"
                f" · [docs]({DOCS_URL}) · re-run by pushing a commit"
            ),
        ]
    )
    return "\n".join(lines)


def _render_non_regression_failures(report: Report) -> str:
    failures = tuple(
        CaseChange(case_id=case.case_id, base=None, head=case)
        for case in report.head.cases
        if case.passed is False
    )
    lines = [
        MARKER,
        (
            f"## EvalFix — ⚠️ {report.head.passed}/{report.head.total} passed "
            "(no regressions)"
        ),
        "",
        (
            "**What broke:** The failing cases were already failing on the base "
            "branch or were added by this PR; no base-pass/head-fail regression "
            "was detected."
        ),
        "",
        _render_failing_cases(failures),
    ]
    if report.comparison.new_case_failures:
        new_cases = ", ".join(
            f"`{item.case_id}`" for item in report.comparison.new_case_failures
        )
        lines.extend(["", f"**New case, failing:** {new_cases}"])
    lines.extend(
        [
            "",
            "---",
            (
                f"Apply locally with `evalfix fix {_inline_code_text(report.project_dir)}`"
                f" · [docs]({DOCS_URL}) · re-run by pushing a commit"
            ),
        ]
    )
    return "\n".join(lines)


def _render_regressions(report: Report) -> str:
    regressions = report.comparison.regressions
    regression_count = len(regressions)
    noun = "regression" if regression_count == 1 else "regressions"
    base_suffix = ""
    if report.base is not None:
        base_suffix = f" (base: {report.base.passed}/{report.base.total})"

    lines = [
        MARKER,
        (
            f"## EvalFix — {regression_count} {noun}  ❌ "
            f"{report.head.passed}/{report.head.total} passed{base_suffix}"
        ),
        "",
        f"**What broke:** {_pattern_summary(report)}",
    ]

    if report.fix is not None and report.fix.root_cause is not None:
        confidence = _format_confidence(report.fix.confidence)
        root_cause = _inline_code(report.fix.root_cause)
        lines.extend(["", f"**Root cause:** {root_cause} · confidence {confidence}"])

    lines.extend(["", _render_failing_cases(regressions)])

    if report.comparison.new_case_failures:
        new_cases = ", ".join(
            f"`{item.case_id}`" for item in report.comparison.new_case_failures
        )
        lines.extend(["", f"**New case, failing:** {new_cases}"])

    if report.fix is not None and report.fix.diff is not None:
        lines.extend(
            [
                "",
                "### Proposed fix (not applied)",
                "```diff",
                _clean_diff(report.fix.diff),
                "```",
                _render_evidence(report.fix),
            ]
        )

    if report.note:
        lines.extend(["", f"> {report.note}"])

    lines.extend(
        [
            "",
            "---",
            (
                f"Apply locally with `evalfix fix {_inline_code_text(report.project_dir)}`"
                f" · [docs]({DOCS_URL}) · re-run by pushing a commit"
            ),
        ]
    )
    return "\n".join(lines)


def _render_failing_cases(regressions: tuple[CaseChange, ...]) -> str:
    lines = [
        f"<details><summary>Failing cases ({len(regressions)})</summary>",
        "",
        "| case | grader | why it failed |",
        "|---|---|---|",
    ]
    for change in regressions:
        case = change.head
        lines.append(
            "| "
            f"`{_escape_code(case.case_id)}` | "
            f"{_escape_table(case.grader or 'unknown')} | "
            f"{_escape_table(_truncate(case.reason or 'No failure reason reported.', 120))} |"
        )
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def _render_evidence(fix: FixResult) -> str:
    after = _score_text(fix.after.passed, fix.after.total)
    intact = fix.previously_passing_intact
    if intact is True:
        count = fix.before.passed
        count_text = f"{count} " if count is not None else ""
        intact_text = f"all {count_text}previously-passing cases intact ✅"
    elif intact is False:
        intact_text = "some previously-passing cases regressed ⚠️"
    else:
        intact_text = "previously-passing case preservation was not reported"
    return f"**Evidence:** with this patch {after} pass · {intact_text}"


def _pattern_summary(report: Report) -> str:
    if report.pattern_summary:
        return _single_paragraph(report.pattern_summary)
    if report.fix is not None and report.fix.diagnosis:
        return _single_paragraph(report.fix.diagnosis)
    count = len(report.comparison.regressions)
    noun = "case passed" if count == 1 else "cases passed"
    return (
        f"{count} {noun} on the base branch but failed on the pull-request head. "
        "No diagnosis was requested."
    )


def _format_confidence(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "not reported"


def _score_text(passed: int | None, total: int | None) -> str:
    if passed is None or total is None:
        return "an unreported number of cases"
    return f"{passed}/{total}"


def _clean_diff(value: str) -> str:
    return value.strip().replace("```", "'''")


def _single_paragraph(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, limit: int) -> str:
    compact = _single_paragraph(value)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _inline_code(value: str) -> str:
    return f"`{_inline_code_text(value)}`"


def _inline_code_text(value: str) -> str:
    return value.replace("`", "'").replace("\n", " ").strip()


def _escape_code(value: str) -> str:
    return value.replace("`", "'").replace("\n", " ")


def _escape_table(value: str) -> str:
    return _single_paragraph(value).replace("|", "\\|")


def _has_marker_first(body: str) -> bool:
    first_line = body.splitlines()[0].strip() if body.splitlines() else ""
    return first_line == MARKER


def _object_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("GitHub API response was not an object.")
    return {str(key): item for key, item in value.items()}


def _required_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} did not include a numeric comment id.")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None
