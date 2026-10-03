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
        name="apply_patch",
        description="Apply a unified diff patch to modify an existing file.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Relative file path to patch"},
                "patch_content": {"type": "string", "description": "Unified diff patch text"},
            },
            "required": ["target", "patch_content"],
        },
    ),
    ToolDefinition(
        name="move_file",
        description="Move or rename a file within the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source path"},
                "destination": {"type": "string", "description": "Destination path"},
            },
            "required": ["source", "destination"],
        },
    ),
    ToolDefinition(
        name="delete_file",
        description="Delete a file or directory within the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Path to delete"}
            },
            "required": ["target"],
        },
    ),
    ToolDefinition(
        name="format_file",
        description="Format and normalize line endings of a file.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Path to format"}
            },
            "required": ["target"],
        },
    ),
    ToolDefinition(
        name="read_many_files",
        description="Read multiple files at once from the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "List of relative file paths"}
            },
            "required": ["paths"],
        },
    ),
    ToolDefinition(
        name="list_directory",
        description="List files and subdirectories within a directory.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path", "default": "."},
                "recursive": {"type": "boolean", "description": "Whether to list recursively", "default": False},
            },
        },
    ),
    ToolDefinition(
        name="glob",
        description="Find files matching a glob pattern.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"},
                "base_dir": {"type": "string", "description": "Base directory", "default": "."},
            },
            "required": ["pattern"],
        },
    ),
    ToolDefinition(
        name="grep",
        description="Search file contents using regex across the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex search pattern"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional list of files to search"},
            },
            "required": ["pattern"],
        },
    ),
    ToolDefinition(
        name="find_symbol",
        description="Locate function and class declarations by symbol name across python files.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to find"}
            },
            "required": ["name"],
        },
    ),
    ToolDefinition(
        name="find_references",
        description="Find all references to a symbol across codebase files.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to search for"}
            },
            "required": ["symbol"],
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
        name="process_start",
        description="Start a background process inside the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}, "description": "Command arguments"}
            },
            "required": ["command"],
        },
    ),
    ToolDefinition(
        name="process_status",
        description="Check status of a running background process.",
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process ID handle"}
            },
            "required": ["process_id"],
        },
    ),
    ToolDefinition(
        name="process_output",
        description="Read latest output lines from a background process.",
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process ID handle"},
                "tail_lines": {"type": "integer", "description": "Max lines to return", "default": 100},
            },
            "required": ["process_id"],
        },
    ),
    ToolDefinition(
        name="process_stop",
        description="Stop and terminate a running background process.",
        parameters={
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "Process ID handle"},
                "timeout": {"type": "number", "description": "Timeout in seconds", "default": 5.0},
            },
            "required": ["process_id"],
        },
    ),
    ToolDefinition(
        name="run_tests",
        description="Run test suite using pytest inside the scratch workspace.",
        parameters={
            "type": "object",
            "properties": {
                "test_path": {"type": "string", "description": "Optional test file or directory path"},
                "selector": {"type": "string", "description": "Optional pytest -k selector expression"},
            },
        },
    ),
    ToolDefinition(
        name="run_lint",
        description="Run syntax and lint checks on workspace python files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file or directory", "default": "."}
            },
        },
    ),
    ToolDefinition(
        name="run_typecheck",
        description="Run type checking verification on workspace files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file or directory", "default": "."}
            },
        },
    ),
    ToolDefinition(
        name="run_security_scan",
        description="Scan workspace code for security issues and hardcoded secrets.",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target file or directory", "default": "."}
            },
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
    ToolDefinition(
        name="find_callers",
        description="Find all functions and methods calling the specified symbol.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to look up callers for"}
            },
            "required": ["symbol"],
        },
    ),
    ToolDefinition(
        name="find_callees",
        description="Find all functions and methods called by the specified symbol.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to look up callees for"}
            },
            "required": ["symbol"],
        },
    ),
    ToolDefinition(
        name="impact_analysis",
        description="Perform graph-based impact analysis for modifying a symbol.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Target symbol to analyze"}
            },
            "required": ["symbol"],
        },
    ),
    ToolDefinition(
        name="dependency_analysis",
        description="Analyze module dependencies and incoming import relationships.",
        parameters={
            "type": "object",
            "properties": {
                "module": {"type": "string", "description": "Module name or path"}
            },
            "required": ["module"],
        },
    ),
    ToolDefinition(
        name="data_flow_analysis",
        description="Trace observed data flows from source symbol to sink.",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source variable or symbol"},
                "sink": {"type": "string", "description": "Optional sink target"},
            },
            "required": ["source"],
        },
    ),
    ToolDefinition(
        name="knowledge_stats",
        description="Get knowledge graph compilation status and node statistics.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="todo",
        description="Manage task todo list items for the active session (add/update/delete/clear).",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "update", "delete", "clear", "list"]},
                "task_text": {"type": "string", "description": "Task description"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "default": "pending"},
                "item_id": {"type": "integer", "description": "Item ID for update/delete"},
            },
            "required": ["action"],
        },
    ),
    ToolDefinition(
        name="ask_user",
        description="Prompt the human user for input or clarification.",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to ask the user"},
                "choices": {"type": "array", "items": {"type": "string"}, "description": "Optional list of choices"},
            },
            "required": ["question"],
        },
    ),
    ToolDefinition(
        name="enter_plan_mode",
        description="Enter architect planning mode (Claude exclusive).",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="exit_plan_mode",
        description="Exit architect planning mode (Claude exclusive).",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="create_work_order",
        description="Create and delegate a new work order to a worker agent (Claude exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Work order title"},
                "deliverable": {"type": "object", "description": "Deliverable specification"},
                "assigned_agent": {"type": "string", "description": "Assigned agent role"},
                "dependencies": {"type": "array", "items": {"type": "string"}, "description": "Optional prerequisite WO IDs"},
                "wo_id": {"type": "string", "description": "Optional explicit WO ID"},
            },
            "required": ["title", "deliverable", "assigned_agent"],
        },
    ),
    ToolDefinition(
        name="update_work_order",
        description="Update an existing work order (Claude exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
                "updates": {"type": "object", "description": "Fields to update"},
            },
            "required": ["wo_id", "updates"],
        },
    ),
    ToolDefinition(
        name="get_contract",
        description="Retrieve contract parameters, allowed scopes, and budgets.",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Optional work order ID"}
            },
        },
    ),
    ToolDefinition(
        name="verify_scope",
        description="Check whether a target path is within the allowed contract boundary.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path"},
                "operation": {"type": "string", "description": "Operation name", "default": "read_file"},
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="explain_denial",
        description="Explain why a target path is denied by the active contract boundary.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path"},
                "operation": {"type": "string", "description": "Operation name", "default": "read_file"},
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="inspect_budget",
        description="Inspect the active contract token, step, and file budgets.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="git_status",
        description="Inspect git working tree status (staged, unstaged, untracked).",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="git_diff",
        description="View git working tree diff.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional specific path to diff"}
            },
        },
    ),
    ToolDefinition(
        name="git_log",
        description="View recent git commits.",
        parameters={
            "type": "object",
            "properties": {
                "max_count": {"type": "integer", "description": "Maximum commits to show", "default": 10}
            },
        },
    ),
    ToolDefinition(
        name="git_show",
        description="View git commit or object details.",
        parameters={
            "type": "object",
            "properties": {
                "commit_or_path": {"type": "string", "description": "Commit hash or ref", "default": "HEAD"}
            },
        },
    ),
    ToolDefinition(
        name="git_blame",
        description="Show what revision and author last modified each line of a file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"}
            },
            "required": ["path"],
        },
    ),
    ToolDefinition(
        name="git_changed_files",
        description="List files changed between current HEAD and base ref.",
        parameters={
            "type": "object",
            "properties": {
                "base_branch": {"type": "string", "description": "Base ref or branch", "default": "HEAD"}
            },
        },
    ),
    ToolDefinition(
        name="git_branch",
        description="List git branches and check current branch.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="git_stage",
        description="Stage modified files for commit (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths to stage"}
            },
            "required": ["paths"],
        },
    ),
    ToolDefinition(
        name="git_restore",
        description="Restore working tree files from git.",
        parameters={
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths to restore"}
            },
            "required": ["paths"],
        },
    ),
    ToolDefinition(
        name="git_create_branch",
        description="Create and switch to a new git branch.",
        parameters={
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "New branch name"}
            },
            "required": ["branch_name"],
        },
    ),
    ToolDefinition(
        name="git_commit",
        description="Commit staged changes to git (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
                "author": {"type": "string", "description": "Optional author string"},
            },
            "required": ["message"],
        },
    ),
    ToolDefinition(
        name="git_tag",
        description="Create an annotated git tag (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "tag_name": {"type": "string", "description": "Tag name"},
                "message": {"type": "string", "description": "Tag annotation message"},
            },
            "required": ["tag_name"],
        },
    ),
    ToolDefinition(
        name="git_push",
        description="Push commits and tags to remote repository (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "remote": {"type": "string", "description": "Remote name", "default": "origin"},
                "branch": {"type": "string", "description": "Branch name to push"},
            },
        },
    ),
    ToolDefinition(
        name="create_release",
        description="Create a formal release with version bump, changelog update, and git tag (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Release tag"},
                "changelog": {"type": "string", "description": "Changelog entry markdown text"},
            },
            "required": ["tag", "changelog"],
        },
    ),
    ToolDefinition(
        name="rollback_release",
        description="Roll back a release tag (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Release tag to remove"}
            },
            "required": ["tag"],
        },
    ),
    ToolDefinition(
        name="verify_deliverable",
        description="Verify deliverables defined in a work order exist and are non-empty (Gemma exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"}
            },
            "required": ["wo_id"],
        },
    ),
    ToolDefinition(
        name="verify_tests",
        description="Verify and aggregate test suite results for QA verdict (Gemma exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "test_path": {"type": "string", "description": "Optional specific test path"}
            },
        },
    ),
    ToolDefinition(
        name="verify_diff",
        description="Verify that all changed files are within the assigned contract scope boundary (Gemma exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"}
            },
            "required": ["wo_id"],
        },
    ),
    ToolDefinition(
        name="verify_provenance",
        description="Verify operation journal evidence provenance for a work order (Gemma exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"}
            },
            "required": ["wo_id"],
        },
    ),
    ToolDefinition(
        name="submit_verdict",
        description="Submit a formal QA review verdict report (Gemma exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
                "verdict": {"type": "string", "enum": ["APPROVED", "NEEDS_CHANGES", "BLOCKED"]},
                "report": {"type": "string", "description": "Markdown QA review report"},
                "metrics": {"type": "object", "description": "Optional QA metrics"},
            },
            "required": ["wo_id", "verdict", "report"],
        },
    ),
    ToolDefinition(
        name="request_changes",
        description="Issue a NEEDS_CHANGES verdict with a list of required fixes (Gemma exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
                "issues": {"type": "array", "items": {"type": "string"}, "description": "List of issues to fix"},
            },
            "required": ["wo_id", "issues"],
        },
    ),
    ToolDefinition(
        name="approve_work_order",
        description="Issue an APPROVED QA verdict signed by the reviewer (Gemma exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
                "signature": {"type": "string", "description": "Optional reviewer signature"},
            },
            "required": ["wo_id"],
        },
    ),
    ToolDefinition(
        name="skill_test",
        description="Run 3-stage verification pipeline on a procedural skill candidate.",
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "Skill name"}
            },
            "required": ["skill_name"],
        },
    ),
    ToolDefinition(
        name="skill_audit",
        description="Audit active procedural skills for code or environment drift.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="validate_release_metadata",
        description="Validate release version and changelog hygiene across package files.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="checkpoint",
        description="Create a recoverable workspace snapshot.",
        parameters={
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Optional label"}
            },
        },
    ),
    ToolDefinition(
        name="restore_checkpoint",
        description="Restore workspace to a previously saved checkpoint.",
        parameters={
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "string", "description": "Checkpoint ID (e.g. cp-12345)"}
            },
            "required": ["checkpoint_id"],
        },
    ),
    ToolDefinition(
        name="list_checkpoints",
        description="List available workspace checkpoints.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="inspect_logs",
        description="Inspect kernel session operation journal logs.",
        parameters={
            "type": "object",
            "properties": {
                "filter_term": {"type": "string", "description": "Optional filter keyword"},
                "tail_lines": {"type": "integer", "description": "Max lines to return", "default": 100},
            },
        },
    ),
    ToolDefinition(
        name="inspect_processes",
        description="Inspect background processes managed in the workspace.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="collect_test_artifacts",
        description="Collect test reports, coverage data, and logs from workspace.",
        parameters={
            "type": "object",
            "properties": {
                "target_dir": {"type": "string", "description": "Target directory", "default": "artifacts"}
            },
        },
    ),
    ToolDefinition(
        name="system_metrics",
        description="Retrieve system environment and runtime diagnostic metrics.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="diagnostics_summary",
        description="Generate an aggregated diagnostics summary of the active session.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="get_context",
        description="Query knowledge graph context for a work order or question.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query or context focus"},
                "work_order_id": {"type": "string", "description": "Optional work order ID"},
            },
        },
    ),
    ToolDefinition(
        name="semantic_search",
        description="Search codebase using semantic matching.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Semantic query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="runtime_evidence",
        description="Retrieve operation journal audit evidence and provenance.",
        parameters={
            "type": "object",
            "properties": {
                "operation_id": {"type": "string", "description": "Optional specific operation ID"},
            },
        },
    ),
    ToolDefinition(
        name="verify_contract",
        description="Validate contract YAML against schema and scope rules.",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
            },
            "required": ["wo_id"],
        },
    ),
    ToolDefinition(
        name="submit_for_review",
        description="Submit completed work order to Gemma's inbox for QA review.",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
                "summary": {"type": "string", "description": "Summary of changes"},
            },
            "required": ["wo_id"],
        },
    ),
    ToolDefinition(
        name="inspect_work_order",
        description="Inspect details and status of an active or completed work order.",
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
            },
            "required": ["wo_id"],
        },
    ),
    ToolDefinition(
        name="inspect_agent",
        description="Inspect agent capabilities, policies, and active assignments.",
        parameters={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Agent name"},
            },
            "required": ["agent_name"],
        },
    ),
    ToolDefinition(
        name="dispatch_subagent",
        description="Dispatch an asynchronous assignment notice to another agent's inbox.",
        parameters={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Target agent name"},
                "wo_id": {"type": "string", "description": "Work order ID"},
                "instructions": {"type": "string", "description": "Instructions"},
            },
            "required": ["agent_name", "wo_id"],
        },
    ),
    ToolDefinition(
        name="web_search",
        description="Perform a web search for documentation and API references.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="web_fetch",
        description="Fetch static content from a documentation URL.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            },
            "required": ["url"],
        },
    ),
    ToolDefinition(
        name="search_docs",
        description="Search documentation and markdown specification files.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term"},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="browser_open",
        description="Open browser page for frontend UI inspection (Gemini).",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open"},
            },
            "required": ["url"],
        },
    ),
    ToolDefinition(
        name="browser_navigate",
        description="Navigate open browser session to URL.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
            },
            "required": ["url"],
        },
    ),
    ToolDefinition(
        name="browser_click",
        description="Click element by CSS selector in browser session.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector to click"},
            },
            "required": ["selector"],
        },
    ),
    ToolDefinition(
        name="browser_type",
        description="Type text into element by CSS selector in browser session.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["selector", "text"],
        },
    ),
    ToolDefinition(
        name="browser_screenshot",
        description="Capture screenshot of browser viewport (Gemini).",
        parameters={
            "type": "object",
            "properties": {
                "output_path": {"type": "string", "description": "Destination image path", "default": "screenshot.png"},
            },
        },
    ),
    ToolDefinition(
        name="inspect_screenshot",
        description="Inspect captured screenshot image metadata.",
        parameters={
            "type": "object",
            "properties": {
                "screenshot_path": {"type": "string", "description": "Path to screenshot image"},
            },
            "required": ["screenshot_path"],
        },
    ),
    ToolDefinition(
        name="inspect_environment",
        description="Inspect Python runtime, environment variables, and system tools.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="compare_snapshots",
        description="Compare workspace state against reference snapshot.",
        parameters={
            "type": "object",
            "properties": {
                "ref_snapshot": {"type": "string", "description": "Reference snapshot identifier"},
            },
        },
    ),
    ToolDefinition(
        name="experience_search",
        description="Search past verified execution experience logs.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="skill_retrieve",
        description="Retrieve verified procedural skills relevant to a query.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Task or problem description"},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="skill_list",
        description="List active or candidate procedural skills.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Skill status (active, candidate, stale)", "default": "active"},
            },
        },
    ),
    ToolDefinition(
        name="skill_mine",
        description="Mine recurring execution clusters for new candidate procedural skills (Claude).",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="skill_promote",
        description="Promote a verified procedural skill candidate to active status (Claude).",
        parameters={
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "Skill name to promote"},
                "allow_medium": {"type": "boolean", "description": "Allow promoting medium risk skills", "default": True},
            },
            "required": ["skill_name"],
        },
    ),
    ToolDefinition(
        name="inspect_version",
        description="Inspect canonical version declarations across project metadata.",
        parameters={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="update_version",
        description="Update version declarations across project files (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "new_version": {"type": "string", "description": "Semantic version string"},
            },
            "required": ["new_version"],
        },
    ),
    ToolDefinition(
        name="update_changelog",
        description="Update CHANGELOG.md with release notes (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "Markdown changelog entry"},
            },
            "required": ["entry"],
        },
    ),
    ToolDefinition(
        name="generate_release_notes",
        description="Generate markdown release notes from git commits since previous tag.",
        parameters={
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Release tag"},
            },
            "required": ["tag"],
        },
    ),
    ToolDefinition(
        name="prepare_release",
        description="Prepare release bump, update metadata, and update changelog (Local-LLM exclusive).",
        parameters={
            "type": "object",
            "properties": {
                "version": {"type": "string", "description": "Release version string"},
            },
            "required": ["version"],
        },
    ),
    ToolDefinition(
        name="request_tools",
        description=(
            "Dynamically activate additional specialized tools for this session by keyword, "
            "category, or tool name (e.g. 'git', 'analysis', 'linters', 'graph', or a specific name like 'git_blame')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Category or tool name, e.g. 'git', 'analysis', 'knowledge', or 'git_blame'",
                }
            },
            "required": ["query"],
        },
    ),
)


