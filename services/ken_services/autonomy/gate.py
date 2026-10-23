"""THE publishing gate: the one function that decides whether a real CMS write may happen.

ADR-0018 s2 keeps `autonomous` (and `guarded`) from being selected by accident, and keeps
them from running against a live site without a second, separate acknowledgement. That used
to live in three places that could drift apart -- the `Settings` startup validator, the
engine's `publishing_enabled()` and the API reader's copy of it (which disagreed with the
engine about a disabled-writes CMS). This module is now the single source of truth for the
RUNTIME gate. The startup validator stays, as the first line of defence at boot, but it can
be bypassed (`Settings.model_construct`, a test, a future code path), which is exactly why the
publisher asks THIS function again immediately before any CMS call: defence in depth.

A leaf module on purpose: it imports only contracts and settings, so the engine, the reader,
the publisher, the job and the CLI can all depend on it without a layering violation. Pure:
no I/O, no clock, no model.
"""

from __future__ import annotations

from enum import StrEnum

from ken_contracts.autonomy import AutonomyError, AutonomyMode
from ken_core.config import Settings

__all__ = [
    "PublishingNotEnabledError",
    "PublishingRefusal",
    "publishing_enabled",
    "publishing_refusal",
    "require_publishing",
]


class PublishingRefusal(StrEnum):
    """Why a mode may not write to the CMS right now. Stable codes, never prose."""

    SHADOW_MODE = "shadow_mode"
    """`shadow` records a counterfactual and publishes nothing, by definition."""
    CMS_WRITES_DISABLED = "cms_writes_disabled"
    """The `http` provider has writes switched off: any apply would be refused by the CMS
    anyway, so the publisher declines before attempting one."""
    PRODUCTION_NOT_ALLOWED = "production_not_allowed"
    """A write-enabled `http` CMS is the real site; publishing without a human against it
    needs `KEN_AUTONOMY_ALLOW_PRODUCTION=true` as well."""
    AUTONOMOUS_NOT_ACKNOWLEDGED = "autonomous_not_acknowledged"
    """`autonomous` needs `KEN_AUTONOMY_AUTONOMOUS_ACKNOWLEDGED=true`."""


class PublishingNotEnabledError(AutonomyError):
    """The configuration does not permit a publish in this mode. Raised BEFORE any CMS call."""

    def __init__(self, refusal: PublishingRefusal) -> None:
        self.refusal = refusal
        super().__init__(f"publishing is not enabled: {refusal.value}")


def publishing_refusal(settings: Settings, mode: AutonomyMode) -> PublishingRefusal | None:
    """Why ``mode`` may not publish under ``settings``, or ``None`` when it may.

    Checks run from the cheapest, most fundamental refusal to the most specific, and every
    one of them is independent: passing one never relaxes another. ``mock`` and ``simulated``
    providers are project-local, disposable CMSs and are not "production", whatever
    ``cms_writes_enabled`` says (that flag gates only the `http` provider).
    """
    if mode is AutonomyMode.SHADOW:
        return PublishingRefusal.SHADOW_MODE
    if settings.cms_provider == "http":
        if not settings.cms_writes_enabled:
            return PublishingRefusal.CMS_WRITES_DISABLED
        if not settings.autonomy_allow_production:
            return PublishingRefusal.PRODUCTION_NOT_ALLOWED
    if mode is AutonomyMode.AUTONOMOUS and not settings.autonomy_autonomous_acknowledged:
        return PublishingRefusal.AUTONOMOUS_NOT_ACKNOWLEDGED
    return None


def publishing_enabled(settings: Settings, mode: AutonomyMode | None = None) -> bool:
    """Whether a real CMS write may happen in ``mode`` (default: the configured mode)."""
    effective = AutonomyMode(settings.autonomy_mode) if mode is None else mode
    return publishing_refusal(settings, effective) is None


def require_publishing(settings: Settings, mode: AutonomyMode) -> None:
    """Raise :class:`PublishingNotEnabledError` unless ``mode`` may publish under ``settings``.

    The publisher calls this before it evaluates, records or sends anything, and again
    immediately before the deployment service is reached.
    """
    refusal = publishing_refusal(settings, mode)
    if refusal is not None:
        raise PublishingNotEnabledError(refusal)
