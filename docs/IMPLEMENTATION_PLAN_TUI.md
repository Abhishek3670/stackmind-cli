# StackMind TUI — Final Frontend Implementation Plan

**Repository:** `Abhishek3670/stackmind-cli`  
**Branch:** `feat/p6-open-source-tui`  
**Current Python package version:** `3.3.0`  
**Frontend:** Rich-first, chat-first, OpenCode-inspired  
**Launch:** `stackmind tui`  
**Commands:** existing `:` colon commands  
**Status:** Implementation-ready

---

## 1. Objective

Implement the final StackMind TUI around the approved UX:

- compact full-width StackMind landing block;
- persistent right-side StackMind Runtime panel;
- full-width conversation with small horizontal padding;
- left-aligned user/assistant messages;
- `✦ StackMind` assistant identity;
- dynamic turn-specific Actions section;
- provider-generated thinking/reasoning when genuinely available;
- clean final assistant responses;
- fixed bottom composer;
- responsive terminal behavior;
- existing StackMind commands and daemon architecture preserved.

The UI should feel as simple as OpenCode while exposing StackMind's core runtime truthfully.

---

# 2. Mandatory Codebase Research Before Changes

**Do not implement from this plan alone.** Before changing code, perform a short research pass against the actual target branch.

Inspect at minimum:

```text
cli/tui/app.py
cli/main.py
cli/tui.py
validators/kernel/tui/client.py
validators/kernel/tui/adapter.py
validators/kernel/tui/views.py
validators/kernel/daemon/
validators/kernel/session.py
relevant provider/backend implementation
relevant event definitions
existing TUI/runtime tests
pyproject.toml
cli/__init__.py
```

Confirm before coding:

- which TUI entrypoint is active;
- how the current Rich UI is rendered;
- actual RPC methods and payloads;
- actual event names/payloads/sequencing;
- actual role, Work Order, and operation structures;
- actual project initialization data;
- provider response structure;
- whether thinking/reasoning events exist;
- whether token streaming exists;
- current reconnect/history behavior;
- current input/cancellation behavior;
- existing autonomous-delivery state and renderers.

### Research rule

If something is uncertain, **clarify or inspect before implementing**. Do not invent an API, event type, state, file, dependency, or runtime capability merely because the UI needs it.

The implementation priority is:

> **Reuse existing code first → refactor only when justified → add only what the research proves is necessary.**

Record any unresolved limitation rather than silently guessing.

---

# 3. Verified Architectural Constraints

## Rich-first

The current project uses Rich and does not include Textual. The implementation should remain Rich-first. Do not add Textual unless the project explicitly changes framework direction.

## Active TUI

The active TUI target is `cli/tui/app.py`. The older `cli/tui.py` prototype is not the primary implementation target unless research proves otherwise.

## Launch

Keep:

```bash
stackmind tui
```

Do not change bare `stackmind` into the TUI launcher as part of this plan.

## Version

Use the authoritative Python package/application version. The current Python package version is `3.3.0`.

Do not hard-code `3.3.0` into the renderer and do not treat the stale static `VERSION` file as a conflicting authority.

## Commands

StackMind commands use `:`:

```text
:help
:status
:diff
:matrix
:events
:approve
:reject
:pause
:resume
:cancel
```

Do not convert these to slash commands.

Do not add a new slash-command parser or autocomplete system.

A slash-prefixed input remains ordinary prompt input unless existing code explicitly says otherwise.

---

# 4. Final UI Model

The UI has four distinct information layers:

### Landing

Identity + current session context.

### Conversation

What the user and StackMind say to each other.

### Turn Actions / Thinking

What actually happened during the current turn.

### Runtime panel

What StackMind's core runtime is doing right now.

These layers must not be conflated.

```text
Conversation
  = what StackMind tells me

Turn Actions
  = what happened for this prompt

Thinking / Reasoning
  = genuine provider-generated reasoning/status, when available

Runtime panel
  = current live StackMind architecture state
```

---

# 5. Landing Block

The landing block spans the full terminal content width while the session is empty.

Final approved structure:

