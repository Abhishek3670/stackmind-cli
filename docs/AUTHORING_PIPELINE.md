# Authoring Pipeline — Canonical Policy, Contract Compiler, and Readiness Gate

**Status:** Implemented (2026-10-06) · **Policy version:** 1.0.0 · **Branch:** `chore/prune-irrelevant-artifacts`

This document describes the hardened authoring → readiness pipeline and the
failure it eliminates (observed in a clean-repo run on 2026-10-06: the
readiness gate rejected schema-valid architect artifacts on requirements the
authoring prompt never stated, and two repair rounds failed on
"affected: none" guidance).

## Flow

```text
Approved PLAN.md
      ↓
Architect authoring turn            (semantic decisions: scope, deliverables,
      ↓                              tests, dependencies, agent per milestone)
Authoring intent (raw WO/contract YAML)
      ↓
Deterministic contract compiler     validators/harness/authoring_compiler.py
      │   injects system invariants: QA verdict channel, milestone_id
      │   validates declared test_plan structure
      │   ARCHITECT-DECISION-REQUIRED findings route to repair
      ↓
Canonical WO + Contract set         (.sync/work-orders/ACTIVE/, .sync/contracts/)
      ↓
Authoring Readiness Gate            validators/harness/authoring_readiness.py
      │   structured, actionable diagnostics (required_action per issue)
      ↓
READY → DISPATCH        FAIL → targeted repair (compiler runs again first)
```

## Canonical authoring policy

`validators/harness/authoring_policy.py` is the single source of truth for the
three governed requirements. The authoring prompt digest, the compiler, the
readiness gate, the D024 gate, and repair diagnostics all derive from it.

| Rule | Requirement | Enforced by |
|---|---|---|
| `milestone.identity` | Every WO carries `milestone_id` matching its approved plan milestone | Readiness gate (ID-primary; title match is a fallback diagnostic) |
| `qa.verdict_channel` | gemma (QA) contracts authorize `.sync/inbox/claude/**` in `scope.allow` | Compiler (auto-injects) + readiness gate |
| `test.companion_coverage` | Every code deliverable is covered by a declared `test_plan` entry or a `tests/test_<stem>.py` companion | Readiness gate + D024 gate (both honor declared mappings) |

## Separation of concerns

- **Architect (LLM) decides:** implementation scope, deliverables, test
  architecture (declared via `test_plan`), dependencies, agent per milestone,
  budgets.
- **System provides deterministically:** QA verdict channel, milestone
  identity, schema normalization, provenance markers (`normalized_by`).

## Milestone identity

`parse_plan` now separates the trailing `(Agent: <id>)` annotation from the
semantic title (`PlanMilestone.title` / `PlanMilestone.agent`). Work orders are
linked to milestones by `milestone_id`, resolved by the compiler in this order:

1. **Explicit authored id** — validated against the approved plan's ids
   (unknown ids route to repair).
2. **Unique title match** — the semantic work-order title matched against the
   plan milestone titles (order-preserving token containment, not the legacy
   sorted-text containment which silently rejected annotated titles).
3. **Ordinal 1:1 fallback** — when the authored set contains exactly one work
   order per plan milestone, remaining unlinked work orders are assigned the
   remaining milestones in order (the same mapping the operator approved in
   the plan preview). Count mismatches or ambiguity defer to the readiness
   gate / repair instead of guessing.

Lexical title similarity is never the authoritative relationship: it seeds the
compiler's resolution, and the gate validates the resulting stable ids.
`determine_assigned_agent` accepts the parsed agent as an explicit hint, so
plan→WO proposals and deterministic synthesis keep the architect's assignment
even though titles no longer carry the annotation.

## Explicit test plans

A work order may declare:

```yaml
test_plan:
  - sources: [public/style.css, public/script.js]
    tests: [tests/test_visuals.py]
```

Consolidated suites are explicitly supported when declared; per-file
`tests/test_<stem>.py` conventions remain valid. The readiness gate validates
coverage against the declared plan, and the D024 gate resolves the companion
test from the same mapping before falling back to its legacy heuristics.

**Consolidated default:** when code deliverables lack declared coverage and
the authored set plans exactly one test artifact (the QA work order's suite),
the compiler injects the consolidated mapping itself and records it in
provenance. The test architecture is already represented in the authored set
(the QA work order); the compiler only makes the source→test mapping explicit
so D024 cannot dead-end later. Multiple planned suites or an explicit
declaration always win over the default.

## Repair loop

Diagnostics now carry `why / actual_state / expected_state / required_action /
canonical_rule / affected_artifacts`. The repair prompt lists the real
affected artifacts (derived from every issue attachment — including relational
findings such as milestone coverage — not only archived content failures) and
never claims "affected: none" while demanding fixes.

**Worker verification failures** (`outcome_verified` / `scope_verified`) are
synthesized into `OUTCOME_NOT_VERIFIED` evidence packets: bounded retries carry
the declared-vs-observed scope evidence and an exact-path instruction, and on
budget exhaustion the run escalates to the Architect's machine-readable
recovery decision instead of hard-blocking.

**Recovery repair authorization:** applying `amend_contract` /
`split_work_order` / `create_dependency_work_order` places (or extends) a
transitional repair-authorization contract (`synthesized_by: recovery-repair`)
so the repair turn passes pre-execution validation (task WO matches contract
WO) and is authorized to write the amended contract / replacement artifacts.
It is retired when the repair succeeds (split) or superseded by the amended
contract (amend), and the readiness gate does not treat it as an orphaned
architect artifact.
