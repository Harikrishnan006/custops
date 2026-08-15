"""Measurement code. Deliberately outside ``src/custops``.

BUILD_SPEC D7 says CrewAI is a measured comparison and never a production path.
Keeping this package outside the application package makes that structural
rather than aspirational: nothing under ``src/custops`` can import CrewAI,
because CrewAI is a dev dependency and this package is not installed with the
application.
"""

from __future__ import annotations
