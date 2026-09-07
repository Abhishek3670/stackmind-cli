# Deep Technical Code Investigation: Procedural Learning vs. StackMind Codebase

**Document Analyzed**: `StackMind_Verified_Procedural_Learning_FINAL.md`  
**Target Codebase**: `stackmind` (Current `main` branch, v2.1.0-dev / v3.0 Architecture)  
**Investigation Date**: 2026-09-01  
**Investigation Status**: `CODEBASE-VALIDATED ARCHITECTURAL MAPPING`  

---

## 1. Executive Verdict & Core Finding

### Final Verdict
> **ARCHITECTURAL COMPATIBILITY ASSESSMENT: FOUNDATIONS EXIST; LEARNING PRIMITIVES PROPOSED.**
>
> The verified procedural learning architecture proposed in `StackMind_Verified_Procedural_Learning_FINAL.md` is **architecturally compatible with StackMind's design**, but its core procedural-learning primitives are **currently not implemented**.
>
> StackMind contains real, functional foundations for identity, contracts, knowledge compilation, and advisory locking:
> 1. **Symbol Registry & NodeID Birth-Hashing** (`validators/knowledge/registry.py`) — `IMPLEMENTED`
> 2. **Three-Tier Knowledge Storage Model** (`validators/knowledge/storage.py`) — `IMPLEMENTED`
> 3. **Fail-Closed Contract Scope Layer (`CONTRACT-01`)** (`validators/knowledge/contract.py`) — `IMPLEMENTED`
> 4. **Governed Harness Execution Runner (`HARNESS-01`)** (`validators/harness/runner.py`) — `IMPLEMENTED`
> 5. **Five-Layer Runtime Validation Engine** (`cli/validate.py`) — `IMPLEMENTED`
> 6. **Destructive Operations Evaluator (`D025`)** (`validators/harness/d025_gate.py`) — `IMPLEMENTED`
>
> However, the current trust and verification substrate does **not yet independently observe actual filesystem modifications or execute code within a true command-execution sandbox**. Autonomous procedural learning cannot safely be enabled until the **Phase 0 Verification & Trust Foundation** is implemented in code.

---

## 2. Status Classification Standards

To maintain absolute code fidelity, all components are classified under four rigorous labels:

* `IMPLEMENTED`: Code exists, is actively wired into runtime control flow, and is verified by tests.
* `PARTIAL / FOUNDATION EXISTS`: A baseline implementation or compatible abstraction exists, but key production capabilities remain incomplete or unintegrated.
* `DOCUMENTED BUT NOT FULLY IMPLEMENTED`: Rules or protocols exist in markdown documentation (`AGENTS.md`, `demo.md`, `PROTOCOL_DIGEST.md`) but lack automated runtime enforcement in Python.
* `PROPOSED / NOT IMPLEMENTED`: Architectural designs described in `StackMind_Verified_Procedural_Learning_FINAL.md` that do not exist anywhere in the current codebase.

---

## 3. Subsystem-by-Subsystem Technical Code Investigation

### 3.1 Harness Runtime & Change Detection (Phase 0 / §28)

#### Actual Codebase Implementation
* **File**: `validators/harness/runner.py`
  * **Staged-State Validation (`lines 484–509`)**: `AgentRunner._validate_staged_state()` creates an isolated temporary directory via `tempfile.TemporaryDirectory()`, copies the project tree using `shutil.copytree` (excluding `.git`, `__pycache__`, `.pytest_cache`, `.ruff_cache`), applies only `.sync` metadata writes (`_apply_non_report_writes`), and runs `validate_runtime(staged_root)` (`cli/validate.py:validate`).
  * **Command Execution Timing (`lines 351–363`)**: Agent shell commands (`decision.commands`) are **NOT executed inside the staged temporary sandbox**. They are evaluated with `D025Gate` and executed directly against the live `self.project_path` after runtime lock acquisition.
  * **Change Verification (`lines 284–294`)**: Post-execution validation calls `verify_post_execution()`, which inspects the LLM-declared `decision.modified_files` string list.
  * **Actual Filesystem Change Detection**: `NOT IMPLEMENTED`. The runner does not compute `git diff`, `git status`, or workspace file hashes before and after execution.

