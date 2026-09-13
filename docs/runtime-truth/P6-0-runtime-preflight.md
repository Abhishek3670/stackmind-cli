# P6-0 Runtime Preflight: Operation Lifecycle and Cooperative Cancellation

Date: 2026-09-11  
Work order: WO-011  
Scope: read-only audit of the daemon runtime, harness runner, and existing daemon cancellation tests.

## Executive finding

The current runtime has an operation-shaped journal and an in-memory cancellation handle, but cancellation is owned by the *session* API. `session.cancel` both signals an active operation and immediately makes its parent session terminal. The harness execution path is not integrated with `SessionManager`, and `AgentRunner` never reads a cancellation handle. P6-1 therefore needs an operation-scoped lifecycle boundary: one operation is created for a runner invocation, cancellation requests target that operation, and the session remains reusable after the operation reaches `CANCELLED`.

## 1. Where an Operation is created

`SessionManager.begin_operation(session_id, operation_name, metadata=None)` in `validators/kernel/daemon/manager.py` is the sole creation point. It requires the session to be `RUNNING`, creates a UUID `operation_id`, creates a `threading.Event`, and records the identifier in both the in-memory active map and `session["active_operation"]`.

It appends this journal record:

```python
{
    "operation_id": operation_id,
    "operation": operation_name,
    "status": "STARTED",
    "started_at": <UTC ISO timestamp>,
}
```

It then publishes, in order, `operation.requested` (including `operation`, `operation_id`, and `metadata`), `operation.authorized`, and `operation.started`, saves state, and returns `(cancel_event, operation_id)`. The graph contains no production caller of `begin_operation`; the current direct caller is the daemon test. `JsonRpcProtocol` exposes no operation RPC methods, and `AgentRunner.run_once()` does not create or complete daemon operations.

## 2. Cancellation-handle ownership and safety

The handle is a `threading.Event`, stored only in `SessionManager._active: dict[str, Event]`, keyed by `operation_id`. The session holds only the corresponding string in `active_operation`; `_view()` deliberately hides that field from session API results. The event is process-local and is not persisted by `DaemonStorage`; after restart, only sessions and event history are restored.

`cancel_session()` finds `session["active_operation"]`, looks up the Event in `_active`, and invokes `set()`. `complete_operation()` treats an operation as cancelled when its handle is absent or set, writes `CANCELLED` in that case, and removes the map entry with `pop()`.

There is no lock around `_sessions`, `_active`, the journal, or `EventDispatcher`. `ThreadingHTTPServer` can dispatch concurrent requests, so these check/update sequences are not atomic. `Event.set()` is thread-safe, but ownership-map changes and the session/journal transitions are not protected. P6-1 should make `SessionManager` the sole owner of operation state under a manager lock, while passing only a read/check capability (or the Event) to the executing runner.

## 3. Why cancelling currently terminates the session

The RPC method `session.cancel` dispatches directly to `SessionManager.cancel_session()`. That method optionally signals the active Event and emits `operation.cancelled`, but then unconditionally returns:

```python
return self._set_state(session_id, "CANCELLED", "session.completed")
```

`_set_state()` marks `CANCELLED` terminal because `_TERMINAL` contains `COMPLETED`, `FAILED`, and `CANCELLED`. Consequently, `resume_session()` cannot revive it (it only accepts `PAUSED`), and `begin_operation()` rejects it because it requires `RUNNING`. This conflates a unit of work with the durable agent/session container.

P6-1 should introduce `cancel_operation(session_id, operation_id)` (or an equivalent operation-targeted RPC), set its state to `CANCEL_REQUESTED`, and leave the session `RUNNING` or return it to `WAITING` only after the operation terminates. Session cancellation should be reserved for an explicit end-of-session policy, with a separately named terminal event such as `session.cancelled`; it must not be the default effect of cancelling work.

## 4. The actual cancellation observer and the 10 required checkpoints

At present, no component observes the Event. `SessionManager` only sets it; `AgentRunner.run_once()` has no `SessionManager`, operation ID, Event, or cancellation predicate. A synchronous `llm_provider.complete(request)` also cannot be interrupted by the daemon as written.

