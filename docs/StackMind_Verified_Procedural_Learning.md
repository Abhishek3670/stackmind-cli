# StackMind Self-Learning Architecture: From Execution Evidence to Verified Procedural Memory

## Overview

StackMind can evolve from a verification-first agent framework into a system that also learns reusable procedures from its own validated execution history.

The core idea is to introduce a controlled learning loop:

> **Experience → Distillation → Verification → Promotion → Reuse → Refinement**

The important design principle is that learning should not weaken StackMind's formal verification edge. Newly learned knowledge should be treated as a candidate artifact until it has sufficient evidence and passes the same verification discipline applied to ordinary agent-generated work.

---

## 1. Experience Store

### Objective

Capture structured execution evidence from agent runs so that successful and unsuccessful trajectories can later be analyzed.

A possible structure:

```text
experience/
├── models.py
├── recorder.py
├── store.py
└── query.py
```

Each experience record should capture more than the agent's textual reasoning. It should represent what actually happened during execution.

### Suggested Experience Record

```text
Experience
├── experience_id
├── work_order_id
├── task_signature
├── environment
├── initial_state
├── actions[]
├── observations[]
├── failures[]
├── corrections[]
├── final_state
├── verification
│   ├── contracts
│   ├── tests
│   ├── diff
│   └── policy
├── outcome
├── duration
└── timestamps
```

### Key Principle

**Record execution evidence, not merely model output.**

For example:

```text
Action:
Run authentication tests

Observation:
3 failures

Action:
Inspect database connection pool

Observation:
Pool exhaustion detected

Action:
Increase pool capacity

Verification:
All authentication tests pass

Result:
SUCCESS
```

This creates a trajectory that can later be analyzed for reusable procedures.

---

# 2. Learn From Both Successes and Failures

The learning system should preserve unsuccessful experiences as well as successful ones.

Successful experiences can produce:

> **"Do this."**

Failed experiences can produce:

> **"Avoid this."**

For example:

```text
Procedure:
Diagnose database timeout

Positive evidence:
✓ Inspect connection pool
✓ Check pool saturation
✓ Correct pool configuration
✓ Run integration tests

Negative evidence:
✗ Increase HTTP timeout without diagnosing the database
✗ Restart the application without identifying the cause
```

However, failures should not automatically become executable skills.

The safer path is:

```text
Failure
   ↓
Failure analysis
   ↓
Root-cause hypothesis
   ↓
Candidate lesson
   ↓
Future experiment
   ↓
Verified success
   ↓
Skill
```

This prevents the system from permanently encoding an incorrect recovery strategy.

---

# 3. Pattern Miner

Before generating a reusable skill, StackMind should determine whether multiple experiences actually represent the same underlying procedure.

This separates two questions:

### Pattern Miner

> **"Is there a repeatable procedure here?"**

### Skill Extractor

> **"What is that procedure?"**

The proposed flow is:

```text
Experience Store
      ↓
Task normalization
      ↓
Trajectory similarity
      ↓
Outcome similarity
      ↓
Common action sequence
      ↓
Pattern cluster
      ↓
Skill candidate
```

This prevents superficial similarities from becoming incorrect skills.

For example, three tasks mentioning the same technology do not necessarily represent the same procedure.

---

# 4. Lesson Distiller / Skill Extractor

Once a repeatable pattern has been identified, StackMind can synthesize it into a reusable procedural artifact.

Possible component:

```text
learning/
└── skill_extractor.py
```

The extractor should produce a **candidate**, not an immediately trusted skill.

### Candidate Skill

```text
Skill Candidate
├── name
├── purpose
├── preconditions
├── procedure
├── expected outcomes
├── failure conditions
├── required tools
├── constraints
├── source experiences
└── verification requirements
```

The extracted procedure should remain traceable to the experiences from which it was derived.

---

# 5. N ≥ 3 Should Be an Evidence Threshold, Not a Promotion Rule

A minimum number of successful executions is useful, but it should only make a pattern eligible for distillation.

It should not automatically promote the resulting skill.

Instead:

```text
N ≥ 3 successful examples
        ↓
Eligible for distillation
        ↓
Generate candidate skill
        ↓
Verify
        ↓
Evaluate evidence
        ↓
Promote / Reject
```

A more robust promotion score could consider:

```text
SkillScore =
    0.30 × success_rate
  + 0.20 × verification_strength
  + 0.15 × recurrence
  + 0.15 × trajectory_similarity
  + 0.10 × generalizability
  + 0.10 × recency
```

The exact weights can be tuned experimentally.

The important concept is that **frequency alone should never establish trust**.

---

# 6. Skill Lifecycle

Learned procedures should have explicit lifecycle states.

```text
CANDIDATE
    ↓
EXPERIMENTAL
    ↓
VALIDATED
    ↓
PROMOTED
    ↓
DEPRECATED
```

This allows StackMind to distinguish between:

- something the system has merely observed,
- something it is experimenting with,
- something it has verified,
- something safe enough for normal reuse,
- and something that should no longer be used.

---

# 7. Verification Integration

Newly generated skills should return to StackMind's existing verification harness.

This is one of the most important aspects of the design.

The learning system should not create a parallel trust mechanism.

Instead:

```text
Skill Candidate
      ↓
StackMind Harness
      ↓
Contracts
      ↓
Tests
      ↓
Diff Validation
      ↓
Policy Validation
      ↓
Promotion Decision
```

The principle is:

> **Learning generates candidates; verification determines trust.**

---

# 8. Three Levels of Skill Verification

## Level A — Structural Validation

Verify that the generated skill is valid before execution.

Examples:

- Schema validation
- Required fields
- Dependency validation
- Tool permission validation
- Policy constraints
- Invalid or unsafe procedure detection

---

## Level B — Historical Replay

Replay the candidate skill against representative historical tasks.

```text
Skill
 ↓
Historical Task #1
 ↓
Harness
 ↓
PASS

Skill
 ↓
Historical Task #2
 ↓
Harness
 ↓
PASS

Skill
 ↓
Historical Task #3
 ↓
Harness
 ↓
PASS
```

