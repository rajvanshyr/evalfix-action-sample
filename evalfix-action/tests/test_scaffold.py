from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_composite_action_contract() -> None:
    action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))

    assert action["runs"]["using"] == "composite"
    assert set(action["inputs"]) == {
        "project_dir",
        "suggest_fix",
        "fail_on_regression",
        "baseline",
        "max_cases",
        "timeout_minutes",
        "comment",
    }
    assert set(action["outputs"]) == {"passed", "total", "regressions", "score_delta"}
    assert action["inputs"]["project_dir"]["required"] is True
    assert action["inputs"]["baseline"]["default"] == "base-branch"


def test_required_repository_layout_exists() -> None:
    required_paths = [
        "action.yml",
        "src/main.py",
        "src/evalfix_adapter.py",
        "src/baseline.py",
        "src/compare.py",
        "src/comment.py",
        "src/errors.py",
        "tests/fixtures",
        "examples/demo-agent/prompt.txt",
        "examples/demo-agent/evals.yaml",
        "Makefile",
        "scripts/smoke.py",
        "README.md",
        ".github/workflows/ci.yml",
    ]

    for relative_path in required_paths:
        assert (ROOT / relative_path).exists(), relative_path
