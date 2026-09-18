"""V2 through real Temporal, native SDK streams, Code Mode, and child workflows."""

import asyncio
import json
import os
import signal
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
from test_live import free_port, wait_for

from sdlc_builder.coding_models import Finding, Recipe, RoleOutput

pytestmark = pytest.mark.skipif(
    os.environ.get("SDLC_INTEGRATION") != "1",
    reason="Set SDLC_INTEGRATION=1 for the isolated local stack",
)


async def assert_role_traces(address, workflow_id, payload_dir):
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
    stream = await AgentClient(client, workflow_id).attach(on_item=lambda item, _: item)
    events = []
    async with asyncio.timeout(12):
        async for item in stream:
            events.append(item)
    assert not any(e.event.type == "subagent_stream_unavailable" for e in events)
    child_states = {
        e.agent_id
        for e in events
        if e.event.type == "state_snapshot" and e.event.state_id == "assignment"
    }
    assert len(child_states) == 2
    assert all(
        any(e.agent_id == child and e.event.type == "tool_start" for e in events)
        for child in child_states
    )


def test_coding_v2_lifecycle(tmp_path):
    port, temporal_port = free_port(), free_port()
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            role = next(
                r
                for r in ["coordinator", "implementer", "reviewer"]
                if f"SDLC {r}" in body["instructions"]
            )
            inputs = body["input"]
            prompt = str(inputs[0])
            outputs = [i for i in inputs if i.get("type") == "function_call_output"]
            repair = '"repair"' in prompt
            if role == "reviewer" and body["model"] == "false-review-fixture":
                encoded = response_events(
                    RoleOutput(summary="Everything is done and verified.").model_dump(
                        mode="json"
                    ),
                    body["model"],
                    text_config=body["text"],
                )
            elif not outputs:
                if role == "implementer":
                    content = (
                        'def greet(name):\n    return f"Hello, {name}!"\n'
                        if repair or body["model"] == "false-review-fixture"
                        else 'def greet(name):\n    return f"Hi, {name}!"\n'
                    )
                    script = (
                        'f = await read_file({"path": "greeting.py"})\nfile = f["file"]\nassert file is not None\nawait apply_patch({"changes": [{"path": "greeting.py", "expected_hash": file["hash"], "content": '
                        + repr(content)
                        + '}, {"path": "feature-note.txt", "expected_hash": "", "content": "Greeting feature"}]})'
                    )
                    script = (
                        'denied = await apply_patch({"changes": [{"path": "outside-scope.txt", "expected_hash": "", "content": "blocked"}]})\nassert not denied["ok"]\n'
                        + script
                    )
                    name, args = "change_code", {"script": script}
                else:
                    name, args = (
                        "inspect_code",
                        {
                            "script": 'import asyncio\nawait asyncio.gather(read_file({"path": "greeting.py"}), read_file({"path": "test_greeting.py"}), get_diff())'
                        },
                    )
                encoded = function_events(
                    name, args, body["model"], text_config=body["text"]
                )
            else:
                assert all("Script error" not in str(o) for o in outputs), outputs
                if role == "coordinator":
                    result = RoleOutput(
                        summary="Fix greeting with a regression test and implementation note.",
                        requirements=["Greeting matches Hello, Ada!"],
                        assumptions=["Keep greet(name) public"],
                        scope=["greeting.py", "feature-note.txt"],
                        checks=[
                            Recipe(
                                title="Regression",
                                argv=["python3", "-m", "unittest", "-v"],
                            )
                        ],
                    )
                elif role == "implementer":
                    result = RoleOutput(summary="Applied the scoped change.")
                else:
                    failed = '"exit_code": 1' in prompt
                    result = RoleOutput(
                        summary="Reviewed code and regression evidence.",
                        findings=[
                            Finding(
                                severity="high",
                                path="greeting.py",
                                line=2,
                                explanation="Greeting does not match the requested format.",
                            )
                        ]
                        if failed
                        else [],
                    )
                encoded = response_events(
                    result.model_dump(mode="json"),
                    body["model"],
                    text_config=body["text"],
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    app_log = (tmp_path / "app.log").open("w")
    process = None
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"X-SDLC": "1"}, timeout=20
    )

    def post(path, payload):
        r = client.post(path, json=payload)
        assert r.status_code == 200, r.text
        return r.json()

    def launch():
        nonlocal process
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "sdlc_builder.cli",
                "--data-dir",
                str(tmp_path / "data"),
                "--port",
                str(port),
                "--temporal-port",
                str(temporal_port),
            ],
            stdout=app_log,
            stderr=app_log,
            env={**os.environ, "SDLC_FIXTURE_KEY": "fake-local-test-key"},
            start_new_session=True,
            cwd=Path(__file__).parents[1],
        )
        for _ in range(150):
            if process.poll() is not None:
                pytest.fail((tmp_path / "app.log").read_text()[-15000:])
            try:
                if client.get("/").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail((tmp_path / "app.log").read_text()[-15000:])
        post(
            "/api/unlock",
            {"token": (tmp_path / "data" / "session-token").read_text().strip()},
        )

    def stop():
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=30)

    try:
        launch()
        project = post("/api/demo", {})
        profile = post(
            "/api/profiles",
            {
                "label": "V2 fixture",
                "provider": "openai",
                "model": "v2-fixture",
                "base_url": f"http://127.0.0.1:{provider.server_port}/v1",
                "env_var": "SDLC_FIXTURE_KEY",
                "max_steps": 24,
            },
        )
        task = post(
            "/api/tasks",
            {
                "id": uuid.uuid4().hex,
                "project_id": project["id"],
                "profile_id": profile["id"],
                "prompt": "Fix the greeting and add a note",
                "mode": "change",
            },
        )
        assert task["engine"] == "v2"
        path = "/api/tasks/" + task["id"]

        def read():
            r = client.get(path).json()
            state = (r.get("snapshot") or {}).get("value", {})
            assert state.get("status") != "failed", (
                state,
                (tmp_path / "app.log").read_text()[-8000:],
            )
            return r

        def gate():
            return wait_for(
                read,
                lambda r: bool((r.get("snapshot") or {}).get("value", {}).get("gate")),
                timeout=60,
            )["snapshot"]["value"]["gate"]

        plan = gate()
        assert plan["kind"] == "plan"
        stop()
        launch()
        assert gate()["id"] == plan["id"]
        rejected = client.post(
            path + "/control", json={"command": "respond", "gate_id": "stale"}
        )
        assert rejected.status_code == 409
        post(path + "/control", {"command": "respond", "gate_id": plan["id"]})
        seen = {plan["id"]}
        for _ in range(2):
            current = wait_for(
                read,
                lambda r: (
                    (g := r["snapshot"]["value"].get("gate")) and g["id"] not in seen
                ),
                timeout=60,
            )["snapshot"]["value"]["gate"]
            seen.add(current["id"])
            assert current["kind"] == "command"
            post(path + "/control", {"command": "respond", "gate_id": current["id"]})
        settled = wait_for(read, lambda r: r.get("can_message"), timeout=60)
        state = settled["snapshot"]["value"]
        assert state["verification"] == "verified", state
        assert state["repair_attempt"] == 1
        assert not (Path(task["workspace"]) / "outside-scope.txt").exists()
        assert len(state["checks"]) == 2 and state["checks"][0]["exit_code"] != 0
        assert state["checks"][0]["stale"] and not state["checks"][1]["stale"]
        assert state["model_calls"] == len(requests) == 10
        assert {a["role"] for a in state["actors"]} == {
            "coordinator",
            "implementer",
            "reviewer",
        }
        asyncio.run(
            assert_role_traces(
                f"127.0.0.1:{temporal_port}",
                task["workflow_id"],
                tmp_path / "data" / "payloads",
            )
        )
        records = list(
            (tmp_path / "data" / "coding-operations" / task["id"]).glob("*.json")
        )
        assert len(records) >= 20
        assert "fake-local-test-key" not in json.dumps(state)
        # An external edit cannot be accepted under the previous green evidence.
        (Path(task["workspace"]) / "greeting.py").write_text(
            "def greet(name): return 'broken'\n"
        )
        stale = client.post(path + "/control", json={"command": "accept"})
        assert stale.status_code == 409
        post(
            path + "/control",
            {
                "command": "accept",
                "text": "Accepting this local experiment with stale checks.",
            },
        )
        before = len(requests)
        post(
            path + "/messages",
            {
                "id": uuid.uuid4().hex,
                "expected_turn": 2,
                "prompt": "Explain the greeting",
                "mode": "ask",
                "profile_id": profile["id"],
                "max_steps": 3,
            },
        )
        followup = wait_for(
            read, lambda r: r.get("can_message") and r.get("next_turn") == 3, timeout=60
        )
        assert len(requests) == before + 2
        assert len(followup["snapshot"]["conversation"]["value"]["turns"]) == 2
        assert len(followup["snapshot"]["value"]["blueprints"]) == 1
        assert followup["workflow_id"] == task["workflow_id"]
        # Human review prepares a targeted follow-up, then the existing durable
        # conversation starts a fresh approval cycle in this same workspace.
        reviewed_file = client.get(
            path + "/review/file", params={"path": "greeting.py"}
        ).json()
        comment_id = uuid.uuid4().hex
        post(
            path + "/review/comments",
            {
                "id": comment_id,
                "path": "greeting.py",
                "token": reviewed_file["token"],
                "side": "new",
                "line": 1,
                "text": "Restore the greeting after the manual edit.",
            },
        )
        review_version = client.get(path + "/review").json()["version"]
        fix = post(
            path + "/review/fix-request",
            {
                "version": review_version,
                "comment_ids": [comment_id],
            },
        )
        review_message = {
            "id": uuid.uuid4().hex,
            "expected_turn": 3,
            "prompt": fix["prompt"],
            "mode": "change",
            "profile_id": profile["id"],
            "max_steps": 24,
        }
        post(path + "/messages", review_message)
        post(path + "/messages", review_message)
        plan = gate()
        assert plan["kind"] == "plan"
        # The human review and queued workflow both survive a process restart.
        stop()
        launch()
        assert gate()["id"] == plan["id"]
        assert (
            client.get(path + "/review")
            .json()["comments"][0]["text"]
            .startswith("Restore")
        )
        post(path + "/control", {"command": "respond", "gate_id": plan["id"]})
        for _ in range(2):
            current = gate()
            assert current["kind"] == "command"
            post(path + "/control", {"command": "respond", "gate_id": current["id"]})
        corrected = wait_for(
            read, lambda r: r.get("can_message") and r.get("next_turn") == 4, timeout=60
        )
        assert corrected["workflow_id"] == task["workflow_id"]
        assert corrected["workspace"] == task["workspace"]
        assert corrected["snapshot"]["value"]["verification"] == "verified"
        assert len(corrected["snapshot"]["conversation"]["value"]["turns"]) == 3
        assert len(corrected["snapshot"]["value"]["blueprints"]) == 2
        assert not client.get(path + "/review").json()["comments"][0]["resolved"]
        # Acceptance authorizes a workflow update; its retry-safe activity writes
        # the source checkout and the receipt survives worker/app restart.
        post(path + "/control", {"command": "accept"})
        merged = post(path + "/merge", {})
        assert merged["files"] == ["feature-note.txt", "greeting.py"]
        assert (Path(project["path"]) / "feature-note.txt").read_text() == (
            "Greeting feature"
        )
        merged_state = read()["snapshot"]["value"]
        assert merged_state["project_merge"]["operation_id"] == merged["operation_id"]
        stop()
        launch()
        restored = read()["snapshot"]["value"]
        assert restored["project_merge"] == merged_state["project_merge"]
        # A claimed review without source inspection cannot turn real passing
        # checks into verified completion, even when the model says "verified".
        false_profile = post(
            "/api/profiles",
            {
                "label": "False review fixture",
                "provider": "openai",
                "model": "false-review-fixture",
                "base_url": profile["base_url"],
                "env_var": "SDLC_FIXTURE_KEY",
                "max_steps": 6,
            },
        )
        incomplete = post(
            "/api/tasks",
            {
                "id": uuid.uuid4().hex,
                "project_id": project["id"],
                "profile_id": false_profile["id"],
                "prompt": "Fix greeting",
                "mode": "change",
            },
        )
        path = "/api/tasks/" + incomplete["id"]
        for kind in ["plan", "command"]:
            current = wait_for(
                read,
                lambda r: (
                    ((r.get("snapshot") or {}).get("value", {}).get("gate") or {}).get(
                        "kind"
                    )
                    == kind
                ),
                timeout=60,
            )["snapshot"]["value"]["gate"]
            post(path + "/control", {"command": "respond", "gate_id": current["id"]})
        incomplete = wait_for(read, lambda r: r.get("can_message"), timeout=60)
        assessment = incomplete["snapshot"]["value"]
        assert assessment["checks"][-1]["exit_code"] == 0
        assert assessment["verification"] == "incomplete"
        assert any("without inspecting" in e for e in assessment["missing_evidence"])
        deleted = client.delete(path)
        assert deleted.status_code == 200, deleted.text
    finally:
        stop()
        provider.shutdown()
        provider.server_close()
        client.close()
        app_log.close()
