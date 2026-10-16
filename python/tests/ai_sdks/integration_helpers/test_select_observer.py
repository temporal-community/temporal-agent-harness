# ABOUTME: Unit-tests select_observer's routing and the flush cadence it threads — that a
# configured ObserverFactory is called with (token, batch_interval=...) so a plugin's
# streaming_batch_interval governs a custom observer's publisher too, and that the default
# raw-topic path carries the same interval. No Temporal server, no provider keys.
#
# Run with: uv run pytest tests/ai_sdks/integration_helpers/test_select_observer.py -v

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest

from temporal_agent_harness.ai_sdks.integration_helpers import (
    ObserverFactory,
    RawTopicObserver,
    StreamObserver,
    select_observer,
)


class _StubObserver:
    """The minimal StreamObserver a factory has to hand back."""

    async def __aenter__(self) -> StreamObserver[Any]:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool | None:
        return None

    async def on_event(self, event: Any) -> None:
        pass


class _RecordingFactory(ObserverFactory):
    """An ObserverFactory that records how select_observer called it."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, timedelta]] = []

    def __call__(
        self, token: Any, *, batch_interval: timedelta
    ) -> AbstractAsyncContextManager[StreamObserver[Any]]:
        self.calls.append((token, batch_interval))

        @asynccontextmanager
        async def _cm() -> AsyncGenerator[StreamObserver[Any], None]:
            yield _StubObserver()

        return _cm()


@pytest.mark.asyncio
async def test_factory_receives_token_and_batch_interval():
    factory = _RecordingFactory()
    token = {"context": {"turn_id": "t-1"}}

    async with select_observer(
        factory=factory, token=token, batch_interval=timedelta(milliseconds=400)
    ):
        pass

    assert factory.calls == [(token, timedelta(milliseconds=400))]


def test_raw_topic_path_carries_the_batch_interval():
    observer = select_observer(
        factory=None, token="raw_events", batch_interval=timedelta(milliseconds=250)
    )
    assert isinstance(observer, RawTopicObserver)
    assert observer._batch_interval == timedelta(milliseconds=250)


@pytest.mark.asyncio
async def test_no_token_means_no_observer_and_no_factory_call():
    factory = _RecordingFactory()
    async with select_observer(factory=factory, token=None) as obs:
        assert obs is None
    assert factory.calls == []


@pytest.mark.asyncio
async def test_non_str_token_without_factory_is_a_no_op():
    async with select_observer(factory=None, token=object()) as obs:
        assert obs is None
