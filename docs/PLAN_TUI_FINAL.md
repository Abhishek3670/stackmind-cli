# StackMind TUI — Final Codebase-Grounded Implementation Plan

**Repository:** `Abhishek3670/stackmind-cli`  
**Target branch:** `feat/p6-open-source-tui`  
**Current Python package version:** `3.3.0`  
**UI direction:** Minimal, chat-first, OpenCode-inspired  
**Command prefix:** `:` (colon)  
**Primary launch command:** `stackmind tui`

---

## 0. Purpose

Implement the final StackMind TUI shown in the approved mockup:

- minimal;
- chat-first;
- branded landing block;
- large central conversation area;
- inline tool/activity events;
- inline diffs;
- persistent bottom composer;
- contextual governance/operation views;
- existing StackMind command semantics;
- daemon-owned execution and state.

The TUI should **look simple like OpenCode** while continuing to expose StackMind's existing governed runtime.

This plan corrects the earlier implementation plan so that it does not assume APIs, dependencies, files, streaming semantics, or launch behavior that are not present in the current branch.

---

# 1. Current Codebase Reality

The implementation must begin from the code that actually runs on `feat/p6-open-source-tui`.

## 1.1 Active TUI entrypoint

The active TUI implementation is:

```text
cli/tui/app.py
```

It is already a substantial implementation containing:

- `AutonomousDeliveryState`;
- `ProjectPhase`;
- `PlanRevision`;
- `RoleStatus`;
- `WorkOrderItem`;
- `OperationNode`;
- `ActivityEntry`;
- project delivery rendering;
- phase banners;
- agent roles;
- work orders;
- operation tree;
- plan approval;
- governance controls;
- completion handover;
- event handling.

The earlier `cli/tui.py` prototype must **not** be treated as the active implementation target.

### Rule

Refactor/extend:

```text
cli/tui/app.py
```

rather than implementing a replacement in the superseded `cli/tui.py`.

---

# 2. Current CLI Launch Behavior

`cli.main:cli` is a Click group.

The installed command is:

```text
stackmind
```

and the TUI is currently a subcommand:

```text
stackmind tui
```

Bare:

```text
stackmind
```

must **not** be changed to launch the TUI as part of this plan.

The existing Click group currently uses the standard group behavior and exposes the project's CLI commands.

## Definition

```text
stackmind tui
```

is the supported TUI launch command.

Do not introduce `invoke_without_command=True` unless a separate product decision explicitly requests bare `stackmind` to become the TUI.

---

# 3. UI Framework Decision

## Decision for this plan: Rich-first

The current project dependencies contain:

```text
click
pyyaml
jsonschema
rich
```

and do not contain Textual.

The existing `validators/kernel/tui/views.py` explicitly describes itself as dependency-free terminal renderers and is built around the project's existing Rich-oriented presentation philosophy.

Therefore this implementation plan does **not** assume Textual.

### Use the existing stack:

```text
Rich Console
Rich Live
Rich Layout
Rich Panel
Rich Text
Rich Markdown
Rich Syntax
```

where appropriate.

Do not add:

```text
textual
```

unless a later architecture decision explicitly chooses to move the TUI to Textual.

This keeps the implementation aligned with the current dependency set and avoids a framework migration during the visual redesign.

---

# 4. Architectural Boundary

The target architecture remains:

```text
                    USER
                     │
                     ▼
┌───────────────────────────────────────────────┐
│                 StackMind TUI                 │
│                                               │
│ Landing / Chat / Activity / Diff / Panels     │
└───────────────────────┬───────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────┐
│             StackMindTuiAdapter               │
│        existing command/action boundary       │
└───────────────────────┬───────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────┐
│                 DaemonClient                  │
│              JSON-RPC / events                │
└───────────────────────┬───────────────────────┘
                        │
                        ▼
┌───────────────────────────────────────────────┐
│              StackMind Daemon                 │
│                                               │
│ sessions / operations / plans / agents        │
│ roles / backends / contracts / execution      │
│ verification / persistence / event journal    │
└───────────────────────────────────────────────┘
```

The TUI is presentation and interaction logic.

The daemon remains responsible for runtime operations.

---

# 5. Existing TUI Components to Preserve