```text
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│                    ✦  StackMind v3.3.0                       │
│                                                              │
│          Your AI development partner, with control.          │
│              PLAN · BUILD · VERIFY · GOVERN                  │
│                                                              │
│                 Build better. Safer. Together.               │
│                                                              │
│──────────────────────────────────────────────────────────────│
│                                                              │
│       Status: ● online                                       │
│       Session: a295c255                                      │
│       Project: ~/projects/stackmind                          │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### Locked decisions

- outer border: keep;
- no inner metadata box;
- one horizontal separator before metadata;
- first line is `✦ StackMind v<version>`;
- tagline is kept and muted;
- `PLAN · BUILD · VERIFY · GOVERN` stays centered;
- `Build better. Safer. Together.` stays centered and muted;
- no shortcut/help rows;
- no `/ Start chatting` hint;
- no command hints inside landing.

### Metadata order

```text
Status: ● online
Session: <actual session id>
Project: <actual project path>
```

Values must be runtime/project-derived.

---

# 6. Persistent Runtime Panel

The Runtime panel is visible from the first screen, including the empty landing state.

It is the permanent home for live StackMind architectural state.

## Layout

```text
Conversation                                  │ StackMind Runtime
                                             │
                                             │ AGENTS
                                             │ ...
                                             │
                                             │ WORK ORDERS
                                             │ ...
                                             │
                                             │ CURRENT OPERATION
                                             │ ...
```

Approximate width:

```text
Conversation: 70–75%
Runtime:      25–30%
```

Use a responsive width range with practical min/max bounds.

## Runtime header

Use a small muted heading:

```text
StackMind Runtime
```

## Sections

Exactly this conceptual order:

```text
StackMind Runtime

AGENTS
...

────────────────────

WORK ORDERS
...

────────────────────

CURRENT OPERATION
...
```

Visual emphasis:

```text
AGENTS         strongest
WORK ORDERS    secondary
CURRENT OP     compact
```

No nested heavy cards.

---

# 7. Runtime Panel Must Be Dynamic

Never hard-code the agent roster.

Do not assume the project always contains:

```text
Architecture
Backend
Frontend
Q/A
GitOps
```

The panel must reflect whatever the initialized project actually defines.

```text
Project initialization
        ↓
Actual runtime state
        ↓
Agents / hierarchy / Work Orders / operations
        ↓
Runtime panel
        ↓
Live updates
```

No synthetic agents, Work Orders, or operations.

---

# 8. Agent Hierarchy

Primary Runtime presentation:

```text
AGENTS

◉ Architecture    orchestrating
  ├─ ● Backend    running
  ├─ ○ Frontend   waiting
  ├─ ○ Q/A        waiting
  └─ ○ GitOps     waiting
```

The structure above is illustrative only. Render actual runtime data.

### States

Normal agents:

```text
idle
waiting
running
completed
```

Architecture has one additional state:

```text
orchestrating
```

This is used when Architecture is coordinating autonomous Work Order execution.

Use subtle symbols/text:

```text
◉ orchestrating
● running
✓ completed
○ waiting
○ idle
× failed
⊘ cancelled
! blocked
```

Do not invent intermediary states.

---

# 9. Runtime Updates

Runtime state changes are event-driven.

Example:

```text
Backend: waiting
        ↓ actual runtime event
Backend: running
        ↓ actual runtime event
Backend: completed
```

Update immediately and subtly.

Do not animate fake intermediate states.

Do not continuously redraw when no relevant state has changed.

---

# 10. Runtime Panel Scrolling

The Runtime panel has an independent vertical scroll position.

If at bottom:

```text
follow live updates
```

If manually scrolled upward:

```text
preserve viewport
```

Show a subtle indicator such as:

```text
↓ New runtime activity
```

when new state arrives outside the current viewport.

Returning to the bottom resumes live-following.

---

# 11. Runtime Interaction Policy

The Runtime panel is primarily display-only.

Do not add:

- mouse navigation;
- row selection;
- keyboard navigation inside the Runtime hierarchy;
- click-to-inspect agent rows;
- general mouse interaction.

The one deliberate mouse exception is the current-turn Actions disclosure.

---

# 12. Narrow Terminal Behavior

When the terminal is too narrow to maintain a readable two-column layout:

```text
hide Runtime panel temporarily
retain conversation + composer
```

Do not squeeze labels into unreadable widths.

Runtime information remains available through existing contextual views.

---

# 13. Conversation Layout

Conversation uses the full available terminal width with small horizontal padding.

Do not use a narrow centered chat column.

Do not let text touch terminal edges.

All normal conversation content is left-aligned.

Example:

```text
  You

  Add authentication to the API.

  ✦ StackMind

  I'll inspect the existing authentication flow first.