def get_tools_for_role(role_or_agent: str) -> list[ToolDefinition]:
    """Filter STANDARD_KERNEL_TOOLS for the given role according to its AuthorizationPolicy."""
    from validators.kernel.identity import get_role_policy
    policy = get_role_policy(role_or_agent)
    return [t for t in STANDARD_KERNEL_TOOLS if policy.permits(t.name)]


PLANNING_CORE_TOOL_NAMES = ("query_graph", "list_directory", "read_file", "write_file")
AUTHORING_CORE_TOOL_NAMES = ("read_file", "write_file", "list_directory")
ARCHITECTURE_INVESTIGATION_TOOL_NAMES = (
    "read_file",
    "write_file",
    "list_directory",
    "query_graph",
    "find_symbol",
    "find_references",
    "git_log",
    "git_diff",
    "run_command",
)


def get_curated_tools_for_phase(
    role_or_agent: str,
    phase: str | None = None,
    backend_id: str | None = None,
) -> list[ToolDefinition]:
    """Return an optimized, task-appropriate tool palette.

    Prevents context bloat and tool dilution on local models while including
    the request_tools meta-tool for dynamic on-demand capability expansion.
    """
    from validators.kernel.identity import get_role_policy
    policy = get_role_policy(role_or_agent)
    all_permitted = [t for t in STANDARD_KERNEL_TOOLS if policy.permits(t.name)]

    norm_role = str(role_or_agent).lower().strip()
    is_architect = norm_role in ("claude", "architecture", "architect")

    request_tools_def = next((t for t in STANDARD_KERNEL_TOOLS if t.name == "request_tools"), None)

    if is_architect:
        if phase == "planning":
            wanted = set(PLANNING_CORE_TOOL_NAMES)
        elif phase == "authoring":
            wanted = set(AUTHORING_CORE_TOOL_NAMES)
        else:
            wanted = set(ARCHITECTURE_INVESTIGATION_TOOL_NAMES)

        curated = [t for t in all_permitted if t.name in wanted]
        if request_tools_def and request_tools_def not in curated:
            curated.append(request_tools_def)
        return curated

    # For other roles (codex, gemini, gemma, local-llm):
    # If running against local Ollama, trim extraneous kernel diagnostic tools
    if backend_id in ("ollama", "local", "local-llm"):
        omitted = {
            "checkpoint", "restore_checkpoint", "inspect_budget", "explain_denial",
            "verify_scope", "dispatch_subagent", "compare_snapshots", "skill_mine",
            "skill_promote", "skill_list", "skill_retrieve", "experience_search",
        }
        curated = [t for t in all_permitted if t.name not in omitted]
        if request_tools_def and request_tools_def not in curated:
            curated.append(request_tools_def)
        return curated

    if request_tools_def and request_tools_def not in all_permitted:
        all_permitted.append(request_tools_def)
    return all_permitted

