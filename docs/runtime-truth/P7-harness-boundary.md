# P7 Runtime Truth: Harness Authority & Integration Boundary Contract

**Date:** 2026-09-11  
**Prerequisites:** PRE-P7-3 & PRE-P7-4  
**Author:** Claude (Senior Architect)  
**Status:** **AUTHORITATIVE TRUTH & GOVERNANCE CONTRACT**  
**Governing Plans:** [`PLAN_P7_PREREQUISITES.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_P7_PREREQUISITES.md) §7-§8, [`PLAN_STACKMIND_CLI_FINAL.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_STACKMIND_CLI_FINAL.md)  

---

## 1. Executive Summary & The Single-Source Rule

StackMind P7 expands the platform from a governed CLI/TUI foundation (Milestone P6) into an autonomous multi-role engineering delivery runtime. 

To ensure complete safety and architectural consistency, StackMind enforces the **Single-Source Rule**:
> **There is exactly one authoritative execution path in StackMind: the `AgentRunner` Harness.**  
> Under no circumstances may a daemon, TUI, orchestrator, or autonomous sub-system introduce an alternative execution engine, direct LLM provider loop, or un-governed tool execution path. All prompt turns, tool invocations, code modifications, and verifications must strictly traverse the `AgentRunner` pipeline.

```text
               User / CLI / TUI (stackmind tui)
                            │
                            ▼ (HTTP JSON-RPC 2.0: /rpc)
                 LocalDaemon (SessionManager)
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      Durable Work Orders       Runtime Operations (7-state)
      (.sync/work-orders/)      (operation_id, cancel_event)
               │                         │
               └────────────┬────────────┘
                            ▼
          AgentRunner Harness (Single Authoritative Engine)
             ├── Contract & Scope Enforcement (CONTRACT-01)
             ├── Knowledge API & Graph Context (KNOW-01)
             ├── 10 Cooperative Cancellation Checkpoints
             ├── Staging & 6D Verification Gate (HARNESS-01)
             ├── D025 Non-Destructive Safety Policy
             └── Structured Event Dispatcher (SSE /events)
                            │
                            ▼
                    Execution Backend
        (Anthropic / OpenAI / Echo / Ollama LLMProvider)
```

---

## 2. Definitional Vocabulary & Durable Identity Contract (PRE-P7-4)

To eliminate ambiguity across autonomous multi-role operations, the identity model is frozen as follows:

| Entity | Definition | Durability | Identity Scheme |
|---|---|---|---|
| **Plan** | Architectural blueprint and decomposition of user objectives into work orders | Ephemeral / Versioned | `PLAN.md`, `PLANv3.md` revision hash |
| **Work Order (WO)** | Durable, stateful, governed assignment defining requirements, dependencies, and deliverables | Permanent (`.sync/work-orders/`) | `WO-xxx` (e.g. `WO-015`) |
| **Operation** | Single in-memory runtime execution attempt of a turn, task, or tool action | Ephemeral / Session-bound | UUID or `OP-xxxxxxxx` |
| **Agent Role** | Logical persona and authority boundary with defined responsibility | Permanent | `claude`, `codex`, `gemini`, `gemma`, `local-llm` |
| **Execution Backend** | Physical model implementation or API gateway executing inference | Configured Provider | `claude-3-5-sonnet`, `stackmind-echo-v1`, `gpt-4o` |

### Core Identity Invariants
1. **$1..N$ Operations per Work Order**: A single Work Order may have multiple Operations over its lifetime (e.g. initial attempt, cooperative cancellation, retry).
2. **Retries Retain Work Order ID**: When a failed or cancelled operation is retried, the durable Work Order retains its identity (`WO-xxx`), while a new unique `operation_id` is created.
3. **Hierarchical Parent/Child Trees**: Parent operations (e.g. Orchestration Turn) may spawn child operations (e.g. Sub-task Worker Execution). Every child operation references its `parent_operation_id`.
4. **Monotonic Scope Restriction**: A child Operation **never** inherits broader scope or budget than its parent. The contract scope boundary of a child operation is strictly a subset ($\subseteq$) of the parent's contract boundary.

