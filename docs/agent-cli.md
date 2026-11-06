# Agent CLI

Install the Temporal CLI separately, then install the Harness extension with Python 3.11+:

```sh
uv tool install 'temporal-agent-harness[cli]'
uv tool update-shell
```

From a repository checkout, use `uv tool install --editable './python[cli]'` instead.
The extension must be on `PATH` for `temporal agent` to work.
Temporal and the agent's worker must already be running.

The CLI reads `./agents.toml` or `~/.config/temporal-agent/agents.toml`
(under `$XDG_CONFIG_HOME` when set). Use `--registry PATH` to select another registry.
Each agent needs a generated `schema` reference; see [agent registration](../README.md#build-with-it--install-from-pypi).

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

In interactive mode, `ask_user` callbacks prompt for an answer in the terminal.
Other callbacks leave work pending and print instructions. Inspect the request
with `status`, perform the work, and submit its result:

```sh
temporal agent callback <session> <tool-id> --result @result.json
temporal agent follow <session>
```
