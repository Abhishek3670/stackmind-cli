"""Work-order dependency freshness and durable operation journaling.

Two responsibilities live here, both deliberately independent of the tool
gateway so they can be exercised directly:

1. **Dependency freshness.** The authoritative dependency model is the
   ``dependencies`` list on each work-order YAML record. When an upstream work
   order is amended or reopened, every work order that reaches it through that
   edge - directly or transitively - has its *current* evidence invalidated while
   its historical completion record is preserved verbatim.

2. **Recoverable operation journal.** A consequential work-order mutation
   records its intent (stable ``op_id``, expected revision, target revision,
   payload hash) durably *before* the authoritative write. The append-only JSONL
   audit is then written after the mutation. A pending journal record therefore
   means "either the mutation did not land, or it landed and the audit did not" -
   both are reconcilable after a restart, and neither is silently treated as
   applied.

What this is not: true multi-file atomic transactions. The work order file, the
INDEX/TREE copies, the audit log, and dependent records are separate files. The
protocol below bounds the window in which they can disagree and makes every such
window observable and repairable; it does not make the set atomic.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from validators.kernel.io import durable_write_text, durable_write_yaml

WORK_ORDER_BUCKETS = ("ACTIVE", "BLOCKED", "COMPLETED")
AUDIT_REL = Path(".sync") / "reports" / "work_order_amendments.jsonl"
JOURNAL_REL = Path(".sync") / "runtime" / "work-order-ops"

#: Bounds keep recovery cheap and observable even if a journal grows unbounded
#: through repeated crashes. The JSONL audit is the unbounded history.
MAX_JOURNAL_RECORDS = 500
JOURNAL_KEEP_FINALIZED = 200
MAX_AUDIT_SCAN_BYTES = 8 * 1024 * 1024

#: Fields whose change means a downstream implementation may need rework, not
#: merely re-verification.
CODE_AFFECTING_FIELDS = frozenset({
    "deliverable",
    "deliverables",
    "dependencies",
    "scope",
    "implementation_estimate",
    "requirement_refs",
    "constraints",
    "instructions",
    "gate_conditions",
    # Requirement statements, not verification metadata: a change here can
    # demand behaviour the dependent's existing tests never exercise, so
    # re-verification alone cannot establish compatibility.
    "description",
    "acceptance_criteria",
})

#: Evidence-invalidating fields whose change only alters which checks exist.
#: Re-running the declared checks is sufficient (revalidation), because the
#: runtime executes the test plan rather than trusting a caller's claim.
REVALIDATION_SAFE_FIELDS = frozenset({"test_plan"})

_RESOLVED_STATUSES = frozenset({"applied", "abandoned", "superseded", "rejected"})
_PENDING_STATUSES = frozenset({"pending"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(value: Any, limit: int = 500) -> str:
    text = str(value if value is not None else "")
    return text.replace("\r", " ").replace("\n", " ").strip()[:limit]


def _normalize_newlines(payload: bytes) -> bytes:
    # Text-mode writes translate \\n to \\r\\n on Windows; hashes must be
    # comparable across platforms and write paths.
    return payload.replace(b"\r\n", b"\n")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(_normalize_newlines(payload)).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


@dataclass(frozen=True)
class WorkOrderRef:
    wo_id: str
    bucket: str
    revision: int
    dependencies: tuple[str, ...]
    data: dict[str, Any]
    path: Path


@dataclass(frozen=True)
class Dependent:
    wo_id: str
    bucket: str
    depth: int
    via: str


# ── Work-order discovery ─────────────────────────────────────────────────


def _wo_path(root: Path, wo_id: str, bucket: str) -> Path:
    return Path(root) / ".sync" / "work-orders" / bucket / f"{wo_id}.yaml"


def load_work_order(root: Path, wo_id: str) -> WorkOrderRef | None:
    """Return the single on-disk record for ``wo_id`` from the canonical buckets."""
    for bucket in WORK_ORDER_BUCKETS:
        path = _wo_path(root, wo_id, bucket)
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        revision = data.get("revision", 1)
        if not isinstance(revision, int) or revision < 1:
            revision = 1
        deps = data.get("dependencies")
        dependencies = tuple(
            str(d).strip() for d in deps if str(d).strip()
        ) if isinstance(deps, list) else ()
        return WorkOrderRef(
            wo_id=wo_id,
            bucket=bucket,
            revision=revision,
            dependencies=dependencies,
            data=dict(data),
            path=path,
        )
    return None


def load_all_work_orders(root: Path) -> dict[str, WorkOrderRef]:
    """Deterministic snapshot of every work order across all buckets."""
    records: dict[str, WorkOrderRef] = {}
    seen: set[str] = set()
    for bucket in WORK_ORDER_BUCKETS:
        bucket_dir = Path(root) / ".sync" / "work-orders" / bucket
        if not bucket_dir.is_dir():
            continue
        for wo_file in sorted(bucket_dir.glob("*.yaml")):
            wo_id = wo_file.stem
            if wo_id in seen:
                continue
            ref = load_work_order(root, wo_id)
            if ref is None:
                continue
            seen.add(wo_id)
            records[wo_id] = ref
    return records


def dependency_graph(root: Path) -> dict[str, tuple[str, ...]]:
    """Reverse-indexed dependency graph using only the declared dependency model."""
    return {wo_id: ref.dependencies for wo_id, ref in load_all_work_orders(root).items()}


def transitive_dependents(
    root: Path, wo_id: str, *, max_nodes: int = 500,
) -> list[Dependent]:
    """Every work order that transitively depends on ``wo_id``.

    Traversal is breadth-first over reverse edges, with a deterministic
    ``(depth, id)`` ordering and cycle protection. ``via`` names the immediate
    upstream that first reached the dependent, which is what the audit records.
    """
    records = load_all_work_orders(root)
    reverse: dict[str, list[str]] = {}
    for dependent_id, ref in records.items():
        for dep in ref.dependencies:
            if dep in records and dep != dependent_id:
                reverse.setdefault(dep, []).append(dependent_id)
    for values in reverse.values():
        values.sort()

    results: list[Dependent] = []
    visited: set[str] = {wo_id}
    queue: list[tuple[str, int]] = [(wo_id, 0)]
    index = 0
    while index < len(queue) and len(results) < max_nodes:
        current, depth = queue[index]
        index += 1
        for dependent_id in reverse.get(current, ()):
            if dependent_id in visited:
                continue
            visited.add(dependent_id)
            ref = records[dependent_id]
            results.append(
                Dependent(
                    wo_id=dependent_id,
                    bucket=ref.bucket,
                    depth=depth + 1,
                    via=current,
                )
            )
            queue.append((dependent_id, depth + 1))
    results.sort(key=lambda item: (item.depth, item.wo_id))
    return results


# ── Evidence invalidation ────────────────────────────────────────────────


def _marker_sources(existing: Any) -> list[dict[str, Any]]:
    if not isinstance(existing, dict):
        return []
    raw = existing.get("sources")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _source_key(source: dict[str, Any]) -> tuple[str, int]:
    upstream = source.get("upstream_work_order")
    revision = source.get("upstream_revision")
    return (
        str(upstream) if upstream is not None else "",
        int(revision) if isinstance(revision, int) else 0,
    )


def mark_dependents_stale(
    root: Path,
    *,
    upstream_id: str,
    upstream_revision: int,
    reason: str,
    fields: Iterable[str],
    actor: str,
    upstream_contract_revision: int | None = None,
) -> dict[str, list[str]]:
    """Invalidate current evidence for every transitive dependent.

    Historical completion records are preserved untouched (``status``,
    ``completed_at`` and ``completion`` are never modified). Only the separate
    ``evidence_stale`` marker is written, and markers accumulate per upstream
    revision so repeated invalidations cannot erase earlier provenance.
    """
    changed_fields = sorted({str(f) for f in fields if str(f).strip()})
    requires_rework = bool(set(changed_fields) & CODE_AFFECTING_FIELDS)
    now = utc_now()
    marked: list[str] = []
    completed: list[str] = []
    failed: list[str] = []

    for dependent in transitive_dependents(root, upstream_id):
        target = _wo_path(root, dependent.wo_id, dependent.bucket)
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
        except Exception:
            failed.append(dependent.wo_id)
            continue
        if not isinstance(data, dict):
            failed.append(dependent.wo_id)
            continue

        existing = data.get("evidence_stale")
        sources = _marker_sources(existing)
        new_source = {
            "upstream_work_order": upstream_id,
            "upstream_revision": upstream_revision,
            "upstream_contract_revision": upstream_contract_revision,
            "via": dependent.via,
            "depth": dependent.depth,
            "fields": changed_fields,
            "reason": _clip(reason),
            "invalidated_at": now,
            "requires_rework": requires_rework,
        }
        key = _source_key(new_source)
        sources = [source for source in sources if _source_key(source) != key]
        sources.append(new_source)

        merged_fields: list[str] = []
        for source in sources:
            for field in source.get("fields") or []:
                token = f"upstream:{field}"
                if token not in merged_fields:
                    merged_fields.append(token)
        dependency_token = f"dependency:{upstream_id}"
        if dependency_token not in merged_fields:
            merged_fields.insert(0, dependency_token)

        revision = data.get("revision", 1)
        if not isinstance(revision, int) or revision < 1:
            revision = 1
        data["evidence_stale"] = {
            "invalidated_at": now,
            "invalidated_by": actor,
            "revision": revision,
            "reason": (
                f"upstream {upstream_id} r{upstream_revision} changed: "
                "dependencies/acceptance assumptions invalidated"
            ),
            "fields": merged_fields,
            "dependents": (
                list(existing.get("dependents") or [])
                if isinstance(existing, dict) and isinstance(existing.get("dependents"), list)
                else []
            ),
            "sources": sources,
            "requires_revalidation": True,
            "requires_rework": requires_rework
            or any(bool(source.get("requires_rework")) for source in sources),
        }
        try:
            durable_write_yaml(target, data, default_flow_style=False)
        except Exception:
            failed.append(dependent.wo_id)
            continue
        marked.append(dependent.wo_id)
        if dependent.bucket == "COMPLETED":
            completed.append(dependent.wo_id)
    return {"marked": marked, "completed": completed, "failed": failed}


def contract_revision(root: Path, wo_id: str) -> int | None:
    """Contract revision currently in force for a work order, if one exists."""
    contract_file = Path(root) / ".sync" / "contracts" / f"{wo_id}.yaml"
    if not contract_file.is_file():
        return None
    try:
        data = yaml.safe_load(contract_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    revision = data.get("contract_revision") if isinstance(data, dict) else None
    return revision if isinstance(revision, int) else None


def stale_sources_resolved(root: Path, stale: Any) -> tuple[bool, str]:
    """Whether every recorded upstream invalidation is still current.

    A source is resolved when the upstream exists and is still at the revision
    that caused the invalidation. If the upstream has advanced again, the
    dependent is stale for a newer reason and may not be cleared. Unknown or
    unreadable upstreams fail closed.
    """
    if not isinstance(stale, dict) or not stale:
        return True, ""
    sources = _marker_sources(stale)
    # Only the newest recorded revision per upstream is binding: a later
    # invalidation from the same upstream supersedes the earlier one, but the
    # earlier record is still kept for audit.
    latest: dict[str, int] = {}
    for source in sources:
        upstream_id = str(source.get("upstream_work_order") or "")
        recorded = source.get("upstream_revision")
        if not upstream_id or not isinstance(recorded, int):
            return False, "invalidation source is malformed"
        latest[upstream_id] = max(recorded, latest.get(upstream_id, recorded))
    for upstream_id, recorded in sorted(latest.items()):
        ref = load_work_order(root, upstream_id)
        if ref is None:
            return False, f"upstream {upstream_id} no longer exists"
        if ref.revision != recorded:
            return (
                False,
                f"upstream {upstream_id} advanced from r{recorded} to r{ref.revision}",
            )
        recorded_contract = None
        for source in sources:
            if (
                str(source.get("upstream_work_order")) == upstream_id
                and source.get("upstream_revision") == recorded
            ):
                recorded_contract = source.get("upstream_contract_revision")
        if isinstance(recorded_contract, int):
            current_contract = contract_revision(root, upstream_id)
            if current_contract != recorded_contract:
                return (
                    False,
                    f"upstream {upstream_id} contract revision changed from "
                    f"{recorded_contract} to {current_contract}",
                )
    return True, ""


def stale_requires_rework(stale: Any) -> bool:
    if not isinstance(stale, dict):
        return False
    if stale.get("requires_rework"):
        return True
    return any(bool(source.get("requires_rework")) for source in _marker_sources(stale))


def stale_sources(stale: Any) -> list[dict[str, Any]]:
    return _marker_sources(stale)


# ── Operation journal ────────────────────────────────────────────────────


def journal_dir(root: Path) -> Path:
    return Path(root) / JOURNAL_REL


def new_operation_id() -> str:
    return f"woop-{uuid.uuid4().hex}"


def _journal_path(root: Path, op_id: str) -> Path:
    return journal_dir(root) / f"{op_id}.json"


def record_pending(
    root: Path,
    *,
    kind: str,
    work_order: str,
    expected_revision: int,
    target_revision: int,
    reason: str,
    actor: str,
    changed_fields: Iterable[str] = (),
    intended_dependents: Iterable[str] = (),
    previous_sha256: str | None = None,
    expected_sha256: str | None = None,
    session_id: str | None = None,
    attempt_id: str | None = None,
    op_id: str | None = None,
) -> dict[str, Any]:
    """Durably record intent *before* the authoritative mutation.

    Raises ``OSError`` if the record cannot be persisted; callers must treat that
    as a hard failure before touching the work order.
    """
    record = {
        "op_id": op_id or new_operation_id(),
        "kind": kind,
        "work_order": work_order,
        "expected_revision": expected_revision,
        "target_revision": target_revision,
        "reason": _clip(reason),
        "actor": actor,
        "session_id": session_id,
        "attempt_id": attempt_id,
        "changed_fields": sorted({str(f) for f in changed_fields if str(f).strip()}),
        "intended_dependents": list(dict.fromkeys(intended_dependents)),
        "previous_sha256": previous_sha256,
        "expected_sha256": expected_sha256,
        "status": "pending",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    directory = journal_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    durable_write_text(
        _journal_path(root, record["op_id"]),
        json.dumps(record, indent=2, sort_keys=True) + "\n",
    )
    return record


def load_operations(root: Path, status: str | None = None) -> list[dict[str, Any]]:
    """Bounded, deterministic read of journal records.

    Unreadable records are surfaced with ``status: "unreadable"`` rather than
    skipped, so a corrupt journal blocks consequential mutation instead of
    silently disappearing.
    """
    directory = journal_dir(root)
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("woop-*.json"))
    if len(files) > MAX_JOURNAL_RECORDS:
        files = files[-MAX_JOURNAL_RECORDS:]
    records: list[dict[str, Any]] = []
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            record = {"op_id": path.stem, "status": "unreadable"}
        if not isinstance(record, dict):
            record = {"op_id": path.stem, "status": "unreadable"}
        record.setdefault("op_id", path.stem)
        record.setdefault("status", "pending")
        if status is None or record.get("status") == status:
            records.append(record)
    records.sort(key=lambda r: (str(r.get("created_at") or ""), str(r["op_id"])))
    return records


def finalize_operation(
    root: Path, op_id: str, status: str, **extra: Any,
) -> bool:
    """Atomically move a journal record to a terminal status."""
    path = _journal_path(root, op_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(record, dict):
        return False
    record.update(extra)
    record["status"] = status
    record["updated_at"] = utc_now()
    try:
        durable_write_text(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def append_audit(
    root: Path,
    entry: dict[str, Any],
    *,
    actor: str | None = None,
    session_id: str | None = None,
    attempt_id: str | None = None,
    dedupe: bool = True,
) -> bool:
    """Append one audit record, idempotently when ``op_id`` is present.

    The JSONL log is never truncated or rewritten; the bounded ``amendments``
    list inside the work-order YAML is a rendering convenience only.
    """
    payload = {
        "timestamp": utc_now(),
        "actor": actor,
        "session_id": session_id,
        "attempt_id": attempt_id,
        **entry,
    }
    op_id = payload.get("op_id")
    outcome = payload.get("outcome")
    audit_path = Path(root) / AUDIT_REL
    if dedupe and op_id and _audit_has_entry(audit_path, str(op_id), str(outcome)):
        return True
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError:
        return False


def _audit_has_entry(audit_path: Path, op_id: str, outcome: str) -> bool:
    if not audit_path.is_file():
        return False
    try:
        size = audit_path.stat().st_size
        with audit_path.open("rb") as handle:
            if size > MAX_AUDIT_SCAN_BYTES:
                handle.seek(size - MAX_AUDIT_SCAN_BYTES)
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    for line in data.splitlines():
        if op_id not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("op_id") == op_id and record.get("outcome") == outcome:
            return True
    return False


def read_audit(root: Path) -> list[dict[str, Any]]:
    audit_path = Path(root) / AUDIT_REL
    if not audit_path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in audit_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def prune_journal(root: Path, keep_finalized: int = JOURNAL_KEEP_FINALIZED) -> int:
    """Delete the oldest finalized records; pending and unreadable stay forever."""
    records = load_operations(root)
    finalized = [
        record for record in records
        if record.get("status") in _RESOLVED_STATUSES
    ]
    if len(finalized) <= keep_finalized:
        return 0
    removed = 0
    for record in finalized[: len(finalized) - keep_finalized]:
        try:
            _journal_path(root, str(record["op_id"])).unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def _current_sha(path: Path) -> str | None:
    return sha256_file(path)


def reconcile_operations(
    root: Path,
    *,
    apply_stale: Callable[[dict[str, Any]], bool] | None = None,
    prune: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Resolve pending journal records without ever applying a mutation twice.

    Outcomes:

    * ``recovered`` - the mutation is on disk (payload hash matches) but the
      audit append never completed. The audit is written, then the record is
      finalized as ``applied``.
    * ``abandoned`` - the work order is unchanged at the expected revision, so
      the mutation never landed. Nothing is rolled back; the intent is audited.
    * ``superseded`` - a newer revision exists. Recovery never overwrites it.
    * unresolved - state is ambiguous (missing file, unexpected hash, or a
      partially applied dependency invalidation with no repair callable). The
      record stays ``pending`` and blocks consequential mutation and release.
    """
    summary: dict[str, list[dict[str, Any]]] = {
        "recovered": [],
        "abandoned": [],
        "superseded": [],
        "unresolved": [],
    }
    for record in load_operations(root, status="pending") + [
        item for item in load_operations(root) if item.get("status") == "unreadable"
    ]:
        op_id = str(record.get("op_id") or "")
        if record.get("status") == "unreadable":
            summary["unresolved"].append({
                "op_id": op_id,
                "reason": "journal record is unreadable",
            })
            continue
        wo_id = str(record.get("work_order") or "")
        ref = load_work_order(root, wo_id)
        if ref is None:
            summary["unresolved"].append({
                "op_id": op_id,
                "work_order": wo_id,
                "reason": "work order not found in ACTIVE/BLOCKED/COMPLETED",
            })
            continue

        current_sha = _current_sha(_wo_path(root, wo_id, ref.bucket))
        expected_sha = record.get("expected_sha256")
        previous_sha = record.get("previous_sha256")
        target = record.get("target_revision")
        expected = record.get("expected_revision")

        if expected_sha and current_sha == expected_sha:
            pending_dependents = [
                dependent_id
                for dependent_id in record.get("intended_dependents") or []
                if _dependent_needs_invalidation(root, dependent_id, wo_id, target)
            ]
            if pending_dependents:
                if apply_stale is None:
                    summary["unresolved"].append({
                        "op_id": op_id,
                        "work_order": wo_id,
                        "reason": (
                            "dependent evidence invalidation is incomplete for "
                            + ", ".join(sorted(pending_dependents))
                        ),
                    })
                    continue
                if not apply_stale(record):
                    summary["unresolved"].append({
                        "op_id": op_id,
                        "work_order": wo_id,
                        "reason": "dependent evidence invalidation could not be repaired",
                    })
                    continue
            ok = append_audit(
                root,
                {
                    "op_id": op_id,
                    "work_order": wo_id,
                    "outcome": "recovered",
                    "kind": record.get("kind"),
                    "expected_revision": expected,
                    "previous_revision": expected,
                    "revision": ref.revision,
                    "reason": record.get("reason"),
                    "changed_fields": record.get("changed_fields") or [],
                    "dependents": record.get("intended_dependents") or [],
                    "recovery": "mutation was durable; audit was completed by reconciliation",
                },
                actor=record.get("actor"),
                session_id=record.get("session_id"),
                attempt_id=record.get("attempt_id"),
            )
            if not ok:
                summary["unresolved"].append({
                    "op_id": op_id,
                    "work_order": wo_id,
                    "reason": "audit append failed during recovery",
                })
                continue
            finalize_operation(root, op_id, "applied", recovered=True)
            summary["recovered"].append({
                "op_id": op_id, "work_order": wo_id, "revision": ref.revision,
            })
            continue

        if (
            isinstance(target, int) and isinstance(expected, int) and ref.revision > target
        ):
            append_audit(
                root,
                {
                    "op_id": op_id,
                    "work_order": wo_id,
                    "outcome": "superseded",
                    "kind": record.get("kind"),
                    "expected_revision": expected,
                    "revision": ref.revision,
                    "reason": record.get("reason"),
                    "changed_fields": record.get("changed_fields") or [],
                    "recovery": "a newer revision exists; recovery did not overwrite it",
                },
                actor=record.get("actor"),
                session_id=record.get("session_id"),
                attempt_id=record.get("attempt_id"),
            )
            finalize_operation(root, op_id, "superseded")
            summary["superseded"].append({
                "op_id": op_id, "work_order": wo_id, "revision": ref.revision,
            })
            continue

        if (
            previous_sha
            and current_sha == previous_sha
            and isinstance(expected, int)
            and ref.revision == expected
        ):
            append_audit(
                root,
                {
                    "op_id": op_id,
                    "work_order": wo_id,
                    "outcome": "abandoned",
                    "kind": record.get("kind"),
                    "expected_revision": expected,
                    "revision": ref.revision,
                    "reason": record.get("reason"),
                    "changed_fields": record.get("changed_fields") or [],
                    "recovery": "interrupted before the mutation landed; nothing to roll back",
                },
                actor=record.get("actor"),
                session_id=record.get("session_id"),
                attempt_id=record.get("attempt_id"),
            )
            finalize_operation(root, op_id, "abandoned")
            summary["abandoned"].append({
                "op_id": op_id, "work_order": wo_id, "revision": ref.revision,
            })
            continue

        summary["unresolved"].append({
            "op_id": op_id,
            "work_order": wo_id,
            "reason": (
                "state is ambiguous: revision r"
                f"{ref.revision} matches neither the expected r{expected} nor the "
                f"target r{target} payload"
            ),
        })

    if prune:
        prune_journal(root)
    return summary


