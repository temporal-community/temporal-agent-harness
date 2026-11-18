# Python authoring

Install `temporal-agent-kit` with the integration extra you use: `openai-agents`, `genai`,
`pydantic-ai`, `strands-agents`, or `typesafe`. `ui` adds the web server dependencies; `code-mode` adds the sandbox.
Pin the package and lock dependencies. Use `temporal-agent-kit init ./my-agent` for the bundled
OpenAI example, then follow its README. To keep an existing project, adapt only the needed files.
The same starter is installed with this skill under `assets/starter/`; read
`my_agent/workflow.py.tmpl` and `my_agent/worker.py.tmpl` there for the complete wiring.

Public entry points:

- `temporal_agent_kit.agent`: `defn`, `init`, `accepts`, `state`, tool declarations.
- `temporal_agent_kit.agent_workflow.AgentWorkflowRunner`: runner for tools and turns.
- `temporal_agent_kit.agent_protocol`: `AgentConfig`, `TextMessage`, `TextReply`,
  `ToolApprovalPolicy`, and `MidTurn`.
- `temporal_agent_kit.plugin.AgentKitPlugin`: registration and payload conversion.

A minimal agent, with a safe inline tool and a model-free handler:

```python
from temporalio.contrib.workflow_streams import WorkflowStream

from temporal_agent_kit import agent
from temporal_agent_kit.agent_protocol import AgentConfig, TextMessage, TextReply, ToolApprovalPolicy
from temporal_agent_kit.agent_workflow import AgentWorkflowRunner


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return sample weather for a city."""
    return f"It's 72°F and sunny in {city}."


@agent.defn(name="HelloAgent")
class HelloAgentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config, stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @agent.accepts
    async def ping(self, message: TextMessage) -> TextReply:
        """Echo the message without a model call."""
        return TextReply(text=message.text)
```

The starter's `ask` handler adds the model turn: it passes `get_weather` through
`as_openai_agent_tool(self._runner, ...)` and drives `Runner.run_streamed(..., context=self._runner)`.

`@agent.defn` makes the class a workflow. `@agent.init` receives `AgentConfig` and optional typed
init data. `@agent.accepts` declares typed handlers using Pydantic inputs/outputs. Declare tools
with `@agent.tool_defn` for deterministic workflow work or `@agent.activity_tool_defn` for I/O.
Use `ToolApprovalPolicy.allow_inherently_safe()` to automatically permit tools explicitly marked
safe; other tools remain gated. Do not disable all approvals merely to make an example run.

For OpenAI, use Agent Kit's `OpenAIAgentsPlugin` and `as_openai_agent_tool`. The starter shows
`stream_to_provider`, `kit_observer_factory`, and consuming `Runner.run_streamed` events:
all are necessary for the Agent Kit streaming path. Add `AgentKitPlugin` last on the Temporal
client, then construct its worker. Activity tools belong in the plugin's `tools` registration.

For Strands Agents, build the agent in `@agent.init` with `kit_strands_agent(runner, ...)` from
`ai_sdks.strands_agents`, passing Agent Kit tools through `as_strands_tool(runner, ...)` and
MCP servers as `TemporalMCPClient(name)`; every call runs under the approval policy. On the worker,
register `StrandsPlugin(models=..., observer_factory=kit_observer_factory)` before
`AgentKitPlugin`. Strands interrupts are refused: use a callback tool to ask a person.

Generate schemas with `temporal-agent-kit schema module:Class -o Agent.schema.json`.
An application module must be installed or importable from the current project. Required init
data produces a startup form in the console. Keep workflow name, registry name, and schema aligned.

Run the local stack with `temporal-agent-kit dev agents.toml` (session manager plus API and
console in one process) beside Temporal and the agent worker. Use
`temporal-agent-kit doctor --registry agents.toml` to diagnose it. `--json` is suitable for
scripts; a failed check exits nonzero. It never starts or resets services.