The current active `cli/tui/app.py` already contains significant delivery/control-plane behavior.

Do not throw this away.

The redesign should separate:

```text
runtime/state semantics
```

from:

```text
visual presentation
```

while preserving useful existing state handling.

Existing concepts include:

```text
ProjectPhase
PlanRevision
RoleStatus
WorkOrderItem
OperationNode
ActivityEntry
AutonomousDeliveryState
```

These should be reused or gradually refactored into smaller modules if needed.

The goal is **visual simplification**, not loss of existing StackMind functionality.

---

# 6. Existing TUI RPC Boundary

The existing package contains:

```text
validators/kernel/tui/
├── __init__.py
├── adapter.py
├── client.py
└── views.py
```

Use:

```text
DaemonClient
StackMindTuiAdapter
```

as the existing daemon boundary.

The adapter already routes commands such as:

```text
:new
:status
:resume
:pause
:cancel
:diff
:matrix
:events
:approve
:reject
:prompt
```

and normal non-colon input is treated as a prompt.

Do not duplicate this dispatch logic inside the new UI.

---

# 7. Command Prefix — IMPORTANT

StackMind commands use a **colon**, not a slash.

Correct:

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
:exit
:quit
```

Incorrect:

```text
/help
/status
/diff
```

A slash-prefixed input is not an existing StackMind command and can be treated as normal prompt text.

## Implementation rule

All documentation, UI hints, tests, and mockups must use:

```text
:
```

for StackMind commands.

Never introduce a slash-command vocabulary.

---

# 8. No New Slash-Command Autocomplete

This is an explicit project constraint.

Do not implement:

```text
new slash command autocomplete
new slash command registry
new command vocabulary
new command parser
```

The UI must use the existing command dispatch path.

If command hints are shown in the landing block, they are static/presentation hints for existing commands only.

---

# 9. Final Visual Direction

The TUI should combine the approved landing screen and chat screen.

The design is:

```text
┌──────────────────────────────────────────────────────────────┐
│ stackmind │ dev   ~/projects/stackmind    session │ agent ●  │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│                         ✦                                    │
│                                                              │
│                      StackMind                               │
│                        v3.3.0                                │
│                                                              │
│              Your AI development partner, with control.     │
│                                                              │
│                PLAN · BUILD · VERIFY · GOVERN                │
│                                                              │
│       / Start chatting       Ctrl+K Commands       :help     │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ You                                                          │
│ explain this code and suggest improvements                    │
│                                                              │
│ ✦ StackMind                                                   │
│ Here's a quick overview...                                   │
│                                                              │
│ ⚒ Read file   app/api/users.py                           ✓   │
│                                                              │
│ ✦ StackMind                                                   │
│ I'll suggest a small refactor...                             │
│                                                              │
│ ┌─ app/api/users.py ───────────────────────────────────────┐ │
│ │ - old code                                               │ │
│ │ + new code                                               │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ > Type a message...                              Ctrl+K       │
└──────────────────────────────────────────────────────────────┘
```

The `/` shown above is an **input/start-chat shortcut**, not a StackMind slash command. It must not be described as slash-command syntax.

---

# 10. Landing Block

Implement a dedicated landing renderer/widget within the existing Rich TUI architecture.

## Content

```text
                         ✦

                      StackMind
                        v3.3.0

             Your AI development partner, with control.

                 PLAN · BUILD · VERIFY · GOVERN

          / Start chatting       Ctrl+K Commands
          :help                  Show available commands
          :status                Show session status
```

## Version

The actual Python package version is:

```text
3.3.0
```

The Python package metadata and `cli.__version__` are the authoritative application version sources.

The static `VERSION` file currently contains an older value and must not be used as the authoritative TUI display source if it disagrees with the package version.

Display:

```text
v3.3.0
```

but derive it programmatically.

## Landing behavior

When there is no conversation:

```text
show landing
focus composer
```

After the first user prompt:

```text
hide landing
show conversation
```

The landing block must not remain permanently above the transcript.

---

# 11. Header

Keep the header extremely small.

Example:

```text
stackmind │ dev   ~/projects/stackmind

