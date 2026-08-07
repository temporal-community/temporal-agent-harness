"""Run the sandboxed coding agent on a laptop with NO Temporal and no server.

Same ``SandboxAgent`` as the durable workflow; the only difference is the sandbox
client is used directly instead of through Temporal activities. This is the "runs
on the end-user machine" mode. The trade against the durable worker: a crash
mid-turn loses the in-flight turn, there is no retry across process death, and no
server-side history. It keeps crash-resume of the conversation through a small
checkpoint file.

    uv run --group examples python -m examples.portable_coding_agent.local_runner \\
        --session myproj "write a script that prints the first 10 fibonacci numbers and run it"

Needs OPENAI_API_KEY. With ``CODING_AGENT_SANDBOX=docker`` (default) the tools run
in a container; ``=local`` runs them on the host with no isolation (trusted use,
or no Docker). The sandbox is created fresh per run, so the session id chains the
conversation, not the sandbox filesystem; mount a project to work on existing
code (see the README).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from agents import Runner, function_tool
from agents.sandbox import SandboxAgent

from .codebase_search import (
    CodebaseIndex,
    _cache_path,
    _openai_embedder,
    _workspace_root,
    format_hits,
)
from .plan import Todo, render_plan
from .sandbox import local_run_config, sandbox_kind
from .workflow import DEFAULT_MODEL, SYSTEM_INSTRUCTION


def _checkpoint_path(session_id: str) -> Path:
    home = Path(os.environ.get("AGENT_HOME", Path.home() / ".portable-coding-agent"))
    return home / "sessions" / f"{session_id}.json"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sandboxed coding agent locally, no server.")
    parser.add_argument("--session", help="session id (default: 'local')", default="local")
    parser.add_argument("task", nargs="+", help="what to do")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    checkpoint = _checkpoint_path(args.session)
    saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    conversation: list = saved.get("conversation", [])
    todos: list[dict] = saved.get("todos", [])

    model = os.environ.get("CODING_AGENT_MODEL", DEFAULT_MODEL)

    # Local variants of the same tools the durable workflow composes. Here they run directly:
    # the plan is a local list, ask_user prompts the terminal, codebase_search reads the
    # workspace, and task runs a nested sandboxed agent.
    @function_tool
    async def update_plan(todos_json: str) -> str:
        """Record your step-by-step plan as a JSON array of {step, status} objects; replaces the
        previous plan. status is pending / in_progress / completed."""
        todos[:] = [Todo.model_validate(t).model_dump() for t in json.loads(todos_json)]
        return render_plan([Todo.model_validate(t) for t in todos])

    @function_tool
    async def ask_user(question: str) -> str:
        """Ask the user a question when the task is ambiguous or needs a decision only they can
        make. Returns their answer."""
        return await asyncio.to_thread(input, f"\n[agent asks] {question}\n> ")

    @function_tool
    async def codebase_search(query: str) -> str:
        """Search the project by meaning and return the most relevant file regions as
        `path:start-end`. Read those regions to see the code."""
        root = _workspace_root()
        index = CodebaseIndex(root, _openai_embedder(), cache_path=_cache_path(root))
        index.index()
        return format_hits(root, index.search(query))

    @function_tool
    async def task(instructions: str) -> str:
        """Delegate a self-contained sub-task to a fresh sandboxed agent and return its result.
        Give it everything it needs in `instructions`; it does not see this conversation."""
        sub = SandboxAgent(name="Task", model=model, instructions=SYSTEM_INSTRUCTION)
        sub_result = await Runner.run(sub, input=instructions, run_config=local_run_config())
        return str(sub_result.final_output)

    sdk_agent = SandboxAgent(
        name="PortableCodingAgent",
        model=model,
        instructions=SYSTEM_INSTRUCTION,
        tools=[update_plan, ask_user, codebase_search, task],
    )
    result = await Runner.run(
        sdk_agent,
        input=[*conversation, {"role": "user", "content": " ".join(args.task)}],
        run_config=local_run_config(),
    )

    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    tmp = checkpoint.with_suffix(".tmp")
    tmp.write_text(json.dumps({"conversation": result.to_input_list(), "todos": todos}))
    tmp.replace(checkpoint)

    print(str(result.final_output))
    print(f"\n[session {args.session}: sandbox {sandbox_kind()!r}]")


if __name__ == "__main__":
    asyncio.run(main())
