"""HTTP/JSON-RPC client for the StackMind daemon; it has no execution privileges."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


class DaemonClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self._request_id = 0
        self._opener = build_opener(ProxyHandler({}))

    def call(self, method: str, **params: Any) -> Any:
        self._request_id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        ).encode()
        request = Request(
            f"{self.url}/rpc", data=payload, headers={"Content-Type": "application/json"}
        )
        with self._opener.open(request) as response:
            body = json.loads(response.read())
        if "error" in body:
            raise RuntimeError(body["error"]["message"])
        return body["result"]

    def create_session(self, **params: Any) -> dict[str, Any]:
        return self.call("session.create", **params)

    def health_version(self, protocol_version: int | None = None) -> dict[str, Any]:
        params = {} if protocol_version is None else {"protocol_version": protocol_version}
        return self.call("health.version", **params)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.call("session.get", session_id=session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.call("session.list")

    def pause(self, session_id: str) -> dict[str, Any]:
        return self.call("session.pause", session_id=session_id)

    def resume(self, session_id: str) -> dict[str, Any]:
        return self.call("session.resume", session_id=session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self.call("session.cancel", session_id=session_id)

    def session_history(self, session_id: str) -> list[dict[str, Any]]:
        return self.call("session.history", session_id=session_id)

    def session_close(self, session_id: str) -> dict[str, Any]:
        return self.call("session.close", session_id=session_id)

    def operation_begin(self, session_id: str, operation: str, **params: Any) -> dict[str, Any]:
        return self.call("operation.begin", session_id=session_id, operation=operation, **params)

    def operation_get(self, operation_id: str) -> dict[str, Any]:
        return self.call("operation.get", operation_id=operation_id)

    def operation_children(self, operation_id: str) -> list[dict[str, Any]]:
        return self.call("operation.children", operation_id=operation_id)

    def operation_cancel(self, operation_id: str, cascade: bool = True) -> dict[str, Any]:
        return self.call("operation.cancel", operation_id=operation_id, cascade=cascade)

    def turn(self, session_id: str, prompt: str, **params: Any) -> dict[str, Any]:
        return self.call("session.turn", session_id=session_id, prompt=prompt, **params)

    def work_order_execute(self, session_id: str, work_order_id: str, prompt: str | None = None, **params: Any) -> dict[str, Any]:
        p = prompt or f"Execute work order {work_order_id}"
        return self.call("work_order.execute", session_id=session_id, work_order_id=work_order_id, prompt=p, **params)

    def plan_propose(
        self,
        session_id: str,
        plan_id: str,
        title: str,
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "plan.propose",
            session_id=session_id,
            plan_id=plan_id,
            title=title,
            content=content,
            metadata=metadata or {},
        )

    def plan_get(self, session_id: str, plan_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"session_id": session_id}
        if plan_id is not None:
            params["plan_id"] = plan_id
        return self.call("plan.get", **params)

    def plan_approve(self, session_id: str, plan_id: str, reason: str = "") -> Any:
        return self.call("plan.approve", session_id=session_id, plan_id=plan_id, reason=reason)

    def plan_reject(self, session_id: str, plan_id: str, reason: str = "") -> dict[str, Any]:
        return self.call("plan.reject", session_id=session_id, plan_id=plan_id, reason=reason)

    def approve(self, session_id: str, approved: bool, reason: str = "") -> dict[str, Any]:
        return self.call(
            "session.approval", session_id=session_id, approved=approved, reason=reason
        )

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.call("event.list", session_id=session_id, after=after)

    def stream_events(
        self, session_id: str | None = None, after: int = 0
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"after": after}
        if session_id is not None:
            params["session_id"] = session_id
        request = Request(f"{self.url}/events?{urlencode(params)}")
        with self._opener.open(request) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    def list_backends(self) -> list[dict[str, Any]]:
        result = self.call("backend.list")
        if isinstance(result, dict) and "backends" in result:
            return result["backends"]
        return result

    def list_roles(self) -> list[dict[str, Any]]:
        result = self.call("role.list")
        if isinstance(result, dict) and "roles" in result:
            return result["roles"]
        return result

    def configure_role_backend(
        self,
        role: str,
        backend: str,
        model: str | None = None,
        credential_ref: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"role": role, "backend": backend}
        if model is not None:
            params["model"] = model
        if credential_ref is not None:
            params["credentialRef"] = credential_ref
        return self.call("role.configureBackend", **params)

    def list_agents(
        self, session_id: str | None = None, operation_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if session_id is not None:
            params["sessionId"] = session_id
        if operation_id is not None:
            params["operationId"] = operation_id
        result = self.call("agent.list", **params)
        if isinstance(result, dict) and "agents" in result:
            return result["agents"]
        return result

    def cancel_agent(
        self,
        agent_id: str,
        session_id: str | None = None,
        reason: str = "user_cancelled",
        cascade: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "agentId": agent_id,
            "reason": reason,
            "cascade": cascade,
        }
        if session_id is not None:
            params["sessionId"] = session_id
        return self.call("agent.cancel", **params)

    def inspect_agent(
        self,
        agent_id: str,
        session_id: str | None = None,
        after: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "agentId": agent_id,
            "after": after,
        }
        if session_id is not None:
            params["sessionId"] = session_id
        return self.call("agent.inspect", **params)

