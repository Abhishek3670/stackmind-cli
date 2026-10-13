# StackMind Skills Execution Profiles and Optimization

## Overview

StackMind provides a centralized, configurable Skills execution profile system designed to optimize complete product-delivery cycles. Rather than indiscriminately retrieving skills or expanding context windows, StackMind exposes three distinct execution profiles that tune the trade-off between execution speed, contextual guidance, token consumption, and verification depth:

| Profile | Skills Enabled | Skill Limit | Token Budget (Est) | Relevance Min | Progressive Detail Threshold | Verification Depth | Deep Review | Default |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Low** | No | 0 | 0 | 1.00 | 1.00 | Standard | No | |
| **Medium** | Yes | 2 | 600 | 0.25 | 0.50 | Standard | No | **Yes** |
| **High** | Yes | 5 | 1500 | 0.15 | 0.15 | Extended | Yes | |

---

## 1. Architectural Architecture & Invariants

```
               ┌────────────────────────────────────────────────────────┐
               │              Operator / TUI / Settings                 │
               │  (:profile low|medium|high | settings.json | :settings) │
               └───────────────────────────┬────────────────────────────┘
                                           │
                         Run Boundary Snap (Immutable per Run)
                                           ▼
┌───────────────────────────────────────────────────────────────────────────────────────┐
│ Lifecycle Run / Supervisor (RunState.execution_profile)                               │
│                                                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐  │
│  │ Knowledge Context Assembly (KnowledgeAPI.assemble_context)                      │  │
│  │                                                                                 │  │
│  │  1. Candidates Identified  ──▶ list_skills(status=ACTIVE)                       │  │
│  │  2. Scope Filtered         ──▶ AgentContract allow/deny boundary                │  │
│  │  3. Ranked by Relevance    ──▶ Term match x Confidence                          │  │
│  │  4. Profile Filter Applied ──▶ Low: bypass / Medium: limit=2 / High: limit=5    │  │
│  │  5. Progressive Loading    ──▶ Compact summary vs. full runbook                 │  │
│  │  6. Budget Enforced        ──▶ Conservative token budget (0 / 600 / 1500)       │  │
│  │  7. Audited & Injected     ──▶ Diagnostics: considered/selected/loaded/injected │  │
│  └─────────────────────────────────────────────────────────────────────────────────┘  │
│                                           │                                           │
│  ┌────────────────────────────────────────┴────────────────────────────────────────┐  │
│  │ Optional Decision Engine (LayaDecisionAdapter)                                  │  │
│  │  - Disabled by default (zero downloads, zero network)                           │  │
│  │  - Deterministic fallback heuristic for task classification                     │  │
│  │  - Shadow evaluation mode for benchmarking                                      │  │
│  └─────────────────────────────────────────────────────────────────────────────────┘  │
│                                           │                                           │
│  ┌────────────────────────────────────────┴────────────────────────────────────────┐  │
│  │ Execution Cycle Telemetry & Delivery Verification Gates                         │  │
│  │  - Staged writes verification (D024) strictly enforced across all profiles      │  │
│  │  - Wall-clock latencies, measured model tokens vs. estimated skill tokens       │  │
│  │  - Event publication: cycle.telemetry                                           │  │
│  └─────────────────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### Invariants:
1. **Safety Controls Are Never Bypassed**: Low profile disables *optional* procedural skills, but **never** bypasses mandatory system prompts, agent contracts, D024 verification gates, release hygiene, or HITL approval requirements.
2. **Cycle Boundary Immutability**: The active execution profile is snapped at the start of a lifecycle run (`LifecycleSupervisor.start_run` and `RunState`). If an operator changes settings while a run is in progress, the active run continues with its snapped profile; changes take effect at the start of the next execution cycle.
3. **Explicit Accounting Distinction**: Exact tokens returned by LLM adapters (`usage.prompt_tokens`, `usage.completion_tokens`) are recorded as measured. Context allocations for procedural skills use a conservative formula `(len(text) + 3) // 4` and are explicitly recorded as `token_usage_measured: false`. Estimated tokens are never fabricated or reported as measured.

---

## 2. Progressive Retrieval & Lifecycle Stages

StackMind maintains a strict audit trail distinguishing the lifecycle of each skill during context assembly:

- **Considered**: Active, verified skills in the skill store whose preconditions match the environment.
- **Selected**: Skills that pass Agent Contract scope boundaries, relevance thresholds, and profile quantity limits.
- **Loaded**: Skills whose content was read from disk (compact summary or full runbook).
- **Injected**: Skills that fit within the remaining token budget and were placed into prompt context.

### Progressive Loading

When `progressive_loading` is enabled (Medium and High profiles):
- Candidates with relevance scores between `min_relevance` and `full_detail_threshold` are formatted as concise, actionable summaries (~40–60 tokens).
- Candidates with relevance scores exceeding `full_detail_threshold` have their complete multi-step runbooks loaded (~150–350 tokens).
- High profile sets `full_detail_threshold = 0.15`, allowing full instructions for relevant candidates up to the 1500-token budget.

---

## 3. Configuration & Operator Controls

### `settings.json` Persistence
The central execution profile is stored under `.sync/settings.json` (or global config):
```json
{
  "execution_profile": "medium",
  "providers": { ... },
  "roles": { ... }
}
```

Supported values: `"low"`, `"medium"`, `"high"` (case-insensitive). Invalid or missing values automatically fall back safely to `"medium"`.

### Interactive TUI Commands
- `:profile` — Displays the active execution profile and current policy details (limits, budget, verification depth).
- `:profile low` — Sets execution profile to Low for subsequent execution cycles.
- `:profile medium` — Sets execution profile to Medium.
- `:profile high` — Sets execution profile to High.
- `:status` — Displays the active profile alongside session, progress, blocker, and phase information.
- `:settings` / `[P]` — Opens the interactive settings panel; pressing `[P]` cycles profiles (Low -> Medium -> High -> Low). Pressing `[S]` persists changes.

---

## 4. Optional Laya Local Decision Engine Adapter

The `LayaDecisionAdapter` integrates NandhaKishorM/laya (`https://github.com/NandhaKishorM/laya`) as an optional local System-1 decision engine.

### Isolation and Governance:
- **Disabled by default**: `LayaMode.DISABLED`.
- **Zero auto-downloads**: Never attempts to download HuggingFace/PyTorch weights during startup or normal operations.
- **Offline operation**: Does not make network calls.
- **Deterministic fallback**: Uses keyword and structure heuristics to classify task domains (`testing`, `backend`, `frontend`, `architecture`, `gitops`) when Laya is disabled, missing dependencies, or unconfigured.
- **Shadow evaluation mode**: `LayaMode.SHADOW` runs predictions in the background for comparison logging without affecting production execution paths.
- **No authority bypass**: Laya has zero permission to authorize tool calls, bypass contracts, or approve releases.

---

## 5. Execution Cycle Telemetry

Every execution cycle compiles an auditable telemetry payload attached to the report meta and emitted as a `cycle.telemetry` event:

```json
{
  "execution_profile": "medium",
  "cycle_start": "2026-10-12T18:40:00.000000",
  "latency_ms": {
    "llm": 420,
    "poll": 15,
    "retrieval": 22,
    "write": 5,
    "total_wall_ms": 462
  },
  "completion_tokens": 184,
  "prompt_tokens": 1120,
  "tokens_measured": true,
  "skill_candidates_considered": ["python-refactoring", "pytest-fixtures"],
  "skill_candidates_selected": ["python-refactoring"],
  "skill_loaded": ["python-refactoring"],
  "skill_injected": ["python-refactoring"],
  "skill_token_estimate": 108,
  "skill_token_usage_measured": false,
  "model_calls": 1,
  "tool_calls_count": 3,
  "verification_passed": true,
  "status": "completed",
  "blockers": []
}
```

---

## 6. Calibration Benchmark Results

Measured using `benchmarks/profile_calibration_benchmark.py` across representative tasks (REST endpoints, unit testing, architecture decomposition):

| Profile | Avg Skills Injected | Avg Tokens (Est) | Token Budget | Retrieval Latency | Verification Depth |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **LOW** | 0.0 | 0.0 | 0 | 0.00 ms | Standard |
| **MEDIUM** | 1.3 | 97.7 | 600 | 15.67 ms | Standard |
| **HIGH** | 2.3 | 234.3 | 1500 | 5.00 ms | Extended |

All 51 regression tests across settings, providers, skill storage, and retrieval integration pass cleanly.
