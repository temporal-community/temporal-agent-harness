import asyncio
import json

import httpx
import pytest
from openai_fixture import function_events, response_events

from sdlc_builder.instructions import decision_prompt
from sdlc_builder.integrations.openai import (
    OpenAIIntegration,
    ProfileModel,
    make_agent,
    model_reference,
    reference_profile,
    run_agent,
)
from sdlc_builder.models import ModelRequest, Profile, TaskInput
from sdlc_builder.provider_checks import check_profile

FAKE_KEY = "sk-fake-connection-test-secret"


@pytest.fixture
def profile(monkeypatch):
    monkeypatch.setenv("SDLC_CHECK_KEY", FAKE_KEY)
    return Profile(
        id="test",
        label="Test",
        provider="openai",
        model="gpt-5.6-luna",
        credential="env:SDLC_CHECK_KEY",
    )


@pytest.fixture
def provider_http(monkeypatch):
    from openai import AsyncOpenAI

    requests = []
    response = {
        "status": 200,
        "body": None,
        "action": {"kind": "finish", "summary": "Connection ready"},
    }

    def handle(request):
        requests.append(request)
        if response["body"] is not None:
            return httpx.Response(response["status"], json=response["body"])
        body = json.loads(request.content)
        assert request.url.path.endswith("/responses")
        assert body["stream"] is True
        assert body["text"]["format"]["type"] == "json_schema"
        if body.get("tools") and not any(
            i.get("type") == "function_call_output" for i in body["input"]
        ):
            return httpx.Response(
                200,
                content=function_events(
                    "connection_probe", {}, body["model"], text_config=body["text"]
                ),
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            content=response_events(
                response["action"], body["model"], text_config=body["text"]
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    def client_factory(**kwargs):
        return AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )

    monkeypatch.setattr("sdlc_builder.integrations.openai.AsyncOpenAI", client_factory)
    return requests, response


async def test_probe_validates_native_tool_roundtrip_and_role_schema_without_repository_data(
    profile, provider_http
):
    requests, _ = provider_http
    result = await check_profile(profile)
    assert result.ok, result
    assert result.stage == "response"
    assert result.credential_source == "Environment variable SDLC_CHECK_KEY"
    assert len(requests) == 2
    probe = json.loads(requests[0].content)
    assert "repository-private-canary" not in requests[0].content.decode()
    request = ModelRequest(
        task=TaskInput(
            task_id="a" * 32, prompt="repository-private-canary", profile=profile
        ),
        context=[],
        step=0,
    )
    # Same workflow-side SDK runner and agent, with only the durable model stub
    # replaced for this unit test. The integration test verifies that stub too.
    actual_result = await run_agent(
        make_agent(profile, model=ProfileModel(profile)), decision_prompt(request)
    )
    assert actual_result.action.kind == "finish" and actual_result.input_tokens == 50
    actual = json.loads(requests[2].content)
    assert probe["text"]["format"]["schema"]["title"] == "RoleOutput"
    assert probe["tools"][0]["name"] == "connection_probe"
    assert any(
        i.get("type") == "function_call_output"
        for i in json.loads(requests[1].content)["input"]
    )
    assert probe["model"] == actual["model"] == "gpt-5.6-luna"
    assert probe["store"] is False and actual["store"] is False
    assert "reasoning_effort" not in probe
    assert probe.get("reasoning", {}).get("effort") != "none"
    assert "previous_response_id" not in actual
    assert FAKE_KEY not in result.model_dump_json()
    assert all(str(r.url) == result.endpoint for r in requests)


@pytest.mark.parametrize(
    ("status", "code", "title"),
    [
        (400, "invalid_request_error", "rejected boltzmann's request"),
        (401, "invalid_api_key", "rejected the API key"),
        (403, "permission_denied", "permission"),
        (404, "model_not_found", "model is unavailable"),
        (429, "insufficient_quota", "quota"),
        (429, "rate_limit_exceeded", "limiting requests"),
        (503, "overloaded_error", "unavailable"),
    ],
)
async def test_provider_details_redacted_no_retries(
    profile, provider_http, status, code, title
):
    requests, response = provider_http
    response.update(
        status=status,
        body={
            "error": {
                "code": code,
                "type": "invalid_request_error",
                "param": "tools[0].function.parameters",
                "message": f"Invalid schema for function final_result. Key: {FAKE_KEY}; masked sk-fake***secret; Authorization: Bearer another-secret",
            }
        },
    )
    result = await check_profile(profile)
    assert not result.ok and result.stage == "request"
    assert result.http_status == status and result.provider_code == code
    assert title in result.title
    assert "Invalid schema for function final_result" in result.provider_message
    assert result.parameter == "tools[0].function.parameters"
    assert FAKE_KEY not in result.model_dump_json()
    assert "sk-fake" not in result.model_dump_json()
    assert "another-secret" not in result.model_dump_json()
    assert len(requests) == 1


async def test_missing_environment_does_not_call_provider(
    profile, provider_http, monkeypatch
):
    monkeypatch.delenv("SDLC_CHECK_KEY")
    result = await check_profile(profile)
    assert not result.ok and result.stage == "credential"
    assert "SDLC_CHECK_KEY is not set" in result.title
    assert "export SDLC_CHECK_KEY" in result.actions[0]
    assert not provider_http[0]


async def test_keyring_and_environment_endpoint_override(
    profile, provider_http, monkeypatch
):
    monkeypatch.setattr("keyring.get_password", lambda service, identity: FAKE_KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.example/v1")
    profile = profile.model_copy(update={"credential": "keyring:test"})
    result = await check_profile(profile)
    assert result.ok and result.credential_source == "OS keyring"
    assert result.endpoint == "https://custom.example/v1/responses"


async def test_invalid_output_is_not_a_success(profile, provider_http):
    requests, response = provider_http
    response["action"] = {"kind": "not-an-action"}
    result = await check_profile(profile)
    assert not result.ok and result.stage == "response", result
    assert "usable action" in result.title
    assert len(requests) == 2


async def test_transport_and_timeout_do_not_leak_exception(profile, monkeypatch):
    async def fail(*args, **kwargs):
        raise httpx.ConnectError("private exception " + FAKE_KEY)

    monkeypatch.setattr(OpenAIIntegration, "probe", fail)
    result = await check_profile(profile)
    assert not result.ok and "connect" in result.title
    assert "private exception" not in result.model_dump_json()

    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(OpenAIIntegration, "probe", slow)
    monkeypatch.setattr("sdlc_builder.provider_checks.CHECK_TIMEOUT", 0.05)
    result = await check_profile(profile)
    assert not result.ok and "time" in result.title


async def test_api_auth_single_flight_no_task_or_result_persistence(
    tmp_path, monkeypatch, profile
):
    from sdlc_builder.provider_checks import ProviderCheck
    from sdlc_builder.server import create_app

    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    app = create_app()
    store = app.state.store
    store.put("profiles", profile.model_dump())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def check(item):
        calls.append(item.id)
        entered.set()
        await release.wait()
        return ProviderCheck(title="ephemeral-result-canary")

    monkeypatch.setattr("sdlc_builder.provider_checks.check_profile", check)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        url = "/api/profiles/test/test"
        assert (await client.post(url, headers={"X-SDLC": "1"})).status_code == 401
        await client.post(
            "/api/unlock",
            headers={"X-SDLC": "1"},
            json={"token": (tmp_path / "session-token").read_text()},
        )
        assert (await client.post(url)).status_code == 403
        client.headers["X-SDLC"] = "1"
        assert (
            await client.post(url, headers={"Origin": "https://evil.example"})
        ).status_code == 403
        pending = asyncio.create_task(client.post(url))
        await asyncio.wait_for(entered.wait(), 2)
        assert (await client.post(url)).status_code == 409
        assert (await client.delete("/api/profiles/test")).status_code == 409
        release.set()
        result = await pending
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        assert (await client.post(url)).status_code == 200  # lock was released
        assert (await client.post("/api/profiles/demo/test")).status_code == 400
        assert (await client.post("/api/profiles/missing/test")).status_code == 400
    assert calls == ["test", "test"]
    assert not store.all("tasks")
    assert store.get("profiles", "test") == profile.model_dump()
    assert b"ephemeral-result-canary" not in (tmp_path / "app.sqlite3").read_bytes()


async def test_native_profiles_resolve_credentials_independently(
    profile, provider_http, monkeypatch
):
    monkeypatch.setenv("SDLC_OTHER_KEY", "fake-second-profile-key")
    other = profile.model_copy(
        update={"id": "other", "credential": "env:SDLC_OTHER_KEY", "model": "gpt-other"}
    )
    assert reference_profile(model_reference(profile)) == profile
    assert FAKE_KEY not in model_reference(profile)
    results = await asyncio.gather(
        *(
            run_agent(make_agent(p, model=ProfileModel(p)), "Connection check")
            for p in [profile, other]
        )
    )
    assert all(r.action.kind == "finish" for r in results)
    requests, _ = provider_http
    assert {
        json.loads(r.content)["model"]: r.headers["authorization"] for r in requests
    } == {
        profile.model: "Bearer " + FAKE_KEY,
        other.model: "Bearer fake-second-profile-key",
    }


async def test_native_activity_failure_does_not_serialize_sdk_exception(
    profile, provider_http
):
    from temporalio.api.failure.v1 import Failure
    from temporalio.converter import DefaultFailureConverter, DefaultPayloadConverter
    from temporalio.exceptions import ApplicationError

    requests, response = provider_http
    response.update(
        status=401, body={"error": {"code": "invalid_api_key", "message": FAKE_KEY}}
    )
    with pytest.raises(ApplicationError) as raised:
        await run_agent(make_agent(profile, model=ProfileModel(profile)), "Check")
    assert len(requests) == 1
    error = raised.value
    assert error.details[0]["category"] == "authentication"
    encoded = Failure()
    DefaultFailureConverter().to_failure(error, DefaultPayloadConverter(), encoded)
    assert not encoded.HasField("cause")
    assert FAKE_KEY.encode() not in encoded.SerializeToString()
