"""Provider, logical-agent, and human authorization identities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class ProviderIdentity:
    provider_id: str
    provider_type: str


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    role: str


@dataclass(frozen=True)
class HumanIdentity:
    human_id: str
    display_name: str | None = None


@dataclass(frozen=True)
class AuthorizationPolicy:
    """Policy assigned by a human authorizer; it never derives from provider identity."""

    policy_id: str
    permitted_operations: frozenset[str] = field(default_factory=frozenset)
    authorized_by: HumanIdentity | None = None

    @property
    def name(self) -> str:
        return self.policy_id

    @classmethod
    def permit(cls, policy_id: str, operations: Iterable[str], authorized_by: HumanIdentity | None = None):
        return cls(policy_id, frozenset(operations), authorized_by)

    def permits(self, operation_type: str) -> bool:
        return operation_type in self.permitted_operations


def get_role_policy(role_or_agent: str) -> AuthorizationPolicy:
    """Return the canonical AuthorizationPolicy for a given agent role or alias."""
    from .operations import OperationType

    norm = role_or_agent.lower().strip()

    # Universal discovery operations available to all roles
    universal_discovery = {
        OperationType.READ_FILE.value,
        OperationType.READ_MANY_FILES.value,
        OperationType.LIST_DIRECTORY.value,
        OperationType.GLOB.value,
        OperationType.GREP.value,
        OperationType.FIND_SYMBOL.value,
        OperationType.FIND_REFERENCES.value,
    }

    # Universal governance introspection
    universal_governance = {
        OperationType.GET_CONTRACT.value,
        OperationType.VERIFY_CONTRACT.value,
        OperationType.VERIFY_SCOPE.value,
        OperationType.EXPLAIN_DENIAL.value,
        OperationType.INSPECT_BUDGET.value,
    }

    # Universal git inspection
    universal_git_inspection = {
        OperationType.GIT_STATUS.value,
        OperationType.GIT_DIFF.value,
        OperationType.GIT_LOG.value,
        OperationType.GIT_SHOW.value,
        OperationType.GIT_BLAME.value,
        OperationType.GIT_CHANGED_FILES.value,
        OperationType.GIT_BRANCH.value,
    }

    # Universal diagnostics & learning basics
    universal_diagnostics = {
        OperationType.INSPECT_ENVIRONMENT.value,
        OperationType.INSPECT_PROCESSES.value,
        OperationType.INSPECT_LOGS.value,
        OperationType.COMPARE_SNAPSHOTS.value,
        OperationType.CHECKPOINT.value,
        OperationType.RESTORE_CHECKPOINT.value,
        OperationType.TODO.value,
        OperationType.INSPECT_WORK_ORDER.value,
        OperationType.INSPECT_AGENT.value,
        OperationType.EXPERIENCE_SEARCH.value,
        OperationType.SKILL_RETRIEVE.value,
        OperationType.SKILL_LIST.value,
        OperationType.SEARCH_DOCS.value,
        OperationType.INSPECT_VERSION.value,
    }

    universal_knowledge = {
        OperationType.QUERY_GRAPH.value,
        OperationType.GET_CONTEXT.value,
        OperationType.FIND_CALLERS.value,
        OperationType.FIND_CALLEES.value,
        OperationType.IMPACT_ANALYSIS.value,
        OperationType.DEPENDENCY_ANALYSIS.value,
        OperationType.SEMANTIC_SEARCH.value,
        OperationType.RUNTIME_EVIDENCE.value,
        OperationType.DATA_FLOW_ANALYSIS.value,
        OperationType.KNOWLEDGE_STATS.value,
    }

    base_set = (
        universal_discovery
        | universal_governance
        | universal_git_inspection
        | universal_diagnostics
        | universal_knowledge
    )

    if any(k in norm for k in ("claude", "architecture", "architect")):
        ops = base_set | {
            OperationType.WRITE_FILE.value,  # Governed artifacts only
            OperationType.RUN_COMMAND.value,  # Read-only inspect
            OperationType.ASK_USER.value,
            OperationType.ENTER_PLAN_MODE.value,
            OperationType.EXIT_PLAN_MODE.value,
            OperationType.CREATE_WORK_ORDER.value,
            OperationType.UPDATE_WORK_ORDER.value,
            OperationType.DISPATCH_SUBAGENT.value,
            OperationType.WEB_SEARCH.value,
            OperationType.WEB_FETCH.value,
            OperationType.INSPECT_LOGS.value,
            OperationType.VERIFY_DIFF.value,
            OperationType.VERIFY_PROVENANCE.value,
            OperationType.SKILL_MINE.value,
            OperationType.SKILL_PROMOTE.value,
        }
        return AuthorizationPolicy.permit(f"policy-{norm}", ops)

    elif any(k in norm for k in ("codex", "backend")):
        ops = base_set | {
            OperationType.APPLY_PATCH.value,
            OperationType.WRITE_FILE.value,
            OperationType.MOVE_FILE.value,
            OperationType.DELETE_FILE.value,
            OperationType.FORMAT_FILE.value,
            OperationType.RUN_COMMAND.value,
            OperationType.PROCESS_START.value,
            OperationType.PROCESS_STATUS.value,
            OperationType.PROCESS_OUTPUT.value,
            OperationType.PROCESS_STOP.value,
            OperationType.RUN_TESTS.value,
            OperationType.RUN_LINT.value,
            OperationType.RUN_TYPECHECK.value,
            OperationType.RUN_SECURITY_SCAN.value,
            OperationType.SUBMIT_FOR_REVIEW.value,
            OperationType.ASK_USER.value,
            OperationType.GIT_RESTORE.value,
            OperationType.GIT_CREATE_BRANCH.value,
            OperationType.WEB_SEARCH.value,
            OperationType.WEB_FETCH.value,
            OperationType.COLLECT_TEST_ARTIFACTS.value,
            OperationType.INSPECT_LOGS.value,
            OperationType.INSPECT_PROCESSES.value,
            OperationType.VERIFY_DELIVERABLE.value,
            OperationType.VERIFY_TESTS.value,
            OperationType.VERIFY_DIFF.value,
        }
        return AuthorizationPolicy.permit(f"policy-{norm}", ops)

    elif any(k in norm for k in ("gemini", "frontend")):
        ops = base_set | {
            OperationType.APPLY_PATCH.value,
            OperationType.WRITE_FILE.value,
            OperationType.MOVE_FILE.value,
            OperationType.DELETE_FILE.value,
            OperationType.FORMAT_FILE.value,
            OperationType.RUN_COMMAND.value,
            OperationType.PROCESS_START.value,
            OperationType.PROCESS_STATUS.value,
            OperationType.PROCESS_OUTPUT.value,
            OperationType.PROCESS_STOP.value,
            OperationType.RUN_TESTS.value,
            OperationType.RUN_LINT.value,
            OperationType.RUN_TYPECHECK.value,
            OperationType.RUN_SECURITY_SCAN.value,
            OperationType.SUBMIT_FOR_REVIEW.value,
            OperationType.ASK_USER.value,
            OperationType.GIT_RESTORE.value,
            OperationType.GIT_CREATE_BRANCH.value,
            OperationType.WEB_SEARCH.value,
            OperationType.WEB_FETCH.value,
            OperationType.BROWSER_OPEN.value,
            OperationType.BROWSER_NAVIGATE.value,
            OperationType.BROWSER_CLICK.value,
            OperationType.BROWSER_TYPE.value,
            OperationType.BROWSER_SCREENSHOT.value,
            OperationType.INSPECT_SCREENSHOT.value,
            OperationType.COLLECT_TEST_ARTIFACTS.value,
            OperationType.INSPECT_LOGS.value,
            OperationType.INSPECT_PROCESSES.value,
            OperationType.VERIFY_DELIVERABLE.value,
            OperationType.VERIFY_TESTS.value,
            OperationType.VERIFY_DIFF.value,
        }
        return AuthorizationPolicy.permit(f"policy-{norm}", ops)

    elif any(k in norm for k in ("gemma", "qa")):
        # Note: No source code editing (apply_patch/write_file/move/delete are withheld)
        ops = base_set | {
            OperationType.RUN_COMMAND.value,
            OperationType.PROCESS_START.value,
            OperationType.PROCESS_STATUS.value,
            OperationType.PROCESS_OUTPUT.value,
            OperationType.PROCESS_STOP.value,
            OperationType.RUN_TESTS.value,
            OperationType.RUN_LINT.value,
            OperationType.RUN_TYPECHECK.value,
            OperationType.RUN_SECURITY_SCAN.value,
            OperationType.SUBMIT_FOR_REVIEW.value,
            OperationType.ASK_USER.value,
            OperationType.WEB_SEARCH.value,
            OperationType.WEB_FETCH.value,
            OperationType.BROWSER_OPEN.value,
            OperationType.BROWSER_NAVIGATE.value,
            OperationType.BROWSER_CLICK.value,
            OperationType.BROWSER_TYPE.value,
            OperationType.BROWSER_SCREENSHOT.value,
            OperationType.INSPECT_SCREENSHOT.value,
            OperationType.COLLECT_TEST_ARTIFACTS.value,
            OperationType.INSPECT_LOGS.value,
            OperationType.INSPECT_PROCESSES.value,
            OperationType.VERIFY_DELIVERABLE.value,
            OperationType.VERIFY_TESTS.value,
            OperationType.VERIFY_DIFF.value,
            OperationType.VERIFY_PROVENANCE.value,
            OperationType.SUBMIT_VERDICT.value,
            OperationType.REQUEST_CHANGES.value,
            OperationType.APPROVE_WORK_ORDER.value,
            OperationType.SKILL_TEST.value,
            OperationType.SKILL_AUDIT.value,
            OperationType.VALIDATE_RELEASE_METADATA.value,
        }
        return AuthorizationPolicy.permit(f"policy-{norm}", ops)

    elif any(k in norm for k in ("local-llm", "gitops", "release")):
        # Exclusively holds all Git mutations and release management
        ops = base_set | {
            OperationType.APPLY_PATCH.value,  # Version/changelog metadata only
            OperationType.WRITE_FILE.value,   # Version/changelog metadata only
            OperationType.MOVE_FILE.value,
            OperationType.DELETE_FILE.value,
            OperationType.FORMAT_FILE.value,
            OperationType.RUN_COMMAND.value,
            OperationType.PROCESS_START.value,
            OperationType.PROCESS_STATUS.value,
            OperationType.PROCESS_OUTPUT.value,
            OperationType.PROCESS_STOP.value,
            OperationType.RUN_TESTS.value,
            OperationType.RUN_LINT.value,
            OperationType.RUN_TYPECHECK.value,
            OperationType.RUN_SECURITY_SCAN.value,
            OperationType.SUBMIT_FOR_REVIEW.value,
            OperationType.ASK_USER.value,
            OperationType.GIT_STAGE.value,
            OperationType.GIT_RESTORE.value,
            OperationType.GIT_CREATE_BRANCH.value,
            OperationType.GIT_COMMIT.value,
            OperationType.GIT_TAG.value,
            OperationType.GIT_PUSH.value,
            OperationType.CREATE_RELEASE.value,
            OperationType.ROLLBACK_RELEASE.value,
            OperationType.COLLECT_TEST_ARTIFACTS.value,
            OperationType.INSPECT_LOGS.value,
            OperationType.INSPECT_PROCESSES.value,
            OperationType.VERIFY_DELIVERABLE.value,
            OperationType.VERIFY_TESTS.value,
            OperationType.VERIFY_DIFF.value,
            OperationType.VERIFY_PROVENANCE.value,
            OperationType.UPDATE_VERSION.value,
            OperationType.UPDATE_CHANGELOG.value,
            OperationType.GENERATE_RELEASE_NOTES.value,
            OperationType.VALIDATE_RELEASE_METADATA.value,
            OperationType.PREPARE_RELEASE.value,
        }
        return AuthorizationPolicy.permit(f"policy-{norm}", ops)

    # Fallback default: universal base set
    return AuthorizationPolicy.permit(f"policy-{norm}", base_set)
