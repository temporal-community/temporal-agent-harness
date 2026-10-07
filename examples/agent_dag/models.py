"""Pydantic models that cross the Temporal converter for the agent DAG example.

NB: no ``from __future__ import annotations`` — stringized annotations trip the pydantic
converter, the handler-parameter schemas the UI renders forms from, and the Code Mode stubs a
flow script is type-checked against.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from temporal_agent_harness.harness.state import HarnessState

# The tools a step agent may be given. `step_tools.py` maps each name to the harness tool it
# runs, and checks at import that the two agree.
StepToolName = Literal[
    "search_flights",
    "search_hotels",
    "book_flight",
    "book_hotel",
    "get_trip_summary",
    "get_weather",
]

StepModel = Literal["gpt-6-luna", "gpt-6.1-sol"]

DEFAULT_STEP_MODEL: StepModel = "gpt-6-luna"


# ---------------------------------------------------------------------------
# DagStepAgent: its init data and observable state
# ---------------------------------------------------------------------------


class StepSpec(BaseModel):
    """Everything that makes one step agent what it is. A flow script sets it per step."""

    name: str = Field(min_length=1, max_length=60, description="A short name for the step.")
    instructions: str = Field(description="The step agent's system prompt.")
    tools: list[StepToolName] = Field(
        default_factory=list, description="The tools this step agent may call."
    )
    model: StepModel = DEFAULT_STEP_MODEL


class StepProfile(HarnessState):
    """The step agent's spec, published so a client can label the step it is watching."""

    name: str = ""
    instructions: str = ""
    tools: list[str] = Field(default_factory=list)
    model: str = ""


# ---------------------------------------------------------------------------
# DagBuilderAgent: accepted messages, and the `run_agent` host function's shapes
# ---------------------------------------------------------------------------


class RunFlow(BaseModel):
    """A flow script to run: Python that calls `run_agent` to start step agents, in sequence
    or in parallel, and returns what the last of them produced."""

    script: str


class AgentStep(BaseModel):
    """One step of a flow: a fresh agent, set up for this step alone, and the task to give it."""

    name: str = Field(description="A short name for the step, e.g. 'Flight scout'.")
    instructions: str = Field(description="The step agent's system prompt: who it is and how it works.")
    prompt: str = Field(
        description="The task for this run, including any upstream steps' outputs it needs."
    )
    tools: list[StepToolName] = Field(
        default_factory=list, description="The tools the step agent may call. Omit for none."
    )
    model: StepModel = DEFAULT_STEP_MODEL


class AgentStepResult(BaseModel):
    name: str = Field(description="The step's name.")
    output: str = Field(description="The step agent's final reply.")

    def __str__(self) -> str:
        # The harness publishes ``str(result)`` as the tool's ``tool_output``.
        return json.dumps(self.model_dump(), indent=2)
