"""doctor.py model-role rows: OK when pulled and fit, WARN with a pull hint otherwise."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from ken_core.config import Settings

DOCTOR = Path(__file__).resolve().parents[2] / "infrastructure" / "scripts" / "doctor.py"


@pytest.fixture(scope="module")
def doctor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ken_doctor_under_test", DOCTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules while the class is built.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        model_fast="qwen3:8b-q4_K_M",
        model_reasoning="qwen3:8b-q4_K_M",
        model_embedding="nomic-embed-text",
    )


def _rows(doctor: ModuleType, installed: dict[str, list[str] | None]) -> dict[str, tuple[str, str]]:
    return {r.check: (r.status, r.detail) for r in doctor.model_role_rows(_settings(), installed)}


def test_embedding_ok_when_pulled_under_latest_tag(doctor: ModuleType) -> None:
    rows = _rows(
        doctor,
        {
            "qwen3:8b-q4_K_M": ["completion", "tools", "thinking"],
            "nomic-embed-text:latest": ["embedding"],
        },
    )
    assert rows["model role embedding"] == ("OK", "nomic-embed-text")
    assert rows["model role fast"][0] == "OK"
    assert rows["model role reasoning"][0] == "OK"


def test_embedding_missing_is_warn_with_pull_hint(doctor: ModuleType) -> None:
    rows = _rows(doctor, {"qwen3:8b-q4_K_M": ["completion"]})
    status, detail = rows["model role embedding"]
    assert status == "WARN"
    assert "ollama pull nomic-embed-text" in detail
    assert "Phase 2" not in detail


def test_embedding_role_with_completion_model_is_warn(doctor: ModuleType) -> None:
    rows = _rows(doctor, {"qwen3:8b-q4_K_M": ["completion"], "nomic-embed-text": ["completion"]})
    status, detail = rows["model role embedding"]
    assert status == "WARN"
    assert "embedding" in detail


def test_unknown_capabilities_are_trusted(doctor: ModuleType) -> None:
    rows = _rows(doctor, {"qwen3:8b-q4_K_M": None, "nomic-embed-text:latest": None})
    assert {status for status, _ in rows.values()} == {"OK"}