session: sm-8f3a2c │ agent: coder │ provider: openai │ ● online
```

Only show values actually available from the current runtime/session.

Do not invent:

```text
agent: coder
provider: openai
```

as defaults in the presentation layer.

The existing session information and delivery state should remain the source for available values.

---

# 12. Chat-First Layout

The central chat area gets almost all available terminal space.

Do not create a permanent:

```text
left dashboard
right telemetry panel
```

for the normal conversation.

StackMind-specific operational information appears contextually.

Primary hierarchy:

```text
conversation
    ↓
assistant result
    ↓
tool activity
    ↓
diff / plan / verification when relevant
```

---

# 13. Message Rendering

## User

Render:

```text
│ You
│ explain this code and suggest improvements
```

Use a subtle accent marker.

Avoid heavy cards around every message.

## Assistant

Render:

```text
✦ StackMind

Here's a quick overview...
```

Support the Rich capabilities already available in the project:

- Markdown;
- code;
- syntax highlighting;
- lists;
- headings;
- inline code;
- readable wrapping.

---

# 14. Streaming Semantics — IMPORTANT CORRECTION

The current runtime does **not** provide token-level LLM streaming.

The current backend invokes the model with non-streaming behavior.

The daemon does emit discrete lifecycle/operational events such as:

```text
operation.started
turn.started
event.toolCall
event.toolResult
operation.completed
```

The completed model response arrives as part of the completed tool/result event rather than as token chunks.

Therefore this implementation must **not claim token streaming**.

## Current target behavior

The UI should provide:

```text
live operational event updates
```

not:

```text
token-by-token assistant streaming
```

Example:

```text
● Thinking...
⚒ Read file app/api/users.py
✓ Read file app/api/users.py
✦ StackMind
<completed assistant response>
```

The UI can update live as daemon events arrive.

The assistant message itself should be rendered when the completed response is available.

---

# 15. Optional Future Token Streaming

True token streaming is explicitly out of scope for this TUI phase.

If desired later, it requires a separate runtime project:

```text
ModelExecutionBackend
        ↓
AgentRunner
        ↓
Daemon event protocol
        ↓
TUI event consumer
```

That future work must define:

- token/chunk event schema;
- ordering;
- cancellation;
- replay;
- backpressure;
- partial-response persistence;
- reconnect semantics.

Do not implement fake token streaming in the TUI.

---

# 16. Live Event Rendering

Use the daemon's existing event stream/retrieval mechanism.

The TUI should:

1. establish the session;
2. obtain the current event position;
3. consume new daemon events;
4. update in-memory presentation state;
5. render the affected section;
6. keep the composer responsive.

Because events are operational rather than token-level, event rendering should focus on state transitions.

---

# 17. Event State

Maintain transient TUI state such as:

```text
latest sequence
active operation
active tool
visible activity entries
current assistant response
current plan
current phase
```

Do not make this a replacement persistence system.

The daemon remains authoritative.

---

# 18. Chat History Restoration

Do **not** use `session.history` as a conversation transcript source.

In the current daemon implementation, `session.history` represents the durable session audit journal/lifecycle history rather than a full chat transcript.

Therefore the TUI must not assume:

```text
session.history == messages
```

## Current restoration strategy

When reconnecting or opening an existing session, reconstruct visible conversation information from the data that actually contains it.

Primary candidate:

```text
event.list
```

Filter/interpret relevant events such as:

```text
turn.started
event.toolResult
```

and other currently emitted event types that carry turn/result content.

Where the runtime's persisted outbox reports contain assistant reports, those may be used as a secondary recovery source if required by the existing runtime.

The implementation must inspect the actual event payload schema before finalizing the reconstruction adapter.

---

# 19. Operation Tree — IMPORTANT CORRECTION

Do not invent granular operations such as:

```text
Inspect repository
Implement changes
Run tests
Verify
```

unless the daemon actually creates those as operations.

The current runtime can represent child operations when they are explicitly created using operation parent relationships, particularly in multi-agent plan flows.

But an individual harness cycle may execute as a single operation.

Therefore the TUI operation tree must render **actual daemon operations**, not imagined task steps.

Example:

```text
Session
└── operation-123   RUNNING
    ├── operation-124   COMPLETED
    └── operation-125   RUNNING
