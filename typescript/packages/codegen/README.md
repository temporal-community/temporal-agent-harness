# @temporal-agent-harness/codegen

Generates TypeScript types for one harness agent from the JSON Schema document the Python
package prints for it (from `python/`):

```sh
uv run temporal-agent-harness schema examples.tictactoe.workflow:TicTacToeAgentWorkflow \
  | harness-codegen - -o TicTacToeAgent.ts
```

The output has an interface per model the agent exposes (its `@agent.accepts` input and output
models and its `agent.state(...)` types) and one mapping type naming them:

```ts
export interface TicTacToeAgent {
  handlers: {
    new_game: { input: NewGame; output: TextReply };
    play: { input: PlayMove; output: TextReply };
  };
  states: {
    board: Board;
  };
}
```

Handler inputs are typed as a sender writes them (a field with a default is optional); handler
outputs and states as they arrive on the wire (every field present). Tool inputs and outputs are
not typed.

Options: `-o <file>` writes to a file instead of stdout; `--name <TypeName>` renames the mapping
type, which defaults to the agent's workflow type name.

## The event protocol

With `--protocol`, it reads `temporal-agent-harness schema --protocol` instead and writes the
event stream's types. The client package (`typescript/packages/client`) commits that output as
its `src/protocol.ts`; `bazel run //typescript/packages/client:protocol_update` regenerates it.

## Development

```sh
pnpm install
pnpm test   # compiles src/ and test/ (including compile-time type checks), then runs node --test
```

`test/golden/TicTacToeAgent.ts` is generated from the tic-tac-toe example's committed schema,
`python/examples/tictactoe/TicTacToeAgent.schema.json`;
`bazel run //typescript/packages/codegen:golden_update` regenerates it.
