"""ken_core.providers.model -- ModelProvider implementations.

Business logic depends on :class:`ken_contracts.model.ModelProvider` and obtains
an instance from :func:`build_model_provider`; it never imports a concrete
provider directly.
"""

from ken_core.providers.model.factory import build_model_provider
from ken_core.providers.model.mock import MockModelProvider
from ken_core.providers.model.ollama import OllamaProvider

__all__ = ["MockModelProvider", "OllamaProvider", "build_model_provider"]
