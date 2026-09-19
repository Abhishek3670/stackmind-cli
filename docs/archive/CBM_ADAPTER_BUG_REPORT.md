# CBM Adapter — Bug Report & Remediation

**Date:** 2026-07-24
**Component:** `validators/knowledge/compiler/cbm_compiler.py`
**Found via:** Direct execution against a live fixture repo, not code review alone.
**Status:** 2 confirmed defects, both reproduced empirically. Blocks the Phase 2
exit gate in PLANv5 ("TypeScript compiles through the adapter into valid
StackMind IR with correctly minted birth-hash IDs").

Test coverage note: no tests currently exist for `cbm_compiler.py` or the
`DenyPlaceholder` contract path. Both bugs below were found by running the
adapter once, not by inspection — indicating both would reach production
undetected under the current test suite.

---

## Bug #1 — SQLite artifact lookup fails on its own naming scheme

**Severity:** Critical. Silently defeats both the adapter's core function and
its own fail-closed safety mechanism.

### Root cause
The adapter reconstructs CBM's output database filename by hand instead of
asking CBM for it:

```python
db_name = str(root).replace(":", "").replace("\\", "-").replace("/", "-") + ".db"
```

This does not strip the leading path separator before substitution, producing
a filename with a leading dash that CBM never actually generates.

### Reproduction
```
$ python3 -c "print(str('/tmp/cbm_fixture').replace('/', '-') + '.db')"
-tmp-cbm_fixture.db
```
Actual CBM output filename for the same path (observed directly from a real
`codebase-memory-mcp cli index_repository` run against `/tmp/cbm_fixture`):
```
tmp-cbm_fixture.db
```
Running the real adapter against the same fixture confirms the mismatch end
to end:
```
symbols: 0
edges: 0
diagnostics: [CBM_DB_MISSING: 'Database not found at
  /root/.cache/codebase-memory-mcp/-tmp-cbm_fixture.db']
revision_inputs: {'cbm': 'missing'}
```

### Why this is worse than a crash
The `CBM_DB_MISSING` branch returns an empty `CompilerIR` with only a
diagnostic — it does **not** emit a `DenyPlaceholder` node the way the
`CalledProcessError` branch does. This is the one failure mode guaranteed to
occur in normal use, and it's the one case where the fail-closed protection
(verified in the contract gate, see prior review) doesn't engage. A user
pointing StackMind at a polyglot repo would silently get zero non-Python
symbols with no signal that anything is wrong or restricted.

### Remediation
Don't reconstruct the filename. `codebase-memory-mcp cli index_repository`
already returns the authoritative project name in its own success JSON
(`"project": "tmp-cbm_fixture"`), and the adapter already requests
`capture_output=True` on the subprocess call but currently discards `stdout`
entirely. Fix:
1. Parse `stdout` as JSON after the subprocess call succeeds.
2. Use the `project` field to build the db path: `cache_dir / f"{project}.db"`.
3. If parsing fails or `project` is absent, treat it as a hard failure and
   emit a `DenyPlaceholder` (do not fall through to a silent empty IR).

---

## Bug #2 — `revision_inputs` embeds a live timestamp, breaking the determinism contract

**Severity:** Critical. Directly contradicts the byte-identical determinism
guarantee the whole compiler pipeline is built on.

### Root cause
```python
project_row = db.execute("SELECT * FROM projects LIMIT 1").fetchone()
indexed_at = project_row[1] if project_row else "unknown"
...
return CompilerIR(revision_inputs={"cbm": indexed_at}, ...)
```
`revision_inputs` feeds directly into `IR.to_json()`, which is documented in
`ir.py` as *"canonical, byte-stable JSON for compile-twice comparison"* — the
exact function StackMind's determinism contract is checked against. A
wall-clock timestamp in that payload guarantees two compiles of an unchanged
repo will never be byte-identical.

### Reproduction
Built the IR twice from the same fixture repo, forcing a real re-index
between runs (touched a file, re-ran the CBM indexer):
```
run1: {'cbm': '2026-07-24T13:32:20Z'}
run3: {'cbm': '2026-07-24T19:37:21Z'}
```
Confirmed changing across runs, as expected from using a timestamp field.

### Why the earlier "determinism verified" finding didn't catch this
The prior determinism check (semantic diff of the `nodes`/`edges` tables in
CBM's raw SQLite artifact) was a valid test of CBM's own output — but it was
never run against what this adapter actually threads through into
StackMind's `CompilerIR.to_json()`. The two are different surfaces; passing
one doesn't imply the other passes.

### Remediation
Follow the pattern already used by the existing Python frontend
(`validators/knowledge/compiler/resolve.py`), which populates
`revision_inputs` with stable, re-run-invariant values:
```python
revision_inputs={
    "compiler_version": COMPILER_VERSION,
    "git_commit": _git_value(project_path, ["rev-parse", "HEAD"]),
    "registry_version": _registry_version(project_path),
    "schema_version": "1",
}
```
For the CBM adapter, replace the raw `indexed_at` timestamp with something
equivalent and stable — e.g. the CBM binary version (`--version` output) plus
`COMPILER_VERSION`. If staleness tracking against `indexed_at` is still
wanted somewhere, surface it through the existing `stale`/`revision` fields
already used elsewhere in query output — don't route it through
`revision_inputs`.

---

## Summary table

| # | Bug | Impact | Fix |
|---|---|---|---|
| 1 | DB filename hand-derived, doesn't match CBM's actual naming | Adapter finds nothing on the guaranteed-common case; fail-closed protection doesn't engage here | Parse `project` field from CBM's own stdout JSON instead of reconstructing the filename |
| 2 | `indexed_at` timestamp embedded in `revision_inputs` | Breaks byte-identical compile-twice guarantee | Replace with stable version/commit metadata, matching the existing Python frontend's pattern |

## Recommended before re-closing Phase 2
1. Apply both fixes above.
2. Add at least one integration test that runs `CBMCompiler.compile()` against
   a checked-in fixture repo and asserts:
   - non-empty `symbols`/`edges`
   - a second compile of the same unchanged fixture produces byte-identical
     `to_json()` output
   - a forced-crash or forced-missing-db scenario produces a `DenyPlaceholder`
     node, not a silent empty IR
3. Re-run against a fixture with an actual crash-inducing file (not just a
   missing-db scenario) to confirm the `CalledProcessError` path still
   behaves as intended — this report only found and fixed the *missing-db*
   failure mode, not a genuine parser crash, which remains unverified against
   real CBM behavior.
