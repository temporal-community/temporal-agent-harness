"""Non-workflow OpenAI Agents SDK caller for the Nexus Hello A2A agent."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    Message,
    Part,
    Role,
    SendMessageRequest,
)
from agents import Agent, Runner, function_tool
from nexus_a2a import (
    A2A_NEXUS_BINDING,
    A2A_PROTOCOL_VERSION,
    a2a_nexus_data_converter,
    create_nexus_a2a_client,
)
from temporalio.client import Client
from temporalio.envconfig import ClientConfig

AGENT_ENDPOINT = "nexus-hello-agent-endpoint"


async def main(prompt: str) -> None:
    """Let an ordinary SDK agent delegate to Nexus Hello through A2A over Nexus."""

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    connect_config = ClientConfig.load_client_connect_config()
    temporal = await Client.connect(
        **connect_config,
        data_converter=a2a_nexus_data_converter,
    )
    # In production this card normally comes from a registry. Passing a card directly
    # avoids pretending that the Nexus endpoint is an HTTP agent-card URL.
    card = AgentCard(
        name="Nexus Hello",
        description="OpenAI Agents SDK demo with Nexus tools and subagents.",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url=AGENT_ENDPOINT,
                protocol_binding=A2A_NEXUS_BINDING,
                protocol_version=A2A_PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[],
    )
    remote = create_nexus_a2a_client(temporal, card)

    @function_tool
    async def ask_nexus_hello(question: str) -> str:
        """Ask the remote Nexus Hello agent to answer a question."""

        task_id = f"external-a2a-{uuid.uuid4()}"
        final_text = ""
        streamed_text: list[str] = []
        async for event in remote.send_message(
            SendMessageRequest(
                message=Message(
                    message_id=str(uuid.uuid4()),
                    task_id=task_id,
                    context_id=task_id,
                    role=Role.ROLE_USER,
                    parts=[Part(text=question)],
                )
            )
        ):
            if event.HasField("artifact_update"):
                streamed_text.extend(
                    part.text
                    for part in event.artifact_update.artifact.parts
                    if part.HasField("text")
                )
            elif event.HasField("message"):
                final_text = "".join(
                    part.text
                    for part in event.message.parts
                    if part.HasField("text")
                )
        return final_text or "".join(streamed_text)

    caller = Agent(
        name="ExternalA2ACaller",
        instructions=(
            "You are outside Temporal. Delegate the user's request to Nexus Hello "
            "with ask_nexus_hello, then return its answer without embellishment."
        ),
        tools=[ask_nexus_hello],
    )
    try:
        result = await Runner.run(caller, prompt)
        print(result.final_output)
    finally:
        await remote.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Invoke Nexus Hello from a non-workflow OpenAI SDK agent."
    )
    parser.add_argument("prompt", help="Question for the external caller agent")
    args = parser.parse_args()
    asyncio.run(main(args.prompt))