This provides offline evidence before the skill is exposed to normal workloads.

---

## Level C — Canary Execution

After historical validation, run the skill against carefully selected live or controlled tasks.

```text
PROMOTED SKILL
      ↓
Canary Task
      ↓
Harness
      ↓
PASS?
   ↙     ↘
 YES      NO
 ↓         ↓
Retain    Rollback
```

This provides protection against situations where a procedure worked historically but fails under a changed environment.

---

# 9. Version Skills and Support Rollback

Learned skills should never simply overwrite their previous versions.

Instead:

```text
deploy-application
│
├── v1
├── v2
├── v3
└── current → v3
```

Each version should retain:

- its source experiences,
- verification results,
- performance history,
- creation timestamp,
- promotion reason,
- and previous version.

If a newer version performs worse:

```text
v4
 ↓
Failure rate increases
 ↓
Rollback
 ↓
v3 becomes current
```

This gives StackMind a form of **procedural version control**.

---

# 10. Two Categories of Learned Knowledge

StackMind should distinguish between at least two major forms of learning.

## Procedural Knowledge

Knowledge about **how to perform an operation**.

Example:

```text
Deploy application

1. Build
2. Run tests
3. Apply migration
4. Deploy
5. Perform health check
```

These become reusable skills or procedural macros.

## Diagnostic Knowledge

Knowledge about **how to reason about failures**.

Example:

```text
IF:
Database requests are timing out

AND:
Connection pool is exhausted

THEN:
Inspect pool configuration before increasing HTTP timeout.
```

These can become diagnostic heuristics rather than executable procedures.

The distinction is important because the evidence required to trust a procedure may differ from the evidence required to trust a diagnostic rule.

---

# 11. Progressive Skill Retrieval

The skill repository should not be injected wholesale into the agent's context.

Instead:

```text
Task
 ↓
Skill Index
 ↓
Relevant Skills
 ↓
Load Required Procedures
 ↓
Agent Execution
```

For example:

```text
Task: Deploy API

Relevant:
- deploy-api
- database-migration
- docker-healthcheck
- rollback-deployment
```

Only the relevant procedures should be loaded.

This keeps context usage manageable as the skill repository grows.

---

# 12. Skill Composition

A mature version of the system should allow learned skills to become reusable primitives.

Instead of learning one large procedure:

```text
deploy-application
```

StackMind could learn smaller validated capabilities:

```text
build-container
run-tests
apply-migration
deploy-container
health-check
rollback-deployment
```

Then compose them:

```text
deploy-application
 =
    build-container
    +
    run-tests
    +
    apply-migration
    +
    deploy-container
    +
    health-check
```

This transforms the learning system from merely storing macros into building a **procedural vocabulary**.

---

# 13. Recommended Architecture

```text
                         ┌──────────────┐
                         │    TASK      │
                         └──────┬───────┘
                                ↓
                     ┌────────────────────┐
                     │ Knowledge Retrieval│
                     │ + Skill Retrieval  │
                     └─────────┬──────────┘
                               ↓
                         ┌───────────┐
                         │   Agent   │
                         └─────┬─────┘
                               ↓
                         Execute Plan
                               ↓
                    ┌─────────────────────┐
                    │ StackMind Harness   │
                    │ Contracts / Tests   │
                    │ Diff / Policy       │
                    └──────────┬──────────┘
                               ↓
                         ┌───────────┐
                         │  Outcome  │
                         └─────┬─────┘
                               ↓
                     ┌─────────────────┐
                     │ Experience Store│
                     └────────┬────────┘
                              ↓
                     ┌─────────────────┐
                     │ Pattern Miner   │
                     └────────┬────────┘
                              ↓
                    ┌──────────────────┐
                    │ Lesson Distiller │
                    └────────┬─────────┘
                             ↓
                     ┌───────────────┐
                     │ Skill Candidate│
                     └───────┬───────┘
                             ↓
                   ┌────────────────────┐
                   │ Offline Replay     │
                   │ + Harness Verify   │
                   └─────────┬──────────┘
                             ↓
                      ┌────────────┐
                      │ Promotion  │
                      │ Policy     │
                      └─────┬──────┘
                            ↓
                     ┌─────────────┐
                     │ Skill Bank  │
                     └──────┬──────┘
                            │
                            └──────────→ Future Tasks
```

The loop closes continuously:

```text
Experience
    ↓
Distill
    ↓
Skill
    ↓
Reuse
    ↓
Execute
    ↓
Verify
    ↓
Experience
    ↺
```

---

# 14. Implementation Roadmap

## Phase 1 — Experience Logging

Implement:

```text
experience/models.py
experience/recorder.py
experience/store.py
```

Capture validated execution trajectories.

---

## Phase 2 — Pattern Detection

Implement:

```text
learning/pattern_miner.py
```

Identify recurring procedures across experiences.

---

## Phase 3 — Skill Distillation

Implement:

```text
learning/skill_extractor.py
```

Generate candidate procedural skills and diagnostic lessons.

---

## Phase 4 — Verification

Integrate candidates with:

```text
harness/verify.py
```

Add structural validation, historical replay, and policy checks.

---

## Phase 5 — Promotion and Rollback

Implement:

```text
learning/promotion.py
learning/skill_registry.py
learning/versioning.py
```

Introduce lifecycle states, evidence scoring, versioning, and rollback.

---

## Phase 6 — Skill Refinement

Allow new experiences to evaluate existing skills.

```text
Existing Skill
      ↓
New Experience
      ↓
Performance Comparison
      ↓
Improvement Candidate
      ↓
Verification
      ↓
New Version
```

---

## Phase 7 — Skill Composition

Allow validated primitives to be composed into higher-level procedures.

```text
Primitive Skills
      ↓
Composition
      ↓
Composite Skill
      ↓
Verification
      ↓
Promotion
```

---

## Phase 8 — Model-Level Learning

Only after StackMind has accumulated a substantial dataset of high-quality, verified trajectories should deeper model-level learning be considered.

