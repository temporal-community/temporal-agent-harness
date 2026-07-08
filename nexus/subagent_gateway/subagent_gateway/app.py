# ABOUTME: Inbound A2A gateway FastAPI app — real A2A HTTP+JSON-RPC(+SSE) fronting one harness
# subagent. The on-ramp for non-Temporal callers (ADK, CrewAI, etc); Temporal-to-Temporal
# callers use NexusTransport directly instead. No push notifications, file parts, history
# pagination, batching, or auth — add auth in front before exposing this publicly.

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from temporalio.client import Client

from temporal_agent_harness.harness.agent_protocol import AgentEventType

from subagents.registry.agent_registry_service import AgentElement

from . import a2a_types as a2a
from . import nexus_ops
from .config import GatewayConfig
from .translate import (
    agent_event_to_a2a_stream_event,
    incoming_message_to_internal,
    internal_task_to_a2a,
)


def _error_response(id_: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        a2a.JSONRPCErrorResponse(
            id=id_, error=a2a.JSONRPCError(code=code, message=message)
        ).model_dump(by_alias=True)
    )


def _success_response(id_: Any, result: dict) -> JSONResponse:
    return JSONResponse(a2a.JSONRPCSuccessResponse(id=id_, result=result).model_dump(by_alias=True))


def build_agent_card(config: GatewayConfig, agent: AgentElement) -> a2a.AgentCard:
    return a2a.AgentCard(
        name=agent.agent_key,
        description=agent.description or f"Agent {agent.agent_key!r}, fronted via Nexus.",
        url=config.public_url,
        skills=[
            a2a.AgentSkill(id=h.name, name=h.name, description=h.description)
            for h in agent.handlers
        ],
    )


async def _poll_until_terminal(client: Client, endpoint: str, task_id: str) -> a2a.Task:
    """Drain pollTaskUpdates until terminal/closed, then return a fresh snapshot — for
    message/send's ``blocking`` mode."""
    cursor = 0
    while True:
        polled = await nexus_ops.poll_task_updates(client, endpoint, task_id, cursor)
        for item in polled.items:
            event = nexus_ops.decode_stream_item(item)
            if event.event.type in (AgentEventType.TURN_END, AgentEventType.ERROR):
                task = await nexus_ops.get_task(client, endpoint, task_id)
                return internal_task_to_a2a(task)
        cursor = polled.next_offset
        if polled.closed:
            task = await nexus_ops.get_task(client, endpoint, task_id)
            return internal_task_to_a2a(task)


async def _sse_frames(
    client: Client, endpoint: str, task_id: str, request_id: Any, *, from_offset: int = 0
) -> AsyncIterator[str]:
    """SSE body for message/stream and tasks/resubscribe: poll, translate, emit each event as a
    JSON-RPC SSE frame until a final status event or the stream closes."""
    cursor = from_offset
    while True:
        polled = await nexus_ops.poll_task_updates(client, endpoint, task_id, cursor)
        for item in polled.items:
            event = nexus_ops.decode_stream_item(item)
            a2a_event = agent_event_to_a2a_stream_event(
                event, task_id=task_id, context_id=task_id
            )
            if a2a_event is None:
                continue
            frame = a2a.JSONRPCSuccessResponse(
                id=request_id, result=a2a_event.model_dump(by_alias=True)
            )
            yield f"data: {frame.model_dump_json(by_alias=True)}\n\n"
            if isinstance(a2a_event, a2a.TaskStatusUpdateEvent) and a2a_event.final:
                return
        cursor = polled.next_offset
        if polled.closed:
            return


def create_app(
    *, client: Client, config: GatewayConfig, agent: AgentElement, default_handler: str
) -> FastAPI:
    """Build the gateway's FastAPI app. ``agent``/``default_handler`` are already resolved
    (see config.resolve_agent)."""
    app = FastAPI(title=f"A2A gateway: {agent.agent_key}")
    card = build_agent_card(config, agent)

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> dict:
        return card.model_dump(by_alias=True, exclude_none=True)

    @app.post("/")
    async def rpc(request: Request) -> Any:
        try:
            body = await request.json()
            req = a2a.JSONRPCRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001 — malformed request, not a handler bug
            return _error_response(None, a2a.INVALID_REQUEST, f"invalid JSON-RPC request: {exc}")

        try:
            if req.method == "message/send":
                params = a2a.MessageSendParams.model_validate(req.params)
                task_id = params.message.task_id or str(uuid.uuid4())
                internal_msg = incoming_message_to_internal(
                    params.message, task_id=task_id, default_handler=default_handler
                )
                task = await nexus_ops.send_message(client, agent.endpoint, internal_msg)
                if params.configuration and params.configuration.blocking:
                    a2a_task = await _poll_until_terminal(client, agent.endpoint, task_id)
                else:
                    a2a_task = internal_task_to_a2a(task)
                return _success_response(req.id, a2a_task.model_dump(by_alias=True))

            if req.method == "message/stream":
                params = a2a.MessageSendParams.model_validate(req.params)
                task_id = params.message.task_id or str(uuid.uuid4())
                internal_msg = incoming_message_to_internal(
                    params.message, task_id=task_id, default_handler=default_handler
                )
                sent = await nexus_ops.send_message(client, agent.endpoint, internal_msg)
                return StreamingResponse(
                    _sse_frames(
                        client,
                        agent.endpoint,
                        task_id,
                        req.id,
                        from_offset=sent.stream_head_offset,
                    ),
                    media_type="text/event-stream",
                )

            if req.method == "tasks/get":
                params = a2a.TaskIdParams.model_validate(req.params)
                task = await nexus_ops.get_task(client, agent.endpoint, params.id)
                return _success_response(req.id, internal_task_to_a2a(task).model_dump(by_alias=True))

            if req.method == "tasks/cancel":
                params = a2a.TaskIdParams.model_validate(req.params)
                task = await nexus_ops.cancel_task(client, agent.endpoint, params.id)
                return _success_response(req.id, internal_task_to_a2a(task).model_dump(by_alias=True))

            if req.method == "tasks/resubscribe":
                params = a2a.TaskIdParams.model_validate(req.params)
                # No per-client cursor tracking, so resubscribe always replays from offset 0 —
                # a client may see events again, but never silently misses any.
                return StreamingResponse(
                    _sse_frames(client, agent.endpoint, params.id, req.id),
                    media_type="text/event-stream",
                )

            return _error_response(req.id, a2a.METHOD_NOT_FOUND, f"unknown method {req.method!r}")
        except ValidationError as exc:
            return _error_response(req.id, a2a.INVALID_PARAMS, str(exc))
        except LookupError as exc:
            return _error_response(req.id, a2a.TASK_NOT_FOUND, str(exc))

    return app