P6-1 should have the daemon create the operation before invoking the runner, pass an operation-scoped cancellation predicate/token into `AgentRunner`, and require these ten checks in `run_once()`:

1. Immediately after task discovery, before creating expensive execution context.
2. Immediately after `assemble_context()`, before pre-execution contract verification.
3. Immediately after pre-execution verification, before the before-workspace snapshot.
4. Immediately after the before snapshot, before retrieval begins.
5. Immediately after retrieval, before `llm_provider.complete()`.
6. Immediately after provider completion returns, before payload/decision validation.
7. Immediately after decision validation, before post-execution contract verification.
8. Immediately after post-execution verification, before staged validation/copying.
9. Immediately after staged validation, before attempting the advisory write lock.
10. Immediately after acquiring the lock, before applying any writes; the command loop must also check before each command as an invariant of checkpoint 10.

At each checkpoint, a requested cancellation must stop further side effects, record the operation as cancelled, release a held lock in `finally`, and return a cancelled result rather than a completed/deferred result. To make an in-flight provider call cooperative rather than merely boundary-cancellable, the provider interface needs a cancellation token and provider implementations must poll it or support an abortable transport.

## 5. Terminal operation records and events

The durable terminal record is the matching `session["journal"]` item. `complete_operation()` changes its `status` from `STARTED` to `COMPLETED` or `CANCELLED`, adds `completed_at`, stores an optional `result`, removes the active handle, and clears `session["active_operation"]` when it matches.

Current events are inconsistent with that durable state:

| Action | Current event | Durable journal state |
| --- | --- | --- |
| Begin | `operation.requested`, `operation.authorized`, `operation.started` | `STARTED` |
| Cancel request | `operation.cancelled` | still `STARTED` until `complete_operation()` |
| Complete after no cancellation | `operation.completed` with `status=COMPLETED` | `COMPLETED` |
| Complete after cancellation | `operation.completed` with `status=CANCELLED` | `CANCELLED` |

Thus `operation.cancelled` currently means *request signalled*, not a terminal operation fact. P6-1 should emit `operation.cancel_requested` when the Event is set, make `complete_operation()` reject/ignore a normal completion when state is `CANCEL_REQUESTED`, and emit exactly one terminal event: `operation.cancelled` for `CANCELLED`, or `operation.completed` for `COMPLETED`. A terminal transition must atomically journal the final status, clear the active handle/session pointer, persist, and publish the matching terminal event. This prevents a late normal completion from overwriting or misreporting a cancellation request.

## Existing tests and required P6-1 coverage

`tests/test_daemon_runtime.py::test_active_operation_cancels_mid_turn_and_is_journaled` currently codifies the incorrect coupling: it calls `cancel_session()`, expects the Event to be set, completes the operation, and asserts that the session is `CANCELLED`. It proves no runner cooperation and no concurrent safety. The other daemon tests cover reconnect/event streaming and recovery, not operation cancellation.

P6-1 should replace that expectation and add coverage for: operation cancellation leaving a reusable session; `CANCEL_REQUESTED -> CANCELLED` with no terminal event before acknowledgement; prevention of a `CANCEL_REQUESTED -> COMPLETED` transition; cleanup of `_active` and `active_operation`; ordered request/terminal events; runner exit at each cooperative checkpoint; cancellation while waiting for/acquiring the write lock; cancellation before each command; and recovery semantics for an operation that was active when the daemon stopped.

## Refactoring blueprint for P6-1

1. Define explicit operation states (`STARTED`, `CANCEL_REQUESTED`, `COMPLETED`, `FAILED`, `CANCELLED`) and legal transitions in `SessionManager`.
2. Add a lock around operation/session lookup, transition, journal update, handle cleanup, persistence, and event publication ordering.
3. Add operation-targeted create/cancel/status protocol methods; retain or deprecate `session.cancel` only with explicit session-terminal semantics.
4. Bridge `LocalDaemon`/protocol operation ownership into `AgentRunner.run_once()` so each run receives its operation ID and cancellation token.
5. Implement the ten checkpoints above plus provider-level cooperative interruption.
6. Make cancellation acknowledgement own the sole `CANCELLED` terminal record/event, then update daemon and harness tests to enforce the transition rules.

No application source code was changed by this preflight audit.
