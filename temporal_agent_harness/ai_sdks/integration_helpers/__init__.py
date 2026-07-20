"""Shared building blocks for AI-SDK Temporal integrations.

.. warning::
    This package is experimental and may change in future versions.

These helpers are SDK-agnostic: they are meant to be reused across the
``google_genai`` plugin and future AI-SDK plugins in this package, so that
common integration concerns (e.g. live-streaming raw provider events to a
:py:class:`~temporalio.contrib.workflow_streams.WorkflowStream`) are defined
once rather than re-implemented per SDK.

Copied into the harness from the sdk-python ``temporalio.contrib.integration_helpers``
experiment so we can iterate on the observer abstraction here without coupling to
sdk-python. Wired into the vendored ``openai_agents`` streaming activity via
:func:`select_observer`; the harness-specific observer lives in
:mod:`temporal_agent_harness.ai_sdks.openai_agents_harness`.

See :py:class:`StreamObserver` for the live-streaming observer contract.
"""

from temporal_agent_harness.ai_sdks.integration_helpers._stream_observer import (
    ObserverFactory,
    RawTopicObserver,
    StreamObserver,
    select_observer,
)

__all__ = [
    "ObserverFactory",
    "RawTopicObserver",
    "StreamObserver",
    "select_observer",
]
