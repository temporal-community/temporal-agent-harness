"""Worker for the hello-world OpenAI Agents agent.

Run from the repo root with:
    uv run --group examples python -m examples.openai_hello.worker

Hosts only the OpenAIHelloAgent workflow. Its one tool (`get_weather`) is an inline workflow
tool with no worker-side body, so there are no tool activities to register — the OpenAI Agents
plugin registers the model activities (including the streaming one) itself.

The plugin is wired for the HARNESS STREAMING PATH:
  * ``model_params.stream_to_provider=stream_to_provider`` — resolves each streamed model
    call's per-turn stream context ambiently off the running workflow, and
  * ``observer_factory=harness_observer_factory`` — turns that context into the observer that
    translates raw OpenAI events into the harness turn-stream vocabulary live.
Drop either one and streaming falls back to the plugin's plain raw-topic behavior.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    OPENAI_HELLO_TASK_QUEUE                   task queue to poll (default: openai-hello)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)

from .workflow import TASK_QUEUE, OpenAIHelloAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("OPENAI_HELLO_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    # --- Tracing -------------------------------------------------------------------------
    # Enabled HERE, in the worker: turn spans are created in the workflow and model/tool spans
    # in its activities, all of which run in this process. Enabling it in a client that merely
    # drives the agent would do nothing.
    #
    # ORDER MATTERS. setup_tracing() must run BEFORE OpenAIAgentsPlugin is constructed, because
    # use_otel_instrumentation=True validates at construction that the global tracer provider is
    # already a ReplaySafeTracerProvider — which is exactly what setup_tracing() installs.
    otel_plugins: list[object] = []
    tracing_status = "OFF (set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY to enable)"
    if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
        from temporal_agent_harness.evals import setup_tracing

        otel_plugins.append(setup_tracing(service_name="openai-hello"))
        tracing_status = (
            f"ON -> {os.environ.get('LANGFUSE_HOST', 'https://cloud.langfuse.com')}"
        )

    # Opt-in tier 2: the Agents SDK's own OpenInference instrumentation, which captures the
    # prompts and completions the harness's spans deliberately do not carry. The plugin has a
    # built-in flag for it; we only have to tell the harness it is on, so the harness stops
    # reporting the same tokens under gen_ai.usage.* and the backend does not bill them twice.
    use_otel = bool(os.environ.get("OPENAI_HELLO_SDK_TRACING")) and bool(otel_plugins)
    if use_otel:
        try:
            import openinference.instrumentation.openai_agents  # noqa: F401
        except ImportError:
            sys.exit(
                "error: OPENAI_HELLO_SDK_TRACING is set but "
                "openinference-instrumentation-openai-agents is not installed. "
                "Install it, or unset the variable to trace with harness spans only."
            )
        from temporal_agent_harness.evals.tier2 import mark_openai_agents_instrumented

        mark_openai_agents_instrumented()
        tracing_status += " +sdk-prompts"

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            # Streaming leans on activity heartbeats to notice a stuck model call; keep the
            # heartbeat timeout well under the overall start-to-close.
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            # The harness streaming seam: route streamed events to the in-flight turn.
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
        use_otel_instrumentation=use_otel,
    )

    # The OpenAI plugin supplies its own (OpenAI-aware, pydantic-compatible) data converter —
    # do NOT pass data_converter= here, it rejects a foreign one. See
    # tests/ai_sdks/openai_agents/test_plugin_composition.py.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin, *otel_plugins])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[OpenAIHelloAgentWorkflow],
        # No tool activities: get_weather is an inline workflow tool. The OpenAI model
        # activities (incl. invoke_model_activity_streaming) are registered by the plugin.
        activities=[],
    )
    print(
        f"OpenAI hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue} "
        f"tracing={tracing_status}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
