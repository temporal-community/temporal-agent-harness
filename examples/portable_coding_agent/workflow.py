"""A durable coding agent whose shell and file edits run in a SANDBOX.

The user chats in plain text ("add a test for X", "why does this crash?") and a
model in the loop works by running shell commands and editing files. Those tools
are the OpenAI Agents SDK's sandbox tools (``SandboxAgent`` with the default
filesystem + shell capabilities), so every command runs inside a sandbox, not on
the worker. The harness makes the sandbox durable: the workflow references a
named backend through ``temporal_sandbox_client`` and each sandbox operation
becomes a Temporal activity, served by the ``SandboxClientProvider`` the worker
registers (see ``worker.py``).

One sandbox is reused for the whole session: the first message creates it, later
messages resume it, and it is deleted when the agent closes. The SDK deletes a
sandbox at the end of a run unless a live session is passed in, so the workflow
keeps only the serializable session state in workflow state and passes a resumed
live session each turn (which the SDK treats as not-owned, so it survives), and a
file written in one message is there in the next.

Two placement facts worth knowing when a pool of workers serves many sessions:

- The sandbox's operations (create / resume / exec / read / write / delete) go to
  the workflow's own task queue and are dispatched eagerly, so they tend to return
  to the worker that holds the container. Because the sandbox persists across
  messages, this is a hard session-affinity requirement: an operation that lands
  on a worker without the container cannot serve it, and the ``docker`` backend
  can resume only on the host that still has it.
- The MODEL call runs on its own task queue (set on the plugin in ``worker.py``),
  since it is the long, provider-bound step.

The same sandbox agent runs with no server through ``local_runner.py``.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from agents import RunConfig, Runner, TResponseInputItem
    from agents.sandbox import SandboxAgent, SandboxRunConfig
    from agents.sandbox.session.sandbox_session import SandboxSession
    from agents.sandbox.session.sandbox_session_state import SandboxSessionState

    from temporal_agent_harness.ai_sdks.openai_agents.workflow import temporal_sandbox_client
    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tools
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.harness.subagent_toolset import subagent_toolset

    from .ask import ASK_TOOLS
    from .codebase_search import SEARCH_TOOLS
    from .plan import PLAN_TOOLS
    from .sandbox import SANDBOX_NAME, sandbox_manifest, sandbox_options
    from .web import WEB_TOOLS


TASK_QUEUE = "portable-coding-agent"
MODEL_QUEUE = f"{TASK_QUEUE}-model"
DEFAULT_MODEL = "gpt-5-mini"
# A subagent is another instance of this workflow driven as a child. Cap how deep delegation can
# nest (1 = only the top-level agent delegates), so a subagent cannot spawn subagents without bound.
MAX_DELEGATION_DEPTH = 1

SUBAGENT_ADDENDUM = """

You are a subagent handling one delegated sub-task. You cannot ask the user, so do not wait on \
anyone: make a reasonable assumption, state it in your reply, and finish the sub-task yourself."""


def _subagent_policy(agent_id: str | None) -> tuple[bool, bool]:
    """Return (can_ask_user, can_delegate) from the agent's id depth.

    A subagent's ``agent_id`` is a compound ``{parent}-{child}`` id (a top-level agent's is a
    single segment or None), so the number of ``-`` separators is the delegation depth. Only the
    top-level agent has a client attached to answer ``ask_user``; a subagent that asked would block
    forever. Delegation is allowed only while under ``MAX_DELEGATION_DEPTH``.
    """
    depth = agent_id.count("-") if agent_id else 0
    return depth == 0, depth < MAX_DELEGATION_DEPTH


SYSTEM_INSTRUCTION = """\
You are a coding agent working inside a sandbox. The user talks to you in plain language, and YOU \
do the work by exploring the project, editing files, and running commands, then verifying the \
result. You act only through your tools.

Your sandbox may start empty or already hold the user's project, and it may not persist between \
messages, so do not assume files you saw earlier are still there: check what is actually present \
before you rely on it. Work only inside the sandbox workspace.

