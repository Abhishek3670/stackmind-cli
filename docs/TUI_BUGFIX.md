# StackMind TUI — Bugfix Checklist

## Scope

Address issues observed in the supplied terminal transcript.
These findings are based on visible output, not source-code inspection.

Preserve the Rich-first interface, existing command dispatch, and
daemon-owned execution, state, policy, and persistence.

## 1. Command Hints Conflict With Colon Syntax

Priority: High

### Observed

The footer advertises:
`/help /status /diff /compact /sessions`

Actual interactions use colon commands such as `:status` and `:events`.

### Fix

- Display only commands supported by the active dispatch path.
- Use colon-prefixed command hints.
- Remove unsupported command hints.
- Preserve slash-prefixed input as normal prompt text.
- Do not introduce a new command parser or slash autocomplete.

### Acceptance

- Footer hints match supported colon commands.
- `/status` is submitted as prompt text.
- Existing colon commands continue to work.

## 2. Assistant Output Contains Non-Final Content

Priority: High

### Observed

The greeting includes self-directed planning before the final answer.
The response also includes a model heading and knowledge-revision metadata.

### Fix

- Inspect the actual provider/daemon response structure.
- Where structured separation exists, render final assistant content only.
- Keep diagnostic metadata outside the normal answer.
- Do not remove legitimate answer text through heuristic phrase matching.
- If reasoning and final content are not separated upstream, document
  the protocol limitation rather than claiming a presentation-only fix.

### Acceptance

- Structured non-final content is not displayed as the assistant answer.
- Normal answers retain Markdown, code, headings, and lists.
- Diagnostic metadata is available through an existing appropriate
  details or logging path.

## 3. Timeout Leaves Activity Visually Running

Priority: High

### Observed

A turn timeout appears while the preceding harness activity still
displays RUNNING. The output does not explain whether execution continues.

### Fix

- Distinguish a client wait timeout from a daemon operation timeout.
- Reconcile operation status through existing daemon mechanisms.
- Do not mark the operation failed, completed, or cancelled without
  authoritative evidence.
- Display an actionable message suggesting `:status` or `:events`.
- Handle a late completion without duplicating the assistant response.

### Acceptance

- A wait timeout does not falsely imply execution has stopped.
- Confirmed terminal states update the displayed activity.
- Unknown status is explicitly communicated.
- Late results appear once.
- Cancellation continues through the existing cancel path.

## 4. Lifecycle Details Overwhelm Conversation

Priority: Medium

### Observed

A single prompt displays submission, operation start, turn start,
repeated prompt content, and full operation identifiers.

### Fix

- Consolidate routine progress into compact inline activity.
- Avoid repeating the user's prompt in normal lifecycle output.
- Keep full identifiers and detailed events in `:events` or existing
  diagnostic views.
- Retain the underlying events; reduce presentation noise only.

### Acceptance

- Conversation remains the dominant surface.
- Routine lifecycle transitions do not produce redundant chat entries.
- Detailed operation information remains accessible.
- Progress reflects actual daemon events, not simulated token streaming.

## 5. Tool Activity Uses Duplicate Heavy Cards

Priority: Medium

### Observed

“Run harness” appears in separate bordered RUNNING and completed cards.

### Fix

- Prefer a compact activity row updated as its state changes.
- Correlate updates using actual event identifiers and schema.
- Preserve distinct invocations as distinct activities.
- Keep the event journal unchanged.

### Acceptance

- One invocation has one visible activity entry with updated status.
- Separate invocations are not accidentally merged.
- Supported terminal states render accurately.
- No synthetic operations or tool actions are introduced.

## 6. Status Metadata Needs Responsive Layout

Priority: Medium

### Observed

Session ID, state, provider, agent, workspace, and connection status
appear on one long line.

### Fix

- Prioritize session context, operation state, and connection status.
- Shorten workspace paths visually.
- Wrap or hide secondary metadata at narrow widths.
- Preserve access to full values through existing details views.

### Acceptance

- Status remains readable at narrow, medium, and wide terminal widths.
- Essential information is not clipped.
- Long paths do not displace the composer or conversation.

## 7. Verify Interaction Behavior Before Declaring Defects

Priority: Investigation

The transcript does not establish whether these behaviors work.
Inspect and test them before changing implementation.

- [ ] Landing appears for an empty conversation.
- [ ] Landing disappears after conversation begins.
- [ ] Composer stays usable during active operations.
- [ ] Rich Live refreshes preserve partially typed input.
- [ ] Repeated composer blocks are not unintended redraw artifacts.
- [ ] Ctrl+K performs the advertised supported action.
- [ ] Ctrl+L has defined behavior without deleting daemon history.
- [ ] Ctrl+C preserves existing input and cancellation semantics.
- [ ] Terminal state is restored on exit and exceptions.
- [ ] Contextual status output does not become a permanent dashboard.
- [ ] Displayed version comes from authoritative package version data.
- [ ] Roles, work orders, and operation relationships use runtime data.
- [ ] Reconnect recovers events without duplicate visible messages.

## Implementation Guardrails

- Refactor the active `cli/tui/app.py`, not superseded `cli/tui.py`.
- Reuse StackMindTuiAdapter and DaemonClient.
- Keep `stackmind tui` as the launch command.
- Leave bare `stackmind` unchanged.
- Use existing Rich dependencies; do not add Textual.
- Preserve existing runtime models and supported contextual views.
- Do not introduce a second runtime, event transport, or persistence store.
- Do not treat `session.history` as a guaranteed conversation transcript.
- Do not invent operations, verification results, or token streaming.

## Validation

- [ ] Record baseline test results and pre-existing failures.
- [ ] Add deterministic rendering tests for the changed surfaces.
- [ ] Test colon-command dispatch and slash-prefixed prompt handling.
- [ ] Test activity correlation and duplicate suppression.
- [ ] Test wait timeout, daemon timeout, late completion, and cancellation.
- [ ] Test response rendering against actual structured payloads.
- [ ] Manually test typing during live updates and terminal resizing.
- [ ] Verify behavior in Windows PowerShell, matching the reported environment.
- [ ] Run the full suite and report regressions separately from baseline failures.

## Completion Criteria

The interface presents clean assistant answers, accurate compact activity,
consistent command hints, and actionable timeout states without changing
the governed runtime or hiding access to operational details.