Possible future directions include:

- Offline reinforcement learning
- Preference optimization
- Trajectory-based fine-tuning
- Specialized policy models

The earlier phases should work without modifying model weights.

---

# 15. Key Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Bad procedure becomes persistent knowledge | Verification + promotion gates |
| Repeated failure gets mistaken for a pattern | Require successful, independently validated evidence |
| Environment changes invalidate a skill | Canary execution + revalidation |
| Skill quality degrades over time | Versioning + regression evaluation |
| Skill repository becomes too large | Progressive retrieval |
| Superficial task similarity creates bad skills | Pattern mining before distillation |
| Failed experiences teach incorrect behavior | Treat failures as lessons, not executable skills |
| One large skill becomes difficult to maintain | Prefer composable primitives |
| Automatic learning bypasses safety controls | Route every candidate through the existing harness |

---

# 16. Core Design Principles

### 1. Evidence Before Trust

A learned procedure is not trusted merely because it was generated.

### 2. Verification Is the Gatekeeper

The learning system proposes; the verification system decides.

### 3. Experience Is the Raw Material

Agent executions should become structured evidence that can be analyzed later.

### 4. Failures Are Valuable Data

Failures should inform future reasoning without automatically becoming executable procedures.

### 5. Skills Need Provenance

Every learned skill should be traceable to the experiences and verification results that produced it.

### 6. Skills Must Be Reversible

Every promoted skill should support versioning, regression testing, and rollback.

### 7. Prefer Small, Composable Procedures

A library of validated primitives is more maintainable than a collection of large opaque macros.

### 8. Learning Should Be Continuous but Controlled

StackMind should improve from experience without allowing uncontrolled knowledge drift.

---

# Final Recommendation

The strongest direction is not simply to add an `experience_log.py` and `skill_extractor.py`.

Instead, StackMind should introduce a complete **verified procedural learning loop**:

```text
EXECUTE
   ↓
CAPTURE EXPERIENCE
   ↓
ANALYZE PATTERNS
   ↓
DISTILL LESSON
   ↓
CREATE SKILL CANDIDATE
   ↓
VERIFY
   ↓
REPLAY
   ↓
CANARY
   ↓
PROMOTE
   ↓
REUSE
   ↓
MEASURE
   ↓
REFINE / ROLLBACK
```

This preserves StackMind's central advantage while adding a new capability:

> **StackMind does not merely remember what happened. It learns reusable procedures from validated experience and requires those procedures to earn trust before they become part of its operational memory.**

That should be the foundation of StackMind's self-learning architecture.

---

# 17. Evidence-Backed Memory and Runtime Experience Compilation

## 17.1 Separate Canonical Evidence From Retrieval Indexes

StackMind should distinguish between the **canonical record of what happened** and the mechanisms used to retrieve that information.

Canonical evidence should remain durable and reconstructable. Graphs, vector indexes, metadata indexes, and summaries should be treated as derived representations.

```text
                 CANONICAL EVIDENCE
                        │
              ┌─────────┴─────────┐
              ↓                   ↓
        Derived indexes       Derived summaries
        Graph / vector        Lessons / skills
              │                   │
              └─────────┬─────────┘
                        ↓
                  Retrieval API
                        ↓
                      Agent
```

This allows derived representations to be rebuilt when an embedding model, graph schema, retrieval strategy, or lesson extractor changes.

### Retrieval layers

**Graph** — explicit relationships.

**Vector / semantic index** — experiences similar to the current problem.

**Structured metadata index** — precise filtering by service, environment, outcome, verification status, date, and other attributes.

The agent should therefore retrieve memory through a **retrieval layer**, rather than reading a large raw memory corpus directly.

---

## 17.2 The `.sync` Runtime as a Learning Substrate

StackMind's existing `.sync` runtime should be investigated as the potential source of raw agent trajectories.

Instead of creating an independent logging system that duplicates runtime state, the preferred architecture is:

```text
.sync
   │
   ├── work order
   ├── agent state
   ├── execution events
   ├── tool calls
   ├── generated changes
   ├── verification
   ├── user feedback
   └── final outcome
            │
            ↓
      Experience Compiler
            │
            ↓
       Experience Store
```

The objective is to turn existing operational history into structured learning data.

---

## 17.3 Experience Compiler

Raw runtime events should not be sent directly to the learning model.

A dedicated **Experience Compiler** should transform noisy runtime history into coherent episodes.

```text
.sync raw events
       ↓
Normalize
       ↓
Remove noise
       ↓
Group into episodes
       ↓
Identify action / observation sequence
       ↓
Attach user feedback
       ↓
Attach code changes
       ↓
Attach verification
       ↓
Create Experience
```

Example:

```text
Episode #472

Task:
Fix authentication timeout

Initial hypothesis:
Increase HTTP timeout

User correction:
Inspect the Redis connection pool

Action:
Inspect Redis pool

Finding:
Pool exhausted

Fix:
Increase pool capacity

Verification:
14/14 tests passed

Outcome:
SUCCESS
```

This episode is substantially more useful for learning than the raw sequence of tool calls and log lines.

---

## 17.4 User Corrections as First-Class Learning Signals

Work orders, user responses, agent corrections, and final verification should all be considered potential experience.

A particularly valuable trajectory is:

```text
Agent hypothesis
      ↓
User correction
      ↓
Alternative hypothesis
      ↓
Experiment
      ↓
Verified outcome
```

The user correction should therefore be represented explicitly rather than buried inside conversational history.

Example:

```text
CorrectionEvent

{
    "type": "user_correction",
    "work_order": "WO-472",
    "agent_hypothesis": "increase_timeout",
    "correction": "inspect_connection_pool",
    "resolved_by": "increase_pool_size",
    "verified": true
}
```

This creates a form of **error-corrected experience**.

---

## 17.5 Learning From the Complete Work-Order Lifecycle

A useful learning unit should not be limited to the final successful command.

It should capture the complete lifecycle:

