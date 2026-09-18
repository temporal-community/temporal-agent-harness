"""Use the packaged Code Mode stepper in supervised, credential-free processes.

Explicit worker activities take precedence over the harness plugin defaults.
The public batch protocol and sandbox implementation remain the package's own.
"""

import asyncio
import json
import os
import signal
import sys

from temporal_agent_harness.harness.code_mode.batch_models import (
    CodeBatchStep,
    ResumeBatchInput,
)
from temporalio import activity


async def step(request: dict) -> CodeBatchStep:
    payload = json.dumps(request).encode()
    if len(payload) > 8_000_000:
        return CodeBatchStep(done=True, error="Code Mode snapshot exceeds 8 MB")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "sdlc_builder.code_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env={"PATH": os.defpath, "LANG": "en_US.UTF-8"},
    )
    try:
        async with asyncio.timeout(10):

            async def write():
                proc.stdin.write(payload)
                await proc.stdin.drain()
                proc.stdin.close()

            writer = asyncio.create_task(write())
            output = bytearray()
            while block := await proc.stdout.read(65536):
                output.extend(block)
                if len(output) > 8_000_000:
                    return CodeBatchStep(
                        done=True, error="Code Mode output/snapshot exceeds 8 MB"
                    )
            await writer
            await proc.wait()
            if proc.returncode:
                return CodeBatchStep(
                    done=True,
                    error="Code Mode stopped: CPU, memory or process limit reached, or sandbox compilation failed",
                )
            result = CodeBatchStep.model_validate_json(output)
            if len(result.pending) > 8:
                return CodeBatchStep(
                    done=True,
                    error="Code Mode permits at most 8 host calls per batch; split the work",
                )
            result.stdout = result.stdout[:12000]
            if result.output_json:
                result.output_json = result.output_json[:100000]
            return result
    except (TimeoutError, BrokenPipeError, ValueError):
        return CodeBatchStep(
            done=True, error="Code Mode step stopped at its execution limit"
        )
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        if "writer" in locals() and not writer.done():
            writer.cancel()
        if "writer" in locals():
            await asyncio.gather(writer, return_exceptions=True)


@activity.defn(name="code_start_batch")
async def bounded_start(
    script: str, type_check_stubs: str | None = None
) -> CodeBatchStep:
    if len(script) > 20000:
        return CodeBatchStep(
            done=True, error="Code Mode script exceeds 20,000 characters"
        )
    return await step({"kind": "start", "script": script, "stubs": type_check_stubs})


@activity.defn(name="code_resume_batch")
async def bounded_resume(input: ResumeBatchInput) -> CodeBatchStep:
    return await step({"kind": "resume", "input": input.model_dump(mode="json")})