```text
CURRENT RUNNER FLOW (Actual Code):
LLM Response ──► decision.modified_files (LLM Claim) ──► verify_post_execution() ──► Staged .sync Check ──► Live Lock & Command Run

PROPOSED RUNNER FLOW (Phase 0 Design):
Runner Snapshot (Before) ──► Staged Sandbox Execution ──► Runner Snapshot (After) ──► Authoritative FS Diff ──► Contract Gate
```

#### Document Proposal (§28.2–§28.4)
* **Runner-Owned Change Detection**: The runner must independently derive the actual change set using before/after workspace snapshots (`path`, `size`, `content_hash`) rather than relying on LLM self-declarations.
* **Git Diff Specifics**: Plain `git diff` reliably captures modifications and deletions to tracked files, but does not capture newly created untracked files without `git status` or staging. A runner-owned snapshot engine captures tracked edits, deletions, untracked additions, and non-git directory mutations.
* **Observed vs. Declared Comparison**: If the runner-observed change set differs from LLM declarations, the execution must not be trusted as routine success and must route to Gemma QA review.

#### Classification
* Staged `.sync` metadata validation: `IMPLEMENTED`
* Independent runner-observed filesystem change detection: `PROPOSED / NOT IMPLEMENTED`
* Staged shell command execution sandbox: `PROPOSED / NOT IMPLEMENTED`

---

### 3.2 Contract Gate & Enforcement (`CONTRACT-01`)

#### Actual Codebase Implementation
* **File**: `validators/harness/contract_gate.py`
  * **Pre-Execution Gate (`lines 38–58`)**: `verify_pre_execution()` loads `AgentContract` from `.sync/contracts/<WO-ID>.yaml` or `.sync/agents/<agent>.contract.yaml`, verifying contract existence, work order alignment, and timestamp expiration (`is_expired()`). `IMPLEMENTED`.
  * **Post-Execution Gate (`lines 59–129`)**: `verify_post_execution()` inspects `decision.modified_files` to verify:
    1. Read-only mode violations (`contract.write_mode == "read-only"`).
    2. File count budget limits (`budget.max_files_touched`).
    3. Module deny rules (`contract.deny_rules`).
    4. Module allow rules via BFS graph traversal (`contract.is_node_in_scope()`).
    5. Destructive commands via `D025Gate.evaluate_sequence()`.
* **Critical Finding**: All post-execution checks operate strictly on the **LLM-declared string paths** in `decision.modified_files`. If an agent modifies a file on disk via a shell command without declaring it in `modified_files`, `verify_post_execution()` does not detect it.

#### Document Proposal (§28.4)
* Move the contract boundary from `LLM-declared files → scope validation` to `Actual filesystem changes → scope validation → contract enforcement`.

#### Classification
* Contract validation of declared files: `IMPLEMENTED`
* Contract enforcement on runner-observed filesystem state: `PROPOSED / NOT IMPLEMENTED`

---

### 3.3 Destructive Operations Gate (`D025`)

#### Actual Codebase Implementation
* **File**: `validators/harness/d025_gate.py`
  * **Command Classification (`lines 80–132`)**: Regex and keyword matching for destructive operations (`git reset --hard`, `git filter-repo`, `rm -rf`, `docker prune`, `drop table`), backup patterns (`cp ... backup`, `tar`, `git archive`, `docker tag/export`, `pg_dump`), and verification patterns (`git status`, `git log`, `ls`, `wc -l`, `pytest`, `stackmind validate`).
  * **Sequence Evaluator (`lines 176–242`)**: Requires every destructive command to be preceded by a backup operation and followed by a verification operation.
  * **Audit Logging (`lines 243–265`)**: Appends structured JSON events to `.sync/state/harness/d025_events.jsonl`.
* **Runtime Integration**: Invoked in `validators/harness/contract_gate.py` (`line 123`) and `validators/harness/runner.py` (`line 353`).
* **Operational Discrepancy**: The full protocol in `AGENTS.md` (requiring manual backup archive creation and written CEO inbox approvals for destructive operations) is partially implemented in automated code via pattern heuristics, while human escalation routing remains a documented procedural rule.

#### Classification
* Automated shell command sequence classifier & audit logger: `IMPLEMENTED`
* Full multi-agent CEO escalation approval workflow: `DOCUMENTED BUT NOT FULLY IMPLEMENTED`

