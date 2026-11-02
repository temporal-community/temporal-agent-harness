"""The console's "Open in Temporal" link follows the Temporal the server is connected to."""

from __future__ import annotations

import pytest

from temporal_agent_harness.web.temporal_ui import temporal_workflows_url


@pytest.fixture(autouse=True)
def _no_configured_ui(monkeypatch) -> None:
    monkeypatch.delenv("TEMPORAL_UI_URL", raising=False)


@pytest.mark.parametrize(
    ("target_host", "namespace", "expected"),
    [
        ("localhost:7233", "default", "http://localhost:8233/namespaces/default/workflows"),
        ("127.0.0.1:7233", "dev", "http://localhost:8233/namespaces/dev/workflows"),
        (
            "demo.a1b2c.tmprl.cloud:7233",
            "demo.a1b2c",
            "https://cloud.temporal.io/namespaces/demo.a1b2c/workflows",
        ),
        (
            "us-west-2.aws.api.temporal.io:7233",
            "demo.a1b2c",
            "https://cloud.temporal.io/namespaces/demo.a1b2c/workflows",
        ),
        ("temporal.internal:7233", "default", None),
        (None, "default", None),
    ],
)
def test_the_ui_is_found_from_the_connection(target_host, namespace, expected) -> None:
    assert temporal_workflows_url(target_host, namespace) == expected


def test_a_configured_ui_wins_for_any_connection(monkeypatch) -> None:
    monkeypatch.setenv("TEMPORAL_UI_URL", "https://temporal-ui.example.com/")
    assert (
        temporal_workflows_url("temporal.internal:7233", "default")
        == "https://temporal-ui.example.com/namespaces/default/workflows"
    )
