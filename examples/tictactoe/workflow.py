"""Tic-tac-toe agent: no LLM in the loop — every decision is a TypeSafe System One judgment.

The other examples in this repo put a generative model in the loop and let it iterate over
tools. This one does not. Each agent turn is ONE call to TypeSafe (``jev``), asking three
typed questions over the same state at once:

* ``move``   — a **Choice** over the empty cells: which one to take. Its probabilities are the
  agent's whole policy; the workflow takes the ``choice`` and acts on it.
* ``threat`` — a **Noul**: can the opponent win on their very next move? (used for the reply)
* ``outlook`` — a **Score** on ``losing`` / ``even`` / ``winning``: how the position stands.

Both halves of a turn are TOOL calls dispatched through ``runner.run_tool``:

1. ``typesafe_system_one`` (``activities.py``) — the model call itself, as an activity-backed
   tool. Its ``tool_start`` carries the exact state + questions sent to TypeSafe and its
   ``tool_end`` the answers, so the console's activity detail shows the whole exchange.
   (The harness's model-interaction events carry only a model id and usage, which is why the
   call is a tool and not a model span for now.)
2. ``place_mark`` (``board.py``) — the action: an inline workflow tool that mutates the
   observable :class:`~.board.Board` state, so the AGENT STATE pane shows the move land.

Rules of the game (legal moves, win detection, rendering) are code in ``board.py``. The model
only supplies the judgment of which legal cell to take, over state the code prepares for it.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from . import activities
    from . import board as game
    from .models import NewGame, PlayMove, SystemOneRequest, SystemOneResult


TASK_QUEUE = "tictactoe-agent"

# Requested model id; TypeSafe resolves ``jev-latest`` to a concrete release (the result's
# ``model`` says which).
MODEL = "jev-latest"

OUTLOOK_LEVELS = ("losing", "even", "winning")


@workflow.defn(name="TicTacToeAgent")
@agent.defn
class TicTacToeAgentWorkflow:
    board = agent.state(game.Board)

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Both tools are marked inherently safe (one reads an API, one writes the agent's
            # own board), so this policy lets a turn flow without a click. Tighten via
            # AgentConfig.approval_policy to approve each judgment and move by hand.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    # ------------------------------------------------------------------ handlers

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def new_game(self, message: NewGame) -> TextReply:
        """Reset the board and start a new game. You are X and open unless
        `agent_goes_first` is set, in which case the agent plays X and opens."""
        agent_mark: game.Mark = "X" if message.agent_goes_first else "O"
        with self.board.mutate() as draft:
            draft.cells = [None] * 9
            draft.moves = []
            draft.status = "playing"
            draft.to_move = "X"
            draft.agent_mark = agent_mark
        if message.agent_goes_first:
            return TextReply(text=await self._agent_turn(prefix="New game — I'm X and I open."))
        return TextReply(
            text=f"New game — you're X, I'm O. Your move.\n\n{self._board_block()}"
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def play(self, message: PlayMove) -> TextReply:
        """Play your mark on an empty cell (1-9, left-to-right, top-to-bottom). The agent
        then judges the position with TypeSafe and answers with its own move."""
        current = self.board.current
        human = game.other(current.agent_mark)
        # An illegal move is normal input, not a failure: report it and leave the turn to the
        # user rather than raising out of the handler.
        try:
            with self.board.mutate() as draft:
                game.apply_move(draft, human, message.cell)
        except ValueError as e:
            return TextReply(text=f"Can't do that: {e}.\n\n{self._board_block()}")

        after = self.board.current
        if after.status != "playing":
            return TextReply(text=self._game_over(f"You play **{message.cell}**."))
        return TextReply(text=await self._agent_turn(prefix=f"You play **{message.cell}**."))

    # ------------------------------------------------------------------ the agent's turn

    async def _agent_turn(self, *, prefix: str) -> str:
        """Judge the position with TypeSafe, act on the judgment through the tool, reply."""
        current = self.board.current
        candidates = game.empty_cells(list(current.cells))
        judgment = await self._judge(current, candidates)

        move = judgment.choices["move"]
        cell = int(move.choice.removeprefix("cell_"))

        # The action: the judgment becomes a tool call, dispatched the normal harness way. The
        # call id correlates its tool_start/tool_end (and the board patch it commits).
        result = await self._runner.run_tool(
            str(workflow.uuid4()),
            game.place_mark,
            game.PlaceMarkRequest(cell=cell),
            injections={"board": self.board},
        )

        threat = judgment.nouls["threat"]
        outlook = judgment.scores["outlook"]
        # Probabilities keyed by the model's cell labels, shown as "5: 62%" for the reply.
        ranked = sorted(move.probabilities.items(), key=lambda kv: -kv[1])
        dist = ", ".join(f"{k.removeprefix('cell_')}: {v:.0%}" for k, v in ranked[:3])
        lines = [
            prefix,
            "",
            (
                f"I play **{cell}** ({game.CELL_NAMES[cell - 1]}) — confidence "
                f"{move.confidence:.0%} · top picks {dist}."
            ),
            (
                f"Threat of an immediate {game.other(current.agent_mark)} win: {threat:.0%}"
                f" · outlook: {_outlook_label(outlook.score)} ({outlook.score:.2f}/2,"
                f" confidence {outlook.confidence:.0%})."
            ),
        ]
        if result.status != "playing":
            return self._game_over("\n".join(lines))
        lines += ["", self._board_block(), "", "Your move."]
        return "\n".join(lines)

    async def _judge(self, current: game.Board, candidates: list[int]) -> SystemOneResult:
        """One System One call: three independent questions over the same board state."""
        me = current.agent_mark
        opponent = game.other(me)
        request = SystemOneRequest(
            model=MODEL,
            state={
                "game": (
                    "Tic-tac-toe on a 3x3 grid. Cells are numbered 1-9 reading left-to-right, "
                    "top-to-bottom. A player wins by holding all three cells of a row, column, "
                    "or diagonal. In `board` an empty cell shows its number; in each "
                    "`lines_through_it` entry '_' is an empty cell."
                ),
                "you_play": me,
                "opponent_plays": opponent,
                "it_is_your_turn": True,
                "board": game.render(list(current.cells)),
                "moves_so_far": [f"{m.mark} took {m.cell}" for m in current.moves],
                # Evidence per candidate: where it is and what is already on each winning line
                # through it, so the model can see a completed line, a block, or a fork.
                "empty_cells": [game.describe_cell(list(current.cells), c) for c in candidates],
            },
            questions={
                "move": {
                    "type": "choice",
                    "instructions": {
                        "question": f"Which empty cell should {me} take right now?",
                        "priorities": [
                            f"Take a cell that completes three {me} in a line (an immediate win).",
                            (
                                f"Otherwise take a cell that blocks {opponent} from completing "
                                "three in a line on their next move."
                            ),
                            (
                                "Otherwise take the cell that creates two lines with two of your "
                                "marks and no opponent mark (a fork), or prevents the opponent's "
                                "fork."
                            ),
                            "Otherwise prefer the center, then corners, then edges.",
                        ],
                    },
                    # Only the legal cells are options: the model cannot pick a cell it is not
                    # offered, so an illegal move is impossible by construction.
                    "criteria": {
                        f"cell_{c}": game.describe_cell(list(current.cells), c)
                        for c in candidates
                    },
                },
                "threat": {
                    "type": "noul",
                    "instructions": (
                        f"Ignoring {me}'s move now, does {opponent} have an empty cell that "
                        f"would complete three {opponent} in a line on their next move?"
                    ),
                },
                "outlook": {
                    "type": "score",
                    "instructions": (
                        f"Assuming both sides play well from here, how does the position stand "
                        f"for {me} before {me} moves?"
                    ),
                    "criteria": [
                        f"{me} is losing: {opponent} can force a win.",
                        f"{me} is even: best play leads to a draw.",
                        f"{me} is winning: {me} can force a win.",
                    ],
                },
            },
        )

        # The model call, dispatched as a tool: tool_start carries `request` (the exact body
        # sent to TypeSafe) and tool_end the answers, so the console shows the whole exchange.
        result: SystemOneResult = await self._runner.run_tool(
            str(workflow.uuid4()), activities.typesafe_system_one, request
        )
        return result

    # ------------------------------------------------------------------ rendering

    def _board_block(self) -> str:
        return f"```\n{game.render(list(self.board.current.cells))}\n```"

    def _game_over(self, prefix: str) -> str:
        status = self.board.current.status
        me = self.board.current.agent_mark
        if status == "draw":
            verdict = "It's a draw."
        elif status[0] == me:
            verdict = f"{me} wins — that's me."
        else:
            verdict = f"{status[0]} wins — well played!"
        return f"{prefix}\n\n{self._board_block()}\n\n**{verdict}** Send `new_game` to play again."


def _outlook_label(score: float) -> str:
    """The nearest rubric level to an expected score that may sit between levels."""
    index = min(range(len(OUTLOOK_LEVELS)), key=lambda i: abs(i - score))
    return OUTLOOK_LEVELS[index]