---

### 3.4 Runtime Validation Layers (`cli/validate.py`)

#### Actual Codebase Implementation
* **File**: `cli/validate.py`
  * **Docstring Header (`lines 3–8`)**: States 4 validation layers (Schema, Structural, Protocol, Boot Integrity).
  * **Actual Execution (`lines 1427–1438`)**: Executes **5 validation layers** plus a repository link check:
    1. `validate_schema(sync_path, result)` — Layer 1
    2. `validate_structure(project_path, sync_path, agents, result)` — Layer 2
    3. `validate_protocol(sync_path, agents, result)` — Layer 3
    4. `validate_boot_integrity(sync_path, agents, result)` — Layer 4
    5. `validate_sync_ref(project_path, sync_path, result)` — Repo anchor check
    6. `validate_knowledge_layer(project_path, result)` — Layer 5 (invokes `validators/knowledge/validate.py:validate_knowledge()`)
* **Authority**: Code implements 5 layers. The module docstring header is stale.

#### Classification
* 5-layer validation engine: `IMPLEMENTED`
* Layer-6 procedural skill validation: `PROPOSED / NOT IMPLEMENTED`

---

### 3.5 Symbol Registry & Identity (`validators/knowledge/registry.py`)

#### Actual Codebase Implementation
* **File**: `validators/knowledge/registry.py`
  * **NodeID Formula (`lines 59–78`)**: `node_id_for(kind, key)` mints `TYPE-first16(SHA256(path:qualname))`.
  * **Kind Registry (`lines 21–52`)**: `KIND_PREFIXES` contains 28 registered kinds:
    * Code symbols: `MOD`, `PKG`, `CLASS`, `FUNC`, `METH`, `VAR`, `CONST`
    * Framework constructs: `DJMID`, `DJFIELD`, `DJMETA`, `DJSER`, `DJSIG`, `DJURL`, `DJVIEW`, `FAPIAUTH`, `FAPIDEP`, `FAPIMID`, `FAPIROUTE`, `PYCONF`, `PYFIELD`, `PYVALID`, `SATABLE`, `SACOL`, `SAREL`, `SAREPO`, `SASESS`
    * Governance items: `WO`, `DEC`, `REV`, `ISSUE`
  * **Registry Sharding (`lines 109–125`)**: Shards JSON files into two-hex buckets under `.sync/knowledge/registry/xx/ID.json`.
* **Finding**: `SKILL-` and `EXP-` prefixes are **NOT present** in `KIND_PREFIXES`.

#### Classification
* Symbol Registry with deterministic birth-hashing: `IMPLEMENTED`
* Procedural learning kinds (`SKILL`, `EXP`): `PROPOSED / NOT IMPLEMENTED`

---

### 3.6 Storage Subsystem: Canonical Knowledge vs. Experience Storage

#### Actual Codebase Implementation
* **File**: `validators/knowledge/storage.py` & `validators/knowledge/writer.py`
  * **Tier 0 (Registry)**: `.sync/knowledge/registry/xx/ID.json` (canonical identity). `IMPLEMENTED`.
  * **Tier 1 (Nodes & Revisions)**: `.sync/knowledge/nodes/<kind>/<bucket>/<node_id>.json` and sequential revision manifests `.sync/knowledge/revisions/REV-<id>.json`. `IMPLEMENTED`.
  * **Tier 2 (Derived Cache)**: `.sync/knowledge/cache/reverse_index/`, `.sync/knowledge/cache/search/`, and `.sync/knowledge/embeddings/cache/`. `IMPLEMENTED`.
  * **Atomic Writer**: `KnowledgeWriter._write_if_changed()` uses `.tmp` files and `os.replace()` under advisory lock. `IMPLEMENTED`.
* **Experience Storage**: No experience storage directory, experience compiler, or SQLite database exists in the codebase. The file `.sync/knowledge/experience.db` does not exist.

#### Classification
* Three-tier canonical knowledge storage (T0/T1/T2): `IMPLEMENTED`
* SQLite experience database & experience compiler: `PROPOSED / NOT IMPLEMENTED`

---

### 3.7 Retrieval Mechanisms (`validators/knowledge/api.py`)

