"""Start and verify the shared workflow tools example."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .models import ANSWER, NOTICE, QUESTION
from .worker import NAMESPACE
from .workflow import MAILBOX_ID, TASK_QUEUE


async def main() -> None:
    client = await Client.connect(
        "localhost:7233", namespace=NAMESPACE, plugins=[AgentHarnessPlugin()]
    )
    try:
        await client.start_workflow(
            "SharedToolsMailbox",
            "openai",
            id=MAILBOX_ID,
            task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    handle = await client.start_workflow(
        "SharedToolsOpenAIAgent",
        AgentConfig(),
        id=f"{TASK_QUEUE}-{stamp}",
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
    assert ANSWER in result, result
    messages = await client.get_workflow_handle(MAILBOX_ID).query("messages")
    assert any(
        m["body"] == NOTICE and m["sender_workflow_id"] == handle.id for m in messages
    ), messages
    history = await handle.fetch_history()
    assert any(
        e.HasField("child_workflow_execution_completed_event_attributes")
        for e in history.events
    )
    assert any(
        e.HasField("external_workflow_execution_signaled_event_attributes")
        for e in history.events
    )
    print(
        json.dumps(
            {
                "workflow_id": handle.id,
                "reply": result,
                "mailbox_messages": len(messages),
                "url": f"http://localhost:8233/namespaces/{NAMESPACE}/workflows/"
                f"{handle.id}/{handle.first_execution_run_id}/history",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