---

## 3. Formal Operation State Machine

Every Operation in `SessionManager` conforms to a 7-state monotonic lifecycle:

```text
               ┌───────────────┐
               │   REQUESTED   │
               └───────┬───────┘
                       │ authorize()
                       ▼
               ┌───────────────┐
               │  AUTHORIZED   │
               └───────┬───────┘
                       │ begin()
                       ▼
         ┌─────────────┴─────────────┐
         ▼                           ▼
  ┌──────────────┐          ┌──────────────────┐
  │   RUNNING    │ ───────► │ CANCEL_REQUESTED │
  └──────┬───────┘          └────────┬─────────┘
         │                           │
   ┌─────┴────────┐                  │ abort()
   ▼              ▼                  ▼
┌───────────┐  ┌────────┐     ┌──────────────┐
│ COMPLETED │  │ FAILED │     │  CANCELLED   │
└───────────┘  └────────┘     └──────────────┘
```

### State Definitions
1. **`REQUESTED`**: Turn or operation request received via JSON-RPC, awaiting authorization.
2. **`AUTHORIZED`**: Scope and contract pre-flight validated; resources allocated.
3. **`RUNNING`**: Execution delegated to `AgentRunner.run_once()`. Cancellation handle active.
4. **`CANCEL_REQUESTED`**: Cooperative cancel signal sent via `operation.cancel()`. Awaiting runner checkpoint.
5. **`COMPLETED`**: Runner finished all checkpoints, staged changes verified, and deliverable produced.
6. **`FAILED`**: Unrecoverable runtime exception, test failure, or contract violation encountered.
7. **`CANCELLED`**: Runner cleanly aborted at an active checkpoint. Parent Session remains healthy and reusable.

---

## 4. Architectural Boundary Answers (PRE-P7-3)

### 4.1 Which `AgentRunner` methods are invoked by the daemon?
The daemon invokes **exactly one** method on `AgentRunner`:
```python
runner = AgentRunner(
    workspace_dir=session["workspace"],
    contract_path=session["contract_path"],
    agent_id=session["agent"],
    provider=resolved_provider,
    event_dispatcher=self.events,
)
result: HarnessRunResult = runner.run_once(cancel_event=operation.cancel_event)
```
- `SessionManager.turn()` (or `SessionManager.start_turn()`) creates the `Operation`, spawns the execution asynchronously outside the manager `RLock`, and passes `operation.cancel_event`.
- The daemon does **not** call provider APIs, does not perform filesystem writes directly, and does not execute tools. All execution is encapsulated within `run_once()`.

### 4.2 Which state remains in `.sync/*` vs daemon persistence?
- **`.sync/*` (Canonical Git-Tracked Truth)**:
  - Work orders: `.sync/work-orders/ACTIVE/` and `.sync/work-orders/COMPLETED/`.
  - Governance contracts: `.sync/contracts/WO-xxx.yaml`.
  - Identity & Status ledger: `.sync/runtime/TREE.yaml`.
  - Asynchronous agent messaging: `.sync/inbox/<agent>/`.
  - Procedural experiences & graph: `.sync/knowledge/` and `.sync/learning/`.
- **Daemon Persistence (`DaemonStorage` / `sessions.json`, `events.json`)**:
  - Ephemeral runtime sessions and client connection metadata.
  - Sequenced event stream history (`RuntimeEvent`).
  - In-memory active operation map (`_active: dict[str, Event]`).
- **Resilience Invariant**: If the daemon process crashes or restarts, all repository truth remains 100% intact in `.sync/*`. On restart, the daemon reads `.sync/runtime/TREE.yaml` and `.sync/work-orders/INDEX.yaml` to reconstruct its operational state.

### 4.3 How does `HarnessTask.work_order_id` map to runtime Operations?
When `AgentRunner` executes:
- If executing an assigned work order: `HarnessTask.work_order_id = "WO-xxx"`.
- The daemon binds `operation.metadata["work_order_id"] = task.work_order_id`.
- The operation outcome is recorded in the daemon event stream.
- The durable Work Order in `.sync/work-orders/` is updated **only** upon Gemma QA approval and Claude's architectural verification, preserving strict role authority.

