"""Tests for QA Full Read Visibility & Persistent Agent Memory (SDP-MEM-001)."""

from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from validators.kernel.contract import AgentContract, ContractEvaluator
from validators.harness.runner.agent_memory import (
    build_agent_persistent_memory_context,
    get_agent_self_memory,
    get_completed_deliverables_summary,
    get_task_attempt_history,
)


def test_qa_agent_unrestricted_read_access():
    """Verify QA agent (gemma) has full read visibility across all workspace code and configs."""
    evaluator = ContractEvaluator()
    qa_contract = AgentContract(
        agent_id="gemma",
        work_order="WO-009",
        allow=(
            "tests/**",
            "workspace/tests/**",
            ".sync/reviews/**",
            "workspace/.sync/reviews/**",
        ),
        deny=(
            ".git/**",
            "workspace/.git/**",
            "package.json",
            "workspace/package.json",
        ),
        write_mode="read-write",
    )

    # 1. Reading application files outside explicit allow (must be AUTHORIZED for QA)
    assert evaluator.authorize(qa_contract, "read_file", "workspace/index.html") == (True, "authorized")
    assert evaluator.authorize(qa_contract, "read_file", "workspace/assets/js/ui.js") == (True, "authorized")
    assert evaluator.authorize(qa_contract, "read_file", "workspace/assets/css/style.css") == (True, "authorized")
    assert evaluator.authorize(qa_contract, "read_file", "workspace/src/calculator-core.js") == (True, "authorized")
    assert evaluator.authorize(qa_contract, "read_file", "workspace/vitest.config.js") == (True, "authorized")

    # 2. Reading package.json (even if in deny list, read is permitted unless secret/credential)
    assert evaluator.authorize(qa_contract, "read_file", "workspace/package.json") == (True, "authorized")

    # 3. Directory listing must be AUTHORIZED
    assert evaluator.authorize(qa_contract, "list_directory", "workspace/src") == (True, "authorized")
    assert evaluator.authorize(qa_contract, "list_directory", "workspace/assets") == (True, "authorized")

    # 4. Security boundaries must still be strictly ENFORCED
    assert evaluator.authorize(qa_contract, "read_file", "workspace/.env")[0] is False
    assert evaluator.authorize(qa_contract, "read_file", "workspace/.git/config")[0] is False
    assert evaluator.authorize(qa_contract, "read_file", "workspace/.sync/inbox/claude/secret.key")[0] is False

    # 5. Write permissions are still strictly restricted to allowed modules (tests/**)
    assert evaluator.authorize(qa_contract, "write_file", "workspace/index.html")[0] is False
    assert evaluator.authorize(qa_contract, "write_file", "workspace/tests/test_calculator.py") == (True, "authorized")


def test_agent_persistent_memory_assembly(tmp_path: Path):
    """Verify persistent memory correctly extracts outbox reports, attempt history, and peer deliverables."""
    # 1. Setup mock outbox for codex
    outbox_dir = tmp_path / ".sync" / "outbox" / "codex"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    report_content = (
        "# Harness Report: WO-006\n\n"
        "- agent: `codex`\n"
        "- task: `WO-006`\n"
        "- status: `completed`\n\n"
        "## Summary\n"
        "Authored package.json with vitest and jsdom devDependencies.\n\n"
        "## Blockers\n"
        "Directory skeleton pending companion orders.\n"
    )
    (outbox_dir / "harness-2026-11-02T22-38-15.md").write_text(report_content, encoding="utf-8")

    # 2. Setup mock work order with history and amendments
    active_wo_dir = tmp_path / ".sync" / "work-orders" / "ACTIVE"
    active_wo_dir.mkdir(parents=True, exist_ok=True)
    wo_007_yaml = {
        "id": "WO-007",
        "title": "Calculator Core Engine",
        "status": "ACTIVE",
        "revision": 2,
        "error": "SyntaxError in previous parser draft",
        "amendments": [
            {"revision": 2, "reason": "Added support for operator precedence and parenthesis nesting"}
        ],
        "log": [
            "2026-11-02T22:45:00: Harness completed initial pass with tokenizer"
        ],
    }
    (active_wo_dir / "WO-007.yaml").write_text(yaml.dump(wo_007_yaml), encoding="utf-8")

    # 3. Setup completed work orders for peer deliverables
    completed_wo_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
    completed_wo_dir.mkdir(parents=True, exist_ok=True)
    wo_000_yaml = {
        "id": "WO-000",
        "title": "System Architecture & Planning",
        "assigned_agents": ["claude"],
        "deliverable": {"path": "PLAN.md"},
    }
    (completed_wo_dir / "WO-000.yaml").write_text(yaml.dump(wo_000_yaml), encoding="utf-8")

    wo_006_yaml = {
        "id": "WO-006",
        "title": "Project Scaffolding & Manifest",
        "assigned_agents": ["codex"],
        "deliverable": {"path": "package.json"},
    }
    (completed_wo_dir / "WO-006.yaml").write_text(yaml.dump(wo_006_yaml), encoding="utf-8")

    # 4. Verify self memory
    self_mem = get_agent_self_memory(tmp_path, "codex")
    assert "YOUR PREVIOUS WORK & MEMORY" in self_mem
    assert "[WO-006] (Status: completed): Authored package.json with vitest and jsdom" in self_mem
    assert "Directory skeleton pending companion orders" in self_mem

    # 5. Verify task attempt history
    task_hist = get_task_attempt_history(tmp_path, "WO-007")
    assert "CURRENT WORK ORDER: WO-007, REVISION 2" in task_hist
    assert "Prior Issue / Blocker: SyntaxError in previous parser draft" in task_hist
    assert "Rev 2: Added support for operator precedence" in task_hist
    assert "revision: 3" in task_hist  # Invariant reminder

    # 6. Verify completed deliverables summary
    deliv_summary = get_completed_deliverables_summary(tmp_path)
    assert "COMPLETED PROJECT DELIVERABLES" in deliv_summary
    assert "WO-000 (claude): System Architecture & Planning -> `PLAN.md`" in deliv_summary
    assert "WO-006 (codex): Project Scaffolding & Manifest -> `package.json`" in deliv_summary

    # 7. Verify full assembly
    full_ctx = build_agent_persistent_memory_context(tmp_path, "codex", work_order_id="WO-007")
    assert "YOUR PREVIOUS WORK & MEMORY" in full_ctx
    assert "TASK ATTEMPT & AMENDMENT HISTORY" in full_ctx
    assert "COMPLETED PROJECT DELIVERABLES" in full_ctx
