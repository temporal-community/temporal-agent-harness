# ABOUTME: One-call setup for harness tracing — build a replay-safe tracer provider, point it
# at an OTLP backend (Langfuse by default), and hand back the Temporal plugin that makes spans
# work inside workflows.
#
# There are exactly two things an application must do to get traces:
#
#     from temporal_agent_harness.evals import setup_tracing
#
#     plugin = setup_tracing()                      # 1. install a replay-safe provider
#     client = await Client.connect(..., plugins=[plugin])   # 2. put the plugin on the CLIENT
#
# The plugin MUST go on the client, not the worker: workers built from a client inherit its
# plugins, and registering it in both places double-instruments every call. Everything else —
# the turn/model/tool spans themselves — is already wired into the harness and lights up as
# soon as a replay-safe provider exists (see ``harness/tracing.py``).

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from temporalio.contrib.opentelemetry import OpenTelemetryPlugin

DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"
DEFAULT_SERVICE_NAME = "temporal-agent-harness"

# Langfuse's OTLP receiver. The Authorization header is Basic auth over the project keys; the
# ingestion-version header is required by the endpoint.
_LANGFUSE_OTEL_PATH = "/api/public/otel/v1/traces"
_LANGFUSE_INGESTION_VERSION = "4"


def langfuse_headers(
    *, public_key: str | None = None, secret_key: str | None = None
) -> dict[str, str]:
    """Auth headers for Langfuse's OTLP endpoint, from args or ``LANGFUSE_*`` env vars.

    Raises if the keys are missing rather than silently producing a provider that drops every
    span — a misconfigured exporter is otherwise entirely invisible until you go looking for
    traces that never arrived.
    """
    public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        raise ValueError(
            "Langfuse credentials missing: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY "
            "(or pass public_key=/secret_key=). To trace somewhere else, pass your own "
            "span_processor= to setup_tracing()."
        )
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "x-langfuse-ingestion-version": _LANGFUSE_INGESTION_VERSION,
    }


def langfuse_span_processor(
    *,
    host: str | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
) -> SpanProcessor:
    """A batching OTLP/HTTP span processor pointed at Langfuse.

    ``host`` defaults to ``$LANGFUSE_HOST`` and then to Langfuse Cloud; point it at
    ``http://localhost:3000`` for a self-hosted instance.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    host = (host or os.environ.get("LANGFUSE_HOST") or DEFAULT_LANGFUSE_HOST).rstrip("/")
    exporter = OTLPSpanExporter(
        endpoint=f"{host}{_LANGFUSE_OTEL_PATH}",
        headers=langfuse_headers(public_key=public_key, secret_key=secret_key),
    )
    return BatchSpanProcessor(exporter)


def setup_tracing(
    *,
    span_processor: SpanProcessor | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    set_global: bool = True,
) -> OpenTelemetryPlugin:
    """Install a replay-safe tracer provider and return the Temporal plugin to pass to a client.

    ``span_processor`` defaults to :func:`langfuse_span_processor`; pass your own to export
    somewhere else (or an ``InMemorySpanExporter``-backed one in tests). ``set_global=False``
    skips installing the provider globally, for a caller that manages that itself.

    The provider comes from Temporal's ``create_tracer_provider()``, which is what makes spans
    created in workflow code replay-safe — see the module docstring of ``harness/tracing.py``
    for why that matters. ``add_temporal_spans`` is left at its default of ``False`` so the
    trace contains the agent's semantics (turns, model calls, tools) rather than Temporal's
    plumbing (StartActivity, RunWorkflow); the goal is a trace an eval backend can read, not an
    infrastructure APM view.
    """
    import opentelemetry.trace
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from temporalio.contrib.opentelemetry import (
        OpenTelemetryPlugin,
        create_tracer_provider,
    )

    provider = create_tracer_provider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(span_processor or langfuse_span_processor())
    if set_global:
        opentelemetry.trace.set_tracer_provider(provider)
    return OpenTelemetryPlugin()


__all__ = [
    "DEFAULT_LANGFUSE_HOST",
    "DEFAULT_SERVICE_NAME",
    "langfuse_headers",
    "langfuse_span_processor",
    "setup_tracing",
]
