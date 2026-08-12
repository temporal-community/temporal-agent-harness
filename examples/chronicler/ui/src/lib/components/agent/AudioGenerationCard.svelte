<script lang="ts">
  import { AudioLines, ChevronRight, Circle, GitBranch, RotateCcw, X } from "@lucide/svelte";
  import type {
    AudioGenerationPhase,
    ChroniclerAudioDestinationApproval,
    ChroniclerAudioSnapshot
  } from "$lib/bridge/api";

  interface Props {
    snapshot: ChroniclerAudioSnapshot;
    cancellation?: { enabled: boolean; detail: string };
    onCancel?: (childWorkflowId: string) => void | Promise<void>;
    onRecover?: (snapshot: ChroniclerAudioSnapshot) => void | Promise<void>;
    recoveryAvailable?: boolean;
    destinationApproval?: ChroniclerAudioDestinationApproval | null;
    destinationAuthority?: { ready: boolean; detail: string };
    onApproveDestination?: (
      approval: ChroniclerAudioDestinationApproval
    ) => void | Promise<void>;
  }

  let {
    snapshot,
    cancellation = { enabled: false, detail: "Connect the browser bridge before canceling audio generation." },
    onCancel,
    onRecover,
    recoveryAvailable = false,
    destinationApproval = null,
    destinationAuthority = { ready: false, detail: "Checking destination approval authority…" },
    onApproveDestination
  }: Props = $props();

  const labels: Record<AudioGenerationPhase, string> = {
    generating_audio: "Generating audio",
    saving_wav: "Saving WAV",
    saving_synthetic_transcript: "Saving synthetic transcript",
    destination_approval_needed: "Destination approval needed",
    waiting_for_folder: "Waiting for folder",
    canceling: "Canceling",
    complete: "Complete",
    failed: "Failed",
    canceled: "Canceled"
  };
  const phaseOrder: AudioGenerationPhase[] = [
    "generating_audio",
    "saving_wav",
    "saving_synthetic_transcript",
    "complete"
  ];

  function formatDuration(durationSeconds: number): string {
    const seconds = Math.round(durationSeconds);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }
</script>

