# 🚀 StackMind CLI & TUI v3.7.0 — Governed Multi-Agent Engineering Runtime

Welcome to **StackMind v3.7.0**, a compiler-backed, contract-governed multi-agent engineering runtime.

StackMind enables full-stack software development driven by specialized AI agent roles (`claude`, `codex`, `gemini`, `gemma`, `local-llm`), coordinated continuously by a deterministic **Lifecycle Supervisor**, monitored live through an interactive Terminal UI control plane (`stackmind tui`), and protected by **Code-Graph Intelligence (KNOW-01)**, **Contract Scope Enforcement (CONTRACT-01)**, **Harness Verification (HARNESS-01)**, and **Human-in-the-Loop Governance**.

---

## 🏛️ Architecture & Governance Model

StackMind enforces the non-negotiable **Single-Source Rule**:
> **There is exactly one authoritative execution path in StackMind: the `AgentRunner` Harness.**  
> Under no circumstances may a daemon, TUI, supervisor, or sub-agent introduce an un-governed tool loop or direct LLM file edit. All prompts, tool invocations, code writes, and verifications strictly traverse the governed `AgentRunner` pipeline within contract boundaries.

```text
                                  USER / CEO
                                      │
                                      ▼
                      Terminal UI Control Plane (:goal / :status)
                                      │  HTTP JSON-RPC 2.0 (:8765/rpc)
                                      ▼
                       LocalDaemon & SessionManager
                                      │
                                      ▼
                         LIFECYCLE SUPERVISOR
                     (Deterministic Orchestration)
   INIT → PLANNING → AWAITING_APPROVAL → AUTHORING → DISPATCHING
   → EXECUTING → INTEGRATION_REVIEW → PRODUCT_READY → GITOPS → COMPLETE
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
    ARCHITECTURE                   BACKEND                     FRONTEND
       Claude                       Codex                       Gemini
  (PLAN & Contracts)         (Backend API / DB)           (UI / Web Pages)
          │                           │                           │
          └───────────────────────────┼───────────────────────────┘
                                      ▼
                                 QA VERIFIER
                                    Gemma
                         (AST, Contracts & Tests)
                                      │
                                      ▼
                              GITOPS & RELEASE
                                  Local-LLM
                       (Release Commit & Provenance)
```

---

## 👥 The Governed Team Roster

StackMind enforces strict separation of concerns and process isolation (**IDE-01**):

| Agent | Role | Authority & Permitted Scope | Explicit Denials & Restrictions |
|---|---|---|---|
| **Claude** | Senior Architect | Product planning, `PLAN.md`, dependency stack decisions, `.sync/work-orders/ACTIVE/`, `.sync/contracts/`, inbox notices. | **MUST NEVER write or edit application source code (`src/**`, `tests/**`).** Delegations must go through Work Orders. |
| **Codex** | Backend Lead | Server-side implementation, APIs, databases, data models, backend scripts (`src/backend.py`, `app/**`). | Denied frontend UI files, contract YAMLs, and dependency manifests unless explicitly assigned by scaffolding WO. |
| **Gemini** | Frontend Lead | Client-side UI/UX, HTML/CSS/JS, React, Flutter, client state (`src/frontend.html`, `ui/**`). | Denied backend APIs, database models, contract YAMLs, and dependency manifests. |
| **Gemma** | QA Lead | Precondition verification, contract boundary audits, AST security scans (hardcoded secrets), verification receipts. | May inspect and run validation tests, but cannot modify application source code or close work orders. |
| **Local-LLM** | GitOps & Release Lead | Package versions (`VERSION.md`, `pyproject.toml`), changelogs, staging verified deliverables, release commits with audit trailers. | May not alter architectural contracts or product scope. Operations subject to D025 safeguards. |

---

## 🔄 The 10-Phase Supervised Lifecycle

The StackMind **Lifecycle Supervisor** (`validators/kernel/daemon/supervisor.py`) deterministically advances each product delivery run through 10 explicit phases:

```text
 1. INIT               Supervisor registers product goal, generates initial run state and session tracking.
 2. PLANNING           Architecture turn (Claude) analyzes codebase with Knowledge API and proposes PLAN.md.
 3. AWAITING_APPROVAL  Execution pauses. Human operator reviews proposed plan and stack decisions.
 4. AUTHORING          On approval, Claude authors child Work Orders and Contracts in .sync/.
 5. DISPATCHING        Supervisor evaluates dependencies and dispatches eligible work orders.
 6. EXECUTING          Workers execute concurrently (S6 Concurrency) within their Contract scopes.
 7. INTEGRATION_REVIEW Architecture validates combined deliverables against original requirements.
 8. PRODUCT_READY      All gates and QA receipts verified; run marked ready for release packaging.
 9. GITOPS             Local-LLM stages verified deliverables and creates release commit with trailers.
10. COMPLETE           Lifecycle finished cleanly; audit receipts recorded in session journal.
```

