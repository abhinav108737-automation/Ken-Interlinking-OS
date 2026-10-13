"""Platform primitives for the Ken Research Agentic Interlinking OS.

This package holds the deterministic plumbing every other package depends on:
configuration, structured logging, database access, readiness probes and the
provider implementations. It deliberately contains no domain intelligence --
TRD.md section 11 reserves deterministic services for exactly this kind of
code, and Phase 0 has no reasoning to place anywhere.
"""

from ken_core.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
