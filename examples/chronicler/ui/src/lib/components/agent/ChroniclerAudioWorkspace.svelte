<script lang="ts">
  import { BookOpen, MessageSquareText, Sparkles } from "@lucide/svelte";
  import type { ChroniclerTranscriptDiscovery, ChroniclerTranscriptSource } from "$lib/bridge/source";
  import type { ChroniclerAudioDestinationApproval, ChroniclerAudioSnapshot } from "$lib/bridge/api";
  import type { AudioDraft } from "./audioUi";
  import AudioApprovalPanel from "./AudioApprovalPanel.svelte";
  import AudioGenerationCard from "./AudioGenerationCard.svelte";

  interface Props {
    discovery?: ChroniclerTranscriptDiscovery | null;
    draft?: AudioDraft | null;
    snapshot?: ChroniclerAudioSnapshot | null;
    cancellation?: { enabled: boolean; detail: string };
    destinationApproval?: ChroniclerAudioDestinationApproval | null;
    playbackUrl?: string | null;
    error?: string | null;
    busy?: boolean;
    readOnly?: boolean;
    approvalAuthority?: { ready: boolean; detail: string };
    destinationAuthority?: { ready: boolean; detail: string };
    recoveryAvailable?: boolean;
    showGenerationCard?: boolean;
    onPrepareExisting?: (source: ChroniclerTranscriptSource) => void | Promise<void>;
    onPrepareTopic?: (topic: string) => void | Promise<void>;
    onApprove?: (draft: AudioDraft) => void | Promise<void>;
    onRequestChanges?: (draft: AudioDraft, changeRequest: string) => void | Promise<void>;
    onApproveDestination?: (approval: ChroniclerAudioDestinationApproval) => void | Promise<void>;
    onCancel?: (childWorkflowId: string) => void | Promise<void>;
    onRecover?: (snapshot: ChroniclerAudioSnapshot) => void | Promise<void>;
    onRetryPlayback?: () => void | Promise<void>;
    onChangeSource?: () => void;
  }

  let {
    discovery = null,
    draft = null,
    snapshot = null,
    cancellation,
    destinationApproval = null,
    playbackUrl = null,
    error = null,
    busy = false,
    readOnly = false,
    approvalAuthority,
    destinationAuthority,
    recoveryAvailable = false,
    showGenerationCard = true,
    onPrepareExisting,
    onPrepareTopic,
    onApprove,
    onRequestChanges,
    onApproveDestination,
    onCancel,
    onRecover,
    onRetryPlayback,
    onChangeSource
  }: Props = $props();
  let topic = $state("");
</script>

