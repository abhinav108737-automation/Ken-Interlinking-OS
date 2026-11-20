"""Deterministic reciprocal-rank fusion for shadow recommendation evaluation.

This module does not replace the governed scorer. It combines channel orderings so RRF can be
evaluated beside the current policy before any promotion decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    candidate_id: str
    score: float
    ranks: Mapping[str, int]


def reciprocal_rank_fusion(
    channels: Mapping[str, Sequence[str]], *, rank_constant: int = 60, limit: int | None = None
) -> tuple[FusedCandidate, ...]:
    """Fuse named ranked lists using ``sum(1 / (rank_constant + rank))``.

    Repeated candidate ids within one channel count only at their first rank. Channel names and
    candidate ids break ties, making the same input byte-for-byte deterministic.
    """
    if rank_constant < 1:
        raise ValueError("rank_constant must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    ranks: dict[str, dict[str, int]] = {}
    for channel in sorted(channels):
        seen: set[str] = set()
        for rank, candidate_id in enumerate(channels[channel], start=1):
            value = str(candidate_id).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            ranks.setdefault(value, {})[channel] = rank
    fused = [
        FusedCandidate(
            candidate_id=candidate_id,
            score=sum(1.0 / (rank_constant + rank) for rank in channel_ranks.values()),
            ranks=dict(sorted(channel_ranks.items())),
        )
        for candidate_id, channel_ranks in ranks.items()
    ]
    fused.sort(key=lambda item: (-item.score, item.candidate_id))
    return tuple(fused[:limit] if limit is not None else fused)


def precision_at_k(ranked: Sequence[str], relevant: set[str], *, k: int) -> float:
    if k < 1:
        raise ValueError("k must be at least 1")
    top = ranked[:k]
    return sum(candidate in relevant for candidate in top) / k


def recall_at_k(ranked: Sequence[str], relevant: set[str], *, k: int) -> float:
    if k < 1:
        raise ValueError("k must be at least 1")
    if not relevant:
        return 0.0
    return sum(candidate in relevant for candidate in ranked[:k]) / len(relevant)
