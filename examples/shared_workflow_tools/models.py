"""Local fixtures drive actual SDK loops for both parent and child agents."""

import json

from temporal_agent_harness.ai_sdks.openai_agents.testing import (
    ResponseBuilders,
    TestModel,
)

QUESTION = "Why is the sky blue?"
ANSWER = (
    "Blue wavelengths scatter more strongly in the atmosphere (Rayleigh scattering)."
)
FIRST_INSTRUCTION = "Explain the physical cause briefly."
FOLLOWUP_INSTRUCTION = "Now explain it for a five-year-old."


def tool_response(name, arguments, call_id):
    response = ResponseBuilders.tool_call(json.dumps(arguments), name)
    response.output[0].call_id = call_id
    return response


class LocalOpenAIModel(TestModel):
    def __init__(self):
        super().__init__(lambda: ResponseBuilders.output_message("unused"))

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        outputs = [
            item
            for item in input
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        if any(tool.name == "start_researcher" for tool in tools):
            count = len(outputs)
            if count == 0:
                return tool_response("start_researcher", {}, "start")
            handle = outputs[0]["output"]
            steps = {
                1: (
                    "send_message",
                    {"recipient": handle, "message": FIRST_INSTRUCTION},
                ),
                2: (
                    "researcher_ask",
                    {"subagent": handle, "message": {"text": QUESTION}},
                ),
                3: (
                    "send_message",
                    {"recipient": handle, "message": FOLLOWUP_INSTRUCTION},
                ),
                4: (
                    "researcher_ask",
                    {
                        "subagent": handle,
                        "message": {"text": "Explain that more simply."},
                    },
                ),
                5: ("stop_researcher", {"subagent": handle}),
            }
            if count in steps:
                name, arguments = steps[count]
                return tool_response(name, arguments, f"parent-{count}")
            return ResponseBuilders.output_message(
                "Parent/child exchange completed. "
                + str(outputs[2]["output"])
                + "\nFollow-up: "
                + str(outputs[4]["output"])
            )
        prompt = next(
            item["content"]
            for item in input
            if isinstance(item, dict) and item.get("role") == "user"
        )
        if not outputs:
            return tool_response(
                "send_message",
                {"recipient": "parent", "message": "Working on: " + prompt},
                "child-progress",
            )
        return ResponseBuilders.output_message(ANSWER + "\nUsed " + prompt)
