<script lang="ts">
  import {
    AlertTriangle,
    BrainCircuit,
    CheckCircle2,
    CircleDot,
    Clock3,
    GitBranch,
    Hourglass,
    LoaderCircle,
    Network,
    Radio,
    ShieldAlert,
    Sparkles,
    Wrench,
    XCircle
  } from "@lucide/svelte";

  export type StatusKind =
    | "idle"
    | "available"
    | "starting"
    | "connecting"
    | "thinking"
    | "planning"
    | "reasoning"
    | "model"
    | "tool"
    | "approval"
    | "delegating"
    | "queued"
    | "blocked"
    | "closed"
    | "complete"
    | "error";

  interface Props {
    label: string;
    kind?: StatusKind;
    detail?: string | null;
    active?: boolean;
    compact?: boolean;
    pulse?: boolean;
  }

  let {
    label,
    kind = "idle",
    detail = null,
    active = false,
    compact = false,
    pulse = false
  }: Props = $props();

  const animated = $derived(
    pulse ||
      active ||
      kind === "connecting" ||
      kind === "thinking" ||
      kind === "reasoning" ||
      kind === "tool" ||
      kind === "delegating"
  );
</script>

<span class={`status-chip ${kind} ${active ? "active" : ""} ${compact ? "compact" : ""}`}>
  <span class={`status-icon ${animated ? "animated" : ""}`} aria-hidden="true">
    {#if kind === "available"}
      <CheckCircle2 size={compact ? 12 : 13} />
    {:else if kind === "starting"}
      <Hourglass size={compact ? 12 : 13} />
    {:else if kind === "connecting"}
      <Radio size={compact ? 12 : 13} />
    {:else if kind === "thinking" || kind === "planning"}
      <Sparkles size={compact ? 12 : 13} />
    {:else if kind === "reasoning" || kind === "model"}
      <BrainCircuit size={compact ? 12 : 13} />
    {:else if kind === "tool"}
      <Wrench size={compact ? 12 : 13} />
    {:else if kind === "approval"}
      <ShieldAlert size={compact ? 12 : 13} />
    {:else if kind === "delegating"}
      <Network size={compact ? 12 : 13} />
    {:else if kind === "queued"}
      <GitBranch size={compact ? 12 : 13} />
    {:else if kind === "blocked"}
      <AlertTriangle size={compact ? 12 : 13} />
    {:else if kind === "closed"}
      <XCircle size={compact ? 12 : 13} />
    {:else if kind === "complete"}
      <CheckCircle2 size={compact ? 12 : 13} />
    {:else if kind === "error"}
      <XCircle size={compact ? 12 : 13} />
    {:else}
      <CircleDot size={compact ? 12 : 13} />
    {/if}
  </span>
  <span class="status-text">{label}</span>
  {#if detail && !compact}
    <span class="status-detail">{detail}</span>
  {/if}
</span>

<style>
  .status-chip {
    --status-color: var(--text-3);
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    min-height: 24px;
    padding: 2px 8px;
    border: 1px solid color-mix(in srgb, var(--status-color) 30%, var(--border));
    border-radius: 6px;
    color: color-mix(in srgb, var(--status-color) 82%, white);
    background:
      linear-gradient(
        180deg,
        color-mix(in srgb, var(--status-color) 10%, var(--surface-2)),
        color-mix(in srgb, var(--surface-1) 92%, black)
      );
    box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.05);
    font-size: 11px;
    font-weight: 750;
    line-height: 1.2;
    white-space: nowrap;
  }

  .status-chip.compact {
    min-height: 20px;
    gap: 5px;
    padding: 1px 6px;
    font-size: 10px;
  }

  .status-chip.active::after {
    content: "";
    position: absolute;
    inset: -1px;
    border-radius: inherit;
    border: 1px solid color-mix(in srgb, var(--status-color) 38%, transparent);
    opacity: 0.7;
    pointer-events: none;
  }

  .status-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
  }

  .status-icon.animated {
    animation: status-breathe 1.3s ease-in-out infinite;
  }

  .status-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .status-detail {
    max-width: 120px;
    overflow: hidden;
    color: var(--text-3);
    font-size: 10px;
    font-weight: 700;
    text-overflow: ellipsis;
  }

  .status-chip.available,
  .status-chip.complete {
    --status-color: var(--success);
  }

  .status-chip.starting,
  .status-chip.connecting,
  .status-chip.thinking,
  .status-chip.planning {
    --status-color: var(--accent);
  }

  .status-chip.reasoning,
  .status-chip.model {
    --status-color: var(--model);
  }

  .status-chip.tool {
    --status-color: var(--warning);
  }

  .status-chip.approval,
  .status-chip.queued {
    --status-color: var(--queue);
  }

  .status-chip.delegating {
    --status-color: var(--reasoning);
  }

  .status-chip.blocked,
  .status-chip.error {
    --status-color: var(--error);
  }

  .status-chip.closed {
    --status-color: var(--text-3);
    color: var(--text-2);
    background:
      linear-gradient(
        180deg,
        color-mix(in srgb, var(--text-3) 7%, var(--surface-2)),
        color-mix(in srgb, var(--surface-1) 94%, black)
      );
  }

  @keyframes status-breathe {
    0%, 100% {
      opacity: 0.68;
      transform: scale(1);
    }
    50% {
      opacity: 1;
      transform: scale(1.08);
    }
  }
</style>
