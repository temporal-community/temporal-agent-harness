<script lang="ts">
  import { onDestroy } from "svelte";
  import type { SwarmMembership, SwarmTopology } from "$lib/api/types";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import SwarmGraph from "./SwarmGraph.svelte";
  import {
    PHASE_LABELS,
    PHASE_TONES,
    needsAttention,
    shortId,
    swarmGraph,
    swarmSummary
  } from "$lib/state/swarmView";

  /**
   * The swarm the session belongs to, live: every member drawn by phase, with counts and the
   * members waiting on a person beside it.
   *
   * It reads visibility, not anyone's event stream, so it costs one request per poll however
   * big the swarm is, and it says how old its picture is (visibility lags by about a second).
   * It polls only while it is on screen and the tab is visible.
   */
  interface Props {
    sessionId: string | null;
    loadMembership: (sessionId: string) => Promise<SwarmMembership>;
    loadTopology: (swarmId: string, options: { board: boolean }) => Promise<SwarmTopology>;
    /** How often to re-read the topology, in ms. */
    pollMs?: number;
  }

  let { sessionId, loadMembership, loadTopology, pollMs = 2000 }: Props = $props();

  let membership = $state<SwarmMembership | null>(null);
  let topology = $state<SwarmTopology | null>(null);
  let error = $state<string | null>(null);
  let showClosed = $state(false);
  let showBoard = $state(true);
  let selectedId = $state<string | null>(null);
  /** 3D unless the browser has no WebGL, which the scene reports and this falls back from. */
  let view = $state<"2d" | "constellation" | "growth">("constellation");
  let webgl = $state(true);
  /* The 3D scene brings three.js, most of the console's weight for a pane most sessions never
     open; importing it here, on first use, lets the bundler put it in a chunk of its own. */
  let scene: Promise<typeof import("./SwarmScene.svelte")> | null = null;
  const loadScene = () => (scene ??= import("./SwarmScene.svelte"));

  let timer: ReturnType<typeof setTimeout> | null = null;
  let generation = 0;

  async function poll(swarmId: string, mine: number): Promise<void> {
    if (mine !== generation) return;
    if (typeof document === "undefined" || !document.hidden) {
      try {
        const next = await loadTopology(swarmId, { board: showBoard });
        if (mine !== generation) return;
        topology = next;
        error = null;
      } catch (e) {
        if (mine !== generation) return;
        error = e instanceof Error ? e.message : String(e);
      }
    }
    timer = setTimeout(() => void poll(swarmId, mine), pollMs);
  }

  $effect(() => {
    const id = sessionId;
    const mine = ++generation;
    membership = null;
    topology = null;
    error = null;
    selectedId = null;
    if (timer) clearTimeout(timer);
    if (!id) return;
    loadMembership(id)
      .then((found) => {
        if (mine !== generation) return;
        membership = found;
        if (found.swarm_id) void poll(found.swarm_id, mine);
      })
      .catch((e) => {
        if (mine === generation) error = e instanceof Error ? e.message : String(e);
      });
  });

  onDestroy(() => {
    generation++;
    if (timer) clearTimeout(timer);
  });

  const graph = $derived(topology ? swarmGraph(topology, { showClosed }) : null);
  const summary = $derived(topology ? swarmSummary(topology) : null);
  const waiting = $derived(topology ? needsAttention(topology) : []);
  const selected = $derived(topology?.nodes.find((n) => n.workflow_id === selectedId) ?? null);
  const age = $derived(topology ? Math.max(0, Date.now() / 1000 - topology.as_of) : 0);

  function time(seconds: number | null): string {
    return seconds == null ? "—" : new Date(seconds * 1000).toLocaleTimeString();
  }
</script>

