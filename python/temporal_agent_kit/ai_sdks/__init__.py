"""First-class integrations between the Temporal-native harness and AI SDKs.

Each integration turns an AI SDK's calls into durable Temporal activities, so workflow code
can use the SDK it already knows while inheriting Temporal's durability, retries, and
observability. One subpackage per SDK; more are expected over time.

  * :mod:`temporal_agent_kit.ai_sdks.google_genai_plugin` — the Google Gemini SDK.
  * :mod:`temporal_agent_kit.ai_sdks.openai_agents` — the OpenAI Agents SDK.
  * :mod:`temporal_agent_kit.ai_sdks.strands_agents` — the Strands Agents SDK (vendored
    from ``temporalio/ai-integrations``), with the harness glue in
    :mod:`temporal_agent_kit.ai_sdks.strands_agents`.
  * :mod:`temporal_agent_kit.ai_sdks.pydantic_ai` — Pydantic AI (glue for the
    unmodified upstream ``pydantic_ai.durable_exec.temporal`` plugin).
  * :mod:`temporal_agent_kit.ai_sdks.typesafe` — typed decisions through
    the canonical ``temporalio-typesafe`` plugin.
  * :mod:`temporal_agent_kit.ai_sdks.jev` — a Jev-backed auto mode evaluator for tool
    approvals, built on the same plugin.
"""
