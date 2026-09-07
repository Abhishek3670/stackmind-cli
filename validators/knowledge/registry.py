"""Canonical symbol registry for StackMind knowledge identity.

The registry mints permanent NodeIDs from a symbol's first-seen birth key and
stores one deterministic JSON record per symbol under sharded hash buckets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli.lock import read_lock

REGISTRY_REL = Path(".sync") / "knowledge" / "registry"
NODE_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9a-f]{16}$")

KIND_PREFIXES = {
    "module": "MOD",
    "package": "PKG",
    "class": "CLASS",
    "djangomiddleware": "DJMID",
    "djangomodelfield": "DJFIELD",
    "djangomodelmeta": "DJMETA",
    "djangoserializer": "DJSER",
    "djangosignalreceiver": "DJSIG",
    "djangourlpattern": "DJURL",
    "djangoviewset": "DJVIEW",
    "function": "FUNC",
    "method": "METH",
    "fastapiauth": "FAPIAUTH",
    "fastapidependency": "FAPIDEP",
    "fastapimiddleware": "FAPIMID",
    "fastapiroute": "FAPIROUTE",
    "pydanticconfig": "PYCONF",
    "pydanticfield": "PYFIELD",
    "pydanticvalidator": "PYVALID",
    "sqlalchemyassociationtable": "SATABLE",
    "sqlalchemycolumn": "SACOL",
    "sqlalchemyrelationship": "SAREL",
    "sqlalchemyrepository": "SAREPO",
    "sqlalchemysession": "SASESS",
    "variable": "VAR",
    "constant": "CONST",
    "workorder": "WO",
    "decision": "DEC",
    "review": "REV",
    "issue": "ISSUE",
    "experience": "EXP",
    "skill": "SKILL",
}


class RegistryLockError(RuntimeError):
    """Raised when a registry write is attempted without the write lock."""


def birth_key(path: str, qualified_name: str) -> str:
    """Return the canonical birth key for a repo-relative symbol path/name."""
    return f"{_normalize_rel_path(path)}:{qualified_name}"


def kind_prefix(kind: str) -> str:
    """Return the NodeID type prefix for a symbol kind."""
    normalized = re.sub(r"[^A-Za-z0-9]+", "", kind).lower()
    return KIND_PREFIXES.get(normalized, normalized.upper())


def birth_digest(key: str) -> str:
    """Return the full SHA-256 hex digest for a birth key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def node_id_for(kind: str, key: str) -> str:
    """Mint the deterministic NodeID for a symbol kind and birth key."""
    return f"{kind_prefix(kind)}-{birth_digest(key)[:16]}"


def _normalize_rel_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


@dataclass(frozen=True)
class RegistryWrite:
    """Result of a registry write operation."""

    node_id: str
    path: Path
    record: dict[str, Any]


