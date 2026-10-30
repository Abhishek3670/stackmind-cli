"""Governed agent execution loop for StackMind Phase 8.

Layout
------
``models``         dataclasses, output schema/validator, prompts, standalone helpers
``core``           AgentRunner, assembled from the mixins below
``execution``      run_once turn loop
``completion``     LLM request assembly / completion handling
``discovery``      next-task discovery and file loading
``decisions``      decision validation and tool-runtime wiring
``prompts``        prompt and review-notice rendering
``commands``       governed/sandboxed command execution
``staging``        staged-state validation and report rendering
``verification``   verification dimensions and verified diff
``reconciliation`` work-order reconciliation and index/tree sync
``workspace``      lock, operation application, release target

The names re-exported here preserve the historical module import surface.
"""

# ruff: noqa: F401

from .core import AgentRunner, HarnessRunner
from .models import (
    _OUTPUT_SCHEMA,
    _OUTPUT_VALIDATOR,
    HARNESS_SNAPSHOT_IGNORED,
    QA_SYSTEM_PROMPT,
    CompletionRecord,
    EchoLLMProvider,
    FileMove,
    FileWrite,
    GovernedToolRuntime,
    HarnessDecision,
    HarnessRunResult,
    HarnessTask,
    LLMProvider,
    LLMRequest,
    LoopSafetyError,
    ResourceReadTracker,
    _first_content_line,
    provider_stream_interruption_result,
)

__all__ = [
    'AgentRunner',
    'EchoLLMProvider',
    'HarnessRunResult',
    'HarnessRunner',
    'HarnessTask',
    'LLMProvider',
]
