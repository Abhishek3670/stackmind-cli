# Worker Task Contract — compiled prompts and in-turn ownership

**Status:** Implemented (2026-10-07) · **Scope:** worker execution layer

Worker prompts are now **compiled, not hand-written**. The same structured
sources the gates read (work order YAML, contract YAML, peer work orders,
harness decision schema) are rendered into one JSON task contract per turn —
so every requirement the harness enforces is displayed to the model exactly
once and exactly as enforced.

## The compiled task contract

`validators/harness/task_contract.py` classifies every in-scope path into
exactly one bucket:

- **`your_deliverable`** — the work order's deliverable path; must exist after
  the turn, written as complete final content.
- **`read_only_files`** — deliverables of *other* active work orders that fall
  inside this contract's scope. Visible to the model, never writable. This is
  the work-order deflection guard.
- **`allowed_extra_paths`** — remaining exact in-scope paths.
- **`scope_globs`** — glob entries from the contract.
- **`file_budget`** — from the contract, with an actionable rule ("at most N
  files including your deliverable; no scaffolding, placeholder, or .gitkeep
  files").
- **`acceptance_criteria`** — authored by the architect, displayed verbatim.
- **`on_failure`** — retry a failed write once, then declare `blocked` with a
  blocker describing the failure.

The rendered system message ends with the **final output contract**: one JSON
object (status enum `completed|blocked|deferred`, blockers required when
blocked, `modified_files` semantics, "do not claim tests passed / files
exist — the harness verifies against the tool log").

## In-turn enforcement (same contract, second consumer)

`TaskOwnership.from_contract(...)` is attached to the tool gateway for every
worker turn. `write_file` checks ownership **before** executing:

- writing a `read_only_files` path → denied with an actionable message pointing
  the model at its own deliverable;
- writing a new file after the budget is exhausted → denied with the budget
  message; rewriting an already-written file is never budget-denied.

Denials raise `PermissionError` inside the turn — the governed tool loop
reports them to the model as errors, so the model can correct course
mid-turn instead of failing at post-turn verification.

## Journal-derived declarations

After the model finishes, `reconcile_modified_files` makes the **write_file
journal authoritative** for `modified_files`: files the model wrote but forgot
to declare are added automatically (and reported in the turn meta as
`auto_added_files`); claimed-but-never-written files remain a declaration
failure (that invariant belongs to the declaration-matches gate).

## Acceptance criteria

The authoring prompt instructs the architect to populate `acceptance_criteria`
with machine-checkable statements (e.g. "`average([])` returns 0"). The
readiness gate nudges with an `ACCEPTANCE_CRITERIA_MISSING` **warning**
(adoption pattern mirrors `test_plan` — warning first, not a hard fail), and
the compiled worker contract displays them verbatim.
