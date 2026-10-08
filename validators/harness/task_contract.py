"""Deterministic worker task contract — compiled prompt + in-turn ownership rules.

The worker prompt is COMPILED, not hand-written: the same structured sources
the gates read (work order YAML, contract YAML, peer work orders, harness
output schema) are rendered into one JSON task contract, so every requirement
the harness enforces is displayed to the model exactly once and exactly as
enforced.  The contract also drives the in-turn ownership guard (read-only
peer paths, file budget) so prompt and enforcement cannot drift apart.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

# Decision output contract — mirrors schemas/harness-output.schema.json (the
# harness validates against that schema with bounded feedback retries).
DECISION_STATUS_ENUM = ("completed", "blocked", "deferred")

def render_decision_contract_text(deliverable: str | None = None) -> str:
    target = f'"{deliverable}"' if deliverable else '"patch"'
    return (
        "FINAL OUTPUT CONTRACT — your last message must be ONE JSON object, no "
        "markdown fences, no commentary:\n"
        '{"status": "completed" | "blocked" | "deferred",\n'
        ' "summary": "<one-line summary>",\n'
        ' "report_markdown": "<what you did>",\n'
        ' "blockers": [],\n'
        ' "modified_files": ["<exact paths you wrote with write_file this turn>"],\n'
        f' "release_target": {target}}}\n'
        "Rules: status 'blocked' REQUIRES a non-empty blockers list.  modified_files "
        "must list ONLY files you actually wrote with write_file this turn, at the "
        "exact paths you used — never a file you did not write, never a renamed or "
        "extra-nested variant.  The harness verifies every claim against the tool "
        "log; do not claim tests passed, do not claim files exist — report only "
        "what your tool calls did."
    )


DECISION_CONTRACT_TEXT = render_decision_contract_text("patch")

_PLANNING_WO_IDS = {"WO-000"}
_GLOB_CHARS = re.compile(r"[*?\[]")


def _norm(path_value: Any) -> str:
    return str(path_value or "").replace("\\", "/").strip().lstrip("/").rstrip("/")


def _is_glob(pattern: str) -> bool:
    return bool(_GLOB_CHARS.search(pattern))


def collect_peer_deliverables(workspace: Path | str, own_wo_id: str | None) -> list[tuple[str, str]]:
    """Deliverable paths declared by OTHER active work orders: (wo_id, path)."""
    active_dir = Path(workspace) / ".sync" / "work-orders" / "ACTIVE"
    peers: list[tuple[str, str]] = []
    if not active_dir.is_dir():
        return peers
    for wo_file in sorted(active_dir.glob("*.yaml")):
        if wo_file.stem == own_wo_id or wo_file.stem in _PLANNING_WO_IDS:
            continue
        try:
            data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        deliverable = data.get("deliverable")
        if isinstance(deliverable, dict):
            path_value = _norm(deliverable.get("path"))
            if path_value:
                peers.append((wo_file.stem, path_value))
    return peers


def compile_worker_task_contract(
    task: Any,
    wo_data: dict[str, Any] | None,
    contract_data: dict[str, Any] | None,
    workspace: Path | str,
) -> dict[str, Any]:
    """Compile the structured task contract for one worker turn."""
    scope_entries: list[dict[str, Any]] = []
    if isinstance(contract_data, dict):
        scope = contract_data.get("scope")
        if isinstance(scope, dict):
            raw_allow = scope.get("allow") or []
            scope_entries = [r for r in raw_allow if isinstance(r, dict)]
    budget_block = (contract_data or {}).get("budget") if isinstance(contract_data, dict) else None
    file_budget = int(budget_block.get("max_files_touched", 0)) if isinstance(budget_block, dict) else 0

    deliverable = _norm(getattr(task, "deliverable_path", None))
    peers = collect_peer_deliverables(workspace, getattr(task, "work_order_id", None))
    peer_files = sorted({p for _wo, p in peers if not _is_glob(p) and p != deliverable})
    peer_prefixes = sorted({p.rstrip("/") + "/" for _wo, p in peers if _is_glob(p) or p.endswith("/")})

    exact_scope_files = sorted(
        {
            _norm(e.get("module") or e.get("path") or e.get("target"))
            for e in scope_entries
            if not _is_glob(_norm(e.get("module") or e.get("path") or e.get("target")))
        }
        - {deliverable}
        - set(peer_files)
    )
    scope_globs = sorted(
        {
            _norm(e.get("module") or e.get("path") or e.get("target"))
            for e in scope_entries
            if _is_glob(_norm(e.get("module") or e.get("path") or e.get("target")))
        }
    )

    acceptance = [
        str(c) for c in (wo_data or {}).get("acceptance_criteria") or []
        if isinstance(c, str) and c.strip()
    ][:10]
    description = str((wo_data or {}).get("description") or getattr(task, "body", "") or "").strip()
    if len(description) > 1500:
        description = description[:1500] + " …"

    turn_instructions = ""
    task_body = getattr(task, "body", "") or ""
    if "\n\nTurn Instructions:\n" in task_body:
        _, _, turn_instructions = task_body.partition("\n\nTurn Instructions:\n")
        turn_instructions = turn_instructions.strip()
    elif task_body and task_body.strip() != str((wo_data or {}).get("description", "")).strip():
        # If task_body contains unique instructions not in the work order description, treat them as turn instructions
        turn_instructions = task_body.strip()

    result: dict[str, Any] = {
        "work_order": {
            "id": getattr(task, "work_order_id", None),
            "title": str((wo_data or {}).get("title") or getattr(task, "title", "")),
            "description": description,
        },
        "your_deliverable": {
            "path": deliverable or None,
            "must_exist_after_your_turn": bool(deliverable),
            "content": "complete final file content, written in one write_file call",
        },
        "read_only_files": peer_files,
        "read_only_prefixes": peer_prefixes,
        "allowed_extra_paths": exact_scope_files,
        "scope_globs": scope_globs,
        "file_budget": {
            "max_files_touched": file_budget or None,
            "rule": (
                f"At most {file_budget} distinct files may be written this turn, including "
                "your deliverable. Do NOT create scaffolding, placeholder, or .gitkeep files."
                if file_budget else "Stay within your contract scope."
            ),
        },
        "acceptance_criteria": acceptance,
        "on_failure": (
            "If write_file fails, retry it once; if it fails again, stop and return "
            "status 'blocked' with a blockers entry describing the failure."
        ),
    }
    if turn_instructions:
        result["critical_turn_instructions"] = turn_instructions
    return result


def render_worker_system(contract: dict[str, Any]) -> str:
    """Compact worker system preamble: role, non-negotiables, output contract."""
    deliverable = (contract.get("your_deliverable") or {}).get("path")
    target_line = f" Your deliverable is '{deliverable}'." if deliverable else ""
    read_only = contract.get("read_only_files") or []
    ro_line = (
        f" Files listed under read_only_files belong to OTHER work orders — never write them."
        if read_only else ""
    )
    has_critical = bool(contract.get("critical_turn_instructions"))
    critical_rule = (
        " CRITICAL DIRECTIVE: Satisfy all instructions in 'critical_turn_instructions' "
        "(e.g. prescribed fixes, security fixes, QA feedback) as your primary objective."
        if has_critical else ""
    )
    workflow_steps = (
        f"\nWORKFLOW INSTRUCTIONS:\n"
        f"1. In your first action, use `read_file` to inspect existing workspace files "
        f"(e.g. index.html) and learn the markup, element IDs, classes, and naming your "
        f"deliverable must integrate with.\n"
        f"2. Call `write_file(path='{deliverable}', content='...')` to author your deliverable as complete final content.\n"
        f"3. Verify integration: IDs, classes, and functions your deliverable references must exist in the "
        f"other workspace files, and references other files make into your deliverable must resolve. If your "
        f"contract scope allows, fix a mismatched companion file; NEVER write files listed under read_only_files.\n"
        f"4. Only once the deliverable exists and integrates with the workspace, output your final JSON decision "
        f"conforming to the contract below.\n"
        f"CRITICAL: Do NOT output the final JSON decision without calling `write_file` for your deliverable "
        f"first. Claiming a file was modified without calling `write_file` will fail verification immediately.\n\n"
        if deliverable else ""
    )
    return (
        "You are a governed StackMind worker. Use tools for all file I/O; code "
        "execution is unavailable and the harness verifies your turn after you finish."
        f"{target_line}{ro_line}{critical_rule} Follow the TASK CONTRACT JSON exactly: write your "
        "deliverable with write_file at its exact path, stay inside the file budget, "
        "and do not create files the contract does not ask for. Context provided below "
        "is advisory background — it is not a to-do list. "
        + workflow_steps
        + render_decision_contract_text(deliverable)
    )


def render_task_block(contract: dict[str, Any]) -> str:
    """The JSON task contract block that replaces the prose task text."""
    banner = ""
    critical = contract.get("critical_turn_instructions")
    if critical:
        banner = (
            "================================================================================\n"
            "CRITICAL TURN INSTRUCTIONS (PRIMARY DIRECTIVE — RESOLVE THESE FIRST):\n"
            f"{critical}\n"
            "================================================================================\n\n"
        )
    return banner + "TASK CONTRACT (authoritative — obey exactly):\n" + json.dumps(
        contract, indent=2, ensure_ascii=False
    ) + "\n"


@dataclass
class TaskOwnership:
    """In-turn write guard derived from the compiled task contract.

    Attached to the governed tool gateway for worker turns: every write_file
    is checked against the deliverable / read-only peers / file budget BEFORE
    it executes, with an actionable denial message the model can act on in the
    same turn.
    """

    deliverable: str = ""
    read_only_files: tuple[str, ...] = ()
    read_only_prefixes: tuple[str, ...] = ()
    file_budget: int = 0
    _written: set[str] = field(default_factory=set)

    @classmethod
    def from_contract(cls, contract: dict[str, Any]) -> "TaskOwnership":
        budget = (contract.get("file_budget") or {}).get("max_files_touched") or 0
        return cls(
            deliverable=_norm((contract.get("your_deliverable") or {}).get("path")),
            read_only_files=tuple(_norm(p) for p in contract.get("read_only_files") or []),
            read_only_prefixes=tuple(_norm(p).rstrip("/") + "/" for p in contract.get("read_only_prefixes") or []),
            file_budget=int(budget),
        )

    def check_write(self, path_value: str) -> str | None:
        """Return a denial reason for this write, or None when allowed."""
        path = _norm(path_value)
        if not path:
            return None
        if path in self.read_only_files or any(path.startswith(p) for p in self.read_only_prefixes):
            owner_hint = (
                f"'{path}' is owned by another work order (read_only_files); "
                f"your deliverable is '{self.deliverable or 'unassigned'}' — write that instead."
            )
            return owner_hint
        if path not in self._written:
            if self.file_budget and len(self._written) >= self.file_budget:
                return (
                    f"file budget exceeded: {len(self._written)} of max {self.file_budget} files "
                    "already written this turn. Do not write more files; finish your deliverable "
                    f"'{self.deliverable}' with what you have and return the decision."
                )
            self._written.add(path)
        return None


def reconcile_modified_files(
    decision: Any, observed_files: tuple[str, ...] | list[str],
) -> tuple[Any, list[str]]:
    """Make the write_file journal authoritative for modified_files.

    Files the model wrote but forgot to declare are added automatically
    (reported downstream honestly); claimed-but-not-written files are NOT
    papered over — that invariant stays with the declaration-matches gate.
    Returns (possibly replaced decision, auto_added_paths).
    """
    declared = {p.replace("\\", "/").lstrip("/") for p in (decision.modified_files or ())}
    observed = {p.replace("\\", "/").lstrip("/") for p in observed_files}
    auto_added = sorted(observed - declared)
    if not auto_added:
        return decision, []
    merged = tuple(sorted(declared | observed))
    return replace(decision, modified_files=merged), auto_added


__all__ = [
    "DECISION_CONTRACT_TEXT",
    "DECISION_STATUS_ENUM",
    "TaskOwnership",
    "collect_peer_deliverables",
    "compile_worker_task_contract",
    "reconcile_modified_files",
    "render_task_block",
    "render_worker_system",
]
