"""Worker for the eval swarm example.

Run from the repo root with:
    bazel run //python/examples/swarm_eval:worker

(or `just worker` from examples/swarm_eval.)

Two workers, because the swarm uses two model providers:

* the EVAL agents (dispatcher, writers, leads, testers, judges) on `swarm-eval`, through a client
  with the OpenAI Agents plugin, in this process;
* the agent UNDER TEST — Monty's conversational travel agent — on `swarm-eval-target`, through a
  client with the Gemini plugin and Monty's travel tools, in a CHILD process this one starts.

They run as two processes because the targets run model-written scripts in a sandbox inside their
workflow tasks: in one Python process with a few hundred eval agents, that CPU slowed every
workflow task, the swarm's own included, enough for the server to give up on board updates. The
parent supervises the child: each stops when the other does, so `just dev` and Ctrl-C need no
special handling.

Each client gets the harness plugin LAST, so its converter composes with the provider's. A run
keeps a hundred or more conversations going, and every one holds activities open while it waits
(a lead on each tester, a tester on each target turn), so both workers allow far more
concurrent activities and workflow tasks than the SDK defaults.

With `--simulate`, or without OPENAI_API_KEY, the eval worker serves scripted runs without model
plugins. Live custom-target evaluations need OPENAI_API_KEY and a separately running target
worker; the optional Monty child additionally needs GEMINI_API_KEY.

`--part eval` or `--part target` runs one half only, to put them on different machines.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            the eval agents' models
    GEMINI_API_KEY                            the target's model
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from datetime import timedelta

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.swarm_eval.eval_agents import EVAL_AGENTS, TARGET_TASK_QUEUE, TASK_QUEUE
from examples.swarm_eval.library import EvalLibrary, LibraryActivities
from examples.swarm_eval.simulated import ScriptedTravelAgent
from examples.swarm_eval.trace import EvalActivities

SWARM_LIMITS = {"max_concurrent_activities": 2000, "max_concurrent_workflow_tasks": 200}


async def _eval_client(connect_config: dict, *, simulate: bool) -> Client:
    if simulate:
        return await Client.connect(**connect_config, plugins=[AgentHarnessPlugin()])
    from temporal_agent_harness.ai_sdks.openai_agents import (
        ModelActivityParameters,
        OpenAIAgentsPlugin,
    )
    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        harness_observer_factory,
        stream_to_provider,
    )

    openai_plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )
    return await Client.connect(**connect_config, plugins=[openai_plugin, AgentHarnessPlugin()])


async def _target_worker(connect_config: dict, api_key: str) -> Worker:
    from google.genai import Client as GeminiClient

    from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin

    from examples.monty import activities
    from examples.monty.conversational_workflow import MontyChatAgentWorkflow

    client = await Client.connect(
        **connect_config,
        plugins=[
            GoogleGenAIPlugin(GeminiClient(api_key=api_key)),
            AgentHarnessPlugin(tools=activities.ALL_TOOLS),
        ],
    )
    return Worker(
        client, task_queue=TARGET_TASK_QUEUE, workflows=[MontyChatAgentWorkflow], **SWARM_LIMITS
    )


# Set on the child's environment, so it can tell its parent has gone.
_PARENT_PID = "SWARM_EVAL_PARENT_PID"


async def _target_child() -> asyncio.subprocess.Process:
    """Start the target half as a child process, sharing this one's environment and output."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "examples.swarm_eval.worker",
        "--part",
        "target",
        env={**os.environ, _PARENT_PID: str(os.getpid())},
    )


async def _orphaned() -> None:
    """Return once the parent that started this child has gone (it is reparented)."""
    parent = int(os.environ[_PARENT_PID])
    while os.getppid() == parent:
        await asyncio.sleep(2)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--simulate", action="store_true", help="run only what a simulated run needs (no API keys)"
    )
    parser.add_argument(
        "--part",
        choices=("both", "eval", "target"),
        default="both",
        help="both halves, the target in a child process (default); or only one of them",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", force=True
    )

    openai_key = os.environ.get("OPENAI_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    simulate = args.simulate or not (gemini_key if args.part == "target" else openai_key)
    if simulate and not args.simulate and args.part != "target":
        print("OPENAI_API_KEY not set: serving simulated runs only (send simulate: true).")
    if args.part == "both" and not simulate and not gemini_key:
        print("GEMINI_API_KEY not set: Monty target disabled; custom target workers must run separately.")

    connect_config = ClientConfig.load_client_connect_config()
    workers: list[Worker] = []
    queues: list[str] = []
    if args.part in ("both", "eval"):
        eval_client = await _eval_client(connect_config, simulate=simulate)
        workers.append(
            Worker(
                eval_client,
                task_queue=TASK_QUEUE,
                workflows=[*EVAL_AGENTS, ScriptedTravelAgent, EvalLibrary],
                activities=[
                    EvalActivities(eval_client).collect_tool_calls,
                    LibraryActivities(eval_client).call_library,
                ],
                **SWARM_LIMITS,
            )
        )
        queues.append(TASK_QUEUE)
    if args.part == "target":
        if simulate:
            raise SystemExit("--part target needs GEMINI_API_KEY (the target is a Gemini agent)")
        assert gemini_key is not None
        workers.append(await _target_worker(connect_config, gemini_key))
        queues.append(TARGET_TASK_QUEUE)
    child = None
    if args.part == "both" and not simulate and gemini_key:
        child = await _target_child()
        queues.append(f"{TARGET_TASK_QUEUE} (child process {child.pid})")
    print(
        f"Eval swarm worker ready: address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} queues={' + '.join(queues)} "
        f"mode={'simulated' if simulate else 'live'}",
        flush=True,
    )

    # Stop on SIGTERM as on Ctrl-C, so the child is always taken down with this process.
    main_task = asyncio.current_task()
    assert main_task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)
    running = [asyncio.ensure_future(worker.run()) for worker in workers]
    if child is not None:
        running.append(asyncio.ensure_future(child.wait()))
    if _PARENT_PID in os.environ:
        running.append(asyncio.ensure_future(_orphaned()))
    try:
        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # a worker that failed fails the process
        if child is not None and child.returncode is not None:
            raise SystemExit(f"the target worker exited ({child.returncode}); stopping")
    finally:
        for task in running:
            task.cancel()
        if child is not None and child.returncode is None:
            child.terminate()
            with contextlib.suppress(ProcessLookupError):
                await child.wait()


if __name__ == "__main__":
    asyncio.run(main())
