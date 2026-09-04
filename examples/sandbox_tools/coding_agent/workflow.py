# ABOUTME: A conversational CODING agent whose tools run inside an E2B cloud sandbox (not on the
# user's machine, and not via callback). Six tools declared as
# @agent.activity_tool_defn(sandboxed=True) — the work happens in a disposable box, on a project that
# lives there. Pairs with the live-preview proxy: the agent builds a web app in the box and you
# preview it. The conversational loop is the OpenAI Agents SDK's own `Runner.run_streamed`.

from typing import Any

from temporalio import workflow

# EVERY temporal_agent_harness / remote-box import must live in this ONE block (take "every"
# literally — todo_tools, tools, agent_protocol, the plugin glue). remote-box (pulled in
# transitively by tools.py) needs pass-through treatment, and splitting even one harness import out
# was enough to load two copies of agent_workflow.py — each with its own _CURRENT_RUNNER contextvar —
# so a sandboxed tool call fails with "tool ... has no active runner". The `agents` SDK imports go in
# here too: it is a third-party SDK doing module-level I/O-ish setup, exactly what the workflow
# sandbox is unhappy about.
with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import CodeInterpreterTool, ModelSettings, Runner, TResponseInputItem
    from openai.types.shared import Reasoning

    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tool
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent, slash_commands
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        SlashCommand,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )

    from examples.coding_agent_common.todo_tools import TodoItem, todoread, todowrite

    from .tools import (
        PREVIEW_BASE_DOMAIN,
        PROJECT_ROOT,
        SANDBOX,
        SANDBOX_ID_ENV_VAR,
        SANDBOXED_CODING_TOOLS,
    )


TASK_QUEUE = "sandboxed-coding-agent"
SUPPORTED_MODELS = ("gpt-5.1", "gpt-4.1")
DEFAULT_MODEL = SUPPORTED_MODELS[0]
# Child instances are this same workflow on TASK_QUEUE. Cap nesting so a child cannot spawn
# further children without bound (1 = only the top-level agent delegates).
MAX_DELEGATION_DEPTH = 1
SUBAGENT_KEY = "task"

MODEL_SETTINGS = ModelSettings(reasoning=Reasoning(effort="high", summary=None))

MAX_TURNS = 100


def _subagent_policy(agent_id: str | None) -> tuple[bool, bool]:
    """Return (is_root, can_delegate) from the agent's id depth.

    A subagent's ``agent_id`` is a compound ``{parent}-{child}`` id (a top-level agent's is a
    single segment or None), so the number of ``-`` separators is the delegation depth.
    Delegation is allowed only while under ``MAX_DELEGATION_DEPTH``.
    """
    depth = agent_id.count("-") if agent_id else 0
    return depth == 0, depth < MAX_DELEGATION_DEPTH


_CODE_INTERPRETER = CodeInterpreterTool(
    tool_config={"type": "code_interpreter", "container": {"type": "auto"}}
)


_BASE_INSTRUCTION = f"""\
You are a capable, careful coding assistant. The user talks to you in plain language — asking you \
to build an app, add a feature, run a command, or explain code — and YOU do the work by calling \
tools. Your tools run inside an isolated cloud sandbox (never on the user's own machine), on a \
project that lives in that sandbox at {PROJECT_ROOT}. All file paths are relative to that \
project root, which starts EMPTY — you scaffold whatever the task needs.

Your sandbox tools: `bash`, `read`, `write`, `edit`, `grep`, `glob`, plus `todowrite`/`todoread` \
for a task list. You also have OpenAI's hosted `code_interpreter` for quick Python/numeric work, \
charts, and one-off calculations without polluting the project tree — use sandbox tools for \
scaffolding projects, installing deps, editing repo files, and running servers. A top-level \
session can delegate a self-contained sub-task to a child coding agent with `start_task` (get a \
handle), `task_ask` (send the brief), and `stop_task` (when that child is done).

How to work:
- PLAN multi-step work with `todowrite`; mark one task `in_progress` and `completed` as you go. \
Skip planning for trivial one-step requests.
- ORIENT with `glob`/`grep`/`read` before editing existing files, so your changes fit in.
- READ before you EDIT. `edit` needs an exact, unique `old_string`. Use `write` for new files or \
full rewrites, `edit` for surgical changes.
- DELEGATE a focused, self-contained sub-task to a child when it helps (e.g. parallel \
workstreams); give it everything it needs, use its result, and carry on.
- VERIFY when it's cheap: run the build or tests via `bash` after a change."""

_PREVIEW_SUBDOMAIN = f"""

## Making a web app previewable

When the user asks for a website or web app they can open in a browser, this sandbox can serve it \
live at its own subdomain. After building the app in the project directory:
1. Write the single server-launch command to `start.sh` in the project root (i.e. `write` with \
file_path="start.sh") — it MUST run in the FOREGROUND (no `&`) and bind 0.0.0.0 on a plain port you \
pick (e.g. 3000), and `cd` into the project dir first. A supervisor picks this up within ~1s and \
keeps it running.
   Example: `write` file_path="start.sh" content="cd {PROJECT_ROOT}\\npython3 -m http.server 3000 --bind 0.0.0.0\\n"
2. Read the sandbox id and give the user the preview URL. Run `bash` with `echo "${SANDBOX_ID_ENV_VAR}"`, \
then tell them to open https://<that-id>-<port>.{PREVIEW_BASE_DOMAIN}/ ."""

