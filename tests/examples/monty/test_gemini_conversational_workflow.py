from __future__ import annotations

from pathlib import Path

from examples.monty.conversational_gemini_subagent_workflow import (
    MontyChatGeminiSubagentWorkflow,
)
from examples.monty.conversational_gemini_workflow import (
    GEMINI_MODEL_OPERATOR_COMMAND,
    GEMINI_SUPPORTED_MODELS,
    MontyChatGeminiAgentWorkflow,
)
from temporal_agent_harness.web.registry import load_agent_registry


def test_gemini_chat_agent_model_command_choices():
    assert GEMINI_MODEL_OPERATOR_COMMAND.argument is not None
    assert tuple(GEMINI_MODEL_OPERATOR_COMMAND.argument.choices) == GEMINI_SUPPORTED_MODELS
    assert GEMINI_SUPPORTED_MODELS == ("gemini-3.5-flash", "gemini-3.1-flash-lite")


def test_gemini_chat_agent_is_separate_workflow_type():
    workflow_defn = getattr(MontyChatGeminiAgentWorkflow, "__temporal_workflow_definition")
    assert workflow_defn.name == "MontyChatGeminiAgent"


def test_gemini_subagent_agent_is_separate_workflow_type():
    workflow_defn = getattr(
        MontyChatGeminiSubagentWorkflow,
        "__temporal_workflow_definition",
    )
    assert workflow_defn.name == "MontyChatGeminiSubagentAgent"


def test_monty_registry_lists_both_providers_and_subagent_variants():
    registry = load_agent_registry(Path("examples/monty/agents.toml"))
    workflow_types = {agent.workflow_type for agent in registry.agents}

    assert {
        "MontyChatOpenAIAgent",
        "MontyChatOpenAISubagentAgent",
        "MontyChatGeminiAgent",
        "MontyChatGeminiSubagentAgent",
    }.issubset(workflow_types)
