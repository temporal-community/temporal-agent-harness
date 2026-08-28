"""Demo 3rd-party subagent for the Nexus-hello example.

Stands in for a real, non-harness agent -- plain HTTP, no Nexus, no Temporal client of its
own. Registered with the same Durable Tools Gateway ToolRegistryWorkflow tool_server.py's
"demo" is registered with (see register-third-party-subagent in the justfile) and reached
at call time through subagent_proxy_activity -- the gateway brokers both resource kinds.

Wire protocol (see nexus/mcp/durable_tools_gateway/registry_service_handler.py):
    POST /turns {idempotency_key, msg_type, payload, expected_turn} -> {output, turn_id, turn_number}
    POST /close -> {}

Dedupes on idempotency_key so a retried delivery (the gateway's proxy activity allows
retries, unlike the MCP proxy) returns the same reply instead of running the turn twice.

Run with (from the repo root):
    uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.subagent_server
"""

from __future__ import annotations

import itertools
import json

from fastapi import FastAPI
from pydantic import BaseModel

PORT = 8766

app = FastAPI()
_turn_counter = itertools.count(1)
_seen: dict[str, "TurnResponse"] = {}


class TurnRequest(BaseModel):
    idempotency_key: str
    msg_type: str
    payload: str
    expected_turn: int


class TurnResponse(BaseModel):
    output: dict
    turn_id: str
    turn_number: int


@app.post("/turns")
def turns(req: TurnRequest) -> TurnResponse:
    if req.idempotency_key in _seen:
        return _seen[req.idempotency_key]  # retried delivery -- same reply, not a new turn
    payload = json.loads(req.payload)
    text = payload.get("text", "")
    turn_number = next(_turn_counter)
    resp = TurnResponse(
        output={"text": f"[3rd-party subagent] you asked {text!r} -- here is a canned answer."},
        turn_id=f"turn-{turn_number}",
        turn_number=turn_number,
    )
    _seen[req.idempotency_key] = resp
    return resp


@app.post("/close")
def close() -> dict:
    return {}


if __name__ == "__main__":
    import uvicorn

    print(f"Demo 3rd-party subagent ready: http://127.0.0.1:{PORT}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT)
