"""Opt-in real Temporal/browser API contract test. No provider keys or model spend."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from openai_fixture import function_events, response_events

pytestmark = pytest.mark.skipif(
    os.environ.get("SDLC_INTEGRATION") != "1",
    reason="Set SDLC_INTEGRATION=1 to launch the local stack",
)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(fn, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if predicate(last):
            return last
        time.sleep(0.15)
    raise AssertionError(f"Timed out. Last response: {last}")


async def assert_state_stream_matches_query(
    address, workflow_id, snapshot, payload_dir
):
    import jsonpatch
    from temporal_agent_harness.harness.agent_client import AgentClient
    from temporal_agent_harness.plugin import AgentHarnessPlugin
    from temporal_agent_harness.utils.large_payload import local_payload_storage
    from temporalio.client import Client

    client = await Client.connect(
        address,
        plugins=[
            AgentHarnessPlugin(
                large_payload_offload=local_payload_storage(base_dir=payload_dir)
            )
        ],
    )
    stream = await AgentClient(client, workflow_id).attach(
        on_item=lambda item, _offset: item
    )
    document = None
    version = -1
    tool_events = 0
    async with asyncio.timeout(10):
        async for item in stream:
            event = item.event
            if (
                event.type in {"state_snapshot", "state_patch"}
                and event.state_id != "sdlc"
            ):
                continue
            if event.type == "state_snapshot":
                document, version = event.value, event.version
            elif event.type == "state_patch":
                assert event.version == version + 1
                document = jsonpatch.apply_patch(document, event.ops)
                version = event.version
            elif event.type == "tool_start":
                tool_events += 1
    assert tool_events >= 5
    assert version == snapshot["version"]
    assert document == snapshot["value"]


async def assert_native_model_path(address, workflow_id, payload_dir):
    from temporal_agent_harness.harness.agent_client import AgentClient
    from temporal_agent_harness.plugin import AgentHarnessPlugin
    from temporal_agent_harness.utils.large_payload import local_payload_storage
    from temporalio.client import Client

    client = await Client.connect(
        address,
        plugins=[
            AgentHarnessPlugin(
                large_payload_offload=local_payload_storage(base_dir=payload_dir)
            )
        ],
    )
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    activity_types = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert activity_types.count("invoke_model_activity_streaming") == 3
    assert "decide" not in activity_types
    assert "fake-local-test-key" not in history.to_json()
    stream = await AgentClient(client, workflow_id).attach(on_item=lambda item, _: item)
    events = []
    async with asyncio.timeout(10):
        async for item in stream:
            events.append(item.event)
    starts = [e for e in events if e.type == "model_interaction_started"]
    ends = [e for e in events if e.type == "model_interaction_ended"]
    assert len(starts) == len(ends) == 3
    assert all(e.model == "fixture" for e in starts + ends)
    assert any(e.type == "reply_delta" for e in events)
    assert sum(e.usage.input_tokens for e in ends if e.usage) == 150


def test_real_lifecycle_and_restart(tmp_path):
    port, temporal_port = free_port(), free_port()
    log = (tmp_path / "app.log").open("w")
    command = [
        sys.executable,
        "-m",
        "sdlc_builder.cli",
        "--data-dir",
        str(tmp_path / "data"),
        "--port",
        str(port),
        "--temporal-port",
        str(temporal_port),
    ]
    process = None
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"X-SDLC": "1"}, timeout=15
    )
    decisions = [
        {
            "kind": "write",
            "summary": "Attempt an edit in Ask mode",
            "path": "unapproved.txt",
            "content": "must not be written",
        },
        {
            "kind": "check",
            "summary": "Attempt a command in Ask mode",
            "argv": ["python3", "-c", "open('unapproved.txt','w').write('bad')"],
        },
        {
            "kind": "finish",
            "summary": "Ask mode correctly refused editing and command execution.",
        },
    ]
    requests = []

    class ProviderStub(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body.get("tools") and body["model"] != "missing-model":
                if any(i.get("type") == "function_call_output" for i in body["input"]):
                    encoded = response_events(
                        {"summary": "Connection ready"},
                        body["model"],
                        text_config=body["text"],
                    )
                else:
                    encoded = function_events(
                        "connection_probe", {}, body["model"], text_config=body["text"]
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            requests.append(body)
            if body["model"] == "missing-model":
                encoded = json.dumps(
                    {
                        "error": {
                            "code": "model_not_found",
                            "message": "fake-local-test-key must never reach task state",
                        }
                    }
                ).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            action = decisions[min(len(requests) - 1, 2)]
            if body["model"] == "change-fixture":
                step = sum(r["model"] == "change-fixture" for r in requests) - 1
                action = [
                    {
                        "kind": "plan",
                        "summary": "Add a small note and verify it",
                        "tasks": ["Add note", "Run check", "Review"],
                        "risks": [],
                    },
                    {
                        "kind": "write",
                        "summary": "Add note",
                        "path": "sdk-note.txt",
                        "content": "SDK integration works\n",
                        "completed_tasks": [0],
                    },
                    {
                        "kind": "check",
                        "summary": "Verify the note",
                        "argv": [
                            "python3",
                            "-c",
                            "from pathlib import Path; assert Path('sdk-note.txt').read_text() == 'SDK integration works\\n'",
                        ],
                        "completed_tasks": [0],
                    },
                    {
                        "kind": "finish",
                        "summary": "Note added and verified",
                        "completed_tasks": [0, 1],
                        "active_task": 2,
                    },
                ][min(step, 3)]
            elif body["model"] == "conversation-fixture":
                content = body["input"][0]["content"]
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content)
                prompt = json.loads(content)
                step = prompt["step"] - 1
                if prompt["mode"] == "ask":
                    action = [
                        {
                            "kind": "read",
                            "path": "sdk-note.txt",
                            "summary": "Read the existing note",
                        },
                        {
                            "kind": "write",
                            "path": "forbidden.txt",
                            "content": "bad",
                            "summary": "Attempt an Ask edit",
                        },
                        {
                            "kind": "check",
                            "argv": [
                                "python3",
                                "-c",
                                "raise Exception('must not run')",
                            ],
                            "summary": "Attempt an Ask command",
                        },
                        {
                            "kind": "finish",
                            "summary": "Answered using the existing note and conversation",
                        },
                    ][min(step, 3)]
                else:
                    note = prompt["request"] + "\n"
                    latest_read = next(
                        (
                            json.loads(item["text"])
                            for item in reversed(prompt["history"])
                            if item["role"] == "tool" and '"hash"' in item["text"]
                        ),
                        {},
                    )
                    action = [
                        {
                            "kind": "write",
                            "path": "forbidden.txt",
                            "content": "bad",
                            "summary": "Attempt edit before new approval",
                        },
                        {
                            "kind": "plan",
                            "summary": "Refine the existing note",
                            "tasks": ["Read note", "Refine note", "Verify", "Review"],
                        },
                        {
                            "kind": "read",
                            "path": "sdk-note.txt",
                            "summary": "Read the current file hash",
                        },
                        {
                            "kind": "write",
                            "path": "sdk-note.txt",
                            "expected_hash": latest_read.get("hash", ""),
                            "content": note,
                            "summary": "Refine note",
                            "completed_tasks": [0, 1],
                        },
                        {
                            "kind": "check",
                            "argv": [
                                "python3",
                                "-c",
                                f"from pathlib import Path; assert Path('sdk-note.txt').read_text() == {note!r}",
                            ],
                            "summary": "Verify refined note",
                            "completed_tasks": [0, 1, 2],
                        },
                        {
                            "kind": "finish",
                            "summary": "Refinement verified",
                            "completed_tasks": [0, 1, 2],
                        },
                    ][min(step, 5)]
            assert self.path == "/v1/responses"
            assert body["stream"] is True and body["store"] is False
            assert body["text"]["format"]["type"] == "json_schema"
            encoded = response_events(action, body["model"], text_config=body["text"])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    provider_server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderStub)
    provider_thread = threading.Thread(
        target=provider_server.serve_forever, daemon=True
    )
    provider_thread.start()

    def launch():
        nonlocal process
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            env={**os.environ, "SDLC_FIXTURE_KEY": "fake-local-test-key"},
            start_new_session=True,
        )
        for _ in range(150):
            if process.poll() is not None:
                pytest.fail((tmp_path / "app.log").read_text())
            try:
                if client.get("/").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail(
                "Server did not become ready: " + (tmp_path / "app.log").read_text()
            )
        token = (tmp_path / "data" / "session-token").read_text().strip()
        assert client.post("/api/unlock", json={"token": token}).status_code == 200

    def stop():
        if process and process.poll() is None:
            # Simulate terminal Ctrl-C, including any child that incorrectly
            # shares the app's process group. The owned Temporal server must
            # remain alive until worker shutdown finishes.
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=20)

    def post(path, payload):
        if path == "/api/tasks":
            payload = {
                **payload,
                "engine": "v1",
            }  # Preserve coverage of existing histories.
        response = client.post(path, json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    try:
        launch()
        project = post("/api/demo", {})
        identity = uuid.uuid4().hex
        payload = {
            "id": identity,
            "project_id": project["id"],
            "profile_id": "demo",
            "prompt": "Fix greeting test",
            "mode": "change",
        }
        task = post("/api/tasks", payload)
        assert post("/api/tasks", payload)["id"] == identity
        assert task["workspace"] != project["path"]

        def get():
            response = client.get("/api/tasks/" + identity)
            assert response.status_code == 200, response.text
            return response.json()

        plan = wait_for(
            get,
            lambda t: (
                t.get("snapshot")
                and (t["snapshot"]["value"].get("gate") or {}).get("kind") == "plan"
            ),
        )
        before = plan["snapshot"]
        assert plan["snapshot"]["value"]["steps"][0]["status"] == "completed"
        assert "hello" in (Path(task["workspace"]) / "greeting.py").read_text()
        assert (
            client.put(
                f"/api/tasks/{identity}/file",
                json={"path": "greeting.py", "content": "bad", "expected_hash": ""},
            ).status_code
            == 409
        )
        # Restart both worker and persistent Temporal at a durable approval gate.
        stop()
        launch()
        recovered = wait_for(get, lambda t: t.get("live"))
        assert recovered["snapshot"] == before
        post(
            f"/api/tasks/{identity}/control",
            {
                "command": "respond",
                "gate_id": before["value"]["gate"]["id"],
                "approved": True,
            },
        )
        check = wait_for(
            get,
            lambda t: (
                (t["snapshot"]["value"].get("gate") or {}).get("kind") == "command"
            ),
        )
        # Stale approval cannot approve the next gate.
        assert (
            client.post(
                f"/api/tasks/{identity}/control",
                json={"command": "respond", "gate_id": before["value"]["gate"]["id"]},
            ).status_code
            == 409
        )
        post(
            f"/api/tasks/{identity}/control",
            {
                "command": "respond",
                "gate_id": check["snapshot"]["value"]["gate"]["id"],
                "approved": True,
            },
        )
        review = wait_for(get, lambda t: t["snapshot"]["value"]["status"] == "review")
        assert review["snapshot"]["value"]["checks"][0]["exit_code"] == 0
        assert all(
            s["status"] == "completed" for s in review["snapshot"]["value"]["steps"][:3]
        )
        asyncio.run(
            assert_state_stream_matches_query(
                f"127.0.0.1:{temporal_port}",
                task["workflow_id"],
                review["snapshot"],
                tmp_path / "data" / "payloads",
            )
        )
        patch = client.get(f"/api/tasks/{identity}/patch")
        assert '+    return f"Hello, {name}!"' in patch.text
        assert (
            Path(project["path"]) / "greeting.py"
        ).read_text() == 'def greet(name):\n    return f"hello {name}"\n'
        assert client.get("/harness/").status_code == 200
        assert any(
            t["workflow_id"] == task["workflow_id"]
            for t in client.get("/harness/api/sessions").json()
        )
        file = client.get(
            f"/api/tasks/{identity}/file", params={"path": "greeting.py"}
        ).json()
        edited = client.put(
            f"/api/tasks/{identity}/file",
            json={
                "path": "greeting.py",
                "expected_hash": file["hash"],
                "content": file["content"] + "\n# Manual review note\n",
            },
        )
        assert edited.status_code == 200, edited.text
        stale = wait_for(get, lambda t: t["snapshot"]["value"]["checks"][0]["stale"])
        assert stale["snapshot"]["value"]["status"] == "review"
        post(f"/api/tasks/{identity}/control", {"command": "accept"})
        wait_for(get, lambda t: t["snapshot"]["value"]["status"] == "accepted")
        followup = post(
            "/api/tasks",
            {
                **payload,
                "id": uuid.uuid4().hex,
                "parent_task_id": identity,
                "prompt": "Explain the fix",
                "mode": "ask",
            },
        )
        assert (Path(followup["workspace"]) / "greeting.py").read_text() == (
            Path(task["workspace"]) / "greeting.py"
        ).read_text()
        profile = post(
            "/api/profiles",
            {
                "label": "Local HTTP fixture",
                "provider": "openai",
                "model": "fixture",
                "base_url": f"http://127.0.0.1:{provider_server.server_port}/v1",
                "env_var": "SDLC_FIXTURE_KEY",
                "max_steps": 3,
            },
        )
        ask = post(
            "/api/tasks",
            {
                **payload,
                "id": uuid.uuid4().hex,
                "profile_id": profile["id"],
                "mode": "ask",
                "prompt": "Explain the greeting",
            },
        )
        asked = wait_for(
            lambda: client.get("/api/tasks/" + ask["id"]).json(),
            lambda t: (
                (t.get("snapshot") or {}).get("value", {}).get("status")
                in {"review", "failed"}
            ),
        )
        assert asked["snapshot"]["value"]["status"] == "review", asked
        assert len(requests) == 3
        assert "Ask is read-only" in json.dumps(requests[-1])
        assert not (Path(ask["workspace"]) / "unapproved.txt").exists()
        assert asked["snapshot"]["value"]["input_tokens"] == 150
        asyncio.run(
            assert_native_model_path(
                f"127.0.0.1:{temporal_port}",
                ask["workflow_id"],
                tmp_path / "data" / "payloads",
            )
        )
        failed_profile = post(
            "/api/profiles",
            {
                "label": "Rejected model fixture",
                "provider": "openai",
                "model": "missing-model",
                "base_url": f"http://127.0.0.1:{provider_server.server_port}/v1",
                "env_var": "SDLC_FIXTURE_KEY",
            },
        )
        failed_task = post(
            "/api/tasks",
            {
                **payload,
                "id": uuid.uuid4().hex,
                "profile_id": failed_profile["id"],
                "mode": "ask",
                "prompt": "Explain the greeting",
            },
        )
        failed = wait_for(
            lambda: client.get("/api/tasks/" + failed_task["id"]).json(),
            lambda t: (
                (t.get("snapshot") or {}).get("value", {}).get("status") == "failed"
            ),
        )
        failure = failed["snapshot"]["value"]["failure"]
        assert failure["category"] == "model_not_found"
        assert failure["http_status"] == 404
        assert failure["provider_code"] == "model_not_found"
        assert "exact API model ID" in " ".join(failure["actions"])
        assert "fake-local-test-key" not in json.dumps(failed)
        assert Path(failed["workspace"]).is_dir()
        assert len(requests) == 4  # No automatic task retry.
        stop()
        launch()
        recovered_failure = wait_for(
            lambda: client.get("/api/tasks/" + failed_task["id"]).json(),
            lambda t: t.get("live"),
        )
        assert recovered_failure["snapshot"] == failed["snapshot"]
        recovered_ask = wait_for(
            lambda: client.get("/api/tasks/" + ask["id"]).json(),
            lambda t: t.get("live"),
        )
        assert recovered_ask["snapshot"] == asked["snapshot"]
        assert len(requests) == 4
        tasks_before = client.get("/api/home").json()["tasks"]
        failed_check = post("/api/profiles/" + failed_profile["id"] + "/test", {})
        assert not failed_check["ok"] and failed_check["http_status"] == 404
        assert "[redacted]" in failed_check["provider_message"]
        assert "fake-local-test-key" not in json.dumps(failed_check)
        passed_check = post("/api/profiles/" + profile["id"] + "/test", {})
        assert passed_check["ok"] and passed_check["stage"] == "response"
        assert (
            len(requests) == 5
        )  # Successful native connection probe is handled separately.
        assert {t["id"] for t in client.get("/api/home").json()["tasks"]} == {
            t["id"] for t in tasks_before
        }
        assert (
            client.get("/api/tasks/" + failed_task["id"]).json()["snapshot"]
            == failed["snapshot"]
        )
        assert client.delete("/api/profiles/" + profile["id"]).status_code == 200
        change_profile = post(
            "/api/profiles",
            {
                "label": "SDK change fixture",
                "provider": "openai",
                "model": "change-fixture",
                "base_url": f"http://127.0.0.1:{provider_server.server_port}/v1",
                "env_var": "SDLC_FIXTURE_KEY",
            },
        )
        change = post(
            "/api/tasks",
            {
                **payload,
                "id": uuid.uuid4().hex,
                "profile_id": change_profile["id"],
                "prompt": "Add a note and verify it",
            },
        )

        def change_state():
            return client.get("/api/tasks/" + change["id"]).json()

        planned = wait_for(
            change_state,
            lambda t: (
                (t.get("snapshot") or {}).get("value", {}).get("status") == "needs_you"
            ),
        )
        assert planned["snapshot"]["value"]["gate"]["kind"] == "plan"
        blocked_delete = client.delete(f"/api/tasks/{change['id']}")
        assert blocked_delete.status_code == 409
        assert Path(planned["workspace"]).exists()
        assert not (Path(change["workspace"]) / "sdk-note.txt").exists()
        requests_before_restart = len(requests)
        stop()
        launch()
        resumed = wait_for(change_state, lambda t: t.get("live"))
        assert resumed["snapshot"] == planned["snapshot"]
        assert len(requests) == requests_before_restart
        post(
            f"/api/tasks/{change['id']}/control",
            {
                "command": "respond",
                "gate_id": resumed["snapshot"]["value"]["gate"]["id"],
                "approved": True,
            },
        )
        checking = wait_for(
            change_state,
            lambda t: (
                (t.get("snapshot") or {}).get("value", {}).get("gate", {}).get("kind")
                == "command"
                if (t.get("snapshot") or {}).get("value", {}).get("gate")
                else False
            ),
        )
        assert checking["snapshot"]["value"]["checks"] == []
        post(
            f"/api/tasks/{change['id']}/control",
            {
                "command": "respond",
                "gate_id": checking["snapshot"]["value"]["gate"]["id"],
                "approved": True,
            },
        )
        reviewed = wait_for(
            change_state,
            lambda t: (
                (t.get("snapshot") or {}).get("value", {}).get("status")
                in {"review", "failed"}
            ),
        )
        assert reviewed["snapshot"]["value"]["status"] == "review", reviewed
        assert reviewed["snapshot"]["value"]["checks"][0]["exit_code"] == 0
        assert not (Path(project["path"]) / "sdk-note.txt").exists()

        conversation_profile = post(
            "/api/profiles",
            {
                "label": "Conversation fixture",
                "provider": "openai",
                "model": "conversation-fixture",
                "base_url": f"http://127.0.0.1:{provider_server.server_port}/v1",
                "env_var": "SDLC_FIXTURE_KEY",
                "max_steps": 6,
            },
        )
        message_path = f"/api/tasks/{change['id']}/messages"

        def message(prompt, mode="ask", budget=4):
            ready = wait_for(change_state, lambda t: t.get("can_message"))
            body = {
                "id": uuid.uuid4().hex,
                "expected_turn": ready["next_turn"],
                "prompt": prompt,
                "mode": mode,
                "profile_id": conversation_profile["id"],
                "max_steps": budget,
            }
            result = post(message_path, body)
            assert result["status"] == "accepted", result
            return body

        # Accepting a task doesn't close its conversation or replace its workspace.
        post(f"/api/tasks/{change['id']}/control", {"command": "accept"})
        first_followup = message("Explain the note you just created")
        answered = wait_for(
            change_state, lambda t: t.get("can_message") and t["next_turn"] == 3
        )
        assert (
            answered["id"] == change["id"]
            and answered["workspace"] == change["workspace"]
        )
        assert answered["snapshot"]["value"]["model_calls"] == 8
        assert [
            t["model_calls"]
            for t in answered["snapshot"]["conversation"]["value"]["turns"]
        ] == [4, 4]
        assert [
            t["status"] for t in answered["snapshot"]["conversation"]["value"]["turns"]
        ] == ["accepted", "review"]
        assert "Add a note and verify it" in json.dumps(requests[-1])
        assert "SDK integration works" in json.dumps(requests[-1])
        assert not (Path(change["workspace"]) / "forbidden.txt").exists()
        count = len(requests)
        assert post(message_path, first_followup)["turn_number"] == 2
        assert len(requests) == count
        assert (
            client.post(
                message_path, json={**first_followup, "id": uuid.uuid4().hex}
            ).status_code
            == 409
        )

        # Repeat a change twice: the same action indices must perform fresh I/O,
        # old approvals must not carry forward, and check evidence becomes stale.
        for turn in (3, 4):
            body = message(f"Note refinement {turn}", "change", 6)
            gate = wait_for(
                change_state, lambda t: bool(t["snapshot"]["value"].get("gate"))
            )
            assert gate["snapshot"]["value"]["gate"]["id"] == f"turn-{turn}-step-1"
            assert (
                client.post(
                    message_path, json={**body, "id": uuid.uuid4().hex}
                ).status_code
                == 409
            )
            assert (
                client.delete("/api/profiles/" + conversation_profile["id"]).status_code
                == 409
            )
            assert (
                client.post(
                    f"/api/tasks/{change['id']}/control",
                    json={
                        "command": "respond",
                        "gate_id": planned["snapshot"]["value"]["gate"]["id"],
                    },
                ).status_code
                == 409
            )
            assert not (Path(change["workspace"]) / "forbidden.txt").exists()
            if turn == 3:
                count = len(requests)
                stop()
                launch()
                restored = wait_for(change_state, lambda t: t.get("live"))
                assert restored["snapshot"] == gate["snapshot"]
                assert len(requests) == count
            post(
                f"/api/tasks/{change['id']}/control",
                {
                    "command": "respond",
                    "gate_id": gate["snapshot"]["value"]["gate"]["id"],
                },
            )
            gate = wait_for(
                change_state,
                lambda t: (
                    (t["snapshot"]["value"].get("gate") or {}).get("kind") == "command"
                ),
            )
            assert gate["snapshot"]["value"]["gate"]["id"] == f"turn-{turn}-step-4"
            assert all(c["stale"] for c in gate["snapshot"]["value"]["checks"])
            post(
                f"/api/tasks/{change['id']}/control",
                {
                    "command": "respond",
                    "gate_id": gate["snapshot"]["value"]["gate"]["id"],
                },
            )
            done = wait_for(
                change_state,
                lambda t: t.get("can_message") and t["next_turn"] == turn + 1,
            )
            assert done["snapshot"]["value"]["status"] == "review"
            assert not done["snapshot"]["value"]["checks"][-1]["stale"]
            assert done["snapshot"]["value"]["checks"][-1]["exit_code"] == 0
            assert (
                Path(change["workspace"]) / "sdk-note.txt"
            ).read_text() == f"Note refinement {turn}\n"

        # Exhaust one message's allowance, then recover with a fresh allowance.
        message("Explain again within a smaller budget", budget=3)
        exhausted = wait_for(
            change_state, lambda t: t.get("can_message") and t["next_turn"] == 6
        )
        assert exhausted["snapshot"]["value"]["failure"]["category"] == "budget"
        message("Continue explaining", budget=4)
        done = wait_for(
            change_state, lambda t: t.get("can_message") and t["next_turn"] == 7
        )
        assert done["snapshot"]["value"]["status"] == "review"
        assert done["snapshot"]["value"]["failure"] is None
        assert done["snapshot"]["value"]["model_calls"] == 27
        turns = done["snapshot"]["conversation"]["value"]["turns"]
        assert [t["model_calls"] for t in turns] == [4, 4, 6, 6, 3, 4]
        assert len({e["id"] for e in done["snapshot"]["value"]["entries"]}) == len(
            done["snapshot"]["value"]["entries"]
        )

        # Stop at a follow-up approval; cancellation must not poison the next turn.
        message("A change to stop", "change", 6)
        wait_for(change_state, lambda t: bool(t["snapshot"]["value"].get("gate")))
        post(f"/api/tasks/{change['id']}/control", {"command": "cancel"})
        wait_for(change_state, lambda t: t.get("can_message") and t["next_turn"] == 8)
        message("Explain after stopping")
        done = wait_for(
            change_state, lambda t: t.get("can_message") and t["next_turn"] == 9
        )
        assert done["snapshot"]["value"]["status"] == "review"
        assert not (Path(project["path"]) / "sdk-note.txt").exists()

        # A provider failure can recover on this task using another profile.
        post(
            f"/api/tasks/{failed['id']}/messages",
            {
                "id": uuid.uuid4().hex,
                "expected_turn": 2,
                "prompt": "Try again with the corrected profile",
                "mode": "ask",
                "profile_id": conversation_profile["id"],
                "max_steps": 4,
            },
        )
        fixed = wait_for(
            lambda: client.get("/api/tasks/" + failed["id"]).json(),
            lambda t: t.get("can_message") and t["next_turn"] == 3,
        )
        assert fixed["snapshot"]["value"]["status"] == "review"
        assert fixed["snapshot"]["value"]["failure"] is None
        assert "model_not_found" in json.dumps(requests[-1])

        # Deletion closes the real workflow, defaults to preserving files, and
        # can optionally remove just that task's clone. Reconciliation/restart
        # must never redispatch either deleted task.
        deleted = client.delete(f"/api/tasks/{failed['id']}")
        assert deleted.status_code == 200, deleted.text
        assert Path(failed["workspace"]).exists()
        deleted = client.request(
            "DELETE", f"/api/tasks/{change['id']}", json={"remove_workspace": True}
        )
        assert deleted.status_code == 200, deleted.text
        assert not Path(reviewed["workspace"]).exists()
        calls_before_delete_restart = len(requests)
        stop()
        launch()
        remaining = client.get("/api/home").json()["tasks"]
        assert not {failed["id"], change["id"]} & {t["id"] for t in remaining}
        sessions = client.get("/harness/api/sessions").json()
        assert not {failed["workflow_id"], change["workflow_id"]} & {
            session["workflow_id"] for session in sessions
        }
        assert len(requests) == calls_before_delete_restart
        assert Path(project["path"]).exists()
    finally:
        stop()
        client.close()
        log.close()
        provider_server.shutdown()
        provider_server.server_close()
