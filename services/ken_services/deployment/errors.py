"""Service-side refusals beyond the frozen ``ken_contracts.deployment`` errors.

All map onto the frozen classes (``DeploymentRefusedError`` -> 409 ``conflict``), so the API
needs no extra vocabulary; ``DeploymentPermissionError`` is the 403 defence in depth.
"""

from __future__ import annotations

from ken_contracts.deployment import DeploymentRefusedError

__all__ = [
    "DeploymentPermissionError",
    "DeploymentStateError",
    "OperationReusedDeploymentError",
]


class DeploymentPermissionError(PermissionError):
    """The principal lacks ``Permission.DEPLOY`` or is not a ``user:`` actor (403)."""


class OperationReusedDeploymentError(DeploymentRefusedError):
    """The ``operation_id`` was already used for a DIFFERENT request (409, no issues)."""


class DeploymentStateError(DeploymentRefusedError):
    """The deployment's status does not allow the action (e.g. verifying a failed one)."""
