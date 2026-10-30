# StackMind Delivery Quality Investigation Report

**Date**: 2026-10-11  
**Branch**: investigate-delivery-quality  
**Baseline Commit**: c839083 fix(init): create initial git commit only for empty projects

---

## 1. Baseline Test Results

```
303 passed, 1 failed (stopping after first failure)
Failed: test_gateway_nudges_when_required_deliverable_not_written
```

**Pre-existing failure**: ProviderGateway test for deliverable nudging functionality.

---

## 2. Clean-Project Leakage Analysis

### Confirmed: StackMind branding in product deliverables

| File | Finding | Impact |
|------|---------|--------|
| `templates/README.template.md` | Contains "managed by [stackmind](https://github.com/stackmind/stackmind)" | Product README contains vendor branding by default |
| `templates/AGENTS.template.md` | Contains full StackMind runtime instructions (D021-D031, CONTRACT-01, etc.) | Agent instructions placed in product root |
| `cli/init.py:649` | `_append_stackmind_readme_section()` appends StackMind runtime note to existing README | Existing user READMEs get StackMind branding injected |
| `cli/init.py:674` | `_merge_stackmind_gitignore()` adds `.sync/` to .gitignore | Runtime paths exposed in product root |
| `cli/init.py:657-667` | CHANGELOG.md, VERSION, AGENTS.md always rendered during init | Internal files always created |

### Pipeline: Clean files created unconditionally

```
stackmind tui / stackmind init
  → cli/init:initialize()
    → render AGENTS.md, PLAN.md, CHANGELOG.md, VERSION, README.md, .gitignore
    → _append_stackmind_readme_section() for existing READMEs
```

---

## 3. Requirements Traceability (Confirmed: Missing)

**Findings**:
- No centralized requirements ledger exists
- PLAN.md tasks do not automatically map to requirements
- Work orders don't declare requirement IDs they implement
- QA verdicts don't track which requirements they verify
- Final review doesn't validate requirements coverage

**Location**: No `.sync/state/requirements.yaml` or equivalent exists.

---

## 4. Verification Issues (Confirmed)

### D024 Gate (validators/harness/d024_gate.py)

| Check | Current Behavior | Issue |
|-------|------------------|-------|
| Deliverable existence | Line 604-629: checks file exists and non-empty | Doesn't verify semantic content |
| QA verdict | Line 706-762: parses verdict files | Doesn't verify tests actually execute |
| Execution evidence | None | File-touched-in-diff used as completeness signal |

### Integration Review (validators/harness/runner.py:1616-1632)

- Prompt tells Claude to "inspect deliverables" but doesn't enforce checklist
- No enforcement that user-requested content must be verified

---

## 5. Context Retrieval (Partial)

**Findings**:
- `KnowledgeAPI.assemble_context()` exists but not tested in production harness path
- Graph compilation happens in `validators/knowledge/compiler/resolve.py`
- No test confirms semantic retrieval actually used in worker prompts

---

## 6. Release/Artifact Generation

| Path | Behavior |
|------|----------|
| `cli/init.py:657-667` | CHANGELOG.md always created |
| `validators/kernel/tools.py:1188-1208` | `create_release()` creates CHANGELOG.md unconditionally |
| `validators/harness/runner.py:1701-1709` | GitOps prompt always mentions VERSION.md/CHANGELOG.md |

---

## 7. Root Cause Ranking

| Rank | Cause | Evidence | Confidence |
|------|-------|----------|------------|
| 1 | **Clean-project leakage** | Templates and init always render internal files | CONFIRMED |
| 2 | **No requirements ledger** | No tracking from product goal to verification | CONFIRMED |
| 3 | **File-based verification** | D024 checks existence, not semantic content | CONFIRMED |
| 4 | **Integration review weak** | Prompt tells Claude to verify but doesn't enforce | CONFIRMED |
| 5 | **Release always creates artifacts** | CHANGELOG.md always created | CONFIRMED |

---

## 8. Proposed Fix Order

1. **Fix 1 - Clean-delivery separation** (highest impact, visible defect)
2. **Fix 2 - Requirements ledger** (enables traceability)
3. **Fix 3 - Content-aware verification** (prevents placeholders)
4. **Fix 4 - Execution-based QA** (enforces evidence)
5. **Fix 5 - Fail-closed review** (prevents false positives)

---

## 9. Known Unknowns

- Whether graph queries actually populate worker context (need test with empty greenfield)
- Actual worker budget/turn limits in production (need to check DEFAULT_MAX_TURNS usage)

---

## 10. Implementation Status

**Phase 1**: COMPLETE  
**Phase 2**: IN PROGRESS

### Implemented Fixes

| Commit | Change | Status |
|--------|--------|--------|
| e3f7514 | Removed StackMind branding from README template | DONE |

### Tests Added

| Test File | Coverage | Status |
|-----------|----------|--------|
| `tests/test_clean_delivery.py` | Clean delivery policy | 4 passed |
