# PLAN.md --- Agent Brain Architecture and Implementation Plan

**Status:** Proposed\
**Purpose:** Define a general, implementation-oriented plan for building
an agent brain with skills, short-term memory, long-term memory, and
tool memory.\
**Planning principle:** Build a narrow, testable vertical slice first;
expand only when measured requirements justify it.

------------------------------------------------------------------------

## 1. Vision

Build a modular agent-brain subsystem that helps an agent find and use
the right information for the current task, preserve useful experience
across tasks, discover reusable skills, and select tools safely.

The brain must optimize for **relevant retrieval, correctness,
provenance, authorization, and predictable latency**---not for storing
or injecting as much context as possible.

## 2. Goals

1.  Provide distinct, well-defined interfaces for short-term memory,
    long-term memory, skills, and tool knowledge.
2.  Support structured lookup, keyword search, and semantic retrieval.
3.  Retrieve context selectively according to the current task and its
    permissions.
4.  Preserve provenance, timestamps, ownership, confidence, and
    lifecycle state for stored memories.
5.  Keep executable skills and authoritative runtime state separate from
    descriptive memory.
6.  Make retrieval quality, latency, failures, and memory usefulness
    measurable.
7.  Support safe updates, expiration, deletion, and migration without
    corrupting agent state.
8.  Begin with a small deployable implementation and preserve the option
    to scale components independently.

## 3. Non-goals

-   Replacing the agent runtime, task scheduler, or existing
    authoritative repository/work-order state.
-   Sending all stored memories, tools, skills, or conversation history
    to every model call.
-   Treating vector similarity as proof that a memory is correct or
    current.
-   Giving memory records the authority to grant tool permissions.
-   Introducing multiple databases or a graph database before a
    demonstrated need.
-   Automatically promoting every conversation or tool result into
    permanent memory.
-   Building autonomous memory mutation without validation,
    auditability, and policy checks.

## 4. Core architecture

### 4.1 Components

-   **Memory Router:** Selects the memory domains and retrieval
    strategies relevant to the active task.
-   **Short-Term Memory:** Holds active task state, recent relevant
    events, intermediate results, and checkpoints.
-   **Long-Term Memory:** Stores validated experiences, facts,
    decisions, preferences, and lessons with provenance.
-   **Skill Registry:** Indexes versioned skill artifacts and their
    metadata. The artifact remains the authoritative source of skill
    instructions.
-   **Tool Registry:** Stores tool capabilities, schemas, compatibility,
    permission requirements, and operational metadata.
-   **Tool Execution History:** Records invocation outcomes, errors,
    latency, and relevant diagnostics without storing secrets.
-   **Hybrid Retrieval:** Combines structured filters, lexical search,
    and vector similarity.
-   **Memory Compiler:** Evaluates task outcomes and candidate memories;
    deduplicates, validates, updates, expires, or rejects them.
-   **Context Compiler:** Produces a bounded, ranked, evidence-linked
    context package for the agent.
-   **Policy and Access Layer:** Enforces tenant, project, user, task,
    and tool permissions before data reaches the model.
-   **Observability Layer:** Measures retrieval latency, quality, cost,
    errors, and downstream usefulness.

### 4.2 Logical flow

1.  Receive a task with an identity, scope, and permission context.
2.  Determine which memory domains are relevant.
3.  Apply authorization and structured metadata filters.
4.  Retrieve candidates using direct lookup, keyword search, and/or
    vector search.
5.  Deduplicate, rank, check freshness, and retain provenance.
6.  Compile a small context package within the task's context budget.
7.  Execute the task using authorized skills and tools.
8.  Record outcomes and execution metadata.
9.  Evaluate candidate long-term memories through the memory compiler.
10. Persist only eligible records and emit auditable events.

## 5. Storage strategy

### 5.1 Initial default

Use **PostgreSQL with pgvector and PostgreSQL full-text search** as the
primary store. This minimizes operational complexity while supporting
relational data, transactional writes, metadata filters, keyword search,
and vector similarity.

Use separate tables, schemas, or clearly separated repositories for each
memory domain. Keep domain boundaries explicit even when the data shares
one database.

