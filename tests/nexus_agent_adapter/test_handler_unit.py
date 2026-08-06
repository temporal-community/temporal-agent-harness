# ABOUTME: Fast, no-server unit tests for handler.py's pure helper logic.
#
# Run with: uv run pytest tests/nexus_agent_adapter/test_handler_unit.py -v

from __future__ import annotations

from unittest.mock import MagicMock

from temporal_agent_harness.nexus_agent_adapter.handler import (
    _is_workflow_already_completed,
)


def test_is_workflow_already_completed_true() -> None:
    err = MagicMock()
    err.__str__.return_value = (
        "rpc error: workflow execution already completed for id 'x'"
    )
    assert _is_workflow_already_completed(err) is True


def test_is_workflow_already_completed_case_insensitive() -> None:
    err = MagicMock()
    err.__str__.return_value = "Workflow Execution Already Completed"
    assert _is_workflow_already_completed(err) is True


def test_is_workflow_already_completed_false_for_unrelated_error() -> None:
    err = MagicMock()
    err.__str__.return_value = "deadline exceeded"
    assert _is_workflow_already_completed(err) is False