```text
WORK ORDER
    ↓
Agent interpretation
    ↓
Initial hypothesis
    ↓
Actions
    ↓
Observations
    ↓
User feedback / correction
    ↓
Replanning
    ↓
Fix
    ↓
Verification
    ↓
Final outcome
```

This makes it possible to learn not only **what fixed the problem**, but also **what initial reasoning was wrong, what evidence changed the hypothesis, and what ultimately proved the correction**.

That information is especially valuable for learning diagnostic heuristics.

---

## 17.6 Correction Strength Hierarchy

Not all feedback should carry equal learning weight.

```text
Level 0 — No feedback
Agent → solution → tests pass

Level 1 — Implicit positive feedback
Agent → solution → user accepts

Level 2 — Explicit correction
User → "That is not the right approach."

Level 3 — Corrective instruction
User → "Do not modify X; investigate Y."

Level 4 — Correction + verified outcome
User correction
      +
Agent adaptation
      +
Objective verification
```

Level 4 should be treated as especially strong evidence because it combines human correction with an independently observable successful outcome.

---

## 17.7 Causal Change Extraction

The Experience Compiler should distinguish between files and components that were merely inspected and changes that were actually associated with resolution.

```text
17 files inspected
      ↓
3 files modified
      ↓
1 change strongly associated with resolution
      ↓
Tests: 3 failing → 0 failing
```

The resulting experience should emphasize the verified causal change rather than treating every inspected artifact as equally important.

This can reduce noisy skills and make distilled procedures more precise.

---

## 17.8 Unified Memory and Learning Architecture

The resulting architecture can be organized as four layers:

```text
                    ┌─────────────────┐
                    │     SKILLS      │
                    │   "HOW TO DO"   │
                    └────────┬────────┘
                             ↑
                    distilled from
                             │
                    ┌────────┴────────┐
                    │   EXPERIENCES   │
                    │ "WHAT HAPPENED" │
                    └────────┬────────┘
                             ↑
                    compiled from
                             │
                    ┌────────┴────────┐
                    │   TRAJECTORIES  │
                    │ "WHAT THE AGENT │
                    │     DID"        │
                    └────────┬────────┘
                             ↑
                    captured from
                             │
                    ┌────────┴────────┐
                    │     .sync       │
                    │  RAW RUNTIME    │
                    └─────────────────┘
```

Graph, vector, and metadata indexes should operate as retrieval infrastructure across these layers rather than replacing the canonical evidence.

---

## 17.9 Recommended Architectural Principle

> **`.sync` records what happened; the Experience Compiler decides what constitutes an experience; retrieval indexes make experiences discoverable; the learning engine decides what can become a lesson; verification decides what can become trusted knowledge.**

This creates a clean separation of responsibility:

```text
.sync
  = operational history

Experience Compiler
  = episode construction

Experience Store
  = durable learning evidence

Graph / Vector / Metadata
  = retrieval

Learning Engine
  = abstraction

Harness
  = trust

Skill Registry
  = reusable procedural memory
```

This separation should be preserved as the learning system evolves.

---

# 18. Lightweight Persistent Index for Experience Retrieval

## 18.1 Do Not Replace `.sync` With a Database

StackMind should retain `.sync` as the canonical operational source.

A lightweight embedded database should be introduced only as a **derived, rebuildable index** for experience retrieval and analytics.

```text
.sync/
   ↓
Canonical operational records
   │
   ├── Work Orders
   ├── Runtime state
   ├── Receipts
   ├── Reports
   ├── Decisions
   └── Reviews
            │
            ↓
      Experience Compiler
            │
            ↓
       Experience Store
            │
            ↓
      Rebuildable Index
            │
       ┌────┼─────┐
       ↓    ↓     ↓
      FTS   SQL  Vector
```

This avoids duplicating the source of truth.

If the schema, embedding model, or retrieval strategy changes, the derived database can be rebuilt from `.sync` and the canonical experience records.

---

## 18.2 SQLite as the Initial Experience Index

SQLite is the preferred first implementation because it is:

- embedded,
- serverless,
- available directly from Python,
- easy to back up and inspect,
- suitable for structured metadata,
- and compatible with full-text search through FTS5.

A possible location is:

```text
.sync/experience/index.db
```

The database should remain disposable and rebuildable.

Possible logical tables include:

```text
experiences
events
actions
observations
outcomes
artifacts
work_orders
lessons
skills
skill_versions
```

FTS5 can provide fast retrieval over:

```text
task description
agent report
failure
root cause
resolution
lesson
```

Structured SQL filtering can handle attributes such as:

```text
service
language
environment
outcome
verification_status
created_at
agent
provider
```

Semantic search can continue to use StackMind's existing embedding infrastructure.

---

## 18.3 Role Separation

The system should clearly separate four responsibilities:

```text
.sync
  = canonical operational history

Experience Store
  = canonical normalized learning records

SQLite / FTS / vectors / graph
  = derived retrieval indexes

Skill Registry
  = promoted procedural knowledge
```

The agent should never depend on a database being the only copy of an experience.

---

# 19. Work-Order-Centric Experience Compilation

## 19.1 Current Learning Unit

At the current stage of StackMind, the most practical learning unit is the **completed Work Order lifecycle** rather than a fine-grained interactive event stream.

A typical learning episode can be reconstructed from:

```text
WORK ORDER
     ↓
PLAN
     ↓
AGENT EXECUTION
     ↓
RUNTIME EVIDENCE
     ↓
CODE / GRAPH CHANGES
     ↓
QA REVIEW
     ↓
VERIFICATION
     ↓
COMPLETION
```

This is already compatible with the current operational model.

---

## 19.2 Objective Learning Signals

The first learning implementation should favor signals already available in StackMind.

### Work-order outcome

```text
COMPLETED
BLOCKED
REWORK
FAILED
```

### Verification evidence

```text
tests
contracts
scope audit
secret scan
workspace validation
review verdict
```

### Runtime and code evidence

```text
CALLS
FLOWS_TO
runtime observations
graph changes
diff
affected nodes
```

