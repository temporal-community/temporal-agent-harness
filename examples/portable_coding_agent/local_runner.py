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
in an isolated container; ``=local`` runs them on the host with no isolation
(trusted use, or no Docker). To work on an existing repo, use ``=local`` and set
``CODING_AGENT_WORKSPACE`` to it: the agent then edits that repo in place and
reads its ``AGENTS.md`` if present (see the README). Otherwise the sandbox starts
empty and the session id only chains the conversation.
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
from .web import fetch_text
from .workflow import DEFAULT_MODEL, SUBAGENT_ADDENDUM, SYSTEM_INSTRUCTION


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
    try:
        saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    except (OSError, ValueError):
        saved = {}  # a truncated or corrupt checkpoint starts a fresh conversation
    conversation: list = saved.get("conversation", [])
    todos: list[dict] = saved.get("todos", [])

    model = os.environ.get("CODING_AGENT_MODEL", DEFAULT_MODEL)

    # Prepend project guidance if the workspace ships an AGENTS.md (an open convention). In local
    # mode the workspace is the real repo, so this is the file the agent is about to work on.
    guide = ""
    guide_path = _workspace_root() / "AGENTS.md"
    if guide_path.is_file():
        guide = "\n\nProject guidance from AGENTS.md:\n" + guide_path.read_text()[:4000]
    instructions = SYSTEM_INSTRUCTION + guide

    # Local variants of the same tools the durable workflow composes. Here they run directly:
    # the plan is a local list, ask_user prompts the terminal, codebase_search reads the
    # workspace, web_fetch pulls a URL, and task runs a nested sandboxed agent.
    @function_tool
    async def update_plan(plan: list[Todo]) -> str:
        """Record or update your step-by-step plan for the current task. Pass the FULL list every
        time; it replaces the previous plan. Each item is {step, status} where status is
        pending / in_progress / completed. Keep exactly one step in_progress."""
        todos[:] = [t.model_dump() for t in plan]
        return render_plan(plan)

    @function_tool
    async def ask_user(question: str, choices: list[str] = []) -> str:
        """Ask the user a question when the task is ambiguous or needs a decision only they can
        make. Pass `choices` to offer options (empty for a free-form answer). Returns their answer."""
        prompt = f"\n[agent asks] {question}\n"
        if choices:
            prompt += "\n".join(f"  {i}) {c}" for i, c in enumerate(choices, 1)) + "\n"
        try:
            answer = await asyncio.to_thread(input, prompt + "> ")
        except EOFError:
            return "(no answer: input is not available; proceed with a reasonable assumption)"
        if choices and answer.strip().isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        return answer

    @function_tool
    async def codebase_search(query: str) -> str:
        """Search the project by meaning and return the most relevant file regions with their code.
        Use it to find where something lives before reading or editing."""
        root = _workspace_root()
        index = CodebaseIndex(root, _openai_embedder(), cache_path=_cache_path(root))
        index.index()
        return format_hits(root, index.search(query))

    @function_tool
    async def web_fetch(url: str) -> str:
        """Fetch a web page or raw file over HTTP(S) and return its text, for documentation or an
        error message the repository does not contain."""
        return await fetch_text(url)

    @function_tool
    async def task(task_instructions: str) -> str:
        """Delegate a self-contained sub-task to a fresh sandboxed agent and return its result.
        Give it everything it needs; it does not see this conversation."""
        sub = SandboxAgent(name="Task", model=model, instructions=SYSTEM_INSTRUCTION + SUBAGENT_ADDENDUM)
        sub_result = await Runner.run(sub, input=task_instructions, run_config=local_run_config())
        return str(sub_result.final_output)

    sdk_agent = SandboxAgent(
        name="PortableCodingAgent",
        model=model,
        instructions=instructions,
        tools=[update_plan, ask_user, codebase_search, web_fetch, task],
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
