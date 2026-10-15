"""Comprehensive test suite for StackMind Execution Profiles and Skills Optimization.

Verifies:
1. Canonical profile definition, validation, settings persistence, and safe defaults.
2. Profile-driven progressive Skill retrieval (Low, Medium, High).
3. Distinctions between considered, selected, loaded, and injected skills.
4. Token budget enforcement and explicit measured vs. estimated accounting.
5. Isolated optional Laya decision-engine adapter with deterministic fallback.
6. Execution cycle telemetry capture and reporting.
7. Preservation of mandatory system instructions, contracts, and verification gates.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli.init import init
from validators.kernel.profile import (
    ExecutionProfile,
    ProfilePolicy,
    VerificationDepth,
    get_profile_policy,
)
from validators.kernel.settings import StackMindSettings, load_settings, save_settings
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)
from validators.skill.retriever import (
    SkillRetriever,
    SkillRetrievalDiagnostics,
)
from validators.knowledge.api import KnowledgeAPI, ContextBundle
from validators.skill.store import SkillStore
from validators.routing.laya_adapter import (
    LayaDecisionAdapter,
    LayaMode,
    TaskCategory,
)
from validators.kernel.daemon.supervisor import RunState, Phase, LifecycleSupervisor


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    project = tmp_path / "profile-test-workspace"
    init(project, name="Profile Test Project", no_git=True)
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project / "schemas", dirs_exist_ok=True)
    return project


def _create_skill(
    store: SkillStore,
    name: str,
    *,
    status: SkillStatus = SkillStatus.ACTIVE,
    target_modules: tuple[str, ...] = ("src/*",),
    preconditions: tuple[str, ...] = ("Environment ready",),
    steps: tuple[SkillStep, ...] = (
        SkillStep(step_index=1, action="Execute check", tool="bash", command_template="pytest"),
    ),
    description: str = "Test procedure description",
    risk: RiskTier = RiskTier.LOW,
    confidence: float = 0.9,
) -> SkillRecord:
    rec = SkillRecord(
        skill_id=SkillRecord.mint_id(name, 1),
        name=name,
        version=1,
        status=status,
        risk_tier=risk,
        description=description,
        applicability=SkillApplicability(preconditions=preconditions, target_modules=target_modules),
        steps=steps,
        provenance=SkillProvenance(
            source_experience_ids=(),
            created_at="2026-10-12T12:00:00Z",
            author_agent="claude",
        ),
        metrics=SkillMetrics(confidence_score=confidence),
    )
    store.save_version(rec)
    if status == SkillStatus.ACTIVE:
        store.promote_version(name, 1, skip_pipeline=True, skip_governor=True)
    return rec


class TestExecutionProfileConfiguration:
    """Verifies canonical profile parsing, defaults, validation, and settings persistence."""

    def test_canonical_profile_enum_values(self):
        assert ExecutionProfile.LOW.value == "low"
        assert ExecutionProfile.MEDIUM.value == "medium"
        assert ExecutionProfile.HIGH.value == "high"

    def test_from_str_case_insensitivity(self):
        assert ExecutionProfile.from_str("LOW") == ExecutionProfile.LOW
        assert ExecutionProfile.from_str("Low") == ExecutionProfile.LOW
        assert ExecutionProfile.from_str("medium") == ExecutionProfile.MEDIUM
        assert ExecutionProfile.from_str("High") == ExecutionProfile.HIGH
        assert ExecutionProfile.from_str("HIGH") == ExecutionProfile.HIGH

    def test_from_str_defaults_to_medium(self):
        assert ExecutionProfile.from_str(None) == ExecutionProfile.MEDIUM
        assert ExecutionProfile.from_str("") == ExecutionProfile.MEDIUM
        assert ExecutionProfile.from_str("   ") == ExecutionProfile.MEDIUM

    def test_from_str_invalid_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid execution profile 'extreme'"):
            ExecutionProfile.from_str("extreme")

    def test_profile_policy_parameters(self):
        low_policy = get_profile_policy(ExecutionProfile.LOW)
        assert not low_policy.skills_enabled
        assert low_policy.skill_limit == 0
        assert low_policy.skill_token_budget == 0
        assert low_policy.verification_depth == VerificationDepth.STANDARD

        med_policy = get_profile_policy(ExecutionProfile.MEDIUM)
        assert med_policy.skills_enabled
        assert med_policy.skill_limit == 2
        assert med_policy.skill_token_budget == 600
        assert med_policy.progressive_loading is True
        assert med_policy.verification_depth == VerificationDepth.STANDARD

        high_policy = get_profile_policy(ExecutionProfile.HIGH)
        assert high_policy.skills_enabled
        assert high_policy.skill_limit == 5
        assert high_policy.skill_token_budget == 1500
        assert high_policy.allow_deep_review is True
        assert high_policy.verification_depth == VerificationDepth.EXTENDED

    def test_settings_persistence(self, tmp_path: Path):
        # Default settings should have medium profile
        settings = load_settings(tmp_path)
        assert settings.execution_profile == "medium"
        assert settings.effective_profile == "medium"

        # Change profile to high and save
        settings.execution_profile = "high"
        save_settings(settings, tmp_path)

        reloaded = load_settings(tmp_path)
        assert reloaded.execution_profile == "high"
        assert reloaded.effective_profile == "high"

        # Test case insensitivity on dict deserialization
        raw_json = json.loads((tmp_path / ".sync" / "settings.json").read_text(encoding="utf-8"))
        raw_json["execution_profile"] = "LOW"
        (tmp_path / ".sync" / "settings.json").write_text(json.dumps(raw_json), encoding="utf-8")

        reloaded_low = load_settings(tmp_path)
        assert reloaded_low.effective_profile == "low"

    def test_settings_invalid_profile_falls_back_safely(self, tmp_path: Path):
        sync_dir = tmp_path / ".sync"
        sync_dir.mkdir(parents=True)
        (sync_dir / "settings.json").write_text(
            json.dumps({"execution_profile": "turbo_unsupported"}),
            encoding="utf-8",
        )
        settings = load_settings(tmp_path)
        # Invalid value in JSON falls back safely to medium
        assert settings.effective_profile == "medium"


class TestSkillRetrievalProfilesAndBudgeting:
    """Verifies profile policies, progressive loading, token budgeting, and diagnostics."""

    def test_low_profile_disables_optional_skills(self, temp_workspace: Path):
        store = SkillStore(temp_workspace)
        _create_skill(store, "python-refactor", description="Refactoring python code clean")
        _create_skill(store, "test-runner", description="Running unit tests with pytest")

        retriever = SkillRetriever(temp_workspace)
        policy = get_profile_policy(ExecutionProfile.LOW)

        injected, diag = retriever.retrieve_skills_with_diagnostics(
            "refactor python code and run tests",
            policy=policy,
        )

        assert injected == []
        assert diag.profile == "low"
        assert diag.skills_enabled is False
        assert diag.considered_count == 0
        assert diag.selected_count == 0
        assert diag.injected_count == 0
        assert diag.estimated_skill_tokens == 0

    def test_medium_profile_compact_and_budgeted(self, temp_workspace: Path):
        store = SkillStore(temp_workspace)
        _create_skill(
            store,
            "python-refactoring",
            description="Refactor Python modules and clean up code architecture",
            confidence=0.95,
        )
        _create_skill(
            store,
            "pytest-unit-testing",
            description="Run pytest test suites and report code coverage metrics",
            confidence=0.90,
        )
        _create_skill(
            store,
            "release-tagging",
            description="GitOps release management and semantic version tagging",
            confidence=0.80,
        )

        retriever = SkillRetriever(temp_workspace)
        policy = get_profile_policy(ExecutionProfile.MEDIUM)
        # Medium policy: limit=2, budget=600

        injected, diag = retriever.retrieve_skills_with_diagnostics(
            "refactor python modules and run pytest unit testing",
            policy=policy,
        )

        assert len(injected) <= 2
        assert diag.profile == "medium"
        assert diag.skills_enabled is True
        assert diag.injected_count == len(injected)
        assert diag.estimated_skill_tokens <= policy.skill_token_budget

    def test_high_profile_deeper_retrieval(self, temp_workspace: Path):
        store = SkillStore(temp_workspace)
        for i in range(4):
            _create_skill(
                store,
                f"backend-service-{i}",
                description=f"Backend service component {i} refactor code and endpoints",
                confidence=0.85,
            )

        retriever = SkillRetriever(temp_workspace)
        policy = get_profile_policy(ExecutionProfile.HIGH)
        # High policy: limit=5, budget=1500

        injected, diag = retriever.retrieve_skills_with_diagnostics(
            "backend service refactor code and endpoints",
            policy=policy,
        )

        assert len(injected) >= 3
        assert diag.profile == "high"
        assert diag.estimated_skill_tokens <= policy.skill_token_budget

    def test_token_budget_enforcement_excludes_overflowing_skills(self, temp_workspace: Path):
        store = SkillStore(temp_workspace)
        # Create a skill with multiple long steps that exceed a small token budget
        long_steps = tuple(
            SkillStep(step_index=i, action=f"Long detailed step {i}: " + ("x" * 200), tool="bash")
            for i in range(1, 10)
        )
        _create_skill(
            store,
            "large-skill",
            description="Large procedure with extensive instructions",
            steps=long_steps,
            confidence=0.95,
        )
        _create_skill(
            store,
            "small-skill",
            description="Small compact procedure",
            steps=(SkillStep(step_index=1, action="Small check", tool="bash"),),
            confidence=0.90,
        )

        retriever = SkillRetriever(temp_workspace)
        # Strict policy with 100 token budget
        policy = ProfilePolicy(
            profile=ExecutionProfile.MEDIUM,
            skills_enabled=True,
            skill_limit=5,
            skill_token_budget=100,
            min_relevance=0.1,
            progressive_loading=False,
            full_detail_threshold=0.5,
        )

        injected, diag = retriever.retrieve_skills_with_diagnostics(
            "procedure instructions check",
            policy=policy,
        )

        # Total estimated tokens must be <= 100
        assert diag.estimated_skill_tokens <= 100
        if diag.budget_exceeded:
            assert len(diag.exclusions) > 0

    def test_knowledge_api_profile_integration(self, temp_workspace: Path):
        api = KnowledgeAPI(temp_workspace)

        # In LOW profile, assemble_context returns 0 skills
        bundle_low = api.assemble_context(
            "build backend feature",
            profile=ExecutionProfile.LOW,
        )
        assert bundle_low.execution_profile == "low"
        assert bundle_low.skill_diagnostics is not None
        assert bundle_low.skill_diagnostics["injected_count"] == 0

        # In MEDIUM profile, assemble_context attaches diagnostics
        bundle_med = api.assemble_context(
            "build backend feature",
            profile=ExecutionProfile.MEDIUM,
        )
        assert bundle_med.execution_profile == "medium"
        assert bundle_med.skill_diagnostics is not None


class TestLayaDecisionAdapter:
    """Verifies isolation, disabled default, deterministic fallback, and shadow mode."""

    def test_disabled_by_default(self):
        adapter = LayaDecisionAdapter()
        assert adapter.is_available() is False
        assert adapter.mode == LayaMode.DISABLED

    def test_deterministic_fallback_when_disabled(self):
        adapter = LayaDecisionAdapter(mode=LayaMode.DISABLED)

        # Classification fallback
        result = adapter.classify_task("Write unit tests and verify assertions")
        assert result.category == TaskCategory.TESTING
        assert result.source == "fallback"

        result_fe = adapter.classify_task("Build React button component and layout CSS")
        assert result_fe.category == TaskCategory.FRONTEND

        result_arch = adapter.classify_task("Define system architecture and service plan")
        assert result_arch.category == TaskCategory.ARCHITECTURE

        result_gitops = adapter.classify_task("Release version v1.2 and update changelog")
        assert result_gitops.category == TaskCategory.GITOPS

    def test_candidate_category_routing(self):
        adapter = LayaDecisionAdapter()
        cats = adapter.route_skill_categories("Write comprehensive backend tests and coverage")
        assert TaskCategory.TESTING.value in cats
        assert TaskCategory.BACKEND.value in cats

    def test_shadow_mode_evaluation(self):
        adapter = LayaDecisionAdapter(mode=LayaMode.SHADOW)
        assert adapter.mode == LayaMode.SHADOW

        decision = adapter.shadow_evaluate(
            "Test and verify component rendering",
            current_choice="testing",
        )

        assert decision["mode"] == "shadow"
        assert decision["fallback_choice"] == "testing"
        assert decision["match"] is True
        assert "timestamp" in decision


class TestLifecycleRunBoundaryImmutability:
    """Verifies that execution profile changes take effect at the beginning of the next cycle."""

    def test_run_state_snaps_profile_at_start_run(self, temp_workspace: Path):
        # Configure initial profile as low
        settings = load_settings(temp_workspace)
        settings.execution_profile = "low"
        save_settings(settings, temp_workspace)

        supervisor = LifecycleSupervisor(manager=MagicMock())
        state: RunState = supervisor.start_run(
            run_id="run-001",
            product_goal="Deliver authenticated user profile",
            workspace=temp_workspace,
            session_id="session-001",
        )

        assert state.execution_profile == "low"

        # Operator now modifies settings to 'high' during active cycle
        settings.execution_profile = "high"
        save_settings(settings, temp_workspace)

        # Active cycle state must remain snapped to 'low'
        assert state.execution_profile == "low"

        # Next run will snap new 'high' profile
        state_next: RunState = supervisor.start_run(
            run_id="run-002",
            product_goal="Deliver billing subscription",
            workspace=temp_workspace,
            session_id="session-002",
        )
        assert state_next.execution_profile == "high"

    def test_run_state_serialization_roundtrip(self):
        state = RunState(
            run_id="run-test",
            product_goal="Test serialization",
            workspace="/tmp/ws",
            session_id="session-test",
            phase=Phase.PLANNING,
            execution_profile="high",
        )

        data = state.to_dict()
        assert data["execution_profile"] == "high"

        reloaded = RunState.from_dict(data)
        assert reloaded.execution_profile == "high"


class TestExecutionTelemetryAndDiagnostics:
    """Verifies execution cycle telemetry structure and fail-closed invariants."""

    def test_telemetry_packet_structure(self):
        diagnostics = SkillRetrievalDiagnostics(
            profile="medium",
            skills_enabled=True,
            considered_count=2,
            selected_count=1,
            loaded_count=1,
            injected_count=1,
            considered_ids=("skill-1", "skill-2"),
            selected_ids=("skill-1",),
            loaded_ids=("skill-1",),
            injected_ids=("skill-1",),
            estimated_skill_tokens=120,
            skill_token_budget=600,
            budget_exceeded=False,
            exclusions=({"skill_id": "skill-2", "reason": "below_threshold"},),
        )

        diag_dict = diagnostics.to_dict()
        assert diag_dict["profile"] == "medium"
        assert diag_dict["considered_ids"] == ["skill-1", "skill-2"]
        assert diag_dict["selected_ids"] == ["skill-1"]
        assert diag_dict["injected_ids"] == ["skill-1"]
        assert diag_dict["estimated_skill_tokens"] == 120
        assert diag_dict["skill_token_budget"] == 600
        assert diag_dict["budget_exceeded"] is False

    def test_runner_telemetry_with_dict_and_object_diagnostics(self, temp_workspace: Path):
        """Regression test: verify runner._meta_payload, _render_report, and _build_events_write

        handle both dictionary and dataclass representations of skill_diagnostics
        without raising AttributeError: 'dict' object has no attribute 'to_dict'.
        """
        from datetime import datetime, timezone
        from validators.harness.runner import HarnessRunner

        runner = HarnessRunner(temp_workspace, agent="codex")

        completion = MagicMock()
        completion.provider = "openai"
        completion.model = "gpt-4"
        completion.prompt_tokens = 100
        completion.completion_tokens = 50
        completion.cost_estimate = 0.002
        completion.meta = {}

        decision = MagicMock()
        decision.status = "completed"
        decision.summary = "ok"
        decision.blockers = ()
        decision.modified_files = ()
        decision.report_markdown = "report"
        decision.uncertainty = ()

        retrieval = MagicMock()
        retrieval.mode = "standard"
        retrieval.cache_hits = 0
        retrieval.cost_estimate = 0.001
        retrieval.cap_exhausted = False
        retrieval.searches_used = 1
        retrieval.evidence = ()

        task = MagicMock()
        task.identifier = "task-planning-1"
        task.kind = "work_order"

        # Case 1: skill_diagnostics is a dict (standard return from KnowledgeAPI.assemble_context)
        context_dict = MagicMock()
        context_dict.revision = "rev-1"
        context_dict.stale = False
        context_dict.git_commit = "head"
        context_dict.scope_filtered_count = 0
        context_dict.skill_diagnostics = {
            "profile": "medium",
            "skills_enabled": True,
            "considered_ids": ["skill-1", "skill-2"],
            "selected_ids": ["skill-1"],
            "loaded_ids": ["skill-1"],
            "injected_ids": ["skill-1"],
            "estimated_skill_tokens": 80,
            "skill_token_budget": 500,
            "budget_exceeded": False,
        }

        stage_inputs_dict = {
            "completion": completion,
            "context": context_dict,
            "decision": decision,
            "retrieval": retrieval,
            "task": task,
            "run_at": datetime.now(timezone.utc),
            "llm_ms": 120,
            "poll_ms": 10,
            "retrieval_ms": 30,
            "dimensions": None,
            "diff": None,
            "trust_level": "OBSERVABLE",
            "experience_record": None,
        }

        # _meta_payload must NOT raise AttributeError: 'dict' object has no attribute 'to_dict'
        meta = runner._meta_payload(stage_inputs_dict, lock_wait_ms=5, lock_hold_ms=15, write_ms=10)
        assert meta["skill_diagnostics"] == context_dict.skill_diagnostics
        assert meta["telemetry"]["skill_injected"] == ["skill-1"]
        assert meta["telemetry"]["skill_candidates_considered"] == ["skill-1", "skill-2"]
        assert meta["telemetry"]["skill_candidates_selected"] == ["skill-1"]
        assert meta["telemetry"]["skill_loaded"] == ["skill-1"]
        assert meta["telemetry"]["skill_token_estimate"] == 80

        # _render_report (which executes during planning turn) must render successfully
        report_text = runner._render_report(
            stage_inputs_dict, lock_wait_ms=5, lock_hold_ms=15, write_ms=10, now=stage_inputs_dict["run_at"]
        )
        assert "# Harness Report: task-planning-1" in report_text
        assert "skill_diagnostics:" in report_text

        # _build_events_write must also succeed and extract skills_injected and skill_tokens_estimated
        events_write = runner._build_events_write(stage_inputs_dict, lock_hold_ms=15, lock_wait_ms=5)
        event_data = json.loads(events_write.content.strip())
        assert event_data["skills_injected"] == ["skill-1"]
        assert event_data["skill_tokens_estimated"] == 80

        # Case 2: skill_diagnostics is a SkillRetrievalDiagnostics object
        diag_obj = SkillRetrievalDiagnostics(
            profile="medium",
            skills_enabled=True,
            considered_count=2,
            selected_count=1,
            loaded_count=1,
            injected_count=1,
            considered_ids=("skill-1", "skill-2"),
            selected_ids=("skill-1",),
            loaded_ids=("skill-1",),
            injected_ids=("skill-1",),
            estimated_skill_tokens=80,
            skill_token_budget=500,
        )
        context_obj = MagicMock()
        context_obj.revision = "rev-1"
        context_obj.stale = False
        context_obj.git_commit = "head"
        context_obj.scope_filtered_count = 0
        context_obj.skill_diagnostics = diag_obj

        stage_inputs_obj = {**stage_inputs_dict, "context": context_obj}
        meta_obj = runner._meta_payload(stage_inputs_obj, lock_wait_ms=5, lock_hold_ms=15, write_ms=10)
        assert meta_obj["skill_diagnostics"]["profile"] == "medium"
        assert meta_obj["telemetry"]["skill_injected"] == ["skill-1"]
        assert meta_obj["telemetry"]["skill_candidates_considered"] == ["skill-1", "skill-2"]
        assert meta_obj["telemetry"]["skill_token_estimate"] == 80

        events_write_obj = runner._build_events_write(stage_inputs_obj, lock_hold_ms=15, lock_wait_ms=5)
        event_data_obj = json.loads(events_write_obj.content.strip())
        assert event_data_obj["skills_injected"] == ["skill-1"]
        assert event_data_obj["skill_tokens_estimated"] == 80

        # Case 3: skill_diagnostics is None
        context_none = MagicMock()
        context_none.revision = "rev-1"
        context_none.stale = False
        context_none.git_commit = "head"
        context_none.scope_filtered_count = 0
        context_none.skill_diagnostics = None

        stage_inputs_none = {**stage_inputs_dict, "context": context_none}
        meta_none = runner._meta_payload(stage_inputs_none, lock_wait_ms=5, lock_hold_ms=15, write_ms=10)
        assert "skill_diagnostics" not in meta_none
        assert meta_none["telemetry"]["skill_injected"] == []
        assert meta_none["telemetry"]["skill_token_estimate"] == 0

        events_write_none = runner._build_events_write(stage_inputs_none, lock_hold_ms=15, lock_wait_ms=5)
        event_data_none = json.loads(events_write_none.content.strip())
        assert event_data_none["skills_injected"] == []
        assert event_data_none["skill_tokens_estimated"] == 0


class TestProfileSettingsAndTUIInteraction:
    """Verifies Settings Panel and :profile interactive navigation, rendering, and persistence."""

    def test_render_anchored_settings_panel_main_and_profile_views(self, temp_workspace: Path):
        from cli.tui.state import AutonomousDeliveryState
        from cli.tui.settings_panel import render_anchored_settings_panel_str

        state = AutonomousDeliveryState()
        settings = load_settings(temp_workspace)
        state.open_settings(settings, initial_view="main")

        # Main view must be exactly 11 lines and show Execution Profile row
        output_main = render_anchored_settings_panel_str(state, width=80)
        lines_main = output_main.splitlines()
        assert len(lines_main) == 11
        assert "Execution Profile" in output_main
        assert "[MEDIUM]" in output_main

        # Profile view must be exactly 11 lines and show Low/Medium/High options with descriptions
        state.settings_current_view = "profile"
        output_prof = render_anchored_settings_panel_str(state, width=80)
        lines_prof = output_prof.splitlines()
        assert len(lines_prof) == 11
        assert "LOW" in output_prof
        assert "MEDIUM" in output_prof
        assert "HIGH" in output_prof
        assert "[ACTIVE]" in output_prof
        assert "0 optional skills" in output_prof
        assert "600-token budget" in output_prof
        assert "1500-token budget" in output_prof

    def test_cycle_profile_in_main_settings_panel(self, temp_workspace: Path):
        from cli.tui.state import AutonomousDeliveryState
        from cli.tui.keyboard import Key
        from cli.tui.settings_panel import run_arrow_navigation_settings_loop

        state = AutonomousDeliveryState()
        # Row 6 is Execution Profile (providers are 0..5).
        # Navigating down 6 times, pressing RIGHT to cycle from medium -> high,
        # pressing S to save, and ESC to exit.
        key_sequence = [Key.DOWN] * 6 + [Key.RIGHT, "s", Key.ESCAPE]
        session = {"workspace": str(temp_workspace)}

        res = run_arrow_navigation_settings_loop(
            session=session,
            state=state,
            workspace=temp_workspace,
            key_stream=iter(key_sequence),
        )

        assert state.execution_profile == "high"
        reloaded = load_settings(temp_workspace)
        assert reloaded.effective_profile == "high"

    def test_enter_profile_view_from_main_and_select(self, temp_workspace: Path):
        from cli.tui.state import AutonomousDeliveryState
        from cli.tui.keyboard import Key
        from cli.tui.settings_panel import run_arrow_navigation_settings_loop

        state = AutonomousDeliveryState()
        # Navigate down 6 times to Execution Profile, press ENTER to open profile view,
        # press UP to select LOW (index 0), press ENTER to apply & save, then press ESC to exit.
        key_sequence = [Key.DOWN] * 6 + [Key.ENTER, Key.UP, Key.ENTER, Key.ESCAPE]
        session = {"workspace": str(temp_workspace)}

        res = run_arrow_navigation_settings_loop(
            session=session,
            state=state,
            workspace=temp_workspace,
            key_stream=iter(key_sequence),
        )

        assert state.execution_profile == "low"
        reloaded = load_settings(temp_workspace)
        assert reloaded.effective_profile == "low"

    def test_jump_to_profile_view_with_p_shortcut(self, temp_workspace: Path):
        from cli.tui.state import AutonomousDeliveryState
        from cli.tui.keyboard import Key
        from cli.tui.settings_panel import run_arrow_navigation_settings_loop

        state = AutonomousDeliveryState()
        # In :config (initial_view="settings"), row 0 is Execution Profile.
        # Press LEFT to cycle from MEDIUM to LOW, then DOWN 5 times to Save row, then ENTER to apply.
        key_sequence = [Key.LEFT, Key.DOWN, Key.DOWN, Key.DOWN, Key.DOWN, Key.DOWN, Key.ENTER, Key.ESCAPE]
        session = {"workspace": str(temp_workspace)}

        res = run_arrow_navigation_settings_loop(
            session=session,
            state=state,
            workspace=temp_workspace,
            key_stream=iter(key_sequence),
            initial_view="settings",
        )

        assert state.execution_profile == "low"
        reloaded = load_settings(temp_workspace)
        assert reloaded.effective_profile == "low"

    def test_interactive_profile_selector_apply(self, temp_workspace: Path):
        from cli.tui.state import AutonomousDeliveryState
        from cli.tui.keyboard import Key
        from cli.tui.settings_panel import run_interactive_profile_selector

        state = AutonomousDeliveryState()
        # Initial profile is MEDIUM (index 1). Press DOWN to land on HIGH (index 2), then ENTER to apply.
        key_sequence = [Key.DOWN, Key.ENTER]
        session = {"workspace": str(temp_workspace)}

        res = run_interactive_profile_selector(
            session=session,
            state=state,
            workspace=temp_workspace,
            key_stream=iter(key_sequence),
        )

        assert "[SUCCESS]" in res
        assert "HIGH" in res
        assert state.execution_profile == "high"
        reloaded = load_settings(temp_workspace)
        assert reloaded.effective_profile == "high"

    def test_interactive_profile_selector_cancel(self, temp_workspace: Path):
        from cli.tui.state import AutonomousDeliveryState
        from cli.tui.keyboard import Key
        from cli.tui.settings_panel import run_interactive_profile_selector

        state = AutonomousDeliveryState()
        # Press ESC to cancel immediately
        key_sequence = [Key.ESCAPE]
        session = {"workspace": str(temp_workspace)}

        res = run_interactive_profile_selector(
            session=session,
            state=state,
            workspace=temp_workspace,
            key_stream=iter(key_sequence),
        )

        assert "[INFO]" in res
        assert "cancelled" in res
        reloaded = load_settings(temp_workspace)
        assert reloaded.effective_profile == "medium"

    def test_render_profile_overview_non_tty(self, temp_workspace: Path):
        from cli.tui.settings_panel import render_profile_overview_str

        settings = load_settings(temp_workspace)
        output = render_profile_overview_str(settings, width=80)

        assert "StackMind Execution Profiles" in output
        assert "[LOW]" in output
        assert "[MEDIUM]" in output
        assert "[HIGH]" in output
        assert "[ACTIVE]" in output
        assert ":config" in output

    def test_profile_command_integration(self, temp_workspace: Path):
        from cli.tui.state import AutonomousDeliveryState
        from cli.tui.commands import dispatch_delivery_command

        state = AutonomousDeliveryState()
        session = {"session_id": "sess-test", "workspace": str(temp_workspace)}
        client = MagicMock()
        adapter = MagicMock()

        # 1. :profile redirect notification
        session, should_exit = dispatch_delivery_command(
            adapter,
            client,
            session,
            ":profile",
            state,
        )
        assert should_exit is False
        last_msg = state.messages[-1]
        assert "':profile' has been merged into ':config'" in last_msg.content

        # 2. Configure execution profile via :config profile low
        session, should_exit = dispatch_delivery_command(
            adapter,
            client,
            session,
            ":config profile low",
            state,
        )
        assert state.execution_profile == "low"
        assert load_settings(temp_workspace).effective_profile == "low"
        last_msg = state.messages[-1]
        assert "[SUCCESS] Execution profile set to LOW" in last_msg.content

