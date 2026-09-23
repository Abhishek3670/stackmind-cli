# P7 Runtime Truth: Plan Lifecycle, Security & Persistence Recovery Contract

**Date:** 2026-09-11  
**Prerequisites:** PRE-P7-8, PRE-P7-9, PRE-P7-10  
**Author:** Claude (Senior Architect)  
**Status:** **AUTHORITATIVE TRUTH & GOVERNANCE CONTRACT**  
**Governing Plans:** [`PLAN_P7_PREREQUISITES.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_P7_PREREQUISITES.md) §12-§14, [`PLAN_STACKMIND_CLI_FINAL.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_STACKMIND_CLI_FINAL.md)  

---

## 1. Executive Overview

This document specifies the three foundational integrity pillars required for safe, autonomous multi-role operations in StackMind P7:
1. **Plan Lifecycle & Human-in-the-Loop (HITL) Gate (PRE-P7-8)**
2. **Security, Authentication & Credential Isolation Boundary (PRE-P7-9)**
3. **Persistence, Compaction & Crash Recovery Invariant (PRE-P7-10)**

---

## 2. Plan Lifecycle Contract (PRE-P7-8)

A Plan represents the architectural translation of user objectives into concrete, governed Work Orders. The Plan lifecycle is strictly governed by a 5-state state machine:

```text
               ┌───────────────┐
               │     DRAFT     │
               └───────┬───────┘
                       │ submit()
                       ▼
               ┌───────────────────────┐
         ┌───► │   AWAITING_APPROVAL   │ ◄───┐
         │     └───────┬───────────────┘     │
         │             │                     │
         │      approve│       reject(reason)│ revise()
         │             ▼                     │
         │     ┌───────────────┐     ┌───────┴───────┐
         │     │   APPROVED    │     │   REJECTED    │
         │     └───────┬───────┘     └───────────────┘
         │             │ (amendment)
         └─────────────┴──► SUPERSEDED
```

### 2.1 State Definitions & Invariants
- **`DRAFT`**: Architecture Agent (Claude) is analyzing the repository knowledge graph and drafting work orders. Work Orders are not created in `.sync/work-orders/ACTIVE/`.
- **`AWAITING_APPROVAL`**: Plan rendered in TUI/RPC (`plan.get`). The runtime blocks all autonomous worker dispatch until human/operator approval.
- **`REJECTED`**: Human operator rejects the plan. Rejection records structured operator feedback. **A rejected plan never creates or dispatches Work Orders.**
- **`APPROVED`**: Human operator confirms the plan (`:approve` or `plan.approve`). Only upon entering `APPROVED` does Claude generate child Work Orders and contracts.
- **`SUPERSEDED`**: A subsequent architectural amendment or scope revision replaces an earlier plan. Historical revisions remain archived in `.sync/plans/` for audit.

### 2.2 Error Invariants
- Invoking `plan.approve` on an already `APPROVED`, `REJECTED`, or `SUPERSEDED` plan raises a structured JSON-RPC error:
  `{"code": -32003, "message": "Plan is not in AWAITING_APPROVAL state"}`.

---

## 3. Security, Authentication & Transport Boundary (PRE-P7-9)

### 3.1 Daemon Loopback & Network Boundary
1. **Default Loopback Binding**: The LocalDaemon binds strictly to `127.0.0.1` (never `0.0.0.0`).
2. **Local Auth Token**:
   - On startup, `LocalDaemon` generates a cryptographically secure 256-bit token (`secrets.token_hex(32)`).
   - Saved to `.sync/runtime/daemon.token` with restrictive file permissions (mode `0600` on POSIX; explicit user ACL on Windows).
   - All incoming JSON-RPC calls over `/rpc` and SSE connections over `/events` must include the header:
     `Authorization: Bearer <token>`.
   - Requests without a valid token are rejected with `HTTP 401 Unauthorized`.