<section class="audio-workspace" aria-label="Chronicler audio recap">
  <header>
    <span><Sparkles size={13} /> Chronicler audio</span>
    <h2>Create spoken recap</h2>
  </header>

  {#if readOnly}<p class="historical">Historical replay · audio controls are read-only.</p>{/if}

  {#if draft || snapshot}
    <button class="change-source" type="button" disabled={readOnly || busy || !onChangeSource} onclick={() => onChangeSource?.()}>Create another recap</button>
  {/if}

  {#if !draft && !snapshot}
    <div class="source-grid">
      <section class="source-card">
        <h3><BookOpen size={14} /> Existing transcript</h3>
        {#if discovery?.sessions.length}
          <div class="session-list">
            {#each discovery.sessions as option}
              {#if option.status === "selectable"}
                <button type="button" disabled={readOnly || busy || !onPrepareExisting} onclick={() => onPrepareExisting?.(option.source)}>
                  <span><strong>{option.source.title}</strong><small>{option.source.sessionId}</small></span>
                  Use transcript
                </button>
              {:else}
                <div class="unavailable"><strong>{option.title}</strong><span>{option.reason}</span></div>
              {/if}
            {/each}
          </div>
        {:else}
          <p>No registered transcripts are available.</p>
        {/if}
      </section>

      <form class="source-card" onsubmit={(event) => { event.preventDefault(); if (topic.trim()) onPrepareTopic?.(topic.trim()); }}>
        <h3><MessageSquareText size={14} /> Draft from a topic</h3>
        <textarea bind:value={topic} disabled={readOnly} rows="3" placeholder="The party’s bargain beneath the black bell"></textarea>
        <button type="submit" disabled={readOnly || busy || !topic.trim() || !onPrepareTopic}>Prepare synthetic transcript</button>
      </form>
    </div>
  {/if}

  {#if draft && !snapshot}
    <AudioApprovalPanel
      {draft}
      {busy}
      authority={approvalAuthority}
      onApprove={readOnly ? undefined : onApprove}
      onRequestChanges={readOnly ? undefined : onRequestChanges}
    />
  {/if}

  {#if snapshot && showGenerationCard}
    <AudioGenerationCard
      {snapshot}
      {cancellation}
      destinationApproval={readOnly ? null : destinationApproval}
      {destinationAuthority}
      onApproveDestination={readOnly ? undefined : onApproveDestination}
      onCancel={readOnly ? undefined : onCancel}
      {recoveryAvailable}
      onRecover={readOnly || busy ? undefined : onRecover}
    />
  {/if}

  {#if snapshot}
    {#if playbackUrl}
      <div class="playback"><audio controls src={playbackUrl}></audio></div>
    {:else if snapshot.state === "completed"}
      <button class="retry" type="button" disabled={readOnly || !onRetryPlayback} onclick={() => onRetryPlayback?.()}>Verify and retry playback</button>
    {/if}
    {#if snapshot.receipts.length}
      <ul class="results">
        {#each snapshot.receipts as receipt}
          <li><strong>{receipt.artifact_role === "wav" ? "WAV" : "Synthetic transcript"}</strong><code>{receipt.relative_path}</code></li>
        {/each}
      </ul>
    {/if}
  {/if}

  {#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
  .audio-workspace { display: grid; gap: 10px; margin: 10px 14px; border-top: 1px solid var(--border); padding-top: 12px; color: var(--text-1); }
  header span { display: flex; align-items: center; gap: 5px; color: var(--warning); font-size: 9px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
  h2 { margin: 3px 0 0; font-size: 14px; }
  .source-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
  .source-card { min-width: 0; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2); padding: 10px; }
  h3 { display: flex; align-items: center; gap: 6px; margin: 0 0 8px; font-size: 11px; }
  .session-list { display: grid; gap: 6px; }
  button { border: 1px solid var(--border); border-radius: 7px; background: color-mix(in srgb, var(--warning) 9%, var(--surface-1)); color: var(--text-1); padding: 7px 8px; font: inherit; font-size: 10px; font-weight: 700; cursor: pointer; }
  button:disabled { cursor: not-allowed; opacity: .45; }
  .session-list button { display: flex; justify-content: space-between; gap: 8px; text-align: left; }
  .session-list button span { display: grid; gap: 2px; }
  small, .source-card p, .unavailable span { color: var(--text-3); font-size: 9px; }
  .unavailable { display: grid; gap: 2px; border-radius: 7px; padding: 7px; background: color-mix(in srgb, var(--error) 7%, transparent); font-size: 10px; }
  textarea { box-sizing: border-box; width: 100%; resize: vertical; border: 1px solid var(--border); border-radius: 7px; background: var(--surface-1); color: var(--text-1); padding: 7px; font: inherit; font-size: 11px; }
  form button { margin-top: 6px; }
  .playback audio { width: 100%; height: 34px; }
  .retry { justify-self: start; }
  .change-source { justify-self: end; }
  .historical { margin: 0; color: var(--text-3); font-size: 10px; }
  .results { display: grid; gap: 5px; margin: 0; padding: 0; list-style: none; }
  .results li { display: flex; gap: 8px; justify-content: space-between; font-size: 10px; }
  .results code { overflow-wrap: anywhere; color: var(--text-3); }
  .error { margin: 0; color: var(--error); font-size: 10px; }
  @media (max-width: 760px) { .source-grid { grid-template-columns: 1fr; } }
</style>