```

Only display children that exist in daemon state/events.

---

# 20. Existing Autonomous Delivery View

The current `cli/tui/app.py` already has a delivery-oriented control-plane model.

Do not delete this functionality.

Instead, convert it from a permanently dense dashboard into contextual views.

Existing concepts to preserve:

```text
ProjectPhase
PlanRevision
RoleStatus
WorkOrderItem
OperationNode
ActivityEntry
```

Possible UI behavior:

```text
normal mode
    → chat-first

:plan
    → plan surface

:roles
    → roles surface

:wo
    → work-order surface

:tree
    → operation surface

:agents
    → agent surface

:completion
    → completion surface
```

Only use commands that actually exist in the current active command implementation. Do not invent command handlers merely because a view exists.

---

# 21. Tool Activity Widget

Use existing event semantics.

Example:

```text
┌──────────────────────────────────────────────────────────────┐
│ ⚒  Read file    app/api/users.py                         0.2s ✓│
└──────────────────────────────────────────────────────────────┘
```

Possible states:

```text
RUNNING
COMPLETED
FAILED
BLOCKED
CANCELLED
```

Use existing `activity_line()` semantics where appropriate.

The widget is a renderer.

It does not execute tools.

---

# 22. Diff Viewer

Keep diff presentation inline.

Use the existing `diff_viewer()` primitive where practical.

Example:

```text
┌─ app/api/users.py ────────────────────────────────────────────┐
│ @@ -1,6 +1,12 @@                                               │
│ - from fastapi import APIRouter                                │
│ + from fastapi import APIRouter, HTTPException                 │
│ + from pydantic import BaseModel                               │
│                                                               │
│ + class CreateUserRequest(BaseModel):                          │
│ +     name: str                                                │
│ +     email: str                                               │
└───────────────────────────────────────────────────────────────┘
```

Do not permanently occupy a separate diff pane.

---

# 23. Plan / HITL

Plan approval remains a first-class contextual surface.

Use the existing plan RPC path.

Render:

```text
┌─ PLAN READY ─────────────────────────────────────────────────┐
│ PLAN.md                                                       │
│                                                               │
│ Architecture                                                 │
│ Backend                                                      │
│ Frontend                                                     │
│ Q/A                                                          │
│ GitOps                                                       │
│                                                               │
│                 [Approve] [Reject] [Inspect]                 │
└───────────────────────────────────────────────────────────────┘
```

Approval/rejection must be routed through the existing adapter/client.

The TUI must not make a local authorization decision.

---

# 24. Contract Surface

The existing contract rendering helper:

```text
contract_panel()
```

should remain useful.

Present contract information contextually:

```text
CONTRACT
Write mode: governed
Allowed: ...
Denied: ...
```

Do not keep a large Contract dashboard permanently visible.

---

# 25. Verification Surface

Use the existing six-dimensional verification model:

```text
Scope
State
AST
Behavioral
Security
Outcome
```

Example:

```text
VERIFICATION

Scope       ✓ PASS
State       ✓ PASS
AST         ✓ PASS
Behavioral  ✓ PASS
Security    ✓ PASS
Outcome     ✓ PASS
```

Use the existing `verification_matrix()` semantics.

Do not fabricate PASS/FAIL values.

---

# 26. Roles / Agents

Use the existing runtime information.

The active TUI already models role/backend information through `RoleStatus`.

Display contextually:

```text
AGENT ROLES

● Architecture   running
● Backend        waiting
○ Frontend       waiting
○ Q/A            waiting
○ GitOps         waiting
```

Only show roles/states supported by actual runtime state.

Do not hard-code the sample statuses.

---

# 27. Work Orders

Preserve the existing `WorkOrderItem` concept.

Display:

```text
WORK ORDERS

WO-001   Architecture & Orchestration    RUNNING
WO-002   Backend API & Services           WAITING
WO-003   Frontend Client & Views          WAITING
```

Again, use actual runtime data.

Do not imply that every work order is an operation.

---

# 28. Composer

The bottom composer is persistent.

```text
┌──────────────────────────────────────────────────────────────┐
│ > Type a message...                              Ctrl+K       │
└──────────────────────────────────────────────────────────────┘
```

Requirements:

- input focus by default;
- prompt submission;
- history where already supported;
- multiline behavior if practical;
- interrupt/cancel behavior consistent with current TUI;
- no new command parser.

Normal text:

```text
user input
   ↓
