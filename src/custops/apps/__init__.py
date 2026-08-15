"""Runnable applications.

Phase 1 ships one: ``api``. Later phases add ``orchestrator`` (LangGraph
runtime), ``enterprise`` (CRM / billing / support domain modules behind one
service, decision D5) and ``legacy_portal`` (the API-less provisioning portal
driven by Playwright, decision D8).
"""

from __future__ import annotations
