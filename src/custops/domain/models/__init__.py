"""SQLAlchemy models.

Importing this package must import every model module: ``Base.metadata`` is only
complete once each mapped class has been defined, and Alembic's autogenerate
compares the database against that metadata. A model that is not imported here is
invisible to migrations — which fails silently, in the worst possible direction.
"""

from __future__ import annotations

from custops.domain.models.audit import AuditEvent
from custops.domain.models.identity import Role, User, UserRole

__all__ = ["AuditEvent", "Role", "User", "UserRole"]
