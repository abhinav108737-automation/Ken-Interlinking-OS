"""Content provenance labels: which pages are synthetic and which came from authorised content.

A label is stamped into every imported page's metadata (``metadata["provenance"]``) at import time,
so no report has to guess. A page with NO label is reported as unlabelled and is never counted as
real content.
"""

from __future__ import annotations

from typing import Final

__all__ = ["AUTHORIZED", "LABELS", "SYNTHETIC_BASE", "SYNTHETIC_EXTENSION", "check_label"]

SYNTHETIC_BASE: Final = "synthetic_base"
SYNTHETIC_EXTENSION: Final = "synthetic_extension"
AUTHORIZED: Final = "authorized_import"
LABELS: Final = frozenset({SYNTHETIC_BASE, SYNTHETIC_EXTENSION, AUTHORIZED})


def check_label(label: str | None) -> str | None:
    if label is not None and label not in LABELS:
        raise ValueError(f"provenance must be one of {sorted(LABELS)}")
    return label
