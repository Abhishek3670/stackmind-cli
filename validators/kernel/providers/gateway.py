"""Provider gateway mediating model interactions, tool execution, and contract budgets."""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Sequence
from typing import Any

from validators.kernel.contract import AgentContract
from validators.kernel.session import Attempt
from validators.kernel.tools import ToolGateway

from .adapter import ProviderAdapter
from .errors import (
    BudgetExceededError,
    ConsecutiveToolFailureError,
    NoProgressLoopError,
    OperationCancelledError,
    TimeoutError,
    ToolLimitExceededError,
    ToolLoopExhaustedError,
)
from .models import (
    Message,
    MessageRole,
    ProviderResponse,
    TokenUsage,
    ToolCallRequest,
    ToolDefinition,
)

STANDARD_KERNEL_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="read_file",
        description="Read the text content of a file located within the scratch workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside workspace"}
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="write_file",
        description="Write text content to a file located within the scratch workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside workspace"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            "required": ["path", "content"],
        },
    ),
    ToolDefinition(
        name="run_command",
        description="Execute a sandboxed shell command inside the scratch workspace.",
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command and argument list",
                }
            },
            "required": ["command"],
        },
    ),
    ToolDefinition(
        name="query_graph",
        description="Query the StackMind derived knowledge graph for symbols or context.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search or query string"}
            },
            "required": ["query"],
        },
    ),
)

DEFAULT_MAX_TURNS: int = 10
DEFAULT_MAX_TOOL_CALLS: int = 25
DEFAULT_MAX_TOOL_CALLS_PER_TURN: int = 10
DEFAULT_MAX_TOOL_OUTPUT_CHARS: int = 16_000
DEFAULT_MAX_CONSECUTIVE_IDENTICAL_CALLS: int = 3
DEFAULT_MAX_CONSECUTIVE_FAILURES: int = 3
DEFAULT_MAX_TOTAL_IDENTICAL_CALLS: int = 3


def truncate_tool_output(output: str, max_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS) -> str:
    """Safely truncate tool output if it exceeds max_chars."""
    if len(output) <= max_chars:
        return output
    head_len = max_chars // 2
    tail_len = max_chars // 2
    omitted = len(output) - (head_len + tail_len)
    return (
        f"{output[:head_len]}\n\n"
        f"[... TRUNCATED: tool output exceeded {max_chars} characters. "
        f"{omitted} characters omitted ...]\n\n"
        f"{output[-tail_len:]}"
    )


def check_cancellation(cancellation_token: Any | None) -> None:
    """Raise OperationCancelledError if cancellation has been requested."""
    if cancellation_token is None:
        return
    is_set = getattr(cancellation_token, "is_set", None)
    if callable(is_set) and is_set():
        raise OperationCancelledError("Operation cancelled via cancellation token")
    if bool(cancellation_token) and not callable(is_set):
        raise OperationCancelledError("Operation cancelled via cancellation token")


def detect_tool_cycle(signatures: list[tuple[str, str]]) -> str | None:
    """Detect repeating cycles of tool calls (e.g. A->B->A->B)."""
    for cycle_len in (2, 3):
        for mult in (3, 2):
            needed = cycle_len * mult
            if len(signatures) >= needed:
                chunk = signatures[-needed:]
                pattern = chunk[:cycle_len]
                if chunk == pattern * mult:
                    names = " -> ".join(p[0] for p in pattern)
                    return f"Pathological tool call cycle detected: ({names}) repeated {mult} times without progress"
    return None


