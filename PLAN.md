# StackMind — Implementation Plan (Multi-Language Universal Frontend)

**Version:** 3.0
**Supersedes:** PLAN.md (v2.0), PLANv5.md
**Authoritative source:** `docs/STACKMIND_ARCHITECTURE.md` (Architecture Handbook)
**Date:** 2026-07-24
**Status:** IN PROGRESS — Implementing Multi-Language Universal Frontend
**Author:** Claude (Senior Architect)

---

## Executive Summary

StackMind's Knowledge Compiler currently produces deterministic IR for Python only, via LibCST + Jedi.

To support additional languages without rewriting parsers and Hybrid LSPs from scratch, we are integrating **`cbm` (Codebase-Memory)** as a Tree-sitter-backed multi-language adapter (`CbmCompiler`). We will use it strictly as a dumb data pipeline: ingest its output and force it through StackMind's native `birth_key()` hashing. This guarantees StackMind's core IR, identity model, and Contract/Governance layer remain 100% deterministic and unaffected by the underlying parser — regardless of what Phase 1 finds about `cbm` itself.

---

## 0. Prerequisites (Critical Sequencing)

**This plan is BLOCKED until WO-036 (Scope-Violation E2E Test & D025 Code Enforcement) passes Gemma QA.**

WO-036 seals the Dual-Repo governance layer — the contract-scope integration test (in-scope Knowledge API queries and harness edits succeed; out-of-scope or denied ones fail closed with structured audit logging) and the `d025_gate.py` destructive-operations gate (backup + post-execution verification before high-risk ops). Adding multi-language surface area before this closes means every new language would inherit the same transactional and scope-boundary gaps. Once WO-036 clears Codex → Gemma review, this plan unblocks.

---

## Phase 1 — Prove the Dependency

Before writing any adapter code, verify `cbm` is safe and viable to depend on. Nothing about its license, determinism, or provenance should be assumed going in.

1. **Read the actual LICENSE file**: Confirm the core binary is MIT or compatible.
2. **Determinism test**: Index a fixture repo twice in `full` mode. Diff the resulting output byte-for-byte. If it is not perfectly deterministic, fall back to Advisory Mode (Phase 4).
3. **Schema inventory**: Document the SQLite schema/JSON output fields available per node/edge to define the adapter's input contract.
4. **Supply-chain check**: Confirm signed/checksummed releases are verifiable.

**Exit Gate:** License confirmed, determinism verified (or Advisory Mode confirmed as fallback), schema documented, binary provenance verified.

---

## Phase 2 — `CbmCompiler` Adapter

Implement the `CompilerFrontend` interface for non-Python languages using `cbm`, joining the existing 14 domain compilers.

1. **`discover_files`**: Delegate to `cbm`'s own discovery (respects `.gitignore`).
2. **`parse` / `resolve`**: Shell out to `cbm index <path> --mode full --json`.
3. **`emit_ir` (The Real Work)**: Translate `cbm`'s raw records into StackMind's IR by running them through `birth_key()`. `cbm`'s internal node IDs are ignored — StackMind mints its own.
4. **Edge Mapping**: Map `cbm`'s CALLS / RESOLVED_CALLS / import edges onto StackMind's existing edge kinds. Unmapped edges are logged, not dropped.

**Exit Gate:** A real TypeScript file compiles through the adapter into valid StackMind IR with correctly minted birth-hash IDs.

---

## Phase 3 — Lazy Packaging

StackMind must remain a zero-dependency Python installation for users working on pure Python projects.

1. **Lazy Prerequisite**: `stackmind` detects the absence of the `cbm` binary and fails with a clear install instruction *only* when a non-Python language is actually encountered in the repository.

---

## Phase 4 — Determinism Fallback (Conditional)

Triggers only if the Phase 1 determinism check fails (i.e., `cbm` cannot give byte-identical output across runs). Not a sequential phase everyone passes through.

1. Use `cbm` in read-only, best-effort mode.
2. Explicitly tag its IR output as `advisory: true` rather than `verified: true`.
3. StackMind's strict determinism guarantees continue to apply only to the Python frontend's output.

---

## Next Milestone / Follow-up Initiative: Verified Procedural Learning

Following the completion of the Multi-Language Universal Frontend and WO-036 governance milestones, StackMind's subsequent major architectural initiative is **Verified Procedural Learning**.

* **Dedicated Implementation Plan**: [`PLAN_PROCEDURAL_LEARNING.md`](file:///W:/Aatish/Stuff/stackmind/PLAN_PROCEDURAL_LEARNING.md)
* **Design Specification**: `StackMind_Verified_Procedural_Learning_FINAL.md` (§1–§30)
* **Technical Codebase Investigation**: [`docs/PROCEDURAL_LEARNING_TECHNICAL_INVESTIGATION.md`](file:///W:/Aatish/Stuff/stackmind/docs/PROCEDURAL_LEARNING_TECHNICAL_INVESTIGATION.md)
* **Core Sequencing Requirement**: **Phase 0 (Verification & Trust Foundation)** must be 100% complete and verified before any autonomous skill capture, compilation, or promotion begins.

```text
CURRENT MILESTONE: Multi-Language Frontend (v3.0) ──► Phase 1-4
                                                                │
                                                                ▼
FOLLOW-UP MILESTONE: Verified Procedural Learning (v4.0) ──► Phase 0 (Trust & Verification Gate)
                                                                │
                                                                ▼
                                                             Phases 1–8 (Capture → Distill → Verify → Promote)
```

---

## Note

Do NOT commit changes yet (per CEO directive) — applies for the duration of this plan, not just its first step.
