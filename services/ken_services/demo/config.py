"""Where the demo may run, and the refusal to run anywhere else.

The demo drops and recreates its databases, so every name is checked against a closed pattern
BEFORE any connection is opened. The patterns accept only the two demo databases (and their
``_test`` twins used by the integration test), on the project PostgreSQL at 127.0.0.1:5433, and
a Qdrant collection prefixed ``ken_demo_`` on 127.0.0.1:6333. Nothing here ever builds a URL for
the developer's own PostgreSQL (5432), the dev database, ``ken_cms_sim``, the dashboard on port
8765, or another project's services.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ken_core.config import REPO_ROOT, Settings

__all__ = [
    "APP_DB_PATTERN",
    "COLLECTION_PREFIX",
    "FIXTURE_PATH",
    "PG_HOST",
    "PG_PORT",
    "QDRANT_HOST",
    "QDRANT_PORT",
    "SIM_DB_PATTERN",
    "DemoConfig",
    "DemoRefusalError",
    "settings_for",
]

PG_HOST: Final = "127.0.0.1"
PG_PORT: Final = 5433
QDRANT_HOST: Final = "127.0.0.1"
QDRANT_PORT: Final = 6333
APP_DB_PATTERN: Final = re.compile(r"^ken_demo(_test)?$")
SIM_DB_PATTERN: Final = re.compile(r"^ken_cms_sim_demo(_test)?$")
COLLECTION_PREFIX: Final = "ken_demo_"
COLLECTION_PATTERN: Final = re.compile(r"^ken_demo_[a-z0-9_]{1,60}$")
FIXTURE_PATH: Final = REPO_ROOT / "tests" / "fixtures" / "demo" / "research_site.jsonl"
SERVICE_USERNAME: Final = "demo-autonomy-service"
HUMAN_USERNAME: Final = "demo_admin"
EMBEDDING_DIM: Final = 256
"""Small, so the deterministic feature-hash embeddings and the Qdrant round trip stay fast."""
MOCK_SIMILARITY_FLOOR: Final = 0.30
"""The semantic floor for the MOCK embeddings. The shipped default (0.78) is calibrated for a
real embedding model; lexical feature hashing yields lower cosines for genuinely related pages
(the integration test suite uses the same 0.30). It admits more candidates; every later gate
(filters, ranking, critic, blocks, verification) is unchanged."""


class DemoRefusalError(RuntimeError):
    """The demo was pointed at a resource it does not own. Nothing was touched."""


@dataclass(frozen=True, slots=True)
class DemoConfig:
    """The disposable resources of one demo run."""

    app_db: str = "ken_demo"
    sim_db: str = "ken_cms_sim_demo"
    collection: str = "ken_demo_pages"
    workdir: Path = REPO_ROOT / "evals" / "results" / "demo"
    ollama: bool = False

    def __post_init__(self) -> None:
        if APP_DB_PATTERN.fullmatch(self.app_db) is None:
            raise DemoRefusalError(
                "the demo only runs against the databases 'ken_demo' / 'ken_demo_test'"
            )
        if SIM_DB_PATTERN.fullmatch(self.sim_db) is None:
            raise DemoRefusalError(
                "the demo only runs against the simulated CMS databases "
                "'ken_cms_sim_demo' / 'ken_cms_sim_demo_test'"
            )
        if COLLECTION_PATTERN.fullmatch(self.collection) is None:
            raise DemoRefusalError("the demo only uses Qdrant collections prefixed 'ken_demo_'")

    @property
    def credentials(self) -> str:
        # The project PostgreSQL container's local development role (docker-compose.yml,
        # .env.example). Overridable for a differently provisioned machine; never printed.
        user = os.environ.get("KEN_DEMO_PG_USER", "ken")
        password = os.environ.get("KEN_DEMO_PG_PASSWORD", "ken")
        return f"{user}:{password}"

    @property
    def app_url(self) -> str:
        return f"postgresql+asyncpg://{self.credentials}@{PG_HOST}:{PG_PORT}/{self.app_db}"

    @property
    def sim_url(self) -> str:
        return f"postgresql+asyncpg://{self.credentials}@{PG_HOST}:{PG_PORT}/{self.sim_db}"

    @property
    def admin_url(self) -> str:
        """The server's maintenance database: used only to CREATE / DROP the two demo
        databases named above."""
        return f"postgresql+asyncpg://{self.credentials}@{PG_HOST}:{PG_PORT}/postgres"

    @property
    def qdrant_url(self) -> str:
        return f"http://{QDRANT_HOST}:{QDRANT_PORT}"

    @property
    def collection_prefix(self) -> str:
        return self.collection.removesuffix("pages")


def settings_for(
    config: DemoConfig,
    *,
    mode: str = "shadow",
    verify_base_url: str | None = None,
    **overrides: object,
) -> Settings:
    """A VALIDATED :class:`Settings` for the demo, built from explicit values only.

    ``_env_file=None`` means a developer's ``.env`` (which names the dev database and an
    Ollama model) can never leak in; the autonomy validators run, so ``autonomous`` carries
    its acknowledgement explicitly and visibly.
    """
    values: dict[str, object] = {
        "environment": "local",
        "log_level": "WARNING",
        "database_url": config.app_url,
        "cms_provider": "simulated",
        "cms_sim_database_url": config.sim_url,
        "model_provider": "ollama" if config.ollama else "mock",
        "mock_embedding_dim": EMBEDDING_DIM,
        "similarity_min_score": MOCK_SIMILARITY_FLOOR,
        "vector_store": "qdrant",
        "qdrant_url": config.qdrant_url,
        "qdrant_collection": config.collection,
        "autonomy_mode": mode,
        "autonomy_autonomous_acknowledged": mode == "autonomous",
        "deploy_verify_base_url": verify_base_url,
        "deploy_verify_retry_delay_s": 0.0,
        "autonomy_strict_verify_delay_s": 0.0,
        "learning_demonstration_mode": True,
    }
    if config.ollama:
        values.pop("mock_embedding_dim")
        values["similarity_min_score"] = Settings.model_fields["similarity_min_score"].default
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]