#### Actual Codebase Implementation
* **File**: `validators/knowledge/api.py` & `validators/harness/retrieval.py`
  * **Graph Traversal (`api.py:279–364`)**: Inbound/outbound graph traversal over `CALLS` and `FLOWS_TO` edges with BFS depth limits. `IMPLEMENTED`.
  * **Reverse Index Lookup (`projections/reverse_index.py`)**: Resolves callers via inverted JSON edge index. `IMPLEMENTED`.
  * **Inverted Index Symbol Search (`projections/search.py`)**: Custom lexical token posting index (`search_symbols()`). `IMPLEMENTED`.
  * **Semantic Vector Search (`api.py:365–428`)**: Computes brute-force cosine similarity over individual JSON embedding files in `.sync/knowledge/embeddings/cache/`. There is no dedicated vector database or ANN index. `PARTIAL / FOUNDATION EXISTS`.
  * **Session Search Tool (`harness/retrieval.py:23–242`)**: `SessionSearchTool` interfaces with external web search or mock search providers for docs lookup. It does **not** search past agent execution sessions. `DOCUMENTED / MISNAMED ABSTRACTION`.
  * **Context Assembly (`api.py:599–691`)**: `assemble_context()` constructs prompt-ready bundles bounded by `token_budget` under active contract rules. `IMPLEMENTED`.
  * **Procedural Skill Retrieval**: `PROPOSED / NOT IMPLEMENTED`.

#### Classification
* Graph traversal, reverse index, lexical search, context assembly: `IMPLEMENTED`
* Vector search: `PARTIAL / FOUNDATION EXISTS` (Brute-force scan over JSON cache)
* Historical session search & skill retrieval: `PROPOSED / NOT IMPLEMENTED`

---

### 3.8 Work Orders, Runtime Evidence & Operating Model

#### Actual Codebase Implementation
* **Operating Model (`demo.md` & `AGENTS.md`)**:
  * StackMind operates through a **Work-Order and `.sync` artifact pipeline**:
    1. Claude (Architect) generates Work Order YAML (`.sync/work-orders/ACTIVE/WO-xxx.yaml`) and Contract YAML (`.sync/contracts/WO-xxx.yaml`).
    2. Codex / Gemini (Workers) boot from snapshots, query the Knowledge API, implement changes, run call tracers (`stackmind analyze runtime`), and write outbox reports.
    3. Gemma (QA) audits diffs against contracts, runs tests, and issues review verdicts.
    4. Local-LLM (GitOps) commits verified state.
* **Persisted Runtime Evidence**:
  * Work orders with lifecycle status logs (`.sync/work-orders/`). `IMPLEMENTED`.
  * Harness run events (`.sync/state/harness/events.jsonl` recording agent, provider, status, latencies, lock timings, context revision). `IMPLEMENTED`.
  * D025 command gate events (`.sync/state/harness/d025_events.jsonl`). `IMPLEMENTED`.
  * Agent outbox markdown reports (`.sync/outbox/<agent>/harness-*.md`). `IMPLEMENTED`.
  * Review requests and completion notices (`.sync/inbox/gemma/`, `.sync/inbox/claude/`). `IMPLEMENTED`.
* **Trajectory Limitations**: Current runtime events do not record step-by-step intermediate shell stdout/stderr, full AST edit patches, or structured user-correction events.
* **Interactive User Corrections**: `FUTURE COMPATIBILITY`. StackMind is batch work-order driven; real-time interactive correction streams are not implemented.

#### Classification
* Work Order and `.sync` governance artifacts: `IMPLEMENTED`
* Full execution trajectory recording for learning: `PARTIAL / FOUNDATION EXISTS`
* Interactive user-correction event streams: `PROPOSED / FUTURE COMPATIBILITY`

---

### 3.9 QA & Gemma Review Path

#### Actual Codebase Implementation
* **File**: `validators/harness/runner.py` (`lines 538–554`)
  * When a work order decision is marked `completed`, `AgentRunner` automatically creates a markdown review request in `.sync/inbox/gemma/<date>_<agent>_<wo_id>-review.md`. `IMPLEMENTED`.
* **Review Execution**:
  * Gemma's QA review process (running `pytest`, validating contracts, writing verdicts back to Claude's inbox) is an **agent protocol** specified in `AGENTS.md` and `demo.md`.
  * Automated programmatic routing of filesystem mismatches directly to Gemma without human intervention is `PROPOSED / NOT IMPLEMENTED`.