<article class="generation-card" aria-label="Generate audio">
  <header>
    <div class="tool-mark"><AudioLines size={16} /></div>
    <div>
      <span class="eyebrow">Parent tool</span>
      <h3>Generate audio</h3>
    </div>
    <span class={`state ${snapshot.state}`}>{snapshot.state}</span>
  </header>

  <div class="child-rail" aria-label="Nested child workflow">
    <ChevronRight class="branch" size={16} aria-hidden="true" />
    <section class="child-card">
      <div class="child-head">
        <span><GitBranch size={13} /> Nested child workflow</span>
        <code>{snapshot.child_workflow_id}</code>
      </div>
      <ol>
        {#each phaseOrder as phase}
          <li class:active={snapshot.status.phase === phase} aria-current={snapshot.status.phase === phase ? "step" : undefined}>
            <Circle size={10} fill={snapshot.status.phase === phase ? "currentColor" : "none"} />
            <span>{labels[phase]}</span>
          </li>
        {/each}
      </ol>
      {#if !phaseOrder.includes(snapshot.status.phase)}
        <p class="interrupt" aria-current="step">{labels[snapshot.status.phase]}</p>
      {/if}
      {#if snapshot.status.detail}<p class="detail">{snapshot.status.detail}</p>{/if}
      {#if snapshot.result?.duration_s != null}
        <p class="duration"><span>Duration</span> {formatDuration(snapshot.result.duration_s)}</p>
      {/if}
      {#if snapshot.status.phase === "destination_approval_needed" && destinationApproval}
        <section class="destination-review" aria-label="Destination-only approval">
          <span>New WAV destination</span>
          <code>{destinationApproval.wav_path}</code>
          {#if destinationApproval.synthetic_markdown_path}
            <span>New synthetic transcript destination</span>
            <code>{destinationApproval.synthetic_markdown_path}</code>
          {/if}
          <button
            type="button"
            disabled={!destinationAuthority.ready || !onApproveDestination}
            onclick={() => onApproveDestination?.(destinationApproval)}
          >Approve new destinations</button>
          <small>{destinationAuthority.detail}</small>
        </section>
      {/if}
      {#if snapshot.state === "running"}
        <footer>
          <span>{cancellation.detail}</span>
          <button
            type="button"
            disabled={snapshot.status.phase === "canceling" || !cancellation.enabled || !onCancel}
            onclick={() => onCancel?.(snapshot.child_workflow_id)}
          ><X size={12} /> Cancel</button>
        </footer>
      {:else if recoveryAvailable && (
        snapshot.result?.outcome === "failed"
        || snapshot.result?.outcome === "needs_recovery"
      )}
        <footer class="recovery">
          <span>Retry the unchanged approved package under the same generation.</span>
          <button type="button" disabled={!onRecover} onclick={() => onRecover?.(snapshot)}>
            <RotateCcw size={12} /> Recover approved package
          </button>
        </footer>
      {/if}
    </section>
  </div>
</article>

<style>
  .generation-card { border: 1px solid var(--border); border-radius: 12px; padding: 12px; background: var(--surface-2); color: var(--text-1); }
  header { display: grid; grid-template-columns: auto 1fr auto; gap: 9px; align-items: center; }
  .tool-mark { display: grid; place-items: center; width: 30px; height: 30px; border-radius: 8px; background: color-mix(in srgb, var(--warning) 18%, transparent); color: var(--warning); }
  h3 { margin: 1px 0 0; font-size: 13px; }
  .eyebrow { color: var(--text-3); font-size: 9px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
  .state { border-radius: 999px; padding: 3px 7px; background: color-mix(in srgb, var(--warning) 14%, transparent); color: var(--warning); font-size: 9px; font-weight: 800; text-transform: uppercase; }
  .state.completed { color: var(--success); background: color-mix(in srgb, var(--success) 13%, transparent); }
  .state.failed, .state.canceled { color: var(--error); background: color-mix(in srgb, var(--error) 13%, transparent); }
  .child-rail { display: grid; grid-template-columns: 24px minmax(0, 1fr); margin: 10px 0 0 14px; }
  .branch { margin-top: 12px; color: var(--text-3); }
  .child-card { border: 1px solid color-mix(in srgb, var(--warning) 30%, var(--border)); border-radius: 9px; padding: 10px; background: color-mix(in srgb, var(--surface-1) 90%, transparent); }
  .child-head { display: grid; gap: 4px; }
  .child-head span { display: flex; align-items: center; gap: 5px; color: var(--text-3); font-size: 10px; font-weight: 700; text-transform: uppercase; }
  code { overflow-wrap: anywhere; color: var(--text-1); font-size: 10px; }
  ol { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 5px; margin: 10px 0 0; padding: 0; list-style: none; }
  li { display: flex; gap: 4px; align-items: center; color: var(--text-3); font-size: 9px; }
  li.active { color: var(--warning); }
  .interrupt { margin: 9px 0 0; color: var(--warning); font-size: 11px; font-weight: 700; }
  .detail { margin: 8px 0 0; color: var(--text-3); font-size: 11px; }
  .duration { margin: 8px 0 0; font-size: 11px; }
  .duration span { margin-right: 4px; color: var(--text-3); font-size: 9px; font-weight: 700; text-transform: uppercase; }
  .destination-review { display: grid; gap: 5px; margin-top: 9px; border: 1px solid color-mix(in srgb, var(--warning) 32%, var(--border)); border-radius: 7px; padding: 8px; }
  .destination-review span { color: var(--text-3); font-size: 9px; font-weight: 700; text-transform: uppercase; }
  .destination-review button { justify-self: end; margin-top: 3px; border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); color: var(--warning); }
  .destination-review small { color: var(--text-3); font-size: 9px; }
  footer { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; border-top: 1px solid var(--border); padding-top: 8px; }
  footer span { color: var(--text-3); font-size: 9px; }
  button { display: inline-flex; align-items: center; gap: 4px; border: 1px solid color-mix(in srgb, var(--error) 45%, var(--border)); border-radius: 7px; background: color-mix(in srgb, var(--error) 10%, transparent); color: var(--error); padding: 5px 7px; font: inherit; font-size: 10px; font-weight: 700; cursor: pointer; }
  button:disabled { cursor: not-allowed; opacity: .45; }
</style>
