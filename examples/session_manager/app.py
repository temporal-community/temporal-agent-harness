"""Example FastAPI app using the packaged harness web API."""

from temporal_agent_harness.web import create_agent_harness_app

from examples.session_manager.agent_registry import load_agent_registry

app = create_agent_harness_app(registry=load_agent_registry)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
