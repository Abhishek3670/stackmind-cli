# StackMind CLI — TUI Code Analysis & Improvement Checklist

Analysis of `stackmind-cli` (v3.3.0), focused on `cli/tui/*` and the daemon/adapter layer in `validators/kernel/{daemon,tui}`. Findings are evidence-based, with file references. Organized by severity: **Critical** (breaks or crashes the app), **Important** (real UX/architecture gaps), and **Polish**.

---

## What's already solid

- **Cross-platform raw input is genuinely well done.** `cli/tui/keyboard.py` implements real per-key raw-mode input for both POSIX (`termios`/`tty`) and Windows (`msvcrt`), with a clean fallback to `input()` for non-TTY/CI contexts. This is a part most homegrown TUIs get wrong or skip.
- **The daemon already has real Ollama auto-discovery.** `validators/harness/backend.py` has `discover_ollama_models()` and `pick_best_ollama_model()` that query `/api/tags` and prefer coding-specialized models automatically. This is more mature than most from-scratch integrations.
- **Real SSE infrastructure exists server-side.** `validators/kernel/daemon/server.py` implements a proper `/events` Server-Sent-Events endpoint with live pub/sub (`daemon.manager.events.subscribe`) and keepalives — this is genuine push infrastructure, not a stub.
- **Governance model is architecturally sound**: plan proposal → approve/reject → execution, with a verification matrix (scope/state/ast/behavioral/security/outcome) as a first-class concept in `governance.py`. That's more rigorous than most agent TUIs attempt.

---

## Critical

### 1. Ctrl+C during a running turn will likely crash the app, not cancel it
In `dispatch_delivery_command` (`cli/tui/app.py`), the turn-wait loop is:
```python
while time.time() - start_time < max_wait:
    events = list(adapter.stream(session["session_id"]))
    ...
    time.sleep(0.05)
```
This is wrapped only in `except Exception as err`. `KeyboardInterrupt` inherits from `BaseException`, not `Exception`, so it is **not caught here**. There is also no top-level `except KeyboardInterrupt` around the body of `tui()` — only `try/finally`. Result: pressing Ctrl+C mid-turn propagates out uncaught, likely printing a raw traceback to the user, and the `finally` block calls `daemon.stop()`, killing the *entire* local daemon (all sessions/agents), not just the one in-flight operation.
- **Fix:** catch `KeyboardInterrupt` specifically inside the wait loop and route it to `client.operation_cancel(op_id)` / the existing `:cancel` path, then return control to the prompt — don't tear down the daemon.

### 2. No token/context-window tracking anywhere
A search across `state.py` for token/context-window/compaction logic returns nothing. There is no analog to "6.2K / 8K tokens" in the UI, and no compaction strategy visible in `AutonomousDeliveryState`. For local Ollama models — which have far smaller context windows than cloud models and degrade badly when overflowed — this is a real gap, not just a nice-to-have. Long sessions with local models will silently degrade or error with no warning to the user.
- **Fix:** track approximate token usage per session (even a cheap char/4 heuristic is better than nothing), surface it in the header/status bar, and implement a compaction step before hitting the model's actual limit.

---

## Important

### 3. The TUI ignores its own SSE endpoint and busy-polls instead
This is the most interesting finding: the daemon (`server.py`) implements a real `/events` SSE stream, and the client even has a working consumer for it — `DaemonClient.stream_events()` in `validators/kernel/tui/client.py` opens the SSE connection and yields events as they arrive. **But `StackMindTuiAdapter.stream()` doesn't call `stream_events()` — it calls `client.events()`, which hits a JSON-RPC `event.list` endpoint and returns a snapshot list.** The interactive turn loop in `app.py` then wraps that in a manual `while ... time.sleep(0.05)` busy-loop, re-fetching the full event list every 50ms.

Consequences:
- Extra load on the daemon from constant re-polling instead of a single held connection.
- No actual push-based responsiveness — worst-case latency is capped by the 50ms poll interval, best case there's no benefit over polling at all.
- This is almost certainly *why* there's no token streaming (finding below) — the transport layer that would carry incremental deltas is built but unused.
- **Fix:** route the interactive wait loop through `client.stream_events()` instead of polling `client.events()`. This alone would unlock real push-based updates without new server work.

### 4. No incremental/token-level rendering
Once a response event appears, it's rendered once in full via an `assistant_rendered` flag that suppresses any further content for that turn:
```python
if resp and not assistant_rendered:
    state.add_message("assistant", resp, actions=turn_acts, thinking=thinking)
    click.echo(render_assistant_message_str(resp, actions=turn_acts, thinking=thinking))
    assistant_rendered = True
```
Text appears all-at-once rather than streaming. This is partly a consequence of #3 (no incremental transport in use) and partly the render path itself, which isn't structured to append partial text. Even if the daemon starts emitting partial-delta events, this code would still only render the *first* one it sees and then go silent for that turn.
- **Fix:** once on real SSE, change the render path to append incoming text deltas to the current message and re-render just that line, rather than gating on a single "have we rendered yet" flag.

