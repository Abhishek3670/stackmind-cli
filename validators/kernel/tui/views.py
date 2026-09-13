"""Dependency-free terminal renderers. They only render runtime-provided data."""

from __future__ import annotations

from typing import Any, Mapping


def session_header(session: Mapping[str, Any]) -> str:
    agent_info = f" | agent: {session['agent']}" if "agent" in session else ""
    ws_info = f" | workspace: {session['workspace']}" if "workspace" in session else ""
    return f"Session {session['session_id']} | {session['state']} | provider: {session['provider']}{agent_info}{ws_info}"


def contract_panel(contract: Mapping[str, Any]) -> str:
    mode_str = f"Write Mode: {contract['write_mode']}\n" if "write_mode" in contract else ""
    return (
        mode_str
        + "Allowed: "
        + ", ".join(contract.get("allow", []))
        + "\nDenied: "
        + ", ".join(contract.get("deny", []))
    )


def activity_line(event: Mapping[str, Any]) -> str:
    name = str(event.get("name", ""))
    payload = event.get("payload", {})
    status = payload.get("status") if isinstance(payload, Mapping) else None
    if "cancelled" in name or status == "cancelled":
        marker = "✗ Cancelled"
    elif "denied" in name or status == "blocked" or status == "denied":
        marker = "✗ Blocked"
    elif status == "failure":
        marker = "✗ Failed"
    else:
        marker = "✓ Allowed"
    return f"{marker} {name}: {payload}"


def verification_matrix(dimensions: Mapping[str, bool]) -> str:
    labels = ("Scope", "State", "AST", "Behavioral", "Security", "Outcome")
    return " | ".join(
        f"{label}: {'PASS' if dimensions.get(label.lower(), False) else 'FAIL'}" for label in labels
    )


def diff_viewer(diff: str) -> str:
    return diff


def hitl_prompt(operation: str, target: str) -> str:
    return (
        f"Approval Required\nOperation: {operation}\nPath: {target}\n"
        "[Approve] [Reject] [Inspect Diff]"
    )
