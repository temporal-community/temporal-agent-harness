"""Model-invocation observer contract shared across AI-SDK Temporal integrations.

The sibling :mod:`._stream_observer` contract covers what is genuinely
*streaming-specific*: the raw provider events of a token-streamed call, observed
inside the activity as they arrive.  This contract covers what is **not**
streaming-specific at all — the fact that a model was invoked, how long the call
took, what it cost, and what it asked for.  Those are facts about the turn, true
whether or not the reply tokens were streamed out, so a non-streamed call must be
able to report them too.

Where the two run differs, and that difference is the whole point:

* a :class:`~._stream_observer.StreamObserver` lives **in the activity**, because
  that is the only place the live event stream exists;
* a :class:`ModelCallObserver` lives **in the workflow**, wrapped around the
  model activity's dispatch — the one place every model call passes through,
  streamed or not.  It is therefore synchronous and must not do I/O: it only
  brackets the ``await`` on the activity.

An SDK plugin resolves one per non-streamed call from a
:data:`ModelCallObserverProvider` and never inspects what it gets back, exactly
as it does with the streaming routing token.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

__all__ = [
    "ModelCallObserver",
    "ModelCallObserverProvider",
    "NullModelCallObserver",
    "select_model_call_observer",
]


class ModelCallObserver(Protocol):
    """Brackets ONE non-streamed model call, workflow-side.

    A synchronous context manager, entered immediately before the model activity
    is dispatched and exited once it settles — so the bracket measures the true
    model-call latency — with :meth:`on_response` called in between on success,
    carrying the provider's response (usage, requested tool calls, …).

    Synchronous and side-effect-light on purpose: it runs in workflow code, so it
    may only do deterministic, non-blocking work (e.g. publishing onto a
    :class:`~temporalio.contrib.workflow_streams.WorkflowStream`).
    """

    def __enter__(self) -> ModelCallObserver: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None: ...

    def on_response(self, response: Any) -> None: ...


# Per-call provider: ``(requested_model_id, run_context) -> observer | None``.
# Mirrors ``ModelActivityParameters.stream_to_provider``: the ``run_context`` is
# whatever the caller threaded through the SDK (e.g. ``Runner.run(..., context=...)``),
# opaque to the plugin, so an embedding runtime can turn its own handle into an
# observer. Returning ``None`` means "don't observe this call".
ModelCallObserverProvider = Callable[[Optional[str], Any], Optional["ModelCallObserver"]]


class NullModelCallObserver:
    """The "no observer configured" sentinel — a do-nothing :class:`ModelCallObserver`.

    Lets a plugin wrap every model call in one uniform ``with`` block instead of
    branching on whether an observer was resolved.
    """

    def __enter__(self) -> NullModelCallObserver:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        # Never swallow the model call's failure.
        return False

    def on_response(self, response: Any) -> None:
        pass


def select_model_call_observer(
    *,
    provider: Optional[ModelCallObserverProvider],
    model: str | None,
    run_context: Any,
) -> ModelCallObserver:
    """Resolve the per-call workflow-side observer, never returning ``None``.

    No provider configured — or a provider that declines this call by returning
    ``None`` — yields a :class:`NullModelCallObserver`, so the call site is a
    plain ``with`` with no ``is None`` branch.
    """
    if provider is None:
        return NullModelCallObserver()
    return provider(model, run_context) or NullModelCallObserver()
