"""A :class:`DatabaseAutonomyEngine` with its database collaborators left unset.

``_decide``, ``_assemble``, ``_apply_floors`` and ``resolve_mode`` touch only the settings and
the clock, so the pure decision logic can be tested without a database. Anything that would
actually use a collaborator raises ``AttributeError`` on ``None``, which is the behaviour we
want: a test that strays into I/O fails loudly instead of silently talking to a database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from ken_contracts.autonomy import AutonomyPolicy
from ken_core.config import Settings
from ken_services.autonomy.engine import DatabaseAutonomyEngine
from ken_services.autonomy.policy import load_autonomy_policy
from tests.conftest import make_settings
from tests.unit.autonomy._support import NOW


def pure_engine(
    settings: Settings | None = None,
    *,
    now: datetime = NOW,
    autonomy_policy: AutonomyPolicy | None = None,
) -> Any:
    """``autonomy_policy`` defaults to the RELEASED policy file, so these tests exercise the
    numbers that actually ship rather than a copy of them."""
    return DatabaseAutonomyEngine(
        sessionmaker=cast(Any, None),
        settings=settings or make_settings(),
        policy=cast(Any, None),
        taxonomy=cast(Any, None),
        autonomy_policy=autonomy_policy or load_autonomy_policy(),
        blocks=cast(Any, None),
        risk=cast(Any, None),
        calibration=cast(Any, None),
        clock=lambda: now,
    )
