# StackMind PLANv5: Multi-Language via a Borrowed Frontend, Not a Built One

**Version:** 5.0 Draft
**Status:** Strategic Roadmap
**Builds on:** PLANv3's `CompilerFrontend` interface (Frontend Interface section),
deferred there as Phase 4. This plan makes that interface concrete and gives it
a first real implementation instead of a Python-native one.
**Depends on:** PLANv4 Phase 1.7 (contract enforcement gaps) should land first —
see Sequencing below for why.

---

## 0. The decision this plan encodes

`codebase-memory-mcp` (DeusData) is a mature, MIT-licensed, single static C
binary: 158 languages via vendored tree-sitter grammars, a genuine Hybrid LSP
type-resolution pass for ~11 language families, SQLite-backed storage, a
research preprint with real benchmarks (arXiv:2603.27277), and CI hygiene
(CodeQL, fuzzing, signed/checksummed releases, 34k+ stars) well beyond what one
person builds solo. It already ships a CLI mode independent of MCP.

**Decision: don't write a tree-sitter + Hybrid LSP layer. Consume this project's
output as an ingestion source behind the `CompilerFrontend` interface, for
non-Python languages, and change nothing else about the compiler core.**

What this plan is *not*: a decision to depend on CBM for Python. Python already
has a working, tested, deterministic frontend (LibCST + Jedi). That frontend
doesn't get touched by this plan unless Phase 5.4 below produces evidence it
should be.

---

## 1. What stays exactly as it is

- Symbol Registry, birth-hash NodeIDs (RFC-001), rename/move continuity
  (`rename.py`)
- IR, storage, projections, determinism contract (compile-twice-byte-identical)
- Contract/governance layer — CBM has no equivalent of this at all; it is
  explicitly out of scope for it ("no LLM inside it... pure structural
  backend"). Nothing here touches `validators/knowledge/contract.py`,
  `validators/harness/contract_gate.py`, or the harness runner.
- The existing Python `CompilerFrontend` (LibCST + Jedi)

This plan only fills in the empty half of the interface PLANv3 already sketched:

```python
class CompilerFrontend:
    language: str
    def discover_files(...): ...
    def parse(...): ...
    def resolve(...): ...
    def emit_ir(...): ...
```

---

## Phase 5.1 — Prove the dependency before building on it

Do this before writing any adapter code. All four are cheap; any one failing
changes the plan.

1. **Read the actual LICENSE file** in the CBM repo (not the README, not
   marketing copy). Confirm the core binary — not just the vendored grammars
   listed in `THIRD_PARTY.md` — is MIT or another license compatible with
   `pip install stackmind` remaining freely distributable. Record the exact
   license and version pinned.
2. **Determinism test.** Index the same small fixture repo twice in `full`
   mode, diff the resulting `graph.db` (or `--json` output) byte-for-byte.
   StackMind's determinism contract is CI-enforced on its own frontend; it
   cannot silently become "usually deterministic" on this one. If `full` mode
   isn't byte-identical across runs, this plan stops here and falls back to
   Phase 5.5 (narrower alternative).
3. **Schema inventory.** Read `store/store.c`'s SQLite schema (`nodes`, `edges`,
   `projects`, plus auxiliary tables) directly, not secondary write-ups. Write
   down the exact fields available per node/edge — this is the input contract
   for the adapter in 5.2.
4. **Supply-chain check.** Confirm signed/checksummed releases are actually
   verifiable in your build (SHA-256 in `checksums.txt`), and decide now
   whether you vendor a pinned binary version or require it as a separate
   install step (this decision is revisited in 5.3, but the trust check
   happens here, before any code depends on it).

**Exit gate:** license confirmed compatible, determinism confirmed in `full`
mode, schema documented, binary provenance verified. If any of these fail,
stop and don't proceed to 5.2.

---

## Phase 5.2 — `TreeSitterFrontend` adapter

A new `CompilerFrontend` implementation, one per non-Python language family CBM
supports via Hybrid LSP first (TypeScript/JS, Go, Java, Rust, C#, PHP — the
languages with real type resolution, not just textual fallback).

- `discover_files`: delegate to CBM's own discovery (it already respects
  `.gitignore`); no reimplementation needed.
