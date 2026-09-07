# Changelog

All notable changes to the **StackMind** platform will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [3.1.1] - 2026-09-06

### Changed
- **Lean Dependency Surface:** Removed unused heavy dependencies `libcst` and `jedi` from runtime requirements in `pyproject.toml` (saving ~100MB download weight) since the compiler uses Python standard library `ast`; packaged `sentence-transformers` under optional `embeddings` extra (`pip install "stackmind[embeddings]"`).
- **$O(1)$ Knowledge Health Stats:** Wired cached projection `summary.json` directly into `_graph_stats` and `stackmind graph stats` to provide instant graph completeness metrics (`resolved_ratio`, `diagnostics`, and `diagnostics_by_code`) without re-parsing raw IR nodes.
- **Topological Sorting via Standard Library:** Replaced hand-rolled Kahn's algorithm in `cli/graph.py` with Python 3.9+ `graphlib.TopologicalSorter`.
- **Safe Boot Snapshot Discovery:** Modernized agent discovery in `cli/validate.py` using `Path.glob("*.boot.yaml")` and `.removesuffix(".boot")`.
- **Consolidated Signature Parsers:** Replaced repetitive semicolon-and-equals string splitting across 5 CLI formatters with a shared `_parse_kv_parts` generator in `cli/graph.py`.

### Fixed
- **Package Schema Fallback:** Fixed schema resolution in validators (`contract.py`, `validate.py`, `models.py`) to fall back to the internal package schema directory, eliminating the requirement for a copied `schemas/` folder in consumer workspaces.

---

## [3.1.0] - 2026-09-01

### Added
- **Phase 0 Verification & Trust Foundation (`PLAN_PROCEDURAL_LEARNING.md` / `FINAL.md §28`):**
  - Runner-owned `WorkspaceSnapshot` and `WorkspaceDiff` engine (`validators/harness/snapshot.py`) providing authoritative before/after filesystem change detection independent of LLM claims.
  - Observed vs. declared change set validation detecting unannounced modifications and phantom declarations.
  - Contract scope enforcement on actual observed filesystem modifications in `verify_post_execution()` (`validators/harness/contract_gate.py`).
  - Multi-dimensional verification reporting (`VerificationDimensions`) covering scope, state, code, behavioral, security, and outcome dimensions.
  - Canonical 3-level trust eligibility gate (`TrustLevel: OBSERVABLE → VERIFIED → LEARNING_ELIGIBLE`).
  - Dedicated Phase 0 automated test suite (`tests/test_phase0_trust_and_verification.py`).
- **Verified Procedural Learning Roadmap:**
  - Added `PLAN_PROCEDURAL_LEARNING.md` defining the 8-phase procedural learning implementation roadmap.
  - Re-validated and corrected codebase investigation report (`docs/PROCEDURAL_LEARNING_TECHNICAL_INVESTIGATION.md`).
  - Updated `PLAN.md` with procedural learning as the follow-up milestone.

### Fixed
- **Security & Trust Hardening:**
  - Fixed write-lock TOCTOU race in `cli/lock.py:acquire_lock()` using atomic `os.O_CREAT | os.O_EXCL` and atomic `.tmp` replacement.
  - Added automatic audit receipts for force-steals (`.sync/runtime/receipts/LOCK_STOLEN_*.yaml`).
  - Fixed path-traversal vulnerability in `validators/knowledge/enricher.py:_source_excerpt()` with strict workspace root anchoring.
  - Expanded secret redaction patterns to cover GitHub PATs, AWS access keys, connection strings, and private keys.

---

## [3.0.0] - 2026-08-26

### Added
- **CONTRACT-01 Agent Governance & The Contract Layer:**
  - Stateful YAML contracts (`.sync/contracts/WO-xxx.yaml`) defining Agent Identity, allowed/denied subgraphs, and token/file budgets.
  - Fail-closed contract access gate integrated into Knowledge API and Harness Runner.
  - New governance CLI commands: `stackmind graph contract show`, `validate`, `explain-denial`, `scope`.
- **Code-Graph Intelligence Engine:**
  - Runtime call tracing (`sys.setprofile`) capturing dynamically observed `CALLS` edges with execution provenance.
  - Bounded data-flow taint analysis generating `FLOWS_TO` edges from sources to sinks.
  - In-process `LocalEmbeddingBackend` with content-hash cache and lexical fallback.
