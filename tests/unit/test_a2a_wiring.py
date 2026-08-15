"""How the runner decides whether to consult the specialist at all.

The switch matters more than it looks. ``A2A_ENABLED`` defaults to false so the
platform never acquires an undeclared hard dependency on an optional process
(ADR-006) — but a switch that is wired backwards, or not wired at all, would be
invisible until the day the specialist went down.
"""

from __future__ import annotations

from custops.a2a.client.billing import BillingSpecialistClient
from custops.apps.orchestrator.runner import _specialist_from
from custops.config import A2ASettings, Settings


def _settings(**a2a: object) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        a2a=A2ASettings(_env_file=None, **a2a),  # type: ignore[arg-type]
    )


def test_the_specialist_is_off_by_default() -> None:
    """An optional second opinion must be opted into, not out of."""
    assert A2ASettings(_env_file=None).enabled is False
    assert _specialist_from(_settings()) is None


def test_no_client_is_built_when_the_specialist_is_disabled() -> None:
    """Disabled means never contacted, not contacted-and-ignored."""
    assert _specialist_from(_settings(enabled=False)) is None


def test_a_client_is_built_when_the_specialist_is_enabled() -> None:
    client = _specialist_from(
        _settings(enabled=True, billing_specialist_url="http://specialist.test:9000")
    )

    assert isinstance(client, BillingSpecialistClient)


def test_the_configured_timeout_reaches_the_client() -> None:
    """A slow second opinion must not stall the workflow behind it."""
    client = _specialist_from(_settings(enabled=True, timeout_seconds=1.5))

    assert client is not None
    assert client._timeout == 1.5


def test_the_configured_url_reaches_the_client() -> None:
    """The orchestrator holds a URL, not an import — so the URL must be used."""
    client = _specialist_from(
        _settings(enabled=True, billing_specialist_url="http://elsewhere.test:9100/")
    )

    assert client is not None
    # Trailing slash normalised, so path joins cannot produce a double slash.
    assert client._base_url == "http://elsewhere.test:9100"
