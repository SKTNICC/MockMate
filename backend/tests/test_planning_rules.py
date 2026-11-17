from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.services.planning import PlanValidationContext, parse_internal_plan, validate_internal_plan

CASES_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "m1" / "rule-cases.json"
DATA = json.loads(CASES_PATH.read_text(encoding="utf-8"))
CASES = DATA["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_rule_case(case: dict) -> None:
    expected = case["expected_error"]
    ctx = PlanValidationContext(allow_test_fixtures=True)
    if expected is None:
        plan = parse_internal_plan(case["plan"])
        validate_internal_plan(plan, ctx)
        assert plan.test_only is True
        return
    with pytest.raises(AppError) as caught:
        plan = parse_internal_plan(case["plan"])
        validate_internal_plan(plan, ctx)
    assert caught.value.code == expected, (caught.value.code, caught.value.details)


def test_rule_fixture_is_marked_test_only() -> None:
    assert DATA["test_only"] is True
    for case in CASES:
        assert case["plan"]["test_only"] is True
        assert case["plan"]["model_id"] == "SYNTHETIC-NO-AI-CALL"


def test_rule_test_content_rejected_without_test_mode() -> None:
    valid = next(case for case in CASES if case["id"] == "valid-six-test-only")
    plan = parse_internal_plan(valid["plan"])
    with pytest.raises(AppError) as caught:
        validate_internal_plan(plan, PlanValidationContext(allow_test_fixtures=False))
    assert caught.value.code == "BANK_INELIGIBLE"
