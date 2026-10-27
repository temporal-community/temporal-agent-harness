"""The model: Gemini, through the Interactions API. Everything here is about talking to Gemini.

``workflow.py`` is the harness side: the agent, its tools, and the policy every tool call goes
through. This file is the other side, the model in the loop. It knows how to prompt Gemini,
send it the conversation, and feed the tool calls it makes back to it, and it hands each tool
call to the harness with ``runner.run_tool``, which is where governance happens. Swapping in
another model means replacing this file; nothing in ``workflow.py`` changes.

The Gemini calls themselves run as activities through the harness's Gemini plugin
(``google_genai_client``), so they're durable too, and reply text streams to the console as it
is generated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import workflow
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai._interactions.types import FunctionCallStep
    from google.genai._interactions.types.function_result_step_param import (
        FunctionResultStepParam,
    )

    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        InteractionConversation,
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

DEFAULT_MODEL = "gemini-3.8-flash"

SYSTEM_INSTRUCTION = """\
You are a data analyst at a bank. You answer the user's business questions from the bank's \
data, which lives in three DuckDB databases:

- `analytics`: branches, branch_deposits, branch_metrics, product_performance, \
transactions_daily. Quarters are strings like '2026-Q3'; the data runs from 2025-Q4 to \
2026-Q3, so "last quarter" is 2026-Q3.
- `risk`: loan_applications, credit_scores. Restricted: reading a risk table needs a person \
to grant you access.
- `customer_pii`: customer names, SSNs, addresses and account numbers. Never available to you.

You work by writing async Python scripts and running them with the `run_analysis` tool. Its \
description lists the host functions a script can call, with their exact signatures and result \
shapes: discovering tables (`list_tables`, `describe_table`), querying (`run_sql`, DuckDB SQL, \
tables named `<database>.<table>`), statistics over a query's result (`summary_stats`, \
`calc_distribution`, `correlate`, `group_compare`, `detect_outliers`), asking a person for \
access (`request_access`), and drawing charts for the user (`render_bar_chart`, \
`render_line_chart`, `render_pie_chart`, `render_scatter_chart`), and publishing a shareable \
report (`publish_report`). Results are dicts; index them \
with normal Python. Compose the functions freely: a script can query, compute, chart and \
combine as much as it needs, and return what you want to read.

A tool policy checks every host call before it runs. When it refuses one, the call raises an \
error that says why. If a risk table needs access, call `request_access` with the table and \
a one-sentence reason the person can judge, then run the query again once it is granted. The \
user may also grant access without being asked, and may revoke it at any time. Whenever the \
policy refuses a risk table for lack of access, even one you could read earlier, ask again. \
If the person declines, or the policy refuses for any other reason, tell the user what was \
refused and why; don't look for another way to the same data.

Answer in plain prose with the numbers that matter. Say which tables you used, and state your \
assumptions. When a comparison, trend or distribution reads better as a picture, draw it: \
each chart function returns a `chart_id`, and writing `[chart:<chart_id>]` on a line of its \
own in your answer shows the chart there. Only reference charts a script actually drew. When \
the user asks to share or publish findings, publish a report whose sections embed the charts \
the same way, and give the user its link."""


class GeminiChat:
    """One Gemini conversation that runs its tool calls through the harness.

    ``tools`` are harness tools (``@agent.*_tool_defn`` or ``code_mode_tool``): Gemini sees
    each one's generated function declaration, and every call it makes goes to
    ``runner.run_tool``, which applies the agent's approval policy before the tool runs.
    """

    def __init__(
        self,
        runner: AgentWorkflowRunner,
        tools: Sequence[Callable[..., Awaitable[Any]]],
        *,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._runner = runner
        self._tools = {tool.__name__: tool for tool in tools}
        self._model = model
        # The whole conversation, sent as the input of every call (see InteractionConversation
        # for why it isn't chained by interaction id).
        self._conversation = InteractionConversation()
        # Notes for Gemini that arrived outside a turn's tool calls, sent with the next turn.
        self._notes: list[str] = []
        # Gemini calls run as activities; the runner streams the reply text as it arrives.
        self._client = google_genai_client(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=3)),
            runner=runner,
        )

    def tell(self, note: str) -> None:
        """Tell Gemini something with the next turn's message. Not mid-turn: the conversation
        can't take text between a tool call and its result."""
        self._notes.append(note)

    async def turn(self, user_text: str) -> str:
        """Run one conversational turn: let Gemini call tools until it replies in prose."""
        declarations = [function_param(tool) for tool in self._tools.values()]
        if self._notes:
            user_text = "\n".join(f"(Note: {note})" for note in self._notes) + "\n\n" + user_text
            self._notes.clear()
        self._conversation.add_user_text(user_text)
        while True:
            stream = await self._client.interactions.create(
                model=self._model,
                input=self._conversation.steps,
                system_instruction=SYSTEM_INSTRUCTION,
                tools=declarations,
                stream=True,
            )
            reply = await self._conversation.read_reply(stream)
            if not reply.function_calls:
                return reply.text
            self._conversation.add_function_results(
                await asyncio.gather(*(self._call(call) for call in reply.function_calls))
            )

    async def _call(self, call: FunctionCallStep) -> FunctionResultStepParam:
        """Run one of Gemini's tool calls through the harness; an error goes back to Gemini
        as the call's result."""
        is_error = False
        try:
            tool = self._tools.get(call.name)
            if tool is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            output = await self._runner.run_tool(call.id, tool, injections=None, **call.arguments)
            result = output.model_dump_json() if isinstance(output, BaseModel) else str(output)
        except Exception as e:
            result, is_error = str(e), True
        response: FunctionResultStepParam = {
            "type": "function_result",
            "call_id": call.id,
            "name": call.name,
            "result": result,
        }
        if is_error:
            response["is_error"] = True
        if call.signature:
            response["signature"] = call.signature
        return response
