# P7 Runtime Truth: Workspace Isolation, Merge & Promotion Contract

**Date:** 2026-09-11  
**Prerequisites:** PRE-P7-5  
**Author:** Claude (Senior Architect)  
**Status:** **AUTHORITATIVE TRUTH & GOVERNANCE CONTRACT**  
**Governing Plans:** [`PLAN_P7_PREREQUISITES.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_P7_PREREQUISITES.md) §9, [`PLAN_STACKMIND_CLI_FINAL.md`](file:///W:/Aatish/Stuff/stackmind-cli/PLAN_STACKMIND_CLI_FINAL.md)  

---

## 1. Objective & Core Principle

In StackMind P7, multiple agent roles (Codex, Gemini, Local-LLM) execute autonomously and potentially concurrently. To guarantee deterministic outcomes, prevent destructive file collisions, and enforce zero-leakage security, the platform enforces:

> **The Isolated Worktree Invariant**:  
> No worker agent may ever execute uncommitted changes directly in the canonical repository working directory.  
> Every concurrent Work Order executes in a dedicated, isolated Git worktree backed by a dedicated branch. All promotions to the canonical branch are strictly serialized, conflict-checked, and gated by formal QA approval.

```text
                        Canonical Project Repository
                       (Branch: feat/p6-open-source-tui)
                                      │
           ┌──────────────────────────┼──────────────────────────┐
           ▼                          ▼                          ▼
     Worktree A                 Worktree B                 Worktree C
 (.sync/worktrees/WO-020)   (.sync/worktrees/WO-021)   (.sync/worktrees/WO-022)
   Branch: wo/WO-020          Branch: wo/WO-021          Branch: wo/WO-022
     (Agent: Codex)            (Agent: Gemini)           (Agent: Codex)
           │                          │                          │
     6D Verification            6D Verification            6D Verification
           │                          │                          │
           └──────────────────────────┼──────────────────────────┘
                                      │
                                      ▼
                        Promotion Serialization Gate
                           (Advisory Lock PLAT-03)
                         1. Pre-merge Conflict Check
                         2. Regression Test Run
                         3. Atomic Merge to Canonical
                         4. Worktree Cleanup
```

---

## 2. Workspace Ownership & Isolation Model

### 2.1 Worktree Allocation
When a Work Order is authorized and transitioned to `ACTIVE`:
1. The system creates a dedicated branch: `wo/<wo-id>` rooted at the latest verified canonical commit.
2. A dedicated Git worktree is spawned:
   ```bash
   git worktree add -b wo/<wo-id> .sync/worktrees/<wo-id> HEAD
   ```
3. The isolated workspace root is set to `.sync/worktrees/<wo-id>`.

### 2.2 Invariants of Workspace Ownership
- **Strict Single-WO Ownership**: Exactly one Work Order owns a given worktree directory.
- **Operation Re-use**: Multiple operations on the *same* Work Order (e.g. retries, sequential turns) execute in that Work Order's dedicated worktree.
- **Child Operations**: Child operations spawned by a parent Work Order execute in the parent's worktree unless explicitly designated as an independent sub-work-order.
- **Canonical Protection**: The canonical workspace root remains pristine and read-only to worker agents.

### 2.3 Workspace Cleanup Policy
- **On Successful Promotion (`COMPLETED`)**:
  1. The worktree is pruned: `git worktree remove --force .sync/worktrees/<wo-id>`.
  2. The branch `wo/<wo-id>` is deleted or merged.
  3. Ephemeral files (caches, `.pytest_cache`) are purged.
- **On Failure / Rework (`BLOCKED`)**:
  - The worktree is **preserved** to allow forensic investigation by Gemma (QA) or Claude (Architect).
- **On Cancellation (`CANCELLED`)**:
  - Uncommitted staged artifacts are rolled back via `git restore`.

---

## 3. The 5-Stage Promotion Pipeline

Promotion from a worker's isolated worktree into canonical truth must follow this serialized pipeline:

```text
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  1. Execute  │ ──► │ 2. Validate  │ ──► │ 3. Staged Diff   │ ──► │ 4. Conflict Gate │ ──► │ 5. Promote  │
└──────────────┘     └──────────────┘     └──────────────────┘     └──────────────────┘     └─────────────┘
```

### Stage 1: Execution in Worktree
- Worker agent executes strictly within Contract scope (`scope.allow`).
- Dependencies, tests, and code modifications touch only `.sync/worktrees/<wo-id>/`.

### Stage 2: In-Worktree Validation Gate
- Prior to requesting review, the worktree executes:
  - 6-Dimensional Verification: Scope, State, AST, Behavioral, Security, Outcome.
  - Test suite: `pytest` inside the worktree environment.
  - Contract Boundary Check: Observed filesystem diff matched against contract `allow`/`deny`.

### Stage 3: Staged Diff & Review Candidate Production
- Worker issues git commit on `wo/<wo-id>`.
- Generates unified candidate diff.
- Submits review request to Gemma inbox (`.sync/inbox/gemma/<date>_<agent>_<wo-id>-review.md`).

### Stage 4: Conflict Check Gate (Pre-Promotion)
When Gemma approves the work order, Claude prepares promotion under the `PLAT-03` advisory write lock:
1. **Base Commit Freshness**: Compare canonical `HEAD` with the base commit of `wo/<wo-id>`.
2. **3-Way Conflict Evaluation**: If canonical `HEAD` advanced, execute non-destructive conflict detection:
   ```bash
   git merge-tree $(git merge-base HEAD wo/<wo-id>) HEAD wo/<wo-id>
   ```
3. If conflicts exist: Halt promotion (see Section 4).

### Stage 5: Serialized Promotion & Commit
- Acquire canonical write lock (`stackmind lock acquire`).
- Merge branch `wo/<wo-id>` into canonical branch:
  ```bash
  git merge --ff-only wo/<wo-id>  # or clean squash merge
  ```
- Update `.sync/work-orders/INDEX.yaml` and `.sync/runtime/TREE.yaml`.
- Release canonical write lock (`stackmind lock release`).
- Clean up worktree.

---

## 4. Conflict Handling & Resolution Contract

| Scenario | Condition | Handling Protocol | Resulting State |
|---|---|---|---|
| **Disjoint Changes** | Canonical advanced, but touched files do not overlap | Clean rebase or recursive merge performed automatically | `PROMOTED` |
| **Direct File Overlap** | Canonical modified the same file as the worktree | Automatic rebase attempted. If clean, re-verify tests. If conflict markers occur, **reject auto-merge** | `REBASE_REQUIRED` / `BLOCKED` |
| **Semantic Drift** | File changed elsewhere that causes worktree tests to fail upon rebase | Merge halted immediately. Regression captured. Work Order assigned back to worker with diff context | `NEEDS_CHANGES` |
| **Stale Parent Assumptions** | Architectural contract or dependencies updated in canonical | Claude revokes previous contract, issues amended contract, worker re-executes | `RE_CONTRACTED` |

### Non-Overwrite Rule
> **Under no circumstance may a merge conflict be resolved by force-pushing, discarding canonical commits, or silently overwriting another agent's work.**  
> If an automated clean merge cannot be proven, the promotion fails closed, the worktree is preserved, and a conflict notification is dispatched to Claude's inbox.

---

## 5. Failure & Rework Lifecycle

1. **Rework Budget**:
   - Each Work Order specifies `rework_budget` (default: 2).
   - If Gemma issues `NEEDS_CHANGES`, `rework_count` is incremented.
   - The worker re-executes inside the existing worktree, addressing the QA feedback.
2. **Exceeded Budget**:
   - If `rework_count > rework_budget`, the worktree transitions to `BLOCKED_BY_REWORK`.
   - The worktree is **frozen** (no further worker writes permitted).
   - Claude (Architect) must intervene: either extend budget, re-plan the task, or escalate to CEO.
3. **QA Review Mandate**:
   - No failure can be discarded without formal Gemma review.
   - All failure evidence (terminal stderr, failed assertions, contract access rejections) is written to `.sync/reports/` for regression tracking.

---

## 6. Exit Gate Summary

| Gate Item | Verification Criterion | Status |
|---|---|---|
| **Worktree Isolation Spec** | Dedicated `.sync/worktrees/<wo-id>` and `wo/<wo-id>` branch per active WO | **VERIFIED** |
| **Promotion Pipeline** | 5-stage serialized pipeline with pre-merge conflict check and PLAT-03 lock | **VERIFIED** |
| **Conflict Handling** | Zero-overwrite rule, 3-way merge-tree check, fail-closed rebase protocol | **VERIFIED** |
| **Failure Policy** | Worktree preservation on failure, bounded rework budget ($N \le 2$), QA review | **VERIFIED** |

This concludes the architectural readiness gate for **PRE-P7-5**.