- **Governed Harness Execution (`HARNESS-01`):**
  - Automated execution loop: poll inbox -> contract check -> assemble context -> call LLM -> validate schema -> verify staged diff -> write-back.
  - Automated D025 destructive operations safeguard gate.
- **Git Repo Isolation:**
  - Guaranteed root `.gitignore` generation and index reset during `stackmind init` ensuring `.sync/` runtime state is never tracked by the outer application repository.
- **5-Layer Integrity Validator Extensions:**
  - Layer 5 knowledge registry invariant checks.
  - Automatic detection of `.sync` leaking into the main project Git index with auto-fix support (`validate --fix`).
  - Automatic reconciliation of canonical drift between `TREE.yaml` and `INDEX.yaml`.
  - Pre-flight protocol validation for handoffs during agent session shutdown.

### Changed
- **Authority Model & Role Separation:** Strictly decoupled Senior Architect (Claude) from implementation; architects write work orders and contracts, while workers implement within contract scope.
- **Compiler Dispatch Optimization:** Consolidated 14 framework compiler augmenters into unified runner loops across `resolve.py` and `incremental.py`.
- **Ponytail Tier 1 Simplifications:** Replaced expensive `deepcopy` calls with dictionary comprehensions, streamlined semver parsing in `doctor`, and eliminated duplicated token estimators.

### Fixed
- Fixed retroactive validation of historical handoff reports in `_read/` archives.
- Fixed CBM Tree-sitter adapter crash on missing binaries with graceful fail-closed behavior.
- Fixed `_normalize_work_order_status` case sensitivity and added support for modern v3.0 work-order properties.

---

## [2.1.0] - 2026-07-15

### Added
- Multi-Language AST frontend adapter integration using Tree-sitter (`cbm`).
- 14 specialized domain compilers: FastAPI, Pydantic, SQLAlchemy, Django, Celery, Alembic, Docs, CI/CD, Configs, Cycles, DeadCode, Health, Impact, and Tests.
- `stackmind graph context` with ranked, bounded token budgeting for AI agents.

---

## [2.0.0] - 2026-06-20

### Added
- **Knowledge Compiler (SKC):** Full-fidelity AST compilation using LibCST and Jedi cross-file static inference.
- **Deterministic 3-Tier Storage:**
  - `T0`: Permanent Symbol Registry with immutable 16-char birth-hashes (`NodeID`).
  - `T1`: Sharded JSON documents storing nodes, edges, and monotonic revision chains.
  - `T2`: Rebuildable derived projections (reverse index, lexical search index, graph metrics).
- **Unified Knowledge API:** Sub-millisecond symbol queries (`lookup`, `callers`, `impact`, `context`).
- **Rename & Alias Tracking:** Permanent node identities survive file renames and structural moves.

---

## [1.2.0] - 2026-05-15

### Added
- **PLAT-03 Advisory Write Locks:** Exclusive lock marker (`.sync/runtime/LOCK`) serializing concurrent agent modifications.
- **PLAT-04 Normalization Audit Trail:** Auto-generated decision records in `decisions/` for canonical mutations.
- **CLAUDE-01 Validated Promotions:** Pre- and post-validation gates for promoting worker draft snapshots.
- **D025 Destructive Operations Safeguard:** Mandatory backup, preconditions check, and CEO escalation before non-reversible actions.

---

## [1.1.0] - 2026-04-10

### Added
- `stackmind shutdown` command with inbox drain enforcement and handoff validation.
- `stackmind migrate` command supporting declarative YAML migration manifests.
- Work order rework budget tracking and escalation workflows.

---

## [1.0.0] - 2026-03-01

### Added
- Initial stable release of StackMind Multi-Agent Runtime.
- Five core agent roles: CEO, Claude (Architect), Codex (Backend), Gemini (Frontend), Gemma (QA), and Local-LLM (GitOps).
- Structured inbox/outbox messaging with `_read/` deduplication.
- Snapshot-based agent boot system (`D021`) reducing token overhead to <3KB per session.
- 4-layer validation CLI (`stackmind validate`, `stackmind doctor`, `stackmind init`).

---

## [0.1.0-alpha] - 2026-01-15

### Added
- Initial proof-of-concept repository scaffold and runtime schemas.
- Prototype work-order lifecycle templates and AGENTS.md authority model.