These signals are preferable to relying primarily on conversational interpretation.

---

## 19.3 Current Experience Pipeline

The immediate architecture should therefore be:

```text
.sync
 │
 ├── Work Order
 ├── Runtime evidence
 ├── Agent handoff
 ├── QA review
 ├── Verification
 └── Completion
          │
          ↓
   Experience Compiler
          │
          ↓
     Experience Store
          │
          ↓
      Pattern Miner
          │
          ↓
     Skill Candidate
          │
          ↓
        Harness
          │
          ↓
   Replay / Verification
          │
          ↓
       Promotion
```

This allows StackMind to begin learning from artifacts it already produces rather than requiring a new interaction model.

---

# 20. Future Compatibilities

The current architecture should remain compatible with richer event sources that may be introduced later.

## 20.1 User-Correction Events

A future TUI or interactive runtime can emit structured correction events:

```text
CorrectionEvent

{
    "type": "user_correction",
    "work_order": "WO-472",
    "agent_hypothesis": "increase_timeout",
    "correction": "inspect_connection_pool",
    "resolved_by": "increase_pool_size",
    "verified": true
}
```

These events can be added to the Experience Compiler as an additional evidence source without changing the canonical experience model.

---

## 20.2 Fine-Grained TUI Events

A future interactive runtime may expose:

```text
TUI
 ↓
tool invocation
 ↓
tool result
 ↓
agent decision
 ↓
user interruption
 ↓
user correction
 ↓
replanning
 ↓
verification
```

The Experience Compiler can then combine these events with the existing Work Order lifecycle.

The architecture should therefore be designed so that:

```text
Current:
.sync artifacts
       ↓
Experience Compiler

Future:
.sync artifacts
+
TUI event stream
       ↓
Experience Compiler
```

The TUI should become an **additional event source**, not a replacement for `.sync`.

---

## 20.3 Interactive Feedback as High-Value Evidence

When available, user corrections can become particularly strong learning signals.

A useful hierarchy is:

```text
Level 0 — No explicit feedback

Level 1 — Implicit acceptance

Level 2 — Explicit rejection

Level 3 — Corrective instruction

Level 4 — Correction + successful adaptation + verification
```

Level 4 can receive substantially greater learning weight because it combines human supervision with an independently verified outcome.

---

## 20.4 Future Event-Sourced Compatibility

The architecture should also remain compatible with a future event-sourced runtime.

Potential future events include:

```text
WORK_ORDER_CREATED
PLAN_CREATED
AGENT_ACTION
TOOL_CALL
TOOL_RESULT
OBSERVATION
USER_CORRECTION
REPLAN
FILE_CHANGE
TEST_RESULT
VERIFICATION_RESULT
REVIEW_RESULT
WORK_ORDER_COMPLETED
```

These events can eventually feed the same Experience Compiler.

The key compatibility rule is:

> **The Experience model should remain stable even as the underlying event sources become more detailed.**

---

# 21. Revised Design Principle

The learning architecture should now be understood as:

> **`.sync` is the current operational source of truth; an Experience Compiler converts those artifacts into normalized episodes; embedded databases and indexes accelerate retrieval; the learning engine abstracts validated patterns; the verification system determines trust; future TUI/event streams can enrich the same model without requiring a redesign.**

This preserves both present-day simplicity and future interactive capabilities.

---

# 22. Revised Implementation Priority

The immediate roadmap should therefore prioritize:

```text
Phase 1
Understand and normalize .sync artifacts
        ↓
Phase 2
Experience schema + compiler
        ↓
Phase 3
SQLite / FTS experience index
        ↓
Phase 4
Pattern Miner
        ↓
Phase 5
Skill Distillation
        ↓
Phase 6
Harness replay + verification
        ↓
Phase 7
Promotion + versioning
```

Future-compatible extensions remain:

```text
TUI
 ↓
fine-grained event stream
 ↓
user correction events
 ↓
interactive feedback
 ↓
richer trajectory reconstruction
```

These should plug into the Experience Compiler rather than force a replacement of the existing `.sync` model.


---

# 23. Proposed StackMind vs. Current Hermes: Architectural Comparison

## 23.1 Scope of This Comparison

This section compares the **proposed StackMind self-learning architecture** described in this document with the **current Hermes architecture**.

This is intentionally different from comparing only today's implemented StackMind code against Hermes.

The following StackMind capabilities are **proposed architectural additions**, not claims about the current implementation:

- Experience Compiler
- Pattern Miner
- Skill Distillation
- Evidence-based skill promotion
- Historical replay
- Canary validation
- Skill versioning and rollback
- Experience indexing with SQLite/FTS
- Autonomous procedural refinement

The comparison therefore asks:

> **What would StackMind become if the proposed architecture were implemented, and how would that compare conceptually with the current capabilities of Hermes?**

---

## 23.2 Executive Comparison

| Dimension | Proposed StackMind | Current Hermes | Conceptual advantage |
|---|---|---|---|
| Core philosophy | Verification-first self-improvement | Self-improving general-purpose agent | Different objectives |
| Experience capture | `.sync` + Experience Compiler | Session history + runtime experience | Both |
| Persistent factual memory | Evidence-backed Knowledge Store | Persistent memory files | Different strengths |
| Procedural memory | Versioned, verified skills | Agent-managed Skills | StackMind concept |
| Experience → skill | Pattern Miner → Distiller | Experience → skill creation/update | StackMind concept |
| Failure learning | Failure → lesson → verified correction | Errors/dead ends can inform skills | Both |
| User correction learning | Future-compatible event source | Already supported operationally | Hermes today |
| Evidence provenance | First-class design principle | Less central to skill lifecycle | StackMind |
| Skill verification | Structural + replay + canary + Harness | Skill safety/write controls | StackMind |
| Skill promotion | Evidence-based lifecycle | Agent-managed updates | StackMind |
| Skill rollback | Explicit versioning + rollback | Checkpoints/filesystem rollback | StackMind concept |
| Retrieval | Graph + vector + metadata + FTS | SQLite/FTS session search + skill retrieval | StackMind breadth |
| Canonical source | `.sync` / knowledge files | Files + SQLite session data | Both |
| Embedded experience DB | SQLite/FTS proposed | SQLite/FTS already implemented | Hermes today |
| Planning/replanning | Not fully specified yet | Mature agent execution/delegation | Hermes |
| Tool ecosystem | Focused on governed engineering workflow | Broad tools + MCP + integrations | Hermes |
| Subagents | Not central to learning design | Built-in delegation | Hermes |
| Automation | Future/optional | Cron and scheduled tasks | Hermes |
| Model/provider routing | Future/optional | Broad provider/model support | Hermes |
| Long-term knowledge governance | Strong proposed model | Strong practical memory/skill system | StackMind concept |
| Model-weight learning | Separate future phase | Separate training/trajectory pipeline | Both |