### 5.2 Storage by domain

  ------------------------------------------------------------------------
  Domain                  Initial storage         Retrieval pattern
  ----------------------- ----------------------- ------------------------
  Short-term memory       PostgreSQL task/session Indexed key lookup
                          state; optional         
                          in-process cache        

  Long-term memory        PostgreSQL + pgvector   Metadata filters +
                                                  lexical/vector retrieval

  Skills                  Version-controlled      Skill ID/version, tags,
                          files or artifact       capability filters,
                          storage; PostgreSQL     optional semantic
                          metadata registry       discovery

  Tool registry           PostgreSQL              Indexed
                                                  tool/capability/schema
                                                  lookup

  Tool execution history  PostgreSQL              Task/tool/time filters
                                                  and aggregated metrics

  Large artifacts and     Filesystem or           Metadata lookup followed
  documents               S3-compatible object    by artifact fetch
                          storage                 

  Cache                   Optional Redis          Frequently read or
                                                  ephemeral state, only if
                                                  measurements justify it
  ------------------------------------------------------------------------

Do not add Redis, a dedicated vector database, or a graph database in
the first vertical slice unless a concrete constraint requires it.

### 5.3 Memory record requirements

Long-term memory records should support, as appropriate:

-   Stable memory ID and memory type
-   Content and normalized searchable representation
-   Source/provenance reference
-   Created and updated timestamps
-   Last-validated timestamp and optional expiration
-   Confidence or evidence quality, with documented semantics
-   Owner, project, tenant, and access-control attributes
-   Status such as candidate, active, superseded, or deleted
-   Links to related tasks, skills, tools, or memories
-   Embedding model/version and embedding status

Treat the schema as a versioned contract. Store vectors in a vector
column, not as JSON text. Avoid storing credentials, access tokens, or
unnecessary sensitive data in searchable memory.

## 6. Memory lifecycle and rules

### 6.1 Short-term memory

-   Scope every record to a task or session.
-   Track current objective, plan status, important observations, tool
    results, and resumable checkpoints.
-   Define retention and expiration policies.
-   Persist critical checkpoints before risky or long-running
    transitions.
-   Do not use semantic search for state that can be retrieved directly
    by ID.

### 6.2 Long-term memory

-   Store durable facts, decisions, lessons, and experiences that are
    likely to improve future tasks.
-   Preserve source references and timestamps.
-   Deduplicate and update existing records rather than blindly
    appending duplicates.
-   Track contradictions and freshness; do not silently merge
    conflicting claims.
-   Support superseding, expiration, deletion, and revalidation.
-   Treat retrieved content as evidence to evaluate, not as trusted
    instructions.

### 6.3 Skills

-   Store skill content as versioned artifacts.
-   Register skill ID, version, description, input/output contracts,
    required capabilities, status, and artifact location.
-   Discover skills through deterministic metadata filters first; use
    semantic retrieval as a supplemental discovery method.
-   Validate a skill's status and required permissions before execution.
-   Keep skill authoring, approval, publication, and deprecation
    explicit.

### 6.4 Tool memory

Separate: 1. **Tool registry:** Available capabilities and current
schemas. 2. **Execution history:** What happened during individual
calls. 3. **Reliability summaries:** Derived success rates, latency
distributions, and known failure patterns.

-   Revalidate tool schemas and permissions at invocation time.
-   Treat historical outcomes as advisory, not as authorization.
-   Redact secrets and minimize sensitive arguments/results in logs.
-   Make retries idempotent where possible and respect tool-specific
    retry rules.

## 7. Retrieval and ranking

Use a staged retrieval pipeline:

1.  **Route:** Identify relevant domains from task type and runtime
    state.
2.  **Authorize:** Enforce access controls before candidate content is
    exposed to the model.
3.  **Filter:** Apply tenant, project, task, type, status, and time
    constraints.
4.  **Retrieve:** Use exact lookup, lexical search, vector search, or a
    combination.
5.  **Fuse:** Combine candidate lists with a documented ranking method.
6.  **Validate:** Consider freshness, provenance, confidence, and
    contradictions.
7.  **Deduplicate:** Merge duplicate candidates without losing source
    references.
8.  **Budget:** Limit records and tokens to the smallest sufficient
    context.
9.  **Return:** Include content, source, status, and relevant metadata.

Do not rank solely by vector similarity. Begin with a simple, testable
ranking function and change it only when evaluation data shows a
benefit.

## 8. Security and governance

-   Apply authorization at query time; never rely on the model to
    enforce access controls.
