# P6 TUI architecture

```
OpenCode-compatible terminal components → StackMindTuiAdapter → DaemonClient
→ LocalDaemon /rpc → SessionManager → Contract/Policy/ToolGateway/Sandbox
```

`DaemonClient` uses only HTTP JSON-RPC. It has no filesystem, subprocess, provider, contract-evaluation, or eligibility APIs. `StackMindTuiAdapter` maps `:new`, `:resume`, `:pause`, and `:cancel` to lifecycle endpoints, polls sequenced `event.list` records for incremental activity, and submits—but never decides—HITL approval through `session.approval`.

The renderer receives runtime snapshots: session header, contract panel, activity events, verification dimensions, and runtime-supplied unified diff. A user action therefore follows `TUI → adapter → daemon → runtime`; no direct execution path exists. Streaming uses sequence checkpoints so reconnects replay missed events without a competing session database.