3. **Payload & Resource Limits**:
   - `Content-Length` capped at 10MB to prevent heap exhaustion.
   - SSE connection limit: Maximum 16 concurrent streaming subscribers.

### 3.2 Credential Isolation Invariant
> **The Zero-Leakage Credential Rule:**  
> Provider credentials (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, tokens) belong strictly to the host runtime.  
> They are NEVER sent over JSON-RPC, NEVER rendered in TUI client views, NEVER emitted in `RuntimeEvent` streams, and NEVER committed to repository files.

- **Redaction Filter**: All event payloads and log messages pass through an automated regex sanitization filter that masks credentials:
  `sk-ant-[a-zA-Z0-9_-]{20,}` → `sk-ant-[REDACTED]`  
  `sk-[a-zA-Z0-9_-]{20,}` → `sk-[REDACTED]`

### 3.3 Terminal Safety & Escape Injection Defense
- Raw model outputs, tool errors, and compiler diagnostics are stripped of ANSI escape injection sequences (`\x1b[...`) before presentation in the TUI, preventing terminal hijacking or cursor manipulation.

---

## 4. Persistence, Compaction & Crash Recovery Contract (PRE-P7-10)

### 4.1 Schema Versioning & Atomic Writes
1. **Schema Versioning**: All serialized JSON/YAML state files include `schema_version: 1` as a required root field.
2. **Atomic Write Replacement**:
   - Direct file overwriting is prohibited.
   - All persistence writes write to a sibling temporary file (`<target>.tmp.<uuid>`) followed by an atomic `replace` / rename operation:
     ```python
     temp_path.write_text(serialized_data, encoding="utf-8")
     temp_path.replace(target_path)
     ```
   - This prevents partial or corrupted state files if a crash occurs mid-write.

### 4.2 Crash Recovery Invariant
If the daemon or system crashes during autonomous multi-role execution:
1. On reboot, `SessionManager` inspects in-flight Operations from persisted storage.
2. If an operation was in state `RUNNING` or `CANCEL_REQUESTED` but its owning process/thread is dead:
   - The operation is transitioned to `FAILED` with failure reason: `"Process terminated unexpectedly (Crash Recovery)"`.
   - A runtime event `operation.failed` is appended.
   - The owning session is restored to `PAUSED` or `IDLE`, preserving its integrity.
3. **Repository Truth Re-sync**:
   - The daemon re-reads `.sync/work-orders/INDEX.yaml` and `.sync/runtime/TREE.yaml` to confirm actual disk state.
   - Active worktrees (`.sync/worktrees/`) are preserved for forensic review.

### 4.3 Event Journal Compaction Policy
- When `events.json` exceeds **10,000 events** or **15 MB**:
  1. Completed sessions older than 7 days have their event segments compressed into `.sync/runtime/archive/events-<timestamp>.json.gz`.
  2. The active `events.json` is compacted to retain only active sessions and the last 1,000 events.
  3. Replay queries requesting `after=<seq>` from archived sequences cleanly stream from the compressed segment.

---

## 5. Exit Gate Verification Summary

| Gate Requirement | Verification Metric | Status |
|---|---|---|
| **Plan FSM** | 5 states (`DRAFT`, `AWAITING_APPROVAL`, `REJECTED`, `APPROVED`, `SUPERSEDED`) | **VERIFIED** |
| **Auth Token** | Loopback binding + Bearer token auth via `.sync/runtime/daemon.token` | **VERIFIED** |
| **Zero Credential Leakage** | Automated token redaction filter + no credentials in RPC/SSE | **VERIFIED** |
| **Terminal Safety** | ANSI escape sequence sanitization for untrusted output | **VERIFIED** |
| **Crash Recovery** | Atomic writes (`.tmp` replace) + dead operation recovery to `FAILED` | **VERIFIED** |
| **Compaction** | Automated archival threshold for journals exceeding 10k events | **VERIFIED** |

This concludes the architectural readiness gate for **PRE-P7-8, PRE-P7-9, and PRE-P7-10**.
