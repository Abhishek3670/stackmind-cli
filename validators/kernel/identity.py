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

    @classmethod
    def permit(cls, policy_id: str, operations: Iterable[str], authorized_by: HumanIdentity | None = None):
        return cls(policy_id, frozenset(operations), authorized_by)

    def permits(self, operation_type: str) -> bool:
        return operation_type in self.permitted_operations
