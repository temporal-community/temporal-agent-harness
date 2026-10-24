# Swarm Triage

A backlog of synthetic issues, triaged by a swarm of hundreds of agents that coordinate through a
shared board and talk to each other as peers. Nothing calls a model: every agent "thinks" by
sleeping a random while and decides with `workflow.random()`. A run costs nothing, needs no API
key, and replays exactly. It exists to exercise orchestration at scale, and the views of it, not
to triage anything.

```
TriageDispatcher            posts the backlog to the board, starts everyone else
  ├─ TriageSpecialist × S   peers any worker consults            (ask_peer → consult)
  ├─ TriageReviewer × R     peers any worker asks for a check    (ask_peer → review)
  └─ TriageLead × T         one per team; keeps W worker slots filled
       └─ TriageWorker      claims its team's issues from the board, retires after a few,
                            and its lead starts a fresh one in its place

SwarmBoard, one lane per team   post_many (dispatcher) · claim · complete · release (workers)
```

At the defaults (400 issues, 8 teams × 16 workers, 6 specialists, 6 reviewers) about 150 agents
run at once and around 250 are started over the run, since workers retire after four issues and
are replaced.

## What it exercises

| Pattern | Where |
| --- | --- |
| Hierarchy | dispatcher → leads → workers, as subagents (Temporal parent links) |
| Peer turns | workers → specialists and reviewers, which are the dispatcher's children, not theirs |
| A shared work hub | the board: each team's lane is its own workflow, so teams never contend |
| Churn | workers retire and are replaced, so agents start and stop all through a run |
| Failure and retry | a failed consult hands the issue back to the board for another worker |
| Rework | a reviewer sometimes sends an issue back to its worker |
| Human bottleneck | with `simulation.approval_rate > 0`, a worker's won't-fix waits for a person |

Every agent is a swarm member, so one visibility query lists the run, with each member's role,
parent and live phase:

```bash
temporal workflow list --query "SwarmId = '<dispatcher workflow id>'"
temporal workflow list --query "SwarmId = '<id>' AND AgentPhase = 'awaiting_approval'"
```

The dispatcher founds the swarm, named after its own workflow id, through the runner's
`swarm_default=`. Every agent it starts inherits the membership, with its subagent key as its role.

## Run

The dev server needs the swarm search attributes registered, which the root `just temporal` does
(this directory's `just temporal` runs it). Then, in separate terminals from this directory:

```bash
just temporal          # 1. local Temporal dev server, with the swarm search attributes
just session-manager   # 2. session-manager worker
just server            # 3. harness API + console on http://localhost:8000
just worker            # 4. all five agents
```

Or run steps 2–4 together with `just dev`. In the console, start a **Swarm Triage** session and
send `triage`. Every field has a default: lower `issues` and `workers_per_team` for a smaller
swarm, or set `simulation.approval_rate` to `0.02` to watch won't-fix approvals pile up. Each one
blocks its worker until you approve or deny it in that worker's session.

## Files

| File | Role |
| --- | --- |
| `triage_agents.py` | The five agents. |
| `models.py` | Their messages, init data and replies, and the `Simulation` knobs. |
| `issues.py` | The synthetic backlog. |
| `worker.py` | Hosts all five agents on one queue, with room for hundreds of concurrent activities. |
