"""Frozen shared contracts for the Ken Research Agentic Interlinking OS.

Everything in this package is a FROZEN contract (Phase 0). Other packages
import from here; nothing here imports from them. A change to any module in
this package must be proposed to the lead orchestrator before it is made --
see MULTI_AGENT_ORCHESTRATION.md, "Shared Contracts".

Design rules that keep these contracts stable across Phases 1-6:

1. Provider protocols are async-only.
2. Every protocol method takes keyword-only arguments, so a new parameter
   never shifts an existing position.
3. Every return value is a Pydantic model, so a new optional field is never
   a breaking change.
4. Failures are typed exceptions, not sentinel return values.
"""

from ken_contracts.common import (
    AsyncClosable,
    ContractModel,
    FrozenModel,
    HealthStatus,
    ServiceIdentity,
)

__all__ = [
    "AsyncClosable",
    "ContractModel",
    "FrozenModel",
    "HealthStatus",
    "ServiceIdentity",
]
