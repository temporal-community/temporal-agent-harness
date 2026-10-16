// Compile-time checks that a generated agent type drives the session's generics: `tsc` fails
// the build if any of these stop holding. The types below have the shape harness-codegen
// emits (see packages/codegen/test/golden/TicTacToeAgent.ts).

import type { HarnessMessage } from "../src/messages.ts";
import type { PlainSessionState } from "../src/plainState.ts";
import type { AgentSessionCore } from "../src/session.ts";

interface Board {
  cells: ("X" | "O" | null)[];
  status: "playing" | "X_wins" | "O_wins" | "draw";
}
interface NewGame {
  agent_goes_first?: boolean;
}
interface PlayMove {
  cell: number;
}
interface TextReply {
  text: string;
}
interface TicTacToeAgent {
  handlers: {
    new_game: { input: NewGame; output: TextReply };
    play: { input: PlayMove; output: TextReply };
  };
  states: {
    board: Board;
  };
}

type Equals<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
const holds = <T extends true>(): T => true as T;

declare const core: AgentSessionCore<TicTacToeAgent>;
declare const state: PlainSessionState<TicTacToeAgent>;

export function checks(): void {
  void core.sendMessage("play", { cell: 5 });
  void core.sendMessage("new_game", {});
  // @ts-expect-error a move must name its cell
  void core.sendMessage("play", {});
  // @ts-expect-error the agent has no such handler
  void core.sendMessage("resign", {});

  holds<Equals<NonNullable<typeof state.states.board>, Board>>();

  const message = state.messages[0]!;
  if (message.handler === "play") {
    holds<Equals<typeof message.input, PlayMove>>();
    holds<Equals<typeof message.output, TextReply | null>>();
  }
  holds<Equals<HarnessMessage<TicTacToeAgent>["handler"], "new_game" | "play">>();
}