existing adapter
   ↓
DaemonClient.turn()
```

Colon commands:

```text
:command
   ↓
existing adapter command()
```

---

# 29. Rich Rendering Loop

Because the implementation is Rich-first, structure the TUI around a controlled live rendering loop.

Suggested model:

```text
Application state
      │
      ▼
Rich render tree
      │
      ▼
Live display
```

Avoid:

```text
print()
print()
print()
```

for every event.

Use a single managed Rich live surface where possible.

The rendering function should be deterministic:

```text
state → renderable
```

This makes visual testing and debugging easier.

---

# 30. Suggested Rich UI Layers

A practical Rich composition:

```text
Root
├── Header
├── Main
│   ├── Landing OR Conversation
│   └── Contextual overlay/panel
└── Composer
```

Potential Rich primitives:

```text
Layout
Panel
Group
Text
Markdown
Syntax
Table
Live
Console
```

Do not introduce Textual widget classes unless the framework decision changes.

---

# 31. Proposed Refactor Structure

Because `cli/tui/app.py` is already large, split presentation code gradually rather than replacing the active TUI wholesale.

Suggested future structure:

```text
cli/tui/
├── __init__.py
├── app.py
├── state.py
├── render.py
├── input.py
├── landing.py
├── chat.py
├── activity.py
├── diff.py
├── plan.py
├── delivery.py
└── panels.py
```

This is a recommendation, not a requirement to perform a large file move in the first commit.

A safer implementation sequence is:

```text
existing app.py
      ↓
extract renderers
      ↓
extract input handling
      ↓
extract state
      ↓
retain app.py as orchestrator
```

---

# 32. Do Not Create a Second TUI Package Yet

Do not simultaneously create:

```text
validators/kernel/tui/ui/
```

and migrate the entire application unless the refactor requires it.

The active implementation already lives under:

```text
cli/tui/
```

while the daemon-facing primitives live under:

```text
validators/kernel/tui/
```

Keep this boundary understandable.

A large package migration should be a separate refactor if needed.

---

# 33. Version Handling

The package currently reports:

```text
3.3.0
```

from the Python package metadata/runtime.

The TUI should obtain the application version from the same authoritative Python package version used by the CLI.

Display:

```text
v3.3.0
```

in the landing block.

Do not hard-code the version.

Do not use an outdated static version file if it disagrees with the package.

---

# 34. Launch Behavior

The acceptance command is:

```bash
stackmind tui
```

Startup sequence:

```text
Click
  ↓
tui command
  ↓
existing daemon startup/connection behavior
  ↓
active TUI application
  ↓
landing screen
  ↓
composer focused
```

Do not change bare `stackmind` behavior in this phase.

---

# 35. Session Startup

Use the existing session lifecycle.

At startup:

```text
connect daemon
    ↓
health/version as currently supported
    ↓
create or resume session using existing implementation
    ↓
load relevant state
    ↓
start event consumption
    ↓
render landing/chat
    ↓
focus input
```

Do not create a new frontend session store.

---

# 36. Reconnect

When the daemon becomes unavailable:

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

Then:

```text
reconnect
   ↓
restore session
   ↓
recover events after last known sequence
   ↓
reconstruct visible state
   ↓
resume event consumption
```

Do not claim that `session.history` contains the chat transcript.

---

# 37. Event Replay

The existing adapter already tracks per-session event sequence positions.

Preserve this behavior.

The UI state layer should maintain:

```text
last_sequence[session_id]
```

or reuse the adapter's existing sequence tracking.

On replay:

```text
for each event after last_sequence:
    process event
    update UI state
```

Events must not be displayed twice.

---

# 38. Input Cancellation

The existing StackMind runtime has pause/resume/cancel behavior.

The UI should map keyboard interruption to existing runtime operations only where appropriate.

Do not invent a separate frontend cancellation protocol.

Example:

```text
Ctrl+C
   ↓
if active operation:
    existing cancel path
else:
    existing input/application behavior
