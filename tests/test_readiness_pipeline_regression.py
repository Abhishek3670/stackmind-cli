"""End-to-end regression tests for the hardened authoring → readiness pipeline.

Reproduces the clean_tui_test production failure (2026-10-06): an architect
authored schema-valid work orders whose titles omitted the plan's
"(Agent: gemini)" annotations, a QA contract without the verdict channel, and
no per-deliverable test plan — the readiness gate rejected the set and two
repair rounds failed on "(affected: none)" guidance.

After the hardening: the deterministic compiler injects the system-owned
invariants, the readiness gate validates the canonical set with actionable
structured diagnostics, and the repair prompt names the real affected
artifacts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.test_authoring_compiler import make_contract, make_wo, write_contract, write_wo
from tests.test_lifecycle_supervisor import MockSessionManager
from validators.harness.authoring_compiler import compile_authoring_artifacts
from validators.harness.authoring_readiness import (
    load_expected_milestones,
    load_plan_milestones,
    milestone_matches,
    validate_authoring_readiness,
)
from validators.harness.d024_gate import D024Gate
from validators.harness.plan import parse_plan
from validators.kernel.daemon.supervisor import LifecycleSupervisor


PLAN_MD = (
    "# Project Plan: Static Welcome Page with Animated Wave\n\n"
    "## Current Architecture\n"
    "Standalone HTML/CSS/JS, zero dependencies.\n\n"
    "## Milestones & Roadmap\n"
    "- [ ] Milestone 1: Frontend Scaffolding (Agent: gemini)\n"
    "  - [ ] Task 1.1: Create public/index.html\n"
    "  - [ ] Task 1.2: Create public/style.css\n"
    "- [ ] Milestone 2: Wave Background Implementation (Agent: gemini)\n"
    "  - [ ] Task 2.1: Implement the wave SVG and CSS keyframes\n"
    "- [ ] Milestone 3: Animated Welcome Text (Agent: gemini)\n"
    "  - [ ] Task 3.1: Write public/script.js\n"
    "- [ ] Milestone 4: QA & Visual Verification (Agent: gemma)\n"
    "  - [ ] Task 4.1: Author and execute tests/test_visuals.py\n"
    "- [ ] Milestone 5: Finalization & GitOps (Agent: local-llm)\n"
    "  - [ ] Task 5.1: Update CHANGELOG.md\n"
)


def build_clean_tui_test_artifacts(ws: Path) -> None:
    """Replicate the exact authored artifact set from the failed run."""
    ws.joinpath("PLAN.md").write_text(PLAN_MD, encoding="utf-8")
    write_wo(ws, make_wo("WO-001", "gemini", "public/index.html", "config",
                         title="Frontend Scaffolding"))
    write_wo(ws, make_wo("WO-002", "gemini", "public/style.css",
                         title="Wave Background Implementation"))
    write_wo(ws, make_wo("WO-003", "gemini", "public/script.js",
                         title="Animated Welcome Text"))
    write_wo(ws, make_wo("WO-004", "gemma", "tests/test_visuals.py",
                         title="QA & Visual Verification"))
    write_wo(ws, make_wo("WO-005", "local-llm", "CHANGELOG.md", "doc",
                         title="Finalization & GitOps"))
    write_contract(ws, make_contract("WO-001", "gemini", [{"module": "public/**"}]))
    write_contract(ws, make_contract("WO-002", "gemini", [{"module": "public/**"}]))
    write_contract(ws, make_contract("WO-003", "gemini", [{"module": "public/**"}]))
    write_contract(ws, make_contract("WO-004", "gemma", [{"module": "public/**"}, {"module": "tests/**"}]))
    write_contract(ws, make_contract("WO-005", "local-llm", [{"module": "CHANGELOG.md"}]))


# ─── Milestone identity parsing/matching ──────────────────────────────

class TestMilestoneIdentity:
    def test_agent_annotation_is_metadata_not_title(self) -> None:
        parsed = parse_plan(PLAN_MD)
        first = parsed.milestones[0]
        assert first.id == "Milestone 1"
        assert first.title == "Frontend Scaffolding"  # annotation separated
        assert first.agent == "gemini"
        assert parsed.milestones[3].agent == "gemma"
        assert parsed.milestones[4].agent == "local-llm"

    def test_annotation_no_longer_breaks_title_matching(self) -> None:
        # Regression: the legacy matcher scored this pair 2/4 = 0.5 < 0.6 and
        # raised MILESTONE_UNCOVERED for the flagship milestone of the run.
        assert milestone_matches("Frontend Scaffolding (Agent: gemini)", "Frontend Scaffolding")

    def test_load_plan_milestones_returns_structured_refs(self, tmp_path: Path) -> None:
        tmp_path.joinpath("PLAN.md").write_text(PLAN_MD, encoding="utf-8")
        refs = load_plan_milestones(tmp_path)
        assert refs[0] == {"id": "Milestone 1", "title": "Frontend Scaffolding", "agent": "gemini"}
        # Backward-compatible titles helper still works
        assert load_expected_milestones(tmp_path)[0] == "Frontend Scaffolding"

    def test_agent_assignment_uses_parsed_metadata_not_title_text(self) -> None:
        # Regression guard: parse_plan strips "(Agent: gemini)" from the title,
        # so downstream assignment must come from the parsed metadata hint —
        # "Wave Background Implementation" has no assignable keyword in it.
        from validators.kernel.daemon.authoring import determine_assigned_agent

        parsed = parse_plan(PLAN_MD)
        wave = parsed.milestones[1]
        assert wave.agent == "gemini"
        agent, role = determine_assigned_agent(wave.title, wave.tasks, agent_hint=wave.agent)
        assert (agent, role) == ("gemini", "frontend")


class TestRepairTurnTaskDiscovery:
    def test_discover_explicit_work_order_resolves_from_blocked_dir(
        self, tmp_path: Path,
    ) -> None:
        """Regression: recovery repair turns target work orders that the
        recovery itself moved to BLOCKED — discover_next_task must resolve
        the explicit id from BLOCKED/COMPLETED instead of falling back to
        the first inbox item (the stale-notice hijack)."""
        from validators.harness.runner import AgentRunner

        runtime = tmp_path / ".sync" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "TREE.yaml").write_text(
            "schema_version: 1\ntree_version: 1\n", encoding="utf-8"
        )
        blocked = tmp_path / ".sync" / "work-orders" / "BLOCKED"
        blocked.mkdir(parents=True)
        (blocked / "WO-001.yaml").write_text(yaml.safe_dump({
            "id": "WO-001", "type": "FEATURE", "title": "Blocked child",
            "status": "BLOCKED", "priority": "P1", "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "code", "path": "src/core/app.py", "description": "child"},
            "description": "child",
        }), encoding="utf-8")
        inbox = tmp_path / ".sync" / "inbox" / "claude"
        inbox.mkdir(parents=True)
        (inbox / "2026-10-07_claude_WO-000-complete.md").write_text("done", encoding="utf-8")

        runner = AgentRunner(tmp_path, "claude")
        task = runner.discover_next_task(runner._load_tree(), work_order_id="WO-001")

        assert task is not None
        assert task.kind == "work_order"
        assert task.identifier == "WO-001"


# ─── The clean_tui_test failure, end to end ───────────────────────────

class TestCleanTuiTestRegression:
    def test_raw_authored_set_fails_with_actionable_diagnostics(self, tmp_path: Path) -> None:
        build_clean_tui_test_artifacts(tmp_path)
        refs = load_plan_milestones(tmp_path)

        result = validate_authoring_readiness(tmp_path, expected_milestones=refs)

        assert not result.ready
        codes = result.issue_codes()
        assert "QA_VERDICT_CHANNEL_MISSING" in codes
        assert [i.code for i in result.issues].count("TEST_COVERAGE_UNPLANNED") == 2
        # The matcher fix means the annotated milestone now matches by title…
        assert "MILESTONE_UNCOVERED" not in codes
        # …and every diagnostic is actionable without inferring anything.
        for issue in result.issues:
            assert issue.required_action, issue.code
            assert issue.canonical_rule, issue.code

    def test_compiled_set_passes_when_coverage_is_declared(self, tmp_path: Path) -> None:
        build_clean_tui_test_artifacts(tmp_path)
        refs = load_plan_milestones(tmp_path)

        # 1. Compiler injects the system-owned invariants.
        compile_result = compile_authoring_artifacts(tmp_path, refs)
        assert not compile_result.decision_required
        wo001 = yaml.safe_load(
            (tmp_path / ".sync/work-orders/ACTIVE/WO-001.yaml").read_text(encoding="utf-8")
        )
        assert wo001["milestone_id"] == "Milestone 1"
        assert wo001["normalized_by"].startswith("authoring-compiler/")
        contract004 = yaml.safe_load(
            (tmp_path / ".sync/contracts/WO-004.yaml").read_text(encoding="utf-8")
        )
        assert {"module": ".sync/inbox/claude/**"} in contract004["scope"]["allow"]

        # 2. The consolidated QA suite is declared explicitly (what the
        #    repair turn would produce under the explicit-test-plan policy).
        wo004_path = tmp_path / ".sync/work-orders/ACTIVE/WO-004.yaml"
        wo004 = yaml.safe_load(wo004_path.read_text(encoding="utf-8"))
        wo004["test_plan"] = [{
            "sources": ["public/style.css", "public/script.js"],
            "tests": ["tests/test_visuals.py"],
        }]
        wo004_path.write_text(yaml.safe_dump(wo004, sort_keys=False), encoding="utf-8")

        # 3. The readiness gate validates the canonical set and passes.
        result = validate_authoring_readiness(tmp_path, expected_milestones=refs)
        assert result.ready, [i.message for i in result.issues]

    def test_milestone_id_primary_matching_rejects_wrong_ids(self, tmp_path: Path) -> None:
        build_clean_tui_test_artifacts(tmp_path)
        refs = load_plan_milestones(tmp_path)
        compile_authoring_artifacts(tmp_path, refs)

        # Point WO-001 at the wrong milestone: title similarity must not save it.
        wo001_path = tmp_path / ".sync/work-orders/ACTIVE/WO-001.yaml"
        wo001 = yaml.safe_load(wo001_path.read_text(encoding="utf-8"))
        wo001["milestone_id"] = "Milestone 3"
        wo001_path.write_text(yaml.safe_dump(wo001, sort_keys=False), encoding="utf-8")

        result = validate_authoring_readiness(tmp_path, expected_milestones=refs)
        assert not result.ready
        uncovered = [i for i in result.issues if i.code == "MILESTONE_UNCOVERED"]
        assert any("Frontend Scaffolding" in i.message for i in uncovered)

    def test_supervisor_gate_runs_compiler_and_passes(self, tmp_path: Path) -> None:
        """The supervisor's readiness-gate phase normalizes before validating."""
        build_clean_tui_test_artifacts(tmp_path)
        # Declare coverage so the canonical set is fully conforming.
        wo004_path = tmp_path / ".sync/work-orders/ACTIVE/WO-004.yaml"
        wo004 = yaml.safe_load(wo004_path.read_text(encoding="utf-8"))
        wo004["test_plan"] = [{
            "sources": ["public/style.css", "public/script.js"],
            "tests": ["tests/test_visuals.py"],
        }]
        wo004_path.write_text(yaml.safe_dump(wo004, sort_keys=False), encoding="utf-8")

        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-regression", "Static welcome page", tmp_path, "sess-reg")
        state.plan_id = "PLAN-001"

        result = supervisor._run_readiness_gate(state, tmp_path)
        assert result.ready, [i.message for i in result.issues]
        # The compiler normalized the authored set on disk: QA channel injected
        # and provenance recorded (a second compile pass is a no-op).
        contract004 = yaml.safe_load(
            (tmp_path / ".sync/contracts/WO-004.yaml").read_text(encoding="utf-8")
        )
        assert {"module": ".sync/inbox/claude/**"} in contract004["scope"]["allow"]
        assert contract004["normalized_by"].startswith("authoring-compiler/")
        assert compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path)).normalized_paths == ()

    def test_supervisor_gate_routes_compiler_rejection_to_diagnostics(self, tmp_path: Path) -> None:
        build_clean_tui_test_artifacts(tmp_path)
        # Ambiguous title: WO-001 matches both "Frontend Scaffolding" (M1, by
        # containment) and the re-worded "Frontend Scaffolding Extensions" (M2,
        # exactly) — the compiler refuses to guess the milestone_id.
        tmp_path.joinpath("PLAN.md").write_text(
            PLAN_MD.replace(
                "Milestone 2: Wave Background Implementation",
                "Milestone 2: Frontend Scaffolding Extensions",
            ),
            encoding="utf-8",
        )
        wo001_path = tmp_path / ".sync/work-orders/ACTIVE/WO-001.yaml"
        wo001 = yaml.safe_load(wo001_path.read_text(encoding="utf-8"))
        wo001["title"] = "Frontend Scaffolding Extensions"
        wo001_path.write_text(yaml.safe_dump(wo001, sort_keys=False), encoding="utf-8")

        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-ambiguous", "Static welcome page", tmp_path, "sess-amb")

        result = supervisor._run_readiness_gate(state, tmp_path)
        assert not result.ready
        codes = result.issue_codes()
        assert "MILESTONE_MAPPING_AMBIGUOUS" in codes
        # Diagnostics are actionable for the repair turn.
        for issue in result.issues:
            assert issue.required_action