---

## 🎓 Step-by-Step Tutorial: Building a Login Page with Backend API

This complete walkthrough demonstrates how to take a simple product goal from concept to a committed, verified feature.

### Goal:
> **"Create a simple login page with a backend login endpoint."**

Deliverables expected:
- Dependency manifest: `requirements.txt` (FastAPI, Uvicorn)
- Backend endpoint: `src/backend.py` (`POST /api/login`)
- Frontend login UI: `src/frontend.html` (Responsive login form with credential submission)

---

### Step 1: Initialize Workspace & Knowledge Graph

Open your terminal in your workspace directory:

```powershell
# 1. Activate your Python virtual environment
.\.venv\Scripts\activate

# 2. Initialize StackMind runtime structure (.sync/, contracts, agents)
stackmind init .

# 3. Build the deterministic Knowledge Graph
stackmind graph build -p .

# 4. Verify runtime health across all governance layers
stackmind validate .
```

Expected output:
```text
[PASS] Schema validation
[PASS] Structure validation
[PASS] Protocol compliance
[PASS] Boot integrity
[PASS] Knowledge validation

Runtime is healthy. (v3.7.0)
```

---

### Step 2: Start the Background Daemon & Launch the TUI

StackMind features an autonomous daemon that communicates with the TUI over JSON-RPC 2.0:

```powershell
# Start the Local Runtime Daemon (background process on port 8765)
stackmind daemon start

# Verify daemon status
stackmind daemon status

# Launch the interactive Terminal Control Plane
stackmind tui
```

*(Tip: In headless environments or CI, commands can also be driven directly via `stackmind cli` or JSON-RPC API).*

---

### Step 3: Enter the Product Goal

In the pinned bottom composer of the TUI, submit the product request:

```text
> :goal Create a login page with a backend login endpoint
```

What happens immediately:
1. The daemon creates a new tracked run: `run-xxxx` in `Phase.INIT`.
2. The supervisor synthesizes bootstrap Work Order `WO-000` assigned to **Claude** (Architecture).
3. The supervisor transitions to **`PLANNING`** and dispatches Turn 1 to Claude through the governed Harness.

In the conversation viewport, Claude queries the Knowledge Graph (`stackmind graph context`) to inspect existing project structure and imports without touching source files.

---

### Step 4: Review and Approve the Architecture Plan (HITL Gate)

When Claude completes the planning turn, `PLAN.md` is proposed on disk.  
The Supervisor automatically transitions to **`AWAITING_APPROVAL`** and **halts execution** waiting for human approval.

Check the run state at any time in the TUI:
```text
> :status
```
Output:
```text
Phase: AWAITING_APPROVAL
Run ID: run-9a8b1c2d
Goal: Create a login page with a backend login endpoint
Plan ID: PLAN-001 (Architecture Plan for: Create a login page...)
```

Inspect the proposed architecture plan:
```text
> :plan
```

Claude's plan outlines three explicit tasks:
1. **Scaffolding (`WO-001`)**: Create `requirements.txt` declaring `fastapi` and `uvicorn`.
2. **Backend (`WO-002`)**: Implement `src/backend.py` with password authentication route `POST /api/login`. Depends on `WO-001`.
3. **Frontend (`WO-003`)**: Implement `src/frontend.html` with responsive form and fetch handler. Depends on `WO-001`.

#### Human Decision:
- To approve the plan and commence delivery:
  ```text
  > :approve Looks good, proceed with FastAPI backend and HTML frontend.
  ```
- To request revisions (e.g. if you wanted Flask instead of FastAPI):
  ```text
  > :reject Please use Flask instead of FastAPI.
  ```
  *(A rejection sends feedback back to Claude, resetting the supervisor to `PLANNING` for a revised plan).*

---

### Step 5: Autonomous Work Order & Contract Authoring

Upon receiving `:approve`, the supervisor transitions to **`AUTHORING`**.

Claude executes Turn 2, authoring the formal work orders and contracts in `.sync/`:
- `.sync/work-orders/ACTIVE/WO-001.yaml` + `.sync/contracts/WO-001.yaml` (Scaffolding)
- `.sync/work-orders/ACTIVE/WO-002.yaml` + `.sync/contracts/WO-002.yaml` (Backend Lead: `codex`)
- `.sync/work-orders/ACTIVE/WO-003.yaml` + `.sync/contracts/WO-003.yaml` (Frontend Lead: `gemini`)

