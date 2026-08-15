"""Contracts and policies.

A fourth module alongside CRM, billing and support. Decision D5 names three
domain modules; contracts are added here rather than folded into one of them
because they belong to none of the three — they are commercial terms that
*constrain* billing, are referenced by CRM, and are interpreted as evidence.
Filing them under billing would make the eligibility rule look like a billing
rule, which is exactly the confusion §12 exists to prevent.
"""

from __future__ import annotations
