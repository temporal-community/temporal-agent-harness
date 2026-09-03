# ABOUTME: SandboxConfig backend provider for microsandbox + OpenAI egress.
# Worker-side only — deliberately NOT imported by tools.py (harness re-imports tools inside
# the sandbox on every tool call).

import os

from temporal_agent_harness.harness.sandbox import MicrosandboxBackend

from .tools import SANDBOX, SANDBOX_BACKEND

PROVIDER_NAME = "microsandbox-openai-egress"

assert SANDBOX.backend in (PROVIDER_NAME, "local"), (
    f"tools.SANDBOX.backend ({SANDBOX.backend!r}) must be {PROVIDER_NAME!r} or 'local'"
)


async def microsandbox_openai_egress():
    """Return the run's sandbox backend — local for dev/demo, microsandbox with egress otherwise."""
    if os.environ.get("SANDBOX_BACKEND", "local").strip().lower() == "local":
        return "local"

    secrets: dict[str, str] = {}
    if key := os.environ.get("OPENAI_API_KEY", "").strip():
        secrets["OPENAI_API_KEY"] = key

    return SANDBOX_BACKEND.model_copy(
        update={
            "secrets": secrets or None,
            # ponytail: microsandbox_session does not pass network to Sandbox.create yet; wire when
            # guest apps need outbound api.openai.com from inside the microVM.
        }
    )