def _dependent_needs_invalidation(
    root: Path, dependent_id: str, upstream_id: str, upstream_revision: Any,
) -> bool:
    """True when the dependent has no marker for this upstream revision yet."""
    ref = load_work_order(root, dependent_id)
    if ref is None:
        return False
    stale = ref.data.get("evidence_stale")
    if not isinstance(stale, dict):
        return True
    for source in _marker_sources(stale):
        if (
            str(source.get("upstream_work_order")) == upstream_id
            and source.get("upstream_revision") == upstream_revision
        ):
            return False
    return True


def unresolved_operations(root: Path) -> list[dict[str, Any]]:
    """Pending or unreadable records after a reconciliation pass."""
    reconcile_operations(root, apply_stale=None, prune=False)
    blocking: list[dict[str, Any]] = []
    for record in load_operations(root):
        status = record.get("status")
        if status in _PENDING_STATUSES or status == "unreadable":
            blocking.append(record)
    return blocking


def blocking_operation_for(root: Path, wo_id: str) -> dict[str, Any] | None:
    for record in unresolved_operations(root):
        if record.get("work_order") == wo_id or record.get("status") == "unreadable":
            return record
    return None


__all__ = [
    "AUDIT_REL",
    "CODE_AFFECTING_FIELDS",
    "Dependent",
    "JOURNAL_REL",
    "WORK_ORDER_BUCKETS",
    "WorkOrderRef",
    "append_audit",
    "blocking_operation_for",
    "contract_revision",
    "dependency_graph",
    "finalize_operation",
    "journal_dir",
    "load_all_work_orders",
    "load_operations",
    "load_work_order",
    "mark_dependents_stale",
    "new_operation_id",
    "prune_journal",
    "read_audit",
    "record_pending",
    "REVALIDATION_SAFE_FIELDS",
    "reconcile_operations",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "stale_requires_rework",
    "stale_sources",
    "stale_sources_resolved",
    "transitive_dependents",
    "unresolved_operations",
    "utc_now",
]
