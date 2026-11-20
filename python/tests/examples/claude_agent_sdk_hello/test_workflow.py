import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.claude_agent_sdk_hello.workflow import ClaudeAgentSDKHelloWorkflow
from temporal_agent_kit.agent_client import AgentClient
from temporal_agent_kit.agent_protocol import AgentConfig, AgentEvent, MessageHandlerEnd, ReplyDelta
from temporal_agent_kit.ai_sdks.claude_agent_sdk import (
    ClaudeAgentSDKPlugin,
    ConnectActivityInput,
    Output,
)
from temporal_agent_kit.plugin import AgentKitPlugin


class _ScriptedProcess:
    def __init__(self) -> None:
        self._output: asyncio.Queue[Output] = asyncio.Queue()
        self.closed = False

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message["type"] == "control_request":
            self._output.put_nowait({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": message["request_id"],
                    "response": {},
                },
            })
        elif message["type"] == "user":
            text = message["message"]["content"]
            for output in [
                {
                    "type": "stream_event",
                    "uuid": "delta",
                    "session_id": "test-session",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "is_error": False,
                    "num_turns": 1,
                    "session_id": "test-session",
                    "result": text,
                },
            ]:
                self._output.put_nowait(output)

    async def read_messages(self) -> AsyncIterator[Output]:
        while True:
            yield await self._output.get()

    async def end_input(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _ScriptedConnector:
    def __init__(self) -> None:
        self.processes: list[_ScriptedProcess] = []

    async def connect(self, request: ConnectActivityInput) -> _ScriptedProcess:
        process = _ScriptedProcess()
        self.processes.append(process)
        return process


async def test_ask_streams_replies_on_the_same_connection() -> None:
    connector = _ScriptedConnector()
    async with await WorkflowEnvironment.start_local() as env:
        task_queue = str(uuid4())
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ClaudeAgentSDKHelloWorkflow],
            plugins=[ClaudeAgentSDKPlugin(connector=connector), AgentKitPlugin()],
            max_cached_workflows=0,
        ):
            handle = await env.client.start_workflow(
                "ClaudeAgentSDKHello", AgentConfig(), id=task_queue, task_queue=task_queue
            )
            client = AgentClient(env.client, handle.id)
            try:
                for prompt in ("First prompt", "Second prompt"):
                    stream = await client.send_message(
                        ClaudeAgentSDKHelloWorkflow.ask.__name__,
                        {"text": prompt},
                        on_item=lambda item, _: item,
                        timeout=15,
                    )
                    events = [item async for item in stream if isinstance(item, AgentEvent)]
                    (delta,) = [item for item in events if isinstance(item.event, ReplyDelta)]
                    (reply,) = [item for item in events if isinstance(item.event, MessageHandlerEnd)]
                    assert isinstance(delta.event, ReplyDelta) and delta.event.text == prompt
                    assert isinstance(reply.event, MessageHandlerEnd) and reply.event.output == {"text": prompt}
                    assert delta.message_id == reply.message_id
                    assert len(connector.processes) == 1
                    assert not connector.processes[0].closed

                await handle.signal("close")
                await handle.result()
                assert connector.processes[0].closed
            finally:
                if not connector.processes or not connector.processes[0].closed:
                    await handle.terminate()
