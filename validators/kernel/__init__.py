"""Authoritative, provider-neutral runtime primitives for StackMind."""

from .boundary import RuntimeBoundary
from .contract import AgentContract, ContractEvaluator, ContractNormalizer
from .daemon import (
    DaemonStorage,
    EventDispatcher,
    JsonRpcProtocol,
    LocalDaemon,
    RuntimeEvent,
    SessionManager,
)
from .evidence import (
    AuthenticEvidenceTracer,
    AuthenticObservation,
    EligibilityDecision,
    ExperienceEligibilityGate,
    FileDiffSnapshot,
    derive_verification_dimensions,
)
from .identity import AgentIdentity, AuthorizationPolicy, HumanIdentity, ProviderIdentity
from .mcp import GovernedToolRegistry, McpProtocol, McpServer, OperatingMode, OperatingModeTracker
from .operations import OperationJournal, OperationRecord, OperationRequest, OperationType
from .providers import (
    STANDARD_KERNEL_TOOLS,
    AuthenticationError,
    BudgetExceededError,
    ContextLengthExceededError,
    Message,
    MessageRole,
    OpenAICompatibleAdapter,
    ProviderAdapter,
    ProviderError,
    ProviderGateway,
    ProviderResponse,
    RateLimitError,
    StreamChunk,
    TimeoutError,
    TokenUsage,
    ToolCallRequest,
    ToolDefinition,
)
from .session import AgentSession, Attempt, LifecycleState
from .tools import ToolGateway
from .verification import SandboxCanaryVerifier
from .workspace import ScratchWorkspace, WorkspaceEscapeError

__all__ = [
    "AgentContract",
    "AgentIdentity",
    "AgentSession",
    "Attempt",
    "AuthenticationError",
    "AuthenticEvidenceTracer",
    "AuthenticObservation",
    "AuthorizationPolicy",
    "BudgetExceededError",
    "ContextLengthExceededError",
    "ContractEvaluator",
    "ContractNormalizer",
    "DaemonStorage",
    "EligibilityDecision",
    "EventDispatcher",
    "GovernedToolRegistry",
    "ExperienceEligibilityGate",
    "FileDiffSnapshot",
    "HumanIdentity",
    "LifecycleState",
    "JsonRpcProtocol",
    "LocalDaemon",
    "Message",
    "MessageRole",
    "OpenAICompatibleAdapter",
    "McpProtocol",
    "McpServer",
    "OperationJournal",
    "OperationRecord",
    "OperationRequest",
    "OperationType",
    "OperatingMode",
    "OperatingModeTracker",
    "ProviderAdapter",
    "ProviderError",
    "ProviderGateway",
    "ProviderIdentity",
    "ProviderResponse",
    "RateLimitError",
    "RuntimeBoundary",
    "STANDARD_KERNEL_TOOLS",
    "RuntimeEvent",
    "SandboxCanaryVerifier",
    "ScratchWorkspace",
    "SessionManager",
    "StreamChunk",
    "TimeoutError",
    "TokenUsage",
    "ToolCallRequest",
    "ToolDefinition",
    "ToolGateway",
    "WorkspaceEscapeError",
    "derive_verification_dimensions",
]
