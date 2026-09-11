# StackMind P6/P7 Runtime & Documentation Truth Baseline

**Repository:** `Abhishek3670/stackmind-cli`  
**Branch:** `feat/p6-open-source-tui`  
**Document Authority:** Authoritative Current-State Baseline (PRE-P7-0 & PRE-P7-1)  
**Governing Documents:** [`PLAN_P7_PREREQUISITES.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_P7_PREREQUISITES.md), [`PLAN_STACKMIND_CLI_FINAL.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_STACKMIND_CLI_FINAL.md), [`AGENTS.md`](file:///W:/Aatish/Stuff/stackmind-cli/AGENTS.md)  
**Date:** 2026-09-11  

---

## 1. Objective & Authority

This document provides the single authoritative statement of what code actually exists on branch `feat/p6-open-source-tui` versus what is described in project documentation (notably [`STACKMIND_CLI.md`](file:///W:/Aatish/Stuff/stackmind-cli/STACKMIND_CLI.md)).

Per `PLAN_STACKMIND_CLI_FINAL.md` §0.1:
- `pyproject.toml` is the authoritative source for version (`3.2.0`), dependencies, and CLI entry points.
- This baseline document (`docs/runtime-truth/P6-P7-baseline.md`) is the authoritative current-state truth.
- `STACKMIND_CLI.md` is target-state/product documentation, not an implementation baseline.
- No planned capability may be described or treated as shipped until verified by the test harness.

---

## 2. Status Classification Vocabulary

Every capability evaluated below is classified into one of five mutually exclusive states:

* **`IMPLEMENTED`**: Fully present in branch code with passing test coverage.
* **`PARTIALLY_IMPLEMENTED`**: Present in branch code as a prototype or partial API, but lacking full protocol wiring, test coverage, or required invariants.
* **`PLANNED`**: Documented as future capability; zero or minimal stub code exists.
* **`DEPRECATED`**: Previously proposed or implemented approach that is formally superseded (e.g., TypeScript/Bun/Zig TUI renderer).
* **`INCORRECT`**: Claimed in prose as "Shipped v3.2.0 GA" or "Available", but contradicted by actual implementation.

---

## 3. Comprehensive Truth Reconciliation Matrix

| Capability / Surface | Claimed Documented State | Actual Branch Implementation State | Status Classification | Required Remediation / Action |
|---|---|---|---|---|
| **CLI Command `stackmind tui`** | Claimed as `v3.2.0 GA` entry point in `STACKMIND_CLI.md` §2. | Not registered in `cli/main.py` Click group. Executing `stackmind tui` errors with unrecognized command. Only root prototype `tui.py` exists. | **INCORRECT** | Implement Click subcommand `tui` in `cli/main.py` delegating to client launcher (Phase 1, WO-016). |
| **Operation-Scoped Cancellation** | Claimed as `v3.2.0 GA` non-terminating operation cancellation. | `SessionManager.cancel_session()` sets session state to `CANCELLED` (terminal) after signaling active operation. No `cancel_operation()` method exists. Session cannot survive operation cancellation. | **INCORRECT** | Decouple Session from Operation in `SessionManager`. Implement 7-state machine and `cancel_operation(operation_id)` (Phase 1, WO-012). |
| **Cooperative Harness Cancellation** | Implied in runtime execution. | `AgentRunner.run_once()` does not take or check a `cancellation_event` handle. Blocking steps and tool calls run to completion. | **PARTIALLY_IMPLEMENTED** | Thread cancellation check through 10 documented checkpoints in `validators/harness/runner.py` (Phase 1, WO-012). |
| **JSON-RPC Protocol Methods** | Claimed support for full session, operation, plan, and tool RPC namespaces. | `JsonRpcProtocol` only handles `session.create`, `session.get`, `session.list`, `session.pause`, `session.resume`, `session.cancel`, `session.approval`, `event.list`. Lacks `operation.*`, `plan.*`, `tool.*`. | **PARTIALLY_IMPLEMENTED** | Expand `JsonRpcProtocol` to standard namespaces specified in `PLAN_STACKMIND_CLI_FINAL.md` §6.2 (Phase 1, WO-013). |
| **Transport Streaming** | Claimed streaming event delivery over JSON-RPC. | `StackMindTuiAdapter.stream()` polls `event.list` in a generator loop with `after` sequence. No persistent SSE / chunked HTTP push stream exists. | **PARTIALLY_IMPLEMENTED** | Validate and implement durable SSE / chunked stream transport over HTTP (Phase 1, WO-014). |
| **Daemon Authentication & Token Security** | Claimed secure local loopback communication. | Daemon binds to `127.0.0.1` but does not check bearer tokens or auth headers. Any local process can hit `/rpc`. | **PLANNED** | Implement token generation, restricted file permissions, and token header verification (Phase 2, PRE-P7-9 / WO-017). |
| **Prompt / Turn Execution API** | Claimed prompt and interactive agent turn flow. | `StackMindTuiAdapter.command()` raises `ValueError("Prompts and tool execution are runtime-owned...")` for prompts. No turn RPC method exists. | **PLANNED** | Implement governed prompt/turn execution endpoint connecting daemon to `AgentRunner` (Phase 1, WO-015). |
| **TUI View Components** | Prototype rendered in `tui.py` demo. | Rich views exist in `validators/kernel/tui/views.py` (`contract_panel`, `diff_viewer`, `verification_matrix`, `hitl_prompt`). Functional for static display. | **IMPLEMENTED** | Retain existing Rich view renderers; connect them to live event stream (Phase 1, WO-016). |
| **TUI Interactive Commands (`:diff`, `:matrix`)** | Claimed in table of available TUI commands. | `tui.py` has stub handlers. `StackMindTuiAdapter` does not handle `:diff` or `:matrix`. | **PARTIALLY_IMPLEMENTED** | Wire `:diff` and `:matrix` commands in TUI adapter to query daemon inspection endpoints. |
| **Governed Harness Loop (`AgentRunner`)** | Governed execution loop with Knowledge API, contract verification, D025 safeguards. | `AgentRunner` in `validators/harness/runner.py` fully implements staging, contract checking, D025 checks, and write lock. | **IMPLEMENTED** | Maintain as single authoritative execution engine; wire into daemon operations. |
| **Knowledge Graph API & Storage** | Indexed Knowledge Compiler with FTS5 search and contract scoping. | Fully implemented in `validators/knowledge/`. Tested and verified (`graph stats` operational). | **IMPLEMENTED** | Continue using Knowledge API as sole context and retrieval provider. |
| **Durable Daemon Persistence & Replay** | Atomic JSON persistence of sessions and events. | Implemented via `DaemonStorage` and atomic file write in `validators/kernel/daemon/storage.py`. | **IMPLEMENTED** | Expand schema to track parent/child operations and plan revisions (PRE-P7-10). |
| **TypeScript / Bun / Zig TUI Client** | Proposed in alternative design notes. | No code in branch. Evaluated as non-viable due to dual-package CI, packaging, and process complexity. | **DEPRECATED** | Formalized architectural rejection per PRE-P7-1. Pure Python-native TUI confirmed. |
| **Git Worktree Workspace Isolation** | Planned for concurrent multi-role execution. | Currently single staging workspace in Harness; no worktree orchestration per Work Order. | **PLANNED** | Implement Git worktree isolation for P7 multi-role execution (Phase 3, P7-2). |

---

## 4. PRE-P7-1: Formal Architectural Decision Record (ADR)

### Context
StackMind requires an interactive terminal control plane (TUI) acting strictly as an unprivileged client to the local JSON-RPC daemon. Initial design iterations explored TypeScript (`@opentui/react`), Bun, Zig native binaries, or Node.js Ink/Blessed.

### Decision: Python-Native TUI
StackMind adopts a **purely Python-native TUI** architecture residing within the existing canonical `stackmind` package.

```text
Canonical Package (stackmind)
  ├── cli/main.py             (Click CLI entry point)
  ├── validators/kernel/daemon/ (JSON-RPC 2.0 Local Daemon)
  ├── validators/kernel/tui/    (Client Adapter & Rich Views)
  └── validators/harness/       (AgentRunner Execution Authority)
```

### Rationale
1. **Single Toolchain**: Single interpreter, single package manager (`pip` / `pyproject.toml`), unified CI/CD.
2. **Zero Native Bridge Overhead**: Eliminates cross-language subprocess communication issues, packaging friction, and platform-dependent Zig/Bun compilation.
3. **No Process Splitting**: Rich and Textual provide full-featured terminal styling, responsive layouts, syntax highlighting, and diff rendering directly in Python.
4. **Governed Client Boundary**: The Python client strictly communicates with the daemon over loopback HTTP/JSON-RPC, preserving the exact same architectural boundary as an external client.

---

## 5. Exit Gate Sign-Off (PRE-P7-0 & PRE-P7-1)

* [x] **Contradictions reconciled**: Documentation claims in `STACKMIND_CLI.md` are mapped against real code.
* [x] **Vocabulary applied**: All capabilities categorized with strict status indicators.
* [x] **Language finalized**: Python-native architecture locked; external frontend runtimes deprecated.
* [x] **Version authority**: Version `3.2.0` rooted in `pyproject.toml`.

**Gate Verdict:** **PASS**. The repository is authorized to proceed to **Phase 1: P6 Runtime Foundation**.
