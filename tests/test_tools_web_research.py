"""Tests for Web Research tools (web_search, web_fetch) in StackMind kernel and gateways."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from validators.kernel import (
    AgentSession,
    ContractNormalizer,
    OperationJournal,
    RuntimeBoundary,
    ScratchWorkspace,
    ToolGateway,
)
from validators.kernel.identity import get_role_policy
from validators.kernel.providers.gateway import (
    PLANNING_CORE_TOOL_NAMES,
    ARCHITECTURE_INVESTIGATION_TOOL_NAMES,
    ProviderGateway,
    get_curated_tools_for_phase,
)
from validators.kernel.web import (
    perform_web_fetch,
    perform_web_search,
    search_brave,
    search_duckduckgo_lite,
    search_serpapi,
    search_tavily,
)


def _setup_gateway(tmp_path: Path, agent: str = "claude"):
    contract = ContractNormalizer.normalize({
        "agent": agent,
        "wo": "WO-001",
        "contract_version": 3,
        "scope": {
            "allowed": ["**"],
            "denied": [],
            "write": "read-write",
        },
        "budget": {
            "max_tokens": 10000,
            "max_files": 15,
        },
    })
    session = AgentSession(agent, "test-provider", str(tmp_path), session_id="s1")
    attempt = session.create_attempt(contract, attempt_id="a1")
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    policy = get_role_policy(agent)
    workspace = ScratchWorkspace(authoritative_root=tmp_path, root=tmp_path, attempt_id="a1")
    gateway = ToolGateway(
        boundary=boundary,
        policy=policy,
        contract=attempt.contract,
        workspace=workspace,
        sandbox=None,
        session_id="s1",
        attempt_id="a1",
        actor_id=agent,
        provider_id="test-provider",
    )
    return gateway, journal


def test_duckduckgo_lite_html_parsing():
    sample_html = """
    <!DOCTYPE html>
    <html><body>
    <table>
      <tr>
        <td>
          <a rel="nofollow" href="https://duckduckgo.com/l/?kh=-1&uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html" class='result-link'>asyncio &mdash; Asynchronous I/O</a>
        </td>
      </tr>
      <tr>
        <td class='result-snippet'>
          asyncio is a library to write <b>concurrent</b> code using the async/await syntax.
        </td>
      </tr>
      <tr>
        <td>
          <a rel="nofollow" href="https://realpython.com/async-io-python/" class='result-link'>Async IO in Python: A Complete Walkthrough</a>
        </td>
      </tr>
      <tr>
        <td class='result-snippet'>
          An in-depth introduction to the asyncio library and async/await in Python.
        </td>
      </tr>
    </table>
    </body></html>
    """

    mock_resp = MagicMock()
    mock_resp.read.return_value = sample_html.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        results = search_duckduckgo_lite("python asyncio", limit=2)

    assert len(results) == 2
    assert results[0]["title"] == "asyncio — Asynchronous I/O"
    assert results[0]["url"] == "https://docs.python.org/3/library/asyncio.html"
    assert "asyncio is a library to write concurrent code" in results[0]["snippet"]

    assert results[1]["title"] == "Async IO in Python: A Complete Walkthrough"
    assert results[1]["url"] == "https://realpython.com/async-io-python/"


def test_search_tavily_api():
    tavily_payload = {
        "results": [
            {
                "title": "FastAPI Framework",
                "url": "https://fastapi.tiangolo.com/",
                "content": "FastAPI is a modern, fast web framework for building APIs with Python.",
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(tavily_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        results = search_tavily("fastapi", api_key="tvly-test", limit=1)

    assert len(results) == 1
    assert results[0]["title"] == "FastAPI Framework"
    assert results[0]["url"] == "https://fastapi.tiangolo.com/"
    assert "modern, fast web framework" in results[0]["snippet"]


def test_search_serpapi():
    serpapi_payload = {
        "organic_results": [
            {
                "title": "Django Documentation",
                "link": "https://docs.djangoproject.com/",
                "snippet": "Django documentation: Everything you need to know about Django.",
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(serpapi_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        results = search_serpapi("django", api_key="serp-test", limit=1)

    assert len(results) == 1
    assert results[0]["title"] == "Django Documentation"
    assert results[0]["url"] == "https://docs.djangoproject.com/"


def test_search_brave():
    brave_payload = {
        "web": {
            "results": [
                {
                    "title": "PostgreSQL Official",
                    "url": "https://www.postgresql.org/",
                    "description": "PostgreSQL: The World's Most Advanced Open Source Relational Database.",
                }
            ]
        }
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(brave_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        results = search_brave("postgres", api_key="brave-test", limit=1)

    assert len(results) == 1
    assert results[0]["title"] == "PostgreSQL Official"
    assert results[0]["url"] == "https://www.postgresql.org/"


def test_perform_web_search_error_handling():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection timed out")):
        results = perform_web_search("test query")

    assert len(results) == 1
    assert results[0]["title"] == "Search Unavailable"
    assert "Connection timed out" in results[0]["snippet"]


def test_perform_web_fetch():
    sample_html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Python Typing Guide</title>
        <script>var x = 1;</script>
        <style>body { font-size: 12px; }</style>
      </head>
      <body>
        <h1>Type Hints Cheat Sheet</h1>
        <p>Python 3.12 introduces new type parameter syntax.</p>
        <div>Use typing.Optional for optional values.</div>
      </body>
    </html>
    """

    mock_resp = MagicMock()
    mock_resp.read.return_value = sample_html.encode("utf-8")
    mock_resp.headers.get_content_type.return_value = "text/html"
    mock_resp.headers.get_content_charset.return_value = "utf-8"
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        text = perform_web_fetch("https://example.com/types")

    assert "# Python Typing Guide" in text
    assert "Type Hints Cheat Sheet" in text
    assert "Python 3.12 introduces new type parameter syntax." in text
    assert "var x = 1" not in text
    assert "font-size" not in text


