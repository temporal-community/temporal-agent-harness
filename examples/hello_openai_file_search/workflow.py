"""A doc-QA agent on OpenAI's HOSTED file_search — the shape of the main agent, on OpenAI.

The counterpart to ``examples/hello_gemini_enterprise``. That example asked "can a harness Gemini
agent move to GEAP?"; this one asks the question that actually decides the migration: **is OpenAI's
hosted ``file_search`` a drop-in for the Gemini Interactions API's built-in ``file_search``, which
GEAP refuses to serve?**

The agent answers questions over a small vector store (see ``corpus.py``) using
``FileSearchTool`` — OpenAI's *server-side* retrieval tool. That is the same division of labour the
Interactions API's ``file_search`` had: you register documents once, the provider chunks, embeds,
retrieves and cites, and the model calls it without you writing a retrieval loop. Contrast GEAP,
where the equivalent (``Tool(retrieval=vertex_rag_store=…)``) needs a corpus built through a
different library and returns citations in a different shape.

Two things this example is careful to keep honest:

* **The hosted tool is NOT a harness tool.** ``file_search`` runs inside OpenAI's backend, so it
  does not pass through ``run_tool`` — no approval gate, no ``tool_start`` / ``tool_end`` for it
  (harness spec §11 defers hosted tool spans). ``get_weather`` is here alongside it precisely to
  show the contrast in one turn: the harness-owned tool emits its full lifecycle, the hosted one
  doesn't. If retrieval must be approval-gated or fully observable, it has to be a harness tool
  instead — which is the provider-agnostic third option in the GEAP example's README.
* **The proof is the answer, not the plumbing.** ``corpus.py`` states a fact no model could know.
  If the reply contains it, retrieval happened. No need to trust a tool-span event.
"""

from __future__ import annotations

import os

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import FileSearchTool, Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tool
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


TASK_QUEUE = "hello-openai-file-search"
DEFAULT_MODEL = "gpt-5.1"

# The vector store the hosted file_search tool reads.
#
# Read from the ENVIRONMENT at module import, which is the only channel that works here. The
# obvious alternative — have the worker assign this module global at startup — silently does not:
# the Temporal workflow sandbox re-imports this module into its own namespace when a workflow task
# runs, so the sandboxed copy re-runs this line and never sees an assignment made to the host
# module. (An earlier draft did exactly that and every turn failed with MissingVectorStore.) The
# env var survives because the environment is process-global and the sandbox's re-import happens
# after the worker has set it.
#
# So `worker.py` resolves/creates the store, exports OPENAI_VECTOR_STORE_ID, and only then starts
# polling. Consequence, same as react_agent's streaming toggle: it is fixed per worker process and
# NOT recorded in workflow history, so keep it stable for the lifetime of a session.
VECTOR_STORE_ID: str | None = os.environ.get("OPENAI_VECTOR_STORE_ID") or None

SYSTEM_INSTRUCTION = """\
You are a precise documentation assistant for the Temporal Agent Harness.

Answer questions about the harness's internal notes by searching the attached documents first.
Quote specific figures exactly as the documents state them; never guess a number. If the documents
don't cover something, say so plainly.

You also have a `get_weather` tool for a city's current weather — use it if asked about weather."""


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    # Here only as the control in the experiment: a HARNESS tool, next to a HOSTED one, so one
    # turn shows which events each produces.
    return f"It's 72°F and sunny in {city}."


@workflow.defn(name="HelloOpenAIFileSearchAgent")
@agent.defn
class HelloOpenAIFileSearchAgentWorkflow:
    """A doc-QA agent combining OpenAI's hosted file_search with a harness-owned tool."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[TResponseInputItem] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask about the harness's internal benchmark notes (or a city's weather)."""
        if not VECTOR_STORE_ID:
            raise ApplicationError(
                "No vector store configured: OPENAI_VECTOR_STORE_ID was unset when this "
                "workflow module was imported. Start the agent via worker.py, which resolves "
                "or creates the store and exports it before polling begins.",
                type="MissingVectorStore",
                non_retryable=True,
            )

        sdk_agent = OpenAIAgent(
            name="HelloOpenAIFileSearch",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            tools=[
                # HOSTED: runs in OpenAI's backend. The vendored plugin already forwards this
                # tool type through to the model activity — nothing harness-side to add.
                FileSearchTool(vector_store_ids=[VECTOR_STORE_ID]),
                # HARNESS-OWNED: dispatched via run_tool, so approval + lifecycle events apply.
                as_openai_agent_tool(self._runner, get_weather),
            ],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        # context=self._runner threads the harness runner to the streaming seam so this turn's
        # model events land on the turn stream.
        result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
