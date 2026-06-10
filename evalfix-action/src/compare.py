"""Classify behavioral changes between base and pull-request head eval runs."""

from __future__ import annotations

from dataclasses import dataclass

from evalfix_adapter import CaseResult, RunResult


@dataclass(frozen=True, slots=True)
class CaseChange:
    """One case whose status is relevant to the pull-request comparison."""

    case_id: str
    base: CaseResult | None
    head: CaseResult


@dataclass(frozen=True, slots=True)
class Comparison:
    """Disjoint categories of behavioral changes introduced by a pull request."""

    regressions: tuple[CaseChange, ...]
    pre_existing_failures: tuple[CaseChange, ...]
    new_case_failures: tuple[CaseChange, ...]
    fixed: tuple[CaseChange, ...]
    unchanged_passing: tuple[CaseChange, ...]
    unknown: tuple[CaseChange, ...]
    score_delta: float | None
    warnings: tuple[str, ...] = ()


def compare_runs(base: RunResult, head: RunResult) -> Comparison:
    """Compare cases by id, using base-pass/head-fail as the regression rule."""

    base_by_id, base_warnings = _index_cases(base.cases, "base")
    head_by_id, head_warnings = _index_cases(head.cases, "head")
    warnings = [*base_warnings, *head_warnings]

    regressions: list[CaseChange] = []
    pre_existing_failures: list[CaseChange] = []
    new_case_failures: list[CaseChange] = []
    fixed: list[CaseChange] = []
    unchanged_passing: list[CaseChange] = []
    unknown: list[CaseChange] = []

    for case_id in sorted(head_by_id):
        head_case = head_by_id[case_id]
        base_case = base_by_id.get(case_id)
        change = CaseChange(case_id=case_id, base=base_case, head=head_case)

        if base_case is None:
            if head_case.passed is False:
                new_case_failures.append(change)
            elif head_case.passed is None:
                unknown.append(change)
            continue

        if base_case.passed is True and head_case.passed is False:
            regressions.append(change)
        elif base_case.passed is False and head_case.passed is False:
            pre_existing_failures.append(change)
        elif base_case.passed is False and head_case.passed is True:
            fixed.append(change)
        elif base_case.passed is True and head_case.passed is True:
            unchanged_passing.append(change)
        else:
            unknown.append(change)

    base_only = sorted(set(base_by_id) - set(head_by_id))
    if base_only:
        warnings.append(
            "Cases present only in the base result were excluded: " + ", ".join(base_only)
        )

    return Comparison(
        regressions=tuple(regressions),
        pre_existing_failures=tuple(pre_existing_failures),
        new_case_failures=tuple(new_case_failures),
        fixed=tuple(fixed),
        unchanged_passing=tuple(unchanged_passing),
        unknown=tuple(unknown),
        score_delta=_score_delta(base, head),
        warnings=tuple(warnings),
    )


def _index_cases(
    cases: tuple[CaseResult, ...],
    label: str,
) -> tuple[dict[str, CaseResult], list[str]]:
    indexed: dict[str, CaseResult] = {}
    warnings: list[str] = []
    for case in cases:
        if case.case_id in indexed:
            warnings.append(
                f"Duplicate case id '{case.case_id}' in {label} results; the last result was used."
            )
        indexed[case.case_id] = case
    return indexed, warnings


def _score_delta(base: RunResult, head: RunResult) -> float | None:
    base_score = base.score
    head_score = head.score
    if base_score is None or head_score is None:
        return None
    return head_score - base_score