def test_tool_gateway_web_search_and_fetch(tmp_path: Path):
    gateway, journal = _setup_gateway(tmp_path, "claude")

    mock_results = [{"title": "Redis Doc", "url": "https://redis.io", "snippet": "Redis in-memory store"}]
    with patch("validators.kernel.web.perform_web_search", return_value=mock_results):
        res = gateway.web_search("redis", limit=1)

    assert res == mock_results
    assert len(journal.records) == 1
    assert journal.records[0].request.operation_type.value == "web_search"

    with patch("validators.kernel.web.perform_web_fetch", return_value="# Redis Quickstart\nRun redis-server"):
        doc = gateway.web_fetch("https://redis.io/docs")

    assert "# Redis Quickstart" in doc
    assert len(journal.records) == 2
    assert journal.records[1].request.operation_type.value == "web_fetch"


def test_planning_phase_and_request_tools_web_integration(tmp_path: Path):
    # 1. Verify planning phase includes web_search and web_fetch
    assert "web_search" in PLANNING_CORE_TOOL_NAMES
    assert "web_fetch" in PLANNING_CORE_TOOL_NAMES
    assert "web_search" in ARCHITECTURE_INVESTIGATION_TOOL_NAMES
    assert "web_fetch" in ARCHITECTURE_INVESTIGATION_TOOL_NAMES

    curated = get_curated_tools_for_phase("claude", phase="planning")
    curated_names = {t.name for t in curated}
    assert "web_search" in curated_names
    assert "web_fetch" in curated_names

    # 2. Verify request_tools activates web tools
    gateway, _ = _setup_gateway(tmp_path, "codex")
    provider_gateway = ProviderGateway(
        adapter=MagicMock(),
        tool_gateway=gateway,
        tools=get_curated_tools_for_phase("codex", phase="implementation"),
    )

    # Initially codex has worker code tools
    initial_names = {t.name for t in provider_gateway.active_tools_list}
    assert "web_search" not in initial_names

    # Activate via request_tools(query='web')
    activate_msg = provider_gateway.execute_tool_call("request_tools", {"query": "web"})
    assert "Successfully activated tool(s)" in activate_msg
    assert "web_search" in activate_msg
    assert "web_fetch" in activate_msg

    updated_names = {t.name for t in provider_gateway.active_tools_list}
    assert "web_search" in updated_names
    assert "web_fetch" in updated_names

    # 3. Verify parameter alias resolution (e.g. q -> query, max_results -> limit)
    with patch("validators.kernel.web.perform_web_search", return_value=[{"title": "Ok", "url": "u", "snippet": "s"}]) as mock_search:
        provider_gateway.execute_tool_call("web_search", {"q": "search term", "max_results": 7})
        mock_search.assert_called_once_with("search term", limit=7)
