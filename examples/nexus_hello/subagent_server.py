"""Run the demo third-party writer as a standard A2A HTTP agent."""

from __future__ import annotations

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from fastapi import FastAPI
from google.protobuf.timestamp_pb2 import Timestamp

PORT = 8766
BASE_URL = f"http://127.0.0.1:{PORT}"

AGENT_CARD = AgentCard(
    name="Writer HTTP",
    description="A minimal third-party writing agent served using standard A2A HTTP+JSON.",
    version="1.0.0",
    supported_interfaces=[
        AgentInterface(
            url=BASE_URL, protocol_binding="HTTP+JSON", protocol_version="1.0"
        )
    ],
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    skills=[
        AgentSkill(
            id="ask",
            name="ask",
            description="Ask the writer agent for prose.",
            tags=["writing"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        )
    ],
)


class WriterExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id is not None
        assert context.context_id is not None
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:
            timestamp = Timestamp()
            timestamp.GetCurrentTime()
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_SUBMITTED,
                        timestamp=timestamp,
                    ),
                    history=[context.message] if context.message is not None else [],
                )
            )
        await updater.start_work()
        text = context.get_user_input()
        await updater.add_artifact(
            [
                Part(
                    text=(
                        f"[3rd-party A2A agent] you asked {text!r} -- "
                        "here is a canned writing answer."
                    )
                )
            ],
            name="answer",
            last_chunk=True,
        )
        # One long-lived A2A task is one UI/harness session. It waits for the
        # next message rather than completing after each conversational turn.
        await updater.requires_input()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id is not None
        assert context.context_id is not None
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


handler = DefaultRequestHandler(
    agent_executor=WriterExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=AGENT_CARD,
)
app = FastAPI()
app.router.routes.extend(create_agent_card_routes(AGENT_CARD))
app.router.routes.extend(create_rest_routes(handler))


if __name__ == "__main__":
    import uvicorn

    print(f"Demo third-party A2A agent ready: {BASE_URL}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT)
