<script lang="ts">
  import {
    Braces,
    ChevronDown,
    History,
    Library,
    RefreshCw,
    Search,
    X
  } from "@lucide/svelte";
  import { SubagentCloseDecisionRequiredError } from "$lib/api/httpClient";
  import type {
    AccountOverview as AccountOverviewData,
    AccountResource,
    CatalogResource,
    Session,
    SubagentCloseResolution,
    SubagentInfo,
    ToolCallRecord
  } from "$lib/api/types";
  import ToolCallHistory from "$lib/components/account/ToolCallHistory.svelte";
  import AgentGlyph from "$lib/components/primitives/AgentGlyph.svelte";
  import StatusChip, {
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";

  interface Props {
    account: AccountOverviewData;
    sessions?: Session[];
    sessionId?: string;
    activeAgentId?: string | null;
    mounting?: boolean;
    refreshingSessions?: boolean;
    onMountAgent: (agentId: string) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
    onRefreshSessions?: () => void | Promise<void>;
    onCloseSession?: (
      sessionId: string,
      resolution?: SubagentCloseResolution
    ) => void | Promise<void>;
    onLoadToolCalls?: (serverName: string) => Promise<ToolCallRecord[]>;
    onLoadCatalog?: () => Promise<CatalogResource[]>;
    onInstallCatalogResource?: (resourceId: string) => Promise<CatalogResource[]>;
    onRemoveCatalogResource?: (resourceId: string) => Promise<CatalogResource[]>;
  }

  let {
    account,
    sessions = [],
    sessionId = "",
    activeAgentId = null,
    mounting = false,
    refreshingSessions = false,
    onMountAgent,
    onSelectSession,
    onRefreshSessions,
    onCloseSession,
    onLoadToolCalls,
    onLoadCatalog,
    onInstallCatalogResource,
    onRemoveCatalogResource
  }: Props = $props();

  let sessionScope = $state<string | null>(null);
  let sessionSearch = $state("");
  let closingSessionIds = $state<string[]>([]);
  let mountingSessionId = $state<string | null>(null);
  let closeDecision = $state<{
    sessionId: string;
    agentLabel: string;
    subagents: SubagentInfo[];
  } | null>(null);
  let sessionPopoverLeft = $state(16);
  let sessionPopoverTop = $state(0);
  let toolCallServerName = $state<string | null>(null);
  let toolCalls = $state<ToolCallRecord[]>([]);
  let loadingToolCalls = $state(false);
  let toolCallError = $state<string | null>(null);
  let toolHistoryLeft = $state(16);
  let toolHistoryTop = $state(16);
  let toolCallRequestVersion = 0;
  let catalogOpen = $state(false);
  let catalogResources = $state<CatalogResource[]>([]);
  let catalogLoading = $state(false);
  let catalogError = $state<string | null>(null);
  let catalogMutation = $state<string | null>(null);
  let catalogLeft = $state(16);
  let catalogTop = $state(16);

  const sortedSessions = $derived([...sessions].sort((a, b) => b.created_at - a.created_at));
  const sortedAgents = $derived(
    [...account.agents].sort(
      (a, b) => Number(a.kind !== "harness_nexus") - Number(b.kind !== "harness_nexus")
    )
  );
  const sortedMcpServers = $derived(
    [...account.mcp_servers].sort(
      (a, b) => Number(a.kind !== "nexus") - Number(b.kind !== "nexus")
    )
  );
  const sortedCatalogResources = $derived(
    [...catalogResources].sort(
      (a, b) =>
        a.category.localeCompare(b.category) ||
        Number(a.transport !== "nexus") - Number(b.transport !== "nexus") ||
        a.label.localeCompare(b.label)
    )
  );
  const scopedSessions = $derived(
    sessionScope === "account" || sessionScope == null
      ? sortedSessions
      : sortedSessions.filter((session) => session.agent_workflow_type === sessionScope)
  );
  const searchTerm = $derived(sessionSearch.trim().toLowerCase());
  const visibleSessions = $derived(
    searchTerm
      ? scopedSessions.filter((session) => sessionMatchesSearch(session, searchTerm))
      : scopedSessions
  );
  const scopedAgent = $derived(
    account.agents.find((agent) => agent.agent_id === sessionScope) ?? null
  );
  const sessionBrowserTitle = $derived(
    sessionScope === "account" || sessionScope == null
      ? "Account sessions"
      : `${scopedAgent?.label ?? sessionScope} sessions`
  );
  const toolCallServer = $derived(
    account.mcp_servers.find((server) => server.name === toolCallServerName) ?? null
  );

  function kindLabel(kind: string): string {
    return kind === "harness_nexus" ? "Harness · Nexus" : "External · HTTP";
  }

  function toggleSessions(scope: string, event: MouseEvent): void {
    if (sessionScope === scope) {
      sessionScope = null;
      return;
    }
    const trigger = event.currentTarget as HTMLElement;
    const triggerRect = trigger.getBoundingClientRect();
    sessionPopoverLeft = triggerRect.right + 10;
    sessionPopoverTop = Math.max(
      16,
      Math.min(triggerRect.top, window.innerHeight - 576)
    );
    sessionScope = scope;
    catalogOpen = false;
    toolCallRequestVersion += 1;
    toolCallServerName = null;
    sessionSearch = "";
    closeDecision = null;
  }

  async function loadCatalog(): Promise<void> {
    if (!onLoadCatalog || catalogLoading) return;
    catalogLoading = true;
    catalogError = null;
    try {
      catalogResources = await onLoadCatalog();
    } catch (error) {
      catalogError = error instanceof Error ? error.message : "Failed to load catalog.";
    } finally {
      catalogLoading = false;
    }
  }

  function toggleCatalog(event: MouseEvent): void {
    if (catalogOpen) {
      catalogOpen = false;
      return;
    }
    const triggerRect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    catalogLeft = triggerRect.right + 10;
    catalogTop = Math.max(16, Math.min(triggerRect.top, window.innerHeight - 620));
    sessionScope = null;
    toolCallServerName = null;
    catalogOpen = true;
    void loadCatalog();
  }

  async function mutateCatalog(resource: CatalogResource): Promise<void> {
    if (catalogMutation) return;
    const operation = resource.installed
      ? onRemoveCatalogResource
      : onInstallCatalogResource;
    if (!operation) return;
    catalogMutation = resource.resource_id;
    catalogError = null;
    try {
      catalogResources = await operation(resource.resource_id);
    } catch (error) {
      catalogError = error instanceof Error ? error.message : "Catalog update failed.";
    } finally {
      catalogMutation = null;
    }
  }

  function catalogKind(resource: CatalogResource): string {
    const transport = resource.transport === "nexus" ? "Nexus" : "External";
    return `${transport} · ${resource.category === "agent" ? "Agent" : "MCP"}`;
  }

  async function loadToolCalls(serverName: string): Promise<void> {
    if (!onLoadToolCalls) return;
    const requestVersion = ++toolCallRequestVersion;
    loadingToolCalls = true;
    toolCallError = null;
    try {
      const loaded = await onLoadToolCalls(serverName);
      if (requestVersion === toolCallRequestVersion) toolCalls = loaded;
    } catch (error) {
      if (requestVersion === toolCallRequestVersion) {
        toolCallError =
          error instanceof Error ? error.message : "Failed to read retained tool calls.";
      }
    } finally {
      if (requestVersion === toolCallRequestVersion) loadingToolCalls = false;
    }
  }

  function toggleToolCalls(server: AccountResource, event: MouseEvent): void {
    if (toolCallServerName === server.name) {
      toolCallServerName = null;
      toolCallRequestVersion += 1;
      return;
    }
    const triggerRect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    toolHistoryLeft = triggerRect.right + 10;
    toolHistoryTop = Math.max(
      16,
      Math.min(triggerRect.top, window.innerHeight - 736)
    );
    sessionScope = null;
    toolCallServerName = server.name;
    toolCalls = [];
    void loadToolCalls(server.name);
  }

  function sessionCreatedAt(value: number): string {
    if (!value) return "Unknown time";
    return new Date(value * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function sessionInitialMessage(session: Session): string {
    return (
      session.initial_user_message?.trim() ||
      (session.is_spawned ? session.label : "No user message yet")
    );
  }

  function sessionAgentLabel(session: Session): string {
    return (
      account.agents.find((agent) => agent.agent_id === session.agent_workflow_type)
        ?.label ?? session.agent_workflow_type
    );
  }

  function sessionMatchesSearch(session: Session, term: string): boolean {
    return [
      sessionInitialMessage(session),
      sessionAgentLabel(session),
      session.workflow_id,
      session.agent_workflow_type
    ].some((value) => value.toLowerCase().includes(term));
  }

  function sessionStatusKind(session: Session): StatusKind {
    if (session.closed) return "closed";
    if (session.workflow_id === sessionId) return "available";
    return session.is_message_queuing_enabled ? "queued" : "idle";
  }

  function sessionStatusLabel(session: Session): string {
    if (session.closed) return "Closed";
    if (session.workflow_id === sessionId) return "Active";
    return session.is_message_queuing_enabled ? "Queue on" : "Idle";
  }

  async function openSession(nextSessionId: string): Promise<void> {
    if (mountingSessionId) return;
    if (nextSessionId === sessionId) {
      sessionScope = null;
      return;
    }
    mountingSessionId = nextSessionId;
    try {
      await onSelectSession?.(nextSessionId);
      sessionScope = null;
    } finally {
      mountingSessionId = null;
    }
  }

  async function refreshSessions(): Promise<void> {
    if (!onRefreshSessions || refreshingSessions) return;
    await onRefreshSessions();
  }

  async function closeSession(
    nextSessionId: string,
    resolution?: SubagentCloseResolution
  ): Promise<void> {
    if (
      !onCloseSession ||
      closingSessionIds.includes(nextSessionId) ||
      sessions.some((session) => session.workflow_id === nextSessionId && session.closed)
    ) {
      return;
    }
    closingSessionIds = [...closingSessionIds, nextSessionId];
    try {
      await onCloseSession(nextSessionId, resolution);
      closeDecision = null;
    } catch (error) {
      if (error instanceof SubagentCloseDecisionRequiredError) {
        const session = sessions.find(
          (item) => item.workflow_id === error.sessionId
        );
        closeDecision = {
          sessionId: error.sessionId,
          agentLabel: session ? sessionAgentLabel(session) : "Agent",
          subagents: error.subagents
        };
      }
      // Other failures are exposed by the controller in the shared status chip.
    } finally {
      closingSessionIds = closingSessionIds.filter((id) => id !== nextSessionId);
    }
  }
</script>

<section
  class="account-pane"
  aria-label={`Account ${account.account_id} resources`}
>
  <button
    type="button"
    class:active={catalogOpen}
    class="catalog-trigger"
    aria-expanded={catalogOpen}
    onclick={toggleCatalog}
  >
    <span class="catalog-trigger-label">
      <Library size={13} />
      <span>Catalog</span>
    </span>
    <ChevronDown size={12} class={catalogOpen ? "rotated" : ""} />
  </button>

  <div class="account-identity">
    <span class="eyebrow">Account</span>
    <strong>{account.account_id}</strong>
    <span class="summary">
      {account.agents.length} agents · {account.mcp_servers.length} MCP servers
    </span>
    <span class="summary">
      {account.active_session_count}/{account.session_count} active sessions
    </span>
    <button
      type="button"
      class:active={sessionScope === "account"}
      class="sessions-trigger"
      aria-expanded={sessionScope === "account"}
      onclick={(event) => toggleSessions("account", event)}
    >
      <History size={11} />
      <span>Sessions</span>
      <ChevronDown size={11} class={sessionScope === "account" ? "rotated" : ""} />
    </button>
  </div>

  <div class="agent-strip" aria-label="Registered agents">
    {#if account.agents.length === 0}
      <div class="agent-empty">
        <strong>No agents registered</strong>
        <span>Registered agents will appear here automatically.</span>
      </div>
    {/if}
    {#each sortedAgents as agent (agent.agent_id)}
      <article class:active={agent.agent_id === activeAgentId} class="agent-card">
        <div class="agent-copy">
          <span class="agent-kind">{kindLabel(agent.kind)}</span>
          <strong>{agent.label}</strong>
          <span title={agent.nexus_endpoint ?? agent.provider_url ?? ""}>
            {agent.nexus_endpoint ?? agent.provider_url ?? "No endpoint"}
          </span>
          <button
            type="button"
            class:active={sessionScope === agent.agent_id}
            class="sessions-trigger agent-sessions-trigger"
            aria-expanded={sessionScope === agent.agent_id}
            onclick={(event) => toggleSessions(agent.agent_id, event)}
          >
            <History size={10} />
            <span>Sessions</span>
            <span>{agent.active_session_count}/{agent.session_count}</span>
            <ChevronDown
              size={10}
              class={sessionScope === agent.agent_id ? "rotated" : ""}
            />
          </button>
        </div>
        <div class="agent-actions">
          <span>{agent.active_session_count} live</span>
          <button
            type="button"
            class="new-button"
            disabled={mounting}
            aria-label={`Create new ${agent.label} session`}
            onclick={() => void onMountAgent(agent.agent_id)}
          >
            New
          </button>
        </div>
      </article>
    {/each}
  </div>

  <div class="resource-strip" aria-label="Account MCP servers">
    <span class="resource-label">MCP servers</span>
    {#each sortedMcpServers as resource (`mcp-${resource.name}`)}
      <article class="resource-card">
        <span>{resource.kind === "nexus" ? "Nexus · MCP" : "External · MCP"}</span>
        <strong>{resource.name}</strong>
        <small title={resource.endpoint}>{resource.endpoint}</small>
        <button
          type="button"
          class:active={toolCallServerName === resource.name}
          class="tool-calls-trigger"
          aria-expanded={toolCallServerName === resource.name}
          onclick={(event) => toggleToolCalls(resource, event)}
        >
          <Braces size={10} />
          <span>Tool calls</span>
          <ChevronDown
            size={10}
            class={toolCallServerName === resource.name ? "rotated" : ""}
          />
        </button>
      </article>
    {/each}

    {#if account.mcp_servers.length === 0}
      <span class="empty">No MCP servers registered</span>
    {/if}
  </div>

  {#if sessionScope}
    <section
      class="session-popover"
      aria-label={sessionBrowserTitle}
      style={`--session-popover-left: ${sessionPopoverLeft}px; --session-popover-top: ${sessionPopoverTop}px`}
    >
      <header class="session-popover-head">
        <span class="session-popover-title">
          <History size={15} />
          <span>{sessionBrowserTitle}</span>
          <small>{scopedSessions.length}</small>
        </span>
        <div class="session-popover-actions">
          {#if onRefreshSessions}
            <button
              type="button"
              class:spinning={refreshingSessions}
              class="icon-button"
              aria-label="Refresh sessions"
              disabled={refreshingSessions}
              onclick={() => void refreshSessions()}
            >
              <RefreshCw size={14} />
            </button>
          {/if}
          <button
            type="button"
            class="icon-button"
            aria-label="Close sessions"
            onclick={() => (sessionScope = null)}
          >
            <X size={15} />
          </button>
        </div>
      </header>

      <label class="session-search">
        <Search size={14} aria-hidden="true" />
        <input
          bind:value={sessionSearch}
          placeholder="Search sessions"
          aria-label="Search sessions"
        />
      </label>

      <div class="session-body">
        {#if closeDecision}
          <section class="close-approval" aria-label="Subagent close confirmation">
          <header>
            <div class="close-approval-copy">
              <strong>Close {closeDecision.agentLabel}?</strong>
              <span>
                {closeDecision.subagents.length} active subagent{closeDecision.subagents.length === 1 ? "" : "s"}
                {closeDecision.subagents.length === 1 ? " is" : " are"} still running.
              </span>
            </div>
            <StatusChip label="Awaiting decision" kind="approval" compact active />
          </header>
          <div class="approval-actions">
            <button
              type="button"
              class="approval-approve"
              disabled={closingSessionIds.includes(closeDecision.sessionId)}
              onclick={() => void closeSession(closeDecision!.sessionId, "keep-open")}
            >
              Keep subagents open
            </button>
            <button
              type="button"
              class="approval-reject"
              disabled={closingSessionIds.includes(closeDecision.sessionId)}
              onclick={() => void closeSession(closeDecision!.sessionId, "close")}
            >
              Close all
            </button>
            <button
              type="button"
              disabled={closingSessionIds.includes(closeDecision.sessionId)}
              onclick={() => (closeDecision = null)}
            >
              Cancel
            </button>
          </div>
          </section>
        {/if}

        <div class="session-list">
          {#if visibleSessions.length === 0}
            <p class="session-empty">No matching sessions.</p>
          {/if}
          {#each visibleSessions as item (item.workflow_id)}
            <div class:active={item.workflow_id === sessionId} class="session-row">
            <div class="session-summary">
              <AgentGlyph
                label={sessionAgentLabel(item)}
                workflowType={item.agent_workflow_type}
                status={item.workflow_id === sessionId && !item.closed ? "available" : "idle"}
              />
              <span class="session-copy">
                <time>{sessionCreatedAt(item.created_at)}</time>
                <strong>{sessionInitialMessage(item)}</strong>
                <small>{sessionAgentLabel(item)}{item.is_spawned ? " · spawned" : ""}</small>
              </span>
            </div>
            <div class="session-actions">
              <StatusChip
                label={sessionStatusLabel(item)}
                kind={sessionStatusKind(item)}
                compact
              />
              <button
                type="button"
                class="mount-session"
                aria-label={`Mount ${sessionAgentLabel(item)} session`}
                disabled={item.workflow_id === sessionId || mountingSessionId != null}
                onclick={() => void openSession(item.workflow_id)}
              >
                {mountingSessionId === item.workflow_id
                  ? "Mounting"
                  : item.workflow_id === sessionId
                    ? "Mounted"
                    : item.closed
                      ? "View"
                      : "Mount"}
              </button>
              {#if !item.closed && onCloseSession}
                <button
                  type="button"
                  class="close-session"
                  aria-label={`Close ${sessionAgentLabel(item)} session`}
                  title="Close agent session"
                  disabled={closingSessionIds.includes(item.workflow_id)}
                  onclick={() => void closeSession(item.workflow_id)}
                >
                  <X size={13} />
                </button>
              {/if}
            </div>
            </div>
          {/each}
        </div>
      </div>
    </section>
  {/if}

  {#if toolCallServer}
    <ToolCallHistory
      server={toolCallServer}
      calls={toolCalls}
      loading={loadingToolCalls}
      error={toolCallError}
      left={toolHistoryLeft}
      top={toolHistoryTop}
      onRefresh={() => loadToolCalls(toolCallServer.name)}
      onClose={() => {
        toolCallServerName = null;
        toolCallRequestVersion += 1;
      }}
    />
  {/if}

  {#if catalogOpen}
    <section
      class="catalog-popover"
      aria-label="Global resource catalog"
      style={`--catalog-left: ${catalogLeft}px; --catalog-top: ${catalogTop}px`}
    >
      <header class="session-popover-head">
        <span class="session-popover-title">
          <Library size={15} />
          <span>Global catalog</span>
          <small>{catalogResources.length}</small>
        </span>
        <div class="session-popover-actions">
          <button
            type="button"
            class:spinning={catalogLoading}
            class="icon-button"
            aria-label="Refresh catalog"
            disabled={catalogLoading}
            onclick={() => void loadCatalog()}
          >
            <RefreshCw size={14} />
          </button>
          <button
            type="button"
            class="icon-button"
            aria-label="Close catalog"
            onclick={() => (catalogOpen = false)}
          >
            <X size={15} />
          </button>
        </div>
      </header>
      <p class="catalog-copy">
        Install agents and MCP servers into {account.account_id}. Native calls remain direct over Nexus.
      </p>
      {#if catalogError}<p class="catalog-error">{catalogError}</p>{/if}
      <div class="catalog-list">
        {#if !catalogLoading && catalogResources.length === 0}
          <p class="session-empty">No catalog resources published.</p>
        {/if}
        {#each sortedCatalogResources as resource (resource.resource_id)}
          <article class="catalog-row">
            <span class="catalog-resource-copy">
              <small>{catalogKind(resource)} · r{resource.revision}</small>
              <strong>{resource.label}</strong>
              <span>{resource.description}</span>
              <code title={resource.endpoint}>{resource.endpoint}</code>
            </span>
            <button
              type="button"
              class:installed={resource.installed}
              class="catalog-action"
              disabled={catalogMutation != null}
              onclick={() => void mutateCatalog(resource)}
            >
              {catalogMutation === resource.resource_id
                ? "Working…"
                : resource.installed
                  ? "Remove"
                  : "Register"}
            </button>
          </article>
        {/each}
      </div>
    </section>
  {/if}
</section>

<style>
  .account-pane {
    position: relative;
    z-index: 5;
    width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
    overflow-x: visible;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 12px;
    border-right: 1px solid var(--border);
    background:
      linear-gradient(180deg, color-mix(in srgb, var(--accent) 7%, transparent), transparent 28%),
      var(--surface-1);
  }

  .account-identity,
  .catalog-trigger,
  .agent-card,
  .resource-strip {
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: color-mix(in srgb, var(--surface-2) 88%, transparent);
  }

  .account-identity {
    min-width: 0;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 8px 10px;
  }

  .eyebrow,
  .agent-kind,
  .resource-label {
    color: var(--text-3);
    font-size: 9px;
    font-weight: 750;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .account-identity > strong {
    overflow: hidden;
    color: var(--text-1);
    font-size: 13px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .summary,
  .empty {
    color: var(--text-3);
    font-size: 10px;
  }

  .catalog-trigger {
    width: 100%;
    min-height: 36px;
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 12px;
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    font-size: 11px;
    font-weight: 750;
  }

  .catalog-trigger-label {
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }

  .catalog-trigger:hover,
  .catalog-trigger:focus-visible,
  .catalog-trigger.active {
    border-color: color-mix(in srgb, var(--reasoning) 45%, var(--border-strong));
    color: var(--text-1);
    background: color-mix(in srgb, var(--reasoning) 8%, var(--control-hover));
    outline: 0;
  }

  .catalog-trigger :global(.rotated) {
    transform: rotate(180deg);
  }

  .sessions-trigger {
    width: fit-content;
    height: 22px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin-top: 5px;
    padding: 0 7px;
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text-2);
    background: var(--control-bg);
    cursor: pointer;
    font: inherit;
    font-size: 9px;
    font-weight: 700;
  }

  .sessions-trigger:hover,
  .sessions-trigger:focus-visible,
  .sessions-trigger.active {
    border-color: color-mix(in srgb, var(--reasoning) 45%, var(--border-strong));
    color: var(--text-1);
    background: color-mix(in srgb, var(--reasoning) 8%, var(--control-hover));
    outline: 0;
  }

  .sessions-trigger :global(.rotated) {
    transform: rotate(180deg);
  }

  .agent-sessions-trigger > span:last-of-type {
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  .agent-strip {
    min-width: 0;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .agent-empty {
    min-width: 0;
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 8px 12px;
    border: 1px dashed var(--border-strong);
    border-radius: 8px;
    background: color-mix(in srgb, var(--surface-2) 72%, transparent);
  }

  .agent-empty strong {
    color: var(--text-2);
    font-size: 11px;
  }

  .agent-empty span {
    color: var(--text-3);
    font-size: 10px;
  }

  .agent-card {
    width: 100%;
    min-width: 0;
    flex: 0 0 auto;
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    padding: 7px 8px 7px 10px;
  }

  .agent-card.active {
    border-color: color-mix(in srgb, var(--accent) 48%, var(--border-strong));
    box-shadow: inset 3px 0 0 color-mix(in srgb, var(--accent) 75%, white);
  }

  .agent-copy {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .agent-copy > strong,
  .agent-copy > span:not(.agent-kind) {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-copy > strong {
    color: var(--text-1);
    font-size: 12px;
  }

  .agent-copy > span:not(.agent-kind),
  .agent-actions > span {
    color: var(--text-3);
    font-size: 9px;
  }

  .agent-actions {
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-end;
  }

  .new-button {
    min-width: 62px;
    height: 26px;
    padding: 0 9px;
    border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border-strong));
    border-radius: 6px;
    color: color-mix(in srgb, var(--accent) 76%, white);
    background: color-mix(in srgb, var(--accent) 11%, var(--control-bg));
    cursor: pointer;
    font: inherit;
    font-size: 10px;
    font-weight: 700;
  }

  .new-button:hover:not(:disabled),
  .new-button:focus-visible {
    background: color-mix(in srgb, var(--accent) 19%, var(--control-bg));
    outline: 0;
  }

  .new-button:disabled {
    cursor: wait;
    opacity: 0.55;
  }

  .resource-strip {
    min-width: 0;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    gap: 5px;
    align-items: stretch;
    padding: 7px 9px;
  }

  .resource-label {
    width: 100%;
  }

  .resource-card {
    min-width: 0;
    display: grid;
    gap: 2px;
    padding: 9px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 48%, var(--surface-1));
  }

  .resource-card > span {
    color: var(--text-3);
    font-size: 9px;
    font-weight: 750;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .resource-card > strong,
  .resource-card > small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .resource-card > strong {
    color: var(--text-1);
    font-size: 11px;
  }

  .resource-card > small {
    color: var(--text-3);
    font-size: 9px;
  }

  .tool-calls-trigger {
    width: fit-content;
    height: 22px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin-top: 5px;
    padding: 0 7px;
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text-2);
    background: var(--control-bg);
    cursor: pointer;
    font: inherit;
    font-size: 9px;
    font-weight: 700;
  }

  .tool-calls-trigger:hover,
  .tool-calls-trigger:focus-visible,
  .tool-calls-trigger.active {
    border-color: color-mix(in srgb, var(--warning) 45%, var(--border-strong));
    color: var(--text-1);
    background: color-mix(in srgb, var(--warning) 8%, var(--control-hover));
    outline: 0;
  }

  .tool-calls-trigger :global(.rotated) {
    transform: rotate(180deg);
  }

  .session-popover {
    position: fixed;
    top: var(--session-popover-top);
    left: var(--session-popover-left);
    z-index: 30;
    width: min(520px, calc(100vw - var(--session-popover-left) - 16px));
    max-height: min(560px, calc(100vh - 180px));
    min-height: 0;
    overflow: hidden;
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr);
    gap: 10px;
    padding: 14px 12px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-1);
    box-shadow: var(--shadow-popover);
  }

  .catalog-popover {
    position: fixed;
    top: var(--catalog-top);
    left: var(--catalog-left);
    z-index: 31;
    width: min(560px, calc(100vw - var(--catalog-left) - 16px));
    max-height: min(600px, calc(100vh - 32px));
    min-height: 0;
    overflow: hidden;
    display: grid;
    grid-template-rows: auto auto auto minmax(0, 1fr);
    gap: 10px;
    padding: 14px 12px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-1);
    box-shadow: var(--shadow-popover);
  }

  .catalog-copy,
  .catalog-error {
    margin: 0;
    font-size: 10px;
  }

  .catalog-copy {
    color: var(--text-3);
  }

  .catalog-error {
    padding: 7px 8px;
    border: 1px solid color-mix(in srgb, var(--error) 45%, var(--border));
    border-radius: 5px;
    color: var(--error);
    background: color-mix(in srgb, var(--error) 7%, transparent);
  }

  .catalog-list {
    min-height: 0;
    overflow-y: auto;
    display: grid;
    align-content: start;
    gap: 8px;
  }

  .catalog-row {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    padding: 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 48%, var(--surface-1));
  }

  .catalog-resource-copy {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .catalog-resource-copy > small {
    color: var(--text-3);
    font-size: 8px;
    font-weight: 750;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .catalog-resource-copy > strong {
    color: var(--text-1);
    font-size: 11px;
  }

  .catalog-resource-copy > span,
  .catalog-resource-copy > code {
    overflow: hidden;
    color: var(--text-3);
    font-size: 9px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .catalog-action {
    min-width: 72px;
    height: 27px;
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
    border-radius: 6px;
    color: color-mix(in srgb, var(--accent) 76%, white);
    background: color-mix(in srgb, var(--accent) 10%, var(--control-bg));
    cursor: pointer;
    font: inherit;
    font-size: 9px;
    font-weight: 750;
  }

  .catalog-action.installed {
    border-color: var(--border);
    color: var(--text-3);
    background: var(--control-bg);
  }

  .catalog-action:hover:not(:disabled),
  .catalog-action:focus-visible {
    color: var(--text-1);
    background: var(--control-hover);
    outline: 0;
  }

  .catalog-action:disabled {
    cursor: wait;
    opacity: 0.55;
  }

  .session-popover-head,
  .session-popover-actions,
  .session-popover-title {
    display: flex;
    align-items: center;
  }

  .session-popover-head {
    min-width: 0;
    justify-content: space-between;
    gap: 10px;
  }

  .session-popover-title {
    min-width: 0;
    gap: 7px;
    color: var(--text-1);
    font-size: 13px;
    font-weight: 700;
  }

  .session-popover-title small {
    color: var(--text-3);
    font-size: 10px;
    font-weight: 600;
  }

  .session-popover-actions {
    gap: 6px;
  }

  .icon-button,
  .close-session {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-3);
    cursor: pointer;
  }

  .icon-button {
    width: 28px;
    height: 28px;
  }

  .icon-button:hover:not(:disabled),
  .icon-button:focus-visible,
  .close-session:hover:not(:disabled),
  .close-session:focus-visible {
    border-color: var(--border-strong);
    color: var(--text-1);
    outline: 0;
  }

  .icon-button:disabled,
  .close-session:disabled {
    cursor: wait;
    opacity: 0.55;
  }

  .icon-button.spinning :global(svg) {
    animation: session-refresh-spin 800ms linear infinite;
  }

  @keyframes session-refresh-spin {
    to {
      transform: rotate(360deg);
    }
  }

  .session-search {
    min-width: 0;
    height: 34px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-3);
    background: var(--control-bg);
  }

  .session-search:focus-within {
    border-color: color-mix(in srgb, var(--accent) 48%, var(--border-strong));
    box-shadow: 0 0 0 3px var(--focus-ring);
  }

  .session-search input {
    min-width: 0;
    border: 0;
    outline: 0;
    color: var(--text-1);
    background: transparent;
    font: inherit;
    font-size: 12px;
  }

  .close-approval {
    min-width: 0;
    display: grid;
    gap: 9px;
    padding: 10px;
    border: 1px solid color-mix(in srgb, var(--queue) 42%, var(--border));
    border-radius: 8px;
    background: color-mix(in srgb, var(--queue) 9%, var(--surface-1));
  }

  .close-approval > header {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .close-approval-copy {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .close-approval-copy strong {
    color: var(--text-1);
    font-size: 12px;
  }

  .close-approval-copy span {
    color: var(--text-3);
    font-size: 11px;
  }

  .approval-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .approval-actions button {
    min-height: 26px;
    padding: 4px 8px;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text-2);
    background: var(--surface-0);
    cursor: pointer;
    font: inherit;
    font-size: 10px;
  }

  .approval-actions button:hover:not(:disabled),
  .approval-actions button:focus-visible {
    border-color: var(--border-strong);
    outline: 0;
  }

  .approval-actions button:disabled {
    cursor: wait;
    opacity: 0.55;
  }

  .approval-actions .approval-approve {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 35%, var(--border));
  }

  .approval-actions .approval-reject {
    color: var(--error);
    border-color: color-mix(in srgb, var(--error) 35%, var(--border));
  }

  .session-list {
    min-height: 0;
    overflow-y: auto;
    display: grid;
    align-content: start;
    gap: 8px;
  }

  .session-body {
    min-height: 0;
    overflow: hidden;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    gap: 8px;
  }

  .session-row {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 9px;
    align-items: center;
    border: 1px solid color-mix(in srgb, var(--reasoning) 10%, var(--border));
    border-radius: 7px;
    color: inherit;
    background: color-mix(in srgb, var(--surface-2) 42%, var(--surface-1));
    transition: border-color 140ms ease, background 140ms ease;
  }

  .session-row:hover,
  .session-row:focus-within {
    border-color: color-mix(in srgb, var(--reasoning) 38%, var(--border-strong));
    background: color-mix(in srgb, var(--reasoning) 5%, var(--surface-2));
  }

  .session-row.active {
    border-color: color-mix(in srgb, var(--accent) 54%, var(--border));
    background: color-mix(in srgb, var(--accent) 10%, var(--surface-1));
    box-shadow: inset 3px 0 0 var(--accent);
  }

  .session-summary {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 9px;
    align-items: start;
    padding: 10px 0 10px 10px;
    color: inherit;
    background: transparent;
    text-align: left;
  }

  .session-copy {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .session-copy time,
  .session-copy small {
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-copy strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: 12px;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-actions {
    display: inline-flex;
    gap: 6px;
    align-items: center;
    padding-right: 10px;
  }

  .mount-session {
    min-width: 50px;
    height: 24px;
    padding: 0 7px;
    border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border-strong));
    border-radius: 6px;
    color: color-mix(in srgb, var(--accent) 76%, white);
    background: color-mix(in srgb, var(--accent) 10%, var(--control-bg));
    cursor: pointer;
    font: inherit;
    font-size: 9px;
    font-weight: 700;
  }

  .mount-session:hover:not(:disabled),
  .mount-session:focus-visible {
    background: color-mix(in srgb, var(--accent) 18%, var(--control-bg));
    outline: 0;
  }

  .mount-session:disabled {
    cursor: default;
    opacity: 0.6;
  }

  .close-session {
    width: 24px;
    height: 24px;
    flex: 0 0 auto;
  }

  .close-session:hover:not(:disabled),
  .close-session:focus-visible {
    border-color: color-mix(in srgb, var(--error) 55%, var(--border-strong));
    color: color-mix(in srgb, var(--error) 78%, white);
    background: color-mix(in srgb, var(--error) 8%, var(--control-bg));
  }

  .session-empty {
    margin: 6px 2px;
    color: var(--text-3);
    font-size: 12px;
  }

  @media (max-width: 1100px) {
    .account-pane {
      grid-template-columns: minmax(160px, 0.6fr) minmax(0, 2fr);
    }

    .resource-strip {
      display: none;
    }
  }

  @media (max-width: 700px) {
    .account-pane {
      grid-template-columns: 1fr;
    }
  }
</style>
