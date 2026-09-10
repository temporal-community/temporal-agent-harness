"""Actual subprocess behavior without an LLM account or Temporal server."""

import asyncio
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from temporal_agent_harness.ai_sdks.codex import CodexConfig
from temporal_agent_harness.ai_sdks.codex._activity import CodexExecutor, JsonlDecoder, LINE_LIMIT, LOG_LIMIT
from temporal_agent_harness.ai_sdks.codex._events import CodexEventMapper


def executor(tmp_path, body, **options):
    binary = tmp_path / "codex-fake"
    binary.write_text(f"#!{sys.executable}\nimport sys,json,time,os\nfrom pathlib import Path\n" + body)
    binary.chmod(0o755)
    return CodexExecutor(CodexConfig(
        workspace=tmp_path, state_dir=tmp_path / "logs", codex_binary=str(binary), **options,
    ))


def mapper():
    return CodexEventMapper(lambda event: None, "invocation")


def test_jsonl_handles_fragments_unicode_noise_and_oversized_lines():
    events = []
    decoder = JsonlDecoder(events.append)
    data = json.dumps({"text": "🏈 café"}, ensure_ascii=False).encode() + b"\n"
    for byte in data:
        decoder.feed(bytes([byte]))
    decoder.feed(b"not json\n[]\n" + b"x" * (LINE_LIMIT + 1))
    assert len(decoder.buffer) <= LINE_LIMIT
    decoder.feed(b'\n{"type":"turn.completed"}')
    decoder.finish()
    assert events == [{"text": "🏈 café"}, {"type": "turn.completed"}]


async def test_cli_flags_final_reply_and_worker_config_are_used(tmp_path):
    run = executor(tmp_path, """
Path('invocation.json').write_text(json.dumps({'args':sys.argv,'prompt':sys.stdin.read()}))
print(json.dumps({'type':'thread.started','thread_id':'thread'}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':3,'cached_input_tokens':2}}))
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('Repository inspected.')
""", model="configured-model", sandbox="workspace-write")
    result = await run.run_process("Inspect this repository", mapper())
    saved = json.loads((tmp_path / "invocation.json").read_text())
    argv = saved["args"]
    assert saved["prompt"] == "Inspect this repository"
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--model") + 1] == "configured-model"
    assert 'approval_policy="never"' in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert 'shell_environment_policy.inherit="core"' in argv
    assert {"--json", "--ephemeral", "--ignore-user-config", "--ignore-rules"} <= set(argv)
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert result.text == "Repository inspected."
    assert result.thread_id == "thread"
    assert result.usage.input_tokens == 10
    assert Path(result.log_path).exists()


async def test_relative_executable_is_resolved_before_changing_workspace(tmp_path, monkeypatch):
    worker_directory = tmp_path / "worker"
    workspace = tmp_path / "project"
    worker_directory.mkdir()
    workspace.mkdir()
    binary = worker_directory / "codex-relative"
    binary.write_text(f"#!{sys.executable}\n" + """
import json
import sys
from pathlib import Path

sys.stdin.read()
Path('working-directory.txt').write_text(str(Path.cwd()))
print(json.dumps({'type': 'turn.completed'}))
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('Inspected the project.')
""")
    binary.chmod(0o755)
    monkeypatch.chdir(worker_directory)
    run = CodexExecutor(CodexConfig(
        workspace=workspace,
        state_dir=tmp_path / "logs",
        codex_binary="./codex-relative",
    ))

    result = await run.run_process("Inspect this project", mapper())

    assert result.text == "Inspected the project."
    assert (workspace / "working-directory.txt").read_text() == str(workspace.resolve())


async def test_raw_stderr_is_not_an_event_and_logs_are_bounded(tmp_path):
    run = executor(tmp_path, """
sys.stdin.read()
print('x'*200000, file=sys.stderr)
print(json.dumps({'type':'turn.completed'}), file=sys.stderr)
print('raw-private-error', file=sys.stderr)
sys.exit(7)
""")
    events = []
    event_mapper = CodexEventMapper(events.append, "call")
    with pytest.raises(RuntimeError, match="exit 7") as error:
        await run.run_process("hello", event_mapper)
    assert "raw-private-error" not in str(error.value)
    assert not event_mapper.completed
    assert not events
    log = next(run.state_dir.glob("*/output.log"))
    assert log.stat().st_size <= LOG_LIMIT
    assert "raw-private-error" in log.read_text()


@pytest.mark.parametrize("body", [
    "sys.stdin.read(); sys.exit(0)",
    "sys.stdin.read(); print(json.dumps({'type':'turn.failed','error':{'message':'private'}}))",
    "sys.stdin.read(); print(json.dumps({'type':'turn.completed'}))",
    "sys.stdin.read(); print(json.dumps({'type':'turn.completed'})); Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('x'*70000)",
])
async def test_incomplete_cli_run_cannot_claim_success(tmp_path, body):
    run = executor(tmp_path, body)
    with pytest.raises(RuntimeError, match="local log"):
        await run.run_process("hello", mapper())


@pytest.mark.parametrize("cancel", [True, False])
async def test_timeout_and_cancellation_kill_descendants(tmp_path, cancel):
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    child = f"import time; from pathlib import Path; time.sleep(1.2); Path({str(survived)!r}).write_text('orphan')"
    run = executor(tmp_path, f"""
import subprocess
sys.stdin.read()
subprocess.Popen([sys.executable,'-c',{child!r}])
Path({str(started)!r}).touch()
time.sleep(60)
""", timeout_seconds=60 if cancel else .3)
    task = asyncio.create_task(run.run_process("hello", mapper()))
    if cancel:
        for _ in range(100):
            if started.exists():
                break
            await asyncio.sleep(.01)
        assert started.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="timed out"):
            await task
    await asyncio.sleep(1.3)
    assert not survived.exists()


def test_worker_rejects_unrestricted_sandbox_and_unknown_settings(tmp_path):
    with pytest.raises(ValidationError):
        CodexConfig(workspace=tmp_path, sandbox="danger-full-access")
    with pytest.raises(ValidationError):
        CodexConfig(workspace=tmp_path, extra_args=["--dangerously-bypass-approvals-and-sandbox"])