_PREVIEW_LOCALHOST = f"""

## Making a web app previewable

When the user asks for a website or web app they can open in a browser, this sandbox can serve it \
live. After building the app in the project directory:
1. Write the single server-launch command to `start.sh` in the project root (i.e. `write` with \
file_path="start.sh") — it MUST run in the FOREGROUND (no `&`) and bind 0.0.0.0 on a plain port you \
pick (e.g. 3000), and `cd` into the project dir first. A supervisor picks this up within ~1s and \
keeps it running.
   Example: `write` file_path="start.sh" content="cd {PROJECT_ROOT}\\npython3 -m http.server 3000 --bind 0.0.0.0\\n"
2. Read the sandbox id and give the user the preview URL. Run `bash` with `echo "${SANDBOX_ID_ENV_VAR}"`, \
then tell them to open http://localhost:8080/s/<that-id>/<port>/ . Prefer RELATIVE asset paths \
(./style.css) or a <base href> so pages load through the path-based proxy."""

_CLOSING_INSTRUCTION = """

Keep going across tool calls until the task is done, then reply in brief, friendly prose: what you \
built or changed, which files, and (if relevant) the preview URL. Never invent file contents or \
command output you didn't actually read."""


SUBAGENT_ADDENDUM = """

You are a subagent handling one delegated sub-task. You cannot ask the user, so do not wait on \
anyone: make a reasonable assumption, state it in your reply, and finish the sub-task yourself."""

SYSTEM_INSTRUCTION = (
    _BASE_INSTRUCTION
    + (_PREVIEW_SUBDOMAIN if PREVIEW_BASE_DOMAIN else _PREVIEW_LOCALHOST)
    + _CLOSING_INSTRUCTION
)


def model_slash_command(set_model) -> slash_commands.SlashCommandDefinition:
    return slash_commands.model_selector(
        choices=SUPPORTED_MODELS,
        set_model=set_model,
        description="Set the model for this sandboxed coding session.",
    )


@workflow.defn(name="SandboxedCodingAgent")
@agent.defn
class SandboxedCodingAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            # Let the runner build the stream so a long session can continue-as-new
            # (passing stream= trades that handover away).
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
            slash_commands=[
                *slash_commands.default_commands(),
                model_slash_command(self._set_model),
            ],
            sandbox=SANDBOX,
        )
        self._model: str = DEFAULT_MODEL
        self._conversation: list[TResponseInputItem] = []
        self._todos: list = []
        # Compound id when this instance is a subagent; gates delegation depth.
        self._agent_id = config.agent_id

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    def _sdk_tools(self) -> list:
        """Harness tools adapted onto the OpenAI Agents SDK."""
        tools = [
            *(as_openai_agent_tool(self._runner, tool) for tool in SANDBOXED_CODING_TOOLS),
            *(
                as_openai_agent_tool(self._runner, tool, injections={"sink": self._todos})
                for tool in (todowrite, todoread)
            ),
        ]
        _, can_delegate = _subagent_policy(self._agent_id)
        if can_delegate:
            # Same workflow on this queue — start_task / task_ask / stop_task.
            tools.extend(
                as_openai_agent_tool(self._runner, tool)
                for tool in agent.subagent_toolset(
                    SandboxedCodingAgentWorkflow, key=SUBAGENT_KEY, task_queue=TASK_QUEUE
                )
            )
        return tools

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the sandboxed coding agent. Ask it to build an app, add a feature, or run
        something; it works on a project inside a cloud sandbox and can serve a web app for live
        preview. Mutating tools pause for your approval."""
        is_root, _ = _subagent_policy(self._agent_id)
        sdk_agent = OpenAIAgent(
            name="SandboxedCodingAgent",
            instructions=SYSTEM_INSTRUCTION if is_root else SYSTEM_INSTRUCTION + SUBAGENT_ADDENDUM,
            model=self._model,
            model_settings=MODEL_SETTINGS,
            tools=[_CODE_INTERPRETER, *self._sdk_tools()],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        result = Runner.run_streamed(
            sdk_agent,
            input=input_items,
            context=self._runner,
            max_turns=MAX_TURNS,
        )
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Apply a slash command to this parent agent session."""
        return TextReply(
            text=(
                f"Unknown sandboxed-coding-agent slash command: `{command.name}`. Try `/model`. "
                "Harness commands include `/approvals`, `/allow-tools`, and `/status`."
            )
        )

    def _set_model(self, model: str) -> None:
        self._model = model

    # Declaring these is what lets a long session roll over into a fresh run rather than
    # growing its history forever; see the harness docs on continue-as-new.
    @agent.snapshot
    def snapshot(self) -> dict[str, Any]:
        """Hand the conversation, the model and the task list to the run that takes over."""
        return {
            "conversation": self._conversation,
            "model": self._model,
            "todos": [item.model_dump(mode="json") for item in self._todos],
        }

    @agent.restore
    def restore(self, state: dict[str, Any]) -> None:
        """Pick the conversation and the task list back up, before the new run's first turn."""
        self._conversation = state["conversation"]
        self._model = state["model"]
        self._todos = [TodoItem.model_validate(item) for item in state["todos"]]
