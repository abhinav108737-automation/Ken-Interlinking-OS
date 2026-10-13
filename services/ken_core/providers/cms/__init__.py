"""ken_core.providers.cms -- CMS providers behind the frozen CMSProvider protocol.

Phase 0 ships only the in-memory :class:`MockCMSProvider`; there is no network
client for any real CMS.
"""

from ken_core.providers.cms.factory import build_cms_provider
from ken_core.providers.cms.mock import AuditEntry, IssueCode, MockCMSProvider
from ken_core.providers.cms.seed import SEED_PAGE_IDS, seed_pages

__all__ = [
    "SEED_PAGE_IDS",
    "AuditEntry",
    "IssueCode",
    "MockCMSProvider",
    "build_cms_provider",
    "seed_pages",
]
