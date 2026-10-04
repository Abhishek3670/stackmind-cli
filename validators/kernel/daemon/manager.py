"""Durable session lifecycle and cancellation coordination."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, Callable
from uuid import uuid4

import yaml

from .events import EventDispatcher, RuntimeEvent
from .storage import DaemonStorage

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_PLAN_STATES = {"DRAFT", "AWAITING_APPROVAL", "APPROVED", "REJECTED", "SUPERSEDED"}
_OPERATION_STATES = {
    "REQUESTED",
    "AUTHORIZED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "BLOCKED",
}
_OPERATION_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}

_LOGICAL_ROLES = ["architecture", "backend", "frontend", "qa", "gitops"]
_ROLE_ALIASES = {
    "claude": "architecture",
    "architecture": "architecture",
    "architect": "architecture",
    "codex": "backend",
    "backend": "backend",
    "gemini": "frontend",
    "frontend": "frontend",
    "gemma": "qa",
    "qa": "qa",
    "local-llm": "gitops",
    "gitops": "gitops",
}
_ROLE_TO_AGENTS = {
    "architecture": ["claude", "architecture", "architect"],
    "backend": ["codex", "backend"],
    "frontend": ["gemini", "frontend"],
    "qa": ["gemma", "qa"],
    "gitops": ["local-llm", "gitops"],
}
_ROLE_TO_PRIMARY_AGENT = {
    "architecture": "claude",
    "backend": "codex",
    "frontend": "gemini",
    "qa": "gemma",
    "gitops": "local-llm",
}
_DEFAULT_ROLE_CONFIG = {
    "architecture": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "backend": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "frontend": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "qa": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
    "gitops": {"backend": "echo-agent", "model": "stackmind-echo-v1", "status": "configured"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_narrower_or_equal(child_pat: str, parent_pat: str) -> bool:
    if child_pat == parent_pat:
        return True
    child_p = child_pat.replace("\\", "/").strip("/")
    parent_p = parent_pat.replace("\\", "/").strip("/")
    if parent_p in ("*", "**", ""):
        return True
    p_base = parent_p
    while p_base.endswith("/*"):
        p_base = p_base[:-2]
    if p_base.endswith("/**"):
        p_base = p_base[:-3]
    if p_base.endswith("*"):
        p_base = p_base[:-1]
    p_base = p_base.rstrip("/")
    if not p_base:
        return True
    return child_p == p_base or child_p.startswith(p_base + "/")


def _verify_contract_scope_narrowing(parent_scope: Any, child_scope: Any) -> None:
    """Verify child's contract_scope is a subset of parent's contract_scope."""
    if parent_scope is None:
        return
    if child_scope is None:
        raise ValueError("Child contract_scope cannot be None when parent has contract_scope")

    def _normalize(scope: Any) -> tuple[set[str] | None, set[str] | None]:
        if scope is None:
            return None, None
        if isinstance(scope, (list, set, tuple)):
            return set(str(x) for x in scope), None
        if isinstance(scope, dict):
            inner = scope.get("scope") if isinstance(scope.get("scope"), dict) else scope
            allow = (
                set(str(x) for x in inner["allow"])
                if "allow" in inner and isinstance(inner["allow"], (list, set, tuple))
                else None
            )
            deny = (
                set(str(x) for x in inner["deny"])
                if "deny" in inner and isinstance(inner["deny"], (list, set, tuple))
                else None
            )
            if allow is None and deny is None:
                return set(str(k) for k in inner.keys()), None
            return allow, deny
        return {str(scope)}, None

    parent_allow, parent_deny = _normalize(parent_scope)
    child_allow, child_deny = _normalize(child_scope)

    if parent_allow is not None:
        if child_allow is None:
            raise ValueError("child contract_scope cannot be wider than parent contract_scope")
        for c in child_allow:
            if not any(_is_narrower_or_equal(c, p) for p in parent_allow):
                raise ValueError(
                    f"child contract_scope allow pattern '{c}' is out of parent allow scope: {parent_allow}"
                )

    if parent_deny is not None:
        if child_deny is None:
            raise ValueError(
                f"Child contract_scope must include all parent deny patterns: {parent_deny}"
            )
        for p in parent_deny:
            if not any(p == c or _is_narrower_or_equal(p, c) for c in child_deny):
                raise ValueError(
                    f"Child contract_scope must include all parent deny patterns: {parent_deny}"
                )


def _scaffold_protocol_citizenship(workspace: Path, agent: str) -> None:
    """Auto-scaffold minimal valid protocol citizenship for workspace agents."""
    sync_dir = workspace / ".sync"
    tree_file = sync_dir / "runtime" / "TREE.yaml"
    tree_data: dict[str, Any] = {}
    if tree_file.exists():
        try:
            loaded = yaml.safe_load(tree_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                tree_data = loaded
        except Exception:
            tree_data = {}
    tree_data.setdefault("schema_version", 1)
    tree_data.setdefault("tree_version", 1)
    tree_data.setdefault("release", "3.1.0")
    agents = tree_data.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        tree_data["agents"] = agents
    if agent not in agents:
        agents[agent] = {
            "session_count": 0,
            "status": "IDLE",
            "assigned_work_orders": [],
        }
    tree_file.parent.mkdir(parents=True, exist_ok=True)
    tree_file.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding="utf-8")

    boot_file = sync_dir / "runtime" / "boot" / f"{agent}.boot.yaml"
    if not boot_file.exists():
        boot_file.parent.mkdir(parents=True, exist_ok=True)
        boot_payload = {
            "agent": agent,
            "role": "Backend Developer" if agent == "codex" else agent.capitalize(),
            "schema_version": 1,
            "release": "3.1.0",
            "session_count": 0,
            "status": "IDLE",
            "assigned_work_orders": [],
            "blockers": [],
        }
        boot_file.write_text(yaml.safe_dump(boot_payload, sort_keys=False), encoding="utf-8")

    agent_md = sync_dir / "agents" / f"{agent}.agent.md"
    if not agent_md.exists():
        agent_md.parent.mkdir(parents=True, exist_ok=True)
        agent_md.write_text(
            f"# Agent: {agent}\n\nRole: Protocol Citizen\nStatus: Active\n",
            encoding="utf-8",
        )

    if agent == "claude":
        claude_contract = sync_dir / "agents" / "claude.contract.yaml"
        if not claude_contract.exists():
            claude_contract.parent.mkdir(parents=True, exist_ok=True)
            claude_contract.write_text(yaml.safe_dump({
                "schema_version": 1,
                "agent_id": "claude",
                "work_order": "WO-000",
                "identity": {
                    "role": "architecture",
                    "reports_to": "ceo",
                },
                "scope": {
                    "allow": [
                        {"module": "PLAN.md"},
                        {"module": ".sync/work-orders/**"},
                        {"module": ".sync/contracts/**"},
                        {"module": ".sync/inbox/local-llm/**"},
                        {"module": ".sync/inbox/claude/**"},
                    ],
                    "deny": [
                        {"module": ".git/**"},
                        {"module": "src/**"},
                        {"module": "tests/**"},
                        {"module": ".sync/inbox/codex/**"},
                        {"module": ".sync/inbox/gemini/**"},
                        {"module": ".sync/inbox/gemma/**"},
                        {"module": ".sync/inbox/CEO/**"},
                        {"module": ".sync/runtime/**"},
                        {"module": ".sync/outbox/**"},
                        {"module": ".sync/agents/**"},
                        {"module": ".sync/knowledge/**"},
                        {"module": ".sync/snapshots/**"},
                        {"module": ".env"},
                        {"module": ".env.*"},
                        {"module": "__pycache__/**"},
                        {"module": ".venv/**"},
                        {"module": "node_modules/**"},
                    ],
                    "write": "read-write",
                },
                "budget": {
                    "max_files_touched": 20,
                    "max_tokens": 50000,
                },
            }, sort_keys=False), encoding="utf-8")

    (sync_dir / "inbox" / agent / "_read").mkdir(parents=True, exist_ok=True)
    (sync_dir / "outbox" / agent).mkdir(parents=True, exist_ok=True)


def resolve_bootstrap_work_order_id(workspace: Path | str, preferred_id: str = "WO-000") -> str:
    """Find a usable work order ID for bootstrap planning.

    Uses preferred_id (default 'WO-000') if available or currently ACTIVE.
    If preferred_id is COMPLETED or archived, finds the lowest unused WO-xxx ID.
    """
    ws = Path(workspace)
    active_path = ws / ".sync" / "work-orders" / "ACTIVE" / f"{preferred_id}.yaml"
    completed_path = ws / ".sync" / "work-orders" / "COMPLETED" / f"{preferred_id}.yaml"
    if not active_path.exists() and not completed_path.exists():
        return preferred_id
    if active_path.exists():
        return preferred_id

    for i in range(1, 1000):
        candidate = f"WO-{i:03d}"
        cand_active = ws / ".sync" / "work-orders" / "ACTIVE" / f"{candidate}.yaml"
        cand_comp = ws / ".sync" / "work-orders" / "COMPLETED" / f"{candidate}.yaml"
        if not cand_active.exists() and not cand_comp.exists():
            return candidate
    return preferred_id


def synthesize_bootstrap_planning(
    workspace: Path | str,
    prompt: str,
    wo_id: str | None = None,
    assigned_agent: str = "claude",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Synthesize trusted bootstrap planning work order and contract artifacts.

    Validates all artifacts fail-closed against AuthoringGate and schemas before writing.
    Updates .sync/work-orders/INDEX.yaml and .sync/runtime/TREE.yaml.
    """
    ws = Path(workspace).resolve()
    _scaffold_protocol_citizenship(ws, assigned_agent)
    now = _now()

    actual_wo_id = wo_id or resolve_bootstrap_work_order_id(ws, "WO-000")

    # 1. Construct Work Order record
    title_snippet = prompt.strip()[:80].replace("\n", " ").strip()
    wo_title = (
        f"System architecture and execution plan for: {title_snippet}"
        if title_snippet
        else "System architecture and execution plan"
    )
    wo_record = {
        "id": actual_wo_id,
        "type": "RESEARCH",
        "title": wo_title,
        "status": "ACTIVE",
        "priority": "P0",
        "assigned_agents": [assigned_agent],
        "dependencies": [],
        "deliverable": {
            "type": "doc",
            "path": "PLAN.md",
            "description": "System architecture and execution plan",
        },
        "description": prompt.strip(),
        "created": now,
        "updated": now,
    }

    # 2. Construct matching Contract record
    contract_record = {
        "schema_version": 1,
        "agent_id": assigned_agent,
        "work_order": actual_wo_id,
        "identity": {
            "role": "architecture",
            "reports_to": "ceo",
        },
        "scope": {
            "allow": [
                {"module": "PLAN.md"},
                {"module": ".sync/work-orders/**"},
                {"module": ".sync/contracts/**"},
                {"module": ".sync/inbox/local-llm/**"},
                {"module": ".sync/inbox/claude/**"},
            ],
            "deny": [
                {"module": ".git/**"},
                {"module": "src/**"},
                {"module": "tests/**"},
                {"module": ".sync/inbox/codex/**"},
                {"module": ".sync/inbox/gemini/**"},
                {"module": ".sync/inbox/gemma/**"},
                {"module": ".sync/inbox/CEO/**"},
                {"module": ".sync/runtime/**"},
                {"module": ".sync/outbox/**"},
                {"module": ".sync/agents/**"},
                {"module": ".sync/knowledge/**"},
                {"module": ".sync/snapshots/**"},
                {"module": ".env"},
                {"module": ".env.*"},
                {"module": "__pycache__/**"},
                {"module": ".venv/**"},
                {"module": "node_modules/**"},
            ],
            "write": "read-write",
        },
        "budget": {
            "max_files_touched": 20,
            "max_tokens": 50000,
        },
    }

    # 3. Fail-closed Authoring Gate validation
    from validators.harness.authoring_gate import AuthoringGate

    gate = AuthoringGate()
    wo_yaml = yaml.safe_dump(wo_record, sort_keys=False)
    contract_yaml = yaml.safe_dump(contract_record, sort_keys=False)

    rel_wo_path = f".sync/work-orders/ACTIVE/{actual_wo_id}.yaml"
    rel_contract_path = f".sync/contracts/{actual_wo_id}.yaml"

    wo_decision = gate.validate_artifact_content(rel_wo_path, wo_yaml, agent="ceo")
    if not wo_decision.passed:
        raise ValueError(
            f"Synthesized bootstrap work order failed authoring gate: {wo_decision.summary}"
        )

    contract_decision = gate.validate_artifact_content(
        rel_contract_path, contract_yaml, agent="ceo"
    )
    if not contract_decision.passed:
        raise ValueError(
            f"Synthesized bootstrap contract failed authoring gate: {contract_decision.summary}"
        )

    # 4. Write artifacts to disk
    wo_path = ws / ".sync" / "work-orders" / "ACTIVE" / f"{actual_wo_id}.yaml"
    wo_path.parent.mkdir(parents=True, exist_ok=True)
    wo_path.write_text(wo_yaml, encoding="utf-8")

    contract_path = ws / ".sync" / "contracts" / f"{actual_wo_id}.yaml"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(contract_yaml, encoding="utf-8")

    # 5. Update .sync/work-orders/INDEX.yaml
    index_path = ws / ".sync" / "work-orders" / "INDEX.yaml"
    index_data: dict[str, Any] = {"schema_version": 1, "next_id": 1, "orders": []}
    if index_path.exists():
        try:
            loaded_index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded_index, dict) and isinstance(loaded_index.get("orders"), list):
                index_data = loaded_index
                index_data.setdefault("next_id", 1)
        except Exception:
            pass

    existing_order_ids = {
        item.get("id") for item in index_data["orders"] if isinstance(item, dict)
    }
    if actual_wo_id not in existing_order_ids:
        index_data["orders"].append({
            "id": actual_wo_id,
            "type": wo_record["type"],
            "title": wo_record["title"],
            "status": wo_record["status"],
            "priority": wo_record["priority"],
            "assigned_agents": wo_record["assigned_agents"],
            "dependencies": wo_record["dependencies"],
            "deliverable": wo_record["deliverable"],
            "created": wo_record["created"],
            "updated": wo_record["updated"],
            "file": f"work-orders/ACTIVE/{actual_wo_id}.yaml",
        })
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

    # 6. Update .sync/runtime/TREE.yaml
    tree_path = ws / ".sync" / "runtime" / "TREE.yaml"
    tree_data: dict[str, Any] = {}
    if tree_path.exists():
        try:
            loaded_tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
            if isinstance(loaded_tree, dict):
                tree_data = loaded_tree
        except Exception:
            pass
    tree_data.setdefault("schema_version", 1)
    tree_data.setdefault("tree_version", 1)
    tree_data.setdefault("release", "3.1.0")
    agents = tree_data.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        tree_data["agents"] = agents
    agent_info = agents.setdefault(assigned_agent, {
        "session_count": 0,
        "status": "idle",
        "assigned_work_orders": [],
    })
    assigned_list = agent_info.setdefault("assigned_work_orders", [])
    if actual_wo_id not in assigned_list:
        assigned_list.append(actual_wo_id)
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding="utf-8")

    return wo_record, contract_record