class SymbolRegistry:
    """Sharded, write-locked symbol registry."""

    def __init__(self, project_path: Path, agent: str = "codex") -> None:
        self.project_path = project_path.resolve()
        self.sync_path = self.project_path / ".sync"
        self.registry_path = self.sync_path / "knowledge" / "registry"
        self.agent = agent
        self._cached_records = None
        self._cached_birth_key_index = None
        self._cached_node_id_index = None

    def shard_for(self, node_id: str) -> str:
        """Return the 2-hex bucket for a NodeID."""
        return node_id.split("-", 1)[1][:2]

    def path_for(self, node_id: str) -> Path:
        """Return the canonical shard path for a NodeID."""
        return self.registry_path / self.shard_for(node_id) / f"{node_id}.json"

    def load(self, node_id: str) -> dict[str, Any] | None:
        """Load a symbol record by NodeID, or None if it is absent."""
        if self._cached_node_id_index is not None:
            return self._cached_node_id_index.get(node_id)
        path = self.path_for(node_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_all(self) -> list[dict[str, Any]]:
        """Load all registry records in deterministic path order."""
        if self._cached_records is None:
            if not self.registry_path.exists():
                self._cached_records = []
            else:
                records: list[dict[str, Any]] = []
                for path in sorted(self.registry_path.glob("*/*.json")):
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                self._cached_records = records

        if self._cached_node_id_index is None:
            self._cached_node_id_index = {r["node_id"]: r for r in self._cached_records}

        if self._cached_birth_key_index is None:
            self._cached_birth_key_index = {}
            for record in self._cached_records:
                if record.get("status") == "active":
                    keys = {record.get("birth_key"), *record.get("aliases", [])}
                    for k in keys:
                        if isinstance(k, str):
                            self._cached_birth_key_index[k] = record
        return self._cached_records

    def lookup_birth_key(self, key: str) -> dict[str, Any] | None:
        """Find the active record for a birth key or alias."""
        if self._cached_birth_key_index is None:
            self.load_all()
        return self._cached_birth_key_index.get(key)

    def get_or_create(
        self,
        *,
        kind: str,
        path: str,
        qualified_name: str,
        owner: str | None = None,
        rev: int = 0,
    ) -> RegistryWrite:
        """Load an existing symbol by birth key, or create it if missing."""
        key = birth_key(path, qualified_name)
        existing = self.lookup_birth_key(key)
        if existing is not None:
            return RegistryWrite(
                node_id=existing["node_id"],
                path=self.path_for(existing["node_id"]),
                record=existing,
            )

        node_id = node_id_for(kind, key)
        digest = birth_digest(key)
        record = {
            "node_id": node_id,
            "kind": kind,
            "birth_key": key,
            "birth_digest": digest,
            "current": {
                "path": _normalize_rel_path(path),
                "qualified_name": qualified_name,
            },
            "aliases": [key],
            "previous_names": [],
            "owner": owner,
            "status": "active",
            "history": [
                {
                    "rev": rev,
                    "event": "born",
                    "key": key,
                }
            ],
        }
        return self.upsert(record)

    def upsert(self, record: dict[str, Any]) -> RegistryWrite:
        """Write a complete registry record under its canonical shard path."""
        self._require_write_lock()
        node_id = record["node_id"]
        if not NODE_ID_RE.match(node_id):
            raise ValueError(f"Invalid NodeID: {node_id}")
        path = self.path_for(node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_canonical_json(record), encoding="utf-8", newline="\n")
        
        # Update cache in-place
        if self._cached_records is not None:
            for i, r in enumerate(self._cached_records):
                if r["node_id"] == node_id:
                    self._cached_records[i] = record
                    break
            else:
                self._cached_records.append(record)

        if self._cached_node_id_index is not None:
            self._cached_node_id_index[node_id] = record

        if self._cached_birth_key_index is not None:
            keys = {record.get("birth_key"), *record.get("aliases", [])}
            for k in keys:
                if isinstance(k, str):
                    if record.get("status") == "active":
                        self._cached_birth_key_index[k] = record
                    else:
                        self._cached_birth_key_index.pop(k, None)

        return RegistryWrite(node_id=node_id, path=path, record=record)

    def mark_obsolete(self, node_id: str, *, rev: int = 0) -> RegistryWrite:
        """Mark an existing symbol record obsolete."""
        record = self.load(node_id)
        if record is None:
            raise KeyError(node_id)
        record["status"] = "obsolete"
        record.setdefault("history", []).append({"rev": rev, "event": "obsolete"})
        return self.upsert(record)

    def alias(
        self,
        node_id: str,
        *,
        path: str,
        qualified_name: str,
        rev: int = 0,
    ) -> RegistryWrite:
        """Add an alias and update the current path/name for a symbol."""
        record = self.load(node_id)
        if record is None:
            raise KeyError(node_id)
        key = birth_key(path, qualified_name)
        aliases = record.setdefault("aliases", [])
        if key not in aliases:
            aliases.append(key)
        current = record.setdefault("current", {})
        previous_name = current.get("qualified_name")
        if previous_name and previous_name != qualified_name:
            previous_names = record.setdefault("previous_names", [])
            if previous_name not in previous_names:
                previous_names.append(previous_name)
        current["path"] = _normalize_rel_path(path)
        current["qualified_name"] = qualified_name
        record.setdefault("history", []).append(
            {
                "rev": rev,
                "event": "alias",
                "key": key,
            }
        )
        return self.upsert(record)

    def _require_write_lock(self) -> None:
        # External projects (no runtime dir) skip lock enforcement
        if not (self.sync_path / "runtime").exists():
            return
        lock = read_lock(self.sync_path)
        if lock is None or lock.get("held_by") != self.agent:
            raise RegistryLockError(
                f"Registry writes require .sync/runtime/LOCK held by {self.agent}"
            )
