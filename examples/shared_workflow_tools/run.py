"""Verify real subagent delegation and messages in both directions."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from temporalio.client import Client

from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .models import ANSWER, FIRST_INSTRUCTION, FOLLOWUP_INSTRUCTION, QUESTION
from .worker import NAMESPACE
from .workflow import TASK_QUEUE


async def main() -> None:
    client = await Client.connect(
        "localhost:7233", namespace=NAMESPACE, plugins=[AgentHarnessPlugin()]
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    handle = await client.start_workflow(
        "MessagingParentAgent",
        AgentConfig(),
        id=f"parent-child-{stamp}",
        task_queue=TASK_QUEUE,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": QUESTION}, expected_turn=1),
        result_type=AgentMessageReply,
        rpc_timeout=timedelta(seconds=30),
    )
    async with asyncio.timeout(60):
        while not (result := await handle.query("result")):
            await asyncio.sleep(0.2)
    assert (
        ANSWER in result
        and FIRST_INSTRUCTION in result
        and FOLLOWUP_INSTRUCTION in result
    ), result
    history = await handle.fetch_history()
    starts = [
        e
        for e in history.events
        if e.HasField("start_child_workflow_execution_initiated_event_attributes")
    ]
    assert len(starts) == 1, starts
    child_id = starts[
        0
    ].start_child_workflow_execution_initiated_event_attributes.workflow_id
    child = client.get_workflow_handle(child_id)
    assert await child.query("received_messages") == [
        FIRST_INSTRUCTION,
        FOLLOWUP_INSTRUCTION,
    ]
    await child.result()
    messages = await handle.query("received_messages")
    assert len(messages) == 2 and all(
        m["sender_workflow_id"] == child_id for m in messages
    ), messages
    print(
        json.dumps(
            {
                "workflow_id": handle.id,
                "child_workflow_id": child_id,
                "reply": result,
                "messages_each_direction": 2,
                "url": f"http://localhost:8233/namespaces/{NAMESPACE}/workflows/{handle.id}/{handle.first_execution_run_id}/history",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
