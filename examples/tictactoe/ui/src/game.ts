// Reading the game off the agent's typed state and messages. Nothing here knows how a move is
// applied: the board is whatever the agent's `board` state says it is.

import type { HarnessMessage } from "@temporal-agent-harness/svelte";

import type { Board, TicTacToeAgent } from "../../client_sdk/TicTacToeAgent";

export type Mark = Board["to_move"];

const LINES = [
  [0, 1, 2], [3, 4, 5], [6, 7, 8],
  [0, 3, 6], [1, 4, 7], [2, 5, 8],
  [0, 4, 8], [2, 4, 6]
] as const;

export function winningLine(cells: Board["cells"]): readonly number[] | null {
  return LINES.find(([a, b, c]) => cells[a] && cells[a] === cells[b] && cells[b] === cells[c]) ?? null;
}

export function other(mark: Mark): Mark {
  return mark === "X" ? "O" : "X";
}

/**
 * What the TypeSafe tool returned. Tool results are opaque to the harness's generated types, so
 * this is the example's own reading of `SystemOneResult` (examples/tictactoe/models.py).
 */
export interface Judgment {
  model: string;
  input_tokens: number | null;
  output_tokens: number | null;
  nouls: Record<string, number>;
  choices: Record<string, { choice: string; confidence: number; probabilities: Record<string, number> }>;
  scores: Record<string, { score: number; confidence: number }>;
}

/** The last completed TypeSafe judgment in the session, if any. */
export function lastJudgment(messages: readonly HarnessMessage<TicTacToeAgent>[]): Judgment | null {
  for (let m = messages.length - 1; m >= 0; m--) {
    const parts = messages[m]!.parts;
    for (let p = parts.length - 1; p >= 0; p--) {
      const part = parts[p]!;
      if (part.type !== "tool" || part.toolName !== "typesafe_system_one") continue;
      if (part.state !== "done" || part.output === null) continue;
      try {
        return JSON.parse(part.output) as Judgment;
      } catch {
        return null;
      }
    }
  }
  return null;
}

export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}
