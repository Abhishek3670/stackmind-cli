"""Auditable first-class operation requests and records."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OperationType(str, Enum):
    # Repository Discovery
    READ_FILE = "read_file"
    READ_MANY_FILES = "read_many_files"
    LIST_DIRECTORY = "list_directory"
    GLOB = "glob"
    GREP = "grep"
    FIND_SYMBOL = "find_symbol"
    FIND_REFERENCES = "find_references"

    # Editing
    APPLY_PATCH = "apply_patch"
    WRITE_FILE = "write_file"
    MOVE_FILE = "move_file"
    DELETE_FILE = "delete_file"
    FORMAT_FILE = "format_file"

    # Execution
    RUN_COMMAND = "run_command"
    PROCESS_START = "process_start"
    PROCESS_STATUS = "process_status"
    PROCESS_OUTPUT = "process_output"
    PROCESS_STOP = "process_stop"
    RUN_TESTS = "run_tests"
    RUN_LINT = "run_lint"
    RUN_TYPECHECK = "run_typecheck"
    RUN_SECURITY_SCAN = "run_security_scan"

    # Knowledge
    QUERY_GRAPH = "query_graph"
    GET_CONTEXT = "get_context"
    FIND_CALLERS = "find_callers"
    FIND_CALLEES = "find_callees"
    IMPACT_ANALYSIS = "impact_analysis"
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    SEMANTIC_SEARCH = "semantic_search"
    RUNTIME_EVIDENCE = "runtime_evidence"
    DATA_FLOW_ANALYSIS = "data_flow_analysis"
    KNOWLEDGE_STATS = "knowledge_stats"

    # Governance
    GET_CONTRACT = "get_contract"
    VERIFY_CONTRACT = "verify_contract"
    VERIFY_SCOPE = "verify_scope"
    EXPLAIN_DENIAL = "explain_denial"
    INSPECT_BUDGET = "inspect_budget"
    SUBMIT_FOR_REVIEW = "submit_for_review"

    # Planning
    ASK_USER = "ask_user"
    TODO = "todo"
    ENTER_PLAN_MODE = "enter_plan_mode"
    EXIT_PLAN_MODE = "exit_plan_mode"
    CREATE_WORK_ORDER = "create_work_order"
    UPDATE_WORK_ORDER = "update_work_order"
    INSPECT_WORK_ORDER = "inspect_work_order"
    INSPECT_AGENT = "inspect_agent"
    DISPATCH_SUBAGENT = "dispatch_subagent"

    # Git Inspection
    GIT_STATUS = "git_status"
    GIT_DIFF = "git_diff"
    GIT_LOG = "git_log"
    GIT_SHOW = "git_show"
    GIT_BLAME = "git_blame"
    GIT_CHANGED_FILES = "git_changed_files"
    GIT_BRANCH = "git_branch"

    # Git Mutation
    GIT_STAGE = "git_stage"
    GIT_RESTORE = "git_restore"
    GIT_CREATE_BRANCH = "git_create_branch"
    GIT_COMMIT = "git_commit"
    GIT_TAG = "git_tag"
    GIT_PUSH = "git_push"
    CREATE_RELEASE = "create_release"
    ROLLBACK_RELEASE = "rollback_release"

    # Web Research
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    SEARCH_DOCS = "search_docs"

    # Browser / Visual
    BROWSER_OPEN = "browser_open"
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    BROWSER_TYPE = "browser_type"
    BROWSER_SCREENSHOT = "browser_screenshot"
    INSPECT_SCREENSHOT = "inspect_screenshot"

    # Diagnostics
    INSPECT_ENVIRONMENT = "inspect_environment"
    COLLECT_TEST_ARTIFACTS = "collect_test_artifacts"
    INSPECT_LOGS = "inspect_logs"
    INSPECT_PROCESSES = "inspect_processes"
    COMPARE_SNAPSHOTS = "compare_snapshots"
    CHECKPOINT = "checkpoint"
    RESTORE_CHECKPOINT = "restore_checkpoint"

    # Verification
    VERIFY_DELIVERABLE = "verify_deliverable"
    VERIFY_TESTS = "verify_tests"
    VERIFY_DIFF = "verify_diff"
    VERIFY_PROVENANCE = "verify_provenance"
    SUBMIT_VERDICT = "submit_verdict"
    REQUEST_CHANGES = "request_changes"
    APPROVE_WORK_ORDER = "approve_work_order"

    # Learning / Skills
    EXPERIENCE_SEARCH = "experience_search"
    SKILL_RETRIEVE = "skill_retrieve"
    SKILL_LIST = "skill_list"
    SKILL_MINE = "skill_mine"
    SKILL_TEST = "skill_test"
    SKILL_AUDIT = "skill_audit"
    SKILL_PROMOTE = "skill_promote"

    # Release
    INSPECT_VERSION = "inspect_version"
    UPDATE_VERSION = "update_version"
    UPDATE_CHANGELOG = "update_changelog"
    GENERATE_RELEASE_NOTES = "generate_release_notes"
    VALIDATE_RELEASE_METADATA = "validate_release_metadata"
    PREPARE_RELEASE = "prepare_release"


@dataclass(frozen=True)
class OperationRequest:
    operation_type: OperationType
    target: str
    session_id: str
    attempt_id: str
    actor_id: str
    provider_id: str
    correlation_id: str | None = None
    operation_id: str = ""

    def __post_init__(self) -> None:
        if not self.operation_id:
            object.__setattr__(self, "operation_id", str(uuid4()))


@dataclass(frozen=True)
class OperationRecord:
    request: OperationRequest
    authorized: bool
    reason: str
    result: Any = None
    recorded_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None


class OperationJournal:
    def __init__(self) -> None:
        self._records: list[OperationRecord] = []

    @property
    def records(self) -> tuple[OperationRecord, ...]:
        return tuple(self._records)

    def record(self, record: OperationRecord) -> OperationRecord:
        if any(existing.request.operation_id == record.request.operation_id for existing in self._records):
            raise ValueError("Operation IDs must be unique")
        self._records.append(record)
        return record

    def complete(self, operation_id: str, result: Any) -> OperationRecord:
        for index, record in enumerate(self._records):
            if record.request.operation_id == operation_id:
                completed = replace(record, result=result, completed_at=utc_now())
                self._records[index] = completed
                return completed
        raise KeyError(operation_id)


MUTATING_OPERATIONS: frozenset[str] = frozenset({
    OperationType.APPLY_PATCH.value,
    OperationType.WRITE_FILE.value,
    OperationType.MOVE_FILE.value,
    OperationType.DELETE_FILE.value,
    OperationType.FORMAT_FILE.value,
    OperationType.RUN_COMMAND.value,
    OperationType.PROCESS_START.value,
    OperationType.PROCESS_STOP.value,
    OperationType.GIT_STAGE.value,
    OperationType.GIT_RESTORE.value,
    OperationType.GIT_CREATE_BRANCH.value,
    OperationType.GIT_COMMIT.value,
    OperationType.GIT_TAG.value,
    OperationType.GIT_PUSH.value,
    OperationType.CREATE_RELEASE.value,
    OperationType.ROLLBACK_RELEASE.value,
    OperationType.CREATE_WORK_ORDER.value,
    OperationType.UPDATE_WORK_ORDER.value,
    OperationType.CHECKPOINT.value,
    OperationType.RESTORE_CHECKPOINT.value,
    OperationType.SUBMIT_VERDICT.value,
    OperationType.REQUEST_CHANGES.value,
    OperationType.APPROVE_WORK_ORDER.value,
    OperationType.SKILL_MINE.value,
    OperationType.SKILL_PROMOTE.value,
    OperationType.UPDATE_VERSION.value,
    OperationType.UPDATE_CHANGELOG.value,
    OperationType.PREPARE_RELEASE.value,
})


def is_mutating_operation(op: OperationType | str) -> bool:
    val = op.value if isinstance(op, OperationType) else str(op)
    return val in MUTATING_OPERATIONS