Notice the Contract Layer (**CONTRACT-01**):
- `WO-001` contract grants write access **only** to `requirements.txt`.
- `WO-002` contract grants write access **only** to `src/backend.py` and `tests/test_backend.py`.
- `WO-003` contract grants write access **only** to `src/frontend.html`.

The supervisor verifies all work orders exist on disk, transitions to **`DISPATCHING`**, and evaluates the dependency graph.

---

### Step 6: Scaffolding Execution & Dependency Manifest Gate

Because `WO-002` and `WO-003` depend on `WO-001`, the supervisor strictly dispatches **`WO-001` first**:

1. **Codex** runs in the governed harness to write `requirements.txt`:
   ```text
   fastapi>=0.100.0
   uvicorn>=0.22.0
   ```
2. **Dependency Satisfiability Gate**: The harness verifies that `requirements.txt` is authorized by `WO-001`'s contract.
3. **6D Verification Gate**: Validates clean diff writeback.
4. `WO-001` completes and is marked `COMPLETED`.

---

### Step 7: Concurrent Backend & Frontend Worker Execution (S6 Concurrency)

With `WO-001` completed, the supervisor detects that **both `WO-002` (Backend) and `WO-003` (Frontend) are now simultaneously eligible**:

1. **Parallel Dispatch Batching**: The supervisor opens a parent batch operation in `SessionManager`:
   ```text
   batch_operation_id = bd30b93e-0bc4-419f-a530-1ae92ab889ee
   ```
2. **Concurrent Workers Running**:
   - **Codex** launches on thread 1 executing `WO-002`: writes `src/backend.py` (FastAPI login route with credential check).
   - **Gemini** launches on thread 2 executing `WO-003`: writes `src/frontend.html` (interactive login form with clean styling and async fetch).
3. Both operations execute concurrently in the daemon's live operation journal:
   ```text
   op_996e4786 (codex)  -> RUNNING (work_order: WO-002)
   op_c94428fe (gemini) -> RUNNING (work_order: WO-003)
   ```
4. **Contract Scope Enforcement**:
   - If `gemini` attempts to touch `src/backend.py`, the write is rejected with `PermissionError: Path 'src/backend.py' is outside allowed contract scope`.
   - If `codex` attempts to modify `requirements.txt`, the write is blocked: manifest ownership belongs strictly to the scaffolding turn.
5. **No-Op Prevention Gate**: The harness verifies that both workers produce real additions to their declared deliverables; empty bookkeeping turns fail closed.

---

### Step 8: QA Verification & Security Checks (Gemma)

Upon turn completion, **Gemma (QA Lead)** performs 3-stage validation:
1. **Preconditions & Dependency Check**: Checks that required packages in `requirements.txt` satisfy the AST imports (`import fastapi`).
2. **Contract Compliance**: Confirms all modified files match the contract's `allow` boundary.
3. **AST Security Audit**: Scans `src/backend.py` for hardcoded sensitive comparisons (`password == "admin"`). If insecure patterns are detected, a `NEEDS_CHANGES` verdict is written to Codex's inbox, triggering the supervisor's governed retry loop (**S7**).
4. When validation passes, Gemma issues an `APPROVED` verdict.

---

### Step 9: Architecture Integration Review & Product Ready

Once all worker WOs are verified:
1. Supervisor transitions to **`INTEGRATION_REVIEW`**.
2. **Claude** executes an integration turn:
   - Validates that `src/frontend.html` targets the endpoint defined in `src/backend.py` (`POST /api/login`).
   - Verifies the user's original product goal has been completely achieved.
3. Claude emits the `PRODUCT READY` verdict.
4. Supervisor transitions to **`PRODUCT_READY`**.

---

### Step 10: GitOps Release & Provenance Commit (Local-LLM)

The supervisor transitions to **`GITOPS`** and dispatches **Local-LLM**:
1. Staging isolation: Only intended application deliverables (`requirements.txt`, `src/backend.py`, `src/frontend.html`) are staged for release.
2. Runtime state, `.sync/inbox/` notices, and knowledge caches are excluded via release policy.
3. Local-LLM executes the governed release commit with **immutable audit trailers**:

```bash
git show --stat HEAD
```

