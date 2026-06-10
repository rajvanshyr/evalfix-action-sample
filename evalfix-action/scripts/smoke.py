"""Manual end-to-end smoke test using the installed EvalFix CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY before running `make smoke`.", file=sys.stderr)
        return 2
    if shutil.which("evalfix") is None:
        print("Install the real CLI first: `python -m pip install evalfix`.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="evalfix-smoke-") as raw_temp:
        repository = Path(raw_temp)
        project = repository / "demo-agent"
        shutil.copytree(ROOT / "examples/demo-agent", project)
        _git(repository, "init")
        _git(repository, "config", "user.email", "smoke@evalfix.local")
        _git(repository, "config", "user.name", "EvalFix Smoke")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "working agent")
        base_sha = _git(repository, "rev-parse", "HEAD").strip()

        project.joinpath("prompt.txt").write_text(
            (
                "You are a customer support agent for Acme Devices.\n\n"
                "Answer clearly and concisely. You may approve a refund when the "
                "purchase was made within the last 30 days.\n"
                "When a refund is not available, explain the reason and offer "
                "escalation to a human support specialist.\n"
            ),
            encoding="utf-8",
        )

        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "GITHUB_WORKSPACE": str(repository),
            "GITHUB_REPOSITORY": "local/evalfix-smoke",
            "GITHUB_TOKEN": "local-only",
            "GITHUB_STEP_SUMMARY": str(repository / "step-summary.md"),
            "GITHUB_OUTPUT": str(repository / "outputs.txt"),
            "INPUT_PROJECT_DIR": "demo-agent",
            "INPUT_SUGGEST_FIX": "true",
            "INPUT_FAIL_ON_REGRESSION": "true",
            "INPUT_BASELINE": "base-branch",
            "INPUT_MAX_CASES": "100",
            "INPUT_TIMEOUT_MINUTES": "15",
            "INPUT_COMMENT": "false",
            "EVALFIX_BASE_REF": base_sha,
        }
        completed = subprocess.run(
            (sys.executable, str(ROOT / "src/main.py")),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        print(completed.stdout)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)

        required = (
            "<!-- evalfix-action -->",
            "regression",
            "Proposed fix (not applied)",
        )
        if not all(item in completed.stdout for item in required):
            print("Smoke output did not contain the full regression comment.", file=sys.stderr)
            return 1
        if completed.returncode != 1:
            print("Smoke run should exit 1 because it introduces a regression.", file=sys.stderr)
            return 1
    return 0


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


if __name__ == "__main__":
    raise SystemExit(main())
