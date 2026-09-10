# ABOUTME: ``AgentHarnessPlugin`` — the single Temporal plugin that wires a worker (and its
# client) for the agent harness. Add the plugin and you get the harness's capabilities; you
# never hand-assemble its activity list or its data converter again.
#
# Before::
#
#     subagents = SubagentActivities(client)
#     client = await Client.connect(
#         **connect_config,
#         plugins=[GoogleGenAIPlugin(gemini)],
#         data_converter=await with_large_payload_offload(pydantic_data_converter),
#     )
#     Worker(
#         client,
#         task_queue=...,
#         workflows=[MyAgent],
#         activities=[
#             *(agent.tool_activity(t) for t in MY_TOOLS),
#             *CODE_MODE_ACTIVITIES,
#             subagents.run_subagent_turn,
#         ],
#     )
#
# After::
#
#     client = await Client.connect(
#         **connect_config,
#         plugins=[GoogleGenAIPlugin(gemini), AgentHarnessPlugin(tools=MY_TOOLS)],
#     )
#     Worker(client, task_queue=..., workflows=[MyAgent])
#
# DESIGN — why a plugin and not a ``create_agent_worker(...)`` helper: Temporal's plugin
# protocol is the composition seam the AI-SDK integrations already use
# (``GoogleGenAIPlugin``, ``OpenAIAgentsPlugin``, Pydantic AI's ``AgentPlugin``). A plugin
# layers onto whichever of those an agent uses instead of competing with them for ownership
# of the ``Client``/``Worker`` constructor, and it applies to the client and the worker from
# one registration — which matters because the harness's data-converter requirements are a
# CLIENT concern while its activities are a WORKER concern.
#
# DESIGN — ORDER the harness plugin LAST: ``plugins=[<ai_sdk_plugin>, AgentHarnessPlugin()]``.
# Plugins configure the client in list order, and an AI-SDK plugin installs its own payload
# converter (``OpenAIAgentsPlugin`` installs ``OpenAIPayloadConverter`` and REJECTS a converter
# it doesn't recognize). This plugin therefore only fills in a payload converter that is still
# the SDK default and otherwise leaves the AI SDK's in place — so it composes when it runs
# last, and would trip the AI SDK's own check if it ran first. See :func:`_data_converter`.
#
# DESIGN — client-bound activities: ``SubagentActivities.run_subagent_turn`` must close over
# the worker's ``Client`` (it talks to CHILD workflows, which
# ``WorkflowStreamClient.from_within_activity()`` cannot reach). ``SimplePlugin``'s
# ``activities`` hook only sees the existing activity list, not the worker config, so this
# plugin overrides :meth:`AgentHarnessPlugin.configure_worker` to read ``config["client"]``
# and bind there. That is the reason this is a ``SimplePlugin`` SUBCLASS rather than a
# ``SimplePlugin`` instance.

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter, ExternalStorage
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkerConfig

from temporal_agent_harness.harness.code_mode.activities import CODE_MODE_ACTIVITIES
from temporal_agent_harness.utils.large_payload import DEFAULT_PAYLOAD_STORAGE


def _activity_name(fn: Callable[..., Any]) -> str | None:
    """The registered Temporal activity name of ``fn``, or ``None`` if it isn't an activity."""
    return getattr(getattr(fn, "__temporal_activity_definition", None), "name", None)


def _tool_activities(
    tools: Sequence[Callable[..., Any]],
) -> list[Callable[..., Any]]:
    """The durable activity bodies of the ``@agent.activity_tool_defn`` tools in ``tools``.

    Takes an agent's whole toolset, not a pre-filtered one: inline (``@agent.tool_defn``) and
    callback (``@agent.callback_tool_defn``) tools have no worker-side body and are skipped, as
    are the inline tools returned by ``agent.subagent_toolset(...)``. Anything that is not a
    harness tool at all is a mistake (a plain function, or a tool whose decorator was forgotten)
    and raises rather than being silently dropped.
    """
    bodies: list[Callable[..., Any]] = []
    for tool in tools:
        activity_fn = getattr(tool, "activity", None)
        if getattr(tool, "__agent_activity_tool__", False) and activity_fn is not None:
            bodies.append(activity_fn)
        elif getattr(tool, "__agent_tool__", False):
            # Inline or callback tool: runs in the workflow (or on an attached client), so
            # there is no activity to register.
            continue
        else:
            name = getattr(tool, "__name__", None) or repr(tool)
            raise TypeError(
                f"AgentHarnessPlugin(tools=...) got {name}, which is not a harness tool. "
                "Pass the tools declared with @agent.activity_tool_defn / @agent.tool_defn / "
                "@agent.callback_tool_defn (the activity-backed ones are registered; the "
                "others are skipped)."
            )
    return bodies