DEFAULT_MAX_TURNS: int = 20
DEFAULT_MAX_TOOL_CALLS: int = 35
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
        request_tools_def = next((t for t in STANDARD_KERNEL_TOOLS if t.name == "request_tools"), None)
        if request_tools_def and request_tools_def.name not in {t.name for t in active_tools}:
            active_tools.append(request_tools_def)
        self.active_tools_list: list[ToolDefinition] = active_tools
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
        tool_call: ToolCallRequest | str,
        cancellation_token: Any | None = None,
        max_tool_calls: int | None = None,
    ) -> str:
        """Translate and execute a tool call against the ToolGateway with bounds and diagnostics."""
        if isinstance(tool_call, str):
            call_args = cancellation_token if isinstance(cancellation_token, dict) else {}
            token_obj = max_tool_calls if not isinstance(max_tool_calls, int) else None
            effective_max = max_tool_calls if isinstance(max_tool_calls, int) else None
            tool_call = ToolCallRequest(id="call-direct", name=tool_call, arguments=call_args)
            cancellation_token = token_obj
            max_tool_calls = effective_max

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

            if name == "request_tools":
                query = str(args.get("query", "")).lower().strip()
                if not query:
                    return "Error: 'request_tools' requires a 'query' argument specifying the tool or category."

                category_tools: dict[str, set[str]] = {
                    "git": {"git_status", "git_diff", "git_log", "git_show", "git_blame", "git_changed_files", "git_branch"},
                    "analysis": {"find_callers", "find_callees", "impact_analysis", "dependency_analysis", "data_flow_analysis"},
                    "knowledge": {"query_graph", "find_symbol", "find_references", "knowledge_stats", "semantic_search"},
                    "graph": {"query_graph", "find_symbol", "find_references", "knowledge_stats", "semantic_search"},
                    "linter": {"run_command"},
                    "linters": {"run_command"},
                    "diagnostics": {"inspect_environment", "inspect_logs", "inspect_version", "checkpoint"},
                    "search": {"grep", "glob", "semantic_search", "search_docs"},
                }

                target_names = set(category_tools.get(query, set()))
                if not target_names:
                    for t in STANDARD_KERNEL_TOOLS:
                        if query == t.name.lower() or query in t.name.lower() or query in t.description.lower():
                            target_names.add(t.name)

                policy = getattr(self.tool_gateway, "policy", None)
                added_tools: list[str] = []
                already_active = {t.name for t in self.active_tools_list}
                for t in STANDARD_KERNEL_TOOLS:
                    if t.name in target_names and t.name not in already_active:
                        if policy is None or policy.permits(t.name):
                            self.active_tools_list.append(t)
                            added_tools.append(t.name)

                self.tools = tuple(self.active_tools_list)
                self.consecutive_failures = 0
                if added_tools:
                    return f"Successfully activated tool(s): {added_tools}. Their schemas are now available for you to call in your next action."
                elif any(t in already_active for t in target_names):
                    return f"Tool(s) matching '{query}' are already active in your session."
                else:
                    return f"No permitted tools found matching '{query}'. Available categories: git, analysis, knowledge, search, linters."

            if hasattr(self.tool_gateway, name):
                method = getattr(self.tool_gateway, name)
                import inspect
                sig = inspect.signature(method)
                kwargs = {}
                for param_name, param in sig.parameters.items():
                    if param_name in args:
                        kwargs[param_name] = args[param_name]
                    elif param_name == "target" and ("path" in args or "file" in args):
                        kwargs[param_name] = args.get("path") or args.get("file")
                    elif param_name == "path" and ("target" in args or "file" in args):
                        kwargs[param_name] = args.get("target") or args.get("file")
                    elif param_name == "filter_term" and ("filter" in args or "q" in args):
                        kwargs[param_name] = args.get("filter") or args.get("q")
                    elif param_name == "task_text" and ("text" in args or "task" in args):
                        kwargs[param_name] = args.get("text") or args.get("task")
                    elif param_name == "checkpoint_id" and ("id" in args or "cp_id" in args):
                        kwargs[param_name] = args.get("id") or args.get("cp_id")
                    elif param_name == "patch_content" and ("patch" in args or "diff" in args):
                        kwargs[param_name] = args.get("patch") or args.get("diff")

                result = method(**kwargs)
                self.consecutive_failures = 0
                if result is None:
                    out_str = f"Successfully executed {name}"
                elif isinstance(result, str):
                    out_str = result
                else:
                    out_str = json.dumps(result, indent=2)
                return truncate_tool_output(out_str, max_chars=self.max_tool_output_chars)

            self.consecutive_failures += 1
            res = f"Error: Unknown tool '{name}'."
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
        if tools is not None:
            self.active_tools_list = list(tools)
            request_tools_def = next((t for t in STANDARD_KERNEL_TOOLS if t.name == "request_tools"), None)
            if request_tools_def and request_tools_def.name not in {t.name for t in self.active_tools_list}:
                self.active_tools_list.append(request_tools_def)
        active_tools = self.active_tools_list

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
