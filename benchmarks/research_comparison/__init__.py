"""The evidence-research subflow, built twice and measured (BUILD_SPEC §10).

Two implementations of the same task — gather subscription, contract, pricing,
policy and support evidence for an account, then synthesise it — one as a
LangGraph parallel fan-out, one as a CrewAI crew.

**What is held constant.** Both run against the same retrieval sources, the same
input set, the same ground truth, and the same deterministic model. The only
variable is the orchestration framework. That is the point: a comparison where
the data layer or the model differs measures nothing about frameworks.

**What the wall-clock number means.** With a deterministic offline model, the
model call costs microseconds instead of seconds. So latency here is *framework
overhead*, not end-to-end production latency — in production both would be
dominated by identical model calls. Framework overhead is precisely the variable
that differs between the two, which is why it is worth isolating, but it must
not be read as "CrewAI is Nx slower to answer a customer".
"""

from __future__ import annotations
