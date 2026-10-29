"""Provider gateway mediating model interactions, tool execution, and contract budgets."""

from __future__ import annotations

import json
import re
import os
from pathlib import Path
from queue import Empty, Queue
import shlex
import time
from collections.abc import Callable, Iterator, Sequence
from threading import Thread
from typing import Any
from uuid import uuid4

from validators.kernel.contract import AgentContract
from validators.kernel.session import Attempt
from validators.kernel.tools import ToolGateway

from .adapter import ProviderAdapter
from .errors import (
    BudgetExceededError,
    ConsecutiveToolFailureError,
    NoProgressLoopError,
    OperationCancelledError,
    ProviderError,
    ProviderStreamInterruptedError,
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
        description=(
            "Amend a live ACTIVE work order (Claude exclusive) under optimistic "
            "concurrency. `expected_revision` MUST be the revision you last read; "
            "a stale value is rejected with REVISION_CONFLICT and nothing is "
            "written. The runtime sets the new revision (current + 1) and records "
            "the reason in `amendments`, which the assigned worker receives as a "
            "scoped correction on its next turn."
        ),
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Work order ID"},
                "updates": {"type": "object", "description": "Fields to update"},
                "reason": {
                    "type": "string",
                    "description": "Why the task is being amended; shown to the worker.",
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Revision currently persisted for this work order. "
                        "Required: updates are rejected when it does not match."
                    ),
                },
            },
            "required": ["wo_id", "updates", "expected_revision"],
        },
    ),
    ToolDefinition(
        name="reopen_work_order",
        description=(
            "Reopen a COMPLETED work order through an explicit lifecycle transition "
            "(Claude/CEO only). The prior completion record is retained for audit in "
            "completion_history and the durable audit log, the revision increments, "
            "and prior verification evidence is marked stale for the new revision. "
            "`expected_revision` MUST be the revision you last read."
        ),
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Completed work order ID"},
                "reason": {
                    "type": "string",
                    "description": "Why the completed task is being reopened.",
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Revision currently persisted for this work order.",
                },
            },
            "required": ["wo_id", "reason", "expected_revision"],
        },
    ),
    ToolDefinition(
        name="revalidate_work_order",
        description=(
            "Clear stale evidence on a COMPLETED work order after it was invalidated "
            "by an upstream amendment (Claude/CEO only). The runtime re-verifies the "
            "declared deliverables and re-executes the work order's declared "
            "test_plan itself; self-reported evidence is not accepted. It does NOT "
            "change status or history. It is refused when the invalidation requires "
            "rework (reopen instead), when an upstream advanced past the revision "
            "that caused the invalidation, or when no runnable verification is "
            "declared. `expected_revision` MUST be the revision you last read."
        ),
        parameters={
            "type": "object",
            "properties": {
                "wo_id": {"type": "string", "description": "Completed work order ID"},
                "expected_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Revision currently persisted for this work order.",
                },
                "verification": {
                    "type": "object",
                    "description": (
                        "Optional audit notes only ({'notes': <string>}). The runtime "
                        "derives the checks from the work order and executes them."
                    ),
                },
            },
            "required": ["wo_id", "expected_revision"],
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
        name="trigger_goal",
        description=(
            "Trigger an autonomous multi-agent development run across the workspace. "
            "Use this when the operator requests concrete feature implementation, structural code changes, "
            "or multi-file projects requiring planning, authoring work orders, and QA verification."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Comprehensive, actionable goal description for the multi-agent team.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this run is being triggered and architectural guidance.",
                },
            },
            "required": ["goal"],
        },
    ),
    ToolDefinition(
        name="delegate_task",
        description=(
            "Delegate a focused, single-component implementation, test, or release task directly to a specialized worker agent "
            "('frontend' / 'gemini' for UI/HTML/CSS/JS, 'backend' / 'codex' for server/APIs/logic, 'qa' / 'gemma' for test authoring/verification, 'gitops' / 'local-llm' for git/versioning/release). "
            "The delegated agent executes under its own scoped permissions to safely mutate source files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_agent": {
                    "type": "string",
                    "description": "Specialist agent or role to execute the task: 'frontend' (or 'gemini'), 'backend' (or 'codex'), 'qa' (or 'gemma'), 'gitops' (or 'local-llm').",
                },
                "instructions": {
                    "type": "string",
                    "description": "Precise implementation instructions for the worker agent.",
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of workspace files the agent is instructed to modify (e.g. ['index.html', 'assets/js/main.js']).",
                },
            },
            "required": ["target_agent", "instructions"],
        },
    ),
    ToolDefinition(
        name="web_search",
        description="Perform a web search for documentation, media assets, and API references. Use media_type='images' to search for image assets.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
                "media_type": {"type": "string", "enum": ["all", "images"], "description": "Search media type ('all' or 'images')", "default": "all"},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="image_search",
        description="Search for real, accessible web image assets, photos, logos, and graphics with direct image URLs.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Image search query (e.g. 'crochet handmade bag')"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name="web_fetch",
        description="Fetch static content and media assets from a documentation or web URL.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "extract_images": {"type": "boolean", "description": "Whether to extract discovered image URLs from the page", "default": True},
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


PLANNING_CORE_TOOL_NAMES = ("query_graph", "list_directory", "read_file", "write_file", "web_search", "image_search", "web_fetch")
AUTHORING_CORE_TOOL_NAMES = ("read_file", "write_file", "list_directory")
ARCHITECTURE_INVESTIGATION_TOOL_NAMES = (
    "read_file",
    "write_file",
    "apply_patch",
    "list_directory",
    "query_graph",
    "find_symbol",
    "find_references",
    "web_search",
    "image_search",
    "web_fetch",
    "git_log",
    "git_diff",
    "run_command",
    "run_tests",
    "run_lint",
    "verify_deliverable",
    "trigger_goal",
    "delegate_task",
    "update_work_order",
    "reopen_work_order",
    "revalidate_work_order",
)


WORKER_CODE_CORE_TOOL_NAMES = (
    "read_file",
    "write_file",
    "apply_patch",
    "list_directory",
    "glob",
    "grep",
    "run_command",
)
WORKER_QA_CORE_TOOL_NAMES = (
    "read_file",
    "write_file",
    "list_directory",
    "glob",
    "run_tests",
    "run_lint",
    "run_security_scan",
    "verify_deliverable",
)
WORKER_GITOPS_CORE_TOOL_NAMES = (
    "read_file",
    "write_file",
    "list_directory",
    "git_status",
    "git_diff",
    "git_log",
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

    # For worker roles (codex, gemini, gemma, local-llm):
    # When running against local/Ollama models or when curated palette is requested,
    # supply role-specific core tool sets (5-8 tools) + request_tools for on-demand discovery.
    is_local_backend = backend_id in ("ollama", "local", "local-llm")
    if is_local_backend or phase is not None:
        if norm_role in ("gemma", "qa"):
            wanted = set(WORKER_QA_CORE_TOOL_NAMES)
        elif norm_role in ("local-llm", "gitops"):
            wanted = set(WORKER_GITOPS_CORE_TOOL_NAMES)
        else:
            wanted = set(WORKER_CODE_CORE_TOOL_NAMES)

        curated = [t for t in all_permitted if t.name in wanted]
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
# A streaming provider must keep producing. These bounds guarantee every turn
# reaches a terminal state even when the transport never closes: silence longer
# than the idle timeout aborts the stream, and the turn deadline bounds streams
# that trickle output forever.
DEFAULT_STREAM_IDLE_TIMEOUT: float = float(os.environ.get("PROVIDER_STREAM_IDLE_TIMEOUT", "120"))
# Prompt prefill for a long planning prompt can legitimately take minutes before
# the first token, so the wait for the first chunk is wider than the gap allowed
# between chunks once generation has started.
DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT: float = float(
    os.environ.get("PROVIDER_STREAM_FIRST_CHUNK_TIMEOUT", "300")
)
DEFAULT_TURN_TIMEOUT: float = float(os.environ.get("PROVIDER_TURN_TIMEOUT", "1800"))
_STREAM_POLL_INTERVAL: float = 0.1


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


_POLLING_TOOLS = {"process_status", "process_output", "process_poll", "task_status"}


def _abort_stream(abort: Callable[[], None] | None) -> None:
    """Best-effort release of an in-flight provider stream.

    Runs off-thread on purpose: closing an ``http.client`` response whose read
    is blocked in another thread can itself block until the socket read returns
    (observed on Windows, where neither ``shutdown`` nor ``socket.close`` wakes
    a pending recv). The turn must terminate now, not when the transport
    eventually notices.
    """
    if abort is None:
        return

    def _release() -> None:
        try:
            abort()
        except Exception:
            pass

    Thread(target=_release, name="provider-stream-abort", daemon=True).start()


def _iter_stream_bounded(
    chunks: Any,
    *,
    idle_timeout: float | None,
    deadline: float | None,
    first_chunk_timeout: float | None = None,
    cancellation_token: Any | None = None,
    abort: Callable[[], None] | None = None,
    poll_interval: float | None = None,
) -> Iterator[Any]:
    """Yield chunks from ``chunks`` without ever blocking the turn forever.

    The provider iterator runs on a worker thread, so a transport that stops
    producing (a stalled socket, a server that holds the connection open, or a
    generator that never returns) cannot pin the operation: this consumer
    enforces an inactivity timeout between chunks, an optional wall-clock
    deadline, and cooperative cancellation.  On any of those it aborts the
    underlying stream and raises, so the caller always reaches a terminal
    state and the transport resource is released.
    """
    queue: Queue[tuple[str, Any]] = Queue()

    def _produce() -> None:
        try:
            for item in chunks:
                queue.put(("chunk", item))
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            queue.put(("error", exc))
        finally:
            queue.put(("done", None))

    Thread(target=_produce, name="provider-stream", daemon=True).start()
    interval = poll_interval if poll_interval is not None else _STREAM_POLL_INTERVAL
    last_chunk_at = time.monotonic()
    received = 0

    while True:
        if cancellation_token is not None:
            try:
                check_cancellation(cancellation_token)
            except OperationCancelledError:
                _abort_stream(abort)
                raise

        now = time.monotonic()
        if deadline is not None and now >= deadline:
            _abort_stream(abort)
            raise ProviderStreamInterruptedError(
                f"Provider stream exceeded the turn deadline after {received} chunk(s)",
                chunks_received=received,
                reason="deadline",
            )

        limit = first_chunk_timeout if received == 0 and first_chunk_timeout else idle_timeout
        wait = interval
        if limit is not None and limit > 0:
            wait = min(wait, max(0.0, limit - (now - last_chunk_at)))
        if deadline is not None:
            wait = min(wait, max(0.0, deadline - now))

        try:
            kind, payload = queue.get(timeout=max(wait, 0.001))
        except Empty:
            silence = time.monotonic() - last_chunk_at
            if limit is not None and limit > 0 and silence >= limit:
                _abort_stream(abort)
                if received == 0:
                    raise ProviderStreamInterruptedError(
                        f"Provider stream produced no output within {limit:g}s",
                        chunks_received=0,
                        reason="first_chunk_timeout",
                    )
                raise ProviderStreamInterruptedError(
                    f"Provider stream stalled: no output for {limit:g}s "
                    f"after {received} chunk(s)",
                    chunks_received=received,
                    reason="idle_timeout",
                )
            continue

        if kind == "done":
            return
        if kind == "error":
            _abort_stream(abort)
            if cancellation_token is not None:
                # Adapters may wrap their own cancellation check as a provider
                # or timeout error (closing the socket mid-cancel does exactly
                # that). A cancelled turn must be reported as cancelled, never
                # as a stream failure.
                check_cancellation(cancellation_token)
            raise payload

        received += 1
        last_chunk_at = time.monotonic()
        yield payload



def detect_tool_cycle(signatures: list[tuple[str, str]]) -> str | None:
    """Detect repeating cycles of tool calls (e.g. A->B->A->B)."""
    for cycle_len in (2, 3):
        for mult in (3, 2):
            needed = cycle_len * mult
            if len(signatures) >= needed:
                chunk = signatures[-needed:]
                pattern = chunk[:cycle_len]
                if chunk == pattern * mult:
                    # Polling tools (e.g. process_status -> process_output) naturally alternate
                    # while waiting for background operations; exempt them from false-positive trips.
                    if mult < 4 and all(p[0] in _POLLING_TOOLS for p in pattern):
                        continue
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
        on_tool_call: Callable[[str, dict[str, Any], str], None] | None = None,
        on_tool_result: Callable[[str, str, str, Any, Any], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_usage: Callable[[TokenUsage], None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.tool_gateway = tool_gateway
        self.attempt = attempt
        self.contract = contract or (attempt.contract if attempt and hasattr(attempt, "contract") else None)
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_thinking = on_thinking
        self.on_token = on_token
        self.on_usage = on_usage
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
        self.required_deliverable: str | None = None
        self.read_counts: dict[str, int] = {}
        self._step_count: int = 0
        self._file_write_count: int = 0
        self.budget_status: dict[str, Any] = {"tokens_used": 0, "max_tokens": None, "tool_calls_used": 0, "max_tool_calls": None, "steps": 0}
        is_local = self._is_local_provider(self.adapter)
        if max_tool_output_chars == DEFAULT_MAX_TOOL_OUTPUT_CHARS and is_local:
            max_tool_output_chars = int(os.environ.get("LOCAL_TOOL_OUTPUT_CHARS", "4000"))
        self.max_tool_output_chars: int = max_tool_output_chars

        # Track budget status
        if self.contract and hasattr(self.contract, "budget") and self.contract.budget:
            self.budget_status["max_tokens"] = self.contract.budget.get("max_tokens")
            self.budget_status["max_tool_calls"] = self.contract.budget.get("max_tool_calls")

    def has_written_deliverable(self, required_deliverable: str | None) -> bool:
        if not required_deliverable:
            return True
        norm_del = Path(required_deliverable).as_posix().lstrip("/")
        norm_written = {Path(p).as_posix().lstrip("/") for p in self.written_files}
        return norm_del in norm_written

    def _compact_messages_for_context(self, messages: list[Message]) -> None:
        """Compact older tool outputs in multi-turn loops to avoid blowing model context window."""
        is_local = self._is_local_provider(self.adapter)

        tool_indices = [i for i, m in enumerate(messages) if str(m.role).lower() == "tool"]
        if not tool_indices:
            return

        keep_recent = 2 if is_local else 4
        older_indices = tool_indices[:-keep_recent] if len(tool_indices) > keep_recent else []

        for idx in older_indices:
            m = messages[idx]
            content = m.content or ""
            max_retain = 250 if is_local else 1200
            if len(content) > max_retain:
                head = content[:150] if is_local else content[:250]
                tail = content[-80:] if is_local else content[-150:]
                compacted = (
                    f"{head}\n"
                    f"... [Earlier tool output truncated to preserve context window: {len(content)} chars omitted] ...\n"
                    f"{tail}"
                )
                messages[idx] = Message.tool(
                    content=compacted,
                    tool_call_id=m.tool_call_id or "call_unknown",
                    name=m.name,
                )

    @staticmethod
    def _is_local_provider(adapter: "ProviderAdapter") -> bool:
        """Detect whether an adapter targets a local/self-hosted model.

        Checks provider_name for known local providers AND base_url/endpoint
        for localhost/127.0.0.1 addresses so that 'custom' providers pointing
        at local servers also bypass cloud-oriented token budgets.
        """
        provider = str(getattr(adapter, "provider_name", "") or "").lower()
        if provider in ("ollama", "local", "llamacpp"):
            return True
        # Check base_url / endpoint for localhost addresses
        base_url = str(
            getattr(adapter, "base_url", None)
            or getattr(adapter, "endpoint", None)
            or ""
        ).lower()
        if base_url:
            # Match http://localhost:*, http://127.0.0.1:*, http://[::1]:*
            for local_host in ("localhost", "127.0.0.1", "[::1]"):
                if f"//{local_host}" in base_url:
                    return True
        return False

    def _get_max_tokens(self) -> int | None:
        # Local models have no token limit (infinite budget)
        if self._is_local_provider(self.adapter):
            return None
        if self.contract and hasattr(self.contract, "budget") and self.contract.budget:
            mt = self.contract.budget.get("max_tokens")
            if mt is None or mt <= 0:
                return None
            return mt
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
                target = (
                    args.get("path")
                    or args.get("target")
                    or args.get("filename")
                    or args.get("file")
                    or args.get("filepath")
                    or args.get("file_path")
                    or ""
                )
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
                r_count = self.read_counts.get(target, 0) + 1
                self.read_counts[target] = r_count
                out = truncate_tool_output(result, max_chars=self.max_tool_output_chars)
                if r_count > 1:
                    out += (
                        f"\n\n[NOTICE: You have already read '{target}' in this session ({r_count} times). "
                        f"Do NOT read this file again. Proceed immediately to author your assigned deliverable using write_file.]"
                    )
                return out

            if name == "write_file":
                target = ""
                for k in ("path", "target", "filename", "file", "filepath", "file_path", "destination", "target_path", "target_files", "files"):
                    v = args.get(k)
                    if isinstance(v, list) and v:
                        target = str(v[0]).strip()
                        break
                    elif isinstance(v, str) and v.strip():
                        target = v.strip()
                        break
                if not target:
                    target = getattr(self, "required_deliverable", None) or ""
                if not target:
                    for msg in reversed(getattr(self, "_active_messages", None) or []):
                        m_tf = re.search(r"Target files?:\s*\n?\s*[-*]?\s*([a-zA-Z0-9_./\\-]+)", getattr(msg, "content", "") or "")
                        if m_tf:
                            target = m_tf.group(1).strip()
                            break
                if not target:
                    content_val = args.get("content") or args.get("data") or args.get("text") or args.get("body") or args.get("code") or ""
                    if isinstance(content_val, str) and ("<!DOCTYPE html" in content_val or "<html" in content_val):
                        target = "index.html"
                if not target:
                    self.consecutive_failures += 1
                    res = "Error: 'write_file' missing required argument 'path'. Example: {\"path\": \"src/api/auth.py\", \"content\": \"...\"}"
                    if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                        raise ConsecutiveToolFailureError(
                            f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                            failures=self.consecutive_failures,
                        )
                    return res

                content = (
                    args.get("content")
                    if "content" in args
                    else args.get("data", args.get("text", args.get("body", args.get("code", None))))
                )
                if content is None:
                    candidates = [
                        v for k, v in args.items()
                        if k not in ("path", "target", "filename", "file", "filepath", "file_path", "destination", "target_path")
                        and isinstance(v, str)
                    ]
                    if len(candidates) == 1:
                        content = candidates[0]

                if content is None:
                    self.consecutive_failures += 1
                    res = "Error: 'write_file' missing required argument 'content'. Example: {\"path\": \"src/api/auth.py\", \"content\": \"...\"}"
                    if self.consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
                        raise ConsecutiveToolFailureError(
                            f"Multiple consecutive tool failures ({self.consecutive_failures}): {res}",
                            failures=self.consecutive_failures,
                        )
                    return res

                old_content = ""
                rel_target = target.replace("\\", "/").strip().lstrip("/")
                try:
                    if hasattr(self.tool_gateway, "workspace"):
                        target_path = self.tool_gateway.workspace.path_for(target)
                        if target_path.is_file():
                            old_content = target_path.read_text(encoding="utf-8")
                except Exception:
                    old_content = ""

                self.tool_gateway.write_file(target, content)
                self.consecutive_failures = 0
                if target not in self.written_files:
                    self.written_files.append(target)

                # Generate standard unified diff for TUI and reviewers
                import difflib
                old_lines = old_content.splitlines(keepends=True) if old_content else []
                new_lines = content.splitlines(keepends=True) if content else []
                diff_lines = list(difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{rel_target}",
                    tofile=f"b/{rel_target}",
                    n=3,
                ))
                diff_str = "".join(diff_lines)
                if diff_str.strip():
                    return f"Successfully wrote {len(content)} characters to {target}\n{diff_str}"
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
                    "web": {"web_search", "web_fetch", "image_search"},
                    "image": {"image_search", "web_search", "web_fetch"},
                    "images": {"image_search", "web_search", "web_fetch"},
                    "linter": {"run_command"},
                    "linters": {"run_command"},
                    "diagnostics": {"inspect_environment", "inspect_logs", "inspect_version", "checkpoint"},
                    "search": {"grep", "glob", "semantic_search", "search_docs", "web_search", "image_search", "web_fetch"},
                    "browser": {"browser_open", "browser_navigate", "browser_click", "browser_type", "browser_screenshot", "inspect_screenshot"},
                    "process": {"process_start", "process_status", "process_output", "process_stop"},
                    "testing": {"run_tests", "run_lint", "run_typecheck", "run_security_scan", "verify_deliverable", "verify_tests"},
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
                    return f"No permitted tools found matching '{query}'. Available categories: web, image, git, analysis, knowledge, search, linters."

            if hasattr(self.tool_gateway, name):
                method = getattr(self.tool_gateway, name)
                import inspect
                sig = inspect.signature(method)
                kwargs = {}
                for param_name, param in sig.parameters.items():
                    if param_name in args:
                        kwargs[param_name] = args[param_name]
                    elif param_name == "target" and any(k in args for k in ("path", "file", "filename", "filepath")):
                        kwargs[param_name] = args.get("path") or args.get("file") or args.get("filename") or args.get("filepath")
                    elif param_name == "path" and any(k in args for k in ("target", "file", "filename", "filepath")):
                        kwargs[param_name] = args.get("target") or args.get("file") or args.get("filename") or args.get("filepath")
                    elif param_name == "filter_term" and any(k in args for k in ("filter", "q", "term")):
                        kwargs[param_name] = args.get("filter") or args.get("q") or args.get("term")
                    elif param_name == "task_text" and any(k in args for k in ("text", "task", "item")):
                        kwargs[param_name] = args.get("text") or args.get("task") or args.get("item")
                    elif param_name == "checkpoint_id" and any(k in args for k in ("id", "cp_id", "label")):
                        kwargs[param_name] = args.get("id") or args.get("cp_id") or args.get("label")
                    elif param_name == "patch_content" and any(k in args for k in ("patch", "diff", "content")):
                        kwargs[param_name] = args.get("patch") or args.get("diff") or args.get("content")
                    elif param_name == "agent_name" and any(k in args for k in ("agent", "name", "role", "actor")):
                        kwargs[param_name] = args.get("agent") or args.get("name") or args.get("role") or args.get("actor")
                    elif param_name == "wo_id" and any(k in args for k in ("work_order_id", "work_order", "id", "wo", "workorder")):
                        kwargs[param_name] = (
                            args.get("work_order_id")
                            or args.get("work_order")
                            or args.get("id")
                            or args.get("wo")
                            or args.get("workorder")
                        )
                    elif param_name == "skill_name" and any(k in args for k in ("skill", "name")):
                        kwargs[param_name] = args.get("skill") or args.get("name")
                    elif param_name == "command" and any(k in args for k in ("cmd", "args")):
                        kwargs[param_name] = args.get("cmd") or args.get("args")
                    elif param_name == "query" and any(k in args for k in ("q", "search", "prompt")):
                        kwargs[param_name] = args.get("q") or args.get("search") or args.get("prompt")
                    elif param_name == "limit" and any(k in args for k in ("max_results", "count", "num_results", "num", "top_k")):
                        kwargs[param_name] = args.get("max_results") or args.get("count") or args.get("num_results") or args.get("num") or args.get("top_k")
                    elif param_name == "media_type" and any(k in args for k in ("type", "media", "search_type")):
                        kwargs[param_name] = args.get("type") or args.get("media") or args.get("search_type")
                    elif param_name == "extract_images" and any(k in args for k in ("images", "include_images")):
                        kwargs[param_name] = args.get("images") if "images" in args else args.get("include_images")
                    elif param_name == "url" and any(k in args for k in ("link", "uri", "target", "path", "href")):
                        kwargs[param_name] = args.get("link") or args.get("uri") or args.get("target") or args.get("path") or args.get("href")
                    elif param_name == "symbol" and any(k in args for k in ("name", "target")):
                        kwargs[param_name] = args.get("name") or args.get("target")
                    elif param_name == "instructions" and any(k in args for k in ("instruction", "prompt", "message", "task")):
                        kwargs[param_name] = args.get("instruction") or args.get("prompt") or args.get("message") or args.get("task")
                    elif param_name == "summary" and any(k in args for k in ("description", "report", "message")):
                        kwargs[param_name] = args.get("description") or args.get("report") or args.get("message")
                    elif param_name == "verdict" and any(k in args for k in ("status", "decision")):
                        kwargs[param_name] = args.get("status") or args.get("decision")
                    elif param_name == "report" and any(k in args for k in ("summary", "description", "details")):
                        kwargs[param_name] = args.get("summary") or args.get("description") or args.get("details")
                    elif param_name == "new_version" and "version" in args:
                        kwargs[param_name] = args.get("version")
                    elif param_name == "entry" and any(k in args for k in ("changelog", "text", "content")):
                        kwargs[param_name] = args.get("changelog") or args.get("text") or args.get("content")
                    elif param_name == "goal" and any(k in args for k in ("prompt", "description", "task", "text")):
                        kwargs[param_name] = args.get("goal") or args.get("prompt") or args.get("description") or args.get("task") or args.get("text")
                    elif param_name == "target_agent" and any(k in args for k in ("agent", "agent_name", "role", "target")):
                        kwargs[param_name] = args.get("target_agent") or args.get("agent") or args.get("agent_name") or args.get("role") or args.get("target")
                    elif param_name == "target_files" and any(k in args for k in ("files", "target_file", "file", "paths")):
                        kwargs[param_name] = args.get("target_files") or args.get("files") or args.get("target_file") or args.get("file") or args.get("paths")

                result = method(**kwargs)
                self.consecutive_failures = 0
                if name == "apply_patch":
                    p_content = kwargs.get("patch_content") or ""
                    tgt = kwargs.get("target") or "file"
                    out_str = f"Successfully applied patch to {tgt}\n{p_content}".strip()
                elif result is None:
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
            from validators.kernel.tools import RevisionConflictError
            if isinstance(ex, RevisionConflictError):
                import json as _json
                self.consecutive_failures = 0
                return (
                    "Error: REVISION_CONFLICT "
                    + _json.dumps(ex.to_dict(), sort_keys=True)
                    + " Nothing was written. Re-read the work order with read_file, then "
                    "retry update_work_order with the reported current_revision."
                )
            self.consecutive_failures += 1
            if isinstance(ex, FileNotFoundError):
                if name in ("run_command", "bash", "execute_command"):
                    cmd_val = args.get("command") or args.get("cmd") or args.get("args") or ""
                    cmd_str = " ".join(str(c) for c in cmd_val) if isinstance(cmd_val, list) else str(cmd_val)
                    res = (
                        f"Error: Command execution '{cmd_str}' failed: executable or file not found. "
                        f"In StackMind CLI, shell tools like grep/cat/mkdir are restricted. "
                        f"Use 'read_file' to inspect file contents or 'list_directory' to list files."
                    )
                else:
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
                actor_str = str(getattr(getattr(self, "tool_gateway", None), "actor_id", "")).lower()
                target_str = (
                    args.get("path")
                    or args.get("target")
                    or args.get("file")
                    or args.get("query")
                    or args.get("q")
                    or ("." if name in ("list_directory", "glob") else "")
                )
                if name == "run_command":
                    cmd_val = args.get("command") or args.get("cmd") or args.get("args") or ""
                    cmd_str = " ".join(str(c) for c in cmd_val) if isinstance(cmd_val, list) else str(cmd_val)
                    target_str = cmd_str
                    res = (
                        f"Error: PermissionError: Command execution '{cmd_str}' was denied: {ex}. "
                        f"CRITICAL: Do NOT retry this command. Follow the actionable remedy below."
                    )
                    if any(k in actor_str for k in ("claude", "architect", "architecture")):
                        res += (
                            "\nGUIDANCE FOR ARCHITECT: As the Lead Architect, shell/bash command execution is strictly denied. "
                            "Use 'read_file' to inspect file contents or 'list_directory' to list files. "
                            "If you need shell tasks (builds, tests) run, delegate them to Backend (Codex) or QA (Gemma)."
                        )
                else:
                    target_display = f"'{target_str}'" if target_str else f"tool '{name}'"
                    if "Authoring validation failed" in str(ex):
                        res = (
                            f"Error: Authoring validation failed for {target_display}: {ex}. "
                            f"Please correct the YAML syntax or schema structure and retry writing."
                        )
                    else:
                        res = (
                            f"Error: PermissionError: Operation on {target_display} was denied by runtime policy/contract: {ex}. "
                            f"CRITICAL: Do NOT retry accessing or writing {target_display} as it is outside your assigned scope. "
                            f"Please proceed immediately with your assigned tasks and deliverables or conclude your turn."
                        )
                        if any(k in actor_str for k in ("claude", "architect", "architecture")) and name == "write_file":
                            res += (
                                "\nGUIDANCE FOR ARCHITECT: As the Lead Architect, you govern the system but cannot directly write workspace application source files. "
                                "To implement these changes autonomously: "
                                "1. Call `delegate_task(target_agent='frontend'|'backend'|'qa'|'gitops', instructions=..., target_files=[...])` for focused changes, tests, or release tasks. "
                                "2. Call `trigger_goal(goal=...)` for full multi-agent feature implementation."
                            )

                # Record incident to .sync/state/incidents.jsonl and extract actionable remedy
                remedy_text = ""
                try:
                    from validators.kernel.incidents import record_permission_incident
                    proj_path = (
                        getattr(getattr(self.tool_gateway, "workspace", None), "authoritative_root", None)
                        or getattr(getattr(self.tool_gateway, "workspace", None), "root", None)
                    )
                    actor_id = getattr(self.tool_gateway, "actor_id", "agent")
                    inc_rec = record_permission_incident(
                        project_path=proj_path,
                        agent=actor_id,
                        role=actor_str,
                        tool=name,
                        target=target_str,
                        reason=str(ex),
                    )
                    if inc_rec and inc_rec.get("remedy"):
                        remedy_text = inc_rec["remedy"]
                except Exception:
                    pass

                # Inject the actionable remedy directly into the tool response
                if remedy_text:
                    res += f"\nACTIONABLE REMEDY: {remedy_text}"

                # In-turn repetition guard: alert if agent retries similar blocked action
                if not hasattr(self, "_denial_history"):
                    self._denial_history = []
                is_repeated = any(
                    prev_tool == name and (prev_target == target_str or (target_str and prev_target in target_str))
                    for prev_tool, prev_target in self._denial_history
                )
                self._denial_history.append((name, target_str))
                if is_repeated:
                    res += (
                        f"\n⚠️ REPETITION ALERT: You have already attempted a denied '{name}' operation in this turn. "
                        f"DO NOT retry this operation. Comply with the actionable remedy above or conclude your turn."
                    )
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

        # Streaming path: use adapter.stream() for progressive delivery (Phase 34)
        # Use streaming when adapter supports it, passing active_tools so the provider
        # can properly generate tool calls during streaming
        supports_streaming = (
            hasattr(self.adapter, "stream")
            and callable(self.adapter.stream)
        )

        if supports_streaming:
            # Use streaming for incremental text delivery (with tools if configured)
            content_parts: list[str] = []
            accumulated_tool_calls: list[ToolCallRequest] = []
            final_finish_reason = None
            final_usage = TokenUsage()
            chunk_count = 0
            effective_timeout = timeout if timeout is not None else DEFAULT_TURN_TIMEOUT
            stream_deadline = (
                time.monotonic() + effective_timeout if effective_timeout and effective_timeout > 0 else None
            )
            # Per-read socket timeout: never longer than the stream watchdog
            # window, so a transport that stops sending releases its reader
            # (and socket) within a bounded time even if it ignores the abort.
            stream_socket_timeout = effective_timeout
            watchdog_windows = [
                window
                for window in (DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT, DEFAULT_STREAM_IDLE_TIMEOUT)
                if window and window > 0
            ]
            if watchdog_windows:
                cap = max(watchdog_windows)
                stream_socket_timeout = (
                    min(stream_socket_timeout, cap)
                    if stream_socket_timeout and stream_socket_timeout > 0
                    else cap
                )

            try:
                for chunk in _iter_stream_bounded(
                    self.adapter.stream(
                        messages,
                        tools=active_tools,
                        timeout=stream_socket_timeout,
                        cancellation_token=cancellation_token,
                        **kwargs,
                    ),
                    idle_timeout=DEFAULT_STREAM_IDLE_TIMEOUT,
                    first_chunk_timeout=DEFAULT_STREAM_FIRST_CHUNK_TIMEOUT,
                    deadline=stream_deadline,
                    cancellation_token=cancellation_token,
                    abort=getattr(self.adapter, "abort", None),
                ):
                    check_cancellation(cancellation_token)
                    chunk_count += 1

                    # Forward thinking deltas immediately
                    if chunk.delta_thinking and self.on_thinking:
                        try:
                            self.on_thinking(chunk.delta_thinking)
                        except Exception:
                            pass

                    # Forward text deltas immediately for progressive rendering
                    if chunk.delta_content:
                        content_parts.append(chunk.delta_content)
                        if self.on_token:
                            try:
                                self.on_token(chunk.delta_content)
                            except Exception:
                                pass

                    # Accumulate tool calls from stream
                    if chunk.delta_tool_calls:
                        accumulated_tool_calls.extend(chunk.delta_tool_calls)

                    # Accumulate usage and track finish reason
                    if chunk.usage:
                        final_usage = final_usage + chunk.usage
                    if chunk.finish_reason:
                        final_finish_reason = chunk.finish_reason

                partial_text = "".join(content_parts)
                if final_finish_reason is None and not accumulated_tool_calls:
                    # The provider closed the stream without its terminal signal.
                    # Returning this as a normal completion silently accepts a
                    # truncated answer, so surface it with the text it did emit.
                    if partial_text.strip():
                        raise ProviderStreamInterruptedError(
                            f"Provider stream ended after {chunk_count} chunk(s) without a completion signal",
                            partial_text=partial_text,
                            chunks_received=chunk_count,
                            reason="premature_end",
                        )
                    raise ProviderStreamInterruptedError(
                        "Provider stream ended without producing any output",
                        chunks_received=chunk_count,
                        reason="empty_stream",
                    )

                # Build final response
                response_message = Message.assistant(
                    content=partial_text,
                    tool_calls=accumulated_tool_calls,
                )
                response = ProviderResponse(
                    message=response_message,
                    usage=final_usage,
                    finish_reason=final_finish_reason or "tool_calls",
                    model=getattr(self.adapter, "model_name", ""),
                    raw_payload={},
                )
            except NotImplementedError:
                # Streaming is genuinely unsupported by this adapter: one
                # bounded non-streaming request, never a blind retry of a
                # stream that already produced output.
                response = self.adapter.complete(
                    messages,
                    tools=active_tools,
                    timeout=effective_timeout,
                    cancellation_token=cancellation_token,
                    **kwargs,
                )
            except OperationCancelledError:
                # Already aborted by the bounded consumer that noticed the
                # cancellation; propagate it unchanged for the daemon's
                # CANCELLED terminal state.
                raise
            except ProviderStreamInterruptedError as exc:
                if not exc.partial_text and content_parts:
                    # The watchdog fires outside the per-chunk loop, so attach
                    # the text the model had already produced.
                    raise ProviderStreamInterruptedError(
                        str(exc),
                        partial_text="".join(content_parts),
                        chunks_received=exc.chunks_received or chunk_count,
                        reason=exc.reason,
                    ) from exc
                raise
            except (ProviderError, TimeoutError) as exc:
                partial_text = "".join(content_parts)
                if partial_text.strip():
                    # Retrying would discard what the user already saw and hide
                    # the failure behind a second full-length request.
                    raise ProviderStreamInterruptedError(
                        f"Provider stream failed after partial output: {exc}",
                        partial_text=partial_text,
                        chunks_received=chunk_count,
                        reason="stream_error",
                    ) from exc
                raise
        else:
            # Standard complete path (with tools or streaming not supported)
            response = self.adapter.complete(
                messages,
                tools=active_tools,
                timeout=timeout,
                cancellation_token=cancellation_token,
                **kwargs,
            )

            # Publish full response via on_token if callback provided
            if response.message and response.message.content:
                from cli.tui.chat import extract_internal_reasoning
                think_extracted = extract_internal_reasoning(response.message.content)
                if not think_extracted and hasattr(response, "raw_payload") and isinstance(response.raw_payload, dict):
                    raw_msg = response.raw_payload.get("message")
                    if isinstance(raw_msg, dict):
                        raw_think = raw_msg.get("thinking") or raw_msg.get("reasoning_content")
                        if isinstance(raw_think, str) and raw_think.strip():
                            think_extracted = raw_think.strip()
                if think_extracted and self.on_thinking:
                    try:
                        self.on_thinking(think_extracted)
                    except Exception:
                        pass

                if self.on_token and not response.message.tool_calls:
                    try:
                        self.on_token(response.message.content)
                    except Exception:
                        pass

        self._record_and_check_budget(response.usage)
        self.budget_status["tokens_used"] = self.total_usage.total_tokens
        self.budget_status["tool_calls_used"] = self.total_tool_calls
        if self.on_usage:
            try:
                self.on_usage(self.total_usage)
            except Exception:
                pass

        messages.append(response.message)

        if response.message and response.message.tool_calls:
            if len(response.message.tool_calls) > DEFAULT_MAX_TOOL_CALLS_PER_TURN:
                raise ToolLimitExceededError(
                    f"Turn emitted {len(response.message.tool_calls)} tool calls, exceeding maximum allowed per turn ({DEFAULT_MAX_TOOL_CALLS_PER_TURN})",
                    tool_calls=len(response.message.tool_calls),
                    max_tool_calls=DEFAULT_MAX_TOOL_CALLS_PER_TURN,
                )
            for tc in (response.message.tool_calls or ()):
                check_cancellation(cancellation_token)
                call_id = tc.id or f"call_{uuid4().hex[:8]}"
                if self.on_tool_call:
                    try:
                        self.on_tool_call(tc.name, tc.arguments or {}, call_id)
                    except Exception:
                        pass
                tool_output = None
                tool_error = None
                status = "success"
                try:
                    tool_output = self.execute_tool_call(
                        tc,
                        cancellation_token=cancellation_token,
                        max_tool_calls=max_tool_calls,
                    )
                    if tool_output and (str(tool_output).startswith("Error:") or str(tool_output).startswith("Warning:")):
                        status = "failure"
                        tool_error = str(tool_output)
                except Exception as err:
                    status = "failure"
                    tool_error = str(err)
                    raise
                finally:
                    if self.on_tool_result:
                        try:
                            self.on_tool_result(tc.name, call_id, status, tool_output, tool_error)
                        except Exception:
                            pass
                messages.append(Message.tool(content=tool_output, tool_call_id=tc.id, name=tc.name))

        return response

    def is_budget_exhausted(self) -> bool:
        """Check if any budget limit has been exhausted."""
        max_tokens = self.budget_status.get("max_tokens")
        if max_tokens is not None and self.budget_status["tokens_used"] >= max_tokens:
            return True
        max_tool_calls = self.budget_status.get("max_tool_calls")
        if max_tool_calls is not None and self.budget_status["tool_calls_used"] >= max_tool_calls:
            return True
        return False

    def run_loop(
        self,
        messages: list[Message],
        *,
        max_turns: int | None = None,
        max_tool_calls: int | None = None,
        tools: Sequence[ToolDefinition] | None = None,
        timeout: float | None = None,
        cancellation_token: Any | None = None,
        required_deliverable: str | None = None,
        **kwargs: Any,
    ) -> list[Message]:
        """Execute full autonomous reasoning/tool loop until task completion or limit."""
        if tools is not None:
            self.active_tools_list = list(tools)
            request_tools_def = next((t for t in STANDARD_KERNEL_TOOLS if t.name == "request_tools"), None)
            if request_tools_def and request_tools_def.name not in {t.name for t in self.active_tools_list}:
                self.active_tools_list.append(request_tools_def)
        active_tools = self.active_tools_list

        self.budget_status["steps"] = 0
        self._active_messages = messages
        if required_deliverable:
            self.required_deliverable = required_deliverable

        budget_turns = self.contract.budget.get("max_turns") if (self.contract and hasattr(self.contract, "budget") and self.contract.budget) else None
        effective_max_turns = max_turns or budget_turns or DEFAULT_MAX_TURNS

        budget_tool_calls = self._get_budget_max_tool_calls()
        effective_max_tool_calls = max_tool_calls or budget_tool_calls or DEFAULT_MAX_TOOL_CALLS

        deadline = time.monotonic() + timeout if timeout else None
        recent_signatures: list[tuple[str, str]] = []
        signature_counts: dict[tuple[str, str], int] = {}

        empty_tool_nudges = 0
        max_empty_tool_nudges = 2
        loop_nudges = 0
        max_loop_nudges = 2

        for turn_idx in range(effective_max_turns):
            check_cancellation(cancellation_token)
            self._compact_messages_for_context(messages)
            self.budget_status["steps"] += 1

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

            # If the assistant gave an answer without emitting new tool calls, check if this was a false finish
            if not (response.message and response.message.tool_calls):
                norm_del = Path(required_deliverable).as_posix().lstrip("/") if required_deliverable else None
                norm_written = {Path(p).as_posix().lstrip("/") for p in self.written_files}
                has_unwritten_deliverable = bool(norm_del and norm_del not in norm_written)
                has_write_tool = any(t.name == "write_file" for t in active_tools)

                should_nudge = bool(
                    required_deliverable
                    and has_write_tool
                    and empty_tool_nudges < max_empty_tool_nudges
                    and (self.total_tool_calls == 0 or has_unwritten_deliverable)
                )

                if should_nudge:
                    empty_tool_nudges += 1
                    target_hint = f" '{required_deliverable}'" if required_deliverable else ""
                    nudge_content = (
                        f"You responded with text without calling tools or authoring required deliverable{target_hint}.\n"
                        f"You MUST invoke `write_file` now to author your required deliverable{target_hint}.\n"
                        "Note: Code execution, virtual environments, and tests are managed externally by the harness and QA lead. "
                        "Do not wait or look for a virtual environment (.venv) or shell execution.\n"
                        "To call write_file, invoke the tool or emit:\n"
                        "<tool_call>\n"
                        f'{{"name": "write_file", "arguments": {{"path": "{required_deliverable or "<file_path>"}", "content": "<file_content>"}}}}\n'
                        "</tool_call>\n"
                        "Please call `write_file` now."
                    )
                    messages.append(Message.user(nudge_content))
                    continue
                return messages
            else:
                empty_tool_nudges = 0

            # Check cumulative tool call limit after executing turn
            if self.total_tool_calls >= effective_max_tool_calls:
                raise ToolLimitExceededError(
                    f"Cumulative tool call limit ({effective_max_tool_calls}) reached",
                    tool_calls=self.total_tool_calls,
                    max_tool_calls=effective_max_tool_calls,
                )

            # Record signatures for repetition/cycle detection
            for tc in (response.message.tool_calls or () if response.message else ()):
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
                    if (
                        last_sig[0] in ("read_file", "list_directory", "glob", "grep")
                        and required_deliverable
                        and loop_nudges < max_loop_nudges
                    ):
                        loop_nudges += 1
                        signature_counts[last_sig] = 1
                        recent_signatures = [s for s in recent_signatures if s != last_sig] + [last_sig]
                        target_hint = (
                            f" You have NOT yet authored your assigned deliverable '{required_deliverable}'. Call write_file now to create '{required_deliverable}'."
                            if not self.has_written_deliverable(required_deliverable)
                            else " Proceed immediately with your work."
                        )
                        messages.append(Message.user(
                            f"[SYSTEM WARNING: You have called '{last_sig[0]}' {DEFAULT_MAX_CONSECUTIVE_IDENTICAL_CALLS} times in a row without making progress. "
                            f"Stop repeating this call.{target_hint} Repeated identical calls will abort your turn.]"
                        ))
                        continue
                    raise NoProgressLoopError(
                        f"Pathological loop: identical call '{last_sig[0]}' repeated {DEFAULT_MAX_CONSECUTIVE_IDENTICAL_CALLS} times without progress",
                        pattern=last_sig[0],
                    )

            # 3. Check per-signature total occurrences across turn
            triggered_sig = None
            triggered_count = 0
            for sig, count in signature_counts.items():
                if count >= DEFAULT_MAX_TOTAL_IDENTICAL_CALLS:
                    triggered_sig = sig
                    triggered_count = count
                    break

            if triggered_sig is not None:
                if (
                    triggered_sig[0] in ("read_file", "list_directory", "glob", "grep")
                    and required_deliverable
                    and loop_nudges < max_loop_nudges
                ):
                    loop_nudges += 1
                    signature_counts[triggered_sig] = 1
                    recent_signatures = [s for s in recent_signatures if s != triggered_sig] + [triggered_sig]
                    target_hint = (
                        f" You have NOT yet authored your assigned deliverable '{required_deliverable}'. Call write_file now to create '{required_deliverable}'."
                        if not self.has_written_deliverable(required_deliverable)
                        else " Proceed immediately with your work."
                    )
                    messages.append(Message.user(
                        f"[SYSTEM WARNING: You have called '{triggered_sig[0]}' with identical arguments {triggered_count} times without making progress. "
                        f"Stop repeating this call.{target_hint} Repeated identical calls will abort your turn.]"
                    ))
                    continue
                raise NoProgressLoopError(
                    f"Pathological loop: call '{triggered_sig[0]}' with identical arguments repeated {triggered_count} times without progress",
                    pattern=triggered_sig[0],
                )

        # If loop exited all turns while still emitting tool calls, it was exhausted
        raise ToolLoopExhaustedError(
            f"Autonomous tool loop reached maximum turns ({effective_max_turns}) without completing the task",
            turns=effective_max_turns,
            max_turns=effective_max_turns,
        )
