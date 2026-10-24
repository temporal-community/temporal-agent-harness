"""The messages, init data and replies of the swarm triage example's agents."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Simulation(BaseModel):
    """How the scripted agents behave. Every agent draws from these with ``workflow.random()``,
    so a run is replayable even though it looks noisy."""

    think_seconds: tuple[float, float] = Field(
        default=(2.0, 8.0),
        description="How long a worker 'thinks' about one issue, uniformly between the two.",
    )
    consult_rate: float = Field(
        default=0.35, ge=0, le=1, description="How often a worker consults a specialist."
    )
    review_rate: float = Field(
        default=0.5, ge=0, le=1, description="How often a worker asks a reviewer to check."
    )
    rework_rate: float = Field(
        default=0.25,
        ge=0,
        le=1,
        description="How often a reviewer sends an issue back to be reworked once.",
    )
    error_rate: float = Field(
        default=0.05,
        ge=0,
        le=1,
        description="How often a specialist fails, so the worker releases its issue to the "
        "board for another try.",
    )
    approval_rate: float = Field(
        default=0.0,
        ge=0,
        le=1,
        description="How often a worker wants to close an issue as won't-fix, a gated call that "
        "waits for a human to approve it in the console. Each one blocks the run until it is "
        "answered, so this is off unless you want to watch a bottleneck form.",
    )


class TriageRun(BaseModel):
    """Triage a synthetic backlog with a swarm of agents."""

    issues: int = Field(default=400, ge=1, le=5000, description="Issues to post to the board.")
    teams: int = Field(default=8, ge=1, le=26, description="Teams, one lead each.")
    workers_per_team: int = Field(
        default=16, ge=1, le=200, description="Workers each lead keeps busy at once."
    )
    items_per_worker: int = Field(
        default=4,
        ge=1,
        description="Issues a worker handles before it retires and its lead starts a fresh one.",
    )
    specialists: int = Field(default=6, ge=1, le=50, description="Specialists workers consult.")
    reviewers: int = Field(default=6, ge=1, le=50, description="Reviewers workers ask to check.")
    simulation: Simulation = Field(default_factory=Simulation)


class TriageReport(BaseModel):
    """How a triage run went."""

    swarm_id: str
    issues: int
    done: int = Field(description="Issues completed on the board.")
    agents_started: int = Field(description="Every agent the run started, retired ones included.")
    releases: int = Field(description="Times a worker handed an issue back to the board.")
    consults: int
    reviews: int
    reworks: int
    wont_fix: int


class TeamSpec(BaseModel):
    """A lead's init data: its team and everything its workers need."""

    team: str
    workers: int
    items_per_worker: int
    specialists: list[str] = Field(description="Workflow ids of the specialists.")
    reviewers: list[str] = Field(description="Workflow ids of the reviewers.")
    simulation: Simulation


class RunTeam(BaseModel):
    """Keep the team's worker slots filled until the team's issues are gone."""


class TeamReport(BaseModel):
    """What one team did."""

    team: str
    workers_started: int
    done: int
    releases: int
    consults: int
    reviews: int
    reworks: int
    wont_fix: int


class WorkerSpec(BaseModel):
    """A worker's init data."""

    team: str
    specialists: list[str]
    reviewers: list[str]
    simulation: Simulation


class Work(BaseModel):
    """Handle up to ``max_items`` of the team's issues from the board, then reply."""

    max_items: int = Field(ge=1)


class WorkReport(BaseModel):
    """What one worker did in its life."""

    done: int = 0
    releases: int = 0
    consults: int = 0
    reviews: int = 0
    reworks: int = 0
    wont_fix: int = 0
    exhausted: bool = Field(
        default=False, description="Whether it stopped because the team had no open issues."
    )


class ServiceSpec(BaseModel):
    """A specialist's or reviewer's init data."""

    simulation: Simulation


class Consult(BaseModel):
    """Ask for advice on one issue."""

    title: str
    area: str


class Advice(BaseModel):
    """A specialist's advice."""

    priority: str
    note: str


class Review(BaseModel):
    """Ask for a check of one triaged issue."""

    title: str
    label: str
    priority: str


class Verdict(BaseModel):
    """A reviewer's verdict."""

    accepted: bool
    note: str
