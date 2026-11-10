"""Unit tests for the compiled worker task contract and the in-turn ownership guard.

The task contract is the deterministic worker prompt: compiled from the work
order, contract, and peer work orders; rendered as JSON; and mirrored by the
TaskOwnership guard that enforces the same rules mid-turn.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from validators.harness.task_contract import (
    TaskOwnership,
    compile_worker_task_contract,
    reconcile_modified_files,
    render_task_block,
    render_worker_system,
)


def _write_plan(ws: Path) -> None:
    ws.joinpath("PLAN.md").write_text(
        "# Project Plan: Tip Calculator\n\n"
        "## Current Architecture\nPython module + static page.\n\n"
        "## Milestones & Roadmap\n"
        "- [ ] Milestone 1: Tip Calculation Logic (Agent: codex)\n"
        "- [ ] Milestone 2: Responsive Frontend UI (Agent: gemini)\n",
        encoding="utf-8",
    )


def _write_wo(ws: Path, wo_id: str, agent: str, deliv_path: str, **extra) -> None:
    record = {
        "id": wo_id,
        "type": "FEATURE",
        "title": f"Work order {wo_id}",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": [agent],
        "dependencies": [],
        "deliverable": {"type": "code", "path": deliv_path, "description": f"{wo_id} output"},
        "description": f"Implement {wo_id}",
    }
    record.update(extra)
    path = ws / ".sync" / "work-orders" / "ACTIVE" / f"{wo_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")


def _write_contract(ws: Path, wo_id: str, agent: str, allow: list[str], max_files: int = 3) -> None:
    record = {
        "schema_version": 1,
        "agent_id": agent,
        "work_order": wo_id,
        "identity": {"role": "worker", "reports_to": "claude"},
        "scope": {"allow": [{"module": a} for a in allow], "deny": [{"module": ".git/**"}], "write": "read-write"},
        "budget": {"max_files_touched": max_files, "max_tokens": 20000},
    }
    path = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")


class _FakeTask:
    def __init__(self, wo_id: str, deliv: str) -> None:
        self.work_order_id = wo_id
        self.deliverable_path = deliv
        self.title = f"Work order {wo_id}"
        self.body = "Implement it"
        self.identifier = wo_id


class TestCompiledTaskContract:
    def test_classification_separates_deliverable_from_peers(self, tmp_path: Path) -> None:
        _write_plan(tmp_path)
        _write_wo(tmp_path, "WO-001", "gemini", "index.html",
                  acceptance_criteria=["contains <html> tag", "heading reads Study Dashboard"])
        _write_wo(tmp_path, "WO-002", "codex", "stats.py")
        _write_contract(tmp_path, "WO-001", "gemini",
                        ["index.html", "style.css", "stats.py"], max_files=4)

        contract = compile_worker_task_contract(
            _FakeTask("WO-001", "index.html"),
            yaml.safe_load((tmp_path / ".sync/work-orders/ACTIVE/WO-001.yaml").read_text(encoding="utf-8")),
            yaml.safe_load((tmp_path / ".sync/contracts/WO-001.yaml").read_text(encoding="utf-8")),
            tmp_path,
        )

        # Own deliverable
        assert contract["your_deliverable"]["path"] == "index.html"
        assert contract["your_deliverable"]["must_exist_after_your_turn"] is True
        # Peer deliverables (other active WOs) are read-only, not "in scope extras"
        assert "stats.py" in contract["read_only_files"]
        # In-scope files that are neither the deliverable nor peers stay extras
        assert "style.css" in contract["allowed_extra_paths"]
        # Budget with the actionable rule
        assert contract["file_budget"]["max_files_touched"] == 4
        assert "At most 4" in contract["file_budget"]["rule"]
        # Acceptance criteria authored upstream are displayed
        assert contract["acceptance_criteria"] == [
            "contains <html> tag", "heading reads Study Dashboard",
        ]
        # Failure behavior
        assert "status 'blocked'" in contract["on_failure"]
        assert contract["project_environment"] == "development"

    def test_environment_configuration_propagated(self, tmp_path: Path) -> None:
        _write_plan(tmp_path)
        _write_wo(tmp_path, "WO-001", "codex", "app.py")
        _write_contract(tmp_path, "WO-001", "codex", ["app.py"])

        # 1. Default (development)
        c1 = compile_worker_task_contract(
            _FakeTask("WO-001", "app.py"), None, None, tmp_path,
        )
        assert c1["project_environment"] == "development"
        assert "Project environment is 'development'." in render_worker_system(c1)

        # 2. Configured production
        cfg = tmp_path / ".sync" / "config.yaml"
        cfg.write_text("environment: production\n", encoding="utf-8")
        c2 = compile_worker_task_contract(
            _FakeTask("WO-001", "app.py"), None, None, tmp_path,
        )
        assert c2["project_environment"] == "production"
        assert "Project environment is 'production'." in render_worker_system(c2)

    def test_rendered_decision_contract_is_valid_json_with_schema_fields(self) -> None:
        _write_plan(tmp_path := Path(tmp_name := __import__("tempfile").mkdtemp()))
        _write_wo(tmp_path, "WO-001", "codex", "stats.py")
        _write_contract(tmp_path, "WO-001", "codex", ["stats.py"])
        contract = compile_worker_task_contract(
            _FakeTask("WO-001", "stats.py"), None, None, tmp_path,
        )
        block = render_task_block(contract)
        # The JSON block parses
        json_block = block.split("TASK CONTRACT (authoritative — obey exactly):\n", 1)[1]
        parsed = json.loads(json_block)
        assert parsed["your_deliverable"]["path"] == "stats.py"
        # The system render carries the decision output contract with enums
        system = render_worker_system(contract)
        assert '"status": "completed" | "blocked" | "deferred"' in system
        assert "ONLY files you actually wrote with write_file" in system
        assert "do not claim tests passed" in system

    def test_prompt_size_stays_bounded(self, tmp_path: Path) -> None:
        _write_plan(tmp_path)
        _write_wo(tmp_path, "WO-001", "gemini", "index.html")
        _write_contract(tmp_path, "WO-001", "gemini", ["index.html", "style.css"])
        contract = compile_worker_task_contract(
            _FakeTask("WO-001", "index.html"), None,
            yaml.safe_load((tmp_path / ".sync/contracts/WO-001.yaml").read_text(encoding="utf-8")),
            tmp_path,
        )
        rendered = render_worker_system(contract) + render_task_block(contract)
        # Guard against "robust prompt" bloat: the compiled contract must stay
        # materially smaller than the prose it replaced.
        assert len(rendered) < 4500


def tmp_name() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


class TestTaskOwnershipGuard:
    def test_read_only_peer_write_denied_with_actionable_message(self) -> None:
        ownership = TaskOwnership.from_contract({
            "your_deliverable": {"path": "index.html"},
            "read_only_files": ["stats.py"],
            "read_only_prefixes": [],
            "file_budget": {"max_files_touched": 3},
        })
        denial = ownership.check_write("stats.py")
        assert denial and "owned by another work order" in denial
        assert "index.html" in denial  # points the model at its deliverable

    def test_deliverable_and_budgeted_writes_allowed(self) -> None:
        ownership = TaskOwnership.from_contract({
            "your_deliverable": {"path": "index.html"},
            "read_only_files": ["stats.py"],
            "read_only_prefixes": [],
            "file_budget": {"max_files_touched": 3},
        })
        assert ownership.check_write("index.html") is None
        assert ownership.check_write("style.css") is None  # in budget
        assert ownership.check_write("script.js") is None

    def test_budget_exceeded_denies_new_files(self) -> None:
        ownership = TaskOwnership.from_contract({
            "your_deliverable": {"path": "index.html"},
            "read_only_files": [],
            "read_only_prefixes": [],
            "file_budget": {"max_files_touched": 2},
        })
        assert ownership.check_write("a.html") is None
        assert ownership.check_write("b.css") is None
        denial = ownership.check_write("c.js")
        assert denial and "budget exceeded" in denial
        # Rewriting an already-written file is never budget-denied
        assert ownership.check_write("a.html") is None

    def test_read_only_prefix_blocks_directory(self) -> None:
        ownership = TaskOwnership.from_contract({
            "your_deliverable": {"path": "index.html"},
            "read_only_files": [],
            "read_only_prefixes": ["src/peer/"],
            "file_budget": {"max_files_touched": 1},
        })
        assert ownership.check_write("src/peer/other.py") and "owned by another work order" in ownership.check_write("src/peer/other.py")


class TestReconcileModifiedFiles:
    def test_undeclared_writes_are_added(self) -> None:
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Decision:
            modified_files: tuple

        decision = _Decision(modified_files=("index.html",))
        observed = ("index.html", "style.css", "script.js")
        merged, auto_added = reconcile_modified_files(decision, observed)
        assert auto_added == ["script.css".replace("css", "js"), "style.css"] or sorted(auto_added) == ["script.js", "style.css"]
        assert set(merged.modified_files) == {"index.html", "style.css", "script.js"}

    def test_noop_when_declarations_match(self) -> None:
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Decision:
            modified_files: tuple

        decision = _Decision(modified_files=("index.html",))
        merged, auto_added = reconcile_modified_files(decision, ("index.html",))
        assert auto_added == []
        assert merged is decision

    def test_unwritten_denied_files_not_reconciled_or_added(self) -> None:
        """Files denied by TaskOwnership must not be added to observed or modified_files."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Decision:
            modified_files: tuple

        decision = _Decision(modified_files=("manifest.json", "css/style.css"))
        # Denied files like index.html and js/main.js are never in observed writes
        observed = ("manifest.json", "css/style.css")
        merged, auto_added = reconcile_modified_files(decision, observed)
        assert auto_added == []
        assert "index.html" not in merged.modified_files
        assert "js/main.js" not in merged.modified_files


