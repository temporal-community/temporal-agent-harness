"""Parameters for configuring Temporal activity execution for model calls."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from agents import Agent, Model, TResponseInputItem

from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent


class ModelSummaryProvider(ABC):
    """Abstract base class for providing model summaries. Essentially just a callable,
    but the arguments are sufficiently complex to benefit from names.
    """

    @abstractmethod
    def provide(
        self,
        agent: Agent[Any] | None,
        instructions: str | None,
        input: str | list[TResponseInputItem],
    ) -> str:
        """Given the provided information, produce a summary for the model invocation activity."""
        pass


@dataclass
class ModelActivityParameters:
    """Parameters for configuring Temporal activity execution for model calls.

    This class encapsulates all the parameters that can be used to configure
    how Temporal activities are executed when making model calls through the
    OpenAI Agents integration.
    """

    task_queue: str | None = None
    """Specific task queue to use for model activities."""

    schedule_to_close_timeout: timedelta | None = None
    """Maximum time from scheduling to completion."""

    schedule_to_start_timeout: timedelta | None = None
    """Maximum time from scheduling to starting."""

    start_to_close_timeout: timedelta | None = timedelta(seconds=60)
    """Maximum time for the activity to complete."""

    heartbeat_timeout: timedelta | None = None
    """Maximum time between heartbeats. For streaming
    (``Runner.run_streamed``), set this lower than
    ``start_to_close_timeout`` so a stuck model call is detected before the
    overall activity timeout fires."""

    retry_policy: RetryPolicy | None = None
    """Policy for retrying failed activities."""

    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL
    """How the activity handles cancellation."""

    versioning_intent: VersioningIntent | None = None
    """Versioning intent for the activity."""

    summary_override: None | (str | ModelSummaryProvider) = None
    """Summary for the activity execution."""

    priority: Priority = Priority.default
    """Priority for the activity execution."""

    use_local_activity: bool = False
    """Whether to use a local activity. If changed during a workflow execution, that would break determinism."""

    workflow_model_provider: Callable[[str | None], Model] | None = None
    """Optional workflow-side model provider for non-streaming model calls.

    When set, ``_TemporalModelStub.get_response`` resolves a ``Model`` from this
    callable (given the requested model name) and awaits its ``get_response``
    directly, INSTEAD of scheduling the ``invoke_model_activity`` activity. The
    resolved model runs in workflow context, so — unlike the activity path's
    worker-side ``model_provider`` — it may do workflow-only things, most usefully
    make its transport a Nexus call (``workflow.create_nexus_client(...)``), which
    is impossible from an activity.

    It is the workflow-side analog of the activity path's ``model_provider``: the
    plugin stays agnostic about the model's transport. Because the stub hands the
    resolved model its already-live ``tools`` / ``handoffs`` / ``model_settings``,
    there is no serialization or tool reconstruction on this path. Streaming
    (``Runner.run_streamed``) ignores it.

    .. warning::
        Experimental; behavior may change."""

    streaming_topic: str | None = None
    """Stream topic to publish raw model stream events to when the workflow
    calls ``Runner.run_streamed``. Required for ``Runner.run_streamed``;
    if left as ``None``, ``run_streamed`` raises before scheduling any
    activity. The workflow must host a
    :class:`temporalio.contrib.workflow_streams.WorkflowStream` to receive
    the publishes; otherwise the signals are unhandled and dropped.

    Streaming is incompatible with ``use_local_activity`` (local activities
    do not support heartbeats or the workflow stream signal channel).

    .. warning::
        Streaming support is experimental and may change in future
        versions."""

    streaming_batch_interval: timedelta = timedelta(milliseconds=100)
    """Interval between automatic flushes for the stream publisher used
    by the streaming activity.

    .. warning::
        Streaming support is experimental and may change in future
        versions."""

    stream_to_provider: Callable[[str | None], Any] | None = None
    """Optional per-call provider of an opaque routing token for streamed
    requests. Called once (in workflow context) at the start of each
    ``Runner.run_streamed`` model call, with the requested model id; its return
    value is handed, opaquely, to the streaming activity's observer factory to
    resolve where live stream events go (and to carry any per-call metadata the
    observer needs, such as the model). Returning ``None`` falls back to
    :attr:`streaming_topic`.

    This is the seam that lets an embedding runtime (e.g. the agent harness)
    target a per-turn stream instead of a static worker-level topic, without
    the plugin knowing anything about the token's concrete type. When left
    ``None``, streaming uses :attr:`streaming_topic` exactly as before.

    .. warning::
        Streaming support is experimental and may change in future
        versions."""
