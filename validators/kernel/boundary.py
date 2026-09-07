"""The sole P0 provider-to-runtime operation boundary.

P0 deliberately records and authorizes requests but exposes no live-workspace
executor. Providers therefore cannot obtain mutation authority from this API.
"""

from __future__ import annotations

from .contract import AgentContract, ContractEvaluator
from .identity import AuthorizationPolicy
from .operations import OperationJournal, OperationRecord, OperationRequest


class RuntimeBoundary:
    def __init__(self, journal: OperationJournal, evaluator: ContractEvaluator | None = None) -> None:
        self.journal = journal
        self.evaluator = evaluator or ContractEvaluator()

    def submit(self, request: OperationRequest, contract: AgentContract,
               policy: AuthorizationPolicy) -> OperationRecord:
        if not policy.permits(request.operation_type.value):
            return self.journal.record(OperationRecord(request, False, "policy denies operation"))
        authorized, reason = self.evaluator.authorize(contract, request.operation_type.value, request.target)
        return self.journal.record(OperationRecord(request, authorized, reason))
