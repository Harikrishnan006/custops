"""A simulated legacy provisioning portal (BUILD_SPEC §11, decision D8).

**This app deliberately has no API.** Server-rendered HTML, session-cookie auth,
form submissions. That is not laziness dressed as realism — it is the constraint
the whole architecture is built around. Because there is no endpoint to call,
the only way to change an entitlement is to drive a browser, which is what makes
Playwright a requirement rather than a résumé keyword.

It is also the **authoritative store for entitlements**. Billing can accept a
plan change and return 200 while this portal still says Professional, and the
customer is then billed for one tier and provisioned for another. The Validator
exists to catch exactly that, and it can only catch it because this is a
genuinely separate system that must be read on its own terms.

Nothing else writes to ``entitlements``. If something did, the divergence this
portal makes possible would stop being detectable.
"""

from __future__ import annotations
