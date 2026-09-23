"""The tic-tac-toe board, as OBSERVABLE agent state, plus the one tool that moves on it.

The board is a :class:`~temporal_agent_harness.harness.state.HarnessState` the workflow
declares once, as the class attribute ``board = agent.state(Board)``; from then on every committed
``mutate()`` block publishes RFC 6902 patch ops on the agent's ``turn_events`` stream, which
is what the console's AGENT STATE pane renders — so a move lands as ``replace /cells/4 "O"``
right next to the ``tool_end`` that made it.

``place_mark`` is the agent's only action. It is an ``@agent.tool_defn`` that runs INLINE in
the workflow (the board is the agent's own state, not the outside world) and it is the only
thing the agent's decision — a TypeSafe judgment, see ``workflow.py`` — is allowed to do:
the model picks a cell, the workflow dispatches this tool through ``run_tool``, and the tool
mutates the board. Everything that is a *rule* of the game (legal moves, win detection,
rendering) is plain code here; the model only supplies the judgment of which legal cell to
take.

NB: no ``from __future__ import annotations`` — the tool's annotations are read concretely to
build its model-facing schema, and the state models cross Temporal's pydantic converter.
"""

from typing import Literal

from pydantic import BaseModel, Field

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.state import HarnessState, StateRef

Mark = Literal["X", "O"]
Status = Literal["playing", "X_wins", "O_wins", "draw"]

# The eight winning lines, as 0-based cell indices. Cells are numbered 1-9 for humans (and the
# model) reading left-to-right, top-to-bottom; every public surface uses 1-based numbers.
LINES: tuple[tuple[int, int, int], ...] = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)
LINE_NAMES = (
    "top row",
    "middle row",
    "bottom row",
    "left column",
    "middle column",
    "right column",
    "diagonal (top-left to bottom-right)",
    "diagonal (top-right to bottom-left)",
)
CELL_NAMES = (
    "top-left corner",
    "top edge",
    "top-right corner",
    "left edge",
    "center",
    "right edge",
    "bottom-left corner",
    "bottom edge",
    "bottom-right corner",
)


# ---------------------------------------------------------------------------
# The observable state
# ---------------------------------------------------------------------------


class Move(HarnessState):
    """One move that has been played, in order."""

    mark: Mark
    cell: int = Field(description="1-9, left-to-right, top-to-bottom.")


class Board(HarnessState):
    """The whole game: nine cells, whose turn it is, who the agent is, and the outcome."""

    cells: list[Mark | None] = [None] * 9
    to_move: Mark = "X"
    agent_mark: Mark = "O"
    status: Status = "playing"
    moves: list[Move] = []


def other(mark: Mark) -> Mark:
    return "O" if mark == "X" else "X"


# ---------------------------------------------------------------------------
# Rules (pure; no state access)
# ---------------------------------------------------------------------------


def winner(cells: list[Mark | None]) -> Mark | None:
    for a, b, c in LINES:
        if cells[a] is not None and cells[a] == cells[b] == cells[c]:
            return cells[a]
    return None


def empty_cells(cells: list[Mark | None]) -> list[int]:
    """The playable cells, 1-based."""
    return [i + 1 for i, cell in enumerate(cells) if cell is None]


def status_after(cells: list[Mark | None]) -> Status:
    won = winner(cells)
    if won is not None:
        return "X_wins" if won == "X" else "O_wins"
    return "draw" if all(cell is not None for cell in cells) else "playing"


def render(cells: list[Mark | None]) -> str:
    """The board as three rows of ``X`` / ``O`` / cell number, so an empty cell shows the
    number you would play to take it."""
    shown = [cell or str(i + 1) for i, cell in enumerate(cells)]
    rows = [" | ".join(shown[r * 3 : r * 3 + 3]) for r in range(3)]
    return "\n---+---+---\n".join(f" {row} " for row in rows)


def describe_cell(cells: list[Mark | None], cell: int) -> dict[str, object]:
    """What the model needs to know about one empty cell: where it is and what is already on
    every winning line that runs through it. This is *evidence*, not the decision — the code
    reports the lines, the model judges which cell to take."""
    index = cell - 1
    lines: list[str] = []
    for (a, b, c), name in zip(LINES, LINE_NAMES):
        if index in (a, b, c):
            marks = " ".join(cells[i] or "_" for i in (a, b, c))
            lines.append(f"{name}: {marks}")
    return {"cell": cell, "position": CELL_NAMES[index], "lines_through_it": lines}


def apply_move(board: Board, mark: Mark, cell: int) -> None:
    """Place ``mark`` on ``cell`` of a *draft* board and advance the game. Raises ValueError
    for an illegal move; the caller decides whether that is a reply or a tool error."""
    if board.status != "playing":
        raise ValueError(f"the game is over ({board.status}); start a new game")
    if mark != board.to_move:
        raise ValueError(f"it is {board.to_move}'s turn, not {mark}'s")
    if not 1 <= cell <= 9:
        raise ValueError(f"cell must be 1-9, got {cell}")
    if board.cells[cell - 1] is not None:
        raise ValueError(f"cell {cell} is already taken by {board.cells[cell - 1]}")
    board.cells[cell - 1] = mark
    board.moves.append(Move(mark=mark, cell=cell))
    board.status = status_after(list(board.cells))
    board.to_move = other(mark)


# ---------------------------------------------------------------------------
# The agent's one tool
# ---------------------------------------------------------------------------


class PlaceMarkRequest(BaseModel):
    cell: int = Field(ge=1, le=9, description="Which cell to take, 1-9 left-to-right, top-to-bottom.")


class PlaceMarkResponse(BaseModel):
    mark: Mark
    cell: int
    status: Status
    board: str = Field(
        description="The board after the move, rendered — so the tool_end shows the result "
        "of its own write."
    )


@agent.tool_defn(inherently_safe=True)
async def place_mark(
    request: PlaceMarkRequest, board: agent.Injected[StateRef[Board]]
) -> PlaceMarkResponse:
    """Place the agent's mark on an empty cell (1-9). The one action the agent takes on the
    board; the mark is the agent's own, read from the board, never chosen by the caller."""
    mark = board.current.agent_mark
    # One mutate() block: the cell, the move history, the status and whose turn it is all land
    # in a single version, so a watcher never sees a placed mark with a stale turn.
    with board.mutate() as draft:
        apply_move(draft, mark, request.cell)
    current = board.current
    return PlaceMarkResponse(
        mark=mark, cell=request.cell, status=current.status, board=render(list(current.cells))
    )
