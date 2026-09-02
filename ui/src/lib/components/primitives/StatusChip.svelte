<script lang="ts" module>
  import type { ChipTone } from "$lib/components/primitives/Chip.svelte";

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
    | "retrying"
    | "stuck"
    | "delegating"
    | "queued"
    | "blocked"
    | "closed"
    | "complete"
    | "error";

  /**
   * One status, one hue, wherever it is drawn. Exported because a status is not
   * always a chip: the session anchor spends it on a single pip, and a second
   * hand-written mapping would be one drift away from a green pip beside a red
   * chip describing the same run.
   */
  export const STATUS_TONES: Record<StatusKind, ChipTone> = {
    idle: "neutral",
    available: "success",
    starting: "accent",
    connecting: "accent",
    thinking: "accent",
    planning: "accent",
    reasoning: "model",
    model: "model",
    tool: "tool",
    /* Waiting on a person, not on a slot. --live is reserved for "this needs
       you" and an approval is the purest case of it — the run is stopped until
       someone answers — so it cannot sit in --queue beside a queued turn, which
       is waiting on capacity and needs nobody. The rest of the app already
       reads it this way: the pane accent, the pane headline pip, and the chat
       pane's "needs you" indicator all put approvals in --live. */
    approval: "live",
    retrying: "retry",
    /* Joins approvals in the "a person should look at this" register. */
    stuck: "live",
    delegating: "reasoning",
    queued: "queue",
    blocked: "error",
    closed: "neutral",
    complete: "success",
    error: "error"
  };
</script>

<script lang="ts">
  import {
    AlertTriangle,
    RotateCcw,
    BrainCircuit,
    CheckCircle2,
    CircleDot,
    GitBranch,
    Hourglass,
    Network,
    Radio,
    ShieldAlert,
    Sparkles,
    Wrench,
    XCircle
  } from "@lucide/svelte";
  import Chip, { type ChipSize } from "$lib/components/primitives/Chip.svelte";

  interface Props {
    label: string;
    kind?: StatusKind;
    detail?: string | null;
    active?: boolean;
    /** Tightens padding and the icon without leaving the control row. */
    compact?: boolean;
    /**
     * Defaults to the content row. Chip height says where you are: 22px inside a
     * pane, 28px in the app chrome. Only the topbar passes "sm".
     */
    size?: ChipSize;
    pulse?: boolean;
  }

  let {
    label,
    kind = "idle",
    detail = null,
    active = false,
    compact = false,
    size = "xs",
    pulse = false
  }: Props = $props();

  const ICONS: Record<StatusKind, typeof CircleDot> = {
    idle: CircleDot,
    available: CheckCircle2,
    starting: Hourglass,
    connecting: Radio,
    thinking: Sparkles,
    planning: Sparkles,
    reasoning: BrainCircuit,
    model: BrainCircuit,
    tool: Wrench,
    approval: ShieldAlert,
    retrying: RotateCcw,
    stuck: AlertTriangle,
    delegating: Network,
    queued: GitBranch,
    blocked: AlertTriangle,
    closed: XCircle,
    complete: CheckCircle2,
    error: XCircle
  };

  const ANIMATED_KINDS: StatusKind[] = [
    "connecting",
    "thinking",
    "reasoning",
    "tool",
    "delegating",
    /* A retry is in motion by definition; a stuck one has stopped being news. */
    "retrying"
  ];

  const Icon = $derived(ICONS[kind]);
  const animated = $derived(pulse || active || ANIMATED_KINDS.includes(kind));
  const iconSize = $derived(compact || size === "xs" ? 12 : 13);
</script>

<Chip tone={STATUS_TONES[kind]} {size} {active} ring={active} dense={compact}>
  {#snippet lead()}
    <span class={`status-icon ${animated ? "animated" : ""}`}>
      <Icon size={iconSize} />
    </span>
  {/snippet}
  <span class="status-text">{label}</span>
  {#if detail && !compact}
    <span class="status-detail">{detail}</span>
  {/if}
</Chip>

<style>
  .status-text {
    flex: none;
    overflow: visible;
  }

  .status-detail {
    max-width: 120px;
    overflow: hidden;
    color: var(--text-3);
    font-size: inherit;
    font-weight: 700;
    text-overflow: ellipsis;
  }

  .status-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .status-icon.animated {
    animation: status-breathe 900ms var(--ease-in-out, ease-in-out) infinite;
  }

  @keyframes status-breathe {
    0%, 100% {
      opacity: 0.55;
    }
    50% {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .status-icon.animated {
      animation: none;
      opacity: 0.85;
    }
  }
</style>
