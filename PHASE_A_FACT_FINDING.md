# Phase A Fact-Finding Report: Governed Agent File I/O Integration

**Author**: Claude (Senior Architect)  
**Date**: 2026-09-26  
**Status**: Completed  
**Reference**: `pA_agent_IO_config.md` (§6, Round A0)  

---

## F1 — Production Entry Point Call Chain

The verified production call chain for agent execution is:

```text
cli/harness.py:run_once(agent, project_path, release_target)
    ↓
validators/harness/runner.py:AgentRunner(project_path, agent, llm_provider=EchoLLMProvider(...))
    ↓
AgentRunner.run_once(cancellation, operation_id)
    ↓
1. Loads TREE.yaml, checks protocol citizenship
2. discover_next_task() from inbox or active work order
3. load_harness_contract() resolves active Contract (.sync/contracts/<WO-ID>.yaml or .sync/agents/<agent>.contract.yaml)
4. KnowledgeAPI.assemble_context() gathers scoped context bundle
5. LLMRequest constructed (messages, schema, prompt)
6. LINE 397: completion = self.llm_provider.complete(request)
    ↓ (Disconnected here from ToolGateway)
7. self._parse_harness_decision(completion.content) → HarnessDecision
8. Staged workspace created in temp directory; decision.commands executed via ProcessSandbox
9. WorkspaceSnapshot.diff() calculates observed changes
10. Dimensions verified → _apply_verified_workspace_diff() commits to live workspace
```

### Key Finding
Line 397 in `validators/harness/runner.py` directly invokes `self.llm_provider.complete(request)`. The active runner does not construct or connect to `ProviderGateway` or `ToolGateway`, forcing the model to rely solely on declared `commands` (which are rightly blocked by interpreter denylists when attempting file writes).

---

## F2 — ProviderGateway & ToolGateway Integration

### ProviderGateway (`validators/kernel/providers/gateway.py`)
- **Constructor**:
  ```python
  ProviderGateway(
      adapter: ProviderAdapter,
      tool_gateway: ToolGateway,
      *,
      attempt: Attempt | None = None,
      contract: AgentContract | None = None,
      tools: Sequence[ToolDefinition] = STANDARD_KERNEL_TOOLS,
  )
  ```
- **Standard Tools**: Already defines `read_file`, `write_file`, `run_command`, `query_graph`.
- **Tool Dispatch**: `execute_tool_call(tool_call: ToolCallRequest) -> str` routes:
  - `read_file` → `tool_gateway.read_file(target)`
  - `write_file` → `tool_gateway.write_file(target, content)`
  - `run_command` → `tool_gateway.run_command(command)`
  - `query_graph` → `tool_gateway.query_graph(query)`
- **Loop Machinery**: `execute_turn()` and `run_loop()` handle multi-turn tool execution, message accumulation, and budget enforcement.

### ToolGateway (`validators/kernel/tools.py`)
- **Constructor**:
  ```python
  ToolGateway(
      workspace: ScratchWorkspace,
      boundary: RuntimeBoundary,
      contract: AgentContract,
      policy: AuthorizationPolicy,
      session_id: str,
      attempt_id: str,
      actor_id: str,
      provider_id: str,
      graph_query: Callable[[str], Any] | None = None,
      sandbox: ProcessSandbox | None = None,
  )
  ```
- **Guards**:
  - `read_file`: Checks `OperationType.READ_FILE` against contract/policy, verifies path is in scratch workspace, journals completion.
  - `write_file`: Checks `OperationType.WRITE_FILE` against contract/policy (fails closed if denied/read-only), ensures parent directories exist, writes to scratch workspace, journals completion.
  - `run_command`: Checks `OperationType.RUN_COMMAND`, enforces `check_command(command)` interpreter denylist, executes via `ProcessSandbox`.

---

## F3 — Contract & Identity Context

In `validators/harness/runner.py`:
- Contract is loaded via `load_harness_contract(project_path, agent, task.work_order_id)`.
- It yields an `AgentContract` (or can be normalized into one via `ContractNormalizer`).
- `AuthorizationPolicy` and `RuntimeBoundary` exist in `validators/kernel/`.
- Session ID: derived from task identifier or runner session counter (`session_id = f"harness-{self.agent}-{task.identifier}"`).
- Attempt ID: `f"attempt-{int(time.time())}"`.
- Actor ID: `self.agent` (e.g. `codex`).
- Provider ID: `self.backend_id`.

---

## F4 — Workspace Ownership & Isolation

- **`ScratchWorkspace` (`validators/kernel/workspace.py`)**:
  - `ScratchWorkspace.create(authoritative_root, attempt_id)` copies the live project to a disposable temporary folder (ignoring `.git`, `__pycache__`, `.venv`, `.sync`).
  - `path_for(relative_target)` strictly forbids absolute paths and `..` traversal, raising `WorkspaceEscapeError`.
- **Single Workspace Invariant**:
  - In Phase A, `ScratchWorkspace` must be established *before* the agent turn starts.
  - `ToolGateway` reads and writes exclusively inside this `ScratchWorkspace`.
  - After the turn completes, snapshots and diffs are taken from this exact `ScratchWorkspace`.
  - When verification passes, `_apply_verified_workspace_diff()` copies the verified diff from the scratch workspace to `project_path`.

---

## F5 — Output Semantics & Verification

- `WorkspaceSnapshot.capture(scratch.root)` captures initial and final state.
- `diff = before_snapshot.diff(after_snapshot)` produces `observed_task_files`.
- `decision.modified_files` is verified against `observed_task_files` (`declaration_matches = set(observed_task_files) == set(decision.modified_files)`).
- Verification dimensions (`scope`, `state`, `ast`, `behavioral`, `security`, `outcome`) evaluate the scratch workspace diff.
- Filesystem state remains strictly authoritative: model declarations cannot alter project files without matching mutations in the scratch workspace.

---

## Architectural Decision for Phase A

1. **Integration Seam**:
   In `AgentRunner.run_once()`, establish the `ScratchWorkspace` and construct `ToolGateway` + `ProviderGateway` around the active provider before running the turn.
2. **Provider Adapter**:
   Provide an adapter allowing `llm_provider` (or test providers) to execute through `ProviderGateway`. For providers emitting tool calls, `ProviderGateway.execute_turn` or `run_loop` dispatches to `ToolGateway`.
3. **No Shell Fallback**:
   The interpreter denylist in `validators/kernel/interpreter_denylist.py` remains completely intact.
4. **Harness Decision Continuity**:
   After tool calls have mutated the scratch workspace, the final model message provides `summary` and `modified_files` for the standard Harness decision pipeline.
