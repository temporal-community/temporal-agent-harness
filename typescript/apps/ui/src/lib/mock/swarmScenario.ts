/**
 * A synthetic swarm for the mock API: a dispatcher, its leads, specialists and board lanes, and
 * workers that retire and are replaced, with phases that move on every read. Deterministic for a
 * given read number, so a test can ask for the same picture twice.
 */

import type { SwarmEdge, SwarmNode, SwarmPhase, SwarmTopology } from "$lib/api/types";
import { seededRandom } from "$lib/state/okfGraph";

const TEAMS = ["auth", "billing", "search", "ui"];
const WORKERS_PER_TEAM = 12;
const SPECIALISTS = 4;
const WORKER_PHASES: SwarmPhase[] = ["working", "working", "working", "awaiting_agent", "tool", "idle"];

export function mockSwarmTopology(
  swarmId: string,
  read: number,
  { board = false }: { board?: boolean } = {}
): SwarmTopology {
  const random = seededRandom(read * 7919 + 1);
  const now = 1_700_000_000 + read * 2;
  const nodes: SwarmNode[] = [];
  const edges: SwarmEdge[] = [];
  const member = (
    workflow_id: string,
    role: string,
    parent_id: string | null,
    phase: SwarmPhase | null,
    closed = false
  ) => {
    nodes.push({
      workflow_id,
      workflow_type: `Mock${role[0].toUpperCase()}${role.slice(1)}`,
      role,
      parent_id,
      phase,
      status: closed ? "COMPLETED" : "RUNNING",
      closed,
      start_time: now - 60,
      close_time: closed ? now - 5 : null,
      last_active: now - Math.floor(random() * 30)
    });
    if (parent_id) edges.push({ source: parent_id, target: workflow_id, kind: "parent", weight: 1 });
  };

  member(swarmId, "dispatcher", null, "awaiting_agent");
  const specialists = Array.from({ length: SPECIALISTS }, (_, i) => `specialist-${i}`);
  for (const id of specialists) member(id, "specialist", swarmId, random() < 0.6 ? "working" : "idle");
  TEAMS.forEach((team, t) => {
    const lead = `lead-${team}`;
    const lane = `swarm-board-${swarmId}-${team}`;
    member(lead, "lead", swarmId, "awaiting_agent");
    member(lane, "board", null, null);
    for (let w = 0; w < WORKERS_PER_TEAM; w++) {
      // Each slot's worker is replaced every few reads; the one before it is kept, closed.
      const generation = Math.floor((read + w + t) / 4);
      const id = `worker-${team}-${w}-${generation}`;
      const approval = (w + t + read) % 37 === 0;
      const phase = approval ? "awaiting_approval" : WORKER_PHASES[Math.floor(random() * WORKER_PHASES.length)];
      member(id, "worker", lead, phase);
      if (generation > 0) member(`worker-${team}-${w}-${generation - 1}`, "worker", lead, "idle", true);
      if (phase === "awaiting_agent") {
        const peer = specialists[Math.floor(random() * specialists.length)];
        edges.push({ source: id, target: peer, kind: "peer", weight: 1 });
      }
      if (board) edges.push({ source: id, target: lane, kind: "board", weight: 1 + (w % 3) });
    }
  });

  const running = nodes.filter((n) => !n.closed);
  const phases: Record<string, number> = {};
  for (const n of running) phases[n.phase ?? "none"] = (phases[n.phase ?? "none"] ?? 0) + 1;
  const roles: Record<string, number> = {};
  for (const n of nodes) roles[n.role ?? "none"] = (roles[n.role ?? "none"] ?? 0) + 1;
  return {
    swarm_id: swarmId,
    as_of: Date.now() / 1000,
    truncated: false,
    nodes,
    edges,
    lanes: board
      ? TEAMS.map((team, t) => ({
          workflow_id: `swarm-board-${swarmId}-${team}`,
          lane: team,
          counts: { open: Math.max(0, 40 - read * 2 - t), claimed: WORKERS_PER_TEAM, done: read * 2 + t }
        }))
      : [],
    phases,
    roles
  };
}