def _reset_phase_operation_id(state: Any, phase: Any) -> None:
    """Clear stale operation references when resuming into a phase so a fresh turn is dispatched."""
    from .supervisor import Phase

    if phase == Phase.GITOPS:
        state.gitops_operation_id = None
    elif phase == Phase.INTEGRATION_REVIEW:
        state.integration_operation_id = None
    elif phase == Phase.PLANNING:
        state.planning_operation_id = None
    elif phase == Phase.AUTHORING:
        state.authoring_operation_id = None
    elif phase in (Phase.DISPATCHING, Phase.EXECUTING):
        state.batch_operation_id = None

    ignored = set(getattr(state, "ignored_operation_ids", []) or [])
    if getattr(state, "integration_operation_id", None) in ignored:
        state.integration_operation_id = None
    if getattr(state, "planning_operation_id", None) in ignored:
        state.planning_operation_id = None
    if getattr(state, "authoring_operation_id", None) in ignored:
        state.authoring_operation_id = None
    if getattr(state, "gitops_operation_id", None) in ignored:
        state.gitops_operation_id = None
    if getattr(state, "batch_operation_id", None) in ignored:
        state.batch_operation_id = None


class SessionManager:
    """Owns daemon sessions, their audit journals, and active-operation cancellation."""

    def __init__(
        self,
        storage: DaemonStorage,
        runner_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.storage = storage
        self._lock = RLock()
        recovered = storage.load()
        self._sessions: dict[str, dict[str, Any]] = recovered["sessions"]
        self._active: dict[str, Event] = {}
        self._turn_threads: dict[str, Thread] = {}
        self._operation_event: Event = Event()
        self._roles: dict[str, dict[str, Any]] = {k: dict(v) for k, v in _DEFAULT_ROLE_CONFIG.items()}
        if "PYTEST_CURRENT_TEST" not in os.environ:
            try:
                from validators.harness.backend import get_default_registry

                reg = get_default_registry()
                if "ollama" in reg and reg.get("ollama").status == "available" and reg.get("ollama").model:
                    ollama_m = reg.get("ollama").model
                    for r_name in self._roles:
                        self._roles[r_name] = {
                            "backend": "ollama",
                            "model": ollama_m,
                            "status": "configured",
                        }
                elif "anthropic" in reg and reg.get("anthropic").status == "available":
                    for r_name in self._roles:
                        self._roles[r_name] = {
                            "backend": "anthropic",
                            "model": reg.get("anthropic").model or "claude-3-7-sonnet",
                            "status": "configured",
                        }
                elif "openai" in reg and reg.get("openai").status == "available":
                    for r_name in self._roles:
                        self._roles[r_name] = {
                            "backend": "openai",
                            "model": reg.get("openai").model or "gpt-4o",
                            "status": "configured",
                        }
            except Exception:
                pass

        for r_name, r_cfg in recovered.get("roles", {}).items():
            if isinstance(r_cfg, dict):
                self._roles[r_name] = dict(r_cfg)
        self._runner_factory = runner_factory or self._default_runner
        self.events = EventDispatcher(recovered.get("events", []), self._persist_event)
        for session in self._sessions.values():
            session.setdefault("plans", {})
            if session["state"] == "RUNNING":
                session["state"] = "WAITING"
                session["updated_at"] = _now()
                self.events.publish("session.recovered", session["session_id"], state="WAITING")
            records = {
                r["operation_id"]: r
                for r in session.get("journal", [])
                if isinstance(r, dict) and "operation_id" in r
            }
            for r in records.values():
                r.setdefault("children", [])
                r.setdefault("child_results", {})
            for r in records.values():
                pid = r.get("parent_operation_id")
                if pid and pid in records:
                    parent_children = records[pid].setdefault("children", [])
                    if r["operation_id"] not in parent_children:
                        parent_children.append(r["operation_id"])
                    if r.get("status") in _OPERATION_TERMINAL or r.get("result") is not None:
                        records[pid].setdefault("child_results", {})[r["operation_id"]] = {
                            "operation_id": r["operation_id"],
                            "agent_id": r.get("agent_id"),
                            "role": r.get("role"),
                            "work_order_id": r.get("work_order_id"),
                            "status": r.get("status"),
                            "result": r.get("result"),
                            "completed_at": r.get("completed_at"),
                        }
        from .supervisor import LifecycleSupervisor
        self.supervisor = LifecycleSupervisor(self)
        self._active_runs: dict[str, Any] = {}
        self._run_driver_threads: dict[str, Thread] = {}
        self._run_stop_events: dict[str, Event] = {}
        for session in self._sessions.values():
            ws_str = session.get("workspace")
            if ws_str:
                ws = Path(ws_str)
                sup_dir = ws / ".sync" / "runtime" / "supervisor"
                if sup_dir.is_dir():
                    from .supervisor import Phase
                    for run_file in sup_dir.glob("run-*.yaml"):
                        try:
                            r_id = run_file.stem
                            loaded_state = self.supervisor.load_run_state(r_id, ws)
                            if loaded_state:
                                self._active_runs[r_id] = loaded_state
                                if loaded_state.phase != Phase.COMPLETE:
                                    if session.get("state") == "WAITING":
                                        session["state"] = "RUNNING"
                                        session["updated_at"] = _now()
                                    if loaded_state.phase in (Phase.BLOCKED, Phase.FAILED):
                                        self._prepare_run_for_resume(loaded_state, session, ws)
                                        try:
                                            self.supervisor.save_run_state(loaded_state, ws)
                                        except Exception:
                                            pass
                                    stop_ev = Event()
                                    self._run_stop_events[r_id] = stop_ev
                                    drv_thread = Thread(
                                        target=self._drive_run,
                                        args=(r_id, stop_ev),
                                        name=f"stackmind-supervisor-{r_id}",
                                        daemon=True,
                                    )
                                    self._run_driver_threads[r_id] = drv_thread
                                    drv_thread.start()
                        except Exception:
                            pass
        self._save()

    def _persist_event(self, _: RuntimeEvent) -> None:
        # Event listeners may publish from runner threads; serialize persistence
        # with all session/operation transitions to avoid competing temp-file replaces.
        with self._lock:
            self._save()

    def _save(self) -> None:
        self.storage.save({
            "sessions": self._sessions,
            "events": self.events.dump(),
            "roles": self._roles,
        })

    def _default_runner(self, workspace: str, agent: str) -> Any:
        import copy
        import importlib
        import sys
        if "validators.harness.runner" in sys.modules:
            try:
                importlib.reload(sys.modules["validators.harness.runner"])
            except Exception:
                pass
        from validators.harness.backend import get_default_registry
        from validators.harness.runner import AgentRunner

        if isinstance(self, SessionManager):
            canonical = self._canonical_role(agent)
            role_cfg = self._roles.get(canonical, {})
            backend_id = role_cfg.get("backend", "echo-agent")
            model_name = role_cfg.get("model")
            registry = get_default_registry()
            backend = registry.get(backend_id) if backend_id in registry else None
            if backend is not None:
                backend = copy.copy(backend)
                if model_name:
                    backend.model = model_name
                    backend.model_name = model_name
            provider_adapter = None
            if backend_id == "ollama":
                from validators.kernel.providers.adapter import OllamaAdapter

                endpoint = getattr(backend, "endpoint", None) or os.getenv("OLLAMA_HOST", "http://localhost:11434")
                if endpoint and not endpoint.startswith("http"):
                    endpoint = f"http://{endpoint}"
                m = model_name or getattr(backend, "model", "qwen2.5-coder:7b") or "qwen2.5-coder:7b"
                t_val = role_cfg.get("timeout") or getattr(backend, "timeout", None)
                provider_adapter = OllamaAdapter(endpoint=endpoint, model=m, default_timeout=float(t_val) if t_val else None)
            elif backend_id in ("openai", "openai-compatible"):
                from validators.kernel.providers.adapter import OpenAICompatibleAdapter

                base_url = getattr(backend, "base_url", None) or "https://api.openai.com/v1"
                api_key = getattr(backend, "api_key", None) or os.getenv("OPENAI_API_KEY", "")
                m = model_name or getattr(backend, "model", "gpt-4o") or "gpt-4o"
                t_val = role_cfg.get("timeout") or getattr(backend, "timeout", None)
                provider_adapter = OpenAICompatibleAdapter(base_url=base_url, api_key=api_key, model=m, default_timeout=float(t_val) if t_val else None)

            return AgentRunner(Path(workspace), agent, backend=backend, provider_adapter=provider_adapter)
        return AgentRunner(Path(workspace), agent)

    def list_backends(self) -> list[dict[str, Any]]:
        from validators.harness.backend import get_default_registry

        return get_default_registry().list_backends()

    def list_roles(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for role_name, config in self._roles.items():
                entry: dict[str, Any] = {
                    "role": role_name,
                    "backend": config.get("backend", "echo-agent"),
                    "model": config.get("model"),
                    "status": config.get("status", "configured"),
                }
                if config.get("credential_ref"):
                    entry["credentialRef"] = config["credential_ref"]
                result.append(entry)
            return result

    def _canonical_role(self, role: str) -> str:
        role_lower = str(role or "").lower().strip()
        return _ROLE_ALIASES.get(role_lower, role_lower)

    def _role_target_identifiers(self, role: str) -> set[str]:
        canonical = self._canonical_role(role)
        agents = set(_ROLE_TO_AGENTS.get(canonical, [canonical]))
        agents.add(str(role).lower().strip())
        agents.add(canonical)
        return agents

    def configure_role_backend(
        self,
        role: str,
        backend: str,
        model: str | None = None,
        credential_ref: str | None = None,
    ) -> dict[str, Any]:
        """Configure the execution backend for an agent role.

        Strict Rebinding Guard:
        Must be rejected (raises ValueError) if target role has any non-terminal
        Work Order in flight or active operation. Rebinding is permitted only
        between assignments when the role has zero non-terminal work orders.
        """
        if not isinstance(role, str) or not role.strip():
            raise ValueError("role is required")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend is required")

        canonical = self._canonical_role(role)
        target_ids = self._role_target_identifiers(role)

        with self._lock:
            for session in self._sessions.values():
                sess_agent = str(session.get("agent", "")).lower().strip()
                is_target_role = sess_agent in target_ids

                # 1. Active operation in session belonging to target role
                if is_target_role:
                    if session.get("active_operation") is not None and session.get("state") == "RUNNING":
                        raise ValueError(
                            f"Cannot rebind role '{role}': session '{session['session_id']}' has an active operation in flight"
                        )
                    for rec in session.get("journal", []):
                        if rec.get("status") not in _OPERATION_TERMINAL:
                            raise ValueError(
                                f"Cannot rebind role '{role}': operation '{rec.get('operation_id')}' is currently in flight ({rec.get('status')})"
                            )

                # 2. Any active operations explicitly tagged with target agent / role
                for rec in session.get("journal", []):
                    if rec.get("status") not in _OPERATION_TERMINAL:
                        op_agent = str(
                            rec.get("role")
                            or rec.get("agent_id")
                            or rec.get("metadata", {}).get("agent", sess_agent)
                        ).lower().strip()
                        if op_agent in target_ids:
                            raise ValueError(
                                f"Cannot rebind role '{role}': active operation '{rec.get('operation_id')}' belongs to role '{role}'"
                            )

                # 3. Created work orders in proposed / approved plans
                for plan in session.get("plans", {}).values():
                    for wo in plan.get("created_work_orders", []):
                        wo_status = str(wo.get("status", "ACTIVE")).upper()
                        if wo_status not in {"COMPLETED", "CANCELLED", "FAILED"}:
                            assigned = [str(a).lower().strip() for a in wo.get("assigned_agents", [])]
                            if any(a in target_ids for a in assigned):
                                raise ValueError(
                                    f"Cannot rebind role '{role}': in-flight non-terminal work order '{wo.get('id')}' assigned to role"
                                )

                # 4. Check workspace disk work orders
                workspace_str = session.get("workspace")
                if workspace_str:
                    active_wos_dir = Path(workspace_str) / ".sync" / "work-orders" / "ACTIVE"
                    if active_wos_dir.is_dir():
                        for wo_file in active_wos_dir.glob("*.yaml"):
                            try:
                                data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
                                if isinstance(data, dict):
                                    status = str(data.get("status", "ACTIVE")).upper()
                                    if status not in {"COMPLETED", "CANCELLED", "FAILED"}:
                                        assigned = [str(a).lower().strip() for a in data.get("assigned_agents", [])]
                                        if any(a in target_ids for a in assigned):
                                            raise ValueError(
                                                f"Cannot rebind role '{role}': in-flight non-terminal work order '{data.get('id', wo_file.stem)}' assigned to role"
                                            )
                            except ValueError:
                                raise
                            except Exception:
                                pass

            # Rebinding is permitted between assignments
            now = _now()
            entry: dict[str, Any] = {
                "backend": backend,
                "model": model,
                "status": "configured",
                "credential_ref": credential_ref,
                "updated_at": now,
            }
            self._roles[canonical] = entry
            self.events.publish(
                "role.backend_configured",
                "daemon",
                role=canonical,
                backend=backend,
                model=model,
                credential_ref=credential_ref,
            )
            self._save()
            return {
                "role": canonical,
                "backend": backend,
                "model": model,
                "appliedAt": now,
            }

    @staticmethod
    def _view(session: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in session.items() if key != "active_operation"}

    def create_session(
        self,
        agent: str,
        provider: str,
        contract: dict[str, Any],
        workspace: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not all(isinstance(value, str) and value for value in (agent, provider, workspace)):
            raise ValueError("agent, provider, and workspace are required")
        if not isinstance(contract, dict):
            raise ValueError("contract must be an object")
        _scaffold_protocol_citizenship(Path(workspace), agent)
        with self._lock:
            identifier = session_id or str(uuid4())
            if identifier in self._sessions:
                raise ValueError("session already exists")
            session = {
                "session_id": identifier,
                "agent": agent,
                "provider": provider,
                "contract": contract,
                "workspace": workspace,
                "state": "RUNNING",
                "created_at": _now(),
                "updated_at": _now(),
                "journal": [],
                "plans": {},
                "active_operation": None,
            }
            self._sessions[identifier] = session
            self.events.publish("session.started", identifier, agent=agent, provider=provider)
            self.events.publish("attempt.started", identifier)
            self.events.publish("contract.loaded", identifier)
            self._save()
            return self._view(session)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                view = self._view(self._sessions[session_id])
                active_run = self.get_active_run(session_id)
                if active_run:
                    view["run"] = active_run
                    view["phase"] = active_run.get("phase")
                return view
            except KeyError as error:
                raise KeyError("unknown session") from error

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._view(session) for session in self._sessions.values()]

    def session_history(self, session_id: str) -> list[dict[str, Any]]:
        """Return the durable audit journal for one session."""
        with self._lock:
            try:
                return [dict(record) for record in self._sessions[session_id]["journal"]]
            except KeyError as error:
                raise KeyError("unknown session") from error

    def close_session(self, session_id: str) -> dict[str, Any]:
        """Explicitly close a session without conflating it with operation cancellation."""
        with self._lock:
            return self._set_state(session_id, "COMPLETED", "session.completed")

    def _set_state(self, session_id: str, state: str, event: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session:
            raise KeyError("unknown session")
        if session["state"] in _TERMINAL:
            raise ValueError("session is terminal")
        session["state"] = state
        session["updated_at"] = _now()
        self.events.publish(event, session_id, state=state)
        self._save()
        return self._view(session)

    def pause_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._set_state(session_id, "PAUSED", "session.paused")

    def resume_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session["state"] not in {"PAUSED", "WAITING"}:
                raise ValueError("only paused or waiting sessions can be resumed")
            return self._set_state(session_id, "RUNNING", "session.resumed")

    def propose_plan(
        self,
        session_id: str,
        plan_id: str,
        title: str,
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Propose an architectural plan, placing it in AWAITING_APPROVAL state."""
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("plan_id is required")
        if not isinstance(title, str) or not title:
            raise ValueError("title is required")
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            if session["state"] in _TERMINAL:
                raise ValueError("session is terminal")

            plans = session.setdefault("plans", {})
            now = _now()
            meta = metadata or {}
            work_orders = meta.get("work_orders", [])

            if plan_id in plans:
                existing = plans[plan_id]
                if existing.get("state") == "APPROVED":
                    raise ValueError(f"Plan '{plan_id}' is already APPROVED")
                # Revision of existing plan
                revisions = existing.setdefault("revisions", [])
                revisions.append({
                    "title": existing.get("title"),
                    "content": existing.get("content"),
                    "state": existing.get("state"),
                    "feedback": existing.get("feedback"),
                    "updated_at": existing.get("updated_at"),
                })
                existing.update(
                    title=title,
                    content=content,
                    metadata=meta,
                    work_orders=work_orders,
                    state="AWAITING_APPROVAL",
                    feedback=None,
                    updated_at=now,
                )
                plan_record = existing
            else:
                plan_record = {
                    "plan_id": plan_id,
                    "session_id": session_id,
                    "title": title,
                    "content": content,
                    "metadata": meta,
                    "work_orders": work_orders,
                    "state": "AWAITING_APPROVAL",
                    "feedback": None,
                    "created_at": now,
                    "updated_at": now,
                }
                plans[plan_id] = plan_record

            self.events.publish(
                "plan.proposed",
                session_id,
                plan_id=plan_id,
                title=title,
                content=content,
                metadata=meta,
                state="AWAITING_APPROVAL",
            )
            self._save()
            return dict(plan_record)

    def get_plan(self, session_id: str, plan_id: str | None = None) -> dict[str, Any]:
        """Return the specified plan or the latest plan for the session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            plans = session.get("plans", {})
            if plan_id is not None:
                if plan_id not in plans:
                    raise KeyError(f"plan '{plan_id}' not found in session")
                return dict(plans[plan_id])
            if not plans:
                raise KeyError(f"no plans found in session '{session_id}'")
            return dict(list(plans.values())[-1])

    def list_plans(self, session_id: str) -> list[dict[str, Any]]:
        """Return all plans proposed in the session."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            return [dict(p) for p in session.get("plans", {}).values()]

    def get_next_plan_id(self, session_id: str) -> str:
        """Generate the next monotonically increasing plan ID for a session (e.g. PLAN-001, PLAN-002)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return "PLAN-001"
            plans = session.get("plans", {})
            max_num = 0
            for pid in plans.keys():
                m = re.match(r"^PLAN-(\d+)$", str(pid))
                if m:
                    max_num = max(max_num, int(m.group(1)))
            return f"PLAN-{max_num + 1:03d}"

    def approve_plan(
        self, session_id: str, plan_id: str, reason: str = ""
    ) -> list[dict[str, Any]]:
        """Approve a proposed plan and create its persistent Work Orders."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            plans = session.get("plans", {})
            if plan_id not in plans:
                raise KeyError(f"plan '{plan_id}' not found in session")

            plan = plans[plan_id]
            if plan.get("state") != "AWAITING_APPROVAL":
                raise ValueError("Plan is not in AWAITING_APPROVAL state")

            now = _now()
            plan["state"] = "APPROVED"
            plan["updated_at"] = now
            plan["approval_reason"] = reason

            created_work_orders: list[dict[str, Any]] = []
            proposed_wos = plan.get("metadata", {}).get("work_orders", [])
            workspace = Path(session["workspace"])
            wo_dir = workspace / ".sync" / "work-orders" / "ACTIVE"
            is_goal_plan = bool(plan.get("metadata", {}).get("is_goal"))

            if is_goal_plan and (workspace / ".sync").exists():
                try:
                    from .authoring import synthesize_child_work_orders
                    synthesized = synthesize_child_work_orders(workspace, plan, session_id=session_id)
                    if synthesized:
                        created_work_orders = synthesized
                except Exception:
                    pass
            else:
                for item in proposed_wos:
                    if isinstance(item, dict):
                        wo_id = str(item.get("id") or item.get("wo_id"))
                        wo_record = {
                            "id": wo_id,
                            "title": item.get("title", f"Work order {wo_id}"),
                            "type": item.get("type", "FEATURE"),
                            "status": "ACTIVE",
                            "priority": item.get("priority", "P0"),
                            "assigned_agents": item.get("assigned_agents", [session["agent"]]),
                            "description": item.get("description", ""),
                            "created": now,
                            "updated": now,
                        }
                        for k, v in item.items():
                            if k not in wo_record:
                                wo_record[k] = v
                    else:
                        wo_id = str(item)
                        wo_record = {
                            "id": wo_id,
                            "title": f"Work order {wo_id}",
                            "type": "FEATURE",
                            "status": "ACTIVE",
                            "priority": "P0",
                            "assigned_agents": [session["agent"]],
                            "description": "",
                            "created": now,
                            "updated": now,
                        }

                    created_work_orders.append(wo_record)
                    if (workspace / ".sync").exists():
                        try:
                            wo_dir.mkdir(parents=True, exist_ok=True)
                            wo_path = wo_dir / f"{wo_id}.yaml"
                            with wo_path.open("w", encoding="utf-8") as handle:
                                yaml.safe_dump(wo_record, handle, sort_keys=False)
                        except Exception:
                            pass
                        index_path = workspace / ".sync" / "work-orders" / "INDEX.yaml"
                        if index_path.exists():
                            try:
                                index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
                                if isinstance(index_data, dict) and "orders" in index_data:
                                    existing_ids = {
                                        o.get("id")
                                        for o in index_data["orders"]
                                        if isinstance(o, dict)
                                    }
                                    if wo_id not in existing_ids:
                                        index_data["orders"].append({
                                            "id": wo_id,
                                            "type": wo_record.get("type", "FEATURE"),
                                            "title": wo_record.get("title", f"Work order {wo_id}"),
                                            "status": wo_record.get("status", "ACTIVE"),
                                            "priority": wo_record.get("priority", "P0"),
                                            "assigned_agents": wo_record.get("assigned_agents", [session["agent"]]),
                                            "dependencies": wo_record.get("dependencies", []),
                                            "deliverable": wo_record.get("deliverable"),
                                            "created": wo_record.get("created"),
                                            "updated": wo_record.get("updated"),
                                            "file": f"work-orders/ACTIVE/{wo_id}.yaml",
                                        })
                                        with index_path.open("w", encoding="utf-8") as handle:
                                            yaml.safe_dump(index_data, handle, sort_keys=False)
                            except Exception:
                                pass

            plan["created_work_orders"] = created_work_orders
            self.events.publish(
                "plan.approved",
                session_id,
                plan_id=plan_id,
                reason=reason,
                work_orders=[w["id"] for w in created_work_orders],
                created_records=created_work_orders,
            )
            self._save()

            # For bootstrap goal plans, dispatch Turn 2: Architecture authors child WOs and Contracts
            if is_goal_plan:
                already_authoring = False
                for r in session.get("journal", []):
                    if r.get("metadata", {}).get("is_authoring") and r.get("status") not in _OPERATION_TERMINAL:
                        already_authoring = True
                        break
                if not already_authoring:
                    authoring_prompt = (
                        f"The architecture plan '{plan_id}' has been approved by the operator (reason: {reason or 'Approved by operator'}).\n\n"
                        "Your task now as Senior Architect is to author the implementation Work Orders and Contracts for the tasks in PLAN.md:\n"
                        "1. Review PLAN.md for the approved milestones and tasks.\n"
                        "2. For each task, call write_file to write a Work Order YAML file to .sync/work-orders/ACTIVE/<WO-ID>.yaml "
                        "(e.g. WO-001.yaml) conforming to schemas/work-order.schema.json.\n"
                        "3. For each Work Order, call write_file to write a corresponding Contract YAML file to .sync/contracts/<WO-ID>.yaml "
                        "conforming to schemas/contract.schema.json.\n"
                        "4. When all work orders and contracts are written, return the final HarnessDecision JSON declaring status 'completed' "
                        "and modified_files listing all authored files."
                    )
                    turn_wo_id = plan.get("metadata", {}).get("work_order_id") or "WO-000"
                    self.start_turn(
                        session_id,
                        authoring_prompt,
                        role="architecture",
                        agent_id="claude",
                        work_order_id=turn_wo_id,
                        is_authoring=True,
                    )

            self._operation_event.set()
            return [dict(w) for w in created_work_orders]

    def reject_plan(
        self, session_id: str, plan_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """Reject a proposed plan with operator feedback, creating zero Work Orders."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            plans = session.get("plans", {})
            if plan_id not in plans:
                raise KeyError(f"plan '{plan_id}' not found in session")

            plan = plans[plan_id]
            if plan.get("state") != "AWAITING_APPROVAL":
                raise ValueError("Plan is not in AWAITING_APPROVAL state")

            now = _now()
            plan["state"] = "REJECTED"
            plan["feedback"] = reason
            plan["reason"] = reason
            plan["updated_at"] = now
            plan["created_work_orders"] = []

            self.events.publish(
                "plan.rejected",
                session_id,
                plan_id=plan_id,
                reason=reason,
            )
            self._save()
            return dict(plan)

    def approve_run(self, session_id: str, reason: str = "") -> dict[str, Any]:
        """Approve an active supervisor run awaiting operator approval."""
        with self._lock:
            active_run = None
            for state in self._active_runs.values():
                if state.session_id == session_id:
                    active_run = state
                    break
            if not active_run:
                raise KeyError("no active supervisor run for session")

            # Route through approve_plan to unify the approval path
            if active_run.plan_id:
                self.approve_plan(session_id, active_run.plan_id, reason=reason)

            self.events.publish(
                "run.approved",
                session_id,
                run_id=active_run.run_id,
                reason=reason,
            )
            self._save()
            return active_run.to_dict()

    def reject_run(self, session_id: str, reason: str = "") -> dict[str, Any]:
        """Reject an active supervisor run awaiting operator approval."""
        with self._lock:
            active_run = None
            for state in self._active_runs.values():
                if state.session_id == session_id:
                    active_run = state
                    break
            if not active_run:
                raise KeyError("no active supervisor run for session")

            # Route through reject_plan to unify the rejection path
            if active_run.plan_id:
                self.reject_plan(session_id, active_run.plan_id, reason=reason)

            self.events.publish(
                "run.rejected",
                session_id,
                run_id=active_run.run_id,
                reason=reason,
            )
            self._save()
            return active_run.to_dict()

    def _prepare_run_for_resume(self, state: Any, session: dict[str, Any], ws: Path) -> Any:
        """Reset failed/blocked run state, unblock work orders on disk, and invalidate stale operations."""
        from .supervisor import Phase

        state.error = None
        state.blocked_wo_ids.clear()
        state.failed_wo_ids.clear()
        state.retry_counts.clear()

        # Invalidate any existing non-completed operations in the session journal
        # so the supervisor does not immediately re-block on stale historical failures.
        completed_set = set(state.completed_wo_ids)
        if not hasattr(state, "ignored_operation_ids"):
            state.ignored_operation_ids = []
        for op in session.get("journal", []):
            op_wo = op.get("work_order_id")
            op_id = op.get("operation_id")
            op_status = str(op.get("status", "")).upper()
            if op_id and (op_wo is None or op_wo not in completed_set):
                if op_status == "COMPLETED":
                    continue
                if op_id not in state.ignored_operation_ids:
                    state.ignored_operation_ids.append(op_id)
                if op_status not in _OPERATION_TERMINAL:
                    t = self._turn_threads.get(op_id)
                    if t is None or not t.is_alive():
                        op["status"] = "CANCELLED"
                        op.setdefault("transitions", []).append({
                            "status": "CANCELLED",
                            "at": _now(),
                            "reason": "invalidated_on_resume",
                        })
                        c_event = self._active.get(op_id)
                        if c_event:
                            c_event.set()

        # Clear session active_operation if it was pointing to an invalidated or dead operation
        active_op_id = session.get("active_operation")
        if active_op_id:
            if active_op_id in state.ignored_operation_ids:
                session["active_operation"] = None
            else:
                t = self._turn_threads.get(active_op_id)
                if t is None or not t.is_alive():
                    session["active_operation"] = None

        # Unblock non-completed work orders on disk (resetting status to ACTIVE and clearing errors)
        self.supervisor.unblock_in_flight_work_orders(state)

        prev_phase = None
        if state.transitions:
            for t in reversed(state.transitions):
                p_from = t.get("from")
                try:
                    candidate = Phase(p_from) if p_from else None
                except ValueError:
                    candidate = None
                if candidate and candidate not in (Phase.FAILED, Phase.BLOCKED):
                    prev_phase = candidate
                    break
        target_phase = prev_phase or (
            Phase.DISPATCHING if state.worker_wo_ids
            else Phase.AUTHORING if state.plan_id
            else Phase.PLANNING
        )
        _reset_phase_operation_id(state, target_phase)
        self.supervisor._transition(state, target_phase)
        return target_phase

    def _cleanup_orphaned_operations(self, state: Any, session: dict[str, Any]) -> None:
        """Mark orphaned non-terminal operations as CANCELLED and clear phase operation IDs."""
        completed_set = set(getattr(state, "completed_wo_ids", []))
        if not hasattr(state, "ignored_operation_ids"):
            state.ignored_operation_ids = []
        for op in session.get("journal", []):
            op_wo = op.get("work_order_id")
            op_id = op.get("operation_id")
            op_status = str(op.get("status", "")).upper()
            if op_id and (op_wo is None or op_wo not in completed_set):
                if op_status == "COMPLETED":
                    continue
                if op_status not in _OPERATION_TERMINAL:
                    t = self._turn_threads.get(op_id)
                    if t is None or not t.is_alive():
                        op["status"] = "CANCELLED"
                        op.setdefault("transitions", []).append({
                            "status": "CANCELLED",
                            "at": _now(),
                            "reason": "cleaned_up_on_resume",
                        })
                        c_event = self._active.get(op_id)
                        if c_event:
                            c_event.set()
                        if op_id not in state.ignored_operation_ids:
                            state.ignored_operation_ids.append(op_id)

        active_op_id = session.get("active_operation")
        if active_op_id:
            if active_op_id in state.ignored_operation_ids:
                session["active_operation"] = None
            else:
                t = self._turn_threads.get(active_op_id)
                if t is None or not t.is_alive():
                    session["active_operation"] = None

        _reset_phase_operation_id(state, state.phase)

    def resume_run(self, session_id: str, run_id: str | None = None) -> dict[str, Any]:
        """Resume an active, paused, or failed supervisor run."""
        with self._lock:
            import importlib
            import sys
            if "validators.kernel.daemon.supervisor" in sys.modules:
                try:
                    reloaded_mod = importlib.reload(sys.modules["validators.kernel.daemon.supervisor"])
                    if hasattr(self, "supervisor") and self.supervisor is not None:
                        self.supervisor.__class__ = reloaded_mod.LifecycleSupervisor
                except Exception:
                    pass
            if "validators.harness.runner" in sys.modules:
                try:
                    importlib.reload(sys.modules["validators.harness.runner"])
                except Exception:
                    pass

            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            ws = Path(session.get("workspace", ""))
            target_run_id = run_id
            if not target_run_id:
                sup_dir = ws / ".sync" / "runtime" / "supervisor"
                if sup_dir.is_dir():
                    run_files = sorted(sup_dir.glob("run-*.yaml"), key=os.path.getmtime, reverse=True)
                    if run_files:
                        target_run_id = run_files[0].stem
            if not target_run_id:
                raise KeyError("no supervisor run found to resume")

            state = self._active_runs.get(target_run_id)
            if state is None:
                state = self.supervisor.load_run_state(target_run_id, ws)
            if state is None:
                raise KeyError(f"run '{target_run_id}' not found")

            if session.get("state") in {"PAUSED", "WAITING"}:
                session["state"] = "RUNNING"
                session["updated_at"] = _now()
                self.events.publish("session.resumed", session_id)

            from .supervisor import Phase
            if state.session_id != session_id:
                state.session_id = session_id
                try:
                    self.supervisor.save_run_state(state, ws)
                except Exception:
                    pass
            if state.phase in (Phase.FAILED, Phase.BLOCKED):
                self._prepare_run_for_resume(state, session, ws)
            else:
                self._cleanup_orphaned_operations(state, session)

            self._active_runs[target_run_id] = state
            try:
                self.supervisor.save_run_state(state, ws)
            except Exception:
                pass
            existing_thread = self._run_driver_threads.get(target_run_id)
            if existing_thread is None or not existing_thread.is_alive():
                stop_event = Event()
                self._run_stop_events[target_run_id] = stop_event
                driver_thread = Thread(
                    target=self._drive_run,
                    args=(target_run_id, stop_event),
                    name=f"stackmind-supervisor-{target_run_id}",
                    daemon=True,
                )
                self._run_driver_threads[target_run_id] = driver_thread
                driver_thread.start()

            self.events.publish(
                "run.resumed",
                session_id,
                run_id=target_run_id,
                phase=state.phase.value,
            )
            self._save()
            self._operation_event.set()
            return state.to_dict()

    def synthesize_bootstrap_planning(
        self,
        workspace: Path | str,
        prompt: str,
        wo_id: str | None = None,
        assigned_agent: str = "claude",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Synthesize bootstrap planning work order and contract as trusted daemon bookkeeping."""
        return synthesize_bootstrap_planning(
            workspace, prompt, wo_id=wo_id, assigned_agent=assigned_agent
        )

    def begin_operation(
        self,
        session_id: str,
        operation_name: str,
        metadata: dict[str, Any] | None = None,
        *,
        parent_operation_id: str | None = None,
        work_order_id: str | None = None,
        contract_scope: Any = None,
        role: str | None = None,
        agent_id: str | None = None,
    ) -> tuple[Event, str]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session.get("state") not in {"RUNNING", "WAITING"}:
                raise ValueError("session is not running")
            if session.get("state") == "WAITING":
                session["state"] = "RUNNING"
                session["updated_at"] = _now()
                self.events.publish("session.resumed", session_id)

            parent_record = None
            if parent_operation_id is not None:
                for r in session.get("journal", []):
                    if r.get("operation_id") == parent_operation_id:
                        parent_record = r
                        break
                if parent_record is None:
                    raise KeyError(f"parent operation '{parent_operation_id}' not found in session")
                if parent_record.get("status") in _OPERATION_TERMINAL:
                    raise ValueError(
                        f"parent operation '{parent_operation_id}' is terminal ({parent_record.get('status')})"
                    )
                if contract_scope == "inherit":
                    contract_scope = parent_record.get("contract_scope")
                _verify_contract_scope_narrowing(parent_record.get("contract_scope"), contract_scope)
            else:
                if session.get("active_operation"):
                    from .supervisor import OperationContentionError
                    raise OperationContentionError("session already has an active operation")

            operation_id = str(uuid4())
            cancel = Event()
            now = _now()
            effective_role = self._canonical_role(
                role or agent_id or (session.get("agent") if parent_operation_id is None else "backend")
            )
            effective_agent_id = agent_id or role or effective_role
            record = {
                "operation_id": operation_id,
                "parent_operation_id": parent_operation_id,
                "children": [],
                "child_results": {},
                "operation": operation_name,
                "metadata": metadata or {},
                "work_order_id": work_order_id,
                "contract_scope": contract_scope,
                "role": effective_role,
                "agent_id": effective_agent_id,
                "status": "RUNNING",
                "started_at": now,
                "transitions": [
                    {"status": "REQUESTED", "at": now},
                    {"status": "AUTHORIZED", "at": now},
                    {"status": "RUNNING", "at": now},
                ],
            }
            if parent_record is not None:
                parent_record.setdefault("children", []).append(operation_id)
            else:
                session["active_operation"] = operation_id

            self._active[operation_id] = cancel
            session["journal"].append(record)
            payload = {
                "operation_id": operation_id,
                "operation": operation_name,
                "parent_operation_id": parent_operation_id,
                "metadata": metadata or {},
                "work_order_id": work_order_id,
                "contract_scope": contract_scope,
                "role": effective_role,
                "agent_id": effective_agent_id,
            }
            self.events.publish("operation.requested", session_id, **payload)
            self.events.publish("operation.authorized", session_id, operation_id=operation_id)
            self.events.publish("operation.started", session_id, operation_id=operation_id)
            if parent_operation_id is not None:
                self.events.publish(
                    "event.agentSpawned",
                    session_id,
                    agent_id=effective_agent_id,
                    role=effective_role,
                    operation_id=operation_id,
                    parent_operation_id=parent_operation_id,
                    work_order_id=work_order_id,
                )
            self._save()
            return cancel, operation_id

    def _operation(self, operation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for session in self._sessions.values():
            for record in session["journal"]:
                if record.get("operation_id") == operation_id:
                    return session, record
        raise KeyError("unknown operation")

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            _, record = self._operation(operation_id)
            return dict(record)

    def list_children(self, operation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            _, record = self._operation(operation_id)
            children_ids = record.get("children", [])
            children = []
            for child_id in children_ids:
                try:
                    children.append(self.get_operation(child_id))
                except KeyError:
                    pass
            return children

    def list_operations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sessions = (
                [self._sessions[session_id]] if session_id is not None else self._sessions.values()
            )
            return [
                dict(record)
                for session in sessions
                for record in session["journal"]
                if "operation_id" in record
            ]

    @staticmethod
    def _transition(record: dict[str, Any], status: str) -> None:
        if status not in _OPERATION_STATES:
            raise ValueError("invalid operation status")
        record["status"] = status
        record.setdefault("transitions", []).append({"status": status, "at": _now()})

    def cancel_operation(self, operation_id: str, cascade: bool = True) -> dict[str, Any]:
        """Request cooperative cancellation without changing the session lifecycle."""
        with self._lock:
            session, record = self._operation(operation_id)
            if record["status"] not in _OPERATION_TERMINAL:
                self._transition(record, "CANCEL_REQUESTED")
                cancel = self._active.get(operation_id)
                if cancel:
                    cancel.set()
                self.events.publish(
                    "operation.cancel_requested",
                    session["session_id"],
                    operation_id=operation_id,
                    cascade=cascade,
                )
            if cascade:
                for child_id in list(record.get("children", [])):
                    try:
                        self.cancel_operation(child_id, cascade=True)
                    except KeyError:
                        pass
            self._save()
            return dict(record)

    def complete_operation(
        self,
        session_id: str | None = None,
        operation_id: str | None = None,
        result: Any = None,
        status: str = "COMPLETED",
    ) -> dict[str, Any]:
        with self._lock:
            if operation_id is None:
                target_op_id = session_id
                target_session_id = None
            else:
                target_op_id = operation_id
                target_session_id = session_id

            if not target_op_id:
                raise KeyError("operation_id is required")

            found_session, record = self._operation(target_op_id)
            if target_session_id is not None and found_session["session_id"] != target_session_id:
                raise KeyError("unknown operation in session")
            session = found_session

            if record["status"] in _OPERATION_TERMINAL:
                return dict(record)
            cancel = self._active.get(target_op_id)
            final_status = (
                "CANCELLED"
                if record["status"] == "CANCEL_REQUESTED" or (cancel is not None and cancel.is_set())
                else status
            )
            if final_status not in _OPERATION_TERMINAL:
                raise ValueError("operation completion status must be terminal")

            if final_status == "COMPLETED":
                children_ids = record.get("children", [])
                for child_id in children_ids:
                    _, child_record = self._operation(child_id)
                    if child_record.get("status") not in _OPERATION_TERMINAL:
                        raise ValueError(
                            f"cannot complete operation '{target_op_id}': child operation '{child_id}' is active ({child_record.get('status')})"
                        )
                    if child_record.get("status") == "FAILED":
                        raise ValueError(
                            f"cannot complete operation '{target_op_id}' as COMPLETED: child operation '{child_id}' failed"
                        )

            self._transition(record, final_status)
            record.update(completed_at=_now(), result=result)
            parent_id = record.get("parent_operation_id")
            if parent_id:
                try:
                    _, parent_record = self._operation(parent_id)
                    parent_record.setdefault("child_results", {})[target_op_id] = {
                        "operation_id": target_op_id,
                        "agent_id": record.get("agent_id"),
                        "role": record.get("role"),
                        "work_order_id": record.get("work_order_id"),
                        "status": final_status,
                        "result": result,
                        "completed_at": record.get("completed_at"),
                    }
                except KeyError:
                    pass
            if record.get("child_results"):
                if isinstance(record.get("result"), dict):
                    record["result"].setdefault("child_results", record["child_results"])
                    record["result"].setdefault(
                        "aggregated_results", list(record["child_results"].values())
                    )
                elif record.get("result") is None:
                    record["result"] = {
                        "child_results": record["child_results"],
                        "aggregated_results": list(record["child_results"].values()),
                    }
            self._active.pop(target_op_id, None)
            if session.get("active_operation") == target_op_id:
                session["active_operation"] = None
            event = (
                "operation.cancelled" if final_status == "CANCELLED"
                else "operation.failed" if final_status == "FAILED"
                else "operation.blocked" if final_status == "BLOCKED"
                else "operation.completed"
            )
            op_role = record.get("role") or session.get("role")
            op_agent = record.get("agent_id") or session.get("agent_id")
            op_wo = record.get("work_order_id") or session.get("work_order_id")
            op_name = record.get("operation") or "operation"
            op_err = record.get("error")
            self.events.publish(
                event,
                session["session_id"],
                operation_id=target_op_id,
                status=final_status,
                role=op_role,
                agent_id=op_agent,
                work_order_id=op_wo,
                operation=op_name,
                error=op_err,
            )
            self._save()
            self._operation_event.set()
            return dict(record)

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            operation_id = session.get("active_operation")
            if operation_id:
                self.cancel_operation(operation_id)
                return self._view(session)
            return self._set_state(session_id, "CANCELLED", "session.cancelled")

    def start_turn(self, session_id: str, prompt: str, **params: Any) -> dict[str, Any]:
        """Start a governed turn in a background runner without holding the manager lock."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            role_param = str(params.get("role") or params.get("agent_id") or session.get("agent") or "").lower().strip()
            if self._canonical_role(role_param) == "gitops" and params.get("work_order_id"):
                ws_path = Path(session["workspace"])
                if (ws_path / ".sync" / "work-orders").is_dir():
                    from validators.harness.d024_gate import D024Gate, D024ViolationError
                    d024_decision = D024Gate().evaluate_work_order(ws_path, str(params["work_order_id"]))
                    if not d024_decision.passed:
                        raise D024ViolationError(f"D024 QA Gate Blocked: {d024_decision.reason}")

            is_goal = bool(params.get("is_goal"))
            created_run_id = None
            if is_goal:
                if "role" not in params:
                    params["role"] = "architecture"
                if "agent_id" not in params:
                    params["agent_id"] = "claude"
                role_param = str(params["role"]).lower().strip()
                canonical_role = self._canonical_role(role_param)
                effective_agent = _ROLE_TO_PRIMARY_AGENT.get(
                    str(params.get("agent_id") or canonical_role).lower().strip(),
                    "claude",
                )
                ws_path = Path(session["workspace"])
                wo_rec, _ = synthesize_bootstrap_planning(
                    ws_path,
                    prompt,
                    wo_id=str(params.get("work_order_id")) if params.get("work_order_id") else None,
                    assigned_agent=effective_agent,
                )
                params["work_order_id"] = wo_rec["id"]

                existing_run_id = params.get("run_id")
                if not existing_run_id:
                    # S1 Wiring: register run with supervisor and launch background driver
                    created_run_id = f"run-{uuid4().hex[:8]}"
                    from .supervisor import Phase
                    run_state = self.supervisor.start_run(created_run_id, prompt, ws_path, session_id)
                    run_state.planning_wo_id = wo_rec["id"]
                    self._active_runs[created_run_id] = run_state
                    stop_event = Event()
                    self._run_stop_events[created_run_id] = stop_event
                    params["run_id"] = created_run_id
                else:
                    params["run_id"] = existing_run_id

            cancel_event, operation_id = self.begin_operation(
                session_id,
                "turn",
                {"prompt": prompt, **params},
                parent_operation_id=params.get("parent_operation_id"),
                work_order_id=params.get("work_order_id"),
                contract_scope=params.get("contract_scope"),
                role=params.get("role"),
                agent_id=params.get("agent_id"),
            )
            if created_run_id and created_run_id in self._active_runs:
                r_state = self._active_runs[created_run_id]
                r_state.planning_operation_id = operation_id
                r_state.phase = Phase.PLANNING
                driver_thread = Thread(
                    target=self._drive_run,
                    args=(created_run_id, self._run_stop_events[created_run_id]),
                    name=f"stackmind-supervisor-{created_run_id}",
                    daemon=True,
                )
                self._run_driver_threads[created_run_id] = driver_thread
                driver_thread.start()

            canonical_role = self._canonical_role(role_param or "backend")
            agent_id_param = str(params.get("agent_id") or params.get("role") or canonical_role)
            self.events.publish(
                "turn.started",
                session_id,
                operation_id=operation_id,
                prompt=prompt,
                role=canonical_role,
                agent_id=agent_id_param,
                work_order_id=params.get("work_order_id"),
                run_id=created_run_id,
            )
            thread = Thread(
                target=self._run_turn,
                args=(session_id, operation_id, cancel_event, prompt),
                name=f"stackmind-turn-{operation_id}",
                daemon=True,
            )
            self._turn_threads[operation_id] = thread
            self._save()
        thread.start()
        turn_op = self.get_operation(operation_id)
        if created_run_id:
            turn_op["run_id"] = created_run_id
        return turn_op

    def execute_work_order(
        self, session_id: str, work_order_id: str, prompt: str | None = None, **params: Any
    ) -> dict[str, Any]:
        """Start a turn to execute an assigned work order via the AgentRunner harness."""
        p = prompt or f"Execute work order {work_order_id}"
        return self.start_turn(session_id, p, work_order_id=work_order_id, **params)

    def _drive_run(
        self,
        run_id: str,
        stop_event: Event,
        max_wait_seconds: float | None = None,
    ) -> None:
        """Background driver loop continuously advancing the supervisor run until completion."""
        import time
        from .supervisor import AdvanceResult, Phase

        if max_wait_seconds is None:
            max_wait_seconds = float(os.environ.get("SUPERVISOR_OPERATION_TIMEOUT", 3600.0))

        state = self._active_runs.get(run_id)
        if not state:
            return
        ws = Path(state.workspace)
        waiting_operation_started_at: float | None = None

        try:
            while not stop_event.is_set() and state.phase not in (Phase.COMPLETE, Phase.FAILED, Phase.BLOCKED):
                prev_phase = state.phase
                try:
                    result = self.supervisor.advance(state)
                except Exception as exc:
                    state.error = f"Supervisor advance exception: {exc}"
                    self.supervisor._transition(state, Phase.FAILED)
                    result = AdvanceResult.FAILED

                if state.phase != prev_phase:
                    waiting_operation_started_at = None
                    self.events.publish(
                        "run.phase",
                        state.session_id,
                        run_id=run_id,
                        phase=state.phase.value,
                        goal=state.product_goal,
                    )
                    try:
                        self.supervisor.save_run_state(state, ws)
                    except Exception:
                        pass

                if result == AdvanceResult.WAITING_FOR_HUMAN:
                    waiting_operation_started_at = None
                    self._operation_event.wait(timeout=0.2)
                    self._operation_event.clear()
                    continue
                if result == AdvanceResult.WAITING_FOR_OPERATION:
                    has_alive_threads = False
                    with self._lock:
                        has_alive_threads = any(t.is_alive() for t in self._turn_threads.values())

                    if has_alive_threads:
                        # As long as worker threads are actively executing, supervisor stays alive
                        # and never times out (vital for slow local quantized models).
                        waiting_operation_started_at = None
                        self._operation_event.wait(timeout=0.5)
                        self._operation_event.clear()
                        continue

                    # If timeout is disabled (max_wait_seconds <= 0), wait indefinitely for event
                    if max_wait_seconds <= 0:
                        self._operation_event.wait(timeout=0.5)
                        self._operation_event.clear()
                        continue

                    # No threads are alive; track whether an orphaned/stuck operation has timed out
                    now_mono = time.monotonic()
                    if waiting_operation_started_at is None:
                        waiting_operation_started_at = now_mono
                    elif now_mono - waiting_operation_started_at > max_wait_seconds:
                        state.error = (
                            f"Timed out waiting for operations in phase {state.phase.value} "
                            f"after {max_wait_seconds:.0f}s"
                        )
                        self.supervisor._transition(state, Phase.BLOCKED)
                        result = AdvanceResult.BLOCKED
                        try:
                            self.supervisor.save_run_state(state, ws)
                        except Exception:
                            pass
                        self.events.publish(
                            "run.blocked",
                            state.session_id,
                            run_id=run_id,
                            phase=state.phase.value,
                            error=state.error,
                        )
                        break

                    self._operation_event.wait(timeout=0.2)
                    self._operation_event.clear()
                    continue
                if result in (AdvanceResult.COMPLETE, AdvanceResult.FAILED, AdvanceResult.BLOCKED):
                    try:
                        self.supervisor.save_run_state(state, ws)
                    except Exception:
                        pass
                    self.events.publish(
                        f"run.{state.phase.value.lower()}",
                        state.session_id,
                        run_id=run_id,
                        phase=state.phase.value,
                        error=state.error,
                    )
                    break
        finally:
            stop_event.set()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Get the state dictionary for an active or persisted run."""
        state = self._active_runs.get(run_id)
        if state is not None:
            return state.to_dict()
        for session in self._sessions.values():
            ws = Path(session.get("workspace", ""))
            persisted = self.supervisor.load_run_state(run_id, ws)
            if persisted is not None:
                return persisted.to_dict()
        return None

    def get_active_run(self, session_id: str) -> dict[str, Any] | None:
        """Get the currently active run for a session, if any."""
        from .supervisor import Phase
        for state in self._active_runs.values():
            if state.session_id == session_id and state.phase != Phase.COMPLETE:
                return state.to_dict()
        session = self._sessions.get(session_id)
        if session:
            ws = Path(session.get("workspace", ""))
            sup_dir = ws / ".sync" / "runtime" / "supervisor"
            if sup_dir.is_dir():
                run_files = sorted(sup_dir.glob("run-*.yaml"), key=os.path.getmtime, reverse=True)
                for rf in run_files:
                    persisted = self.supervisor.load_run_state(rf.stem, ws)
                    if persisted and persisted.session_id == session_id and persisted.phase != Phase.COMPLETE:
                        self._active_runs[rf.stem] = persisted
                        return persisted.to_dict()
        return None

    def _run_turn(
        self, session_id: str, operation_id: str, cancel_event: Event, prompt: str
    ) -> None:
        """Run outside the manager lock; terminal state is resolved under it."""
        with self._lock:
            session = self._sessions[session_id]
            workspace = str(session["workspace"])
            _, op_rec = self._operation(operation_id)
            raw_agent = str(op_rec.get("agent_id") or op_rec.get("role") or session.get("agent") or "codex").lower().strip()
            agent = _ROLE_TO_PRIMARY_AGENT.get(raw_agent, raw_agent)
            op_role = op_rec.get("role") or self._canonical_role(agent)
        tool_name = "harness.run_once"
        self.events.tool_call(
            session_id, tool_name, operation_id, {"prompt": prompt}, operation_id,
            role=op_role, agent_id=agent,
        )
        try:
            ws_path = Path(workspace)
            _scaffold_protocol_citizenship(ws_path, agent)
            runner = self._runner_factory(workspace, agent)
            runner.on_token = lambda text: self.events.publish("token.delta", session_id, delta=text, operation_id=operation_id)
            with self._lock:
                try:
                    _, op_rec = self._operation(operation_id)
                    b_id = getattr(runner, "backend_id", None)
                    if isinstance(b_id, str):
                        op_rec["backend_id"] = b_id
                    b_mod = getattr(runner, "backend_model", None)
                    if isinstance(b_mod, str):
                        op_rec["model"] = b_mod
                except KeyError:
                    pass
            wo_id_param = op_rec.get("work_order_id")
            is_authoring_param = bool(op_rec.get("metadata", {}).get("is_authoring"))
            try:
                result = runner.run_once(
                    cancel_event=cancel_event,
                    operation_id=operation_id,
                    prompt=prompt,
                    work_order_id=wo_id_param,
                    is_authoring=is_authoring_param,
                )
            except TypeError:
                try:
                    result = runner.run_once(
                        cancel_event=cancel_event,
                        operation_id=operation_id,
                        prompt=prompt,
                        work_order_id=wo_id_param,
                    )
                except TypeError:
                    try:
                        result = runner.run_once(cancel_event=cancel_event, operation_id=operation_id, prompt=prompt)
                    except TypeError:
                        result = runner.run_once(cancel_event=cancel_event, operation_id=operation_id)
            b_id = getattr(runner, "backend_id", None)
            b_mod = getattr(runner, "backend_model", None)
            res_meta = getattr(result, "meta", None) or {}
            res_reason = getattr(result, "reason", None)
            res_status = getattr(result, "status", "completed")
            result_data = {
                "status": res_status,
                "persisted": getattr(result, "persisted", False),
                "task_id": getattr(result, "task_id", None),
                "reason": res_reason,
                "error": res_reason or res_status,
                "report_path": str(result.report_path) if getattr(result, "report_path", None) else None,
                "summary": res_meta.get("summary"),
                "blockers": res_meta.get("blockers") or ([res_reason] if res_reason else []),
                "backend_id": b_id if isinstance(b_id, str) else None,
                "model": b_mod if isinstance(b_mod, str) else None,
            }
            if result.status == "cancelled" or cancel_event.is_set():
                self.events.tool_result(
                    session_id, tool_name, operation_id, "cancelled", operation_id=operation_id,
                    role=op_role, agent_id=agent,
                )
                self.complete_operation(session_id, operation_id, result_data, status="CANCELLED")
            elif result.status in {"completed", "idle"}:
                # Check if this was a Turn 1 planning turn that generated a valid PLAN.md
                is_goal_turn = bool(op_rec.get("metadata", {}).get("is_goal"))
                is_authoring_turn = bool(op_rec.get("metadata", {}).get("is_authoring"))
                plan_file = ws_path / "PLAN.md"
                if is_goal_turn and not is_authoring_turn and plan_file.is_file():
                    try:
                        from validators.harness.plan import validate_plan_structure, parse_plan
                        plan_content = plan_file.read_text(encoding="utf-8")
                        is_valid, _ = validate_plan_structure(plan_content)
                        if is_valid:
                            parsed_plan = parse_plan(plan_content)
                            from .authoring import get_next_work_order_int, determine_assigned_agent, extract_deliverable_spec
                            start_idx = get_next_work_order_int(ws_path)
                            proposed_wos = []
                            for offset, m in enumerate(parsed_plan.milestones):
                                idx = start_idx + offset
                                wo_id = f"WO-{idx:03d}"
                                agent_id, role = determine_assigned_agent(m.title, m.tasks)
                                deliv = extract_deliverable_spec(m.title, m.tasks, role)
                                proposed_wos.append({
                                    "id": wo_id,
                                    "title": m.title,
                                    "type": "FEATURE",
                                    "priority": "P1" if offset > 0 else "P0",
                                    "assigned_agents": [agent_id],
                                    "dependencies": [f"WO-{idx-1:03d}"] if offset > 0 else [],
                                    "deliverable": deliv,
                                    "description": "\n".join(m.tasks) if m.tasks else m.title,
                                })
                            plan_meta = {
                                "work_orders": proposed_wos,
                                "plan_structure": parsed_plan.to_dict(),
                                "prompt": prompt,
                                "operation_id": operation_id,
                                "is_goal": True,
                                "work_order_id": op_rec.get("work_order_id"),
                            }
                            existing_plans = self.list_plans(session_id)
                            if existing_plans and str(existing_plans[-1].get("state", "")).upper() == "REJECTED":
                                plan_id = existing_plans[-1].get("plan_id")
                            else:
                                plan_id = self.get_next_plan_id(session_id)

                            self.propose_plan(
                                session_id=session_id,
                                plan_id=plan_id,
                                title=parsed_plan.title or f"Plan for: {prompt[:60]}",
                                content=plan_content,
                                metadata=plan_meta,
                            )
                    except Exception:
                        pass

                self.events.tool_result(
                    session_id, tool_name, operation_id, "success", operation_id=operation_id,
                    result=result_data, role=op_role, agent_id=agent,
                )
                self.complete_operation(session_id, operation_id, result_data)
            elif result.status == "blocked":
                self.events.tool_result(
                    session_id, tool_name, operation_id, "blocked", operation_id=operation_id,
                    result=result_data, role=op_role, agent_id=agent,
                )
                self.complete_operation(session_id, operation_id, result_data, status="BLOCKED")
            else:
                self.events.tool_result(
                    session_id, tool_name, operation_id, "failure", operation_id=operation_id,
                    error=result_data, role=op_role, agent_id=agent,
                )
                self.complete_operation(session_id, operation_id, result_data, status="FAILED")
        except Exception as error:
            err_dict = {
                "type": type(error).__name__,
                "message": str(error),
                "error": str(error) or type(error).__name__,
            }
            self.events.tool_result(
                session_id, tool_name, operation_id, "failure", operation_id=operation_id,
                error=err_dict, role=op_role, agent_id=agent,
            )
            self.complete_operation(
                session_id, operation_id, err_dict, status="FAILED"
            )
        finally:
            with self._lock:
                self._turn_threads.pop(operation_id, None)

    def record_verification(self, session_id: str, result: Any) -> None:
        with self._lock:
            self.events.publish("verification.started", session_id)
            self.events.publish("verification.completed", session_id, result=result)

    def record_experience(self, session_id: str, experience_id: str) -> None:
        with self._lock:
            self.events.publish("experience.recorded", session_id, experience_id=experience_id)

    def record_approval(self, session_id: str, approved: bool, reason: str = "") -> None:
        """Persist a human decision; the UI may request this, never make it itself."""
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise KeyError("unknown session")
            decision = "APPROVED" if approved else "REJECTED"
            session["journal"].append(
                {
                    "operation": "human.approval",
                    "status": decision,
                    "reason": reason,
                    "completed_at": _now(),
                }
            )
            self.events.publish("approval.recorded", session_id, approved=approved, reason=reason)
            self._save()

    def dispatch_subagent(
        self,
        session_id: str,
        parent_operation_id: str,
        role: str,
        work_order_id: str,
        contract_scope: Any = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a specialized subagent operation under a parent operation."""
        canonical_role = self._canonical_role(role)
        if canonical_role not in _LOGICAL_ROLES:
            raise ValueError(f"Unknown role '{role}'; must be one of {_LOGICAL_ROLES}")
        with self._lock:
            parent_session, parent_record = self._operation(parent_operation_id)
            if parent_session["session_id"] != session_id:
                raise KeyError(
                    f"parent operation '{parent_operation_id}' does not belong to session '{session_id}'"
                )
            effective_scope = (
                parent_record.get("contract_scope")
                if contract_scope is None or contract_scope == "inherit"
                else contract_scope
            )
            _verify_contract_scope_narrowing(parent_record.get("contract_scope"), effective_scope)

            if canonical_role == "gitops" and work_order_id:
                ws_path = Path(parent_session["workspace"])
                if (ws_path / ".sync" / "work-orders").is_dir():
                    from validators.harness.d024_gate import D024Gate, D024ViolationError
                    d024_decision = D024Gate().evaluate_work_order(ws_path, work_order_id)
                    if not d024_decision.passed:
                        raise D024ViolationError(f"D024 QA Gate Blocked: {d024_decision.reason}")
        _, op_id = self.begin_operation(
            session_id,
            f"execute.{canonical_role}",
            metadata=metadata,
            parent_operation_id=parent_operation_id,
            work_order_id=work_order_id,
            contract_scope=effective_scope,
            role=canonical_role,
            agent_id=agent_id or canonical_role,
        )
        return self.get_operation(op_id)

    def _resolve_agent_operation(
        self, agent_identifier: str, session_id: str | None = None
    ) -> dict[str, Any] | None:
        """Find the most relevant operation for an agent identifier or operation_id."""
        target_id = str(agent_identifier).strip()
        sessions = (
            [self._sessions[session_id]]
            if session_id is not None and session_id in self._sessions
            else list(self._sessions.values())
        )
        # 1. Exact operation_id match
        for session in sessions:
            for r in session.get("journal", []):
                if r.get("operation_id") == target_id:
                    return r
        # 2. Match by agent_id or role
        candidates: list[dict[str, Any]] = []
        target_canonical = self._canonical_role(target_id)
        for session in sessions:
            for r in session.get("journal", []):
                if "operation_id" not in r:
                    continue
                aid = str(r.get("agent_id", "")).lower()
                role = str(r.get("role", "")).lower()
                if (
                    aid == target_id.lower()
                    or role == target_id.lower()
                    or role == target_canonical
                    or self._canonical_role(aid) == target_canonical
                ):
                    candidates.append(r)
        if not candidates:
            return None
        active_candidates = [c for c in candidates if c.get("status") not in _OPERATION_TERMINAL]
        if active_candidates:
            active_ids = {x["operation_id"] for x in active_candidates}
            roots = [c for c in active_candidates if c.get("parent_operation_id") not in active_ids]
            return roots[-1] if roots else active_candidates[0]
        candidate_ids = {x["operation_id"] for x in candidates}
        roots = [c for c in candidates if c.get("parent_operation_id") not in candidate_ids]
        return roots[-1] if roots else candidates[-1]

    def list_agents(
        self, session_id: str | None = None, operation_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            sessions = (
                [self._sessions[session_id]]
                if session_id is not None and session_id in self._sessions
                else list(self._sessions.values())
            )
            agents: list[dict[str, Any]] = []
            seen_ops: set[str] = set()

            for session in sessions:
                records = [r for r in session.get("journal", []) if "operation_id" in r]
                if operation_id is not None:
                    records = [
                        r for r in records
                        if r["operation_id"] == operation_id or r.get("parent_operation_id") == operation_id
                    ]
                for r in records:
                    op_id = r["operation_id"]
                    if op_id in seen_ops:
                        continue
                    seen_ops.add(op_id)
                    role = r.get("role") or self._canonical_role(
                        r.get("agent_id") or session.get("agent", "backend")
                    )
                    agent_id = r.get("agent_id") or role or session.get("agent", "agent")
                    entry = {
                        "agentId": agent_id,
                        "agent_id": agent_id,
                        "role": role,
                        "state": r.get("status", "UNKNOWN"),
                        "status": r.get("status", "UNKNOWN"),
                        "workOrderId": r.get("work_order_id"),
                        "work_order_id": r.get("work_order_id"),
                        "operationId": op_id,
                        "operation_id": op_id,
                        "parentOperationId": r.get("parent_operation_id"),
                        "parent_operation_id": r.get("parent_operation_id"),
                        "contractScope": r.get("contract_scope"),
                        "contract_scope": r.get("contract_scope"),
                        "sessionId": session.get("session_id"),
                        "session_id": session.get("session_id"),
                    }
                    if r.get("backend_id"):
                        entry["backend"] = r["backend_id"]
                    if r.get("model"):
                        entry["model"] = r["model"]
                    agents.append(entry)
            return agents

    def cancel_agent(
        self,
        agent_identifier: str,
        session_id: str | None = None,
        reason: str = "user_cancelled",
        cascade: bool = True,
    ) -> dict[str, Any]:
        """Cancel an agent's operation subtree without affecting parent or siblings."""
        with self._lock:
            op_record = self._resolve_agent_operation(agent_identifier, session_id=session_id)
            if op_record is None:
                raise KeyError(f"agent '{agent_identifier}' not found")
            op_id = op_record["operation_id"]
            cancelled_record = self.cancel_operation(op_id, cascade=cascade)
            return {
                "agentId": op_record.get("agent_id", agent_identifier),
                "agent_id": op_record.get("agent_id", agent_identifier),
                "role": op_record.get("role"),
                "operationId": op_id,
                "operation_id": op_id,
                "canceled": True,
                "cancelled": True,
                "reason": reason,
                "state": cancelled_record.get("status"),
            }

    def inspect_agent(
        self,
        agent_identifier: str,
        session_id: str | None = None,
        after: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            op_record = self._resolve_agent_operation(agent_identifier, session_id=session_id)
            if op_record is None:
                raise KeyError(f"agent '{agent_identifier}' not found")
            op_id = op_record["operation_id"]
            sess_id = session_id
            if sess_id is None:
                for s in self._sessions.values():
                    if any(r.get("operation_id") == op_id for r in s.get("journal", [])):
                        sess_id = s.get("session_id")
                        break

            all_events = self.events.events(session_id=sess_id, after=after)
            agent_events = [
                e
                for e in all_events
                if e.payload.get("operation_id") == op_id
                or e.payload.get("agent_id") == op_record.get("agent_id")
                or (e.payload.get("parent_operation_id") == op_id)
            ]
            cursor = max((e.sequence for e in agent_events), default=after)
            role = op_record.get("role") or self._canonical_role(op_record.get("agent_id", "backend"))
            agent_id = op_record.get("agent_id") or role
            return {
                "agentId": agent_id,
                "agent_id": agent_id,
                "role": role,
                "operationId": op_id,
                "operation_id": op_id,
                "workOrderId": op_record.get("work_order_id"),
                "work_order_id": op_record.get("work_order_id"),
                "state": op_record.get("status"),
                "status": op_record.get("status"),
                "contractScope": op_record.get("contract_scope"),
                "contract_scope": op_record.get("contract_scope"),
                "parentOperationId": op_record.get("parent_operation_id"),
                "parent_operation_id": op_record.get("parent_operation_id"),
                "children": list(op_record.get("children", [])),
                "transitions": list(op_record.get("transitions", [])),
                "result": op_record.get("result"),
                "childResults": op_record.get("child_results", {}),
                "child_results": op_record.get("child_results", {}),
                "events": [e.as_dict() for e in agent_events],
                "cursor": cursor,
            }
