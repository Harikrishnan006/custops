"""Chat completion — the second provider capability (D11).

Structured output only. Every agent that calls a model wants a *typed object*
(a classification, a plan, a rationale summary), not free text, so the contract
is "give me an instance of this Pydantic model" rather than "give me a string I
will then parse". Parsing model prose into structure is where hallucinated
fields and silent format drift enter a system.

The deterministic implementation below is the same device as Phase 3's
deterministic embedder: a **labelled test double**, refused outside
``local``/``test``, that makes every graph path exercisable with no API key. It
does not simulate intelligence — it returns a fixed, valid instance of whatever
schema it is asked for, which is precisely what is needed to test that routing,
budgets and state accumulation behave, without entangling those tests with model
behaviour.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from custops.providers.base import ProviderName

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@runtime_checkable
class ChatProvider(Protocol):
    """Turns a prompt into a validated instance of a schema."""

    @property
    def model(self) -> str: ...

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
    ) -> SchemaT:
        """Return an instance of ``schema``, or raise ``ProviderError``."""
        ...


class DeterministicChatProvider:
    """A fixed, schema-valid responder for tests and offline development.

    **Not a model.** It performs no reasoning. Given a schema it constructs the
    registered canned instance, or failing that a minimal valid instance built
    from field defaults. Identical inputs always produce identical output, so a
    graph test that fails indicates a graph regression rather than model drift.

    Canned responses are registered per schema by the test that needs them,
    which keeps the double free of knowledge about any particular workflow.
    """

    MODEL_NAME = "deterministic-chat-v1"

    def __init__(self, responses: dict[type[BaseModel], BaseModel] | None = None) -> None:
        self._responses: dict[type[BaseModel], BaseModel] = dict(responses or {})
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return self.MODEL_NAME

    @property
    def provider(self) -> str:
        return ProviderName.DETERMINISTIC

    def register(self, schema: type[BaseModel], response: BaseModel) -> None:
        """Pin the instance returned for ``schema``."""
        self._responses[schema] = response

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
    ) -> SchemaT:
        # Recording calls lets a test assert *that* a node consulted the model
        # and with what — without asserting anything about what a model said.
        self.calls.append({"system": system, "user": user, "schema": schema.__name__})

        canned = self._responses.get(schema)
        if canned is not None:
            if not isinstance(canned, schema):
                raise TypeError(
                    f"Registered response for {schema.__name__} is a {type(canned).__name__}."
                )
            return canned

        # No canned response: build the minimal valid instance. This fails
        # loudly for schemas with required fields, which is correct — a test
        # that needs a specific answer should say so rather than silently
        # receiving an empty one.
        return schema()