```

The exact key behavior must be tested against the current terminal input implementation.

---

# 39. Contextual Views

The application should feel simple because these views are opened only when needed.

Core:

```text
Chat
```

Contextual:

```text
Status
Events
Diff
Matrix
Plan
Roles
Agents
Work Orders
Operation Tree
Completion
```

The implementation should use existing command handlers where available.

If a command/view does not currently exist in the active dispatch path, do not silently assume it exists.

---

# 40. Error Presentation

Errors should be concise.

Example:

```text
┌─ ERROR ───────────────────────────────────────────────────────┐
│ Operation failed                                              │
│                                                               │
│ Use :status or :events for details.                           │
└───────────────────────────────────────────────────────────────┘
```

Do not dump Python tracebacks into the normal conversation view.

Debug information should remain available through logging/debug paths.

---

# 41. Terminal Sizing

The design must degrade gracefully.

## Wide

Show:

```text
full header
large landing block
wide conversation
wide diff
```

## Medium

Reduce:

```text
workspace path
secondary metadata
decorative spacing
```

## Narrow

Keep:

```text
StackMind
session/status
conversation
composer
```

The landing block must not clip.

---

# 42. Theme

Default:

```text
dark
```

Visual language:

- near-black background;
- muted gray secondary text;
- bright primary text;
- restrained cyan/blue;
- purple StackMind accent;
- green success;
- red failure;
- thin borders;
- minimal containers;
- monospace typography.

Avoid turning the interface into a glowing dashboard.

---

# 43. Images

Image support is not required for the first implementation of the approved UI.

If image events already exist, render them only when the terminal/runtime supports a safe presentation path.

Do not create a new file-access mechanism.

This is lower priority than:

```text
landing
chat
activity
diff
composer
commands
session
reconnect
```

---

# 44. Testing Strategy

## 44.1 Existing tests

Run the full existing test suite before and after the UI refactor.

The TUI change must not regress daemon/runtime behavior.

## 44.2 Rendering tests

Test pure render functions for:

```text
landing
header
user message
assistant message
activity
diff
plan
contract
verification
roles
work orders
operation tree
errors
```

Prefer deterministic Rich renderables/text snapshots where practical.

## 44.3 Integration tests

Test:

```text
stackmind tui
```

startup and:

```text
prompt → adapter → daemon
```

flow.

## 44.4 Command regression

Explicitly test existing colon commands:

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
:exit
:quit
```

Do not test `/status` as a command.

It should not become a StackMind command.

---

# 45. Streaming/Event Tests

Because token streaming does not currently exist, test:

```text
operation.started
turn.started
event.toolCall
event.toolResult
operation.completed
```

according to the actual event schema present in the branch.

Verify that:

- events appear in order;
- tool activity updates;
- completed assistant content appears;
- operation completion updates the UI;
- failures/cancellation render correctly;
- duplicate events are not displayed twice.

---

# 46. Reconnect Tests

Simulate:

```text
event 1
event 2
disconnect
event 3
reconnect
```

Verify that:

```text
event 3
```

is recovered once.

Also verify that visible conversation state is reconstructed from actual event/result data rather than `session.history` alone.

---

# 47. Operation Tree Tests

Test against actual operation relationships.

Example:

```text
operation A
    parent = None

operation B
    parent = A

operation C
    parent = A
```

Expected:

```text
A
├── B
└── C
```

Do not test hypothetical child operations that the daemon never creates.

---

# 48. Version Tests

Verify that the landing block uses:

```text
package __version__
```

and displays:

```text
v3.3.0
```

for the current branch.

The test should not depend on a duplicated literal.

---

# 49. Performance

The current runtime does not provide token streaming, so performance requirements focus on event-driven updates.

Avoid:

```text
polling extremely frequently
full transcript reconstruction for every event
full terminal redraw for unrelated state changes
```

Prefer:

```text
daemon event
   ↓
state update
   ↓
controlled Rich Live refresh
```

---

# 50. Security

The TUI must not directly access:

```text
provider credentials
API keys
secret environment variables
```

The TUI must not:

```text
execute shell commands
write project files
invoke providers
execute tools
make authorization decisions
```

All privileged operations go through the existing daemon boundary.

---

# 51. Dependency Requirements

For the approved Rich-first implementation:

No new TUI framework dependency is required.

Continue using the project's existing:

