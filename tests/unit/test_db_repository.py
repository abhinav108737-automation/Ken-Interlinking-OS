"""Event repository argument validation (no database needed)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ken_contracts.events import EventEnvelope, EventName
from ken_core.db.repositories.events import MAX_LIST_LIMIT, append_event, list_events


class NoIOSession:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"session.{name} must not be reached")


@pytest.mark.parametrize("limit", [0, -1, MAX_LIST_LIMIT + 1])
async def test_list_events_rejects_out_of_range_limit_before_io(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await list_events(NoIOSession(), limit=limit)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Promotion governance must hold at the PERSISTENCE boundary (QA cleanup H1).
# Construction-time validation alone was bypassable by three routes; each would
# have written an ungoverned promotion that then breaks list_events forever.
# --------------------------------------------------------------------------- #
GOOD_PROMOTION: dict[str, Any] = {
    "artifact_kind": "skill",
    "name": "select_natural_anchor",
    "version": "2",
    "previous_version": "1",
    # Phase 8 (ADR-0016 15.7.4) TIGHTENED both: ``promoted_by`` must be ``user:<username>``
    # (before, ``agent:reflector@1`` validated despite the docstring claiming a human) and
    # ``evaluation_ref`` must be ``str(policy_evaluations.id)``.
    "promoted_by": "user:seo-manager",
    "evaluation_ref": "2b4e6a8c-1d3f-4a5b-8c7d-9e0f1a2b3c4d",
}


def _heartbeat() -> EventEnvelope:
    return EventEnvelope(event_name=EventName.WORKER_HEARTBEAT, emitted_by="ken-worker")


def _promotion() -> EventEnvelope:
    return EventEnvelope(
        event_name=EventName.SKILL_PROMOTED,
        emitted_by="ken-api",
        subject_type="skill",
        subject_id="select_natural_anchor",
        payload=dict(GOOD_PROMOTION),
    )


async def test_append_rejects_promotion_smuggled_in_via_model_copy() -> None:
    """model_copy(update=...) skips validation: append_event must re-validate."""
    smuggled = _heartbeat().model_copy(update={"event_name": EventName.POLICY_PROMOTED})
    assert smuggled.payload == {}  # proves the bypass exists at the model level
    with pytest.raises(ValidationError):
        await append_event(NoIOSession(), smuggled)  # type: ignore[arg-type]


async def test_append_rejects_promotion_payload_mutated_in_place() -> None:
    """An in-place dict mutation is invisible to the model; persistence catches it."""
    envelope = _promotion()
    envelope.payload["promoted_by"] = ""
    with pytest.raises(ValidationError):
        await append_event(NoIOSession(), envelope)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_name", EventName.SKILL_PROMOTED),
        ("subject_id", "other"),
        ("subject_type", "skill"),
        ("payload", {}),
    ],
)
def test_assignment_is_refused_and_leaves_the_envelope_unchanged(field: str, value: Any) -> None:
    """Frozen: a refused assignment must not have mutated the object (QA re-review)."""
    envelope = _heartbeat()
    before = envelope.model_dump()
    with pytest.raises(ValidationError):
        setattr(envelope, field, value)
    assert envelope.model_dump() == before


def test_promotion_payload_is_stored_normalised() -> None:
    """Whitespace is stripped in what is STORED, not only in what is checked."""
    envelope = EventEnvelope(
        event_name=EventName.SKILL_PROMOTED,
        emitted_by="ken-api",
        subject_type="skill",
        subject_id="select_natural_anchor",
        payload={
            **GOOD_PROMOTION,
            "name": "select_natural_anchor  ",
            "promoted_by": " user:bob ",
        },
    )
    assert envelope.payload["name"] == "select_natural_anchor"
    assert envelope.payload["promoted_by"] == "user:bob"
    assert envelope.payload["name"] == envelope.subject_id


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_whitespace_only_authoriser_is_not_an_authoriser(blank: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(
            event_name=EventName.SKILL_PROMOTED,
            emitted_by="ken-api",
            subject_type="skill",
            subject_id="select_natural_anchor",
            payload={**GOOD_PROMOTION, "promoted_by": blank},
        )


class RecordingSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []

    async def execute(self, statement: Any) -> None:
        self.executed.append(statement)


async def test_a_valid_promotion_is_still_appended() -> None:
    """The guard must not over-constrain: a governed promotion goes through."""
    session = RecordingSession()
    await append_event(session, _promotion())  # type: ignore[arg-type]
    assert len(session.executed) == 1
