"""Tests that local model providers (ollama, custom-localhost) bypass token budget enforcement.

Failing-first: 'custom' provider pointing to localhost triggers BudgetExceededError
because _get_max_tokens() only matches exact provider_name in ("ollama", "local", "llamacpp").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from validators.kernel.providers.adapter import (
    OpenAICompatibleAdapter,
    OllamaAdapter,
)
from validators.kernel.providers.gateway import ProviderGateway


def _make_gateway_minimal(adapter, max_tokens=200):
    """Create a ProviderGateway with a mock tool_gateway and a budget contract."""
    contract = SimpleNamespace(
        budget={"max_tokens": max_tokens, "max_files_touched": 10},
    )
    # ProviderGateway only needs adapter + tool_gateway (mock is fine for unit tests).
    # We pass tools=[] to avoid STANDARD_KERNEL_TOOLS resolution.
    mock_tool_gw = SimpleNamespace()
    gw = ProviderGateway(
        adapter=adapter,
        tool_gateway=mock_tool_gw,
        contract=contract,
        tools=[],
    )
    return gw


class TestLocalModelBudgetBypass:
    """Token budget must be bypassed for local/self-hosted models."""

    def test_ollama_adapter_bypasses_budget(self):
        """OllamaAdapter (provider_name='ollama') -> _get_max_tokens() returns None."""
        adapter = OllamaAdapter(
            endpoint="http://localhost:11434",
            model="gemma4:31b-cloud",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() is None, (
            "OllamaAdapter should bypass budget (return None)"
        )

    def test_openai_compat_with_provider_name_ollama_bypasses(self):
        """OpenAICompatibleAdapter with provider_name='ollama' -> bypass."""
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:11434/v1",
            model="gemma4:31b-cloud",
            provider_name="ollama",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() is None

    def test_custom_provider_localhost_bypasses_budget(self):
        """'custom' provider pointing to localhost -> should bypass budget.

        This is the exact scenario from the user's settings.json where
        architecture uses provider='custom' with base_url='http://localhost:20128/v1'.
        """
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:20128/v1",
            api_key="sk-test",
            model="openrouter/cohere/north-mini-code:free",
            provider_name="custom",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() is None, (
            "Custom provider on localhost should bypass budget"
        )

    def test_custom_provider_127_bypasses_budget(self):
        """'custom' provider on 127.0.0.1 -> should bypass budget."""
        adapter = OpenAICompatibleAdapter(
            base_url="http://127.0.0.1:20128/v1",
            api_key="sk-test",
            model="some-local-model",
            provider_name="custom",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() is None

    def test_openai_cloud_still_enforces_budget(self):
        """Cloud OpenAI provider must still enforce budget limits."""
        adapter = OpenAICompatibleAdapter(
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model="gpt-4o",
            provider_name="openai",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() == 200, (
            "Cloud OpenAI provider must enforce budget"
        )

    def test_openrouter_cloud_still_enforces_budget(self):
        """Cloud OpenRouter provider must still enforce budget limits."""
        adapter = OpenAICompatibleAdapter(
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-test",
            model="anthropic/claude-3.7-sonnet",
            provider_name="openrouter",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() == 200

    def test_llamacpp_bypasses_budget(self):
        """llamacpp provider -> bypass."""
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:8080/v1",
            model="local-model",
            provider_name="llamacpp",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() is None

    def test_ollama_port_11434_bypasses_even_wrong_provider_name(self):
        """Adapter with ollama port (11434) but generic provider_name -> bypass."""
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:11434/v1",
            model="gemma4:31b-cloud",
            provider_name="openai-compatible",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=200)
        assert gw._get_max_tokens() is None, (
            "Ollama endpoint (port 11434) should bypass budget even with generic provider name"
        )

    def test_anthropic_cloud_enforces_budget(self):
        """Anthropic cloud endpoint must enforce budget."""
        adapter = OpenAICompatibleAdapter(
            base_url="https://api.anthropic.com/v1",
            api_key="sk-test",
            model="claude-3-7-sonnet-20250219",
            provider_name="anthropic",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=500)
        assert gw._get_max_tokens() == 500

    def test_zero_budget_returns_none(self):
        """max_tokens=0 in contract means no limit (existing behavior)."""
        adapter = OpenAICompatibleAdapter(
            base_url="https://api.openai.com/v1",
            model="gpt-4o",
            provider_name="openai",
        )
        gw = _make_gateway_minimal(adapter, max_tokens=0)
        assert gw._get_max_tokens() is None
