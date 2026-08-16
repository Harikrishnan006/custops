"""Authentication and endpoint authorization (§17, Phase 13).

Before this phase every endpoint was open, and ``actor_user_id`` on an approval
decision was *asserted by the caller*. Anyone who could reach the API could
approve a high-value upgrade as the finance approver, and the audit trail would
faithfully record the lie.

Three pieces, deliberately separate:

``tokens``
    Minting, hashing and verifying bearer credentials. Knows nothing about HTTP.
``principal``
    The FastAPI dependency that turns an ``Authorization`` header into an
    authenticated principal, or refuses the request.
``custops.domain.policies.endpoint_authority``
    Which roles may do what. A policy, not a router concern, so it is testable
    without a request.

**There is no authentication-disabled mode.** Tests authenticate through the
same dependency production uses. A bypass flag is the thing that eventually
ships enabled.
"""

from __future__ import annotations