{#if !sessionId}
  <EmptyState title="No session selected" />
{:else if error && !topology}
  <EmptyState title="Could not read the swarm">{error}</EmptyState>
{:else if membership && !membership.swarm_id}
  <EmptyState title="This session is not part of a swarm">
    An agent joins one through <code>AgentConfig.swarm</code>, or founds one with the runner's
    <code>swarm_default</code>.
  </EmptyState>
{:else if !topology || !graph || !summary}
  <EmptyState title="Reading the swarm…" />
{:else}
  <div class="swarm">
    <header class="hud" aria-label="Swarm summary">
      <span class="figure"><strong>{summary.running}</strong> running</span>
      <span class="figure muted">{summary.closed} closed</span>
      {#each summary.phases as entry (entry.phase)}
        <span class="phase" style={`--tone: var(${PHASE_TONES[entry.phase]})`}>
          <i aria-hidden="true"></i>{entry.count} {PHASE_LABELS[entry.phase]}
        </span>
      {/each}
      {#if summary.board}
        <span class="figure">
          board {summary.board.done} done · {summary.board.claimed} claimed · {summary.board.open} open
        </span>
      {/if}
      <span class="spacer"></span>
      <div class="views" role="radiogroup" aria-label="View">
        {#each [["2d", "2D"], ["constellation", "3D"], ["growth", "Growth"]] as [value, label] (value)}
          <button
            type="button"
            role="radio"
            aria-checked={view === value}
            disabled={value !== "2d" && !webgl}
            title={value === "growth" ? "Height is when each member started" : value === "constellation" ? "Height is depth in the hierarchy" : "Flat force layout"}
            onclick={() => (view = value as typeof view)}>{label}</button
          >
        {/each}
      </div>
      <label><input type="checkbox" bind:checked={showClosed} /> closed</label>
      <label><input type="checkbox" bind:checked={showBoard} /> board</label>
      <span class="figure muted" title="Visibility is eventually consistent">
        {error ? "stale — " + error : `read ${age < 1 ? "just now" : `${Math.round(age)}s ago`}`}
      </span>
    </header>
    {#if topology.truncated}
      <p class="note">Showing the first {topology.nodes.length} members; the swarm has more.</p>
    {/if}
    <div class="body">
      <div class="canvas">
        {#if view === "2d" || !webgl}
          <SwarmGraph {graph} bind:selectedId />
        {:else}
          {#await loadScene()}
            <EmptyState title="Loading the 3D view…" />
          {:then { default: SwarmScene }}
            <SwarmScene
              {graph}
              mode={view}
              bind:selectedId
              onUnsupported={() => {
                webgl = false;
                view = "2d";
              }}
            />
          {:catch failure}
            <EmptyState title="Could not load the 3D view">{failure}</EmptyState>
          {/await}
        {/if}
      </div>
      <aside class="side">
        {#if selected}
          <section>
            <h3>{selected.role ?? "member"}</h3>
            <dl>
              <dt>Phase</dt>
              <dd>{selected.closed ? selected.status.toLowerCase() : (selected.phase ?? "—")}</dd>
              <dt>Type</dt>
              <dd>{selected.workflow_type}</dd>
              <dt>Workflow</dt>
              <dd class="mono">{selected.workflow_id}</dd>
              <dt>Parent</dt>
              <dd class="mono">{selected.parent_id ?? "—"}</dd>
              <dt>Started</dt>
              <dd>{time(selected.start_time)}</dd>
              <dt>Last active</dt>
              <dd>{time(selected.last_active)}</dd>
            </dl>
          </section>
        {/if}
        <section>
          <h3>Needs a person ({waiting.length})</h3>
          {#if waiting.length === 0}
            <p class="muted">Nothing is waiting on an approval or a callback.</p>
          {:else}
            <ul>
              {#each waiting as member (member.workflow_id)}
                <li>
                  <button type="button" onclick={() => (selectedId = member.workflow_id)}>
                    {member.role ?? "member"} · <span class="mono">{shortId(member.workflow_id)}</span>
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
        </section>
        <section>
          <h3>Roles</h3>
          <ul class="roles">
            {#each summary.roles as entry (entry.role)}
              <li><span>{entry.role}</span><span>{entry.count}</span></li>
            {/each}
          </ul>
        </section>
      </aside>
    </div>
  </div>
{/if}

<style>
  .swarm {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
  }

  .hud {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 14px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    color: var(--text-2);
  }

  .hud strong {
    color: var(--text-1);
  }

  .phase {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .phase i {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--tone);
  }

  .spacer {
    flex: 1;
  }

  .views {
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }

  .views button {
    all: unset;
    padding: 2px 9px;
    cursor: pointer;
    color: var(--text-2);
  }

  .views button + button {
    border-left: 1px solid var(--border);
  }

  .views button[aria-checked="true"] {
    background: var(--surface-1);
    color: var(--text-1);
  }

  .views button:disabled {
    cursor: not-allowed;
    opacity: 0.4;
  }

  .views button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }

  .muted {
    color: var(--text-3);
  }

  .note {
    margin: 0;
    padding: 4px 12px;
    font-size: 12px;
    color: var(--warning);
  }

  .body {
    display: flex;
    flex: 1;
    min-height: 0;
  }

  .canvas {
    flex: 1;
    min-width: 0;
  }

  .side {
    width: 240px;
    flex: none;
    overflow-y: auto;
    border-left: 1px solid var(--border);
    padding: 10px 12px;
    font-size: 12px;
  }

  .side section + section {
    margin-top: 14px;
  }

  h3 {
    margin: 0 0 6px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-3);
  }

  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 3px 8px;
    margin: 0;
  }

  dt {
    color: var(--text-3);
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--text-1);
  }

  .mono {
    font-family: var(--font-mono);
    font-size: 11px;
  }

  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  li button {
    all: unset;
    cursor: pointer;
    color: var(--text-1);
  }

  li button:hover,
  li button:focus-visible {
    text-decoration: underline;
  }

  .roles li {
    display: flex;
    justify-content: space-between;
  }

  @container (max-width: 560px) {
    .side {
      display: none;
    }
  }
</style>
