"""Friendly onboarding comments for known EvalFix Action failure states.

Gate 3 defines and snapshot-tests the user-facing markdown. Gate 4 detects
these states, posts the rendered comment, and selects the final exit code.
"""

from __future__ import annotations

from comment import DOCS_URL, MARKER

SECRETS_DOCS_URL = (
    "https://docs.github.com/en/actions/security-for-github-actions/"
    "security-guides/using-secrets-in-github-actions"
)
ISSUES_URL = "https://github.com/evalfix/evalfix-action/issues/new"


def missing_anthropic_key() -> str:
    return _error(
        "ANTHROPIC_API_KEY is not configured",
        "EvalFix needs an Anthropic API key to run your agent evals.",
        (
            "Add these two lines to the workflow step that uses EvalFix:\n\n"
            "```yaml\n"
            "env:\n"
            "  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n"
            "```\n\n"
            f"Then add the repository secret in GitHub. [GitHub secrets docs]"
            f"({SECRETS_DOCS_URL})"
        ),
    )


def missing_project_files(project_dir: str) -> str:
    safe_dir = _inline(project_dir)
    return _error(
        "Agent eval files were not found",
        f"EvalFix expected this layout at `{safe_dir}`:",
        (
            "```text\n"
            f"{safe_dir}/\n"
            "├── prompt.txt\n"
            "└── evals.yaml\n"
            "```\n\n"
            f"Create the files with `evalfix init {safe_dir}` and push them to the PR. "
            f"[Read the setup docs]({DOCS_URL})"
        ),
    )


def invalid_evals_yaml(error_message: str) -> str:
    return _error(
        "evals.yaml could not be parsed",
        "Fix the YAML error below and push a new commit:",
        (
            f"```text\n{_fenced(error_message)}\n```\n\n"
            "Minimal valid example:\n\n"
            "```yaml\n"
            "tests:\n"
            "  - id: greeting\n"
            '    input: "Hello"\n'
            '    expected: "Respond politely"\n'
            "    grader: semantic\n"
            "```"
        ),
    )


def cli_failure(stderr: str, *, timed_out: bool) -> str:
    title = "EvalFix timed out" if timed_out else "EvalFix CLI failed"
    guidance = (
        "Increase `timeout_minutes` or reduce the number of cases, then re-run the workflow."
        if timed_out
        else "Check the CLI output below, then re-run the workflow."
    )
    return _error(
        title,
        guidance,
        (
            "Last CLI output:\n\n"
            f"```text\n{_last_lines(stderr, 20)}\n```\n\n"
            f"If this persists, [open an issue]({ISSUES_URL}) with the workflow logs."
        ),
    )


def _error(title: str, summary: str, instructions: str) -> str:
    return "\n".join(
        [
            MARKER,
            "## EvalFix — setup needs attention ⚠️",
            "",
            f"### {title}",
            "",
            summary,
            "",
            instructions,
        ]
    )


def _last_lines(value: str, count: int) -> str:
    lines = value.strip().splitlines()
    selected = lines[-count:] if lines else ["No stderr was captured."]
    return _fenced("\n".join(selected))


def _fenced(value: str) -> str:
    return value.strip().replace("```", "'''")


def _inline(value: str) -> str:
    return value.replace("`", "'").replace("\n", " ").strip()