How to work:
- UNDERSTAND first. Explore before you change anything: list directories, read the files you will \
touch, and search. Use `codebase_search` to find relevant code by meaning ("where are retries \
configured") and shell tools like `grep` and `find` for exact strings and file names. Read a file \
before you edit it.
- PLAN multi-step work with `update_plan`: lay out the steps, mark one `in_progress`, and mark \
each `completed` as you finish, keeping exactly one in progress. Skip the plan for a trivial \
one-step change.
- ASK when it matters. If the task is ambiguous or needs a decision only the user can make, call \
`ask_user` rather than guess.
- DELEGATE a self-contained sub-task to a subagent when it helps (for example a focused search or \
a mechanical change across files); give it everything it needs, use its result, and carry on.
- Use `web_fetch` for documentation or an error message that is not in the repository; prefer the \
repository itself for anything already in it.
- Make small, surgical edits that match the project's existing style and conventions. If an edit \
does not apply cleanly, re-read the exact lines and retry with more surrounding context rather than \
forcing it or guessing the file's contents.
- VERIFY your work. Run the build or the relevant test after a change and fix what you broke; \
prefer the narrowest check that proves the change.
- Keep command output small: read and search targeted regions rather than dumping whole files or \
long logs; pipe through head/tail or grep when you need only part.
- Work in parallel when it is safe: batch independent reads and searches into one step.
- Do not spin. If the same step fails a couple of times, change approach; if you are still \
blocked, say what is blocking you (or ask) instead of looping.
- Keep going until the task is actually done. Then reply in brief prose: what you changed, which \
files, and how you checked it. Never invent file contents or command output you did not see."""


@workflow.defn(name="PortableCodingAgent")
@agent.defn
class PortableCodingAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        # The sandbox is the safety boundary here: the model's commands run inside it, not on the
        # worker, so this agent does not gate individual tool calls. (Per-command approval would be
        # added with the harness approval policy if commands ran on the host.)
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[TResponseInputItem] = []
        # The plan, durable workflow state, edited in place by `update_plan`.
        self._todos: list = []
        # Compound id when this instance is a subagent; gates ask_user + delegation depth.
        self._agent_id = config.agent_id
        # The sandbox is created once for the session and reused every turn; only its serializable
        # state lives in workflow state, so a lost worker can resume the same sandbox (subject to the
        # placement note in the README). None until the first turn creates it.
        self._sandbox_state: SandboxSessionState | None = None

    def _sandbox_client(self):
        return temporal_sandbox_client(
            SANDBOX_NAME, config=ActivityConfig(start_to_close_timeout=timedelta(minutes=5))
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        try:
            await self._runner.run(self)
        finally:
            # The session is ours (owns_session=False in the SDK), so nothing else deletes it.
            # Reclaim the sandbox when the agent closes; an abandoned workflow is swept out of band.
            if self._sandbox_state is not None:
                client = self._sandbox_client()
                await client.delete(await client.resume(self._sandbox_state))
                self._sandbox_state = None

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the coding agent. Ask it to explain, fix, refactor, test, or run something; it
        works in a sandbox by running shell commands and editing files, and replies with what it
        did."""
        # The sandbox supplies read / edit / shell. The rest are harness tools composed on top:
        # plan (inline, its sink is the workflow's todo list), ask_user (a callback the client
        # answers), codebase_search (an activity), and a `task` subagent toolset that drives
        # another instance of this agent as a child workflow.
        can_ask, can_delegate = _subagent_policy(self._agent_id)
        tools = [
            *as_openai_agent_tools(self._runner, PLAN_TOOLS, injections={"sink": self._todos}),
            *as_openai_agent_tools(self._runner, SEARCH_TOOLS),
            *as_openai_agent_tools(self._runner, WEB_TOOLS),
        ]
        if can_ask:
            tools += as_openai_agent_tools(self._runner, ASK_TOOLS)
        if can_delegate:
            tools += as_openai_agent_tools(
                self._runner,
                subagent_toolset(PortableCodingAgentWorkflow, key="task", task_queue=TASK_QUEUE),
            )
        sdk_agent = SandboxAgent(
            name="PortableCodingAgent",
            model=DEFAULT_MODEL,
            instructions=SYSTEM_INSTRUCTION if can_ask else SYSTEM_INSTRUCTION + SUBAGENT_ADDENDUM,
            tools=tools,
        )
        # Reuse one sandbox for the whole session: create it on the first turn, resume it after.
        # Each sandbox operation (create / resume / exec / read / write / delete) is a Temporal
        # activity. Passing a live session (not client/options) makes the SDK treat it as not-owned,
        # so it does NOT delete the sandbox at the end of the run and files persist to the next turn.
        client = self._sandbox_client()
        if self._sandbox_state is None:
            session: SandboxSession = await client.create(
                manifest=sandbox_manifest(), options=sandbox_options()
            )
        else:
            session = await client.resume(self._sandbox_state)
        # Pass BOTH: the SDK prefers `session` (so it reuses this one and does not delete it), while
        # `client`/`options` satisfy the harness runner's requirement that a temporal client is set.
        run_config = RunConfig(
            sandbox=SandboxRunConfig(
                client=client, session=session, options=sandbox_options()
            )
        )

        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]
        result = await Runner.run(sdk_agent, input=input_items, run_config=run_config)
        self._conversation = result.to_input_list()
        self._sandbox_state = session.state
        return TextReply(text=str(result.final_output))
