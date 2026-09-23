// The design doc's goal, as a component `tsc` type-checks against the example's generated client
// types. It is never rendered; it is here to be compiled.

import type { TicTacToeAgent } from "../../../examples/tictactoe/client_sdk/TicTacToeAgent";
import { useAgentSession } from "../src/index.js";

export function TicTacToeBoard({ sessionId }: { sessionId: string }) {
  const agent = useAgentSession<TicTacToeAgent>({ sessionId });
  const board = agent.states.board;

  function misuse(): void {
    // @ts-expect-error a move names a cell
    void agent.sendMessage("play", {});
    // @ts-expect-error the agent has no such handler
    void agent.sendMessage("resign", {});
    const message = agent.messages[0];
    // @ts-expect-error a new game has no cell
    if (message?.handler === "new_game") void message.input.cell;
  }
  void misuse;

  return (
    <div>
      {board?.cells.map((cell, i) => (
        <button key={i} onClick={() => agent.sendMessage("play", { cell: i + 1 })}>
          {cell ?? ""}
        </button>
      ))}
      {agent.messages.map((m) => (
        <p key={m.id}>
          {m.handler === "play" ? `play ${m.input.cell}` : "new game"} · {m.output?.text}
        </p>
      ))}
    </div>
  );
}
