"""The local end-to-end demonstration (``make demo``; ``docs/DEMO.md``).

A thin orchestration layer over the existing services. It re-implements none of the engine:
every stage calls the same library entry points the CLIs call, against DISPOSABLE resources
that :mod:`ken_services.demo.config` refuses to confuse with anything else.
"""

from __future__ import annotations