```

---

# 14. User Message Styling

User messages are visually quiet:

```text
You

Add authentication to the API.
```

No user icon, heavy card, or large background.

---

# 15. Assistant Identity

Use exactly:

```text
✦ StackMind
```

for assistant responses.

Keep it subtle, consistent, left-aligned, and unboxed.

---

# 16. Conversation Spacing

User messages are more compact.

Assistant responses receive slightly more breathing room.

Example:

```text
You

Explain this function.

✦ StackMind

Here's what I found.

The current implementation has three issues:

1. ...
2. ...
3. ...
```

Do not introduce excessive gaps.

---

# 17. No Per-Message Timestamps

Do not show timestamps on normal user/assistant messages.

Timestamps remain available in:

- expanded Actions;
- `:events`;
- diagnostic/detail views.

---

# 18. Dynamic Turn Structure

Each assistant turn can contain three independently populated regions:

```text
✦ StackMind

[Turn-specific StackMind/tool actions]

[Provider-generated thinking/reasoning, when available]

[Final assistant response]
```

These are **dynamic**, not static stages.

A turn may have:

- actions but no thinking;
- thinking but no tool action;
- both;
- neither diagnostic layer before the final response.

The frontend must only render data that actually exists upstream.

---

# 19. Turn-Specific Actions

Turn Actions describe what actually happened for the current prompt.

Examples:

```text
Read src/auth.py
Read tests/test_auth.py
Run harness
Verify changes
```

They belong in the assistant's current turn because they explain the execution performed for that request.

They do **not** replace or duplicate the Runtime panel.

---

# 20. Actions Group

Show actions as one compact disclosure group.

Default:

```text
▸ Actions · 4 completed
```

Expanded:

```text
▾ Actions · 4 completed
  ✓ Read src/auth.py
  ✓ Read tests/test_auth.py
  ✓ Run harness
  ✓ Verify changes
```

Do not use multiple heavy bordered cards for one turn.

---

# 21. Actions Dynamic State

While active:

```text
▸ Actions · 3
```

Expanded:

```text
▾ Actions · 3
  ✓ Read src/auth.py
  ✓ Read tests/test_auth.py
  ● Run harness
```

When the same invocation completes:

```text
✓ Run harness
```

Do not append a second completed card for the same invocation.

Use actual event identifiers and schema to correlate state changes.

Distinct invocations remain distinct.

---

# 22. Actions Expansion Behavior

Locked behavior:

- collapsed by default;
- expandable/collapsible;
- current-turn state is preserved while the UI is open;
- expanded state shows individual actual actions;
- completed turns remain collapsed by default unless the user already expanded them.

---

# 23. Mouse Exception

General TUI interaction is keyboard-first.

The single approved mouse interaction is:

```text
▸ Actions · N
```

Mouse click expands it.

```text
▾ Actions · N
```

Mouse click collapses it.

Keyboard interaction must also be available.

Do not expand mouse support elsewhere.

---

# 24. Provider Thinking / Reasoning

`Thinking...` is **not** a static frontend status.

If the upstream provider/runtime exposes genuine thinking/reasoning/status information, render it.

Example:

```text
Thinking...
  Inspecting the existing authentication middleware...
```

If no such upstream capability exists:

```text
do not fabricate Thinking...
```

Do not use heuristic fake reasoning strings.

---

# 25. Current Streaming Reality

The current codebase does not provide token-level LLM streaming.

The daemon can expose discrete operational events, while model output may arrive as a completed result.

Therefore this TUI phase must not claim token streaming.

Support:

```text
live runtime/tool events
```

and:

```text
completed assistant content
```

until the runtime genuinely supports token/chunk streaming.

---

# 26. Future Provider Streaming

If provider thinking/reasoning or token streaming becomes a requirement, first research the actual provider and daemon path.

Potential future boundary:

```text
Provider
  ↓