---

## 23.3 Fundamental Architectural Difference

The two systems can be summarized as two different loops.

### General self-improvement loop

```text
Experience
    ↓
Remember
    ↓
Create / update skill
    ↓
Reuse
    ↓
Improve
```

### Proposed StackMind loop

```text
Experience
    ↓
Structure
    ↓
Find recurring pattern
    ↓
Distill lesson
    ↓
Create candidate
    ↓
Verify
    ↓
Replay
    ↓
Canary
    ↓
Promote
    ↓
Version
    ↓
Reuse
    ↓
Measure
    ↓
Refine / Rollback
```

The proposed StackMind loop intentionally introduces more control points.

That additional complexity should be justified by a corresponding increase in reliability, traceability, and resistance to knowledge drift.

---

## 23.4 Memory Architecture

A useful conceptual separation is:

```text
Memory = what the system knows

Skills = how the system does something
```

The proposed StackMind design adds another layer:

```text
Experience = why the system believes a procedure is useful
```

Therefore:

```text
                   SKILL
                "HOW TO DO"
                     ↑
                distilled from
                     │
                EXPERIENCE
               "WHAT HAPPENED"
                     ↑
                compiled from
                     │
                 .sync
             "WHAT OCCURRED"
```

This gives proposed StackMind a richer provenance chain than a simple memory → skill relationship.

---

## 23.5 Canonical Memory vs. Retrieval

The proposed StackMind architecture treats the canonical record and retrieval mechanisms separately.

```text
Canonical evidence
        │
        ├── Graph index
        ├── Vector index
        ├── Metadata index
        └── FTS index
                │
                ↓
          Retrieval API
                ↓
              Agent
```

This means derived indexes can be rebuilt without losing the source evidence.

The strategy is therefore:

> **Do not make the retrieval database the only copy of knowledge.**

The database should accelerate access; the canonical artifacts should preserve provenance.

---

## 23.6 Procedural Learning

The proposed StackMind design adds an explicit abstraction pipeline:

```text
Multiple Experiences
        ↓
Pattern Miner
        ↓
Generalizable Pattern
        ↓
Skill Distiller
        ↓
Candidate Skill
```

The key difference is the separation of:

> **"Is there actually a reusable pattern?"**

from:

> **"What should the resulting skill look like?"**

This reduces the risk that superficial similarity between tasks creates a reusable procedure that is not truly generalizable.

---

## 23.7 Evidence and Skill Trust

The proposed StackMind design makes evidence part of the identity of a learned skill.

A skill version should retain:

```text
source experiences
verification results
performance history
creation timestamp
promotion reason
previous version
```

The resulting object is closer to:

```text
Verified procedural artifact
```

than simply:

```text
LLM-generated text file
```

This distinction is central to the design.

---

## 23.8 Skill Promotion

A proposed StackMind skill should not become trusted solely because the same pattern was seen multiple times.

For example:

```text
N ≥ 3
   ↓
eligible for distillation
```

not:

```text
N ≥ 3
   ↓
automatically trusted
```

The promotion sequence should remain:

```text
Candidate
   ↓
Evidence evaluation
   ↓
Structural validation
   ↓
Historical replay
   ↓
Canary
   ↓
Promotion
```

Frequency is therefore treated as an input to trust, not trust itself.

---

## 23.9 Failure Learning

The proposed StackMind design deliberately distinguishes:

```text
Failure
   ↓
Failure analysis
   ↓
Root-cause hypothesis
   ↓
Candidate lesson
   ↓
Future experiment
   ↓
Verified success
   ↓
Skill
```

This prevents a failed strategy from automatically becoming a future recommendation.

The important rule is:

> **Failure is evidence, not automatically knowledge.**

---

## 23.10 User Corrections

The proposed StackMind architecture keeps explicit user-correction events as a **future-compatible capability**, because the current StackMind operating model is Work Order and `.sync` driven rather than a fine-grained interactive TUI event stream.

The compatibility model is:

```text
Current:
.sync artifacts
      ↓
Experience Compiler

Future:
.sync artifacts
+
TUI event stream
      ↓
Experience Compiler
```

This allows user correction to become a stronger supervised learning signal later without redesigning the experience model.

---

## 23.11 Retrieval Comparison

The proposed StackMind architecture can support several retrieval modes:

### Semantic

```text
"Find previous problems similar to this one."
```

### Graph

```text
"What components are related to this failure?"
```

### Structured

```text
"Find successful production fixes for this service."
```

### Historical

```text
"What happened during Work Order WO-472?"
```

### Procedural

```text
"What verified skill applies here?"
```

The proposed combination is:

```text
Graph
+
Vector
+
Metadata
+
FTS
+
Skill retrieval
```

---

## 23.12 Context Efficiency

A large skill store should not be injected wholesale into the model context.

The preferred approach is:

```text
Task
 ↓
Retrieve relevant knowledge
 ↓
Retrieve relevant experiences
 ↓
Retrieve relevant skills
 ↓
Load only required procedures
 ↓
Execute
```

A mature implementation could evolve this into:

> **Evidence-aware context assembly**

where the system chooses context according to relevance, confidence, provenance, and task requirements.

---