-   Scope queries to the active identity, tenant, project, and task.
-   Use least-privilege database roles and parameterized queries.
-   Keep secrets in a secret manager or secure runtime configuration.
-   Treat memory, documents, skill files, and tool outputs as
    potentially untrusted input.
-   Defend against prompt injection by separating retrieved content from
    governing instructions and by enforcing tool policy outside the
    model.
-   Audit memory creation, updates, access where required, supersession,
    and deletion.
-   Define retention and deletion propagation for source records,
    derived memories, embeddings, and caches.
-   Version schemas and embedding models; support safe re-embedding and
    rollback.
-   Do not let a memory record or skill artifact grant itself additional
    capabilities.

## 9. Implementation phases

### Phase 0 --- Requirements and baseline

-   Inspect the current runtime, storage conventions, task lifecycle,
    skill format, and permission model.
-   Identify authoritative sources of truth and existing APIs.
-   Define expected scale, concurrency, latency targets, retention
    needs, and deployment constraints.
-   Record current behavior and baseline tests.
-   Produce a short architecture decision record for the initial storage
    choice.

**Exit criteria:** Interfaces, ownership boundaries, security
requirements, and measurable success criteria are agreed upon.

### Phase 1 --- Contracts and schema

-   Define typed contracts for memory records, retrieval
    requests/results, skills, tools, and context packages.
-   Design migrations and indexes.
-   Define status transitions and memory lifecycle rules.
-   Add provenance and authorization fields.
-   Define error handling, timeouts, and idempotency behavior.

**Exit criteria:** Schema and API contracts are reviewed; migrations can
be applied and rolled back safely in a test environment.

### Phase 2 --- Vertical-slice retrieval

-   Implement direct lookup for short-term state.
-   Implement metadata-filtered long-term memory lookup.
-   Add PostgreSQL full-text search and pgvector similarity search.
-   Implement candidate fusion, ranking, deduplication, and bounded
    context output.
-   Return provenance with every retrieved memory.
-   Add a small representative retrieval evaluation dataset.

**Exit criteria:** An end-to-end task can retrieve a relevant,
authorized, bounded context package with reproducible tests.

### Phase 3 --- Skills and tool registry

-   Register and retrieve versioned skills.
-   Validate skill contracts and required capabilities.
-   Register tool schemas and permission requirements.
-   Record tool execution metadata and sanitized outcomes.
-   Add stale-schema and unavailable-tool handling.

**Exit criteria:** The agent can discover a skill and an authorized
tool, validate both, and report execution outcomes without treating
historical memory as permission.

### Phase 4 --- Memory compiler and lifecycle

-   Extract candidate memories from completed tasks.
-   Apply eligibility, provenance, deduplication, and contradiction
    checks.
-   Add update, supersession, expiration, and deletion workflows.
-   Record why a candidate was accepted or rejected.
-   Ensure memory writes are idempotent and recoverable.

**Exit criteria:** Task outcomes can produce governed memory updates
without automatically persisting every message or result.

### Phase 5 --- Runtime integration and resilience

-   Integrate routing and context compilation into the agent lifecycle.
-   Add timeouts, bounded retries, graceful degradation, and
    checkpoints.
-   Ensure unavailable vector retrieval can fall back to structured or
    lexical retrieval where appropriate.
-   Add structured logs, metrics, tracing, and health checks.
-   Test restart and recovery behavior.

**Exit criteria:** The agent can complete supported tasks when optional
retrieval components fail, without bypassing authorization.

### Phase 6 --- Evaluation and optimization

-   Measure retrieval precision/recall at k on a representative test
    set.
-   Measure p50/p95 retrieval latency, error rate, and database resource
    usage.
-   Evaluate stale-memory and contradiction handling.
-   Measure context size and downstream task quality.
-   Tune indexes, query plans, embedding models, and ranking only
    against measured results.
-   Introduce Redis or another specialized store only when evidence
    supports it.

**Exit criteria:** Performance and quality meet targets defined in Phase
0; regressions are covered by automated tests.

## 10. Testing strategy

-   **Unit tests:** Validation, ranking, filtering, deduplication,
    lifecycle transitions, and permission decisions.
-   **Integration tests:** Migrations, PostgreSQL queries, pgvector,
    full-text search, and artifact retrieval.
-   **Contract tests:** Memory APIs, skill manifests, tool schemas, and
    context-package formats.
-   **Security tests:** Cross-project access, unauthorized retrieval,
    prompt-injection payloads, secret redaction, and deletion
    propagation.
