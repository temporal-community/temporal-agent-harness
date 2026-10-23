<script lang="ts">
  import {
    AlertTriangle,
    CheckCircle2,
    LoaderCircle,
    ShieldAlert,
    XCircle
  } from "@lucide/svelte";
  import type { StatusKind } from "$lib/components/primitives/StatusChip.svelte";

  /**
   * The glyph that leads a title, and only where the glyph is the state:
   * finished, waiting on a person, failed, or in motion. Every other state
   * draws nothing, so the title starts flush. Coloured by the caller's
   * --status-color; the status word beside it carries the meaning, so this
   * is hidden from assistive tech.
   */
  interface Props {
    kind: StatusKind;
    running?: boolean;
    size?: number;
  }

  let { kind, running = false, size = 14 }: Props = $props();

  const GLYPHS: Partial<Record<StatusKind, typeof CheckCircle2>> = {
    available: CheckCircle2,
    complete: CheckCircle2,
    approval: ShieldAlert,
    stuck: AlertTriangle,
    blocked: AlertTriangle,
    error: XCircle
  };

  const Glyph = $derived(running ? LoaderCircle : GLYPHS[kind]);
</script>

{#if Glyph}
  <span class={["state-glyph", running && "spinning"]} aria-hidden="true">
    <Glyph {size} />
  </span>
{/if}

<style>
  .state-glyph {
    flex: 0 0 auto;
    display: inline-flex;
    color: var(--status-color, currentColor);
  }

  .spinning {
    animation: state-spin 1s linear infinite;
  }

  @keyframes state-spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .spinning {
      animation: none;
    }
  }
</style>
