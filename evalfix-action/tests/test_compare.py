from compare import compare_runs
from evalfix_adapter import CaseResult, RunResult


def _case(case_id: str, passed: bool | None) -> CaseResult:
    return CaseResult(case_id=case_id, passed=passed)


def _run(*cases: CaseResult, score: float | None = None) -> RunResult:
    passed = sum(case.passed is True for case in cases)
    failed = sum(case.passed is False for case in cases)
    return RunResult(
        total=len(cases),
        passed=passed,
        failed=failed,
        score=score,
        cases=cases,
    )


def test_distinguishes_all_required_change_categories() -> None:
    base = _run(
        _case("regression", True),
        _case("pre_existing", False),
        _case("fixed", False),
        _case("still_passing", True),
        score=0.5,
    )
    head = _run(
        _case("regression", False),
        _case("pre_existing", False),
        _case("fixed", True),
        _case("still_passing", True),
        _case("new_failure", False),
        _case("new_pass", True),
        score=0.5,
    )

    comparison = compare_runs(base, head)

    assert [item.case_id for item in comparison.regressions] == ["regression"]
    assert [item.case_id for item in comparison.pre_existing_failures] == ["pre_existing"]
    assert [item.case_id for item in comparison.new_case_failures] == ["new_failure"]
    assert [item.case_id for item in comparison.fixed] == ["fixed"]
    assert [item.case_id for item in comparison.unchanged_passing] == ["still_passing"]
    assert comparison.unknown == ()
    assert comparison.score_delta == 0.0


def test_unknown_status_is_not_mislabeled_as_regression() -> None:
    comparison = compare_runs(
        _run(_case("unknown_head", True), _case("unknown_base", None)),
        _run(_case("unknown_head", None), _case("unknown_base", False)),
    )

    assert comparison.regressions == ()
    assert [item.case_id for item in comparison.unknown] == [
        "unknown_base",
        "unknown_head",
    ]


def test_pre_existing_failures_do_not_count_as_regressions() -> None:
    comparison = compare_runs(
        _run(_case("existing_failure", False)),
        _run(_case("existing_failure", False)),
    )

    assert comparison.regressions == ()
    assert len(comparison.pre_existing_failures) == 1


def test_duplicate_and_base_only_cases_produce_warnings() -> None:
    comparison = compare_runs(
        _run(
            _case("duplicate", True),
            _case("duplicate", False),
            _case("removed", True),
        ),
        _run(_case("duplicate", True)),
    )

    assert len(comparison.warnings) == 2
    assert "Duplicate case id 'duplicate'" in comparison.warnings[0]
    assert "removed" in comparison.warnings[1]


def test_score_delta_is_none_when_either_score_is_unavailable() -> None:
    comparison = compare_runs(
        _run(_case("case", True), score=None),
        _run(_case("case", True), score=1.0),
    )

    assert comparison.score_delta is None
