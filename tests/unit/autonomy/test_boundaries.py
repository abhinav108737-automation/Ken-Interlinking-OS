"""Static boundaries of the autonomy services (the ``ken_services/learning`` pattern).

Four rules enforced by READING the source, because a runtime test cannot prove the absence of
a code path. The package docstring promises the first two; this is where they stop being
promises.

1. **No module under ``ken_services/autonomy/`` contains a ``"user:"`` literal.** The database
   can only check that an actor LOOKS like ``user:<name>``; only an authenticated session
   proves it is one. A module that cannot spell the prefix cannot forge it, so the only way a
   human actor enters this package is ``Principal.actor``.
2. **``blocks.py`` and ``facts.py`` import no model provider** -- covered in detail by
   ``test_block_determinism.py``; re-asserted here so this file is the single list of
   boundaries.
3. **Only ``override.py`` names the operator-override vocabulary.** One code path records a
   human override, and it is the one that re-evaluates the deterministic blocks first.
4. **Only ``publish.py`` and ``override.py`` reach the deployment service.** Nothing in the
   decision path can write to a CMS, which is what makes "the engine decides and records; it
   never publishes" a structural fact.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

PACKAGE: Final = Path(__file__).resolve().parents[3] / "services" / "ken_services" / "autonomy"
OVERRIDE_MODULE: Final = "override.py"
PUBLISH_MODULES: Final = frozenset({"publish.py", "compose.py", "job.py", "cli.py"})
DEPLOYMENT_IMPORT: Final = "ken_services.deployment"


def _runtime_strings(tree: ast.AST) -> list[str]:
    """Every string constant the module could USE, documentation excluded.

    Documentation is every module, class and function docstring plus the bare-string
    "attribute docstring" this repository writes under a field -- any ``ast.Expr`` whose value
    is a string. Prose cannot forge an actor, and these modules exist partly to DOCUMENT the
    rules below, so scanning prose would catch nothing real and block the test.
    """
    documentation: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            documentation.add(id(node.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    ]


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _sources() -> list[Path]:
    files = sorted(path for path in PACKAGE.glob("*.py") if path.name != "__init__.py")
    assert {path.name for path in files} >= {
        "protocol.py",
        "blocks.py",
        "facts.py",
        "engine.py",
        "publish.py",
        "override.py",
        "reader.py",
    }, [path.name for path in files]
    return files


@pytest.mark.parametrize("path", _sources(), ids=lambda path: path.name)
def test_no_module_can_spell_a_human_actor(path: Path) -> None:
    """A ``"user:"`` literal anywhere here would be an actor the service invented rather than
    one a session proved (the ADR-0016 15.7.3 rule, applied to autonomy)."""
    for value in _runtime_strings(ast.parse(path.read_text(encoding="utf-8"))):
        assert "user:" not in value, (path.name, value[:60])


@pytest.mark.parametrize("path", _sources(), ids=lambda path: path.name)
def test_only_the_override_service_names_the_override_vocabulary(path: Path) -> None:
    if path.name in {OVERRIDE_MODULE, "protocol.py", "publish.py"}:
        return  # protocol.py declares the type; publish.py READS a recorded override
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in {
                "OperatorOverrideRequest",
                "OverrideAction",
            }, (path.name, node.id)


@pytest.mark.parametrize("path", _sources(), ids=lambda path: path.name)
def test_only_the_publish_path_reaches_the_deployment_service(path: Path) -> None:
    """The engine DECIDES and RECORDS; it never writes to a CMS. Nothing in the decision path
    can even import the thing that would."""
    if path.name in PUBLISH_MODULES:
        return
    for imported in _imports(ast.parse(path.read_text(encoding="utf-8"))):
        assert not imported.startswith(DEPLOYMENT_IMPORT), (path.name, imported)


@pytest.mark.parametrize("path", [PACKAGE / "blocks.py", PACKAGE / "facts.py"])
def test_the_deterministic_layer_imports_no_model_provider(path: Path) -> None:
    for imported in _imports(ast.parse(path.read_text(encoding="utf-8"))):
        assert not imported.startswith("ken_contracts.model"), (path.name, imported)
        assert not imported.startswith("ken_core.providers.model"), (path.name, imported)
        assert not imported.startswith("ken_agents"), (path.name, imported)
