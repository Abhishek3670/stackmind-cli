# CBM Dependency Investigation Report

**Date:** 2026-07-24
**Subject:** Evaluation of `codebase-memory-mcp` (CBM) as the Universal Polyglot Frontend
**Status:** **BLOCKED / ABORTED**

## Executive Summary
Per `PLAN.md` Phase 1 (Prove the Dependency), an investigation was conducted to verify if `codebase-memory-mcp` is suitable to serve as the underlying engine for StackMind's Multi-Language Universal Frontend. While the package met the licensing and supply-chain requirements, it **failed the critical determinism test** due to severe instability.

As a result, the Multi-Language Frontend initiative has been paused.

## Phase 1 Checks & Results

### 1. License Verification: ✅ PASS
* **Result:** The `npm` registry confirms the package (`codebase-memory-mcp@0.9.0`) is distributed under the **MIT License**.
* **Impact:** Compatible with StackMind's distribution requirements.

### 2. Supply-Chain Check: ✅ PASS
* **Result:** The binary is published by the verified author `DeusData`. The npm package includes `.shasum` and `.integrity` (SHA-512) signatures which validate successfully upon installation.

### 3. Determinism Test: ❌ CRITICAL FAILURE
* **Methodology:** We attempted to index two different repositories using `npx codebase-memory-mcp cli index_repository --mode full`.
    1. The main StackMind codebase.
    2. A clean, minimal fixture repository containing a single 1-line Python file and a single 1-line TypeScript file.
* **Result:** In all scenarios, the CBM indexer crashed immediately with `exit_code=1`.
* **Error Log Output:**
  ```json
  {"status":"error","outcome":"exit_nonzero","hint":"Indexing worker crashed on a file. The crash was contained (the server survived). Re-run to retry; a future release isolates the culprit file."}
  ```
* **Impact:** Because the binary completely fails to parse even trivial files, it cannot produce byte-identical output across runs. It is incapable of meeting StackMind's strict determinism guarantees.

### 4. Schema Inventory: ❌ BLOCKED
* **Result:** Blocked by the determinism failure. Because the binary crashes before generating an output payload, we cannot extract or document the schema for the adapter's input contract.

## Conclusion & Next Steps
`codebase-memory-mcp` v0.9.0 is fundamentally too unstable to be used as a backend engine for StackMind. 

Even relying on the "Determinism Fallback" (Phase 4 of `PLAN.md` — using CBM in a read-only, best-effort advisory mode) is impossible, as the tool does not yield any output whatsoever before crashing.

**Decision:** 
* The Multi-Language Frontend implementation is officially PAUSED.
* The team should wait for a more stable release of CBM (where the "future release isolates the culprit file" patch is shipped) or evaluate alternative parsers.
