"""Start the review workers, session manager, and UI; no model request until you submit a diff."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from google.protobuf.duration_pb2 import Duration
from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from .providers import LiteLLMSettings


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    try:
        LiteLLMSettings.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    env = dict(os.environ)
    env.setdefault("TEMPORAL_ADDRESS", "localhost:7233")
    env.setdefault("TEMPORAL_NAMESPACE", "code-review-demo")
    try:
        async with asyncio.timeout(10):
            client = await Client.connect(env["TEMPORAL_ADDRESS"])
            try:
                await client.workflow_service.register_namespace(
                    RegisterNamespaceRequest(
                        namespace=env["TEMPORAL_NAMESPACE"],
                        workflow_execution_retention_period=Duration(
                            seconds=7 * 24 * 60 * 60
                        ),
                    )
                )
            except RPCError as exc:
                if exc.status != RPCStatusCode.ALREADY_EXISTS:
                    raise
    except (TimeoutError, RuntimeError, RPCError):
        parser.error(
            "Cannot reach Temporal. Start `temporal server start-dev` in another terminal first."
        )

    root = Path(__file__).resolve().parents[2]
    commands = [
        ["examples.code_review.worker"],
        ["temporal_agent_harness.web.cli", "session-manager"],
        [
            "temporal_agent_harness.web.cli",
            "serve",
            "examples/code_review/agents.toml",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ],
    ]
    processes = []
    waiters = []
    try:
        for command in commands:
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-B",
                    "-m",
                    *command,
                    cwd=root,
                    env=env,
                )
            )
        print(f"Code review UI: http://localhost:{args.port}", flush=True)
        print(
            f"Temporal UI: http://localhost:8233/namespaces/{env['TEMPORAL_NAMESPACE']}/workflows",
            flush=True,
        )
        print(
            "Wait for READY, then paste a diff in the UI or run examples.code_review.run. Ctrl-C stops this stack.",
            flush=True,
        )
        waiters = [asyncio.create_task(process.wait()) for process in processes]
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        exit_code = next(iter(done)).result()
        raise SystemExit(
            f"A code review service exited (code {exit_code}); stopping the stack."
        )
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(*(process.wait() for process in processes))
        except TimeoutError:
            for process in processes:
                if process.returncode is None:
                    process.kill()
            await asyncio.gather(*(process.wait() for process in processes))
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