#### Classification
* Review request artifact generation: `IMPLEMENTED`
* Gemma review workflow: `DOCUMENTED / AGENT PROTOCOL`
* Automated mismatch routing to QA: `PROPOSED / NOT IMPLEMENTED`

---

### 3.10 Security & Trust Debt Analysis

| Security / Trust Area | Current Codebase Implementation | Location in Code | Exposure / Status | Proposed Phase 0 Resolution |
|---|---|---|---|---|
| **TOCTOU / Write Lock** | File-based advisory lock (`acquire_lock`/`release_lock`). Does not prevent external direct OS writes. | `cli/lock.py:12–110` | `PARTIAL / ADVISORY ONLY`: Safe for compliant agents; vulnerable to unmanaged processes. | Atomic staging validation + lock enforcement before live write-back. |
| **Path Traversal** | Basic path normalization (`replace("\\", "/").strip("/")`). | `validators/knowledge/registry.py:80`, `contract.py:24` | `PARTIAL`: No strict chroot boundary checking on all arbitrary runner paths. | Strict workspace root anchoring and path resolution checks. |
| **Secret Redaction** | Regex pattern matching for API keys and Bearer tokens. | `validators/knowledge/enricher.py:24–27`, `validators/harness/retrieval.py:113` | `PARTIAL / FOUNDATION EXISTS`: Applied to AI summaries and search snippets; not global. | Comprehensive input/output redaction filter across all runtime events. |
| **Prompt Injection** | Basic sanitization of external search snippets in `retrieval.py`. | `validators/harness/retrieval.py:112–129` | `PARTIAL`: Historical logs/outbox files not strictly isolated from instructions. | Hard isolation: historical artifacts treated strictly as data, never executable prompts (§28.8). |
| **Destructive Commands** | Command sequence evaluator requiring pre-backup and post-verification. | `validators/harness/d025_gate.py:77–242` | `IMPLEMENTED`: Actively guards runner shell executions. | Maintain D025 gate as mandatory pre-execution check. |

---

### 3.11 Performance Claims & Snapshot Overhead

#### Codebase Analysis & Overhead Tradeoffs
* **Existing Overhead**: The runner already executes `shutil.copytree()` across the entire repository directory inside `_validate_staged_state()` on every single run (`runner.py:487–497`).
* **Proposed Snapshot Overhead**: Adding before/after workspace change detection introduces additional filesystem `stat` traversals and SHA-256 content hashing.
* **Architectural Tradeoff**:
  * *Benefit*: Authoritative, independent detection of actual changes without trusting LLM claims.
  * *Cost*: Additional I/O latency on large codebases.
  * *Mitigation*: Scoped traversal limited to paths permitted by active Contract allow rules, metadata-first checks (`mtime`, `size`), and content hashing only when metadata indicates potential modification.

---

## 4. Strict Codebase-vs-Proposal Gap Analysis Table

| Component | Current Codebase Status | Evidence (File & Symbol) | Proposed Requirement (`FINAL.md`) | Gap Description | Confidence |
|---|---|---|---|---|---|
| **Symbol Identity** | `IMPLEMENTED` | `validators/knowledge/registry.py:node_id_for` | Stable birth-hash IDs for all entities | Add `SKILL-` and `EXP-` to `KIND_PREFIXES` | High |
| **Work Orders** | `IMPLEMENTED` | `cli/validate.py:_iter_work_order_files` | Work-order-centric learning unit | Add structured trajectory linkage to work orders | High |
| **Runtime Events** | `IMPLEMENTED` | `validators/harness/runner.py:_build_events_write` | Execution logging for distillation | Capture detailed AST diffs and tool stdout | High |
| **Actual FS Change Detection** | `NOT IMPLEMENTED` | `validators/harness/runner.py:run_once` | Runner-owned before/after workspace diff | Build `WorkspaceSnapshot` and diff engine | High |
| **Contract Scope Validation** | `PARTIAL` | `validators/harness/contract_gate.py:verify_post_execution` | Scope check on actual changes | Validate runner-observed changes instead of LLM string claims | High |
| **D025 Command Safety** | `IMPLEMENTED` | `validators/harness/d025_gate.py:D025Gate` | Destructive command safeguards | Fully functional; integrate into skill execution checks | High |
| **Experience Store** | `NOT IMPLEMENTED` | *None* | Canonical `.sync/experience/` records | Create experience artifact schema and storage | High |
| **SQLite Experience Index** | `NOT IMPLEMENTED` | *None* (`experience.db` does not exist) | Rebuildable T2 experience index | Build SQLite schema, compiler, and FTS engine | High |
| **Pattern Miner** | `NOT IMPLEMENTED` | *None* | Clustering recurring episodes ($N \ge 3$) | Build offline episodic pattern mining engine | High |
| **Skill Distiller** | `NOT IMPLEMENTED` | *None* | Candidate skill extraction | Build procedure distillation and schema generator | High |
| **Skill Replay** | `NOT IMPLEMENTED` | *None* | Historical trajectory verification | Build replay test harness and execution sandbox | High |
| **Canary Testing** | `NOT IMPLEMENTED` | *None* | Shadow/canary execution on live tasks | Build parallel execution and comparator gate | Medium |
| **Skill Versioning** | `NOT IMPLEMENTED` | *None* | Versioned `.sync/skills/` with rollback | Create skill storage layout and rollback CLI | High |
| **Skill Decay & Staleness** | `NOT IMPLEMENTED` | *None* | Continuous revalidation and decay | Build staleness triggers linked to code/test changes | High |