# ─── D024 honors declared test plans ──────────────────────────────────

class TestD024DeclaredTestPlan:
    def test_declared_consolidated_mapping_satisfies_companion_requirement(self, tmp_path: Path) -> None:
        deliverable = tmp_path / "public" / "style.css"
        deliverable.parent.mkdir(parents=True, exist_ok=True)
        deliverable.write_text("body { color: rebeccapurple; }", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_visuals.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("def test_visuals():\n    assert True\n", encoding="utf-8")

        wo_data = {
            "test_plan": [{
                "sources": ["public/style.css", "public/script.js"],
                "tests": ["tests/test_visuals.py"],
            }],
        }

        companion = D024Gate()._declared_test_plan_companion(tmp_path, wo_data, "public/style.css")
        assert companion == test_file

    def test_declared_mapping_with_missing_test_file_is_not_satisfied(self, tmp_path: Path) -> None:
        (tmp_path / "public").mkdir(parents=True, exist_ok=True)
        (tmp_path / "public" / "script.js").write_text("console.log('hi');", encoding="utf-8")
        wo_data = {"test_plan": [{"sources": ["public/script.js"], "tests": ["tests/test_missing.py"]}]}

        assert D024Gate()._declared_test_plan_companion(tmp_path, wo_data, "public/script.js") is None

    def test_unmapped_deliverable_falls_back_to_heuristics(self, tmp_path: Path) -> None:
        (tmp_path / "public").mkdir(parents=True, exist_ok=True)
        (tmp_path / "public" / "other.js").write_text("x", encoding="utf-8")
        wo_data = {"test_plan": [{"sources": ["public/style.css"], "tests": ["tests/test_style.py"]}]}

        # 'public/other.js' is not in the declared mapping → no declared companion;
        # the legacy heuristic path remains responsible.
        assert D024Gate()._declared_test_plan_companion(tmp_path, wo_data, "public/other.js") is None


# ─── Repair prompt accuracy ───────────────────────────────────────────

class TestRepairPromptAccuracy:
    def _supervisor_and_state(self, tmp_path: Path) -> tuple[LifecycleSupervisor, Any]:
        mock_mgr = MockSessionManager(tmp_path)
        supervisor = LifecycleSupervisor(mock_mgr)
        state = supervisor.start_run("run-repair", "Build app", tmp_path, "sess-repair")
        return supervisor, state

    def test_prompt_lists_affected_artifacts_and_required_actions(self, tmp_path: Path) -> None:
        supervisor, state = self._supervisor_and_state(tmp_path)
        state.readiness_issues = [
            {
                "code": "QA_VERDICT_CHANNEL_MISSING",
                "category": "scope",
                "message": "Contract '.sync/contracts/WO-004.yaml' does not authorize the QA verdict channel",
                "work_order_id": "WO-004",
                "artifact_path": ".sync/contracts/WO-004.yaml",
                "required_action": "Add the canonical QA verdict permission.",
                "affected_artifacts": [".sync/contracts/WO-004.yaml"],
            },
            {
                "code": "MILESTONE_UNCOVERED",
                "category": "coverage",
                "message": "Expected implementation milestone 'Frontend Scaffolding' has no authored work order",
                "work_order_id": None,
                "artifact_path": None,
                "required_action": "Set milestone_id on the work order.",
            },
        ]

        prompt = supervisor._authoring_repair_prompt(state)

        assert "required_action: Add the canonical QA verdict permission." in prompt
        assert "required_action: Set milestone_id on the work order." in prompt
        assert ".sync/contracts/WO-004.yaml" in prompt
        # The old contradiction — "affected: (none)" alongside demanded fixes — is gone.
        assert "(none)" not in prompt
        assert "Correct ONLY the affected governed artifacts" not in prompt

    def test_prompt_reports_set_wide_failures_honestly(self, tmp_path: Path) -> None:
        supervisor, state = self._supervisor_and_state(tmp_path)
        state.readiness_issues = [
            {
                "code": "MILESTONE_UNCOVERED",
                "category": "coverage",
                "message": "Expected implementation milestone 'X' has no authored work order",
                "work_order_id": None,
                "artifact_path": None,
                "required_action": "Set milestone_id on the work order.",
            },
        ]

        prompt = supervisor._authoring_repair_prompt(state)

        assert "No individual artifact is named" in prompt
        assert "(none)" not in prompt


class TestSynonymAndDecompositionMilestoneCoverage:
    def test_milestone_synonym_and_prefix_matching(self) -> None:
        # Action verb stripping + calculation/logic
        assert milestone_matches("Tip Calculation Engine", "Implement Tip Calculation Logic")
        # QA <-> Quality Assurance, Testing <-> Validation
        assert milestone_matches("Quality Assurance & Validation", "End-to-End Testing & QA")
        # Release <-> Packaging & Versioning
        assert milestone_matches("Final Release & GitOps", "Release Packaging & Versioning")
        # Distinct milestones must still not collide
        assert not milestone_matches("Tip Calculation Engine", "End-to-End Testing & QA")
        assert not milestone_matches("Frontend UI Development", "Final Release & GitOps")

    def test_multiple_work_orders_with_explicit_milestone_id_is_valid_decomposition(
        self, tmp_path: Path
    ) -> None:
        """When an architect breaks a milestone into multiple work orders and explicitly
        binds them via milestone_id, authoring readiness gate accepts the decomposition."""
        (tmp_path / "PLAN.md").write_text(
            "# Project Plan: Engine\n\n## Current Architecture\nCore engine.\n\n## Milestones & Roadmap\n"
            "- [ ] Milestone 1: Core Engine (Agent: codex)\n",
            encoding="utf-8",
        )
        write_wo(tmp_path, {
            "id": "WO-001", "type": "FEATURE", "title": "Engine Core",
            "status": "PENDING", "priority": "P1", "assigned_agents": ["codex"],
            "dependencies": [], "milestone_id": "Milestone 1",
            "deliverable": {"type": "code", "path": "src/core.py"},
            "implementation_estimate": {"expected_files": ["src/core.py"]},
        })
        write_contract(tmp_path, {
            "work_order": "WO-001", "agent_id": "codex",
            "budget": {"max_files_touched": 5},
            "scope": {"allow": [{"module": "src/**"}]},
        })
        write_wo(tmp_path, {
            "id": "WO-002", "type": "FEATURE", "title": "Engine API",
            "status": "PENDING", "priority": "P1", "assigned_agents": ["codex"],
            "dependencies": ["WO-001"], "milestone_id": "Milestone 1",
            "deliverable": {"type": "code", "path": "src/api.py"},
            "implementation_estimate": {"expected_files": ["src/api.py"]},
        })
        write_contract(tmp_path, {
            "work_order": "WO-002", "agent_id": "codex",
            "budget": {"max_files_touched": 5},
            "scope": {"allow": [{"module": "src/**"}]},
        })

        refs = load_plan_milestones(tmp_path)
        result = validate_authoring_readiness(tmp_path, expected_milestones=refs)
        assert "MILESTONE_AMBIGUOUS" not in result.issue_codes()
        assert "MILESTONE_UNCOVERED" not in result.issue_codes()

    def test_compiler_dependency_decomposition_fallback(self, tmp_path: Path) -> None:
        """When 1 work order maps 1:1 to milestone and a secondary work order depends on it,
        the compiler injects the parent's milestone_id as dependency decomposition."""
        (tmp_path / "PLAN.md").write_text(
            "# Project Plan: Tip Calculator\n\n## Current Architecture\nCalculator backend.\n\n## Milestones & Roadmap\n"
            "- [ ] Milestone 1: Tip Calculation Engine (Agent: codex)\n",
            encoding="utf-8",
        )
        write_wo(tmp_path, {
            "id": "WO-001", "type": "FEATURE", "title": "Implement Tip Calculation Logic",
            "status": "PENDING", "priority": "P1", "assigned_agents": ["codex"],
            "dependencies": [],
            "deliverable": {"type": "code", "path": "src/calc.py"},
            "implementation_estimate": {"expected_files": ["src/calc.py"]},
        })
        write_contract(tmp_path, {
            "work_order": "WO-001", "agent_id": "codex",
            "budget": {"max_files_touched": 5},
            "scope": {"allow": [{"module": "src/**"}]},
        })
        write_wo(tmp_path, {
            "id": "WO-002", "type": "FEATURE", "title": "Implement Backend API",
            "status": "PENDING", "priority": "P1", "assigned_agents": ["codex"],
            "dependencies": ["WO-001"],
            "deliverable": {"type": "code", "path": "src/app.py"},
            "implementation_estimate": {"expected_files": ["src/app.py"]},
        })
        write_contract(tmp_path, {
            "work_order": "WO-002", "agent_id": "codex",
            "budget": {"max_files_touched": 5},
            "scope": {"allow": [{"module": "src/**"}]},
        })

        refs = load_plan_milestones(tmp_path)
        compile_res = compile_authoring_artifacts(tmp_path, refs)
        assert not compile_res.decision_required
        wo002 = yaml.safe_load((tmp_path / ".sync/work-orders/ACTIVE/WO-002.yaml").read_text(encoding="utf-8"))
        assert wo002.get("milestone_id") == "Milestone 1"

    def test_compiler_disambiguates_milestones_sharing_common_words(self, tmp_path: Path) -> None:
        """Regression test for shared-token milestones under the same agent.
        
        Milestone 2 ('Animated Background Implementation') and Milestone 3
        ('Animated Welcome Message') share the word 'Animated' and are both
        assigned to 'gemini'. The compiler must map WO-002 and WO-003 to
        their respective milestones without flagging MILESTONE_MAPPING_AMBIGUOUS.
        """
        plan_content = (
            "# Project Plan: Animated Welcome\n\n"
            "## Current Architecture\n"
            "Standalone HTML/CSS/JS, zero dependencies.\n\n"
            "## Milestones & Roadmap\n"
            "- [ ] Milestone 1: Scaffolding (Agent: gemini)\n"
            "- [ ] Milestone 2: Animated Background Implementation (Agent: gemini)\n"
            "- [ ] Milestone 3: Animated Welcome Message (Agent: gemini)\n"
        )
        (tmp_path / "PLAN.md").write_text(plan_content, encoding="utf-8")
        write_wo(tmp_path, {
            "id": "WO-001", "type": "FEATURE", "title": "Scaffolding",
            "status": "ACTIVE", "priority": "P0", "assigned_agents": ["gemini"],
            "dependencies": [], "deliverable": {"type": "code", "path": "index.html"},
        })
        write_wo(tmp_path, {
            "id": "WO-002", "type": "FEATURE", "title": "Animated Background Implementation",
            "status": "ACTIVE", "priority": "P1", "assigned_agents": ["gemini"],
            "dependencies": ["WO-001"], "deliverable": {"type": "code", "path": "style.css"},
        })
        write_wo(tmp_path, {
            "id": "WO-003", "type": "FEATURE", "title": "Animated Welcome Message",
            "status": "ACTIVE", "priority": "P1", "assigned_agents": ["gemini"],
            "dependencies": ["WO-002"], "deliverable": {"type": "code", "path": "script.js"},
        })
        for wid in ("WO-001", "WO-002", "WO-003"):
            write_contract(tmp_path, {
                "work_order": wid, "agent_id": "gemini",
                "budget": {"max_files_touched": 5},
                "scope": {"allow": [{"module": "**"}]},
            })

        refs = load_plan_milestones(tmp_path)
        compile_res = compile_authoring_artifacts(tmp_path, refs)
        assert not compile_res.decision_required, [f"{i.code}: {i.message}" for i in compile_res.decision_required]

        wo2 = yaml.safe_load((tmp_path / ".sync/work-orders/ACTIVE/WO-002.yaml").read_text(encoding="utf-8"))
        wo3 = yaml.safe_load((tmp_path / ".sync/work-orders/ACTIVE/WO-003.yaml").read_text(encoding="utf-8"))
        assert wo2.get("milestone_id") == "Milestone 2"
        assert wo3.get("milestone_id") == "Milestone 3"


class TestMultiGoalMilestoneIsolation:
    """Regression tests for multi-goal / continuation runs.

    In a continuation run (e.g. Goal 2 after Goal 1), earlier work orders
    (WO-001, WO-002, etc.) reside in .sync/work-orders/COMPLETED/ with
    historical milestone_ids ('Milestone 1', 'Milestone 2').
    The authoring readiness gate must scope milestone coverage strictly
    to the active plan's work orders (those created after the planning WO,
    or explicitly tracked in state.completed_wo_ids) so that historical
    work orders do not trigger spurious MILESTONE_AMBIGUOUS or MILESTONE_COVERAGE errors.
    """

    def test_historical_completed_wos_do_not_collide_with_new_goal_milestones(self, tmp_path: Path) -> None:
        # 1. Simulate Goal 1 completed work orders in COMPLETED/
        completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
        completed_dir.mkdir(parents=True, exist_ok=True)

        wo1 = make_wo("WO-001", "gemini", "public/index.html", "code", title="Project Scaffolding", milestone_id="Milestone 1")
        wo2 = make_wo("WO-002", "gemini", "public/style.css", "code", title="Wave Animation", milestone_id="Milestone 2")
        (completed_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo1), encoding="utf-8")
        (completed_dir / "WO-002.yaml").write_text(yaml.safe_dump(wo2), encoding="utf-8")

        # 2. Simulate Goal 2: new PLAN.md with its own Milestone 1 and Milestone 2
        goal2_plan = (
            "# Project Plan: Codebase Audit & Gap Analysis\n\n"
            "## Milestones & Roadmap\n"
            "- [ ] Milestone 1: Codebase Audit (Agent: gemini)\n"
            "  - [ ] Task 1.1: Audit files\n"
            "- [ ] Milestone 2: Gap Analysis & Correction (Agent: gemini)\n"
            "  - [ ] Task 2.1: Fix issues\n"
            "- [ ] Milestone 3: Final Verification (Agent: gemma)\n"
            "  - [ ] Task 3.1: Run verification suite\n"
        )
        (tmp_path / "PLAN.md").write_text(goal2_plan, encoding="utf-8")

        # Active work orders created under Goal 2 (planning work order was WO-007)
        wo8 = make_wo("WO-008", "gemini", "audit.py", "code", title="Codebase Audit", milestone_id="Milestone 1")
        wo9 = make_wo("WO-009", "gemini", "fix.py", "code", title="Gap Analysis & Correction", milestone_id="Milestone 2", dependencies=["WO-008"])
        wo10 = make_wo("WO-010", "gemma", "tests/test_verify.py", "code", title="Final Verification", milestone_id="Milestone 3", dependencies=["WO-009"])
        wo10["test_plan"] = [{
            "sources": ["audit.py", "fix.py"],
            "tests": ["tests/test_verify.py"],
        }]
        write_wo(tmp_path, wo8)
        write_wo(tmp_path, wo9)
        write_wo(tmp_path, wo10)

        # Contracts for Goal 2
        write_contract(tmp_path, make_contract("WO-008", "gemini", [{"module": "audit.py"}, {"module": "tests/**"}]))
        write_contract(tmp_path, make_contract("WO-009", "gemini", [{"module": "fix.py"}, {"module": "tests/**"}]))
        write_contract(tmp_path, make_contract("WO-010", "gemma", [{"module": "**"}, {"module": ".sync/inbox/claude/**"}]))

        # 3. Validate authoring readiness with planning_wo_id="WO-007"
        # and completed_wo_ids=[] (Goal 2 has no completed work orders yet)
        res = validate_authoring_readiness(
            tmp_path,
            plan_id="PLAN-002",
            planning_wo_id="WO-007",
            completed_wo_ids=[],
        )
        assert res.ready, f"Expected ready=True, got issues: {[f'{i.code}: {i.message}' for i in res.issues]}"

    def test_milestone_isolation_via_index_fallback_when_completed_ids_omitted(self, tmp_path: Path) -> None:
        # Same setup, but without passing completed_wo_ids explicitly;
        # the gate should infer index > parse_wo_index(planning_wo_id)
        completed_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
        completed_dir.mkdir(parents=True, exist_ok=True)

        wo1 = make_wo("WO-001", "gemini", "a.py", "code", title="Historical 1", milestone_id="Milestone 1")
        (completed_dir / "WO-001.yaml").write_text(yaml.safe_dump(wo1), encoding="utf-8")

        goal_plan = (
            "# Project Plan\n\n"
            "## Milestones & Roadmap\n"
            "- [ ] Milestone 1: Fresh Work (Agent: gemma)\n"
            "  - [ ] Task 1.1: Do work\n"
        )
        (tmp_path / "PLAN.md").write_text(goal_plan, encoding="utf-8")

        wo8 = make_wo("WO-008", "gemma", "tests/test_b.py", "code", title="Fresh Work", milestone_id="Milestone 1")
        write_wo(tmp_path, wo8)
        write_contract(tmp_path, make_contract("WO-008", "gemma", [{"module": "**"}, {"module": ".sync/inbox/claude/**"}]))

        res = validate_authoring_readiness(
            tmp_path,
            plan_id="PLAN-002",
            planning_wo_id="WO-007",
            # completed_wo_ids omitted -> relies on WO index > 7
        )
        assert res.ready, f"Expected ready=True, got issues: {[f'{i.code}: {i.message}' for i in res.issues]}"