ModelExecutionBackend
  ↓
AgentRunner
  ↓
Daemon event protocol
  ↓
DaemonClient
  ↓
TUI
```

Only implement the frontend consumer after the upstream event contract exists.

---

# 27. Final Assistant Content

The final assistant response should remain clean and human-readable.

Example:

```text
✦ StackMind

▸ Actions · 4 completed

Thinking... ✓

The authentication flow has been updated.
```

The exact presence/order of Actions and Thinking is data-dependent.

Do not show empty placeholder sections.

---

# 28. Activity Density

Do not flood the chat with raw lifecycle messages such as:

```text
turn submitted
operation started
turn started
prompt repeated
operation completed
```

Routine lifecycle events should feed the appropriate current-turn Actions group or Runtime state.

Keep detailed event history available through existing diagnostic/event views.

---

# 29. Composer

The composer stays fixed at the bottom.

It has the strongest border in the interface.

Default:

```text
┌──────────────────────────────────────────────────────────────┐
│ > Type a message...                                      ↵   │
└──────────────────────────────────────────────────────────────┘
```

Avoid oversized controls or large filled backgrounds.

---

# 30. Composer Behavior

### Default height

One line.

### Multiline

Expand vertically when the user enters multiline content.

### During active operations

Remain usable while StackMind is working.

Do not disable input merely because an operation is active.

Actual concurrent submission semantics must follow the daemon; do not invent client-side concurrency rules.

### Focus

After an operation completes, restore composer focus when appropriate, but never steal focus from a user who is actively typing.

Live updates must preserve partially typed input.

---

# 31. Conversation Auto-Scroll

If the user is at the bottom:

```text
new content → follow automatically
```

If the user has scrolled upward:

```text
new content → preserve viewport
```

Show:

```text
↓ New activity
```

where useful.

Returning to the bottom restores live-follow.

---

# 32. Code and Diff Presentation

Use existing Rich Markdown/Syntax capabilities.

Support:

- headings;
- lists;
- inline code;
- fenced code;
- syntax highlighting.

Large code and diff blocks should scroll independently when needed.

Short blocks remain naturally embedded in the answer.

---

# 33. Diff

Keep diffs inline with the conversation.

Use the existing diff rendering capability where practical.

Do not create a permanent diff pane.

---

# 34. Contextual StackMind Views

StackMind-specific diagnostic/detail views remain available through the existing command system.

Do not put every diagnostic surface permanently on screen.

The default workspace remains:

```text
Conversation │ Runtime
```

---

# 35. Command Policy

Preserve the existing `:` command model.

Do not add slash-command autocomplete.

Do not add a replacement command registry.

Do not change `:` commands to `/` commands.

Do not advertise a shortcut until its behavior has been confirmed in the actual codebase.

---

# 36. Runtime vs Actions — Non-Negotiable

### Runtime panel

Shows:

```text
AGENTS
WORK ORDERS
CURRENT OPERATION
```

Meaning:

> What StackMind's core architecture is doing right now.

### Actions group

Shows:

```text
Read file
Run harness
Verify changes
...
```

Meaning:

> What actually happened during this specific prompt/turn.

### Assistant body

Meaning:

> The final response to the user.

### Provider Thinking

Meaning:

> Genuine provider-generated reasoning/status when available.

Never merge these layers into one generic activity stream.

---

# 37. Initialization

The Runtime panel must initialize before the first prompt.

Startup flow:

```text
stackmind tui
    ↓
connect to daemon
    ↓
read actual project/runtime state
    ↓
populate Runtime panel
    ↓
render landing
    ↓
focus composer
    ↓
start live runtime/event updates
```

The runtime panel must not wait for the first user message.

---

# 38. Reconnect

When connection is interrupted:

```text
● online
```

becomes:

```text
○ reconnecting
```

After recovery:

```text
● online
```

Recovery should:

```text
restore session
recover missing events
reconstruct visible state
avoid duplicate visible content
resume live updates
```

Do not assume `session.history` is the chat transcript.

---

# 39. Conversation Recovery

Before implementing chat restoration, inspect the actual event schema and persistence model.

Do not assume:

```text
session.history == chat transcript
```

Use the actual sources containing prompt/result content, such as relevant `event.list` records if confirmed by source inspection.

If the available persisted data is insufficient for full transcript restoration, document the limitation rather than inventing a new persistence protocol in the TUI.

---

# 40. Timeout UX

Distinguish:

```text
client wait timeout
```

from:

```text
authoritative daemon operation timeout
```

A client-side timeout must not falsely claim that execution stopped.

Example:

```text
Request timeout