```text
rich>=13.0
```

Do not add:

```text
textual
```

unless the implementation direction is explicitly changed later.

---

# 52. Implementation Phases

## Phase 0 — Baseline

Before modifying code:

```text
run existing tests
launch stackmind tui
inspect current app.py behavior
inspect actual event payloads
inspect current command dispatch
```

Document any existing failures separately.

---

## Phase 1 — Extract presentation boundaries

Refactor `cli/tui/app.py` enough to isolate:

```text
state
rendering
input
event handling
```

Do not change behavior unnecessarily.

---

## Phase 2 — Landing screen

Implement:

```text
StackMind
v3.3.0
tagline
PLAN · BUILD · VERIFY · GOVERN
existing command hints
```

Acceptance:

- landing appears on an empty session;
- composer is usable;
- correct version is displayed.

---

## Phase 3 — Chat-first layout

Make conversation the primary view.

Acceptance:

- landing disappears after first prompt;
- messages occupy the majority of terminal space;
- composer remains persistent.

---

## Phase 4 — Existing daemon integration

Connect the redesigned renderer to:

```text
StackMindTuiAdapter
DaemonClient
```

Acceptance:

- existing prompt flow still works;
- existing colon commands still work.

---

## Phase 5 — Live operational events

Implement Rich Live updates for actual daemon events.

Acceptance:

- operation state updates;
- tool calls appear;
- tool results appear;
- completion/failure/cancellation appear.

No fake token streaming.

---

## Phase 6 — Inline activity and diff

Implement:

```text
activity
diff
```

Acceptance:

- tool events are compact;
- diffs are readable;
- conversation remains dominant.

---

## Phase 7 — Contextual governance

Expose:

```text
plan
contract
verification
```

contextually.

Acceptance:

- plan approval works;
- plan rejection works;
- verification values come from actual runtime data.

---

## Phase 8 — Delivery/control-plane surfaces

Refactor existing delivery UI into contextual views:

```text
roles
work orders
agents
operations
completion
```

Do not remove their underlying runtime semantics.

---

## Phase 9 — Session/reconnect

Implement:

```text
event replay
conversation reconstruction
reconnect
```

using actual event/result sources.

Acceptance:

- no duplicate events;
- no silent loss after reconnect;
- session remains daemon-authoritative.

---

## Phase 10 — Polish

Implement:

- responsive sizing;
- spacing;
- keyboard behavior;
- error presentation;
- Rich Live performance;
- terminal compatibility.

---

## Phase 11 — Hardening

Run:

```text
full test suite
TUI tests
command regression
event replay tests
reconnect tests
operation tree tests
version tests
manual terminal checks
```

---

# 53. File-Level Work Plan

## Primary active TUI

```text
cli/tui/app.py
```

This is the main implementation target.

Refactor and extend it.

Do not implement the redesign exclusively in the old `cli/tui.py`.

## Existing daemon-facing TUI primitives

```text
validators/kernel/tui/client.py
validators/kernel/tui/adapter.py
validators/kernel/tui/views.py
```

Reuse them.

Only modify them when required by an actual missing capability.

## CLI entrypoint

```text
cli/main.py
```

Keep:

```text
stackmind tui
```

as the supported launch path.

Do not change bare `stackmind` behavior as part of this plan.

## Version

Use the existing Python package version source.

Current package version:

```text
3.3.0
```

---

# 54. Do Not Do These Things

The following are explicitly prohibited for this implementation:

### Do not add Textual

unless the framework decision is intentionally changed.

### Do not add token streaming

The daemon does not currently provide it.

### Do not modify the superseded TUI prototype

as the primary implementation target.

### Do not change commands from `:` to `/`

StackMind uses colon commands.

### Do not create slash-command autocomplete

Use the existing command system.

### Do not use `session.history` as chat transcript

It is an audit/lifecycle journal.

### Do not invent operation steps

Only render operations actually represented by the daemon.

### Do not make bare `stackmind` launch the TUI

Use:

```text
stackmind tui
```

### Do not use `3.2.0`

The current Python package version is:

```text
3.3.0
```

### Do not create a second runtime

The TUI remains a client.

### Do not create a second persistence layer

Daemon remains authoritative.

### Do not create a second event transport