class ProviderGateway:
    """Gateway orchestrating provider adapter, tool execution, and contract budgets."""

    def __init__(
        self,
        adapter: ProviderAdapter,
        tool_gateway: ToolGateway,
        *,
        attempt: Attempt | None = None,
        contract: AgentContract | None = None,
        tools: Sequence[ToolDefinition] = STANDARD_KERNEL_TOOLS,
        max_tool_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS,
    ) -> None:
        self.adapter = adapter
        self.tool_gateway = tool_gateway
        self.attempt = attempt
        self.contract = contract or (attempt.contract if attempt and hasattr(attempt, "contract") else None)
        active_tools = list(tools)
        if self.contract and hasattr(tool_gateway, "boundary") and hasattr(tool_gateway.boundary, "evaluator"):
            evaluator = tool_gateway.boundary.evaluator
            policy = getattr(tool_gateway, "policy", None)
            can_run_cmd = False
            if policy is None or policy.permits("run_command"):
                can_run_cmd, _ = evaluator.authorize(self.contract, "run_command", "workspace/command")
            if not can_run_cmd:
                active_tools = [t for t in active_tools if t.name != "run_command"]
        self.tools = tuple(active_tools)
        self.total_usage = TokenUsage()
        self.total_tool_calls: int = 0
        self.consecutive_failures: int = 0
        self.written_files: list[str] = []
        self.max_tool_output_chars: int = max_tool_output_chars

    def _get_max_tokens(self) -> int | None:
        if self.contract and hasattr(self.contract, "budget") and self.contract.budget:
            return self.contract.budget.get("max_tokens")
        return None

    def _get_budget_max_tool_calls(self) -> int | None:
        if self.contract and hasattr(self.contract, "budget") and self.contract.budget:
            return self.contract.budget.get("max_tool_calls")
        return None

    def _check_budget_before_call(self) -> None:
        max_tokens = self._get_max_tokens()
        if max_tokens is not None and self.total_usage.total_tokens >= max_tokens:
            raise BudgetExceededError(
                f"Contract token budget reached or exceeded: {self.total_usage.total_tokens} >= {max_tokens}",
                tokens_used=self.total_usage.total_tokens,
                max_tokens=max_tokens,
            )

    def _record_and_check_budget(self, usage: TokenUsage) -> None:
        self.total_usage = self.total_usage + usage
        if self.attempt is not None:
            self.attempt.add_usage(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
        max_tokens = self._get_max_tokens()
        if max_tokens is not None and self.total_usage.total_tokens > max_tokens:
            raise BudgetExceededError(
                f"Contract token budget exceeded: {self.total_usage.total_tokens} > {max_tokens}",
                tokens_used=self.total_usage.total_tokens,
                max_tokens=max_tokens,
            )

    def execute_tool_call(
        self,
        tool_call: ToolCallRequest,
        cancellation_token: Any | None = None,
        max_tool_calls: int | None = None,
    ) -> str:
        """Translate and execute a tool call against the ToolGateway with bounds and diagnostics."""
        check_cancellation(cancellation_token)

        budget_limit = self._get_budget_max_tool_calls()
        effective_limit = max_tool_calls or budget_limit or DEFAULT_MAX_TOOL_CALLS
        if self.total_tool_calls >= effective_limit:
            raise ToolLimitExceededError(
                f"Cumulative tool call limit ({effective_limit}) reached",
                tool_calls=self.total_tool_calls,
                max_tool_calls=effective_limit,
            )
        self.total_tool_calls += 1

        name = tool_call.name
        args = tool_call.arguments or {}

        try:
            if name == "read_file":
                target = args.get("path") or args.get("target") or args.get("filename") or ""
                if not target:
                    self.consecutive_failures += 1
                    res = "Error: 'read_file' missing required argument 'path'. Example: {\"path\": \"src/api/auth.py\"}"
                    if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                        raise ConsecutiveToolFailureError(
                            f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                            failures=self.consecutive_failures,
                        )
                    return res

                result = self.tool_gateway.read_file(target)
                self.consecutive_failures = 0
                return truncate_tool_output(result, max_chars=self.max_tool_output_chars)

            if name == "write_file":
                target = args.get("path") or args.get("target") or args.get("filename") or ""
                if not target:
                    self.consecutive_failures += 1
                    res = "Error: 'write_file' missing required argument 'path'. Example: {\"path\": \"src/api/auth.py\", \"content\": \"...\"}"
                    if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                        raise ConsecutiveToolFailureError(
                            f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                            failures=self.consecutive_failures,
                        )
                    return res

                if "content" not in args and "data" not in args and "text" not in args:
                    self.consecutive_failures += 1
                    res = "Error: 'write_file' missing required argument 'content'. Example: {\"path\": \"src/api/auth.py\", \"content\": \"...\"}"
                    if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                        raise ConsecutiveToolFailureError(
                            f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                            failures=self.consecutive_failures,
                        )
                    return res

                content = args.get("content", args.get("data", args.get("text", "")))
                self.tool_gateway.write_file(target, content)
                self.consecutive_failures = 0
                if target not in self.written_files:
                    self.written_files.append(target)
                return f"Successfully wrote {len(content)} characters to {target}"

            if name == "run_command":
                cmd = args.get("command") or args.get("cmd") or args.get("args")
                if not cmd:
                    self.consecutive_failures += 1
                    res = "Error: 'run_command' missing required argument 'command'. Example: {\"command\": [\"pytest\"]}"
                    if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                        raise ConsecutiveToolFailureError(
                            f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                            failures=self.consecutive_failures,
                        )
                    return res

                if isinstance(cmd, str):
                    cmd = shlex.split(cmd)
                elif not isinstance(cmd, (list, tuple)):
                    self.consecutive_failures += 1
                    return "Error: command must be a list of strings or string"

                result = self.tool_gateway.run_command(cmd)
                self.consecutive_failures = 0
                raw_res = (
                    f"Process finished with returncode {result.returncode}.\n"
                    f"STDOUT: {result.stdout}\n"
                    f"STDERR: {result.stderr}"
                )
                return truncate_tool_output(raw_res, max_chars=self.max_tool_output_chars)

            if name == "query_graph":
                query = args.get("query") or args.get("q") or ""
                if not query:
                    self.consecutive_failures += 1
                    res = "Error: 'query_graph' missing required argument 'query'. Example: {\"query\": \"auth\"}"
                    if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                        raise ConsecutiveToolFailureError(
                            f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                            failures=self.consecutive_failures,
                        )
                    return res

                result = self.tool_gateway.query_graph(query)
                self.consecutive_failures = 0
                out_str = json.dumps(result) if not isinstance(result, str) else result
                return truncate_tool_output(out_str, max_chars=self.max_tool_output_chars)

            self.consecutive_failures += 1
            res = f"Error: Unknown tool '{name}'. Available tools: read_file, write_file, run_command, query_graph."
            if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                raise ConsecutiveToolFailureError(
                    f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                    failures=self.consecutive_failures,
                )
            return res

        except (ToolLimitExceededError, ConsecutiveToolFailureError, OperationCancelledError):
            raise
        except Exception as ex:
            self.consecutive_failures += 1
            if isinstance(ex, FileNotFoundError):
                target_str = args.get("path") or args.get("target") or args.get("file") or ""
                existing_files: list[str] = []
                try:
                    ws_root = getattr(getattr(self.tool_gateway, "workspace", None), "root", None)
                    if ws_root and ws_root.is_dir():
                        for p in ws_root.rglob("*"):
                            if p.is_file() and not any(part in (".git", ".sync", "__pycache__", ".venv", "node_modules") for part in p.parts):
                                existing_files.append(p.relative_to(ws_root).as_posix())
                except Exception:
                    pass
                files_hint = f" Existing workspace files: {', '.join(sorted(existing_files)[:25])}." if existing_files else ""
                res = f"Error: FileNotFoundError: File '{target_str}' does not exist in workspace.{files_hint}"
            elif isinstance(ex, PermissionError):
                if name == "run_command":
                    cmd_val = args.get("command") or args.get("cmd") or args.get("args") or ""
                    cmd_str = " ".join(str(c) for c in cmd_val) if isinstance(cmd_val, list) else str(cmd_val)
                    res = (
                        f"Error: PermissionError: Command execution '{cmd_str}' was denied: "
                        f"command execution is not permitted for this work order's contract. "
                        f"Retrying will not help. Files should be finished with write_file and the final decision returned."
                    )
                else:
                    target_str = (
                        args.get("path")
                        or args.get("target")
                        or args.get("file")
                        or args.get("query")
                        or args.get("q")
                        or ""
                    )
                    res = f"Error: PermissionError: Operation on '{target_str}' was denied by runtime policy/contract: {ex}"
            else:
                res = f"Error executing {name}: {type(ex).__name__}: {ex}"

            if self.consecutive_failures == DEFAULT_MAX_CONSECUTIVE_FAILURES - 1:
                res += (
                    f"\nWarning: {self.consecutive_failures} consecutive tool failures; "
                    f"the next will end this run; change approach or finish now."
                )

            if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                raise ConsecutiveToolFailureError(
                    f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                    failures=self.consecutive_failures,
                )
            return res

    def execute_turn(
        self,
        messages: list[Message],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        max_tool_calls: int | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Execute one conversational turn, calling tools if requested."""
        check_cancellation(cancellation_token)
        self._check_budget_before_call()

        active_tools = tools if tools is not None else self.tools
        response = self.adapter.complete(
            messages,
            tools=active_tools,
            timeout=timeout,
            cancellation_token=cancellation_token,
            **kwargs,
        )
        self._record_and_check_budget(response.usage)

        messages.append(response.message)

        if response.message.tool_calls:
            if len(response.message.tool_calls) > DEFAULT_MAX_TOOL_CALLS_PER_TURN:
                raise ToolLimitExceededError(
                    f"Turn emitted {len(response.message.tool_calls)} tool calls, exceeding maximum allowed per turn ({DEFAULT_MAX_TOOL_CALLS_PER_TURN})",
                    tool_calls=len(response.message.tool_calls),
                    max_tool_calls=DEFAULT_MAX_TOOL_CALLS_PER_TURN,
                )
            for tc in response.message.tool_calls:
                check_cancellation(cancellation_token)
                tool_output = self.execute_tool_call(
                    tc,
                    cancellation_token=cancellation_token,
                    max_tool_calls=max_tool_calls,
                )
                messages.append(Message.tool(content=tool_output, tool_call_id=tc.id, name=tc.name))

        return response

    def run_loop(
        self,
        messages: list[Message],
        *,
        max_turns: int | None = None,
        max_tool_calls: int | None = None,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        """Execute full autonomous reasoning/tool loop until task completion or limit."""
        active_tools = tools if tools is not None else self.tools

        budget_turns = self.contract.budget.get("max_turns") if (self.contract and hasattr(self.contract, "budget") and self.contract.budget) else None
        effective_max_turns = max_turns or budget_turns or DEFAULT_MAX_TURNS

        budget_tool_calls = self._get_budget_max_tool_calls()
        effective_max_tool_calls = max_tool_calls or budget_tool_calls or DEFAULT_MAX_TOOL_CALLS

        deadline = time.monotonic() + timeout if timeout else None
        recent_signatures: list[tuple[str, str]] = []
        signature_counts: dict[tuple[str, str], int] = {}

        for turn_idx in range(effective_max_turns):
            check_cancellation(cancellation_token)

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Autonomous tool loop timed out after {timeout:.1f}s")
                turn_timeout = min(remaining, timeout)
            else:
                turn_timeout = None

            response = self.execute_turn(
                messages,
                tools=active_tools,
                timeout=turn_timeout,
                cancellation_token=cancellation_token,
                max_tool_calls=effective_max_tool_calls,
                **kwargs,
            )

            # If the assistant gave an answer without emitting new tool calls, the task finished
            if not response.message.tool_calls:
                return messages

            # Check cumulative tool call limit after executing turn
            if self.total_tool_calls >= effective_max_tool_calls:
                raise ToolLimitExceededError(
                    f"Cumulative tool call limit ({effective_max_tool_calls}) reached",
                    tool_calls=self.total_tool_calls,
                    max_tool_calls=effective_max_tool_calls,
                )

            # Record signatures for repetition/cycle detection
            for tc in response.message.tool_calls:
                sig = (tc.name, json.dumps(tc.arguments or {}, sort_keys=True))
                recent_signatures.append(sig)
                signature_counts[sig] = signature_counts.get(sig, 0) + 1

            # 1. Check ping-pong cycles
            cycle_err = detect_tool_cycle(recent_signatures)
            if cycle_err:
                raise NoProgressLoopError(cycle_err, pattern=cycle_err)

            # 2. Check consecutive identical tool calls
            if len(recent_signatures) >= DEFAULT_MAX_CONSECUTIVE_IDENTICAL_CALLS:
                last_sig = recent_signatures[-1]
                if all(s == last_sig for s in recent_signatures[-DEFAULT_MAX_CONSECUTIVE_IDENTICAL_CALLS:]):
                    raise NoProgressLoopError(
                        f"Pathological loop: identical call '{last_sig[0]}' repeated {DEFAULT_MAX_CONSECUTIVE_IDENTICAL_CALLS} times without progress",
                        pattern=last_sig[0],
                    )

            # 3. Check per-signature total occurrences across turn
            for sig, count in signature_counts.items():
                if count >= DEFAULT_MAX_TOTAL_IDENTICAL_CALLS:
                    raise NoProgressLoopError(
                        f"Pathological loop: call '{sig[0]}' with identical arguments repeated {count} times without progress",
                        pattern=sig[0],
                    )

        # If loop exited all turns while still emitting tool calls, it was exhausted
        raise ToolLoopExhaustedError(
            f"Autonomous tool loop reached maximum turns ({effective_max_turns}) without completing the task",
            turns=effective_max_turns,
            max_turns=effective_max_turns,
        )
