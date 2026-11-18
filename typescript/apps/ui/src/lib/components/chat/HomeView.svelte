<script lang="ts" module>
  import { BookOpen, LayoutGrid, LayoutList, MessageCircle, Rows3, Rows4, Scale } from "@lucide/svelte";
  import { createAtom } from "@tanstack/svelte-store";
  import type { ColumnSizingState } from "@tanstack/svelte-table";
  export const REPO = "https://github.com/temporal-community/temporal-agent-harness";
  export const RELEASES = `${REPO}/releases`;

  const REGISTRY = { python: "PyPI", typescript: "npm" };
  const DOCS = `${REPO}/blob/main`;

  const GUIDES = `${DOCS}/docs/guides`;
  const CAPABILITIES = `${GUIDES}/capabilities.md`;
  const EXAMPLES = `${GUIDES}/examples.md`;

  /* The console's links to the project, on the home desk's status line and in the Sessions
     sidebar's footer menu. `name` is the full name for the tip and a screen reader where the
     label is short. */
  export const RESOURCES = [
    { label: "Guides", name: "Guides", href: CAPABILITIES, icon: BookOpen },
    { label: "Examples", name: "Examples", href: EXAMPLES, icon: LayoutGrid },
    { label: "Community", name: "Community Slack", href: "https://temporal.io/slack", icon: MessageCircle },
    { label: "MIT", name: "MIT License", href: `${DOCS}/LICENSE`, icon: Scale }
  ];

  /* What the harness does, one card each, linked to the guide section that explains it. Two of
     those headings open with an emoji, whose invisible joiners GitHub keeps in the anchor. The
     docs-drift test checks every section is still there. */
  export const FEATURES = [
    { title: "Durable by default", href: `${GUIDES}/capabilities.md#%EF%B8%8F-durable-execution--the-foundation`, body: "Every agent is a Temporal workflow. A worker can crash or redeploy mid-turn and the agent resumes where it left off, without running a tool call twice." },
    { title: "Approvals that wait", href: `${GUIDES}/capabilities.md#-human-in-the-loop-solved`, body: "Gate any tool call on a person. The turn pauses durably, for seconds or days, and resumes in place when they answer." },
    { title: "Auto mode", href: `${GUIDES}/approvals.md#auto-mode--letting-code-or-a-model-decide`, body: "Let code or a model judge gated calls against rules you can edit mid-session. Anything it isn't sure of still goes to a person." },
    { title: "Code Mode", href: `${GUIDES}/capabilities.md#%E2%80%8D-code-mode--give-the-model-code-not-just-a-call-menu`, body: "Give the model one tool that runs a Python script over your toolset, with every host call still a durable, gated activity." },
    { title: "Typed composition", href: `${GUIDES}/capabilities.md#-composable-by-construction`, body: "Handlers declared with @agent.accepts describe themselves, so one agent can drive another through a typed contract." },
    { title: "One event stream", href: `${GUIDES}/capabilities.md#-standardized-agents-fully-observable`, body: "Every turn, model call, tool call and approval lands on one stream. Watch it live in this console, or replay it afterward." }
  ];

  /* Each SDK's quickstart, as its README has it: "Start the three services" is that README's step
     3, word for word, and the docs-drift test holds the two together. */
  export const SDKS = [
    {
      key: "python",
      label: "Python",
      quickstart: `${DOCS}/python/README.md#quickstart`,
      registry: REGISTRY.python,
      packageName: "temporal-agent-kit",
      packageUrl: "https://pypi.org/project/temporal-agent-kit/",
      prompt:
        "Set up the Temporal Agent Kit for Python in this project: run `uvx temporal-agent-kit@latest init my-agent`, then `uv run temporal-agent-kit skills install`. Pin the version it installs; the project is pre-1.0. Then use the /temporal-agent-kit-python skill: every agent is a Temporal workflow declared with @agent.defn, with @agent.accepts handlers for typed operations and @agent.activity_tool_defn for durable tools.",
      services: [
        { name: "Temporal service", command: "temporal server start-dev" },
        { name: "Session manager, API and console", command: "uv run temporal-agent-kit dev agents.toml" },
        { name: "Your Python agent worker", command: "uv run --env-file .env.local python -m my_agent.worker" }
      ]
    },
    {
      key: "typescript",
      label: "TypeScript",
      quickstart: `${DOCS}/typescript/packages/core/README.md#quickstart`,
      registry: REGISTRY.typescript,
      packageName: "@temporalio/agent-kit",
      packageUrl: "https://www.npmjs.com/package/@temporalio/agent-kit",
      prompt:
        "Set up the Temporal Agent Kit for TypeScript in this project: run `npx -y -p @temporalio/agent-kit-worker@latest agent-kit init my-agent`, then `npm run agent-kit:skill`. Pin the version it installs; the project is pre-1.0. Then use the /temporal-agent-kit-typescript skill: every agent is a Temporal workflow built with defineAgent and typed handler() operations, run on a worker with AgentKitPlugin.",
      services: [
        { name: "Temporal service", command: "temporal server start-dev" },
        { name: "Session manager, API and console", command: "npm run agent-kit:dev" },
        { name: "Your TypeScript agent worker", command: "npm start" }
      ]
    }
  ];

  /* How many agents have no worker to run them: the bay's status, shown in its head. */
  export function agentsWithoutWorker(agents: { worker?: { status?: string } | null }[]): number {
    return agents.filter((agent) => agent.worker?.status === "no_worker").length;
  }

  /* How much each bay row shows, chosen from the bay's pane header and remembered.
     Compact is one line; comfortable puts a line of description under the name; detailed wraps
     it to two. */
  export const BAY_DENSITIES = [
    { key: "compact", label: "Compact rows", icon: Rows4 },
    { key: "comfortable", label: "Comfortable rows", icon: Rows3 },
    { key: "detailed", label: "Detailed rows", icon: LayoutList }
  ];
  const DENSITY_KEY = "temporal-agent-ui.bay-density";
  export const bayDensity = $state({ key: (typeof localStorage === "undefined" ? null : localStorage.getItem(DENSITY_KEY)) ?? "comfortable" });
  export function bayDensityAt(): number {
    return Math.max(0, BAY_DENSITIES.findIndex((item) => item.key === bayDensity.key));
  }
  /* The bay's columns in the order and at the widths a reader left them, kept across
     reloads. Table slices the app owns, as the TanStack Store atoms the table writes through. */
  const COLUMNS_KEY = "temporal-agent-ui.bay-columns";
  const keptColumns = (() => {
    try {
      const kept = JSON.parse((typeof localStorage === "undefined" ? null : localStorage.getItem(COLUMNS_KEY)) ?? "null") as { order?: unknown; sizes?: unknown } | null;
      const order = Array.isArray(kept?.order) ? kept.order.filter((item): item is string => typeof item === "string") : [];
      const sizes = Object.fromEntries(
        Object.entries(kept?.sizes && typeof kept.sizes === "object" ? kept.sizes : {}).filter((entry): entry is [string, number] => Number.isFinite(entry[1]))
      );
      return { order, sizes };
    } catch {
      return { order: [], sizes: {} };
    }
  })();
  const bayColumnOrder = createAtom<string[]>(keptColumns.order);
  const bayColumnSizing = createAtom<ColumnSizingState>(keptColumns.sizes);
  if (typeof localStorage !== "undefined") {
    const keep = () => localStorage.setItem(COLUMNS_KEY, JSON.stringify({ order: bayColumnOrder.get(), sizes: bayColumnSizing.get() }));
    bayColumnOrder.subscribe(keep);
    bayColumnSizing.subscribe(keep);
  }

  export function cycleBayDensity(): void {
    bayDensity.key = BAY_DENSITIES[(bayDensityAt() + 1) % BAY_DENSITIES.length].key;
    localStorage.setItem(DENSITY_KEY, bayDensity.key);
  }
