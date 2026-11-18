# ABOUTME: Mid-turn steering's value types — one queued steering message, and the batch
# ``AgentWorkflowRunner.claim_steering`` hands to the code building model input, which renders
# it as the XML block appended to a tool result.

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

STEERING_PREAMBLE = (
    "The user sent the following while you were working. "
    "Take them into account before continuing."
)


@dataclass(frozen=True)
class SteeringMessage:
    """One piece of steering text: its own id, and the message whose handler queued it."""

    steering_id: str
    message_id: str
    text: str


@dataclass(frozen=True)
class ClaimedSteering:
    """Every steering message that was queued when ``claim_steering`` ran, in arrival order."""

    messages: tuple[SteeringMessage, ...]

    @property
    def steering_ids(self) -> list[str]:
        return [m.steering_id for m in self.messages]

    def render(self) -> str:
        """The block to append to model input.

        The explanatory line is inside the block because the harness does not own the system
        prompt: without it the model sees an unfamiliar tag at the end of a tool result and has
        to guess whether the tool produced it. Message ids are left out; the model can do
        nothing with them, and events carry them instead.
        """
        body = "\n".join(f"<message>{escape(m.text)}</message>" for m in self.messages)
        return f"<steering_messages>\n{STEERING_PREAMBLE}\n{body}\n</steering_messages>"

    def append_to(self, text: str) -> str:
        """``text`` with the rendered block appended after a blank line."""
        return f"{text}\n\n{self.render()}" if text else self.render()
