"""Chronicler example FastAPI app using the packaged harness web API."""

from pathlib import Path

from temporal_agent_harness.web import create_agent_harness_app

app = create_agent_harness_app(
    registry_path=Path(__file__).with_name("agents.toml"),
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