class TestSingleDeliverableOwnershipAndPeerProtection:
    def test_declared_deliverable_never_hijacked_by_peer(self, tmp_path: Path) -> None:
        """When a work order explicitly declares a deliverable (e.g. assets/js/app.js),
        it must NEVER be replaced by an arbitrary fallback (e.g. index.html) even if
        another peer work order declared the same path."""
        _write_plan(tmp_path)
        # WO-016 declares assets/js/app.js
        _write_wo(tmp_path, "WO-016", "gemini", "assets/js/app.js")
        # WO-027 also declares assets/js/app.js
        _write_wo(tmp_path, "WO-027", "gemini", "assets/js/app.js")

        wo_data = yaml.safe_load((tmp_path / ".sync/work-orders/ACTIVE/WO-016.yaml").read_text(encoding="utf-8"))
        contract_data = {
            "identity": {"role": "frontend"},
            "scope": {"allow": [{"module": "assets/js/app.js"}]},
            "budget": {"max_files_touched": 3},
        }

        compiled = compile_worker_task_contract(
            _FakeTask("WO-016", "assets/js/app.js"),
            wo_data,
            contract_data,
            tmp_path,
        )

        # Must NOT be hijacked to index.html
        assert compiled["your_deliverable"]["path"] == "assets/js/app.js"
        # Must NOT put own deliverable into read_only_files
        assert "assets/js/app.js" not in compiled["read_only_files"]

    def test_task_ownership_always_permits_declared_deliverable(self) -> None:
        """TaskOwnership.check_write must always allow the worker's declared deliverable,
        even if read_only_prefixes or read_only_files would otherwise match."""
        ownership = TaskOwnership.from_contract({
            "your_deliverable": {"path": "assets/js/app.js"},
            "read_only_files": ["assets/js/app.js", "src/engine.js"],
            "read_only_prefixes": ["assets/"],
            "file_budget": {"max_files_touched": 3},
        })
        # Writing own deliverable is always permitted
        assert ownership.check_write("assets/js/app.js") is None
        # Peer files remain denied
        assert ownership.check_write("src/engine.js") is not None
        assert "owned by another work order" in ownership.check_write("src/engine.js")

    def test_collect_peer_deliverables_ignores_cancelled_and_completed_wos(self, tmp_path: Path) -> None:
        """collect_peer_deliverables must only collect deliverables from ACTIVE non-cancelled WOs."""
        _write_plan(tmp_path)
        # Active WO
        _write_wo(tmp_path, "WO-010", "codex", "src/engine.js")
        # Cancelled WO in ACTIVE
        _write_wo(tmp_path, "WO-011", "gemini", "old.js", status="CANCELLED")
        # Completed WO
        comp_dir = tmp_path / ".sync" / "work-orders" / "COMPLETED"
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / "WO-001.yaml").write_text(
            yaml.dump({
                "id": "WO-001",
                "status": "COMPLETED",
                "deliverable": {"path": "package.json"},
            }),
            encoding="utf-8",
        )

        from validators.harness.task_contract import collect_peer_deliverables
        peers = collect_peer_deliverables(tmp_path, "WO-016")
        peer_paths = [p for _, p in peers]

        assert "src/engine.js" in peer_paths
        assert "old.js" not in peer_paths
        assert "package.json" not in peer_paths
