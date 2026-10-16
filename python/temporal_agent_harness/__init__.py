"""Temporal-native agent harness (experimental).

A Temporal-native outer agent harness that gives you the full power of Temporal's
durable-execution primitives while letting you keep using the AI SDKs you already know
(the inner harness), via first-class integrations under :mod:`temporal_agent_harness.ai_sdks`.

Subpackages:
  * :mod:`temporal_agent_harness.harness`  — the core harness: the agent workflow runner,
    the agent/subagent protocol, tool definitions, and human-in-the-loop tool approvals.
  * :mod:`temporal_agent_harness.ai_sdks`  — integrations that make AI SDK calls durable
    Temporal activities (currently the Google Gemini SDK).
  * :mod:`temporal_agent_harness.plugin`   — ``AgentHarnessPlugin``, the one Temporal plugin
    that wires a client + worker for all of the above: it supplies the harness's data
    converter and registers its activities, so a worker declares only its workflows.
"""