No response received within the client wait time.
The operation may still be running.
Use :status or :events to inspect it.
```

Late completion must appear once.

Cancellation must use the existing daemon cancellation path.

---

# 41. Responsive Layout

Target at least:

```text
80 × 24
100 × 30
120 × 40
160 × 50
```

At wide widths:

```text
Conversation 70–75%
Runtime 25–30%
```

At narrow widths:

```text
Runtime hidden temporarily
Conversation + composer remain usable
```

Long project paths should be shortened visually.

Essential state must never be clipped.

---

# 42. Visual Language

Recommended restrained palette:

```text
Primary       light/white
Secondary     muted gray
StackMind     purple
Accent        cyan/blue
Success       green
Error         red
Warning       yellow
```

Use color primarily for state, not decoration.

---

# 43. Border Hierarchy

```text
Landing outer border   subtle
Landing metadata       no inner box
Conversation           mostly borderless
Actions                compact
Tool/Diff              subtle container
Runtime                divider + subtle separators
Composer               strongest border
```

The Runtime divider is a single subtle vertical line.

The landing uses an outer border and a single internal horizontal separator before metadata.

---

# 44. Keyboard / Mouse Policy

Keyboard-first behavior remains the default.

No general mouse UI.

One approved mouse exception:

```text
Actions disclosure
```

Do not add click-driven Runtime navigation.

---

# 45. Refactoring Strategy

The active `cli/tui/app.py` is already substantial and contains existing delivery/state behavior.

Do not rewrite it blindly.

First research which existing pieces already implement:

```text
project phase
roles
work orders
operation tree
activity state
plan/approval
completion
```

Then refactor presentation incrementally.

Preferred sequence:

```text
research
 ↓
identify reusable state/renderers
 ↓
extract only necessary presentation code
 ↓
introduce final landing/chat/runtime layout
 ↓
