"""Policy: thresholds and permissions that gate action.

Separate from ``domain/rules`` on purpose. A *rule* answers "what is true?"
(this proration comes to $412.90; this contract forbids the change). A *policy*
answers "what are we willing to do without asking a human?" — a question whose
answer is a configuration decision the business owns, not a fact.

Keeping them apart means thresholds can be tuned per environment without editing
the arithmetic, and the arithmetic can be tested without reference to whatever
the current risk appetite happens to be.
"""

from __future__ import annotations
