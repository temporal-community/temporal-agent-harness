"""One disposable Monty step; no host functions, credentials or repository access."""

import asyncio
import json
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (3, 4))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    import pydantic_monty as monty

    # Harness 0.4.0 does not expose Monty's public ResourceLimits argument.
    # Adapt its constructor ONLY in this disposable process, before importing
    # the packaged stepper. No private helpers or parent-process SDK are patched.
    # Remove this shim when the package exposes resource limits on Code Mode.
    original = monty.Monty

    class LimitedMonty:
        def __init__(self, *args, **kwargs):
            self.instance = original(*args, **kwargs)

        def start(self, **kwargs):
            return self.instance.start(
                **kwargs,
                limits={
                    "max_memory": 32_000_000,
                    "max_allocations": 500000,
                    "max_duration_secs": 2,
                    "max_recursion_depth": 200,
                },
            )

    monty.Monty = LimitedMonty
    from temporal_agent_harness.harness.code_mode.activities import (
        code_resume_batch,
        code_start_batch,
    )
    from temporal_agent_harness.harness.code_mode.batch_models import ResumeBatchInput

    request = json.loads(sys.stdin.buffer.read(8_000_001))
    if request["kind"] == "start":
        result = asyncio.run(code_start_batch(request["script"], request["stubs"]))
    else:
        result = asyncio.run(
            code_resume_batch(ResumeBatchInput.model_validate(request["input"]))
        )
    sys.stdout.write(result.model_dump_json())


if __name__ == "__main__":
    main()
