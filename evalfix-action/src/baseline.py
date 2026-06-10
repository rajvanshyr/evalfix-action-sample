"""Evaluate the pull request's base revision in an isolated git worktree."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from evalfix_adapter import CliExecution, invoke_run


@dataclass(frozen=True, slots=True)
class BaselineExecution:
    """Result of preparing and evaluating the base revision."""

    execution: CliExecution | None
    warning: str | None = None


def run_base_evaluation(
    *,
    repository_root: Path,
    project_dir: Path,
    head_evals_path: Path,
    base_ref: str,
    timeout_seconds: float,
) -> BaselineExecution:
    """Run base prompt/code with the head branch's eval cases."""

    worktree_root = Path(tempfile.mkdtemp(prefix="evalfix-base-"))
    added = False
    try:
        merge_base = _git(
            repository_root,
            "merge-base",
            "HEAD",
            base_ref,
        ).strip()
        if not merge_base:
            return BaselineExecution(None, "Git returned an empty merge-base.")

        _git(
            repository_root,
            "worktree",
            "add",
            "--detach",
            str(worktree_root),
            merge_base,
        )
        added = True

        base_project = worktree_root / project_dir
        if not (base_project / "prompt.txt").is_file():
            return BaselineExecution(
                None,
                f"Base revision has no prompt.txt at {project_dir}.",
            )

        base_project.mkdir(parents=True, exist_ok=True)
        shutil.copy2(head_evals_path, base_project / "evals.yaml")
        return BaselineExecution(
            invoke_run(base_project, timeout_seconds=timeout_seconds)
        )
    except (OSError, subprocess.CalledProcessError) as error:
        return BaselineExecution(None, _command_error(error))
    finally:
        if added:
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(repository_root),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_root),
                ),
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(worktree_root, ignore_errors=True)


def resolve_base_ref(repository_root: Path, base_branch: str) -> str:
    """Prefer the fetched remote base branch and fall back to a local ref."""

    remote_ref = f"origin/{base_branch}"
    probe = subprocess.run(
        ("git", "-C", str(repository_root), "rev-parse", "--verify", remote_ref),
        check=False,
        capture_output=True,
        text=True,
    )
    return remote_ref if probe.returncode == 0 else base_branch


def _git(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository_root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _command_error(error: OSError | subprocess.CalledProcessError) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        stderr = error.stderr.strip() if isinstance(error.stderr, str) else ""
        return stderr or str(error)
    return str(error)
