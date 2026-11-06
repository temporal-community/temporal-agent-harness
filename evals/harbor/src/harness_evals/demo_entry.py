"""Run a checkout example with the demo's documented compatibility fix."""

import runpy
import sys

from harness_evals.demo import AGENTS


def fix_openai_tool_events() -> None:
    # The observer at the initial evals branch base eagerly evaluates event.name in dict.pop's
    # default. Arguments-done events do not have that field. Use the metadata
    # already captured by output_item.added; fail explicitly if it is missing.
    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        OpenAIStreamObserver,
        ToolRequested,
        _parse_tool_args,
    )

    def emit(self, event, publisher):
        buffered = self._arg_buffers.pop(event.item_id, "")
        call = self._fn_calls.pop(event.item_id, None)
        if call is None:
            raise RuntimeError("Tool arguments arrived without opening call metadata")
        call_id, name = call
        publisher.publish(
            ToolRequested(
                tool_id=call_id,
                tool_name=name,
                tool_input=_parse_tool_args(event.arguments or buffered),
            )
        )

    OpenAIStreamObserver._emit_tool_requested = emit


if __name__ == "__main__":
    sdk = sys.argv[1]
    if sdk == "openai":
        fix_openai_tool_events()
    runpy.run_module(f"examples.{AGENTS[sdk][2]}.worker", run_name="__main__")
