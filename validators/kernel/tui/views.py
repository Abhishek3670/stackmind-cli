"""Dependency-free terminal renderers. They only render runtime-provided data."""

from __future__ import annotations

from typing import Any, Mapping


def session_header(session: Mapping[str, Any]) -> str:
    return f"Session {session['session_id']} | {session['state']} | provider: {session['provider']}"


def contract_panel(contract: Mapping[str, Any]) -> str:
    return (
        "Allowed: "
        + ", ".join(contract.get("allow", []))
        + "\nDenied: "
        + ", ".join(contract.get("deny", []))
    )


def activity_line(event: Mapping[str, Any]) -> str:
    marker = (
        "✗ Blocked" if "cancelled" in event["name"] or "denied" in event["name"] else "✓ Allowed"
    )
    return f"{marker} {event['name']}: {event.get('payload', {})}"


def verification_matrix(dimensions: Mapping[str, bool]) -> str:
    labels = ("Scope", "State", "AST", "Behavioral", "Security", "Outcome")
    return " | ".join(
        f"{label}: {'PASS' if dimensions.get(label.lower(), False) else 'FAIL'}" for label in labels
    )


def diff_viewer(diff: str) -> str:
    return diff


def hitl_prompt(operation: str, target: str) -> str:
    return f"Approval Required\nOperation: {operation}\nPath: {target}\n[Approve] [Reject] [Inspect Diff]"
