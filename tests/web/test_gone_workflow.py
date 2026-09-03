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


# The statuses a busy dev stack produces, and the remedy each one should be answered
# with. A throttle is not a server fault and must not arrive as one.
DEGRADED_STATUSES = [
    (RPCStatusCode.RESOURCE_EXHAUSTED, 429, "temporal_throttled"),
    (RPCStatusCode.UNAVAILABLE, 503, "temporal_unavailable"),
    (RPCStatusCode.DEADLINE_EXCEEDED, 504, "temporal_timeout"),
]


@pytest.mark.parametrize("route", INTERFACE_ROUTES)
@pytest.mark.parametrize(("status", "expected_code", "expected_error"), DEGRADED_STATUSES)
def test_degraded_temporal_answers_its_own_status(
    route: str,
    status: RPCStatusCode,
    expected_code: int,
    expected_error: str,
) -> None:
    failure = RPCError(status.name, status, b"")

    with patch(
        "temporal_agent_harness.web.app.AgentClient",
        side_effect=failure,
    ):
        response = _client().get(route)

    assert response.status_code == expected_code, (
        f"{route} answered {response.status_code} for {status.name}"
    )
    assert response.json()["error"] == expected_error


@pytest.mark.parametrize("route", INTERFACE_ROUTES)
def test_throttle_tells_the_caller_to_come_back(route: str) -> None:
    """The whole point of 429 over 500: waiting is the fix, so say how long.

    ``DEADLINE_EXCEEDED`` deliberately has no ``Retry-After`` — an immediate retry of
    something that already ran out of time is how a stuck queue gets hammered.
    """
    throttled = RPCError(
        "namespace rate limit exceeded",
        RPCStatusCode.RESOURCE_EXHAUSTED,
        b"",
    )

    with patch(
        "temporal_agent_harness.web.app.AgentClient",
        side_effect=throttled,
    ):
        response = _client().get(route)

    assert response.headers["Retry-After"] == "1"
