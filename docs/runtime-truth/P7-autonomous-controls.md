# P7 Runtime Truth: Autonomous Controls & Role/Backend Routing Contract

**Date:** 2026-09-11  
**Prerequisites:** PRE-P7-6 & PRE-P7-7  
**Author:** Claude (Senior Architect)  
**Status:** **AUTHORITATIVE TRUTH & GOVERNANCE CONTRACT**  
**Governing Plans:** [`PLAN_P7_PREREQUISITES.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_P7_PREREQUISITES.md) §10-§11, [`PLAN_STACKMIND_CLI_FINAL.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_STACKMIND_CLI_FINAL.md)  

---

## 1. Objective & Governance Mandate

StackMind P7 empowers agents to autonomously execute multi-turn engineering tasks across Work Orders. To eliminate infinite loops, run-away costs, runaway retries, and credential leakage, this document establishes the **Autonomous Execution Control Contract** and the **Role-to-Backend Binding Model**.

---

## 2. Bounded Autonomous Execution Limits (PRE-P7-6)

All autonomous execution is bounded by strict, non-bypassable envelope limits enforced at the Harness and Contract layers:

| Dimension | Default Limit | Enforcement Layer | Failure Action |
|---|---|---|---|
| **Plan Revision Limit** | Max 3 revisions | Orchestrator Gate | Terminal `PLAN_REJECTED_TERMINAL` → Escalate to Human/CEO |
| **Work Order Rework Limit** | Max 2 iterations (`rework_budget: 2`) | Work Order Gate | Worktree frozen → `BLOCKED_BY_REWORK` → Architect review |
| **Operation Timeout** | 60 minutes (`max_time_minutes: 60`) | Daemon / Operation | Cancellation signal triggered (`cancel_event.set()`) |
| **Token Budget** | 80,000 tokens (worker), 120,000 (architect) | Contract (`budget.max_tokens`) | Session terminates fail-closed; write-back denied |
| **Max Files Touched** | 6 to 8 files per Work Order | Contract (`budget.max_files_touched`) | Post-execution verification fails; staging denied |
| **Tool Call Budget** | Max 50 calls per operation | `AgentRunner` Loop Guard | Runner halts; emits `LoopSafetyError` |
| **Command Budget** | Max 30 shell executions | `AgentRunner` Subprocess Guard | Execution halted; operation marked `FAILED` |
| **Subagent Depth** | Strictly 0 in-process subagents (`IDE-01`) | Platform Invariant | Cross-agent delegation is asynchronous via `.sync/inbox/` |

---

## 3. Plan Rejection Lifecycle & Terminal Gate

The planning phase operates as a finite state machine preventing cyclical re-planning:

```text
       ┌───────────────┐
       │   PROPOSED    │
       └───────┬───────┘
               │ user/HITL review
       ┌───────┴───────┐
       ▼               ▼
 ┌───────────┐   ┌────────────┐
 │ APPROVED  │   │  REJECTED  │
 └─────┬─────┘   └─────┬──────┘
       │               │
       ▼ (Execute)     ▼ (revision_count < 3)
                 ┌────────────┐
                 │  REVISING  │
                 └─────┬──────┘
                       │ (revision_count >= 3)
                       ▼
                 ┌──────────────────────────┐
                 │  PLAN_REJECTED_TERMINAL  │
                 └──────────────────────────┘
```

1. **Finite Revisions**: The Orchestrator may submit at most **3 plan revisions**.
2. **Deterministic Termination**: If the human operator or CEO rejects revision 3, the session transitions to `PLAN_REJECTED_TERMINAL`.
3. **No Phantom Re-plans**: The platform will not autonomously attempt revision 4. Execution freezes until the user provides a completely new objective or modifies instructions.

---

## 4. Retry & Exponential Backoff Policy

When an operation terminates prematurely or fails, the runtime distinguishes **retryable** vs **non-retryable** failure modes:

### 4.1 Categorization

| Failure Category | Retryable? | Handling Protocol |
|---|---|---|
| **Transient Network / HTTP 429 / 503** | **YES** | Exponential backoff with jitter; retry within same operation |
| **Process Crash / Deadlock Timeout** | **YES** | New Operation spawned; Work Order retained (`retry_count + 1`) |
| **Contract Scope Violation (`ContractAccessDenied`)** | **NO** | Fail-closed immediately. Log audit security alert |
| **D025 Destructive Action Attempt** | **NO** | Abort immediately. Escalate to CEO inbox |
| **Broken Environment (GEMINI-01)** | **NO** | Mark session `BLOCKED`; open explicit BUGFIX work order |
| **Schema / Syntax Validation Failure** | **NO** | Emit `NEEDS_CHANGES` verdict; invoke rework budget |

