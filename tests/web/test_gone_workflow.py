from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app

GONE = "agent-session-gone"

# Every route that takes a workflow id and queries it. A dead id reaches all of
# them the same way, so they must answer the same way.
INTERFACE_ROUTES = [
    f"/api/agent-interface/{GONE}",
    f"/api/operator-interface/{GONE}",
    f"/api/status/{GONE}",
]


def _client() -> TestClient:
    app = create_agent_harness_app(registry=AgentRegistry())
    # The routes read app.state.temporal to build the client, and that read
    # happens before the patched constructor runs. Without it the test would
    # fail on a missing attribute instead of exercising the handler. The value
    # is never used, since AgentClient is patched to raise.
    app.state.temporal = object()
    # raise_server_exceptions=False so a propagated error surfaces as the 500 a
    # browser would see, rather than being re-raised into the test.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("route", INTERFACE_ROUTES)
def test_gone_workflow_answers_404_not_500(route: str) -> None:
    not_found = RPCError(f"workflow not found for ID: {GONE}", RPCStatusCode.NOT_FOUND, b"")

    with patch(
        "temporal_agent_harness.web.app.AgentClient",
        side_effect=not_found,
    ):
        response = _client().get(route)

    assert response.status_code == 404, f"{route} answered {response.status_code}"
    assert response.json()["error"] == "workflow_not_found"


@pytest.mark.parametrize("route", INTERFACE_ROUTES)
def test_other_rpc_failures_keep_their_500(route: str) -> None:
    """A real fault must not be laundered into a 404 by the same handler."""
    denied = RPCError("permission denied", RPCStatusCode.PERMISSION_DENIED, b"")

    with patch(
        "temporal_agent_harness.web.app.AgentClient",
        side_effect=denied,
    ):
        response = _client().get(route)

    assert response.status_code == 500, f"{route} answered {response.status_code}"