class AgentHarnessPlugin(SimplePlugin):
    """One plugin that gives a client + worker every agent-harness capability.

    Register it on the client alongside whichever AI-SDK plugin the agent uses — **last**, so
    the AI SDK's own payload converter wins (see the module notes) — and the worker needs
    nothing but its workflows::

        client = await Client.connect(
            **ClientConfig.load_client_connect_config(),
            plugins=[
                OpenAIAgentsPlugin(model_params=...),
                AgentHarnessPlugin(tools=MY_TOOLS),
            ],
        )
        worker = Worker(client, task_queue="my-agent", workflows=[MyAgent])

    What it configures:

    * **Data converter** — a Pydantic payload converter (the harness's events, tool payloads,
      and Code Mode batch models are Pydantic models with discriminated unions) plus
      large-payload offload to external storage. The offload matters across processes, not just
      within one: every client, worker, and server that reads a harness payload must use the
      same converter or an offloaded payload can't be read back, and this plugin is how they
      agree. Only filled in where it isn't already set, so an AI-SDK plugin's converter and a
      hand-passed ``data_converter=`` both survive.
    * **Subagent activity** — ``run_subagent_turn``, bound to the worker's own ``Client`` so it
      can drive child agents. Every agent built with ``agent.subagent_toolset(...)`` needs it.
    * **Code Mode activities** — the two sandbox-stepping activities, registered
      unconditionally. Whether the optional ``code-mode`` extra is installed is checked per
      call, inside the activity, so a worker without it fails a Code Mode call with an
      actionable non-retryable error rather than leaving the activity names unregistered —
      which Temporal answers with a *retryable* error, hanging the turn (see
      :func:`temporal_agent_harness.harness.code_mode.activities._require_code_mode_extra`).
    * **Tool activities** — the durable body of each ``@agent.activity_tool_defn`` tool in
      ``tools``.

    Args:
        tools: The agent's tools. The activity-backed ones get their durable bodies
            registered; inline and callback tools are skipped (they have no worker-side
            body), so an agent's whole toolset can be passed as-is.
        large_payload_offload: Where the data converter offloads oversized payloads —
            Code Mode snapshots and large tool results routinely exceed Temporal's ~2 MB
            limit. Defaults to :func:`~temporal_agent_harness.utils.large_payload.local_payload_storage`,
            which is single-host only; pass
            :func:`~temporal_agent_harness.utils.large_payload.s3_payload_storage` (or any
            ``ExternalStorage`` of your own) for a multi-host deploy, reading whatever env
            vars your deployment uses in your own startup code. Every client, worker, and
            server must end up with the same backend, or a payload offloaded by one is
            unreadable by another. ``None`` disables offloading entirely.
    """

    def __init__(
        self,
        *,
        tools: Sequence[Callable[..., Any]] = (),
        # Defaulting to the shared local storage rather than to ``None`` keeps ``None`` free
        # to mean the one other thing a caller might want: no offloading at all.
        large_payload_offload: ExternalStorage | None = DEFAULT_PAYLOAD_STORAGE,
    ) -> None:
        # Resolved eagerly so a bad `tools` entry surfaces at plugin construction, next to the
        # developer's own call, rather than deep inside Worker() where the traceback points at
        # SDK internals.
        self._worker_activities: list[Callable[..., Any]] = [
            *_tool_activities(tools),
            *CODE_MODE_ACTIVITIES,
        ]

        def data_converter(converter: DataConverter | None) -> DataConverter:
            return _data_converter(converter, offload=large_payload_offload)



        super().__init__(
            name="AgentHarnessPlugin",
            data_converter=data_converter,
            activities=self._append_activities,
        )

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """See base class.

        Extends ``SimplePlugin`` to add the activities that must close over the worker's
        ``Client`` — ``run_subagent_turn`` talks to CHILD agent workflows, so it cannot use
        the activity's own parent (see the module notes).
        """
        config = super().configure_worker(config)
        client = config.get("client")
        if client is None:  # pragma: no cover - Worker always supplies its client
            return config

        from temporal_agent_harness.harness.subagent_activities import (
            SubagentActivities,
        )

        config["activities"] = _merge_activities(
            config.get("activities"),
            [SubagentActivities(client).run_subagent_turn],
        )
        return config

    def _append_activities(
        self, activities: Sequence[Callable[..., Any]] | None
    ) -> Sequence[Callable[..., Any]]:
        return _merge_activities(activities, self._worker_activities)


def _merge_activities(
    existing: Sequence[Callable[..., Any]] | None,
    added: Sequence[Callable[..., Any]],
) -> list[Callable[..., Any]]:
    """``existing`` plus the entries of ``added`` whose activity names aren't already present.

    Temporal rejects a worker with two activities of the same name, and the harness's
    activities are exactly the ones a worker written before this plugin registered by hand —
    so a half-migrated worker (or one that passes ``CODE_MODE_ACTIVITIES`` explicitly) keeps
    working instead of failing at startup. Registration is first-one-wins: an explicitly
    passed activity is never displaced by the plugin's copy of it.
    """
    merged = list(existing or [])
    seen = {name for name in (_activity_name(fn) for fn in merged) if name is not None}
    for fn in added:
        name = _activity_name(fn)
        if name is not None and name in seen:
            continue
        if name is not None:
            seen.add(name)
        merged.append(fn)
    return merged


def _data_converter(
    converter: DataConverter | None, *, offload: ExternalStorage | None
) -> DataConverter:
    """Fill in the harness's data-converter requirements without displacing anything set.

    A Pydantic payload converter is installed only when the converter is still the SDK
    default: an AI-SDK plugin's converter is already Pydantic-compatible (OpenAI's
    ``OpenAIPayloadConverter`` wraps a Pydantic JSON converter), and overwriting it would
    break that SDK. Likewise ``offload`` is applied only when the converter has no external
    storage of its own, so a caller who built their own keeps it.
    """
    if converter is None:
        converter = DataConverter(payload_converter_class=PydanticPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        converter = dataclasses.replace(
            converter, payload_converter_class=PydanticPayloadConverter
        )
    if offload is not None and converter.external_storage is None:
        converter = dataclasses.replace(converter, external_storage=offload)
    return converter