### 4.2 Exponential Backoff Formula
For transient retryable failures:
$$T_{\text{wait}} = \min\left(60\text{s}, 2^n \times 1.5\text{s}\right) \pm \text{jitter}(0.5\text{s})$$
Where $n \in \{1, 2, 3\}$. Maximum retries per operation: 3.

---

## 5. Multi-Role Parent & Sibling Failure Policy

In a concurrent multi-role execution (e.g. Codex building backend, Gemini building frontend, Gemma validating):

```text
               Parent Feature Work Order (Milestone)
                                 │
        ┌────────────────────────┴────────────────────────┐
        ▼                                                 ▼
Backend WO (Codex)                              Frontend WO (Gemini)
    Status: APPROVED                                  Status: FAILED / BLOCKED
        │                                                 │
        ▼                                                 ▼
Candidate Worktree                                Worktree Frozen
(Unpromoted Candidate)                            (Under Investigation)
```

### Policy Rules
1. **All-or-Nothing Milestone Promotion**: A parent milestone work order is promoted to canonical truth **only** when all child work orders pass Gemma's QA approval.
2. **Candidate Isolation**: Successful sibling work orders (e.g. Backend) remain in their candidate worktrees as unpromoted candidates. They are **not** merged into canonical until the failing sibling is resolved.
3. **No Cascading Rollbacks**: Pre-existing canonical commits from previous closed work orders are never reverted automatically. Only the active, unmerged worktrees are affected.
4. **Architect Escalation**: When a child work order reaches `BLOCKED` or `FAILED`, Claude (Architect) receives an escalation notice in `.sync/inbox/claude/` and determines whether to:
   - Issue a targeted `BUGFIX` Work Order.
   - Adjust the child contract scope.
   - Re-route the task to an alternative execution backend.

---

## 6. Role-to-Backend Binding Contract (PRE-P7-7)

StackMind maintains a strict separation between **Agent Roles** (logical responsibilities) and **Execution Backends** (physical models and gateways):

```text
Agent Role (Logical Persona)
      │
      ▼
Role Binding Configuration (config.yaml / runtime)
      │
      ▼
Execution Backend (LLMProvider Protocol)
      ├── anthropic (Claude 3.5 Sonnet / 3.7 Sonnet)
      ├── openai (GPT-4o / o1 / o3-mini)
      ├── local_vllm / ollama (DeepSeek / Qwen / Llama-3)
      └── echo (Testing & Verification Gateway)
```

### 6.1 Canonical Role Binding Matrix

| Agent Role | Primary Focus | Default Backend Binding | Fallback Cascade |
|---|---|---|---|
| **Claude** | Architecture, Planning, Work Orders | `anthropic:claude-3-7-sonnet` | `anthropic:claude-3-5-sonnet` |
| **Codex** | Backend Services, APIs, Kernel, DB | `anthropic:claude-3-5-sonnet` | `openai:gpt-4o` |
| **Gemini** | Frontend, TUI, Client UI/UX | `anthropic:claude-3-5-sonnet` | `openai:gpt-4o` |
| **Gemma** | QA Lead, Verification Gates, Audits | `anthropic:claude-3-5-sonnet` | `openai:gpt-4o-mini` |
| **Local-LLM** | GitOps, Versioning, Release Hygiene | `local:ollama-qwen-2.5-coder` | `echo` |

### 6.2 Credential Isolation & Zero-Leakage Mandate
1. **Environment & Keyring Only**: Provider credentials (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) are read strictly from OS environment variables or secure credential stores by `AgentRunner` at the point of inference.
2. **No Protocol Exposure**: API keys are **never** transmitted over JSON-RPC, never included in session headers, and never written to `.sync/*` files or daemon event logs.
3. **Redaction Filter**: All outgoing event payloads emitted to `EventDispatcher` pass through a regex redaction filter that strips patterns matching API key formats (`sk-ant-...`, `sk-...`).

---

## 7. Exit Gate Verification

| Requirement | Verification Proof | Status |
|---|---|---|
| **Bounded Budgets** | Strict token, file, time, and tool limits formalized per contract | **VERIFIED** |
| **Finite Rejection Policy** | Max 3 plan revisions with terminal `PLAN_REJECTED_TERMINAL` gate | **VERIFIED** |
| **Deterministic Retry** | Clear retryable vs non-retryable categorization and backoff formula | **VERIFIED** |
| **Sibling Failure Policy** | All-or-nothing milestone promotion; candidate worktree isolation | **VERIFIED** |
| **Role-to-Backend Binding** | Frozen role matrix and credential isolation specification | **VERIFIED** |

This concludes the architectural readiness gate for **PRE-P7-6** and **PRE-P7-7**.
