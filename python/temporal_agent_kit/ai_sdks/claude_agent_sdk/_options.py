"""Claude Agent SDK options for Temporal Agent Kit streaming."""

from dataclasses import dataclass
from typing import cast

from claude_agent_sdk import ClaudeAgentOptions, HookContext, HookInput, HookJSONOutput, HookMatcher
from claude_agent_sdk.types import HookEvent

from temporal_agent_kit.agent_protocol import ModelInteractionStarted
from temporal_agent_kit.agent_workflow import AgentWorkflowRunner
from temporal_agent_kit.stream_context import TurnStreamContext

# The events that come right before Claude calls the model.
# The SDK's HookEvent type is missing PostToolBatch, hence the cast.
_TEMPORAL_AGENT_KIT_EVENTS: tuple[HookEvent, ...] = (
    "UserPromptSubmit",
    cast(HookEvent, "PostToolBatch"),
)

class _TemporalAgentKitMatcher(HookMatcher):
    """A HookMatcher labelled as ours, so we can tell it apart from the user's."""

@dataclass(kw_only=True)
class TemporalAgentKitOptions(ClaudeAgentOptions):
    """Claude Agent SDK options that report model calls to an Agent Kit runner.

    Your own `hooks` are kept. Unless you set `thinking` or `max_thinking_tokens`,
    Claude thinks adaptively and streams a summary of its thinking. If a subclass
    overrides `__post_init__`, it must call `super().__post_init__()`.
    """

    runner: AgentWorkflowRunner
    include_partial_messages: bool = True

    def __post_init__(self) -> None:
        self._stream_context: TurnStreamContext | None = None

        # Without a display, Claude Code streams thinking with no text to show.
        # thinking takes precedence over max_thinking_tokens, so keep either one.
        if self.thinking is None and self.max_thinking_tokens is None:
            self.thinking = {"type": "adaptive", "display": "summarized"}

        new_hooks: dict[HookEvent, list[HookMatcher]] = {}

        # Copy the user's hooks, skipping any of ours left over from a copy.
        if self.hooks:
            for event, matchers in self.hooks.items():
                kept = []
                for matcher in matchers:
                    if not isinstance(matcher, _TemporalAgentKitMatcher):
                        kept.append(matcher)
                new_hooks[event] = kept

        # Add ours to the end of each model-start event.
        ours = _TemporalAgentKitMatcher(hooks=[self._on_model_start])
        for event in _TEMPORAL_AGENT_KIT_EVENTS:
            if event not in new_hooks:
                new_hooks[event] = []
            new_hooks[event].append(ours)

        self.hooks = new_hooks

    async def _on_model_start(
        self, _input: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        self.runner.publish(
            ModelInteractionStarted(model=self.model or "Claude"),
            stream_context=self._stream_context,
        )
        return {}
