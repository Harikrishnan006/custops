"""Driving the legacy provisioning portal (§11, D8).

A separate package rather than an enterprise domain module, because this is not
a domain: it is an integration with an external system that happens to have no
API. Everything browser-specific lives behind ``ProvisioningClient`` so the rest
of the codebase never imports Playwright, and so the execution path can be
exercised without a browser.
"""

from __future__ import annotations
