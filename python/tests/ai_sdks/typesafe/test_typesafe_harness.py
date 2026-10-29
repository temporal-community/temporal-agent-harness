"""Real Temporal executions with the canonical plugin and fake provider HTTP."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from unittest.mock import Mock

import httpx2
import pytest
from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy, Score
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy as TemporalRetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.typesafe import TypeSafePlugin
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.typesafe_harness import HarnessTypeSafe
from temporal_agent_harness.plugin import AgentHarnessPlugin


@workflow.defn
class Probe:
    @workflow.run
    async def run(self, mode: str) -> dict:
        self.events = []
        self.task = None
        proxy = HarnessTypeSafe(
            runner=self, model="requested",
            activity_config={"retry_policy": TemporalRetryPolicy(
                maximum_attempts=2, initial_interval=timedelta(milliseconds=1))},
        )
        self.task = asyncio.create_task(proxy.system_one(
            {"mode": mode},
            {"scope": Choice(criteria={"low": "small fix", "high": "feature"}),
             "size": Score(criteria=["small", "large"])},
            model="override",
        ))
        try:
            result = await self.task
            return {"response": result.response.model_dump(mode="json"),
                    "request_id": result.request_id, "events": self.events}
        except BaseException as exc:
            return {"error": type(exc).__name__, "events": self.events}

    def publish(self, event):
        self.events.append(event.model_dump(mode="json"))

    @workflow.signal
    def cancel_call(self):
        self.task.cancel()


def client_for(handler):
    return AsyncTypeSafeClient(api_key="fake", retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler))


def response():
    return {"model": "served", "usage": {"input_tokens": 10, "output_tokens": 3},
        "answers": {
            "scope": {"type": "choice", "choice": "low", "confidence": .97,
                      "probabilities": {"low": .97, "high": .03}},
            "size": {"type": "score", "score": .2, "confidence": .9,
                     "probabilities": {"0": .8, "1": .2},
                     "legend": {"0": "small", "1": "large"}}}}


@pytest.mark.parametrize("mode", ["success", "retry", "failure", "cancel"])
async def test_native_round_trip_events_and_replay(mode):
    attempts = []
    started = asyncio.Event()
    async def transport(request):
        attempts.append(json.loads(request.content))
        started.set()
        if mode == "cancel":
            await asyncio.Event().wait()
        if mode == "failure" or (mode == "retry" and len(attempts) == 1):
            return httpx2.Response(503, json={"message": "unavailable"}, request=request)
        return httpx2.Response(200, json=response(),
                              headers={"x-request-id": "req-123"}, request=request)

    async with client_for(transport) as provider:
        plugins = [TypeSafePlugin(provider), AgentHarnessPlugin(large_payload_offload=None)]
        async with await WorkflowEnvironment.start_local() as env:
            client = Client(env.client.service_client, plugins=plugins)
            queue = str(uuid.uuid4())
            async with Worker(client, task_queue=queue, workflows=[Probe],
                              workflow_runner=UnsandboxedWorkflowRunner()):
                handle = await client.start_workflow(Probe.run, mode, id=queue, task_queue=queue)
                if mode == "cancel":
                    await asyncio.wait_for(started.wait(), 20)
                    await handle.signal(Probe.cancel_call)
                result = await handle.result()
                history = await handle.fetch_history()
            await Replayer(workflows=[Probe], workflow_runner=UnsandboxedWorkflowRunner(),
                           data_converter=client.data_converter).replay_workflow(history)
    assert [event["type"] for event in result["events"]] == [
        "model_interaction_started", "model_interaction_ended"]
    assert all(event["model"] == "override" for event in result["events"])
    if mode in {"success", "retry"}:
        assert result["response"] == response()
        assert result["request_id"] == "req-123"
        assert result["events"][1]["usage"]["input_tokens"] == 10
        assert result["events"][1]["usage"]["output_tokens"] == 3
    else:
        assert "error" in result
        assert result["events"][1]["usage"] is None
    assert len(attempts) == (2 if mode in {"retry", "failure"} else 1)
    assert attempts[0]["model"] == "override"


async def test_failure_is_propagated_and_span_closed():
    runner = Mock()
    proxy = HarnessTypeSafe(runner=runner)
    async def fail(*args, **kwargs):
        raise ValueError("provider failure")
    proxy._proxy.system_one = fail
    with pytest.raises(ValueError, match="provider failure"):
        await proxy.system_one("state", {})
    assert runner.publish.call_count == 2


async def test_cancellation_is_propagated_and_span_closed():
    runner = Mock()
    proxy = HarnessTypeSafe(runner=runner)
    async def fail(*args, **kwargs):
        raise asyncio.CancelledError()
    proxy._proxy.system_one = fail
    with pytest.raises(asyncio.CancelledError):
        await proxy.system_one("state", {})
    assert runner.publish.call_count == 2


async def test_typed_example_uses_real_harness_and_sandbox():
    from examples.typesafe_hello.workflow import TypeSafeHelloAgent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig, AgentMessage, AgentMessageReply, SEND_AGENT_MESSAGE_UPDATE)
    from temporalio.contrib.workflow_streams import WorkflowStreamClient
    from temporal_agent_harness.harness.agent_protocol import AgentEvent

    def transport(request):
        body = response()
        body["answers"].pop("size")
        return httpx2.Response(200, json=body, request=request)
    async with client_for(transport) as provider:
        async with await WorkflowEnvironment.start_local() as env:
            client = Client(env.client.service_client, plugins=[TypeSafePlugin(provider),
                AgentHarnessPlugin(large_payload_offload=None)])
            queue = str(uuid.uuid4())
            async with Worker(client, task_queue=queue, workflows=[TypeSafeHelloAgent]):
                handle = await client.start_workflow(TypeSafeHelloAgent.run, AgentConfig(),
                                                     id=queue, task_queue=queue)
                await handle.execute_update(SEND_AGENT_MESSAGE_UPDATE,
                    AgentMessage(type="classify", payload={"text": "Fix typo"}),
                    result_type=AgentMessageReply)
                stream = WorkflowStreamClient.create(client, queue)
                events = []
                async with asyncio.timeout(10):
                    async for item in stream.subscribe(topics=["turn_events"], from_offset=0, result_type=AgentEvent):
                        events.append(item.data.event.type)
                        if item.data.event.type == "turn_end":
                            break
                assert events.index("model_interaction_started") < events.index("model_interaction_ended")
                await handle.cancel()
