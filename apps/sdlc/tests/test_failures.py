import json

import httpx
import pytest
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
from temporalio.exceptions import ActivityError, ApplicationError

from sdlc_builder.activities import decide
from sdlc_builder.failures import FAILURE_TYPE, model_failure, workflow_failure
from sdlc_builder.models import ModelRequest, Profile, TaskInput


@pytest.mark.parametrize(
    "status,code,category",
    [
        (401, "invalid_api_key", "authentication"),
        (403, "permission_error", "permission"),
        (404, "model_not_found", "model_not_found"),
        (404, "not_found_error", "not_found"),
        (429, "insufficient_quota", "quota"),
        (429, "rate_limit_exceeded", "rate_limit"),
        (429, "", "rate_limit"),
        (400, "context_length_exceeded", "context_limit"),
        (400, "unsupported_parameter", "request_rejected"),
        (422, "invalid_request_error", "request_rejected"),
        (500, "", "provider_unavailable"),
        (529, "overloaded_error", "provider_unavailable"),
        (504, "", "timeout"),
    ],
)
def test_provider_diagnostics(status, code, category):
    failure = model_failure(
        ModelHTTPError(
            status,
            "test-model",
            {
                "error": {"code": code, "message": "SECRET key and private request"},
            },
        )
    )
    assert failure.category == category
    assert failure.http_status == status
    assert failure.provider_code == code
    assert failure.actions
    assert "SECRET" not in failure.model_dump_json()


def test_unknown_codes_and_messages_are_not_reflected():
    for body in [
        {"error": {"code": "SECRET", "type": "SECRET", "message": "SECRET"}},
        {"error": "SECRET"},
        "SECRET",
        {"error": {"code": ["SECRET"], "type": {"SECRET": True}}},
    ]:
        failure = model_failure(ModelHTTPError(400, "test-model", body))
        assert failure.provider_code == ""
        assert "SECRET" not in failure.model_dump_json()
    anthropic = model_failure(
        ModelHTTPError(
            401,
            "test-model",
            {
                "error": {"type": "authentication_error", "message": "SECRET"},
            },
        )
    )
    assert anthropic.provider_code == "authentication_error"


def test_request_rejection_identifies_only_known_parameter_roots():
    failure = model_failure(
        ModelHTTPError(
            400,
            "test-model",
            {
                "error": {"param": "tools[0].SECRET", "message": "SECRET"},
            },
        )
    )
    assert "Rejected parameter: tools." in failure.explanation
    assert "SECRET" not in failure.model_dump_json()
    for param in ["SECRET", ["SECRET"], {"SECRET": True}]:
        failure = model_failure(
            ModelHTTPError(
                400,
                "test-model",
                {
                    "error": {"param": param, "message": "SECRET"},
                },
            )
        )
        assert "SECRET" not in failure.model_dump_json()


@pytest.mark.parametrize(
    "cause,category",
    [
        (httpx.ConnectError("SECRET URL"), "connection"),
        (httpx.ReadTimeout("SECRET URL"), "timeout"),
        (UnexpectedModelBehavior("SECRET response"), "invalid_response"),
    ],
)
def test_wrapped_transport_errors(cause, category):
    wrapper = ModelAPIError("test-model", "SECRET")
    wrapper.__cause__ = cause
    assert model_failure(wrapper).category == category
    assert "SECRET" not in model_failure(wrapper).model_dump_json()


def test_unknown_workspace_failure_is_not_blamed_on_provider():
    failure = workflow_failure(RuntimeError("SECRET"), "workspace")
    assert failure.category == "action_failed"
    assert "SECRET" not in failure.model_dump_json()
    assert workflow_failure(RuntimeError(), "budget").category == "budget"


async def test_activity_failure_survives_temporal_without_leaking_sdk_error(
    monkeypatch,
):
    monkeypatch.setenv("SDLC_TEST_KEY", "SECRET")

    def reject(*args, **kwargs):
        raise ModelHTTPError(
            404,
            "test-model",
            {
                "error": {
                    "code": "model_not_found",
                    "message": kwargs["provider"].client.api_key,
                },
            },
        )

    monkeypatch.setattr("pydantic_ai.models.openai.OpenAIResponsesModel", reject)
    request = ModelRequest(
        task=TaskInput(
            task_id="a" * 32,
            prompt="test",
            profile=Profile(
                id="test",
                label="Test",
                provider="openai",
                model="test-model",
                credential="env:SDLC_TEST_KEY",
            ),
        ),
        context=[],
        step=0,
    )
    with pytest.raises(ApplicationError) as raised:
        await decide(request)
    error = raised.value
    assert error.type == FAILURE_TYPE and error.non_retryable
    converter, payload_converter = DefaultFailureConverter(), DefaultPayloadConverter()
    encoded = Failure()
    converter.to_failure(error, payload_converter, encoded)
    assert not encoded.HasField("cause")
    assert b"SECRET" not in encoded.SerializeToString()
    decoded = converter.from_failure(encoded, payload_converter)
    wrapper = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test-worker",
        activity_type="decide",
        activity_id="test",
        retry_state=None,
    )
    wrapper.__cause__ = decoded
    restored = workflow_failure(wrapper, "model")
    assert restored.category == "model_not_found"
    assert restored.http_status == 404
    assert "SECRET" not in json.dumps(restored.model_dump())
