"""AI Customer Operations Orchestrator.

An agentic platform that converts natural-language B2B SaaS customer-operations
requests into executable, stateful, auditable workflows producing real state
changes. See docs/BUILD_SPEC.md for the authoritative architecture.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    __version__ = _package_version("custops")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
