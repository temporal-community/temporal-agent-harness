<script lang="ts">
  import { onMount, untrack } from "svelte";
  import {
    ArrowRight,
    Check,
    CheckCheck,
    Circle,
    Download,
    FileCode2,
    MessageSquare,
    Plus,
    RefreshCw,
    X,
  } from "@lucide/svelte";
  import {
    api,
    type ReviewSummary,
    type ReviewDetail,
    type ReviewComment,
  } from "./types";

  let {
    taskId,
    visible,
    initialPath = "",
    requestKey = 0,
    onrequest,
  }: {
    taskId: string;
    visible: boolean;
    initialPath?: string;
    requestKey?: number;
    onrequest: (prompt: string) => void;
  } = $props();
  type Draft = {
    id: string;
    path: string;
    token: string;
    comparison: string;
    side: string;
    line: number;
    text: string;
  };
  let data = $state<ReviewSummary | null>(null);
  let file = $state<ReviewDetail | null>(null);
  let selected = $state("");
  let comparison = $state<"all" | "reviewed">("all");
  let commentsView = $state(false);
  let busy = $state(false);
  let loading = $state(false);
  let error = $state("");
  let fileError = $state(false);
  let draft = $state<Draft | null>(null);
  let ready = $state(false);
  let disposed = false;
  let selection = 0;
  let handledRequest = -1;
  let include = $state<Record<string, boolean>>({});
  let commentInput = $state<HTMLTextAreaElement>();
  let files = $derived(
    data?.files.filter(
      (f) => comparison === "all" || f.since_review || !f.has_checkpoint,
    ) ?? [],
  );
  let current = $derived(data?.files.find((f) => f.path === selected));
  let outdated = $derived(
    Boolean(
      file &&
      data &&
      (current ? current.token !== file.source_token : file.changed),
    ),
  );
  let selectedComments = $derived(
    data?.comments.filter((c) => !c.resolved && include[c.id] !== false) ?? [],
  );
  let shownComments = $derived(
    data?.comments.filter((c) => commentsView || c.path === selected) ?? [],
  );
  let reviewed = $derived(data?.files.filter((f) => f.reviewed).length ?? 0);
  let url = $derived("/tasks/" + taskId + "/review");

  $effect(() => {
    if (ready) {
      try {
        if (draft)
          localStorage.setItem(
            "sdlc.review-draft." + taskId,
            JSON.stringify(draft),
          );
        else localStorage.removeItem("sdlc.review-draft." + taskId);
      } catch {
        /* The draft remains available in memory. */
      }
    }
  });
  $effect(() => {
    if (visible && initialPath && requestKey !== handledRequest) {
      handledRequest = requestKey;
      const path = initialPath;
      untrack(() => {
        selected = path;
        commentsView = false;
        void refresh(true);
      });
    }
  });
  async function loadFile(path: string) {
    const ticket = ++selection;
    selected = path;
    file = null;
    fileError = false;
    if (!path) return;
    try {
      const next = await api<ReviewDetail>(
        url +
          "/file?path=" +
          encodeURIComponent(path) +
          "&comparison=" +
          comparison,
      );
      if (!disposed && ticket === selection) file = next;
    } catch (e) {
      if (!disposed && ticket === selection) {
        fileError = true;
        error = (e as Error).message;
      }
    }
  }
  async function refresh(reload = false) {
    if (loading || disposed) return;
    loading = true;
    try {
      const next = await api<ReviewSummary>(url);
      if (disposed) return;
      data = next;
      if (draft && next.comments.some((c) => c.id === draft?.id)) draft = null;
      if (!selected) {
        await loadFile(files[0]?.path ?? "");
      } else if (reload || (!file && !fileError)) await loadFile(selected);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }
  async function act(fn: () => Promise<void>) {
    if (busy) return;
    busy = true;
    error = "";
    try {
      await fn();
      await refresh();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
  function startComment(side = "file", line = 0) {
    if (!file || file.issue || outdated) return;
    if (
      draft?.text.trim() &&
      !confirm("Discard your unsaved review comment and start a new one?")
    )
      return;
    draft = {
      id: crypto.randomUUID().replaceAll("-", ""),
      path: file.path,
      token: file.token,
      comparison,
      side,
      line,
      text: "",
    };
    setTimeout(() => {
      commentInput?.focus({ preventScroll: true });
      commentInput?.scrollIntoView({ block: "nearest" });
    }, 0);
  }
  async function saveComment() {
    if (!draft?.text.trim()) return;
    const submitted = draft;
    await act(async () => {
      await api(url + "/comments", "POST", submitted);
      if (draft?.id === submitted.id) draft = null;
    });
  }
  async function markReviewed() {
    if (!file) return;
    const submitted = file;
    await act(async () => {
      await api(url + "/checkpoint", "PUT", {
        path: submitted.path,
        token: submitted.source_token,
        reviewed: !current?.reviewed,
      });
      await loadFile(submitted.path);
    });
  }
  async function resolve(comment: ReviewComment) {
    await act(async () => {
      await api(url + "/comments/" + comment.id, "PUT", {
        resolved: !comment.resolved,
      });
    });
  }
  async function requestFixes() {
    await act(async () => {
      const result = await api<{ prompt: string }>(
        url + "/fix-request",
        "POST",
        {
          version: data!.version,
          comment_ids: selectedComments.map((c) => c.id),
        },
      );
      onrequest(result.prompt);
    });
  }
  async function compare(value: "all" | "reviewed") {
    comparison = value;
    await loadFile(
      files.some((f) => f.path === selected)
        ? selected
        : (files[0]?.path ?? ""),
    );
  }
  onMount(() => {
    try {
      draft = JSON.parse(
        localStorage.getItem("sdlc.review-draft." + taskId) ?? "null",
      );
    } catch {
      draft = null;
    }
    ready = true;
    if (visible) void refresh(true);
    const timer = setInterval(() => {
      if (visible && !busy) void refresh();
    }, 5000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  });
  $effect(() => {
    if (visible && ready)
      untrack(() => {
        void refresh();
      });
  });
</script>

<section class="review" aria-label="Interactive review">
  <div class="review-toolbar">
    <div>
      <span class="eyebrow">YOUR REVIEW</span>
      <h2>
        {reviewed}<span> / {data?.files.length ?? "–"} files reviewed</span>
      </h2>
    </div>
    <div class="toolbar-actions">
      <button
        disabled={loading || busy}
        onclick={() => {
          error = "";
          void refresh(true);
        }}
        aria-label="Refresh review"><RefreshCw size={14} /></button
      ><a class="button" href={"/api/tasks/" + taskId + "/patch"} download
        ><Download size={14} />Patch</a
      >
    </div>
  </div>
  {#if error}<div class="review-message error" role="alert">
      {error}<button
        aria-label="Dismiss review error"
        onclick={() => (error = "")}><X size={14} /></button
      >
    </div>{/if}
  {#if data?.omitted}<p class="review-message">
      Showing the first 500 files; {data.omitted} more need local review.
    </p>{/if}
  <div class="comparison-bar">
    <div class="segmented">
      <button aria-pressed={comparison === "all"} onclick={() => compare("all")}
        >All changes</button
      ><button
        aria-pressed={comparison === "reviewed"}
        onclick={() => compare("reviewed")}>Since last review</button
      >
    </div>
    <small
      >{comparison === "all"
        ? `Against task base ${data?.base_commit.slice(0, 8) ?? "…"}`
        : "Each file uses its last reviewed contents; unreviewed files use the task base."}</small
    >
  </div>
  <div class="review-grid">
    <nav class="review-files" aria-label="Review files">
      <button
        class="comments-link"
        class:selected={commentsView}
        onclick={() => (commentsView = true)}
        ><MessageSquare size={13} />All comments
        <span>{data?.comments.filter((c) => !c.resolved).length ?? 0}</span
        ></button
      >
      {#each files as item}<button
          class:selected={selected === item.path && !commentsView}
          onclick={() => {
            commentsView = false;
            error = "";
            void loadFile(item.path);
          }}
        >
          <span class="file-icon" class:reviewed={item.reviewed}
            >{#if item.reviewed}<Check size={14} />{:else}<FileCode2
                size={14}
              />{/if}</span
          ><span class="file-name"
            >{item.path}<small
              >{item.issue
                ? "Needs local inspection"
                : item.since_review
                  ? "Changed since review"
                  : item.reviewed
                    ? "Reviewed"
                    : item.status}</small
            ></span
          ><span class="file-count"
            ><b
              >+{comparison === "reviewed"
                ? (item.review_additions ?? item.additions)
                : item.additions}</b
            ><i
              >−{comparison === "reviewed"
                ? (item.review_deletions ?? item.deletions)
                : item.deletions}</i
            ></span
          >
        </button>{/each}
      {#if !files.length && data}<p>
          {comparison === "reviewed"
            ? "No new changes since your review."
            : "No workspace changes yet."}
        </p>{/if}
    </nav>
    <div class="review-content">
      {#if !commentsView}
        {#if file}
          <div class="file-heading">
            <div>
              <code>{file.path}</code><small>{file.baseline} → workspace</small>
            </div>
            <button
              class:marked={current?.reviewed}
              disabled={busy || outdated || !!file.issue || file.truncated}
              onclick={markReviewed}
              >{#if current?.reviewed}<CheckCheck
                  size={14}
                />Reviewed{:else}<Circle size={14} />Mark reviewed{/if}</button
            >
          </div>
          {#if outdated}<div class="review-message changed" role="status">
              This file changed. Your diff stays at the version you opened.<button
                disabled={busy}
                onclick={() => loadFile(selected)}>Load latest diff</button
              >
            </div>{/if}
          {#if file.issue}<p class="review-message">{file.issue}</p>{:else}
            <div class="diff-instruction">
              <span>Click a line to leave a comment.</span><button
                disabled={outdated || busy}
                onclick={() => startComment()}
                ><Plus size={12} />File comment</button
              >
            </div>
            {#if file.old_mode !== file.new_mode}<p class="diff-meta">
                Mode {file.old_mode || "absent"} → {file.new_mode || "absent"}
              </p>{/if}
            <div class="review-diff" aria-label="File diff">
              {#each file.lines as line}
                {#if line.kind === "hunk"}<div class="diff-hunk">
                    <code>{line.text}</code>
                  </div>
                {:else}<button
                    class="diff-line {line.kind}"
                    disabled={outdated || busy}
                    aria-label={`Comment on ${line.new ? "new" : "old"} line ${line.new || line.old}`}
                    onclick={() =>
                      startComment(
                        line.new ? "new" : "old",
                        line.new || line.old,
                      )}
                    ><span>{line.old || ""}</span><span>{line.new || ""}</span
                    ><b
                      >{line.kind === "addition"
                        ? "+"
                        : line.kind === "deletion"
                          ? "−"
                          : " "}</b
                    ><code>{line.text || " "}</code></button
                  >{/if}
              {/each}
            </div>
            {#if !file.lines.length}<div class="empty-diff">
                <Check size={18} />
                <p>
                  {file.changed
                    ? "Only file metadata, existence, or line endings changed."
                    : "No content changes against this version."}
                </p>
              </div>{/if}
            {#if file.old_final_newline !== file.new_final_newline}<p
                class="diff-meta"
              >
                Final newline: {file.old_final_newline ? "present" : "absent"} → {file.new_final_newline
                  ? "present"
                  : "absent"}
              </p>{/if}
            {#if file.truncated}<p class="review-message">
                Diff exceeds 6,000 displayed lines. Inspect the complete change
                locally before accepting.
              </p>{/if}
          {/if}
        {:else}<div class="empty-diff">
            <FileCode2 size={22} />
            <p>
              {fileError
                ? "This file needs local inspection. Choose another file or retry after correcting the issue."
                : loading || selected
                  ? "Loading your diff…"
                  : comparison === "reviewed"
                    ? "Reviewed files are unchanged. Choose All changes to revisit them."
                    : "Select a file to review its changes."}
            </p>
          </div>{/if}
      {:else}<div class="file-heading">
          <h3>Review comments</h3>
          <span>{data?.comments.length ?? 0} total</span>
        </div>{/if}
      {#if draft}<form
          class="comment-draft"
          onsubmit={(event) => {
            event.preventDefault();
            void saveComment();
          }}
        >
          <div>
            <strong
              >Comment on <code>{draft.path}</code>{draft.line
                ? ` · ${draft.side} line ${draft.line}`
                : ""}</strong
            ><button
              type="button"
              aria-label="Discard comment draft"
              disabled={busy}
              onclick={() => {
                if (
                  !draft?.text.trim() ||
                  confirm("Discard this unsaved comment?")
                )
                  draft = null;
              }}><X size={13} /></button
            >
          </div>
          <textarea
            aria-label="Review comment"
            bind:this={commentInput}
            bind:value={draft.text}
            disabled={busy}
            rows="3"
            maxlength="2000"
            placeholder="What needs to change, and why? ⌘ / Ctrl + Enter to save."
            onkeydown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void saveComment();
              }
            }}></textarea>
          <div>
            <small>Saved comments do not start the agent.</small><button
              type="submit"
              class="primary"
              disabled={busy || !draft.text.trim()}>Save comment</button
            >
          </div>
        </form>{/if}
      <div class="review-comments">
        {#each shownComments as comment}<article
            class:resolved={comment.resolved}
          >
            <div class="comment-heading">
              <label
                ><input
                  type="checkbox"
                  aria-label={"Include comment: " + comment.text}
                  checked={!comment.resolved && include[comment.id] !== false}
                  disabled={comment.resolved}
                  onchange={(event) =>
                    (include[comment.id] = event.currentTarget.checked)}
                /><span
                  ><code>{comment.path}</code><small
                    >{comment.side === "file"
                      ? "File comment"
                      : `${comment.side} line ${comment.line}`}{comment.outdated
                      ? " · Earlier file version"
                      : ""}</small
                  ></span
                ></label
              ><button disabled={busy} onclick={() => resolve(comment)}
                >{comment.resolved ? "Reopen" : "Resolve"}</button
              >
            </div>
            {#if comment.excerpt}<pre>{comment.excerpt}</pre>{/if}
            <p>{comment.text}</p>
            <small class="comment-date"
              >{new Date(
                comment.created_at * 1000,
              ).toLocaleString()}{comment.resolved
                ? " · Resolved by you"
                : ""}</small
            >
          </article>{/each}
        {#if commentsView && !shownComments.length}<div class="empty-diff">
            <MessageSquare size={20} />
            <p>Open a file, then click a line or add a file comment.</p>
          </div>{/if}
      </div>
    </div>
  </div>
  <div class="review-footer">
    <div>
      <strong
        >{selectedComments.length} open {selectedComments.length === 1
          ? "comment"
          : "comments"} selected</strong
      >
      <p>
        Request fixes prepares a follow-up. Review it and send; comments stay
        open until you resolve them.
      </p>
    </div>
    <button
      class="primary"
      disabled={busy || !selectedComments.length}
      onclick={requestFixes}>Request fixes <ArrowRight size={14} /></button
    >
  </div>
</section>

<style>
  .review {
    background: var(--surface-0);
    container-type: inline-size;
  }
  .review-toolbar,
  .toolbar-actions,
  .comparison-bar,
  .file-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .review-toolbar {
    padding: 20px;
  }
  h2 {
    margin-top: 7px;
    font: 20px var(--font-mono);
  }
  h2 span {
    color: var(--text-3);
    font-size: 12px;
  }
  .comparison-bar {
    border-block: 1px solid var(--border);
    padding: 12px 20px;
    flex-wrap: wrap;
  }
  .comparison-bar small {
    color: var(--text-3);
    font-size: 10px;
    max-width: 350px;
    line-height: 1.5;
  }
  .segmented {
    display: flex;
    border: 1px solid var(--border);
    padding: 2px;
    border-radius: 5px;
  }
  .segmented button {
    border: 0;
    background: transparent;
    min-height: 28px;
    font-size: 10px;
    border-radius: 3px;
  }
  .segmented button[aria-pressed="true"] {
    color: var(--accent-soft);
    background: var(--accent-bg);
  }
  .review-grid {
    display: grid;
    grid-template-columns: 225px minmax(0, 1fr);
    min-height: 400px;
  }
  .review-files {
    min-width: 0;
    border-right: 1px solid var(--border);
    padding: 10px 0;
    background: var(--surface-spine);
  }
  .review-files button {
    display: flex;
    width: 100%;
    border: 0;
    border-left: 2px solid transparent;
    border-radius: 0;
    padding: 13px 12px;
    background: transparent;
    justify-content: start;
    gap: 8px;
    text-align: left;
  }
  .review-files button.selected {
    border-left-color: var(--accent);
    background: var(--accent-bg);
  }
  .file-icon {
    color: var(--text-3);
  }
  .file-icon.reviewed {
    color: var(--success);
  }
  .file-name {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
    font: 11px var(--font-mono);
    line-height: 1.6;
  }
  .file-name small {
    display: block;
    font: 9px var(--font-sans);
    color: var(--text-3);
    margin-top: 5px;
  }
  .file-count {
    font: 9px var(--font-mono);
    display: grid;
    gap: 4px;
  }
  .file-count b {
    color: var(--success);
    font-weight: 400;
  }
  .file-count i {
    color: var(--error);
    font-style: normal;
  }
  .review-files .comments-link {
    color: var(--text-2);
    font-size: 11px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 6px;
  }
  .comments-link span {
    margin-left: auto;
    color: var(--accent-soft);
    font: 10px var(--font-mono);
  }
  .review-files > p {
    font-size: 12px;
    color: var(--text-3);
    padding: 16px;
  }
  .review-content {
    min-width: 0;
  }
  .file-heading {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .file-heading > div {
    min-width: 0;
  }
  .file-heading code {
    overflow-wrap: anywhere;
  }
  .file-heading small {
    display: block;
    margin-top: 6px;
    font-size: 10px;
    color: var(--text-3);
  }
  .file-heading button {
    font-size: 10px;
  }
  .file-heading button.marked {
    color: var(--success);
    border-color: var(--success-border);
    background: var(--success-bg);
  }
  .file-heading > span {
    color: var(--text-3);
    font-size: 11px;
  }
  .diff-instruction {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 20px;
    gap: 8px;
    color: var(--text-3);
    font-size: 10px;
  }
  .diff-instruction button {
    font-size: 10px;
    border: 0;
    background: transparent;
    padding: 4px;
  }
  .review-diff {
    overflow: auto;
    max-height: 620px;
    padding-bottom: 6px;
  }
  .diff-hunk {
    background: var(--accent-bg);
    color: var(--accent-soft);
    padding: 9px 14px;
    min-width: max-content;
  }
  .diff-line {
    display: grid;
    grid-template-columns: 36px 36px 22px minmax(0, 1fr);
    width: max-content;
    min-width: 100%;
    min-height: 23px;
    padding: 0 12px 0 0;
    gap: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    text-align: left;
    font-weight: 400;
  }
  .diff-line > span {
    color: var(--text-3);
    font: 10px/23px var(--font-mono);
    text-align: right;
    padding-right: 8px;
    user-select: none;
  }
  .diff-line b {
    font: 11px/23px var(--font-mono);
    text-align: center;
  }
  .diff-line code {
    white-space: pre;
    line-height: 23px;
  }
  .diff-line.addition {
    background: var(--success-bg);
  }
  .diff-line.deletion {
    background: var(--error-bg);
  }
  .diff-line.addition b {
    color: var(--success);
  }
  .diff-line.deletion b {
    color: var(--error);
  }
  .diff-line:hover {
    background: var(--accent-bg);
    box-shadow: inset 2px 0 var(--accent);
  }
  .diff-line:disabled {
    opacity: 0.7;
  }
  .diff-meta {
    padding: 8px 20px;
    font: 10px var(--font-mono);
    color: var(--warning);
  }
  .empty-diff {
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: center;
    padding: 50px 24px;
    color: var(--text-3);
    font-size: 12px;
  }
  .empty-diff :global(svg) {
    flex-shrink: 0;
  }
  .review-message {
    margin: 12px 16px;
    padding: 12px;
    font-size: 12px;
    background: var(--warning-bg);
    color: var(--warning);
    border: 1px solid var(--warning-border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
  }
  .review-message.error {
    color: var(--error);
    background: var(--error-bg);
    border-color: var(--error-border);
  }
  .comment-draft {
    margin: 20px;
    padding: 16px;
    background: var(--accent-bg);
    border: 1px solid var(--accent-border);
    border-radius: 5px;
  }
  .comment-draft > div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: center;
  }
  .comment-draft strong {
    font-size: 11px;
    line-height: 1.6;
    overflow-wrap: anywhere;
  }
  .comment-draft textarea {
    margin: 12px 0;
    font-size: 12px;
  }
  .comment-draft small {
    font-size: 10px;
    color: var(--text-3);
  }
  .review-comments {
    padding: 0 20px;
  }
  .review-comments article {
    border-top: 1px solid var(--border);
    padding: 20px 0;
  }
  .review-comments article.resolved {
    opacity: 0.65;
  }
  .comment-heading {
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 10px;
  }
  .comment-heading label {
    display: flex;
    gap: 10px;
    align-items: start;
    min-width: 0;
  }
  .comment-heading input {
    width: 14px;
    height: 14px;
    margin: 0;
  }
  .comment-heading code {
    overflow-wrap: anywhere;
  }
  .comment-heading small {
    display: block;
    color: var(--text-3);
    font-size: 10px;
    margin-top: 5px;
  }
  .comment-heading button {
    font-size: 10px;
    min-height: 26px;
    padding: 3px 8px;
  }
  article pre {
    background: var(--surface-spine);
    border-left: 2px solid var(--border-strong);
    padding: 8px 12px;
  }
  article p {
    font-size: 12px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin-top: 12px;
  }
  .comment-date {
    display: block;
    margin-top: 10px;
    color: var(--text-3);
    font: 9px var(--font-mono);
  }
  .review-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    padding: 18px 20px;
    border-top: 1px solid var(--border);
    background: var(--surface-spine);
  }
  .review-footer strong {
    font-size: 12px;
    font-weight: 500;
  }
  .review-footer p {
    font-size: 10px;
    color: var(--text-3);
    margin-top: 4px;
    max-width: 450px;
  }
  .review-footer button {
    flex-shrink: 0;
  }
  @container (max-width:620px) {
    .review-grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .review-files {
      display: flex;
      overflow: auto;
      border-right: 0;
      border-bottom: 1px solid var(--border);
      padding: 0;
    }
    .review-files button {
      min-width: 180px;
      width: 180px;
    }
    .review-files .comments-link {
      margin: 0;
      border-bottom: 0;
      min-width: 155px;
    }
    .review-footer {
      flex-wrap: wrap;
    }
    .comment-heading {
      flex-wrap: wrap;
    }
    .comment-draft {
      margin: 14px;
      padding: 12px;
    }
    .review-toolbar {
      padding: 16px;
    }
  }
</style>
