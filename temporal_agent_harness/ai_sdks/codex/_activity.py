"""Bounded asynchronous CLI execution and live harness publishing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile

from temporalio import activity
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import ModelInteractionEnded, ModelInteractionStarted
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

from ._events import CodexEventMapper
from ._models import ACTIVITY_NAME, CodexConfig, CodexRequest, CodexResult


LOG_LIMIT = 64 * 1024
LINE_LIMIT = 512 * 1024
REPLY_LIMIT = 64 * 1024


class JsonlDecoder:
    """Incrementally decode stdout; discard bad/oversized lines and recover."""

    def __init__(self, consume: Callable[[dict], None]):
        self.consume = consume
        self.buffer = bytearray()
        self.discarding = False

    def feed(self, chunk: bytes) -> None:
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if not self.discarding:
                if len(self.buffer) + len(part) > LINE_LIMIT:
                    self.buffer.clear()
                    self.discarding = True
                else:
                    self.buffer.extend(part)
            if index < len(parts) - 1:
                self.finish()

    def finish(self) -> None:
        if self.buffer and not self.discarding:
            try:
                event = json.loads(self.buffer)
            except (ValueError, RecursionError):
                pass
            else:
                if isinstance(event, dict):
                    self.consume(event)
        self.buffer.clear()
        self.discarding = False


class CodexExecutor:
    def __init__(self, config: CodexConfig):
        if os.name != "posix":
            raise ValueError("CodexPlugin currently requires a POSIX worker for process-group cleanup")
        self.config = config.model_copy(deep=True)
        self.workspace = Path(config.workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Codex workspace is not a directory: {self.workspace}")
        binary = shutil.which(config.codex_binary)
        if not binary:
            raise ValueError(f"Codex executable not found: {config.codex_binary}. Install Codex CLI first.")
        # Anchor relative PATH entries before the subprocess changes directories.
        self.binary = str(Path(binary).absolute())
        self.state_dir = Path(config.state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = asyncio.Lock()

    @activity.defn(name=ACTIVITY_NAME)
    async def execute(self, request: CodexRequest) -> CodexResult:
        async def heartbeat():
            while True:
                activity.heartbeat("Codex process running or waiting for its workspace")
                await asyncio.sleep(5)

        pulse = asyncio.create_task(heartbeat())
        try:
            async with self._lock:
                async with AgentWorkflowRunner.publisher_from_activity(
                    request.stream_context, batch_interval=timedelta(milliseconds=100),
                ) as publisher:
                    mapper = CodexEventMapper(publisher.publish, request.invocation_id)
                    publisher.publish(ModelInteractionStarted(model=self.config.model))
                    failure = None
                    try:
                        return await self.run_process(request.prompt, mapper)
                    except asyncio.CancelledError:
                        failure = "Codex execution was cancelled."
                        raise
                    except Exception as error:
                        failure = str(error)
                        raise ApplicationError(failure, type="CodexExecutionError", non_retryable=True) from error
                    finally:
                        mapper.finish(error=failure)
                        publisher.publish(ModelInteractionEnded(model=self.config.model, usage=mapper.usage))
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse

    async def run_process(self, prompt: str, mapper: CodexEventMapper) -> CodexResult:
        directory = Path(tempfile.mkdtemp(prefix="codex-", dir=self.state_dir))
        log = directory / "output.log"
        log.touch(mode=0o600)
        reply = directory / "reply.txt"
        argv = [
            self.binary, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", self.config.sandbox, "--config", 'approval_policy="never"',
            "--config", "sandbox_workspace_write.network_access=false",
            "--config", 'shell_environment_policy.inherit="core"',
            "--color", "never", "--cd", str(self.workspace), "--output-last-message", str(reply),
        ]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        argv.append("-")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=self.workspace, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            log.write_text(str(error))
            raise RuntimeError(f"Could not start Codex. See local log: {log}") from error

        decoder = JsonlDecoder(mapper.on_event)
        tail = bytearray()

        async def drain(stream: asyncio.StreamReader, *, events: bool):
            while chunk := await stream.read(65536):
                tail.extend(chunk)
                del tail[:-LOG_LIMIT]
                log.write_bytes(tail)
                if events:
                    decoder.feed(chunk)
            if events:
                decoder.finish()

        async def write_prompt():
            assert process.stdin is not None
            try:
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        assert process.stdout is not None and process.stderr is not None
        tasks = [
            asyncio.create_task(write_prompt()),
            asyncio.create_task(drain(process.stdout, events=True)),
            asyncio.create_task(drain(process.stderr, events=False)),
            asyncio.create_task(process.wait()),
        ]

        async def stop():
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.sleep(0.2)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

        try:
            await asyncio.wait_for(asyncio.gather(*tasks), self.config.timeout_seconds)
        except TimeoutError as error:
            await asyncio.shield(stop())
            raise RuntimeError(f"Codex timed out after {self.config.timeout_seconds:g}s. See local log: {log}") from error
        except BaseException:
            await asyncio.shield(stop())
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if process.returncode or mapper.failure or not mapper.completed:
            raise RuntimeError(f"Codex did not complete successfully (exit {process.returncode}). See local log: {log}")
        if not reply.is_file() or reply.is_symlink() or reply.stat().st_size > REPLY_LIMIT:
            raise RuntimeError(f"Codex did not write a bounded final reply. See local log: {log}")
        text = reply.read_text()
        if not text.strip():
            raise RuntimeError(f"Codex returned an empty final reply. See local log: {log}")
        return CodexResult(text=text, thread_id=mapper.thread_id, usage=mapper.usage, log_path=str(log))
