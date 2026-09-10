"""Send a repository question and print the resulting harness events as JSON lines."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from urllib.parse import urlencode
import uuid

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig, AgentEvent

from .workflow import TASK_QUEUE, WORKFLOW_NAME


DEFAULT_QUESTION = (
    "Inspect this repository with read-only commands. What are its main entry points, "
    "and where are the tests? Cite the files you inspected."
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="?")
    parser.add_argument("--id", help="Existing or new session ID; defaults to a new session")
    parser.add_argument(
        "--task-queue", default=os.environ.get("CODEX_HELLO_TASK_QUEUE", TASK_QUEUE),
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Replay and follow an existing session; use --id and omit the message",
    )
    args = parser.parse_args()
    if args.watch and (not args.id or args.message is not None):
        parser.error("--watch requires --id and does not accept a message")
    workflow_id = args.id or f"codex-hello-{uuid.uuid4().hex[:12]}"
    connect_config = ClientConfig.load_client_connect_config()
    connect_config.setdefault("target_host", "localhost:7233")
    client = await Client.connect(**connect_config, data_converter=pydantic_data_converter)
    agent_client = AgentClient(client, workflow_id)
    print(f"Session: {workflow_id}", file=sys.stderr)
    print(f"UI: http://localhost:8000/?{urlencode({'s': workflow_id})}", file=sys.stderr)
    if args.watch:
        stream = await agent_client.attach(on_item=lambda item, _offset: item)
    else:
        await client.start_workflow(
            WORKFLOW_NAME,
            AgentConfig(),
            id=workflow_id,
            task_queue=args.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        status = await agent_client.get_status()
        expected_turn = status.current_turn + len(status.pending_turns) + 1
        stream = await agent_client.send_message(
            "ask", {"text": args.message or DEFAULT_QUESTION}, expected_turn=expected_turn,
            on_item=lambda item, _offset: item, timeout=None,
        )
    async for item in stream:
        if isinstance(item, AgentEvent):
            print(item.model_dump_json(), flush=True)
        else:
            raise SystemExit(str(item))


if __name__ == "__main__":
    asyncio.run(main())
