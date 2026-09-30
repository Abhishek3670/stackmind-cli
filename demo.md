# 🚀 StackMind CLI & TUI v3.7.0 — Governed Multi-Agent Engineering Runtime

Welcome to **StackMind v3.7.0**, a compiler-backed, contract-governed multi-agent engineering runtime.

StackMind enables full-stack software development driven by specialized AI agent roles (`claude`, `codex`, `gemini`, `gemma`, `local-llm`), coordinated continuously by a deterministic **Lifecycle Supervisor**, monitored live through an interactive Terminal UI control plane (`stackmind tui`), and powered by an expanded **92-Tool Governed Capability Layer** spanning 14 capability domains, protected by **Code-Graph Intelligence (KNOW-01)**, **Contract Scope Enforcement (CONTRACT-01)**, **Harness Verification (HARNESS-01)**, and **Human-in-the-Loop Governance**.

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

## 🛠️ Governed Agent Capability Matrix & 92-Tool Catalog

StackMind v3.7.0 expands agent capabilities from 4 primitive operations to a comprehensive **92-tool capability surface** across 14 distinct functional domains.

### The 3-Tier Security & Governance Chain
Every tool invocation initiated by any agent must traverse StackMind's invariant verification pipeline before execution:
1. **Role Filtering (`get_tools_for_role`)**: Tool schemas provided in LLM tool-calling prompts are strictly filtered by the agent's role policy. Forbidden tools are omitted from the prompt schema entirely.
2. **Kernel Policy Check (`AuthorizationPolicy.permits(op)`)**: If a model generates an unpermitted tool call, the kernel rejects it with a fail-closed `PermissionError`.
3. **Contract Boundary Check (`ContractEvaluator.authorize(contract, op, target)`)**: Workspace file writes, patches, and command executions must fall strictly within the Work Order's `allow` pattern and outside its `deny` pattern.
4. **Audit Journal (`OperationJournal`)**: Every call (attempt, authorization status, runtime duration, output summary, or denial reason) is immutably journaled.

```text
  LLM Prompt (Role-Filtered Schemas)
                │
                ▼
  Kernel Tool Gateway Dispatcher
                │
                ├─► [Gate 1] Role Authorization Policy   ──► REJECT (if unpermitted for role)
                │
                ├─► [Gate 2] Contract Scope Evaluator    ──► REJECT (if target outside allow/in deny)
                │
                ├─► [Gate 3] Execution Sandbox / Process  ──► EXECUTE (isolated scratch workspace)
                │
                └─► [Gate 4] Operation Audit Journal     ──► RECORD (unforgeable provenance log)
```

### The 14 Capability Domains