## 23.13 Skill Lifecycle and Rollback

Proposed StackMind treats learned skills as versioned artifacts:

```text
deploy-application
│
├── v1
├── v2
├── v3
└── current → v3
```

A new version should be able to regress:

```text
v4
 ↓
Failure rate increases
 ↓
Regression detected
 ↓
Rollback
 ↓
v3 becomes current
```

This resembles software release management applied to procedural knowledge.

The important concept is:

> **Learned behavior must be reversible.**

---

## 23.14 Current Hermes Strengths That StackMind Should Not Ignore

Hermes is currently much broader as a general-purpose agent platform.

Its practical strengths include:

```text
Tools
Memory
Skills
MCP
Browser / web interaction
Subagents
Automation
Multiple interfaces
Provider/model flexibility
Session search
TUI
```

Proposed StackMind should therefore avoid trying to recreate every capability immediately.

A better strategy is:

```text
Agent capability layer
        +
StackMind learning/governance layer
```

The agent layer can grow independently while the learning layer remains evidence-driven.

---

## 23.15 Conceptual Performance

No directly comparable benchmark should be claimed without controlled measurement.

The following observations are therefore architectural rather than empirical.

### Learning latency

General autonomous skill creation can be relatively fast:

```text
Experience
 ↓
Skill
```

The proposed StackMind pipeline is deliberately slower:

```text
Experience
 ↓
Pattern
 ↓
Candidate
 ↓
Verification
 ↓
Replay
 ↓
Canary
 ↓
Promotion
```

This increases learning latency but may reduce the chance of persistent incorrect procedural knowledge.

### Runtime execution

Once a procedure is trusted, both architectures can reuse it without repeatedly rediscovering the workflow.

The proposed StackMind design adds provenance and validation metadata, which introduces some retrieval and policy overhead.

### Long-term behavior

The intended tradeoff is:

```text
More validation overhead
        ↓
Higher resistance to
knowledge drift and regressions
```

This is a design objective, not a measured performance result.

---

## 23.16 Architectural Tradeoff

The central tradeoff can be summarized as:

| Dimension | More aggressive learning | More controlled learning |
|---|---|---|
| Skill creation speed | Higher | Lower |
| Immediate adaptation | Higher | Lower |
| Verification cost | Lower | Higher |
| Promotion complexity | Lower | Higher |
| Provenance | Lower | Higher |
| Rollback capability | Variable | Explicit |
| Knowledge governance | Lower | Higher |
| Risk of propagating bad procedures | Higher | Lower |
| Operational simplicity | Higher | Lower |

The proposed StackMind approach should only accept its additional complexity if the resulting trust and reliability benefits are demonstrable.

---

## 23.17 Strategic Positioning

The strategic positioning should therefore not be:

> **"Build another general-purpose agent."**

It should be:

> **"Build an agent that can improve itself while maintaining an evidence trail and explicit trust boundary around what it learns."**

The distinction is:

```text
General self-improving agent
            │
            ↓
      Capability growth

Proposed StackMind
            │
            ↓
Capability growth
            +
Evidence
            +
Verification
            +
Provenance
            +
Versioning
            +
Rollback
```

---

## 23.18 Final Comparison

The architectural thesis can be expressed in one line each:

> **Hermes: self-improving agent.**

> **Proposed StackMind: self-improving verified agent.**

The objective is not to make StackMind learn more aggressively.

The objective is to make learned capabilities:

```text
Traceable
Testable
Evidence-backed
Versioned
Reversible
Governable
Reusable
```

This gives StackMind a distinct architectural identity:

> **Autonomous procedural learning under a verification regime.**

That should remain the guiding principle for future learning-engine work.

---

# 24. Final Architecture Safeguards

Before implementation, the learning architecture should explicitly address not only how StackMind learns, but also how it determines whether learned knowledge remains useful, applicable, and trustworthy.

## 24.1 Learning Evaluation vs. Verification

Verification answers:

> **"Does the learned procedure work?"**

Learning evaluation must additionally answer:

> **"Is the learned procedure actually better than the alternative?"**

A skill can pass correctness checks while still being slower, more expensive, less robust, or more complex than the baseline agent behavior.

```text
                 Candidate Skill
                       ↓
                  Verification
                       ↓
                Baseline Comparison
                       ↓
        ┌──────────────┼──────────────┐
        ↓              ↓              ↓
    Correctness     Efficiency     Robustness
        │              │              │
        └──────────────┼──────────────┘
                       ↓
                 Promotion Score
```

Relevant performance signals can include:

```text
success rate
verification pass rate
latency
tool-call count
token / cost usage
rework rate
failure rate
rollback rate
```

The exact metrics and weights should be calibrated experimentally rather than hard-coded as permanent policy.

### Core distinction

```text
Verification
= Can it work?

Learning Evaluation
= Is it worth preferring?
```

This prevents StackMind from learning procedures that are technically correct but operationally inferior.

---

## 24.2 Skill Applicability and Boundaries

A learned skill can be valid but still be inappropriate outside the conditions under which it was learned.

Every promoted skill should therefore describe its applicability.

```text
Skill
├── Preconditions
├── Applicability
├── Procedure
├── Expected outcomes
├── Constraints
├── Environment assumptions
└── Known exclusions
```

Example:

```text
Skill:
Increase Redis connection pool

Valid for:
service-A
production
10-worker deployment

Known exclusion:
serverless deployment
```

The system should retrieve a skill only when the current context satisfies its applicability conditions.

This reduces incorrect generalization.

---

## 24.3 Knowledge Staleness and Revalidation

Learned knowledge can become obsolete without ever having been incorrect.

Examples of change include:

```text
dependency upgrade
architecture change
runtime environment change
configuration change
API change
tool replacement
major code refactor
```

A skill should therefore have a lifecycle beyond simple promotion.

```text
PROMOTED
    ↓
ACTIVE
    ↓
STALE
    ↓
REVALIDATION
    ↓
ACTIVE / DEPRECATED
```

Possible staleness triggers include:

```text
verification failures
success-rate degradation
related code changes
dependency changes
environment changes
long periods without successful use
```

The goal is to make the knowledge system **self-maintaining rather than merely self-growing**.

---

## 24.4 Anti-Learning: Decay, Downgrade, and Removal

A self-learning system must be able to stop believing something.

Learning should therefore support both:

```text
new knowledge
```

and:

```text
knowledge decay
```

A possible lifecycle is:

```text
ACTIVE SKILL
      ↓
new contradictory evidence
      ↓
confidence decreases
      ↓
REVIEW / REVALIDATION
      ↓
ACTIVE
or
DEPRECATED
or
REMOVED
```

The learning loop therefore becomes:

```text
       EXPERIENCE
            ↓
   ┌────────┴────────┐
   ↓                 ↓
LEARN NEW         REASSESS OLD
   │                 │
   ↓                 ↓
NEW SKILL        UPDATE / DECAY
   │                 │
   └────────┬────────┘
            ↓
         VERIFY
            ↓
        PROMOTE
```

This is essential to prevent indefinite accumulation of stale or contradictory procedural knowledge.

---

## 24.5 Risk-Tiered Promotion

A single promotion threshold should not govern every type of skill.

Different procedures carry different consequences.

A possible policy model is:

```text
                         Promotion Policy
                                │
                 ┌──────────────┼──────────────┐
                 ↓              ↓              ↓
              Low Risk       High Risk      Critical
                 ↓              ↓              ↓
            Auto Promote    Strong Gates    Human Gate
```

Examples:

```text
Low risk:
formatting / local analysis
→ relatively low promotion threshold

Medium risk:
code modification / refactoring
→ stronger replay and regression evidence

High risk:
production deployment / data migration
→ strong verification + canary

Critical:
security-sensitive or irreversible operation
→ explicit human approval or no autonomous promotion
```

The exact risk taxonomy should be defined independently from the skill extraction mechanism.

The key principle is:

> **The amount of autonomy granted to a learned procedure should scale with the consequence of failure.**

---

# 25. Learning vs. Optimization

StackMind should explicitly distinguish **learning** from **optimization**.

### Learning

> **"I discovered a reusable way to solve this class of problems."**

### Optimization

> **"I found a way to solve it faster, cheaper, or more reliably."**

These goals may produce different candidate skills.

Example:

```text
Skill A
success = 90%
latency = 5s

Skill B
success = 94%
latency = 8s

Skill C
success = 93%
latency = 3s
```

Verification alone might allow A, B, and C.

Learning evaluation should determine which procedure should be preferred under the task's risk and performance requirements.

A mature skill record should therefore maintain a **performance profile**, not only a binary trusted/untrusted state.

Possible profile dimensions:

```text
correctness
latency
cost
tool usage
rework
robustness
environment coverage
failure modes
```

This allows StackMind to evolve from:

```text
"Is this skill valid?"
```

toward:

```text
"Which valid skill is best for this situation?"
```

---

# 26. Final Pre-Implementation Checklist

Before the architecture is implemented, StackMind should be able to answer the following:

```text
1. Where does experience come from?
   → .sync

2. What turns raw runtime artifacts into an experience?
   → Experience Compiler

3. Where is canonical experience stored?
   → Experience Store

4. How is experience retrieved?
   → Graph + vector + FTS + structured filters

5. How is a pattern detected?
   → Pattern Miner

6. How is a procedure generated?
   → Skill Distiller

7. How does it earn trust?
   → Verification + replay + canary

8. How do we know it is actually better?
   → Baseline comparison + performance metrics

9. Where can the skill be used?
   → Applicability + preconditions + constraints

10. How does the system detect outdated knowledge?
    → Staleness + revalidation

11. How does it stop believing something?
    → Decay + downgrade + deprecation + removal

12. How does it handle different levels of risk?
    → Risk-tiered promotion

13. How does it improve an existing skill?
    → Refinement + new versions

14. How does it recover from regression?
    → Versioning + rollback

15. Can the system explain why it trusts a skill?
    → Provenance + evidence

16. Can the retrieval layer be rebuilt?
    → Canonical artifacts + rebuildable indexes

17. Can future interaction models be added?
    → Stable Experience model + extensible event sources

18. Can the system distinguish learning from optimization?
    → Correctness + performance profile
```

---

# 27. Final Architectural Thesis

StackMind's self-learning architecture should not be defined merely as the ability to create skills from previous executions.

It should be defined as:

> **A controlled system that converts operational experience into reusable procedural knowledge, evaluates that knowledge against evidence and baselines, promotes it according to risk, continuously monitors its validity, and can degrade or remove it when new evidence contradicts it.**

The complete lifecycle becomes:

```text
                    EXECUTE
                       ↓
                CAPTURE EXPERIENCE
                       ↓
                 COMPILE EPISODE
                       ↓
                 ANALYZE PATTERNS
                       ↓
                  DISTILL LESSON
                       ↓
                CREATE CANDIDATE
                       ↓
                    VERIFY
                       ↓
                    REPLAY
                       ↓
                    CANARY
                       ↓
               EVALUATE vs BASELINE
                       ↓
                RISK-BASED PROMOTION
                       ↓
                 VERSIONED SKILL
                       ↓
                     REUSE
                       ↓
                    MEASURE
                       ↓
             ┌─────────┴─────────┐
             ↓                   ↓
          IMPROVE               DECAY
             ↓                   ↓
         NEW VERSION      REVALIDATE / REMOVE
             └─────────┬─────────┘
                       ↓
                     REUSE
                       ↺
```

The architectural identity of StackMind should therefore remain:

> **Autonomous procedural learning under a verification regime.**

Its objective is not to learn as aggressively as possible.

Its objective is to **increase capability while preserving evidence, traceability, controllability, and reversibility as learned knowledge grows**.

This completes the conceptual architecture sufficiently for implementation planning. Future work should focus on concrete schemas, interfaces, metrics, and incremental implementation rather than introducing additional conceptual layers without evidence that they are necessary.