---

## 5. Implementation Roadmap & Recommended Sequence

To build the proposed system without disrupting the existing codebase, work must proceed in the strict order defined in Section 28 of the FINAL design document:

```text
STEP 1: Phase 0 — Verification & Trust Foundation (PREREQUISITE)
  ├── 1.1 Harden advisory write lock and path safety boundaries
  ├── 1.2 Implement WorkspaceSnapshot (before/after filesystem delta derivation)
  ├── 1.3 Update verify_post_execution() to enforce contracts on observed changes
  ├── 1.4 Implement explicit Multi-Dimensional Verification flags
  └── 1.5 Implement Learning Eligibility Gate (OBSERVABLE → VERIFIED → LEARNING-ELIGIBLE)

STEP 2: Experience Capture & Compilation (Phases 1 & 2)
  ├── 2.1 Register EXP- kind in SymbolRegistry
  ├── 2.2 Define canonical experience artifact schema in schemas/
  ├── 2.3 Build SQLite T2 experience compiler (.sync/knowledge/experience.db)
  └── 2.4 Add `stackmind experience compile` CLI command

STEP 3: Skill Storage & Versioning (Phases 3 & 4)
  ├── 3.1 Register SKILL- kind in SymbolRegistry
  ├── 3.2 Define skill.schema.json and .sync/skills/ storage hierarchy
  └── 3.3 Implement `stackmind skill list/show/rollback` CLI commands

STEP 4: Pattern Mining & Verification Pipeline (Phases 5 & 6)
  ├── 4.1 Build offline episodic pattern miner (N >= 3 recurrence)
  ├── 4.2 Build skill distiller for candidate generation
  └── 4.3 Build 3-level verification pipeline (Structural → Replay → Canary)

STEP 5: Retrieval & Continuous Lifecycle (Phases 7 & 8)
  ├── 5.1 Connect skill retrieval into KnowledgeAPI.assemble_context()
  ├── 5.2 Implement staleness detection triggers on code/dependency refactors
  ├── 5.3 Implement decay, downgrade, and deprecation lifecycle
  └── 5.4 Add Layer-6 Skill validation in cli/validate.py
```

---

## 6. Final Status

```text
Current implementation: Foundations present (Registry, T0/T1/T2 storage, Contracts, Harness, D025, 5-layer validation).
Learning primitives status: PROPOSED / NOT IMPLEMENTED (Experience store, SQLite index, Pattern miner, Skill distiller, Replay).
Critical blocker: Runner lacks independent observation of actual workspace changes (trusts LLM-declared modified_files).
Recommended implementation order:
  1. Trust / security debt hardening
  2. Runner-owned actual change detection (WorkspaceSnapshot)
  3. Contract enforcement on observed changes
  4. Learning eligibility gate
  5. Experience compilation & SQLite T2 index
  6. Pattern mining & Skill distillation
  7. Replay verification & Canary promotion
  8. Staleness detection & Anti-learning decay
```