regression-test existing behavior
```

Do not create a parallel TUI package merely to match a hypothetical architecture.

---

# 46. Testing — Baseline First

Before changing code:

```text
run existing test suite
launch stackmind tui
record known failures
inspect current TUI behavior
```

Separate pre-existing failures from regressions.

---

# 47. Rendering Tests

Add deterministic tests for:

- landing;
- version;
- landing metadata order;
- Runtime panel;
- dynamic agent hierarchy;
- Work Orders;
- Current Operation;
- user messages;
- assistant messages;
- Actions collapsed;
- Actions expanded;
- diff;
- errors;
- narrow layouts.

Prefer pure rendering/state tests when possible.

---

# 48. Runtime Tests

Verify:

- Runtime panel initializes before first prompt;
- actual project roles are used;
- actual Work Orders are used;
- actual operation relationships are used;
- state transitions come from runtime events/state;
- idle state is rendered without placeholder data;
- no synthetic hierarchy is created.

---

# 49. Actions Tests

Verify:

- collapsed by default;
- expands/collapses with keyboard;
- expands/collapses with the approved mouse interaction;
- one invocation maps to one visible activity entry;
- running → completed updates in place;
- failed/cancelled states render accurately;
- separate invocations remain separate.

---

# 50. Input / Focus Tests

Verify:

- composer remains usable during operations;
- typed text survives Rich Live updates;
- Enter submission works;
- multiline composer expands;
- completion does not steal focus from active typing;
- current Ctrl+C behavior remains correct;
- no unsupported shortcut is advertised.

---

# 51. Scroll Tests

Verify:

- conversation auto-follows only at bottom;
- upward scrolling preserves position;
- new-content indicator appears when necessary;
- Runtime scroll is independent;
- Runtime auto-follow works only at its bottom;
- large code/diff content can scroll independently.

---

# 52. Thinking / Streaming Tests

Before implementing provider thinking UI:

```text
inspect provider response
inspect backend behavior
inspect daemon event schema
```

If genuine provider thinking data exists, test rendering and updates.

If not, verify that no fake thinking text is generated.

Do not write tests for token streaming until token streaming actually exists upstream.

---

# 53. Reconnect / Timeout Tests

Test:

```text
disconnect → reconnect → event replay
```

and:

```text
client wait timeout
late completion
cancellation
```

Verify no duplicate visible result and no false stopped/failed state.

---

# 54. Command Regression Tests

Existing colon commands must remain functional according to the active implementation.

Examples:

```text
:help
:status
:diff
:matrix
:events
:approve
:reject
:pause
:resume
:cancel
```

Do not introduce slash-command dispatch.

---

# 55. Manual UX Validation

Perform real terminal testing for:

1. launch empty session;
2. Runtime panel population;
3. landing appearance;
4. first prompt transition;
5. active-runtime updates;
6. current-turn Actions updates;
7. Actions mouse disclosure;
8. Actions keyboard disclosure;
9. typing during live updates;
10. conversation scrolling;
11. Runtime scrolling;
12. large code/diff scrolling;
13. timeout;
14. reconnect;
15. terminal resize;
16. clean exit.

---

# 56. Implementation Phases

## Phase 0 — Research

Confirm the actual codebase behavior before changes.

## Phase 1 — Preserve current runtime semantics

Ensure daemon/client/adapter/state behavior remains stable.

## Phase 2 — Final landing

Implement the locked landing design and authoritative version source.

## Phase 3 — Two-column workspace

Implement:

```text
Conversation │ Runtime
```

with responsive width and narrow-terminal fallback.

## Phase 4 — Dynamic Runtime panel

Implement actual:

```text
AGENTS
WORK ORDERS
CURRENT OPERATION
```

from project/runtime data.

## Phase 5 — Conversation styling

Implement:

```text
You
✦ StackMind
```

plus clean Rich Markdown/code rendering.

## Phase 6 — Turn Actions

Implement the dynamic collapsed-by-default Actions disclosure.

## Phase 7 — Provider thinking

Implement only if actual upstream thinking/reasoning data exists.

## Phase 8 — Composer/scroll/focus

Implement fixed composer, multiline behavior, auto-scroll, independent Runtime scrolling, and focus preservation.

## Phase 9 — Reconnect/timeout/error handling

Use authoritative runtime state and existing event mechanisms.

## Phase 10 — Polish

Responsive sizing, typography, separators, status symbols, and terminal compatibility.

## Phase 11 — Hardening

Run the complete automated and manual validation plan.

---

# 57. Explicit Non-Goals

Do not implement:

- Textual migration;
- fake token streaming;
- fake provider thinking;
- direct provider calls from TUI;
- direct filesystem execution;
- direct subprocess execution;
- second runtime;
- second persistence authority;
- second event transport;
- hard-coded role roster;
- synthetic Work Orders;
- synthetic operation-tree steps;
- new slash-command parser;
- new slash-command autocomplete;
- general mouse UI;
- permanent telemetry dashboard;
- bare `stackmind` launch behavior change.

---

# 58. Final Acceptance Layout

## Empty session

```text
┌───────────────────────────────────────────────────────────────┬──────────────────────┐
│                                                               │ StackMind Runtime    │
│                    ✦  StackMind v3.3.0                        │                      │
│                                                               │ AGENTS               │
│          Your AI development partner, with control.           │ <actual agents>      │
│              PLAN · BUILD · VERIFY · GOVERN                   │                      │
│                                                               │ WORK ORDERS          │
│                 Build better. Safer. Together.                │ <actual work>        │
│                                                               │                      │
│───────────────────────────────────────────────────────────────│ CURRENT OPERATION    │
│       Status: ● online                                        │ <actual operation>  │
│       Session: a295c255                                       │                      │
│       Project: ~/projects/stackmind                           │                      │
└───────────────────────────────────────────────────────────────┴──────────────────────┘
│ > Type a message...                                                           ↵    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

## Active conversation