-   **Reliability tests:** Timeouts, database outages, retry behavior,
    interrupted writes, restart recovery, and duplicate events.
-   **Evaluation tests:** Known queries with expected relevant memories,
    freshness cases, contradictory facts, and retrieval-quality
    thresholds.
-   **Regression tests:** Existing runtime, task lifecycle, and skill
    behavior must remain intact.

## 11. Observability and success metrics

Establish targets from a baseline rather than assuming universal
thresholds.

Track at minimum:

-   Retrieval p50/p95 latency and timeout rate
-   Retrieval precision/recall at k on a labeled evaluation set
-   Percentage of retrieved records with valid provenance
-   Stale-memory and contradiction rates
-   Unauthorized retrieval test failures (target: zero)
-   Context tokens per task and retrieval cost
-   Memory acceptance, rejection, update, and deduplication rates
-   Skill discovery and execution success rates
-   Tool success rate, latency, and failure categories
-   Task completion quality with and without retrieved context
-   Database query performance, index size, and resource utilization

## 12. Risks and mitigations

  -----------------------------------------------------------------------
  Risk                                Mitigation
  ----------------------------------- -----------------------------------
  Irrelevant memories pollute context Task routing, metadata filters,
                                      ranking evaluation, context budgets

  Stale or contradictory memories     Provenance, validation timestamps,
                                      supersession, contradiction
                                      handling

  Unauthorized data reaches a model   Query-time authorization, scoped
                                      queries, security tests

  Skills become inconsistent with     Versioned artifacts, contract
  runtime                             validation, explicit publication
                                      lifecycle

  Tool history is mistaken for        Separate policy enforcement from
  permission                          descriptive memory

  Embedding changes degrade retrieval Model/version metadata, evaluation
                                      suite, controlled re-embedding

  Overengineering slows delivery      One primary database and a narrow
                                      vertical slice

  Memory writes are duplicated or     Idempotency, transactions,
  lost                                outbox/checkpoint patterns

  Database outage blocks all work     Timeouts, graceful fallback,
                                      explicit degraded-mode behavior
  -----------------------------------------------------------------------

## 13. Initial deliverables

1.  Architecture decision record for storage and memory boundaries.
2.  Versioned schema and database migrations.
3.  Typed domain models and API contracts.
4.  PostgreSQL-backed memory repository.
5.  Hybrid retrieval and ranking service.
6.  Skill and tool registries.
7.  Memory lifecycle/compiler service.
8.  Context compiler integration.
9.  Automated unit, integration, security, and retrieval evaluation
    tests.
10. Operational documentation, configuration reference, and runbook.

## 14. Definition of done

The initial implementation is complete when:

-   All four memory domains have explicit contracts and ownership
    boundaries.
-   Short-term state can be retrieved by stable identifiers.
-   Long-term memory supports structured, keyword, and semantic
    retrieval.
-   Skills are versioned artifacts with validated metadata.
-   Tool registry, execution history, and authorization are separate
    concerns.
-   Retrieved context is permission-filtered, bounded, ranked, and
    traceable to sources.
-   Memory writes support validation, deduplication, update, expiration,
    and deletion.
-   Automated tests cover normal behavior, failure modes, and security
    boundaries.
-   Retrieval quality and latency are measurable in a repeatable
    evaluation.
-   Existing runtime behavior remains compatible, or any breaking change
    is explicitly approved and documented.

## 15. Working rules

-   Inspect the existing codebase before proposing file changes or new
    abstractions.
-   Preserve existing interfaces unless a change is necessary and
    documented.
-   Implement one vertical slice at a time; do not attempt every phase
    in one change.
-   Define acceptance criteria and tests before implementation.
-   Keep planning, implementation, QA, and release decisions distinct.
-   Treat generated plans and memory as proposals until validated by the
    appropriate authority.
-   Record important architectural trade-offs and unresolved questions.
-   Do not claim a phase is complete without evidence from tests,
    inspection, or runtime checks.

------------------------------------------------------------------------

## Immediate next step

Complete Phase 0 by auditing the current agent runtime and documenting
the existing sources of truth, skill format, task lifecycle, storage
setup, authorization boundaries, and test commands. Then implement the
smallest end-to-end vertical slice: **store a memory with provenance,
retrieve it using metadata plus hybrid search, compile bounded context,
and verify authorization and retrieval quality with tests.**
