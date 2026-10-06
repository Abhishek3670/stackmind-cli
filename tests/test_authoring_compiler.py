"""Unit tests for the deterministic contract compiler / normalizer.

The compiler sits between architect authoring and the readiness gate: it
injects system-owned invariants (QA verdict channel, milestone identity),
validates declared test plans, and classifies non-normalizable intent as
ARCHITECT-DECISION-REQUIRED.  It must be deterministic and never auto-fill
semantic decisions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from validators.harness.authoring_compiler import compile_authoring_artifacts
from validators.harness.authoring_readiness import (
    load_plan_milestones,
    validate_authoring_readiness,
)


# ─── Helpers ──────────────────────────────────────────────────────────

PLAN_MD = (
    "# Project Plan: Clean TUI Test\n\n"
    "## Current Architecture\n"
    "Static frontend, no backend.\n\n"
    "## Milestones & Roadmap\n"
    "- [ ] Milestone 1: Frontend Scaffolding (Agent: gemini)\n"
    "  - [ ] Task 1.1: Create public/index.html\n"
    "- [ ] Milestone 2: Wave Background Implementation (Agent: gemini)\n"
    "  - [ ] Task 2.1: Create public/style.css\n"
)


def write_plan(ws: Path, content: str = PLAN_MD) -> None:
    ws.joinpath("PLAN.md").write_text(content, encoding="utf-8")


def write_wo(ws: Path, record: dict[str, Any]) -> str:
    rel = f".sync/work-orders/ACTIVE/{record['id']}.yaml"
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return rel


def write_contract(ws: Path, record: dict[str, Any]) -> str:
    rel = f".sync/contracts/{record['work_order']}.yaml"
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return rel


def make_wo(wo_id: str, agent: str, deliv_path: str, deliv_type: str = "code",
            title: str | None = None, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": wo_id,
        "type": "FEATURE",
        "title": title or f"Work order {wo_id}",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": [agent],
        "dependencies": [],
        "deliverable": {"type": deliv_type, "path": deliv_path, "description": f"{wo_id} output"},
        "description": f"Implement {wo_id}",
    }
    record.update(extra)
    return record


def make_contract(wo_id: str, agent: str, allow: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "agent_id": agent,
        "work_order": wo_id,
        "identity": {"role": "qa" if agent == "gemma" else "backend", "reports_to": "claude"},
        "scope": {
            "allow": list(allow or [{"module": "public/**"}]),
            "deny": [{"module": ".git/**"}],
            "write": "read-write",
        },
        "budget": {"max_files_touched": 5, "max_tokens": 20000},
    }


# ─── Fixtures for the clean_tui_test v2 scenario ──────────────────────

PLAN_MD_V2 = (
    "# Project Plan: Static Welcome Page\n\n"
    "## Current Architecture\n"
    "Static site, no backend.\n\n"
    "## Milestones & Roadmap\n"
    "- [ ] Milestone 1: Project Scaffolding (Agent: gemini)\n"
    "- [ ] Milestone 2: Animated Background Implementation (Agent: gemini)\n"
    "- [ ] Milestone 3: Animated Welcome Message (Agent: gemini)\n"
    "- [ ] Milestone 4: Quality Assurance & Polish (Agent: gemma)\n"
    "- [ ] Milestone 5: Final Release (Agent: local-llm)\n"
)


def write_plan_v2(ws: Path) -> None:
    ws.joinpath("PLAN.md").write_text(PLAN_MD_V2, encoding="utf-8")


# ─── QA verdict channel injection ─────────────────────────────────────

class TestQaVerdictChannelInjection:
    def test_gemma_contract_without_channel_gets_it(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", "gemma", "tests/test_auth.py"))
        contract_rel = write_contract(tmp_path, make_contract("WO-001", "gemma", [{"module": "tests/**"}]))

        result = compile_authoring_artifacts(tmp_path)

        assert not result.decision_required
        assert contract_rel in result.normalized_paths
        assert f"+scope.allow: .sync/inbox/claude/**" in result.injections[contract_rel]
        saved = yaml.safe_load((tmp_path / contract_rel).read_text(encoding="utf-8"))
        assert {"module": ".sync/inbox/claude/**"} in saved["scope"]["allow"]
        assert saved["normalized_by"].startswith("authoring-compiler/")

    def test_injection_is_idempotent(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", "gemma", "tests/test_auth.py"))
        write_contract(tmp_path, make_contract("WO-001", "gemma", [{"module": "tests/**"}]))

        first = compile_authoring_artifacts(tmp_path)
        second = compile_authoring_artifacts(tmp_path)

        assert first.normalized_paths
        assert second.normalized_paths == ()  # nothing left to normalize
        assert second.decision_required == ()

    def test_non_qa_contracts_are_untouched(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", "codex", "src/app.py"))
        contract_rel = write_contract(tmp_path, make_contract("WO-001", "codex", [{"module": "src/**"}]))

        result = compile_authoring_artifacts(tmp_path)

        assert result.normalized_paths == ()
        saved = yaml.safe_load((tmp_path / contract_rel).read_text(encoding="utf-8"))
        assert not any(".sync/inbox" in str(rule) for rule in saved["scope"]["allow"])

    def test_explicit_channel_is_not_duplicated(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", "gemma", "tests/test_auth.py"))
        contract_rel = write_contract(tmp_path, make_contract(
            "WO-001", "gemma", [{"module": "tests/**"}, {"module": ".sync/inbox/claude/**"}],
        ))

        result = compile_authoring_artifacts(tmp_path)

        assert contract_rel not in result.normalized_paths
        saved = yaml.safe_load((tmp_path / contract_rel).read_text(encoding="utf-8"))
        assert sum(1 for r in saved["scope"]["allow"] if r.get("module") == ".sync/inbox/claude/**") == 1


# ─── Milestone identity injection ─────────────────────────────────────

class TestMilestoneIdentityInjection:
    def test_wo_title_matched_to_plan_milestone_gets_id(self, tmp_path: Path) -> None:
        # Regression for the clean_tui_test failure: plan milestone carries the
        # "(Agent: gemini)" annotation, the authored work order does not — the
        # compiler links them by semantic title and injects the stable id.
        write_plan(tmp_path)
        wo_rel = write_wo(tmp_path, make_wo("WO-001", "gemini", "public/index.html", "config",
                                            title="Frontend Scaffolding"))

        result = compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path))

        assert not result.decision_required
        assert wo_rel in result.normalized_paths
        saved = yaml.safe_load((tmp_path / wo_rel).read_text(encoding="utf-8"))
        assert saved["milestone_id"] == "Milestone 1"

    def test_ambiguous_title_routes_to_repair_not_injection(self, tmp_path: Path) -> None:
        write_plan(tmp_path, PLAN_MD.replace(
            "Milestone 2: Wave Background Implementation",
            "Milestone 2: Frontend Scaffolding Extensions",
        ))
        wo_rel = write_wo(tmp_path, make_wo("WO-001", "gemini", "public/index.html", "config",
                                            title="Frontend Scaffolding"))

        result = compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path))

        codes = [i.code for i in result.decision_required]
        assert "MILESTONE_MAPPING_AMBIGUOUS" in codes
        assert wo_rel not in result.normalized_paths  # unsafe to guess
        issue = next(i for i in result.decision_required if i.code == "MILESTONE_MAPPING_AMBIGUOUS")
        assert issue.required_action

    def test_unknown_declared_milestone_id_routes_to_repair(self, tmp_path: Path) -> None:
        write_plan(tmp_path)
        wo_rel = write_wo(tmp_path, make_wo("WO-001", "gemini", "public/index.html", "config",
                                            title="Frontend Scaffolding", milestone_id="Milestone 9"))

        result = compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path))

        codes = [i.code for i in result.decision_required]
        assert "MILESTONE_ID_UNKNOWN" in codes
        issue = next(i for i in result.decision_required if i.code == "MILESTONE_ID_UNKNOWN")
        assert "Milestone 9" in issue.message

    def test_ordinal_fallback_maps_renamed_work_orders(self, tmp_path: Path) -> None:
        # Regression for the second clean_tui_test failure: the architect
        # legitimately renamed work orders ("Implement Visual Styling" for
        # milestone "Animated Background Implementation"), so title matching
        # resolves only the first one.  When the authored set is 1:1 with the
        # plan, the remaining identities are assigned in order.
        write_plan_v2(tmp_path)
        wo_rels = [
            write_wo(tmp_path, make_wo("WO-001", "gemini", "public/index.html", "config",
                                       title="Project Scaffolding")),
            write_wo(tmp_path, make_wo("WO-002", "gemini", "styles.css",
                                       title="Implement Visual Styling")),
            write_wo(tmp_path, make_wo("WO-003", "gemini", "script.js",
                                       title="Implement Animations")),
            write_wo(tmp_path, make_wo("WO-004", "gemma", "tests/test_visuals.py",
                                       title="QA and Visual Verification")),
            write_wo(tmp_path, make_wo("WO-005", "local-llm", "CHANGELOG.md", "doc",
                                       title="Finalize Assets and Versioning")),
        ]

        result = compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path))

        assert not result.decision_required
        saved_ids = {}
        for rel in wo_rels:
            assert rel in result.normalized_paths
            saved = yaml.safe_load((tmp_path / rel).read_text(encoding="utf-8"))
            saved_ids[saved["id"]] = saved["milestone_id"]
        assert saved_ids == {
            "WO-001": "Milestone 1",
            "WO-002": "Milestone 2",
            "WO-003": "Milestone 3",
            "WO-004": "Milestone 4",
            "WO-005": "Milestone 5",
        }
        assert any("(ordinal)" in item for items in result.injections.values() for item in items)

    def test_ordinal_fallback_defers_when_set_is_not_one_to_one(self, tmp_path: Path) -> None:
        write_plan(tmp_path)  # 2 plan milestones
        write_wo(tmp_path, make_wo("WO-001", "gemini", "public/index.html", "config",
                                   title="Totally Different Thing"))
        write_wo(tmp_path, make_wo("WO-002", "gemini", "styles.css",
                                   title="Another Unrelated Task"))
        write_wo(tmp_path, make_wo("WO-003", "gemma", "tests/test_x.py",
                                   title="Also Unrelated"))

        result = compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path))

        # 3 WOs vs 2 milestones: guessing is unsafe — defer to the readiness
        # gate's structured diagnostics instead of injecting.
        assert not result.decision_required
        for rel in (".sync/work-orders/ACTIVE/WO-001.yaml",
                    ".sync/work-orders/ACTIVE/WO-002.yaml",
                    ".sync/work-orders/ACTIVE/WO-003.yaml"):
            saved = yaml.safe_load((tmp_path / rel).read_text(encoding="utf-8"))
            assert "milestone_id" not in saved

    def test_no_plan_skips_milestone_injection(self, tmp_path: Path) -> None:
        wo_rel = write_wo(tmp_path, make_wo("WO-001", "codex", "src/app.py"))

        result = compile_authoring_artifacts(tmp_path, None)

        assert not result.decision_required
        assert wo_rel not in result.normalized_paths
        saved = yaml.safe_load((tmp_path / wo_rel).read_text(encoding="utf-8"))
        assert "milestone_id" not in saved


# ─── Consolidated test_plan default ───────────────────────────────────

class TestConsolidatedTestPlanDefault:
    """Regression for the third clean_tui_test failure: the architect planned
    exactly one QA suite but never declared the source→test mapping, and two
    repair rounds could not add it.  The compiler now defaults the mapping."""

    def _build(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", "gemini", "css/style.css"))
        write_wo(tmp_path, make_wo("WO-002", "gemma", "tests/test_visuals.py"))
        write_contract(tmp_path, make_contract("WO-001", "gemini", [{"module": "css/**"}]))
        write_contract(tmp_path, make_contract("WO-002", "gemma", [{"module": "tests/**"}]))

    def test_single_qa_suite_becomes_declared_coverage(self, tmp_path: Path) -> None:
        self._build(tmp_path)
        wo_rel = ".sync/work-orders/ACTIVE/WO-001.yaml"

        result = compile_authoring_artifacts(tmp_path)

        assert not result.decision_required
        assert wo_rel in result.normalized_paths
        saved = yaml.safe_load((tmp_path / wo_rel).read_text(encoding="utf-8"))
        assert saved["test_plan"] == [
            {"sources": ["css/style.css"], "tests": ["tests/test_visuals.py"]}
        ]
        assert saved["normalized_by"].startswith("authoring-compiler/")

        # The readiness gate accepts the defaulted mapping.
        gate_result = validate_authoring_readiness(tmp_path)
        assert gate_result.ready, [i.message for i in gate_result.issues]

    def test_default_not_injected_with_multiple_suites(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", "gemini", "css/style.css"))
        write_wo(tmp_path, make_wo("WO-002", "gemma", "tests/test_visuals.py"))
        write_wo(tmp_path, make_wo("WO-003", "gemma", "tests/test_structure.py"))
        write_contract(tmp_path, make_contract("WO-001", "gemini", [{"module": "css/**"}]))
        write_contract(tmp_path, make_contract("WO-002", "gemma", [{"module": "tests/**"}]))
        write_contract(tmp_path, make_contract("WO-003", "gemma", [{"module": "tests/**"}]))

        result = compile_authoring_artifacts(tmp_path)

        # Ambiguous which suite covers what — defer to the gate/repair.
        saved = yaml.safe_load(
            (tmp_path / ".sync/work-orders/ACTIVE/WO-001.yaml").read_text(encoding="utf-8")
        )
        assert "test_plan" not in saved

    def test_default_respects_explicit_declaration(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo(
            "WO-001", "gemini", "css/style.css",
            test_plan=[{"sources": ["css/style.css"], "tests": ["tests/test_custom.py"]}],
        ))
        write_wo(tmp_path, make_wo("WO-002", "gemma", "tests/test_visuals.py"))
        write_contract(tmp_path, make_contract("WO-001", "gemini", [{"module": "css/**"}]))
        write_contract(tmp_path, make_contract("WO-002", "gemma", [{"module": "tests/**"}]))

        result = compile_authoring_artifacts(tmp_path)

        wo_rel = ".sync/work-orders/ACTIVE/WO-001.yaml"
        assert wo_rel not in result.normalized_paths
        saved = yaml.safe_load((tmp_path / wo_rel).read_text(encoding="utf-8"))
        assert saved["test_plan"][0]["tests"] == ["tests/test_custom.py"]

    def test_default_skips_exempt_and_stem_matched_stems(self, tmp_path: Path) -> None:
        write_wo(tmp_path, make_wo("WO-001", "gemini", "index.html", "code"))  # exempt stem
        write_wo(tmp_path, make_wo("WO-002", "gemini", "src/style.py"))  # stem-matched suite
        write_wo(tmp_path, make_wo("WO-003", "gemma", "tests/test_style.py"))
        for wo_id, agent, allow in (
            ("WO-001", "gemini", [{"module": "**"}]),
            ("WO-002", "gemini", [{"module": "**"}]),
            ("WO-003", "gemma", [{"module": "tests/**"}]),
        ):
            write_contract(tmp_path, make_contract(wo_id, agent, allow))

        result = compile_authoring_artifacts(tmp_path)

        assert not result.decision_required
        for rel in (".sync/work-orders/ACTIVE/WO-001.yaml",
                    ".sync/work-orders/ACTIVE/WO-002.yaml"):
            saved = yaml.safe_load((tmp_path / rel).read_text(encoding="utf-8"))
            assert "test_plan" not in saved


# ─── Declared test plan validation ────────────────────────────────────

class TestDeclaredTestPlanValidation:
    def test_valid_consolidated_plan_passes(self, tmp_path: Path) -> None:
        wo_rel = write_wo(tmp_path, make_wo(
            "WO-001", "gemini", "public/style.css",
            test_plan=[{"sources": ["public/style.css", "public/script.js"],
                        "tests": ["tests/test_visuals.py"]}],
        ))

        result = compile_authoring_artifacts(tmp_path)

        assert not result.decision_required
        assert wo_rel not in result.normalized_paths  # valid intent is preserved

    def test_non_test_paths_are_rejected(self, tmp_path: Path) -> None:
        wo_rel = write_wo(tmp_path, make_wo(
            "WO-001", "gemini", "public/style.css",
            test_plan=[{"sources": ["public/style.css"], "tests": ["public/style.css"]}],
        ))

        result = compile_authoring_artifacts(tmp_path)

        codes = [i.code for i in result.decision_required]
        assert "TEST_PLAN_INVALID" in codes
        issue = next(i for i in result.decision_required if i.code == "TEST_PLAN_INVALID")
        assert issue.required_action
        assert issue.affected_artifacts == (wo_rel,)

    def test_malformed_structure_is_rejected(self, tmp_path: Path) -> None:
        wo_rel = write_wo(tmp_path, make_wo(
            "WO-001", "gemini", "public/style.css", test_plan=[{"sources": ["public/style.css"]}],
        ))

        result = compile_authoring_artifacts(tmp_path)

        assert "TEST_PLAN_INVALID" in [i.code for i in result.decision_required]

    def test_compiler_is_deterministic(self, tmp_path: Path) -> None:
        write_plan(tmp_path)
        write_wo(tmp_path, make_wo("WO-001", "gemma", "tests/test_auth.py"))
        write_contract(tmp_path, make_contract("WO-001", "gemma", [{"module": "tests/**"}]))

        first = compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path))
        snapshot = (tmp_path / ".sync" / "contracts" / "WO-001.yaml").read_text(encoding="utf-8")
        second = compile_authoring_artifacts(tmp_path, load_plan_milestones(tmp_path))

        assert first.normalized_paths
        assert second.normalized_paths == ()
        assert (tmp_path / ".sync" / "contracts" / "WO-001.yaml").read_text(encoding="utf-8") == snapshot
