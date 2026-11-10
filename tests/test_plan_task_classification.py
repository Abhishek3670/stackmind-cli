"""Only genuine planning turns may be classified as the PLAN.md task."""
from __future__ import annotations

import pytest

from validators.harness.runner.completion import _is_plan_task


def _classify(**overrides) -> bool:
    args = dict(
        is_architecture=False,
        is_delivery_status=False,
        is_authoring_task=False,
        is_integration_review=False,
        deliverable_path="",
        work_order_id=None,
        title="",
        body="",
    )
    args.update(overrides)
    return _is_plan_task(**args)


def test_qa_fallback_prompt_mentioning_plan_md_is_not_a_planning_turn() -> None:
    # _qa_review_prompt emits this text for work orders with no criteria/test_plan.
    assert _classify(
        is_architecture=False,
        title="QA review WO-002",
        body="- (derive from the approved PLAN.md scope and contract)",
        deliverable_path="assets/js/scene.js",
        work_order_id="WO-002",
    ) is False


def test_worker_task_mentioning_plan_md_is_not_a_planning_turn() -> None:
    assert _classify(body="Implement per PLAN.md section 3", work_order_id="WO-004") is False


@pytest.mark.parametrize("path", ["PLAN.md", ".sync/playbook/PLAN.md", "docs/PLAN.md", ".sync\\playbook\\PLAN.md"])
def test_plan_deliverable_path_is_a_planning_turn_for_any_agent(path: str) -> None:
    assert _classify(deliverable_path=path) is True


def test_wo_000_is_a_planning_turn() -> None:
    assert _classify(work_order_id="WO-000", is_architecture=True) is True


def test_architect_text_mention_still_counts() -> None:
    assert _classify(is_architecture=True, title="Write PLAN.md for the project") is True


@pytest.mark.parametrize("flag", ["is_delivery_status", "is_authoring_task", "is_integration_review"])
def test_other_task_phases_take_precedence(flag: str) -> None:
    assert _classify(**{flag: True}, is_architecture=True, deliverable_path="PLAN.md", work_order_id="WO-000") is False
