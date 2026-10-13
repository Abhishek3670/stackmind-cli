"""Provider adapters, gateway, and native communication models."""

from .adapter import (
    AnthropicAdapter,
    GeminiAdapter,
    OllamaAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
)
from .errors import (
    AuthenticationError,
    BudgetExceededError,
    ConsecutiveToolFailureError,
    ContextLengthExceededError,
    NoProgressLoopError,
    OperationCancelledError,
    ProviderError,
    RateLimitError,
    TimeoutError,
    ToolLimitExceededError,
    ToolLoopExhaustedError,
)
from .gateway import STANDARD_KERNEL_TOOLS, ProviderGateway, get_tools_for_role
from .models import (
    Message,
    MessageRole,
    ProviderResponse,
    StreamChunk,
    TokenUsage,
    ToolCallRequest,
    ToolDefinition,
)

__all__ = [
    "AnthropicAdapter",
    "AuthenticationError",
    "BudgetExceededError",
    "ConsecutiveToolFailureError",
    "ContextLengthExceededError",
    "GeminiAdapter",
    "Message",
    "MessageRole",
    "NoProgressLoopError",
    "OllamaAdapter",
    "OpenAICompatibleAdapter",
    "OperationCancelledError",
    "ProviderAdapter",
    "ProviderError",
    "ProviderGateway",
    "ProviderResponse",
    "RateLimitError",
    "STANDARD_KERNEL_TOOLS",
    "StreamChunk",
    "TimeoutError",
    "TokenUsage",
    "ToolCallRequest",
    "ToolDefinition",
    "ToolLimitExceededError",
    "ToolLoopExhaustedError",
    "get_tools_for_role",
]