</script>

<script lang="ts">
  /**
   * The body of one of the home desk's panes, the desk with no session open, or its status
   * line of links. PaneShell draws the frame and the head; this draws what is in it. The bay
   * and the counts are the server's; features and setup are fixed docs content, and releases
   * are a log kept in this file. Each pane's status rides in its own head, not the status line.
   */
  import { ArrowUpRight, Check, Copy, Package, PanelBottom, Play, ScrollText } from "@lucide/svelte";
  import type { AgentDescriptor, JsonRecord, Session } from "$lib/api/types";
  import StartSessionDialog, { agentEntries } from "$lib/components/chat/StartSessionDialog.svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import Copyable, { copyText } from "$lib/components/primitives/Copyable.svelte";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import DataTable, { type DataColumn } from "$lib/components/primitives/DataTable.svelte";
  import StatusChip from "$lib/components/primitives/StatusChip.svelte";
  import { sessionAge } from "$lib/state/sessionHistory";

  type HomePart = "bay" | "setup" | "releases" | "capabilities" | "status";

  interface Props {
    part: HomePart;
    agents: AgentDescriptor[];
    sessions: Session[];
    starting?: boolean;
    refreshingAgents?: boolean;
    agentsError?: string | null;
    onStart: (workflowType: string, data?: JsonRecord) => void | Promise<void>;
    onRefreshAgents?: () => void | Promise<void>;
    /** The status line stands in the transport's row, so it carries the drawer's switch too. */
    drawerOpen?: boolean;
    onToggleDrawer?: () => void;
  }

  let {
    part,
    agents,
    sessions,
    starting = false,
    refreshingAgents = false,
    agentsError = null,
    onStart,
    onRefreshAgents,
    drawerOpen = false,
    onToggleDrawer
  }: Props = $props();

  const id = $props.id();

  let sdkKey = $state("python");
  const sdk = $derived(SDKS.find((item) => item.key === sdkKey) ?? SDKS[0]);

  let copied = $state(false);
  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  async function copyPrompt(): Promise<void> {
    try {
      await navigator.clipboard.writeText(sdk.prompt);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => (copied = false), 1800);
    } catch {
      /* No clipboard: the prompt is on screen to select. */
    }
  }

  /* Each agent with its running count on the row, so a sort by Running follows the count. */
  type BayRow = ReturnType<typeof agentEntries>[number] & { live: number };
  const bayRows = $derived(agentEntries(agents).map((entry) => ({ ...entry, live: runningByType.get(entry.agent.workflow_type) ?? 0 })));
  const WORKER_LABELS: Record<string, string> = { no_worker: "No worker", unknown: "Unknown" };
  const workerLabel = (row: BayRow) => WORKER_LABELS[row.agent.worker?.status ?? ""] ?? "Ready";
  /* Status first, then the name and its task queue, then the figures, each sized to fit its
     header. Task queue, the least read, drops first in a narrow pane. */
  const BAY_COLUMNS: DataColumn<BayRow>[] = $derived([
    { id: "agent", header: "Agent", value: (row) => row.name, cell: agentCell, rowHeader: true },
    { id: "worker", header: "Worker", value: workerLabel, cell: workerCell, filter: true, size: 104, minSize: 88, tip: "Ready: a worker is polling this agent's task queue, so a session starts at once.\nNo worker: none is, so a new session waits for one." },
    { id: "running", header: "Running", value: (row) => row.live, cell: runningCell, align: "end", size: 64, tip: "This agent's sessions running now" },
    { id: "queue", header: "Task queue", value: (row) => row.agent.task_queue, cell: queueCell, class: "c-queue", size: 160, minSize: 96, hideBelow: 560, tip: "The Temporal task queue this agent's worker polls" },
    { id: "seen", header: "Last seen", value: (row) => row.agent.worker?.last_seen ?? -1, cell: seenCell, align: "end", class: "c-seen", size: 80, tip: "When a worker last polled this agent's task queue" }
  ]);
  const bayActions = (row: BayRow) => [
    { label: "Start session", icon: Play, run: () => (picked = row.agent.workflow_type) },
    { label: "Copy task queue", icon: Copy, run: () => void copyText(row.agent.task_queue) },
    { label: "Copy workflow type", icon: Copy, run: () => void copyText(row.agent.workflow_type) }
  ];
  const noWorker = $derived(agentsWithoutWorker(agents));
  const running = $derived(sessions.filter((session) => !session.closed).length);

  /* What the server says about an agent's worker, for the tip on its status. */
  function workerTip(agent: AgentDescriptor): string | undefined {
    const worker = agent.worker;
    if (!worker) return undefined;
    if (worker.status === "unknown") return worker.error ?? "The server couldn't check this task queue";
    if (worker.poller_count === 0) return `No worker is polling ${agent.task_queue}`;
    const seen = worker.last_seen == null ? "" : ` · last seen ${sessionAge(worker.last_seen, Date.now()).long}`;
    return `${worker.poller_count} ${worker.poller_count === 1 ? "worker" : "workers"} polling${seen}`;
  }

  const runningByType = $derived(
    sessions.reduce((counts, session) => (session.closed ? counts : counts.set(session.agent_workflow_type, (counts.get(session.agent_workflow_type) ?? 0) + 1)), new Map<string, number>())
  );
  /* With nothing listed and a failed request, the console doesn't know whether there is
     anything to list, so it says it couldn't ask rather than that there is nothing. */
  const unreachable = $derived(Boolean(agentsError) && agents.length === 0 && !refreshingAgents);

  /* The agent a row was clicked for, or "" for the whole picker: New session's own dialog. */
  let picked = $state<string | null>(null);