```text
┌───────────────────────────────────────────────────────────────┬──────────────────────┐
│ You                                                           │ StackMind Runtime    │
│                                                               │                      │
│ Add authentication to the API.                                │ AGENTS               │
│                                                               │ ◉ Architecture       │
│ ✦ StackMind                                                   │   ├─ ● Backend       │
│                                                               │   ├─ ○ Frontend      │
│ ▸ Actions · 4 completed                                      │   ├─ ○ Q/A           │
│                                                               │   └─ ○ GitOps        │
│ Thinking...                                                   │                      │
│   <only if genuinely supplied>                                │ WORK ORDERS          │
│                                                               │ ...                  │
│ The authentication flow has been updated.                     │                      │
│                                                               │ CURRENT OPERATION    │
│                                                               │ ● Backend            │
└───────────────────────────────────────────────────────────────┴──────────────────────┘
│ > Type a message...                                                           ↵    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

---

# 59. Definition of Done

- [ ] Mandatory codebase research completed before implementation.
- [ ] Any uncertainty was resolved by inspection or explicit clarification.
- [ ] Unnecessary files/code/dependencies were avoided.
- [ ] Rich-first approach preserved.
- [ ] Active `cli/tui/app.py` behavior preserved/refactored rather than bypassed.
- [ ] `stackmind tui` remains the launch command.
- [ ] Bare `stackmind` remains unchanged.
- [ ] Landing matches the approved design.
- [ ] Landing contains no shortcut/help rows.
- [ ] Landing shows authoritative `v3.3.0` dynamically.
- [ ] Landing metadata order is Status → Session → Project.
- [ ] Runtime panel is visible from launch.
- [ ] Runtime panel remains visible while idle.
- [ ] Runtime panel is driven entirely by initialized project/runtime data.
- [ ] No hard-coded agent roster exists in presentation code.
- [ ] AGENTS / WORK ORDERS / CURRENT OPERATION are rendered from actual state.
- [ ] Runtime updates are event/state-driven.
- [ ] Runtime panel scrolls independently.
- [ ] Runtime panel is responsive and hides only when necessary on narrow terminals.
- [ ] Runtime panel has no general mouse interaction.
- [ ] Conversation uses full available width with small padding.
- [ ] Conversation is left-aligned.
- [ ] Assistant identity is `✦ StackMind`.
- [ ] Normal messages do not show timestamps.
- [ ] Turn Actions are distinct from Runtime state.
- [ ] Actions are collapsed by default.
- [ ] Actions expand/collapse via keyboard.
- [ ] Actions expand/collapse through the one approved mouse exception.
- [ ] Action lifecycle updates in place without duplicate cards.
- [ ] Provider thinking is shown only when genuinely available upstream.
- [ ] No fake Thinking text is generated.
- [ ] No fake token streaming is represented.
- [ ] Large code/diff blocks can scroll independently.
- [ ] Composer remains fixed at the bottom.
- [ ] Composer is usable during active operations.
- [ ] Composer expands for multiline input.
- [ ] Composer does not steal focus from active typing.
- [ ] Conversation auto-scroll is context-aware.
- [ ] Runtime auto-scroll is context-aware.
- [ ] Timeout behavior does not falsely imply operation termination.
- [ ] Late completions appear once.
- [ ] Reconnect recovers missed events without duplicates.
- [ ] `session.history` is not assumed to be the chat transcript.
- [ ] Existing colon commands continue to work.
- [ ] No new slash-command autocomplete/parser exists.
- [ ] No direct provider/tool/filesystem/subprocess execution exists in the TUI.
- [ ] Full suite and TUI tests pass, with baseline failures separated from regressions.
- [ ] Manual terminal validation succeeds.

---

# 60. Final Product Principle

The final StackMind TUI should make this distinction obvious at a glance:

```text
┌──────────────────────────────────────────────┬──────────────────────────┐
│                                              │                          │
│ CONVERSATION                                 │ STACKMIND RUNTIME        │
│                                              │                          │
│ What StackMind tells me                     │ What StackMind is doing  │
│                                              │ right now                 │
│ ✦ StackMind                                 │                          │
│ ▸ Actions · N                               │ AGENTS                   │
│ Thinking...                                 │ WORK ORDERS              │
│ Final response                              │ CURRENT OPERATION        │
│                                              │                          │
└──────────────────────────────────────────────┴──────────────────────────┘
```

**Simple presentation. Truthful runtime. Minimal duplication. No invented capabilities.**
