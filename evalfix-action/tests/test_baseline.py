from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from baseline import run_base_evaluation
from evalfix_adapter import CliExecution


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_base_uses_base_prompt_with_head_evals(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "EvalFix Test")

    project = repository / "agent"
    project.mkdir()
    project.joinpath("prompt.txt").write_text("base prompt", encoding="utf-8")
    project.joinpath("evals.yaml").write_text(
        "tests:\n  - id: base\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")

    project.joinpath("prompt.txt").write_text("head prompt", encoding="utf-8")
    project.joinpath("evals.yaml").write_text(
        "tests:\n  - id: head-new-case\n",
        encoding="utf-8",
    )

    observed: dict[str, str] = {}

    def fake_invoke(project_dir: Path, *, timeout_seconds: float) -> CliExecution:
        observed["prompt"] = project_dir.joinpath("prompt.txt").read_text(
            encoding="utf-8"
        )
        observed["evals"] = project_dir.joinpath("evals.yaml").read_text(
            encoding="utf-8"
        )
        return CliExecution(("evalfix",), 0, '{"results":[]}', "")

    with patch("baseline.invoke_run", side_effect=fake_invoke):
        result = run_base_evaluation(
            repository_root=repository,
            project_dir=Path("agent"),
            head_evals_path=project / "evals.yaml",
            base_ref=base_sha,
            timeout_seconds=30,
        )

    assert result.execution is not None
    assert observed["prompt"] == "base prompt"
    assert "head-new-case" in observed["evals"]
    assert "base" not in observed["evals"]
    assert _git(repository, "worktree", "list").count("\n") == 0
