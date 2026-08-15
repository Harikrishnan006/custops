"""Test helpers importable from any test module.

Kept out of ``conftest.py`` because importing a conftest module directly is a
pytest anti-pattern: conftest files are loaded by the plugin system, and
importing one by name can load it twice under two different module identities.
"""

from __future__ import annotations

import socket

DEFAULT_PROBE_TIMEOUT_SECONDS = 0.5


def service_reachable(
    host: str,
    port: int,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Return whether a TCP connection to ``host:port`` succeeds.

    Used to skip integration tests when a dependency is not running rather than
    fail them. A skipped test reports "not exercised"; a failing test would
    assert the code is broken, which is a different and false claim.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
