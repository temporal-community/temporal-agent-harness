# Claude Agent SDK Temporal Integration


Install with `uv add 'temporal-agent-kit[claude-agent-sdk]'`. Import `ClaudeAgentSDKPlugin` and
`TemporalTransport` from `temporal_agent_kit.ai_sdks.claude_agent_sdk` when following the
[upstream usage guide](https://docs.temporal.io/develop/python/integrations/claude-agent-sdk).

In addition to the upstream guide, use `TemporalAgentKitOptions(runner=runner)` in place of
`ClaudeAgentOptions`, and pass the same options to `ClaudeSDKClient` and `TemporalTransport`.
Claude's replies, thinking, and model calls then stream to the Agent Kit turn:

```python
options = TemporalAgentKitOptions(runner=self._runner)
client = ClaudeSDKClient(options=options, transport=TemporalTransport(options))
```

See the [Claude Agent SDK Hello example](../../../examples/claude_agent_sdk_hello).

To prepare files on the selected Worker before Claude Code starts, register your
setup Activity on the plugin when you create the Worker:

```python
worker = Worker(
    client,
    task_queue="my-agent",
    workflows=[MyAgentWorkflow],
    plugins=[ClaudeAgentSDKPlugin(setup_activities=[stage_files]), AgentKitPlugin()],
)
```

Then pass an async callback to the transport that awaits the Activity on the
supplied task queue:

```python
async def setup_callback(task_queue: str) -> None:
    await workflow.execute_activity(
        stage_files,
        source,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(minutes=5),
    )

transport = TemporalTransport(options, setup_callback=setup_callback)
```

The callback runs in the Workflow, so do the actual work in the setup Activity,
for example downloading files from S3 and writing them to disk.
