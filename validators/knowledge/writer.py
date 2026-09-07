"""Atomic writer for deterministic knowledge artifacts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cli.lock import acquire_lock, release_lock
from validators.knowledge.compiler.ir import CompilerIR
from validators.knowledge.registry import SymbolRegistry

from .storage import (
    build_node_documents,
    build_revision_document,
    canonical_json,
    knowledge_root,
    latest_revision_id,
    node_path,
    revision_path,
)


@dataclass(frozen=True)
class KnowledgeWriteResult:
    revision_id: int
    revision_path: Path
    written_paths: tuple[Path, ...]
    unchanged_paths: tuple[Path, ...]


def _write_if_changed(path: Path, document: dict) -> bool:
    payload = canonical_json(document)
    if path.exists() and path.read_text(encoding="utf-8").replace("\r\n", "\n") == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


class KnowledgeWriter:
    def __init__(self, project_path: Path, agent: str = "codex") -> None:
        self.project_path = project_path.resolve()
        self.agent = agent

    def write(self, ir: CompilerIR, *, built_at: str = "") -> KnowledgeWriteResult:
        sync_path = self.project_path / ".sync"
        external = not (sync_path / "runtime").exists()

        lock_acquired = False
        if not external:
            ok, message = acquire_lock(sync_path, self.agent, session_id="storage")
            if not ok:
                raise RuntimeError(message)
            lock_acquired = True

        try:
            written: list[Path] = []
            unchanged: list[Path] = []

            # Collect current IR node IDs for reconciliation
            current_node_ids = {symbol.node_id for symbol in ir.symbols}

            for node_id, document in sorted(build_node_documents(ir).items()):
                path = node_path(self.project_path, document["kind"], node_id)
                (written if _write_if_changed(path, document) else unchanged).append(path)

            # Reconciliation: prune ghost nodes not in current IR
            self._reconcile_ghosts(current_node_ids, ir, written)

            parent = latest_revision_id(self.project_path)
            revision_id = parent + 1
            path = revision_path(self.project_path, revision_id)
            _write_if_changed(
                path, build_revision_document(ir, revision_id, parent or None, built_at)
            )
            written.append(path)
            return KnowledgeWriteResult(revision_id, path, tuple(written), tuple(unchanged))
        finally:
            if lock_acquired:
                release_lock(sync_path, self.agent)

    def _reconcile_ghosts(
        self,
        current_node_ids: set[str],
        ir: CompilerIR,
        written: list[Path],
    ) -> None:
        """Remove node files for symbols no longer in IR and mark them obsolete in registry.

        Uses rename detection: vanished symbols that match an appeared symbol
        (by kind + content_hash + owner + signature) get aliased instead of deleted.
        """
        from validators.knowledge.compiler.rename import (
            AppearedSymbol,
            HistoricalSymbol,
            detect_renames,
        )

        nodes_dir = knowledge_root(self.project_path) / "nodes"
        if not nodes_dir.exists():
            return

        # Find all existing node IDs on disk
        existing_node_ids: dict[str, Path] = {}
        for node_file in nodes_dir.glob("*/*/*.json"):
            node_id = node_file.stem
            existing_node_ids[node_id] = node_file

        # Identify vanished: on disk but not in current IR
        vanished_ids = set(existing_node_ids.keys()) - current_node_ids
        if not vanished_ids:
            return

        # Load vanished symbols for rename detection
        vanished_symbols: list[HistoricalSymbol] = []
        for node_id in sorted(vanished_ids):
            node_file = existing_node_ids[node_id]
            try:
                import json
                node = json.loads(node_file.read_text(encoding="utf-8"))
                det = node.get("deterministic", {})
                vanished_symbols.append(HistoricalSymbol(
                    node_id=node_id,
                    kind=node.get("kind", ""),
                    path=det.get("path", ""),
                    qualified_name=det.get("qualified_name", ""),
                    signature=det.get("signature", ""),
                    content_hash=det.get("content_hash", ""),
                    owner_qualified_name=det.get("owner"),
                ))
            except (OSError, ValueError, KeyError):
                # Can't load — just delete
                vanished_symbols.append(HistoricalSymbol(
                    node_id=node_id, kind="", path="", qualified_name="",
                    signature="", content_hash="", owner_qualified_name=None,
                ))

        # Build appeared symbols (new in IR, not previously on disk)
        appeared_ids = current_node_ids - set(existing_node_ids.keys())
        appeared_symbols: list[AppearedSymbol] = []
        for symbol in ir.symbols:
            if symbol.node_id in appeared_ids:
                appeared_symbols.append(AppearedSymbol(
                    kind=symbol.kind,
                    path=symbol.path,
                    qualified_name=symbol.qualified_name,
                    signature=symbol.signature,
                    content_hash=symbol.content_hash,
                    owner_qualified_name=symbol.owner,
                ))

        # Run rename detection
        renames, _ = detect_renames(vanished_symbols, appeared_symbols)
        renamed_node_ids = {match.node_id for match in renames}

        # Apply renames as aliases in registry
        registry = SymbolRegistry(self.project_path, agent=self.agent)
        rev = latest_revision_id(self.project_path) + 1
        for match in renames:
            try:
                registry.alias(
                    match.node_id,
                    path=match.new_path,
                    qualified_name=match.new_qualified_name,
                    rev=rev,
                )
            except (KeyError, OSError):
                pass

        # Delete ghost nodes (vanished and not renamed)
        for node_id in sorted(vanished_ids):
            if node_id in renamed_node_ids:
                continue
            node_file = existing_node_ids[node_id]
            if node_file.exists():
                node_file.unlink()
                written.append(node_file)
            # Mark obsolete in registry
            try:
                registry.mark_obsolete(node_id, rev=rev)
            except (KeyError, OSError):
                pass


def write_knowledge(
    project_path: Path, ir: CompilerIR, *, agent: str = "codex", built_at: str = ""
) -> KnowledgeWriteResult:
    return KnowledgeWriter(project_path, agent).write(ir, built_at=built_at)
