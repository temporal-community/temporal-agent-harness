# ABOUTME: Nexus-brokered subagent support for the harness, split into two independent
# submodules — import ``subagents.transport``/``subagents.registry`` directly, never this
# top-level package. Deliberately NOT re-exported here: ``subagents.transport`` (NexusTransport,
# driving an already-identified agent over a KNOWN endpoint) has zero dependency on the agent
# registry concept, and importing this package's own ``__init__`` would defeat that if it
# eagerly pulled in ``subagents.registry`` too. See each submodule's own docstring.