- `parse` / `resolve`: invoke `codebase-memory-mcp index <path> --mode full
  --json` (or read `graph.db` directly via SQLite if `--json` proves lossy on
  inspection) as a subprocess. This is the one new architectural piece: a
  Python frontend that shells out to a compiled binary instead of using a
  Python library, unlike every existing frontend.
- `emit_ir`: **this is the real work.** Translate CBM's raw
  `(path, name/qualified-name-equivalent, kind)` records into StackMind's IR by
  running them through `birth_key()` exactly as the Python frontend does.
  CBM's own node identifiers are not used as StackMind identifiers — they are
  raw input to your minting function. This preserves RFC-001's identity model
  unchanged: CBM becomes a symbol *source*, not a NodeID *source*.
- Map CBM's `CALLS` / `RESOLVED_CALLS` / import edges onto StackMind's existing
  edge kinds; anything CBM detects that doesn't have a home yet (HTTP routes,
  cross-service links) gets recorded as unmapped and logged, not dropped
  silently and not force-fit into an existing edge type.

**Exit gate:** one language (recommend TypeScript — largest overlap with
Python shops likely to adopt StackMind, and one of CBM's most mature Hybrid
LSP passes) compiles through the adapter into a valid StackMind IR that passes
the existing IR schema validation, with birth-hash IDs correctly minted.

---

## Phase 5.3 — Packaging

Right now `pip install stackmind` has zero native dependencies. This changes
that, and it should change deliberately, not as a side effect discovered at
release time.

Two options, pick one and record why:
- **Vendor pinned per-platform binaries** the way CBM itself does (their
  install script already handles platform detection — study
  `scripts/setup.sh` before reimplementing it).
- **Require CBM as a separate prerequisite**, with `stackmind` detecting its
  absence and failing with a clear install instruction, only when a
  non-Python language is actually encountered in the repo (Python-only repos
  never need it — the dependency should be lazy, not eager).

Recommend the second for the MVP: it keeps the Python-only install path
untouched and zero-dependency, and only asks for the extra binary when someone
actually points StackMind at a polyglot repo.

---

## Phase 5.4 — Optional: benchmark Hybrid LSP against Jedi for Python

Not required, but worth doing once 5.1-5.3 exist: CBM's Hybrid LSP pass also
covers Python. Run both frontends against the same fixture set and compare
resolution accuracy on the cases Jedi is known to struggle with (dynamic
imports, some metaclass patterns). If CBM's pass is measurably better, that's
a future decision about the Python frontend itself — not something to act on
now, just something worth having data on before assuming Jedi is permanently
the right choice.

**This phase produces a memo, not a code change.**

---

## Phase 5.5 — Fallback if Phase 5.1 fails

If the determinism check in 5.1 fails and CBM can't give byte-identical output
in `full` mode, the fallback is narrower, not a full retreat to building
tree-sitter integration from scratch:
- Use CBM in read-only, best-effort mode for non-Python languages, explicitly
  excluded from the determinism contract (tag its IR output as
  `advisory: true` rather than `verified: true`), while the identity/rename/
  determinism guarantees continue to apply only to the Python frontend's
  output.
- This is a real product decision (StackMind would then have two tiers of
  trust in its own graph) and should be surfaced to users in the CLI output
  (`stale`/`semantic` fields already exist for this kind of signal — extend
  the pattern rather than inventing a new one), not buried in docs.

---

## Sequencing

This plan assumes PLANv4's Phase 1.7 (scope-violation end-to-end test, D025
code enforcement) lands first. Reasoning: multi-language breadth adds surface
area to a governance story that still has two open enforcement gaps on the
one language it fully covers today. Closing those first means every new
language this plan adds inherits a governance layer that's actually complete,
not one with known holes multiplying across languages.

**Recommended order:** PLANv4 Phase 1.7 → PLANv5 Phase 5.1 (verify, cheap, can
run in parallel with 1.7 if desired since it's read-only investigation) →
5.2 → 5.3 → ship TypeScript as the first non-Python language → 5.4 (optional
memo) → repeat 5.2/5.3 per additional language family.

---

## Exit gate for PLANv5 as a whole

One non-Python language (TypeScript) compiles end-to-end through the new
`TreeSitterFrontend` adapter, produces valid birth-hash IDs, passes existing
IR schema validation, and a contract-scoped agent can be correctly blocked
from writing outside its allowed TypeScript modules — proving the governance
layer generalizes across the frontend boundary, which was always the actual
point of doing this.