</script>

{#if part === "capabilities"}
  <div class="stack home-pane">
    <ul class="scroll caps">
      {#each FEATURES as feature (feature.title)}
        <li><a class="cap" href={feature.href} target="_blank" rel="noopener noreferrer"><h3>{feature.title} <ArrowUpRight class="cap-go" size={12} aria-hidden="true" /></h3><p>{feature.body}</p></a></li>
      {/each}
    </ul>
  </div>
{:else if part === "bay"}
  <div class="stack home-pane">
    {#if agentsError && !unreachable}<p class="error" role="alert">{agentsError}</p>{/if}
    {#if unreachable}
      <EmptyState title="Couldn't reach the server." >
        <p class="error-detail">{agentsError}</p>
        {#if onRefreshAgents}<Chip fill="bare" onclick={() => void onRefreshAgents?.()}>Retry</Chip>{/if}
      </EmptyState>
    {:else if bayRows.length === 0}
      <EmptyState title={refreshingAgents ? "Loading agents…" : "No agents are registered yet."} >
        {#if !refreshingAgents}<p>Start a worker that registers an agent with this server; Setup shows how.</p>{/if}
        {#if onRefreshAgents}
          <Chip fill="bare" disabled={refreshingAgents} onclick={() => void onRefreshAgents?.()}>{refreshingAgents ? "Checking…" : "Recheck"}</Chip>
        {/if}
      </EmptyState>
    {:else}
      <DataTable
        class={`bay-cols density-${bayDensity.key}`}
        label="Agents"
        rows={bayRows}
        columns={BAY_COLUMNS}
        rowKey={(row) => row.agent.key}
        rowProps={(row) => ({ "aria-haspopup": "dialog", title: row.summary || undefined })}
        onRowClick={(row) => (picked = row.agent.workflow_type)}
        rowActions={bayActions}
        columnOrder={bayColumnOrder}
        columnSizing={bayColumnSizing}
      />
    {/if}
  </div>
{:else if part === "setup"}
  <div class="stack home-pane">
    <div class="scroll setup">
      <div class="setup-group">
        <div class="setup-bar">
          {@render sdkToggle(sdk.key, (key) => (sdkKey = key))}
        </div>
        <!-- The copy button sits in the block it copies, as the console's code blocks keep theirs. -->
        <figure class="prompt-box">
          <figcaption class="prompt-head">
            <span class="kicker" id={`${id}-prompt`}>Agent prompt</span>
            <Chip fill="solid" tone="accent" size="xs" onclick={copyPrompt} aria-describedby={`${id}-prompt`}>
              {#snippet lead()}{#if copied}<Check size={12} aria-hidden="true" />{:else}<Copy size={12} aria-hidden="true" />{/if}{/snippet}
              {copied ? "Copied" : "Copy"}
            </Chip>
          </figcaption>
          <p class="prompt">{sdk.prompt}</p>
        </figure>
      </div>
      <div class="setup-group">
      <section class="run" aria-labelledby="home-run">
        <div class="run-head">
          <h3 class="kicker" id="home-run">Start the three services</h3>
          <Chip fill="bare" tone="accent" toned href={sdk.quickstart} aria-label={`${sdk.label} quickstart`}>Quickstart <ArrowUpRight size={12} aria-hidden="true" /></Chip>
        </div>
        <p class="run-note">Each in its own terminal, from your project directory.</p>
        <ol class="run-steps">
          {#each sdk.services as service, i (service.command)}
            <li>
              <span class="kicker">{i + 1}</span>
              <span class="service">
                <span class="service-name">{service.name}</span>
                <Copyable value={service.command} label={`Copy: ${service.name}`}><code>{service.command}</code></Copyable>
              </span>
            </li>
          {/each}
        </ol>
      </section>
      </div>
    </div>
  </div>
{:else if part === "releases"}
  <!-- Where each package's versions are published, rather than a list of them typed in here:
       the registries and GitHub always have the current one. -->
  <div class="stack home-pane">
    <div class="thead link-cols" aria-hidden="true"><span>Go to</span><span></span></div>
    <ol class="scroll table link-cols">
      {#each SDKS as item (item.key)}
        <li><a class="trow" href={item.packageUrl} target="_blank" rel="noopener noreferrer"><span class="c-main fade-end"><Package class="row-icon" size={14} aria-hidden="true" /><span class="t-name">{item.label}</span> <span class="t-desc">{item.packageName} on {item.registry}</span></span><ArrowUpRight size={12} aria-hidden="true" /></a></li>
      {/each}
      <li><a class="trow" href={RELEASES} target="_blank" rel="noopener noreferrer"><span class="c-main fade-end"><ScrollText class="row-icon" size={14} aria-hidden="true" /><span class="t-name">Release notes</span> <span class="t-desc">Every release of both SDKs, on GitHub</span></span><ArrowUpRight size={12} aria-hidden="true" /></a></li>
    </ol>
  </div>
{:else}
  <footer class="home-status" aria-label="Status">
    <nav class="resources" aria-label="Resources">
      {#each RESOURCES as link (link.href)}
        {@const Icon = link.icon}
        <Chip size="xs" fill="bare" href={link.href} aria-label={link.name} title={link.name}>
          {#snippet lead()}<Icon size={12} aria-hidden="true" />{/snippet}
          <span class="status-label">{link.label}</span>
        </Chip>
      {/each}
    </nav>
    {#if onToggleDrawer}
      <IconButton
        label={drawerOpen ? "Close the bottom drawer" : "Open the bottom drawer"}
        tip={drawerOpen ? "Close the bottom drawer\nD" : "Open the bottom drawer\nD"}
        pressed={drawerOpen}
        onclick={onToggleDrawer}
      >
        <PanelBottom size={14} />
      </IconButton>
    {/if}
  </footer>
{/if}

<!-- The bay's cells; DataTable draws the rows and the header around them. -->
{#snippet agentCell(row: BayRow)}
  <span class="c-main fade-end" dir="auto"><span class="t-head"><span class="t-name">{row.name}</span>{#if row.agent.init_data?.required}{" "}<span class="kicker t-needs">Needs data</span>{/if}</span> <span class="t-desc">{row.summary || row.agent.workflow_type}</span></span>
{/snippet}

{#snippet runningCell(row: BayRow)}
  <span class={["count", row.live > 0 && "live"]}>{row.live.toLocaleString()}</span>
{/snippet}

{#snippet workerCell(row: BayRow)}
  {@const status = row.agent.worker?.status}
  <span class="c-worker" data-tip={workerTip(row.agent)}>
    <StatusChip plain label={workerLabel(row)} kind={status === "no_worker" ? "stuck" : status === "unknown" ? "idle" : "available"} />
  </span>
{/snippet}

{#snippet seenCell(row: BayRow)}
  {@const seen = row.agent.worker?.last_seen}
  {#if seen == null}
    <span class="count">—</span>
  {:else}
    {@const age = sessionAge(seen, Date.now())}
    <span class="count" title={age.long}>{age.short}</span>
  {/if}
{/snippet}

{#snippet queueCell(row: BayRow)}
  <span class="c-queue-text kicker fade-end" dir="auto">{row.agent.task_queue}</span>
{/snippet}

<!-- Setup's SDK switch. -->
{#snippet sdkToggle(chosen: string, choose: (key: string) => void)}
  <div class="row-of sdk-switch" role="group" aria-label="SDK">
    {#each SDKS as item (item.key)}
      <Chip size="xs" fill="bare" active={item.key === chosen} aria-pressed={item.key === chosen} onclick={() => choose(item.key)}>{item.label}</Chip>
    {/each}
  </div>
{/snippet}

{#if picked !== null}
  <StartSessionDialog
    {agents}
    preferredAgentType={picked || undefined}
    {starting}
    refreshing={refreshingAgents}
    error={agentsError}
    onRefresh={onRefreshAgents}
    {onStart}
    onClose={() => (picked = null)}
  />
{/if}

<style>
  /* Each body fills its pane at height: 100%, as the desk's do, and scrolls inside it. No
     pinned footers: stacked panes would lose a row of height to each. */
  .stack,
  .scroll {
    flex: 1 1 0;
    display: flex;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
  }

  .stack {
    height: 100%;
  }

  .scroll {
    overflow-y: auto;
    scrollbar-gutter: stable;
  }

  /* One SDK is always chosen: it is outlined, as the header's toggles are when on, and the other
     is bare text, so the pair reads as a choice rather than two filters. */
  .sdk-switch :global(.chip.bare.active) {
    border-color: var(--border-strong);
    background: transparent;
    color: var(--text-1);
  }

  .row-of {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  .error {
    margin: 0;
    padding: var(--gap-sm) var(--pane-inset);
    color: var(--error);
    font-size: var(--font-sm);
    overflow-wrap: anywhere;
  }

  /* Raised a pixel so the first row's rules land on the pane head's bottom border rather
     than drawing a second line just under it. */
  /* One, two, three or six columns: counts that divide the six cards, so every row is full
     and its rules run the pane's width. */
  .caps {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-content: start;
    gap: 0 var(--gutter);
    margin: -1px 0 0;
    padding: 0 calc(var(--gutter) - var(--scrollbar-size)) 0 var(--gutter);
    list-style: none;
  }

  .cap {
    display: grid;
    align-content: start;
    gap: var(--gap-xs);
    padding: var(--gutter) 0;
    border-top: 1px solid var(--border);
    color: inherit;
    text-decoration: none;
  }

  .cap:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* The way to the guide, shown where a pointer can hover only once it does. */
  .cap :global(.cap-go) {
    color: var(--accent);
    vertical-align: -1px;
  }

  @media (hover: hover) and (pointer: fine) {
    .cap :global(.cap-go) {
      opacity: 0;
    }

    .cap:is(:hover, :focus-visible) :global(.cap-go) {
      opacity: 1;
    }

    .cap:hover h3 {
      color: var(--accent);
    }
  }

  .cap h3 {
    margin: 0;
    color: var(--text-1);
    font-size: var(--font-lg);
    font-weight: 500;
  }

  .cap p {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-md);
    line-height: 1.5;
  }

  /* ---------- Bay: a DataTable ----------
     One line a row under a header, read top to bottom like the Temporal Web UI's workflow list. */
  .error-detail {
    color: var(--error);
    overflow-wrap: anywhere;
  }

  .c-main,
  .c-queue-text {
    display: block;
  }

  .c-main {
    color: var(--text-1);
    unicode-bidi: plaintext;
  }

  .t-name {
    font-weight: 500;
  }

  .t-desc {
    color: var(--text-3);
  }

  .count {
    color: var(--text-4);
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }

  .count.live {
    color: var(--accent-soft);
  }

  .c-worker :global(.chip) {
    height: auto;
  }

  .t-needs {
    color: var(--text-2);
  }

  /* ---------- Row density ---------- */
  /* Comfortable and detailed put the description on its own line under the name; the other
     cells keep to the name's line, the first baseline. Comfortable's one line still fades. */
  .home-pane :global(:is(.density-comfortable, .density-detailed) .trow > *) {
    padding-block: var(--gap-sm);
  }

  .home-pane :global(:is(.density-comfortable, .density-detailed) .c-main) {
    display: grid;
    gap: var(--gap-2xs);
  }

  .home-pane :global(.density-detailed .c-main) {
    white-space: normal;
    animation: none;
    -webkit-mask-image: none;
    mask-image: none;
  }

  .home-pane :global(.density-detailed .t-desc) {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
    line-height: 1.45;
  }

  /* ---------- Releases' rows of links ---------- */
  .link-cols {
    --cols: minmax(0, 1fr) 1rem;
  }

  .thead.link-cols,
  .link-cols .trow {
    display: grid;
    grid-template-columns: var(--cols);
    gap: var(--gutter);
    align-items: center;
  }

  .thead.link-cols {
    flex: none;
    padding: var(--gap-sm) var(--pane-inset);
    border-bottom: 1px solid var(--border);
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--label-size);
    font-weight: var(--label-weight);
    letter-spacing: var(--label-tracking);
    text-transform: uppercase;
  }

  .table.link-cols {
    display: block;
    margin: 0;
    padding: 0 calc(var(--pane-inset) - var(--scrollbar-size)) 0 var(--pane-inset);
    list-style: none;
  }

  .link-cols .trow {
    width: 100%;
    min-height: var(--control-height);
    padding: var(--gap-xs) 0;
    border: 0;
    border-bottom: 1px solid var(--border);
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: var(--font-md);
    text-align: start;
    text-decoration: none;
    cursor: pointer;
  }

  .link-cols .c-main {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    white-space: nowrap;
  }

  .link-cols :global(.row-icon) {
    flex: none;
    color: var(--text-3);
  }

  /* ---------- Setup ---------- */
  .setup {
    gap: calc(2 * var(--gutter));
    padding: var(--pane-inset);
    padding-inline-end: calc(var(--pane-inset) - var(--scrollbar-size));
  }

  /* The SDK switch and its links share one line; the links wrap under it when narrow. */
  .setup-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
  }

  /* Quiet, as EmptyState's quick start is: it is text to hand an agent, not code to read, so
     it skips the violet code-block surface. Its caption and copy button sit in a head strip
     inside the border. */
  .prompt-box {
    flex: none;
    margin: 0;
    border: 1px solid var(--border);
    background: var(--surface-2);
  }

  .prompt-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
    padding: var(--gap-sm) var(--gap-sm) 0 var(--gap-lg);
  }

  .prompt-head .kicker {
    color: var(--text-4);
  }

  .prompt {
    max-height: 26vh;
    margin: 0;
    padding: var(--gap-md) var(--gap-lg) var(--gap-lg);
    overflow: auto;
    color: var(--text-2);
    font-size: var(--font-md);
    line-height: 1.55;
    overflow-wrap: anywhere;
  }

  .run {
    display: grid;
    gap: var(--gap-lg);
  }

  .run h3 {
    margin: 0;
  }

  .run-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
  }

  .run-steps {
    display: grid;
    gap: var(--gap-lg);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .run-note {
    margin: calc(-1 * var(--gap-md)) 0 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .run-steps li {
    display: flex;
    gap: var(--gap-md);
    align-items: baseline;
    min-width: 0;
  }

  .service {
    display: grid;
    gap: var(--gap-xs);
    min-width: 0;
  }

  .service-name {
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .run-steps code {
    color: var(--text-1);
    font-family: var(--font-mono);
    font-size: var(--font-code);
    overflow-wrap: anywhere;
  }

  /* ---------- Status line: in the transport's row, drawn as the transport is ---------- */
  /* A status line, not a transport: it holds a line of small text and links, so it is
     slimmer than the session's transport row it stands in for. */
  .home-status {
    position: relative;
    z-index: 5;
    display: flex;
    align-items: center;
    gap: var(--gap-md);
    min-width: 0;
    padding: var(--gap-xs) var(--gutter);
    border-top: 1px solid var(--border);
    background: color-mix(in srgb, var(--surface-1) 92%, var(--surface-0));
  }

  /* On a narrow window the links scroll sideways rather than pushing the page wider. */
  .resources {
    flex: 1 1 auto;
    display: flex;
    justify-content: safe flex-end;
    gap: var(--gap-md);
    min-width: 0;
    overflow-x: auto;
  }

  /* ---------- Responsive to the pane, not the window ----------
     A home pane is a desk pane: it can be a tall narrow column in the rail, a short wide
     strip in the drawer, folded, or dragged anywhere between. Each one reads its own box,
     both ways, the way the desk's panes read theirs, at the desk's own breakpoints. */
  .home-pane {
    container: home-pane / size;
  }

  .setup-group {
    display: flex;
    flex-direction: column;
    gap: var(--gutter);
    min-width: 0;
  }

  @container home-pane (width >= 520px) {
    .caps {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @container home-pane (width >= 780px) {
    .caps {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
  }

  @container home-pane (width >= 1560px) {
    .caps {
      grid-template-columns: repeat(6, minmax(0, 1fr));
    }
  }

  /* The status line: icons alone for the links when the row is narrow; each keeps its name
     for a screen reader and in the tip. */
  .home-status {
    container: home-status / inline-size;
  }

  @container home-status (width < 640px) {
    .resources .status-label {
      display: none;
    }
  }
</style>
