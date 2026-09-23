// Compile-time checks on the golden output: `tsc` fails the build if any of these stop holding.

import type { Board, NewGame, PlayMove, TextReply, TicTacToeAgent } from "./golden/TicTacToeAgent.ts";

type Equals<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
const holds = <T extends true>(): T => true as T;

holds<Equals<keyof TicTacToeAgent["handlers"], "new_game" | "play">>();
holds<Equals<TicTacToeAgent["handlers"]["play"]["input"], PlayMove>>();
holds<Equals<TicTacToeAgent["handlers"]["play"]["output"], TextReply>>();
holds<Equals<keyof TicTacToeAgent["states"], "board">>();
holds<Equals<TicTacToeAgent["states"]["board"], Board>>();

// A handler input may omit a defaulted field; a state document always carries every field.
const newGame: NewGame = {};
// @ts-expect-error cell has no default, so a move must name one
const noCell: PlayMove = {};
// @ts-expect-error a published Board always includes status
const partialBoard: Board = { cells: [], to_move: "X", agent_mark: "O", moves: [] };

export { newGame, noCell, partialBoard };
