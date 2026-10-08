"""Local model fixtures: no credentials or external model requests."""

import json


from temporal_agent_harness.ai_sdks.openai_agents.testing import (
    ResponseBuilders,
    TestModel,
)

QUESTION = "Why is the sky blue?"
ANSWER = (
    "Blue wavelengths scatter more strongly in the atmosphere (Rayleigh scattering)."
)
NOTICE = "Research finished: " + ANSWER


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
        if not outputs:
            response = ResponseBuilders.tool_call(
                json.dumps({"question": QUESTION}), "research"
            )
            response.output[0].call_id = "research-call"
            return response
        if len(outputs) == 1:
            response = ResponseBuilders.tool_call(
                json.dumps({"recipient": "mailbox", "message": NOTICE}), "send_message"
            )
            response.output[0].call_id = "message-call"
            return response
        return ResponseBuilders.output_message(
            "OpenAI adapter completed: " + str(outputs[0]["output"])
        )
