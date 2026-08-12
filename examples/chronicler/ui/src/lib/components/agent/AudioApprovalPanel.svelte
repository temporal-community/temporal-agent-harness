<script lang="ts">
  import { Check, FileAudio, FileText, Mic2, Pencil, Sparkles } from "@lucide/svelte";
  import type { AudioDraft } from "./audioUi";

  interface Props {
    draft: AudioDraft;
    busy?: boolean;
    authority?: { ready: boolean; detail: string };
    onApprove?: (draft: AudioDraft) => void | Promise<void>;
    onRequestChanges?: (draft: AudioDraft, changeRequest: string) => void | Promise<void>;
  }

  let {
    draft,
    busy = false,
    authority = { ready: false, detail: "Checking audio approval authority…" },
    onApprove,
    onRequestChanges
  }: Props = $props();
  let changeRequest = $state("");
</script>

<section class="audio-approval" aria-label="Review audio package">
  <header>
    <span class="eyebrow"><Sparkles size={13} /> Audio recap</span>
    <h3>Review audio package</h3>
    <p>Nothing is generated until you approve this exact package.</p>
  </header>

  <div class="review-grid">
    <article>
      <span class="label"><FileText size={13} /> Transcript</span>
      <pre>{draft.source_content}</pre>
    </article>
    <article>
      <span class="label"><Mic2 size={13} /> Exact narration</span>
      <p class="script">{draft.recap_script}</p>
    </article>
  </div>

  <dl>
    <div><dt>Voice</dt><dd>{draft.voice}</dd></div>
    <div><dt><FileAudio size={13} /> WAV</dt><dd>{draft.wav_path}</dd></div>
    {#if draft.synthetic_markdown_path}
      <div><dt><FileText size={13} /> Synthetic transcript</dt><dd>{draft.synthetic_markdown_path}</dd></div>
    {/if}
  </dl>

  <footer>
    {#if onRequestChanges}
      <label class="change-request">
        <span>What should change?</span>
        <textarea bind:value={changeRequest} rows="2" placeholder="Make the narration more ominous."></textarea>
        <button type="button" class="secondary" disabled={busy || !changeRequest.trim()} onclick={() => onRequestChanges?.(draft, changeRequest.trim())}>
          <Pencil size={13} /> Reprepare review
        </button>
      </label>
    {/if}
    <div class="approval-action">
      <small>{authority.detail}</small>
      <button type="button" class="approve" disabled={busy || !authority.ready || !onApprove} onclick={() => onApprove?.(draft)}>
      <Check size={14} /> {busy ? "Starting…" : "Approve and generate"}
      </button>
    </div>
  </footer>
</section>

<style>
  .audio-approval {
    border: 1px solid color-mix(in srgb, var(--warning) 35%, var(--border));
    border-radius: 12px;
    background: color-mix(in srgb, var(--surface-2) 92%, var(--warning));
    padding: 14px;
    color: var(--text-1);
  }
  header h3 { margin: 4px 0; font-size: 15px; }
  header p { margin: 0; color: var(--text-3); font-size: 12px; }
  .eyebrow, .label, dt { display: flex; align-items: center; gap: 6px; color: var(--warning); font-size: 10px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
  .review-grid { display: grid; gap: 10px; margin-top: 12px; }
  article { min-width: 0; border: 1px solid var(--border); border-radius: 9px; background: color-mix(in srgb, var(--surface-1) 88%, transparent); padding: 10px; }
  pre, .script { margin: 8px 0 0; white-space: pre-wrap; font: inherit; font-size: 12px; line-height: 1.55; color: var(--text-1); }
  pre { max-height: 150px; overflow: auto; }
  dl { display: grid; gap: 7px; margin: 12px 0 0; }
  dl div { display: grid; grid-template-columns: 128px minmax(0, 1fr); gap: 8px; align-items: center; }
  dt { color: var(--text-3); }
  dd { margin: 0; overflow-wrap: anywhere; font: 11px/1.4 ui-monospace, SFMono-Regular, monospace; }
  footer { display: flex; justify-content: flex-end; align-items: end; gap: 8px; margin-top: 14px; }
  .change-request { display: grid; gap: 4px; margin-right: auto; flex: 1; }
  .change-request > span { color: var(--text-3); font-size: 9px; font-weight: 700; text-transform: uppercase; }
  textarea { box-sizing: border-box; width: 100%; resize: vertical; border: 1px solid var(--border); border-radius: 7px; background: var(--surface-1); color: var(--text-1); padding: 6px; font: inherit; font-size: 10px; }
  .change-request button { justify-self: start; }
  .approval-action { display: grid; justify-items: end; gap: 4px; }
  .approval-action small { color: var(--text-3); font-size: 9px; }
  button { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px; font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; }
  button:disabled { cursor: not-allowed; opacity: .5; }
  .secondary { background: transparent; color: var(--text-3); }
  .approve { border-color: color-mix(in srgb, var(--warning) 55%, var(--border)); background: color-mix(in srgb, var(--warning) 18%, var(--surface-1)); color: var(--text-1); }
</style>