```text
commit 98b9acfd92c656d892ff4987db44d8c6ae80ae6a
Author: Local-LLM <gitops@stackmind.local>
Date:   Tue Sep 29 18:41:29 2026 +0530

    feat(auth): implement login page with backend authentication endpoint

    - Scaffolding: declared fastapi and uvicorn dependencies
    - Backend: implemented POST /api/login endpoint in src/backend.py
    - Frontend: created responsive login interface in src/frontend.html
    - Verified via Gemma QA Gate and Architecture Integration Review

    Work-Order: WO-002, WO-003
    Released-By: local-llm
    Approved-By: gemma
    Architect: claude
```

4. Supervisor transitions to **`COMPLETE`**.
5. The TUI notifies the user:
   ```text
   ✦ StackMind: Product goal completed successfully. Release commit created.
   ```

---

## 🎮 TUI Commands Reference

Type `:` in the composer to access interactive controls:

| Command | Shortcut | Purpose | Example |
|---|---|---|---|
| `:status` | `:s` | View current supervisor phase, active run ID, and pending operations | `:status` |
| `:plan` | `:p` | View proposed architecture plan (`PLAN.md`) | `:plan` |
| `:approve` | `Ctrl+A` | Formally approve the proposed plan to start worker execution | `:approve Looks good` |
| `:reject` | `Ctrl+R` | Reject the plan with feedback to trigger re-planning | `:reject Use SQLite` |
| `:wo` | `:w` | Inspect active, queued, and completed Work Orders | `:wo` |
| `:roles` | `:a` | Display live agent status (`claude`, `codex`, `gemini`, `gemma`, `local-llm`) | `:roles` |
| `:diff` | `:d` | View live unified diffs for staged files | `:diff` |
| `:matrix` | `:m` | Inspect authentic 6D verification results (Scope, AST, Security, etc.) | `:matrix` |
| `:tree` | | Display hierarchical operation tree (`Session` → `Plan` → `Task`) | `:tree` |
| `:actions` | | Toggle expandable tool actions disclosure group | `:actions` |
| `:rebind <role> <backend> [model]` | | Dynamically switch LLM backend for any agent role | `:rebind codex ollama qwen2.5-coder:14b` |
| `:clear` | `Ctrl+L` | Redraw screen and clear conversation viewport | `:clear` |
| `:quit` | `:q` | Cleanly exit TUI (daemon continues running in background) | `:quit` |

---

## ⚡ Reliability & Fault Recovery

StackMind is built for high-reliability production environments:

### 1. Cooperative Cancellation (`Ctrl+C`)
Pressing `Ctrl+C` sends an authenticated JSON-RPC cancellation event to the active operation. The runner terminates cleanly at the nearest checkpoint without leaving corrupted state or dirty file handles.

### 2. Double-Bounded Operation Contention
If the supervisor encounters transient operation lock contention (another operation winding down), it yields and retries up to `max_contention_retries=50` attempts. If an operation remains stuck longer than 300 seconds, the driver automatically halts and transitions to `Phase.BLOCKED` with an explicit diagnostic message rather than hanging in an infinite loop.

### 3. Crash Recovery & Resumability (S8)
If the machine restarts or the daemon crashes while an operation is running:
- Run state is persisted on disk in `.sync/runtime/runs/<run_id>.json`.
- On daemon restart, the supervisor inspects the session journal, re-adopts in-flight operations, and resumes from the exact phase without re-executing completed work orders.

### 4. QA Retry Loop (S7)
If a worker's implementation fails a test or contract gate:
- Gemma generates a `NEEDS_CHANGES` verdict with detailed logs.
- The supervisor automatically re-dispatches the worker with the feedback.
- Retries are bounded by `max_retries=2`. If all retries fail, the supervisor transitions to `Phase.FAILED` with a diagnostic summary.

---

## 📦 Key CLI Commands Summary

```powershell
# --- Daemon Management ---
stackmind daemon start                    # Start background daemon on port 8765
stackmind daemon status                   # Inspect daemon health and active runs
stackmind daemon stop                     # Gracefully stop the daemon process

# --- Control Plane ---
stackmind tui                             # Launch interactive Terminal UI
stackmind tui --demo                      # Run simulated multi-agent demonstration

# --- Code-Graph Intelligence ---
stackmind graph build -p .                # Compile full repository symbol graph
stackmind graph update -p .               # Fast incremental update after edits
stackmind graph query "login"             # Locate symbol definitions
stackmind graph context "auth endpoint"   # Retrieve ranked context bundle for prompt

# --- Verification & Health ---
stackmind validate .                      # Validate all 5 integrity layers
stackmind doctor .                        # Check interpreter, venv, and agent tools
stackmind shutdown <agent>                # Run clean agent shutdown and write handoff
```

---

*StackMind CLI & TUI v3.7.0 — Autonomous Multi-Role Engineering Delivery Built for Production.*