Use existing daemon events.

---

# 55. Final User Experience

## Empty session

```text
stackmind │ dev   ~/projects/stackmind

                         ✦

                      StackMind
                        v3.3.0

             Your AI development partner, with control.

                 PLAN · BUILD · VERIFY · GOVERN

          / Start chatting       Ctrl+K Commands
          :help                  Show available commands
          :status                Show session status


> Type a message...
```

## Active conversation

```text
stackmind │ dev   ~/projects/stackmind     session │ agent │ ●

You
explain this code and suggest improvements

✦ StackMind

Here's a quick overview...

⚒ Read file    app/api/users.py                         ✓

✦ StackMind

I'll suggest a small refactor. Here's a diff:

┌─ app/api/users.py ─────────────────────────────────────────┐
│ - old code                                                  │
│ + new code                                                  │
└────────────────────────────────────────────────────────────┘

> Type a message...
```

## Contextual inspection

```text
:matrix
```

opens:

```text
VERIFICATION

Scope       ✓ PASS
State       ✓ PASS
AST         ✓ PASS
Behavioral  ✓ PASS
Security    ✓ PASS
Outcome     ✓ PASS
```

Then the user returns naturally to chat.

---

# 56. Definition of Done

The implementation is complete when:

- [ ] `stackmind tui` launches the redesigned TUI.
- [ ] Bare `stackmind` behavior is unchanged.
- [ ] The initial landing block displays `StackMind`.
- [ ] Landing displays the authoritative package version.
- [ ] Current version displays as `v3.3.0`.
- [ ] Landing disappears when conversation starts.
- [ ] Conversation is the dominant UI.
- [ ] Composer remains persistent.
- [ ] Existing colon commands continue to work.
- [ ] No slash-command autocomplete is introduced.
- [ ] User messages render cleanly.
- [ ] Assistant Markdown/code renders cleanly.
- [ ] Live daemon lifecycle events render.
- [ ] No token-streaming behavior is falsely represented.
- [ ] Tool calls/results render inline.
- [ ] Diffs render inline.
- [ ] Plan approval/rejection continues to work.
- [ ] Contract information is available contextually.
- [ ] Verification information uses actual runtime values.
- [ ] Roles/work orders/agents remain available contextually.
- [ ] Operation trees contain only actual daemon operations.
- [ ] `session.history` is not treated as the chat transcript.
- [ ] Chat recovery uses actual event/result sources.
- [ ] Reconnect replays missed events.
- [ ] Duplicate events are not rendered.
- [ ] No direct provider execution exists in the TUI.
- [ ] No direct tool execution exists in the TUI.
- [ ] No direct privileged filesystem execution exists in the TUI.
- [ ] No second persistence authority exists.
- [ ] No Textual dependency is required for the Rich-first implementation.
- [ ] Existing runtime tests remain passing.
- [ ] TUI rendering tests pass.
- [ ] Command regression tests pass.
- [ ] Reconnect/event replay tests pass.
- [ ] Version sourcing test passes.
- [ ] The UI remains usable across terminal widths.

---

# 57. Final Architecture

```text
                         USER
                           │
                           ▼
┌──────────────────────────────────────────────────────┐
│                    STACKMIND TUI                     │
│                                                      │
│  Landing → Chat → Activity → Diff → Contextual UI   │
│                           │                          │
│                        Composer                      │
│                                                      │
│                 Rich Console / Live                  │
└───────────────────────────┬──────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────┐
│               StackMindTuiAdapter                    │
│                                                      │
│       existing colon-command + action routing       │
└───────────────────────────┬──────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────┐
│                    DaemonClient                      │
│                 JSON-RPC / Events                   │
└───────────────────────────┬──────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────┐
│                  STACKMIND DAEMON                    │
│                                                      │
│ Sessions │ Operations │ Plans │ Agents │ Roles       │
│ Contracts │ Tools │ Providers │ Verification         │
│ Persistence │ Event Journal │ Cancellation           │
└──────────────────────────────────────────────────────┘
```

## Core principle

> **Make StackMind feel as simple as OpenCode without simplifying away StackMind's governed runtime.**

The user should see a clean coding conversation.

The daemon should continue to control execution, state, policy, operations, and verification.
