# Agent CLI

`temporal agent` runs agents built with the Temporal Agent Harness from your terminal.
Run it in the directory that holds the agents: your project, or an example under
`python/examples/` in this repository. It works the same everywhere.

## Set up

1. Install the [Temporal CLI](https://docs.temporal.io/cli) and the Harness extension
   (Python 3.11+), and put both on `PATH`:

   ```sh
   uv tool install 'temporal-agent-harness[cli]'
   uv tool update-shell
   ```

   From a Harness checkout, use `uv tool install --editable './python[cli]'`.

2. Give the CLI an `agents.toml` registry with a generated schema for each agent
   (see [agent registration](../README.md#build-with-it--install-from-pypi)).
   Generate a schema from the agent's workflow class and reference it from the registry:

   ```sh
   temporal-agent-harness schema my_app.workflow:MyAgentWorkflow -o MyAgent.schema.json
   ```

   ```toml
   [[agents]]
   key = "my-agent"
   workflow_type = "MyAgent"
   task_queue = "my-agent-task-queue"
   schema = "MyAgent.schema.json"   # relative to agents.toml
   ```

   Regenerate the schema when the agent's inputs change. The examples here already
   include theirs.

3. Start Temporal and the agent's worker. The CLI talks to the agent's workflow
   directly, so it needs no session manager or web server.

4. Run the agent from the directory containing `agents.toml`:

   ```sh
   temporal agent my-agent --interactive
   ```

   The CLI reads `./agents.toml`, else `~/.config/temporal-agent/agents.toml` (under
   `$XDG_CONFIG_HOME` when set). Use `--registry PATH` to select another registry.
   It connects with your Temporal SDK configuration (a `temporal.toml` profile and
   `TEMPORAL_*` variables); `--profile`, `--address` and `--namespace` override it.

Discover commands and agent inputs through help:

```sh
temporal agent --help
temporal agent <agent> --help
```

For agents with several operations, select the operation shown in help:
`temporal agent <agent> <operation> --help`.

## Sessions

An invocation sends a message and waits for its response. It starts a new session
unless `--session` selects an existing one. Supply the input flags shown in help,
or a complete JSON message:

```sh
temporal agent <agent> --input @message.json
temporal agent <agent> --session <session> --input @message.json
temporal agent status <session>
temporal agent follow <session>
temporal agent close <session>
```

`--detach` returns after the message is accepted. Ctrl+C stops waiting while the
agent continues; use `follow` to reconnect. `close` ends the session.

## Interactive mode

Use `--interactive` to send successive messages in one terminal conversation:

```sh
temporal agent <agent> --interactive
temporal agent <agent> --session <session> --interactive
```

Type a message and press Enter. Resuming displays earlier messages and replies.
`/exit` or Ctrl+D leaves the session open and prints a resume command.
History is retained and used according to the agent's implementation.

Interactive mode requires a terminal and a handler with exactly one required
string input field. Supply any other required fields through their flags.

## Approvals and callbacks

Tools requiring approval prompt before execution; Enter denies. Without terminal
input, the CLI leaves the decision pending and prints instructions. You can also
resolve it explicitly, then follow the session:

```sh
temporal agent approve <session> <tool-id>
temporal agent deny <session> <tool-id> --reason "Explain the decision"
temporal agent follow <session>
```

`ask_user` callbacks prompt for an answer in the terminal.
Other callbacks leave work pending and print instructions. Inspect the request
with `status`, perform the work, and submit its result:

```sh
temporal agent callback <session> <tool-id> --result @result.json
temporal agent follow <session>
```