### 4.4 How does operation cancellation reach an executing Harness run?
- The operation instantiates a standard `threading.Event` (`cancel_event`).
- When `operation.cancel(operation_id)` or `session.cancel(session_id)` is invoked, `SessionManager` marks the operation `CANCEL_REQUESTED` and sets `cancel_event.set()`.
- `AgentRunner.run_once()` checks `cancel_event.is_set()` at **10 cooperative checkpoints**:
  1. Immediately post-task discovery.
  2. Immediately post-context assembly (`assemble_context`).
  3. Immediately post-pre-contract verification.
  4. Immediately post-before snapshot.
  5. Immediately post-retrieval.
  6. Immediately post-provider completion (`complete()`).
  7. Immediately post-decision schema validation.
  8. Immediately post-post-contract verification.
  9. Immediately post-after snapshot.
  10. Immediately post-D025 verification before write-back.
- When triggered, `AgentRunner` cleanly halts, skips workspace promotion, returns `status="cancelled"`, and transitions the operation to `CANCELLED` without corrupting the session.

### 4.5 How do Harness verification and persistence results become runtime events?
As `AgentRunner` reaches execution milestones, it invokes `event_dispatcher.publish()`:
- `operation.started`: Turn initiated.
- `event.toolCall`: Tool invoked (`status="running"`, `call_id`, `tool_name`, `arguments`).
- `event.toolResult`: Tool finished with terminal status (`success`, `failure`, `cancelled`, `denied`, `approval_required`).
- `verification.completed`: 6D verification matrix evaluated (scope, state, AST, behavioral, security, outcome).
- `operation.completed` / `operation.cancelled` / `operation.failed`: Operation terminal state reached.
These events stream in real time to connected TUI / SSE clients over `GET /events`.

### 4.6 How are duplicate journals and races prevented?
1. **Manager RLock**: All state transitions, journal mutations, and event emissions occur under `SessionManager`'s recursive thread lock (`RLock`).
2. **Monotonic Sequences**: `EventDispatcher` assigns strictly ascending integer sequence numbers (`1, 2, 3...`) to all runtime events.
3. **Single-Flight Work Order Closure**: Workers cannot close work orders; only Gemma issues QA verdicts, and only Claude moves work orders to `COMPLETED/` and commits canonical state.

### 4.7 How is `LLMProvider` generalized without creating a second execution model?
The `LLMProvider` interface in `validators/harness/runner.py` is a clean protocol:
```python
class LLMProvider(Protocol):
    provider_name: str
    model_name: str
    def complete(self, request: LLMRequest) -> CompletionRecord: ...
```
- Multi-role autonomous execution binds each **Agent Role** to a configured provider implementation (e.g. Anthropic, OpenAI, Local Ollama, Echo).
- Regardless of backend model or provider, the request is formed as an `LLMRequest` containing the contract context bundle, and execution is strictly monitored and gated by `AgentRunner`.

---

## 5. Exit Gate Acceptance Verification

| Requirement | Proof | Status |
|---|---|---|
| **Single Harness Entrypoint** | `AgentRunner.run_once(cancel_event=...)` is the exclusive execution engine | **VERIFIED** |
| **Cooperative Cancellation** | 10 execution checkpoints verified via `test_daemon_turn.py` & `test_harness_cancellation.py` | **VERIFIED** |
| **Identity Contract** | Plan, Work Order, Operation, Agent Role, Execution Backend definitions frozen | **VERIFIED** |
| **7-State Machine** | Operation lifecycle decoupled from Session; verified in `test_daemon_runtime.py` | **VERIFIED** |
| **No Duplicate Engine** | Zero direct provider or tool calls from TUI / Daemon layer | **VERIFIED** |

This concludes the architectural readiness gate for **PRE-P7-3** and **PRE-P7-4**.
