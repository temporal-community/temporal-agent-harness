"""Run the HTTP subagent for the Nexus hello example.

The Durable Tools Gateway sends turns to this server. The server has no Nexus or Temporal
client.

Wire protocol (see nexus/mcp/durable_tools_gateway/registry_service_handler.py):
    POST /sessions {idempotency_key} -> {instance_id}
    POST /sessions/{instance_id}/turns -> {output, turn_id, turn_number}
    POST /sessions/{instance_id}/close -> {}

The server deduplicates retried requests by idempotency key.

Run with (from the repo root):
    uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.subagent_server
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PORT = 8766

app = FastAPI()
_start_keys: dict[str, str] = {}
_sessions: dict[str, "SessionState"] = {}


class StartRequest(BaseModel):
    idempotency_key: str


class StartResponse(BaseModel):
    instance_id: str


class TurnRequest(BaseModel):
    idempotency_key: str
    msg_type: str
    payload: str
    expected_turn: int


class TurnResponse(BaseModel):
    output: dict
    turn_id: str
    turn_number: int


@dataclass
class SessionState:
    next_turn: int = 1
    seen: dict[str, tuple[TurnRequest, TurnResponse]] = field(default_factory=dict)


@app.post("/sessions")
def start(req: StartRequest) -> StartResponse:
    instance_id = _start_keys.get(req.idempotency_key)
    if instance_id is None:
        instance_id = uuid.uuid4().hex
        _start_keys[req.idempotency_key] = instance_id
        _sessions[instance_id] = SessionState()
    return StartResponse(instance_id=instance_id)


@app.post("/sessions/{instance_id}/turns")
def turns(instance_id: str, req: TurnRequest) -> TurnResponse:
    session = _sessions.get(instance_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown subagent instance")
    prior = session.seen.get(req.idempotency_key)
    if prior is not None:
        prior_request, prior_response = prior
        if prior_request != req:
            raise HTTPException(status_code=409, detail="idempotency key reused")
        return prior_response
    if req.expected_turn != session.next_turn:
        raise HTTPException(
            status_code=409,
            detail=f"expected turn {session.next_turn}, got {req.expected_turn}",
        )
    if req.msg_type != "ask":
        raise HTTPException(status_code=400, detail=f"unknown handler {req.msg_type!r}")
    try:
        payload = json.loads(req.payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid payload JSON") from exc
    text = payload.get("text", "")
    turn_number = session.next_turn
    resp = TurnResponse(
        output={"text": f"[3rd-party subagent] you asked {text!r} -- here is a canned answer."},
        turn_id=f"{instance_id}-turn-{turn_number}",
        turn_number=turn_number,
    )
    session.seen[req.idempotency_key] = (req, resp)
    session.next_turn += 1
    return resp


@app.post("/sessions/{instance_id}/close")
def close(instance_id: str) -> dict:
    _sessions.pop(instance_id, None)
    return {}


if __name__ == "__main__":
    import uvicorn

    print(f"Demo 3rd-party subagent ready: http://127.0.0.1:{PORT}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT)