| Domain | Description | Canonical Tools | Permitted Roles |
|---|---|---|---|
| **1. Repository & File Discovery** | High-speed multi-file reading, directory trees, glob pattern matching, and regex grep. | `read_file`, `read_many_files`, `list_directory`, `glob`, `grep`, `find_symbol`, `find_references` | Universal (`claude`, `codex`, `gemini`, `gemma`, `local-llm`) |
| **2. Structured Editing & Patching** | Unified diff patching with fuzzy hunk matching, file creation, movement, deletion, and formatting. | `write_file`, `apply_patch`, `move_file`, `delete_file`, `format_file` | `codex`, `gemini`, `local-llm` (metadata only). **Forbidden:** `claude`, `gemma`. |
| **3. Process & Execution Lifecycle** | Managed background processes, status tracking, non-blocking tailing, and test runners. | `run_command`, `process_start`, `process_status`, `process_output`, `process_stop`, `run_tests`, `run_lint`, `run_typecheck`, `run_security_scan`, `cleanup` | `codex`, `gemini`, `gemma`, `local-llm`. Read-only inspect: `claude`. |
| **4. Knowledge Graph Intelligence** | AST symbol lookup, caller/callee graphs, blast-radius impact analysis, and context assembly. | `query_graph`, `get_context`, `find_callers`, `find_callees`, `impact_analysis`, `dependency_analysis`, `semantic_search`, `runtime_evidence`, `data_flow_analysis`, `knowledge_stats` | Universal (Read-only) |
| **5. Planning & Task Orchestration** | Structured task checklists, user clarification modals, plan mode toggles, and work order authoring. | `todo`, `ask_user`, `enter_plan_mode`, `exit_plan_mode`, `create_work_order`, `update_work_order`, `inspect_work_order`, `dispatch_subagent` | Authoring: `claude`. Task tracking & user interaction: `codex`, `gemini`, `gemma`. |
| **6. Governance & Contract Introspection** | Inspecting active contracts, validating scope boundaries, explaining denials, and checking budgets. | `get_contract`, `verify_contract`, `verify_scope`, `explain_denial`, `inspect_budget`, `inspect_agent` | Universal |
| **7. Git Working Tree Inspection** | Status, diffs, blame, commit logs, branch checking, and changed file discovery without mutation. | `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame`, `git_changed_files`, `git_branch` | Universal (Read-only) |
| **8. GitOps & Release Mutations** | Staging deliverables, creating branches, committing with audit trailers, tagging, and releases. | `git_stage`, `git_restore`, `git_create_branch`, `git_commit`, `git_tag`, `git_push`, `create_release`, `rollback_release`, `validate_release_metadata` | **`local-llm` Exclusive**. All other roles strictly denied. |
| **9. QA Verification & Verdicts** | Evaluating deliverables, executing test suites, validating scope diffs, and issuing signed verdicts. | `verify_deliverable`, `verify_tests`, `verify_diff`, `verify_provenance`, `submit_verdict`, `request_changes`, `approve_work_order`, `submit_for_review` | **`gemma` Exclusive** (Verdicts/Approvals). Review requests: `codex`, `gemini`. |
| **10. Procedural Learning & Skills** | Experience search, candidate pattern mining, canary verification, and skill promotion. | `experience_search`, `skill_retrieve`, `skill_list`, `skill_mine`, `skill_promote`, `skill_test`, `skill_audit` | Mining: `claude`. Verification: `gemma`. Retrieval: `codex`, `gemini`. |
| **11. Diagnostics & Checkpointing** | Workspace snapshots, rollback points, process inspection, system metrics, and session summaries. | `checkpoint`, `restore_checkpoint`, `list_checkpoints`, `inspect_logs`, `inspect_processes`, `collect_test_artifacts`, `system_metrics`, `diagnostics_summary`, `inspect_environment`, `compare_snapshots` | Universal |
| **12. Browser & UI Automation** | Headless browser management, navigation, element interaction, and viewport screenshots. | `browser_open`, `browser_navigate`, `browser_click`, `browser_type`, `browser_screenshot`, `inspect_screenshot` | **`gemini` Exclusive** (Frontend Lead) |
| **13. Web & Documentation Retrieval** | Sandboxed external search and documentation fetching. | `web_search`, `web_fetch`, `search_docs` | `claude`, `codex`, `gemini`, `gemma` |
| **14. Versioning & Package Hygiene** | Reading and updating version declarations in `pyproject.toml`, `package.json`, and `CHANGELOG.md`. | `inspect_version`, `update_version`, `update_changelog`, `generate_release_notes`, `prepare_release` | Mutations: **`local-llm` Exclusive**. Inspect: Universal. |

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

In the conversation viewport, Claude calls `get_context` and `find_symbol` on the Knowledge Graph to inspect existing project structure and dependencies without touching source files. Claude initializes structured planning using `enter_plan_mode` and tracks planning milestones with `todo`.

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

