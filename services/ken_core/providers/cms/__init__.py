"""ken_core.providers.cms -- CMS providers behind the frozen CMSProvider protocol.

``mock`` (in memory), ``simulated`` (a local CMS in its own database) and ``http`` (the TRD
section 13 adapter, gated and inert until enabled). See ``factory.py`` and ADR-0014.
"""

from ken_core.providers.cms.factory import build_cms_provider, build_html_engine
from ken_core.providers.cms.html_engine import HtmlOutcome, LinkHtmlEngine
from ken_core.providers.cms.mock import AuditEntry, IssueCode, MockCMSProvider
from ken_core.providers.cms.seed import SEED_PAGE_IDS, seed_pages

__all__ = [
    "SEED_PAGE_IDS",
    "AuditEntry",
    "HtmlOutcome",
    "IssueCode",
    "LinkHtmlEngine",
    "MockCMSProvider",
    "build_cms_provider",
    "build_html_engine",
    "seed_pages",
]
