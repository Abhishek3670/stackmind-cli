 Project understanding

 StackMind is a governed multi-agent engineering runtime, not merely an LLM wrapper. Its central idea is:

 ```text
   Human goal
     → CLI/TUI
     → daemon + deterministic lifecycle supervisor
     → planning and human approval
     → work orders/contracts
     → governed agent execution
     → QA/verification
     → GitOps release
 ```

 The repository currently contains substantial implementation for runtime governance, orchestration, knowledge compilation, harness execution, and the TUI.

 ────────────────────────────────────────────────────────────────────────────────

 1. Main architectural layers

 ### CLI — cli/

 cli/main.py is the Click entry point. Major command groups include:

 - init — create a governed project instance
 - validate — schema, structure, protocol, boot, and knowledge checks
 - doctor — runtime health and compatibility
 - migrate — runtime migrations
 - lock — canonical write-lock management
 - shutdown — governed session shutdown
 - promote — promote worker boot drafts to canonical snapshots
 - graph — knowledge compilation and querying
 - harness — execute one governed worker turn
 - daemon — manage the local runtime daemon
 - tui — start the terminal control plane
 - experience, learn, skill — procedural learning and skill management

 The package metadata identifies the current project version as 3.7.0 in:

 - pyproject.toml
 - VERSION
 - VERSION.md
 - CHANGELOG.md

 The runtime schema itself is version 3.1.0, recorded in .sync/RUNTIME_VERSION. These are separate version concepts: package/release version versus initialized runtime protocol version.

 ────────────────────────────────────────────────────────────────────────────────

 ### Runtime daemon — validators/kernel/daemon/

 The daemon provides a local JSON-RPC runtime boundary.

 Relevant pieces:

 - server.py — local server
 - protocol.py — request/response protocol
 - manager.py — sessions, plans, operations, cancellation, persistence
 - storage.py — crash-safe JSON state persistence
 - events.py — runtime events
 - supervisor.py — product lifecycle state machine

 Daemon state is stored below:

 ```text
   .sync/runtime/daemon/
   ├── daemon-state.json
   └── daemon.pid
 ```

 The default daemon port is 8765.

 ────────────────────────────────────────────────────────────────────────────────

 ### Lifecycle Supervisor — validators/kernel/daemon/supervisor.py

 LifecycleSupervisor is the deterministic product-delivery state machine.

 Its phases are:

 ```text
   INIT
    → PLANNING
    → AWAITING_APPROVAL
    → AUTHORING
    → DISPATCHING
    → EXECUTING
    → INTEGRATION_REVIEW
    → PRODUCT_READY
    → GITOPS
    → COMPLETE
 ```

 It also supports:

 ```text
   FAILED
   BLOCKED
 ```

 Important behavior implemented in the supervisor includes:

 - human approval and rejection of plans;
 - work-order dependency checking;
 - worker dispatch;
 - concurrent sibling operations;
 - retry limits;
 - contention handling;
 - crash recovery and operation rediscovery;
 - deliverable existence checks on disk;
 - QA verdict checks;
 - integration review;
 - GitOps completion and release commit tracking.

 The supervisor is intentionally deterministic: it reads persisted state and decides which transition or operation is valid next. It is not itself an LLM agent.

 ────────────────────────────────────────────────────────────────────────────────

 ### TUI — cli/tui/

 The TUI is a client/control surface over the daemon and supervisor rather than a separate source of truth.

 It includes views for:

 - project phase;
 - agent roles and execution backends;
 - work orders;
 - operation trees;
 - activity streams;
 - contracts;
 - diffs;
 - verification matrices;
 - plan approval and rejection;
 - completion handover;
 - streamed assistant output.

 The main implementation is cli/tui/app.py, with supporting layout, state, keyboard, event, governance, chat, and runtime-panel modules.

 ────────────────────────────────────────────────────────────────────────────────

 2. Governance model

 The project’s governing rules are in AGENTS.md.

 The important model is:

 ```text
   CEO / human
      ↓
   Claude — architecture, planning, contracts, work orders
      ↓
   Gemma — QA and approval gates
      ↓
   Codex / Gemini / Local-LLM — implementation and release work
 ```

 The runtime uses:

 - identity-specific agents;
 - work orders;
 - YAML contracts;
 - explicit allow/deny path scopes;
 - file and token budgets;
 - a canonical write lock;
 - worker drafts;
 - validation before and after promotion;
 - inbox/outbox communication;
 - handoff reports;
 - shutdown receipts;
 - Git-backed runtime state.

 The intended authority boundary is fail-closed: an agent should not be able to write outside its contract or bypass the runtime gates.

 .sync/ is the durable coordination system. It currently contains:

 ```text
   .sync/
   ├── agents/
   ├── contracts/
   ├── decisions/
   ├── experience/
   ├── handoffs/
   ├── inbox/
   ├── knowledge/
   ├── outbox/
   ├── reports/
   ├── reviews/
   ├── runtime/
   ├── skills/
   ├── state/
   └── work-orders/
 ```

 The main repository and .sync are separate Git repositories, as described in .sync/SYSTEM_CONTEXT.md.

 ────────────────────────────────────────────────────────────────────────────────

 3. Harness execution

 The governed worker path is centered on:

 ```text
   cli/harness.py
     → validators/harness/runner.py
     → contract/context/tool/runtime gates
     → staged workspace
     → verification
     → validated write-back
 ```

 AgentRunner handles:

 1. discovering an inbox item or assigned work order;
 2. loading the relevant contract;
 3. checking protocol citizenship;
 4. assembling graph context;
 5. creating a scratch workspace;
 6. invoking a provider;
 7. processing tool calls;
 8. validating the model decision;
 9. comparing declared files with actual staged changes;
 10. evaluating verification dimensions;
 11. applying only verified changes;
 12. writing reports and work-order artifacts under governance.

 The provider/tool layer is implemented in:

 - validators/kernel/providers/gateway.py
 - validators/kernel/providers/adapter.py
 - validators/kernel/tools.py
 - validators/kernel/workspace.py
 - validators/kernel/sandbox.py
 - validators/kernel/contract.py

 Available governed tools include:

 - read_file
 - write_file
 - run_command
 - query_graph

 Important safeguards include:

 - scratch-only file operations;
 - path traversal prevention;
 - contract authorization;
 - interpreter denylisting;
 - token budgets;
 - tool-call limits;
 - repeated-call and pathological-loop detection;
 - consecutive failure limits;
 - staged-diff verification;
 - no-op deliverable prevention;
 - contract-specific authoring validation for work orders and contracts.

 The CLI currently constructs AgentRunner with EchoLLMProvider by default. The provider gateway and adapter architecture also supports real model integrations, including the Ollama-related implementation
 described in the changelog.

 ────────────────────────────────────────────────────────────────────────────────

 4. Knowledge compiler and Knowledge API

 Unlike some older planning documents suggest, the knowledge subsystem is present in the current source tree under validators/knowledge/.

 ### Compilation pipeline

 The implemented compiler includes:

 ```text
   Python/source files
     → parse
     → symbol resolution
     → framework-specific augmentation
     → IR
     → knowledge node documents
     → projections and indexes
 ```

 Relevant modules include:

 - validators/knowledge/compiler/parse.py
 - resolve.py
 - ir.py
 - incremental.py
 - rename.py
 - framework compilers for FastAPI, Django, Pydantic, SQLAlchemy, Celery, Alembic, tests, configuration, CI/CD, and others;
 - storage.py
 - writer.py
 - registry.py
 - projections/
 - api.py
 - enricher.py
 - enricher_queue.py

 ### Symbol registry

 validators/knowledge/registry.py gives symbols stable IDs derived from their first-seen birth key:

 ```text
   TYPE-<first 16 hex characters of SHA-256(path:qualified_name)>
 ```

 The registry preserves identity across aliases, renames, and moves. Registry files are sharded under:

 ```text
   .sync/knowledge/registry/
 ```

 ### Storage

 The compiled knowledge store uses:

 ```text
   .sync/knowledge/
   ├── registry/     # canonical symbol identity
   ├── nodes/        # compiled symbol/runtime nodes
   ├── revisions/   # revision and provenance records
   ├── cache/       # search/reverse-index/metrics caches
   └── enrichment/  # background enrichment queue
 ```

 The current workspace contains approximately:

 - 9,989 knowledge nodes
 - 73,596 edges
 - 30 revisions
 - 0 reported diagnostics
 - reported resolved ratio: 0.7258

 The Knowledge API in validators/knowledge/api.py is read-oriented and supports:

 - lookup;
 - filtering;
 - traversal;
 - callers;
 - impact;
 - flows;
 - search;
 - context assembly;
 - contract-scoped access filtering;
 - revision and Git metadata;
 - stale-state reporting;
 - semantic fallback behavior.

 The current graph context output reported:

 ```text
   revision: 30
   git_commit: 4223f4c84bc2996037ca4c6e9c3ae59b6a9faa79
   stale: False
   semantic: False
 ```

 semantic: False indicates the particular query used deterministic/text retrieval rather than embedding-based semantic retrieval.

 ────────────────────────────────────────────────────────────────────────────────

 5. Procedural learning and enrichment

 The repository also contains:

 - validators/experience/
 - validators/learning/
 - validators/skill/

 These support experience records, pattern mining, skill storage, retrieval, decay, and verification.

 The enrichment subsystem is separate from deterministic compilation. It maintains queued jobs and budget counters under .sync/knowledge/enrichment/.

 The intended architectural boundary is:

 ```text
   Deterministic compiler facts
     → optional AI summaries/embeddings
 ```

 AI-derived information is meant to remain advisory and separately tagged rather than becoming authoritative graph structure.

 ────────────────────────────────────────────────────────────────────────────────

 6. Important documentation caveat

 There is a significant difference between the current source and some architecture documents.

 docs/STACKMIND_ARCHITECTURE.md and docs/SKC-STATUS.md describe the knowledge compiler, harness, TUI, and related systems as planned or pre-implementation. However:

 - the current source contains those implementations;
 - CHANGELOG.md documents the 3.6.0 and 3.7.0 runtime/harness work;
 - .sync/knowledge/ contains a populated compiled graph;
 - stackmind graph commands are registered and operational.

 Therefore:

 - use the architecture handbook for design principles and intended invariants;
 - use current source, changelog, and runtime artifacts for implementation status;
 - treat the older SKC/RFC planning documents as design history unless confirmed against code.

 The clearest current mental model is:

 ```text
   Git repository + governed .sync state
           ↓
   knowledge compiler and runtime projections
           ↓
   read-only Knowledge API
           ↓
   contract-bound harness/agents
           ↓
   validated writes back to repository or .sync
 ```

 ────────────────────────────────────────────────────────────────────────────────

 7. Verified Autonomous Lifecycle & Tool Validation Matrix

 In live end-to-end integration testing against real LLM providers (`ollama/gemma4:31b-cloud`), StackMind demonstrated complete autonomous product delivery:
 Goal: *"build a self-contained token bucket rate limiter module in app/rate_limiter.py with unit tests in tests/test_rate_limiter.py, verify test coverage with QA, and record a release commit."*

 The complete lifecycle ran through the deterministic Supervisor and TUI, verifying all major tool groups, protocol endpoints, and governance gates:

 ### Tested Tools & Subsystems

 1. **Governed Agent Tools (`validators/kernel/tools.py`, `gateway.py`, `runner.py`)**:
    - `write_file`: Scoped write operations bound by contract permissions. Used by Claude to author child WOs/contracts, Codex to implement `app/rate_limiter.py` and `tests/test_rate_limiter.py`, and Local-LLM to generate release notes.
    - `read_file`: Scoped read operations. Used by Claude to inspect `PLAN.md` and deliverables during integration review, and Gemma to inspect code and tests during QA.
    - `run_command`: Sandboxed command execution in designated project virtual environment (verified test execution via `pytest tests/test_rate_limiter.py` without environment escape).
    - `query_graph`: Graph context retrieval and contract-checked symbol scoping.

 2. **Daemon Protocol & API Endpoints (`validators/kernel/daemon/`)**:
    - `session.create` / `session.start`: Session scaffolding and protocol citizenship initialization.
    - `plan.propose`, `plan.get`, `plan.approve`, `plan.reject`: Architecture plan proposal and human approval handling.
    - `run.get`, `run.approve`, `run.resume`: Lifecycle run state queries, operator sign-offs, and crash-recovery unblocking.
    - `operation.turn` / `session.turn`: Multi-turn role dispatching with cancellation tokens and audit journaling.
    - `event.list` / `/events` (SSE): Real-time Server-Sent Events stream with sequence replay and keepalive heartbeats.
    - `role.configureBackend`, `role.list`, `backend.list`: Dynamic backend rebinding and provider resolution.

 3. **Lifecycle Supervisor & Deterministic State Machine (`validators/kernel/daemon/supervisor.py`)**:
    - Complete 10-phase progression: `INIT` → `PLANNING` → `AWAITING_APPROVAL` → `AUTHORING` → `DISPATCHING` → `EXECUTING` → `INTEGRATION_REVIEW` → `PRODUCT_READY` → `GITOPS` → `COMPLETE`.
    - DAG dependency resolution (topological ordering: `WO-001` → `WO-002` → `WO-003`).
    - Dedicated synthesized read-only Integration Review scope (`WO-005`) with bounded schema retries.
    - Transient failure retry logic in GitOps turns and automatic boot recovery from `Phase.BLOCKED`/`Phase.FAILED`.
    - Work order archiving to `.sync/work-orders/COMPLETED/` upon release.

 4. **Runtime Governance & Verification Gates**:
    - `D024Gate`: QA test companion discovery and verdict evaluation.
    - `AuthoringGate`: Work order and contract schema validation.
    - `ContractBoundary`: Strict scope enforcement (blocking out-of-scope edits and fail-closed path boundaries).
    - `stackmind validate`: State-directory collision detection (`ACTIVE` vs `COMPLETED`), ledger consistency (`INDEX.yaml`), and canonical runtime synchronization (`TREE.yaml`).
    - `create_gitops_commit`: Governed GitOps release commit with canonical provenance trailers (`Work-Order`, `Released-By`, `Approved-By`, `Architect`, `Target-Work-Orders`).

 5. **TUI Interactive Commands (`cli/tui/`)**:
    - `:goal`: Autonomous product delivery submission.
    - `:status`: Real-time HUD displaying active phase, role backends, and work order progress.
    - `:approve`: Operator plan authorization gate.
    - `:resume`: Supervisor recovery and unblocking.