Claude executes Turn 2, invoking `create_work_order` and `verify_contract` to author the formal work orders and contracts in `.sync/`:
- `.sync/work-orders/ACTIVE/WO-001.yaml` + `.sync/contracts/WO-001.yaml` (Scaffolding)
- `.sync/work-orders/ACTIVE/WO-002.yaml` + `.sync/contracts/WO-002.yaml` (Backend Lead: `codex`)
- `.sync/work-orders/ACTIVE/WO-003.yaml` + `.sync/contracts/WO-003.yaml` (Frontend Lead: `gemini`)

Claude dispatches assignment notices asynchronously to worker inboxes using `dispatch_subagent`.

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
   - **Codex** launches on thread 1 executing `WO-002`:
     - Calls `read_many_files(["requirements.txt"])` to inspect available frameworks.
     - Calls `write_file("src/backend.py", ...)` or `apply_patch` (using unified diffs with fuzzy hunk matching) to implement the FastAPI login route.
     - Formats code with `format_file("src/backend.py")`.
     - Executes local unit tests with `run_tests("tests/test_backend.py")`.
     - Submits completion notice via `submit_for_review("WO-002", summary="FastAPI login endpoint")`.
   - **Gemini** launches on thread 2 executing `WO-003`:
     - Calls `write_file("src/frontend.html", ...)` to implement the responsive login interface.
     - Calls `browser_open("file://src/frontend.html")` and `browser_screenshot("artifacts/login_preview.png")` to verify UI rendering.
     - Submits completion notice via `submit_for_review("WO-003", summary="Responsive login form")`.
3. Both operations execute concurrently in the daemon's live operation journal:
   ```text
   op_996e4786 (codex)  -> RUNNING (work_order: WO-002)
   op_c94428fe (gemini) -> RUNNING (work_order: WO-003)
   ```
4. **Contract Scope Enforcement**:
   - If `gemini` attempts to call `write_file` or `apply_patch` on `src/backend.py`, the write is rejected with `PermissionError: Path 'src/backend.py' is outside allowed contract scope`.
   - If `codex` attempts to call `git_commit`, the call is immediately blocked by the `codex` role policy.
5. **No-Op Prevention Gate**: The harness verifies that both workers produce real additions to their declared deliverables; empty bookkeeping turns fail closed.

---

### Step 8: QA Verification & Security Checks (Gemma)

Upon turn completion, **Gemma (QA Lead)** performs 3-stage validation using dedicated verification tools:
1. **Preconditions & Deliverables**: Calls `verify_deliverable(wo_id="WO-002")` and `verify_deliverable(wo_id="WO-003")` confirming files exist and are non-empty.
2. **Contract Compliance**: Calls `verify_diff(wo_id="WO-002")` to confirm all changed files fall strictly within the contract's `allow` boundary.
3. **Automated Test Verification**: Calls `verify_tests()` and `run_tests()` to execute test suites and collect provenance evidence via `verify_provenance()`.
4. **AST Security Audit**: Scans `src/backend.py` for hardcoded sensitive comparisons (`password == "admin"`). If insecure patterns are detected, Gemma calls `request_changes(wo_id="WO-002", issues=[...])`, triggering the supervisor's governed retry loop (**S7**).
5. **Approval Verdict**: When all gates pass, Gemma signs off by invoking `approve_work_order("WO-002")` and `submit_verdict("WO-002", verdict="APPROVED", report="...")`.
   *(Note: Gemma is strictly an evaluator; any attempt by Gemma to invoke `write_file` or `apply_patch` is immediately blocked by policy).*

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
1. Staging isolation: Local-LLM calls `git_status()` and `git_diff()` to inspect the workspace, then calls `git_stage(paths=["requirements.txt", "src/backend.py", "src/frontend.html"])` to stage only intended application deliverables.
2. Version hygiene: Calls `validate_release_metadata()` and `update_changelog()` to record the release entry in `CHANGELOG.md`.
3. Provenance commit: Local-LLM invokes `git_commit()` to generate the governed release commit with **immutable audit trailers**:

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
