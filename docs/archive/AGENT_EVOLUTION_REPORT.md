# Multi-Agent Team Evolution Report

**Date:** 2026-05-24  
**Sessions Completed:** 26 (Claude), 16+ (Gemini), multiple (Codex, Gemma, Local-LLM)

---

## Claude (Senior Architect)

**Learned:**
- Snapshot-based boot sequences dramatically reduce context waste — reading only what changed vs. full state
- Tree versioning enables reliable state synchronization across async agent sessions
- Inbox routing with `_read/` folders prevents duplicate processing
- Work order atomicity matters — one clear deliverable per WO reduces coordination overhead

**Improvements:**
- Evolved from verbose planning to minimal, actionable work orders
- Shifted from micromanaging implementation to trusting worker agents with clear specs
- Learned to batch related changes (WO-014 + WO-015) when dependencies are tight
- Better at recognizing when to escalate vs. decide autonomously

---

## Gemini (Frontend Lead)

**Learned:**
- Component isolation enables parallel work without merge conflicts
- CSS-in-JS patterns (Tailwind) reduce style collision across sessions
- Incremental delivery (small PRs) gets faster QA turnaround
- Animation timing requires iteration — specs rarely capture "feel" on first pass

**Improvements:**
- Faster at interpreting design intent from minimal specs
- Better at self-testing before sending to Gemma
- Learned to document hover states, transitions, edge cases in completion notices
- Reduced rework cycles from ~3 to ~1 per feature

---

## Codex (Backend Lead)

**Learned:**
- API contracts must be locked before frontend work begins
- Data validation at boundaries prevents downstream bugs
- Stateless design simplifies multi-agent coordination

**Improvements:**
- Cleaner separation between data layer and presentation
- Better at anticipating frontend needs in API responses
- Learned to include example payloads in specs

---

## Gemma (QA Lead)

**Learned:**
- Review checklists catch more issues than ad-hoc inspection
- Blocking early saves total project time (vs. late-stage rework)
- Accessibility and responsive checks need explicit inclusion

**Improvements:**
- Faster turnaround on reviews (minutes, not hours)
- More precise feedback — specific line references, not vague concerns
- Learned to approve with minor notes vs. blocking on trivial issues
- Better calibration on what constitutes a blocker

---

## Local-LLM (GitOps Lead)

**Learned:**
- Atomic commits with clear messages enable bisect debugging
- Branch hygiene (feature → develop → main) prevents release chaos
- `.sync` repo commits preserve agent state across sessions

**Improvements:**
- Reliable commit-push-verify cycle
- Learned to checkpoint `.sync` state before shutdown
- Better at coordinating release timing with QA approval
- Reduced manual intervention in merge workflows

---

## Team-Wide Learnings

1. **Protocol > Improvisation** — Defined handoff formats eliminated ambiguity
2. **Async-first works** — Agents don't need real-time coordination if state is explicit
3. **Trust but verify** — Workers propose, architects commit, QA gates
4. **Minimal context loading** — Version checks before full reads saved significant tokens
5. **Explicit shutdown** — Handoff reports prevent lost work across sessions

---

## What We'd Do Differently

- Start with inbox routing from day one (early sessions had message collisions)
- Define work order schema earlier (inconsistent formats caused parsing issues)
- Add graph versioning sooner (late addition in D023.3)
- More explicit "blocked" states in TREE.yaml

---

This team evolved from chaotic multi-agent experiments to a functioning async engineering org. The key insight: **treat agent coordination like distributed systems — explicit state, clear contracts, idempotent operations.**
