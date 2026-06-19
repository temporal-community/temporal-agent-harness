"""Temporal-aware ``interactions`` shim.

``TemporalAsyncInteractions`` mirrors the relevant subset of the SDK's
``google.genai._interactions.resources.AsyncInteractionsResource`` so
workflow code can use ``gemini.interactions.create(...)`` with the same
shape it would use against the real SDK — the call is silently routed
through the ``gemini_interactions_create_streamed`` Temporal activity,
which holds the real ``genai.Client`` on the worker side.

The shim is wired into :class:`TemporalAsyncClient` (overriding the base
``interactions`` property), so consumers don't need any plugin-aware
helper functions to invoke the Interactions API from a workflow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast

from google.genai._interactions._models import construct_type
from google.genai._interactions.resources import AsyncInteractionsResource
from google.genai._interactions.types import InteractionSSEEvent

from temporalio import workflow as temporal_workflow
from temporalio.workflow import ActivityConfig

from ._interactions_models import _InteractionResult
from ._temporal_api_client import TemporalApiClient


_INTERACTIONS_ACTIVITY_NAME = "gemini_interactions_create_streamed"


def _deserialize_event(d: dict[str, Any]) -> InteractionSSEEvent:
    """Rehydrate one event dict into its concrete ``InteractionSSEEvent`` subtype.

    Uses Stainless's lenient ``construct_type`` rather than Pydantic's
    strict ``model_validate``. The API legitimately emits sparse nested
    payloads (e.g., the ``interaction.created`` event carries an
    ``Interaction`` with just ``id`` and ``object`` — ``created``,
    ``steps``, ``updated`` arrive later via deltas), and the SDK's own
    SSE parser tolerates that. ``construct_type`` matches that behavior
    and also dispatches the ``InteractionSSEEvent`` discriminated union
    on the ``event_type`` field.
    """
    return cast(InteractionSSEEvent, construct_type(type_=InteractionSSEEvent, value=d))


class _TemporalAsyncInteractionStream:
    """Async-iterable wrapper that quacks like the SDK's ``AsyncStream``.

    The activity has already drained the upstream SSE stream by the time
    we wrap its result, so iteration is just walking the in-memory list
    and rehydrating each event back into its typed form.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def __aiter__(self) -> AsyncIterator[InteractionSSEEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[InteractionSSEEvent]:
        for d in self._events:
            yield _deserialize_event(d)


class TemporalAsyncInteractions(AsyncInteractionsResource):
    """Workflow-side ``interactions`` shim.

    Subclasses :class:`AsyncInteractionsResource` so :attr:`TemporalAsyncClient.interactions`
    type-checks as a valid override, but skips the parent ``__init__``:
    the Stainless ``AsyncGeminiNextGenAPIClient`` it would expect doesn't
    exist on the workflow side. Only the streaming ``create`` path is
    overridden — every other inherited method would dereference the
    missing ``_client`` and raise; extend the shim if a future workflow
    needs ``get`` / ``cancel`` / non-streaming ``create``.

    The streaming ``create`` dispatches through the
    ``gemini_interactions_create_streamed`` Temporal activity. The
    activity itself republishes streaming text content as ``reply_delta``
    events on the workflow's :class:`WorkflowStream` when the
    ``TemporalApiClient`` was constructed with a runner, so workflows
    don't need to wire that side-channel themselves.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        api_client: TemporalApiClient,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        # Skip super().__init__(): AsyncAPIResource expects a real
        # AsyncGeminiNextGenAPIClient (with httpx, credentials, etc.), and
        # we'd never use it — every supported call routes through a
        # Temporal activity that holds the real client on the worker side.
        self._api_client = api_client
        self._activity_config = (
            ActivityConfig(start_to_close_timeout=timedelta(minutes=3))
            if activity_config is None
            else activity_config
        )

    async def create(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> _TemporalAsyncInteractionStream:
        """Dispatch one streamed ``interactions.create`` call through an activity.

        ``kwargs`` is forwarded verbatim to ``client.aio.interactions.create``
        on the worker — no per-field enumeration here, the SDK validates
        its own signature. Returns an async-iterable wrapper whose events
        match the SDK's ``AsyncStream[InteractionSSEEvent]`` shape:
        iterate with ``async for event in await gemini.interactions.create(...)``.
        """
        if not stream:
            raise NotImplementedError(
                "TemporalAsyncInteractions only supports stream=True today; "
                "pass stream=True or extend the shim for the non-streaming path."
            )
        kwargs["stream"] = True

        # The runner (if any) is wired into the TemporalApiClient at
        # construction time; the streaming activity reads
        # ``current_stream_context`` from it to publish per-text-chunk
        # ``reply_delta`` events on the workflow's stream.
        stream_context = (
            self._api_client._runner.current_stream_context
            if self._api_client._runner is not None
            else None
        )
        act_config: ActivityConfig = {**self._activity_config}
        if "summary" not in act_config:
            act_config["summary"] = "interactions.create"
        result: _InteractionResult = await temporal_workflow.execute_activity(
            _INTERACTIONS_ACTIVITY_NAME,
            args=[kwargs, stream_context],
            result_type=_InteractionResult,
            **act_config,
        )
        return _TemporalAsyncInteractionStream(result.events)