### 5. Approval is plan-level only — no per-write/per-command gate visible
`governance.py`'s `:approve`/`:reject` operate on a *plan* before a turn executes. There's no code path in `app.py`'s turn-execution flow showing a second checkpoint before an individual file write or shell command runs once a plan is approved — execution appears to proceed to completion inside the daemon/backend once approved. That may be an intentional design (approve-the-strategy, not-each-step), but it's worth being explicit about, especially once Ollama models — which hallucinate tool calls more than cloud models — are driving execution.
- **Fix (or documentation fix):** either add a lightweight per-tool-call confirmation mode (configurable, since it trades safety for speed), or explicitly document that plan approval is the only gate so users calibrate their trust correctly.

### 6. Hardcoded 45-second synchronous wait, with a fully blocking loop
`client_timeout: float = 45.0` in `dispatch_delivery_command` is not exposed as a config/flag. During this window the loop blocks synchronously — no way to keep typing, no other input processed. For multi-step agent orchestration (Backend/Frontend/QA/Gitops work orders), 45 seconds is a plausible mid-task duration, not a worst case, and the fallback message ("No response within client wait time... Use :status or :events") pushes the burden back onto the user to babysit progress manually.
- **Fix:** make the timeout configurable, and — more importantly — render live progress from the events already being fetched each poll cycle (you have the data; you're already calling `render_operational_event_str` per event) so waiting doesn't look like hanging.

### 7. No terminal resize handling during a blocking wait
`shutil.get_terminal_size()` is only re-read at the top of each outer loop iteration (i.e., between prompts). During the up-to-45-second synchronous turn wait, a resized terminal won't be reflected until the turn completes and the next prompt cycle begins. No `SIGWINCH` handler exists anywhere in `cli/tui/*.py` (confirmed via grep).
- **Fix:** either poll terminal size within the wait loop (cheap, since you're already looping every 50ms) or install a `SIGWINCH` handler on POSIX that marks a "needs redraw" flag.

### 8. Full-screen redraw model, not diff-based rendering
`redraw_full_screen(...)` is invoked on most interactions (help, clear, after each dispatched command). This is simpler to reason about than incremental rendering, but for a control-plane-style UI with a busy runtime panel (agents, work orders, operation tree) updating frequently, full redraws will cause visible flicker on slower terminals/SSH sessions as the transcript grows. Not urgent today, but will become a bottleneck once true SSE streaming (finding #3/#4) is wired in and updates arrive more frequently.
- **Fix:** consider a `Rich.Live` region for the parts of the screen that update often (runtime panel, streaming message) while leaving the static header/landing block untouched.

---

## Polish

- **No visible model/quantization indicator per role.** The runtime panel shows agent role + status (waiting/orchestrating) but not which backend/model is bound to each — relevant since `:rebind`/`:configure` lets you mix Ollama + cloud backends per role. Surfacing "Backend: ollama/qwen2.5-coder:7b" next to each role would remove a `:roles` round-trip to check.
- **No tokens/sec or generation-speed indicator**, which matters more here than in a cloud-only tool given Ollama performance varies heavily by hardware and quantization.
- **Timeout/error messages are good but not actionable inline** — e.g., `REQUEST TIMEOUT` tells the user to run `:status` or `:events` rather than offering a keybinding/quick-action to do it immediately.
- **`:rebind`/`:configure` usage errors are string-based** (`"Usage: :rebind <role> <backend> [model]"`) rather than structured — fine for now, but if you ever add a command palette (Ctrl+K is already reserved for "commands" per the header), this parsing would need to be more declarative anyway.
- **No persisted session list/picker visible in `app.py`** — sessions get an ID (`session[\"session_id\"]`) but there's no evident `:sessions` implementation shown resuming a prior session by picking from a list (only referenced in a footer hint `/sessions` in the screenshot). Worth confirming this is wired end-to-end rather than aspirational.

---

## Priority order if you want to tackle this incrementally

1. Fix the Ctrl+C/`KeyboardInterrupt` handling (critical, low effort, prevents a bad crash-and-kill-daemon experience).
2. Swap the turn-wait loop to consume `client.stream_events()` instead of polling `client.events()` — unlocks real push-based updates using infrastructure you've already built.
3. Add incremental rendering on top of that stream so text appears progressively instead of all at once.
4. Add basic token/context tracking and surface it in the header — important specifically because you're targeting local models with small context windows.
5. Everything else (resize handling, live-region rendering, model/quant display) is meaningful polish once the above are solid.
