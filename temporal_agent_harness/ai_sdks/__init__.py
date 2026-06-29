"""First-class integrations between the Temporal-native harness and AI SDKs.

Each integration turns an AI SDK's calls into durable Temporal activities, so workflow code
can use the SDK it already knows while inheriting Temporal's durability, retries, and
observability. One subpackage per SDK; more are expected over time.

  * :mod:`temporal_agent_harness.ai_sdks.google_genai_plugin` — the Google Gemini SDK.
  * :mod:`temporal_agent_harness.ai_sdks.openai_agents_plugin` — the OpenAI Agents SDK.
"""
