"""Composition root of the deployment service: the real ``HtmlMutator`` and
``HtmlLinkVerifier`` are resolved lazily BY NAME (they live in their own modules, owned by
the mutation workstream); everything else is injected. The API and the CLI build the
service only through :func:`build_deployment_service`.
"""

from __future__ import annotations

import importlib
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ken_contracts.cms import CMSProvider
from ken_core.config import Settings
from ken_services.deployment.protocol import LinkVerifier, Mutator
from ken_services.deployment.service import DatabaseDeploymentService

__all__ = ["MUTATION_MODULE", "VERIFICATION_MODULE", "build_deployment_service", "real_engines"]

MUTATION_MODULE: Final = "ken_services.deployment.mutation"
VERIFICATION_MODULE: Final = "ken_services.deployment.verification"


def real_engines() -> tuple[Mutator, LinkVerifier]:
    mutation: Any = importlib.import_module(MUTATION_MODULE)
    verification: Any = importlib.import_module(VERIFICATION_MODULE)
    mutator: Mutator = mutation.HtmlMutator()
    verifier: LinkVerifier = verification.HtmlLinkVerifier()
    return mutator, verifier


def build_deployment_service(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cms: CMSProvider,
) -> DatabaseDeploymentService:
    mutator, verifier = real_engines()
    return DatabaseDeploymentService(
        sessionmaker=sessionmaker, settings=settings, cms=cms, mutator=mutator, verifier=verifier
    )
