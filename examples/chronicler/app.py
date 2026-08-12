"""Chronicler example FastAPI app using the packaged harness web API."""

from pathlib import Path

from fastapi import FastAPI

from examples.chronicler.audio_api import router as chronicler_audio_router
from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app


_UI_DIST = Path(__file__).with_name("ui_dist")


def create_app(
    *, registry: AgentRegistry | None = None, static_dir: Path | None = None
) -> FastAPI:
    """Create the Chronicler UI/API with its example-only audio routes."""
    resolved_static_dir = _UI_DIST if static_dir is None else static_dir
    if registry is None:
        app = create_agent_harness_app(
            registry_path=Path(__file__).with_name("agents.toml"),
            static_dir=resolved_static_dir,
        )
    else:
        app = create_agent_harness_app(registry=registry, static_dir=resolved_static_dir)
    app.include_router(chronicler_audio_router)
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
