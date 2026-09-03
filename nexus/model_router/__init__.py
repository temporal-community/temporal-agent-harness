"""A model router exposed over Temporal Nexus (Chat Completions / LiteLLM shape).

Standalone — nothing here depends on the OpenAI Agents plugin. Import from the
submodules, not this package: ``service`` pulls in the OpenAI SDK, and keeping
``__init__`` empty means loading the ``workflow`` submodule inside a Temporal
workflow sandbox doesn't drag ``openai`` in unguarded.
"""
