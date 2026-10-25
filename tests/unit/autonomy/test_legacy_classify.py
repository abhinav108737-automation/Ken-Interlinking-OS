"""The pure classification rule for plans stranded in ``awaiting_approval``."""

from __future__ import annotations

import pytest

from ken_contracts.autonomy import BlockCode, CriticOutcome
from ken_services.autonomy.legacy import LegacyClass, classify

B = BlockCode
C = LegacyClass


@pytest.mark.parametrize(
    ("blocks", "critic", "duplicate", "expected"),
    [
        ((), CriticOutcome.ACCEPT, False, C.ELIGIBLE),
        ((B.STALE_SOURCE_VERSION,), CriticOutcome.ACCEPT, False, C.STALE),
        # freshness wins over every other finding, including a duplicate
        ((B.STALE_SOURCE_VERSION, B.PROTECTED_PAGE), CriticOutcome.ACCEPT, True, C.STALE),
        ((), CriticOutcome.ACCEPT, True, C.DUPLICATE),
        ((B.PROTECTED_PAGE,), CriticOutcome.ACCEPT, False, C.PROTECTED),
        ((B.PROTECTED_PAGE, B.BROKEN_TARGET), CriticOutcome.ACCEPT, False, C.PROTECTED),
        ((B.BROKEN_TARGET,), CriticOutcome.ACCEPT, False, C.INVALID),
        ((B.UNPUBLISHED_DESTINATION,), CriticOutcome.ACCEPT, False, C.INVALID),
        ((B.SELF_LINK,), CriticOutcome.ACCEPT, False, C.INVALID),
        # a critic that did not ACCEPT is never "eligible" (fail closed)
        ((), CriticOutcome.REJECT, False, C.CRITIC_NOT_ACCEPT),
        ((), CriticOutcome.NO_ACTION, False, C.CRITIC_NOT_ACCEPT),
    ],
)
def test_classification(
    blocks: tuple[BlockCode, ...], critic: CriticOutcome, duplicate: bool, expected: LegacyClass
) -> None:
    assert classify(blocks=blocks, critic=critic, older_open_duplicate=duplicate) is expected


def test_only_the_four_defect_classes_have_a_move() -> None:
    from ken_services.autonomy.legacy import _MOVES

    assert set(_MOVES) == {C.STALE, C.DUPLICATE, C.PROTECTED, C.INVALID}
    assert {m.value for m in _MOVES.values()} == {"expired", "blocked"}, (
        "never approves or publishes"
    )
