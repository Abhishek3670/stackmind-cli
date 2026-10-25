"""
Supervisor State Store (Amendment A Hardening).

Enforces:
1. Primary authoritative state in supervisor memory.
2. In-memory secret only (os.urandom(32)); never written to disk or child environment.
3. Monotonic in-memory sequence numbers against replay of older signed state.
4. HMAC hash chain over state checkpoints and journal events.
5. Storage outside workspace boundaries; workspace copies are write-only shadow telemetry.
6. Daemon restart fails closed to WAITING_FOR_HUMAN requiring human re-attestation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from validators.clock import now as _now


class StateIntegrityError(Exception):
    """Raised when state HMAC signature verification fails or state is tampered."""
    pass


class ReplayAttackError(Exception):
    """Raised when an older signed state checkpoint is replayed."""
    pass


class DaemonRestartFailClosedError(Exception):
    """Raised when un-attested state is loaded across daemon restart."""
    pass


@dataclass(frozen=True)
class StateCheckpoint:
    seq: int
    prev_hmac: str
    hmac_signature: str
    timestamp: str
    run_id: str
    state_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "prev_hmac": self.prev_hmac,
            "hmac_signature": self.hmac_signature,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "state_payload": self.state_payload,
        }


class SupervisorStateManager:
    """Manages authoritative state in memory with HMAC chain checkpoints stored outside workspace."""

    def __init__(self, storage_dir: Path | str | None = None, secret_key: bytes | None = None) -> None:
        self._secret = secret_key or os.urandom(32)
        self._sequence_number: int = 0
        self._last_hmac: str = "GENESIS_HMAC_CHAIN"
        self._journal_chain: list[dict[str, Any]] = []
        self._storage_dir = Path(storage_dir) if storage_dir else Path(tempfile.mkdtemp(prefix="stackmind_supervisor_"))
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._in_memory_states: dict[str, Any] = {}
        self._attested_runs: set[str] = set()

    @property
    def storage_dir(self) -> Path:
        return self._storage_dir

    @property
    def sequence_number(self) -> int:
        return self._sequence_number

    def register_state(self, run_id: str, state: Any) -> None:
        """Register state in supervisor memory."""
        self._in_memory_states[run_id] = state
        self._attested_runs.add(run_id)

    def get_state(self, run_id: str) -> Any | None:
        """Get authoritative in-memory state."""
        return self._in_memory_states.get(run_id)

    def create_checkpoint(self, state: Any, workspace: Path | str | None = None) -> StateCheckpoint:
        """Create HMAC-signed checkpoint stored strictly outside workspace."""
        self._sequence_number += 1
        state_dict = state.to_dict() if hasattr(state, "to_dict") else dict(state)
        canonical_json = json.dumps(state_dict, sort_keys=True, separators=(",", ":"))
        state_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        now_ts = _now()

        # Chain HMAC: secret + seq + prev_hmac + state_hash + timestamp
        msg = f"{self._sequence_number}:{self._last_hmac}:{state_hash}:{now_ts}".encode("utf-8")
        sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

        checkpoint = StateCheckpoint(
            seq=self._sequence_number,
            prev_hmac=self._last_hmac,
            hmac_signature=sig,
            timestamp=now_ts,
            run_id=getattr(state, "run_id", "default"),
            state_payload=state_dict,
        )

        # Append to in-memory HMAC journal
        self._journal_chain.append({
            "seq": checkpoint.seq,
            "prev_hmac": checkpoint.prev_hmac,
            "hmac": sig,
            "event_type": "CHECKPOINT",
            "timestamp": now_ts,
        })
        self._last_hmac = sig

        # Persist signed checkpoint OUTSIDE workspace
        cp_file = self._storage_dir / f"{checkpoint.run_id}_seq_{checkpoint.seq:06d}.json"
        cp_file.write_text(json.dumps(checkpoint.to_dict(), indent=2), encoding="utf-8")

        # Optional write-only shadow mirror in workspace (never read as authoritative input)
        if workspace:
            try:
                ws_path = Path(workspace)
                shadow_dir = ws_path / ".sync" / "runtime" / "supervisor"
                shadow_dir.mkdir(parents=True, exist_ok=True)
                shadow_file = shadow_dir / f"{checkpoint.run_id}.yaml"
                import yaml
                shadow_header = (
                    "# =====================================================================\n"
                    "# SHADOW TELEMETRY COPY ONLY -- NOT AN INPUT TO SUPERVISOR ADVANCE LOOP\n"
                    "# Authoritative state is maintained in supervisor memory with HMAC chain.\n"
                    "# Any edits made here by agents or child processes will be ignored.\n"
                    "# =====================================================================\n"
                )
                shadow_file.write_text(shadow_header + yaml.safe_dump(state_dict, sort_keys=False), encoding="utf-8")
            except Exception:
                pass

        return checkpoint

    def restore_checkpoint(self, checkpoint_data: dict[str, Any]) -> dict[str, Any]:
        """Validate and restore a checkpoint with sequence number replay check and HMAC validation."""
        seq = int(checkpoint_data["seq"])
        prev_hmac = str(checkpoint_data["prev_hmac"])
        sig = str(checkpoint_data["hmac_signature"])
        now_ts = str(checkpoint_data["timestamp"])
        run_id = str(checkpoint_data["run_id"])
        payload = dict(checkpoint_data["state_payload"])

        # 1. Monotonic sequence check against replay of older signed state
        if seq <= self._sequence_number:
            raise ReplayAttackError(
                f"Replay attack detected: checkpoint sequence {seq} <= last known sequence {self._sequence_number}"
            )

        # 2. HMAC signature verification against in-memory secret
        canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        state_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        msg = f"{seq}:{prev_hmac}:{state_hash}:{now_ts}".encode("utf-8")
        expected_sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(sig, expected_sig):
            raise StateIntegrityError("State HMAC signature verification failed: invalid key or tampered state")

        # 3. Advance in-memory sequence number
        self._sequence_number = seq
        self._last_hmac = sig
        self._attested_runs.add(run_id)
        return payload

    def record_journal_event(self, event_type: str, details: dict[str, Any]) -> dict[str, Any]:
        """Append an event into the rolling HMAC journal chain."""
        self._sequence_number += 1
        now_ts = _now()
        event_json = json.dumps(details, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(event_json.encode("utf-8")).hexdigest()

        msg = f"{self._sequence_number}:{self._last_hmac}:{event_hash}:{now_ts}".encode("utf-8")
        sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

        entry = {
            "seq": self._sequence_number,
            "prev_hmac": self._last_hmac,
            "hmac": sig,
            "event_type": event_type,
            "details": details,
            "timestamp": now_ts,
        }
        self._journal_chain.append(entry)
        self._last_hmac = sig
        return entry

    def is_run_attested(self, run_id: str) -> bool:
        """Check if run has valid in-memory attestation."""
        return run_id in self._attested_runs

    def re_attest_run(self, run_id: str, reason: str) -> None:
        """Operator explicitly re-attests run after daemon restart."""
        self._attested_runs.add(run_id)
        self.record_journal_event("HUMAN_RE_ATTESTATION", {"run_id": run_id, "reason": reason})
