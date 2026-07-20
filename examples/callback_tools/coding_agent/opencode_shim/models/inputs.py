"""Message related models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import OpenCodeBaseModel
from .common import ModelRef, TextSpan  # noqa: TC001
from .parts import FilePartSource  # noqa: TC001


class TextPartInput(OpenCodeBaseModel):
    """Text part for input."""

    type: Literal["text"] = Field(default="text", init=False)
    text: str


class FilePartInput(OpenCodeBaseModel):
    """File part for input (image, document, etc.)."""

    type: Literal["file"] = Field(default="file", init=False)
    mime: str
    filename: str | None = None
    url: str  # Can be data: URI or file path
    source: FilePartSource | None = None


class AgentPartInput(OpenCodeBaseModel):
    """Agent mention part for input - references a sub-agent to delegate to.

    When a user types @agent-name in the prompt, this part is created.
    """

    type: Literal["agent"] = Field(default="agent", init=False)
    name: str
    """Name of the agent to delegate to."""
    source: TextSpan | None = None
    """Source location in the original prompt text."""


class SubtaskPartInput(OpenCodeBaseModel):
    """Subtask part for input - spawns a subtask to another agent."""

    type: Literal["subtask"] = Field(default="subtask", init=False)
    prompt: str
    """The prompt for the subtask."""
    description: str
    """Description of what the subtask does."""
    agent: str
    """The agent to handle this subtask."""
    model: ModelRef | None = None
    """Optional model to use for the subtask."""


PartInput = TextPartInput | FilePartInput | AgentPartInput | SubtaskPartInput
