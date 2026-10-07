<!-- The flow script, read-only: the agent is the only one who writes it. Lines it just touched
     glow for a moment, and a cursor marks where it is working. -->
<script module lang="ts">
  export interface Flash {
    id: number;
    from: number;
    to: number;
  }

  interface Token {
    cls: string;
    text: string;
  }

  const KEYWORDS = new Set(
    "and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield".split(" ")
  );
  const CONSTANTS = new Set(["None", "True", "False"]);
  const BUILTINS = new Set(
    "print len range min max sorted sum dict list str int float bool enumerate zip any all round abs set tuple isinstance".split(" ")
  );
  const HOST = new Set(["run_agent"]);

  const TOKEN =
    /(#[^\n]*)|([rRbBfFuU]{0,2}(?:"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?))|(\b\d[\d_]*(?:\.\d+)?\b)|([A-Za-z_]\w*)|(\s+)|([^\sA-Za-z_\d])/g;

  /** Python source as lines of classed tokens; a token spanning lines is split across them. */
  export function highlight(source: string): Token[][] {
    const lines: Token[][] = [[]];
    let previous = "";
    const push = (cls: string, text: string) => {
      text.split("\n").forEach((piece, i) => {
        if (i > 0) lines.push([]);
        if (piece) lines[lines.length - 1]!.push({ cls, text: piece });
      });
    };
    for (const m of source.matchAll(TOKEN)) {
      const [text, comment, string, number, name, space] = m;
      let cls = "op";
      if (comment) cls = "comment";
      else if (string) cls = string.includes('"""') || string.includes("'''") ? "string doc" : "string";
      else if (number) cls = "number";
      else if (space) cls = "";
      else if (name) {
        const next = source.slice((m.index ?? 0) + text.length).match(/^\s*(\S)/)?.[1];
        if (KEYWORDS.has(name)) cls = "keyword";
        else if (CONSTANTS.has(name)) cls = "const";
        else if (HOST.has(name)) cls = "host";
        else if (previous === "def") cls = "fn def";
        else if (BUILTINS.has(name) && next === "(") cls = "builtin";
        else if (next === "(") cls = "fn";
        else if (/^[A-Z][A-Z0-9_]+$/.test(name)) cls = "const";
        else cls = "name";
      }
      if (!space) previous = text;
      push(cls, text);
    }
    return lines;
  }

  /** The 1-based lines of `next` that are not in `previous`, as ranges. */
  export function changedRanges(previous: string, next: string): Array<[number, number]> {
    const before = new Set(previous.split("\n"));
    const ranges: Array<[number, number]> = [];
    next.split("\n").forEach((line, i) => {
      if (before.has(line) || !line.trim()) return;
      const last = ranges[ranges.length - 1];
      if (last && last[1] >= i) last[1] = i + 1;
      else ranges.push([i + 1, i + 1]);
    });
    return ranges;
  }
</script>

<script lang="ts">
  import { tick } from "svelte";

  interface Props {
    code: string;
    flashes: readonly Flash[];
    /** The line the agent is working on, while it edits. */
    cursor: number | null;
    /** The agent is typing out a whole new script. */
    typing: boolean;
    editing: boolean;
  }
  let { code, flashes, cursor, typing, editing }: Props = $props();

  let scroller: HTMLElement | undefined = $state();

  const lines = $derived(highlight(code));
  const lineCount = $derived(code ? code.split("\n").length : 0);

  function flashAt(line: number): Flash | undefined {
    for (let i = flashes.length - 1; i >= 0; i--) {
      const flash = flashes[i]!;
      if (line >= flash.from && line <= flash.to) return flash;
    }
    return undefined;
  }

  // Keep the agent's cursor in view as it moves.
  $effect(() => {
    const line = cursor;
    if (line === null || !scroller) return;
    void tick().then(() => {
      scroller?.querySelector(`[data-line="${line}"]`)?.scrollIntoView({ block: "nearest", behavior: typing ? "auto" : "smooth" });
    });
  });
</script>

<section class="editor">
  <header>
    <div class="tab">
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5c-3 0-2.8 1.3-2.8 1.3v1.4H8v.4H3.9S2 4.4 2 7.5s1.7 3 1.7 3h1V9s-.1-1.7 1.7-1.7h2.8s1.6 0 1.6-1.6V3.1S11.1 1.5 8 1.5zm-1.6.9a.5.5 0 1 1 0 1 .5.5 0 0 1 0-1z" fill="currentColor" opacity=".9"/><path d="M8 14.5c3 0 2.8-1.3 2.8-1.3v-1.4H8v-.4h4.1s1.9.2 1.9-2.9-1.7-3-1.7-3h-1V7s.1 1.7-1.7 1.7H6.8s-1.6 0-1.6 1.6v2.6S4.9 14.5 8 14.5zm1.6-.9a.5.5 0 1 1 0-1 .5.5 0 0 1 0 1z" fill="currentColor" opacity=".55"/></svg>
      flow.py
    </div>
    <div class="meta">
      {#if editing}
        <span class="live"><i></i>agent is editing</span>
      {:else}
        <span class="lock" title="The agent writes this file; describe changes in the chat.">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 7V5a3 3 0 0 1 6 0v2h.5A1.5 1.5 0 0 1 13 8.5v4a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 3 12.5v-4A1.5 1.5 0 0 1 4.5 7H5zm1.5 0h3V5a1.5 1.5 0 0 0-3 0v2z" fill="currentColor"/></svg>
          read-only
        </span>
      {/if}
      <span>{lineCount} lines</span>
      <span>Python · Monty sandbox</span>
    </div>
  </header>

  <div class="scroller" bind:this={scroller}>
    {#if !code}
      <div class="empty">
        <div class="glyph">
          <svg viewBox="0 0 64 64" aria-hidden="true">
            <circle cx="12" cy="20" r="6" /><circle cx="12" cy="44" r="6" /><circle cx="36" cy="32" r="6" /><circle cx="56" cy="32" r="5" />
            <path d="M18 20c8 0 10 12 12 12M18 44c8 0 10-12 12-12M42 32h9" fill="none" />
          </svg>
        </div>
        <h2>No flow yet</h2>
        <p>Describe a job in the chat. The agent writes a Python flow here that wires fresh agents into a DAG — you never have to type a line.</p>
      </div>
    {:else}
      <pre class="code"><code>{#each lines as tokens, i (`${i}:${flashAt(i + 1)?.id ?? ""}`)}{@const n = i + 1}<div class="line" class:flash={flashAt(n) !== undefined} class:cursor={cursor === n} data-line={n}><span class="gutter">{n}</span><span class="text">{#each tokens as token, t (t)}<span class={token.cls}>{token.text}</span>{/each}{#if cursor === n}<span class="caret"></span>{/if}</span></div>{/each}</code></pre>
    {/if}
  </div>
</section>

<style>
  .editor {
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    overflow: hidden;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    height: 38px;
    padding: 0 12px 0 0;
    background: var(--panel-2);
    border-bottom: 1px solid var(--line);
    flex: none;
  }
  .tab {
    display: flex;
    align-items: center;
    gap: 7px;
    height: 100%;
    padding: 0 14px;
    font: 500 12.5px var(--mono);
    background: var(--panel);
    border-right: 1px solid var(--line);
    box-shadow: inset 0 2px 0 var(--accent);
  }
  .tab svg { width: 14px; height: 14px; color: var(--syn-host); }
  .meta { display: flex; align-items: center; gap: 14px; color: var(--faint); font-size: 11.5px; white-space: nowrap; overflow: hidden; }
  .lock, .live { display: inline-flex; align-items: center; gap: 5px; }
  .lock svg { width: 11px; height: 11px; }
  .live { color: var(--accent); font-weight: 600; }
  .live i { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); animation: pulse 1s infinite; }

  .scroller { flex: 1; overflow: auto; min-height: 0; }
  .code {
    margin: 0;
    padding: 10px 0 40px;
    font: 12.8px/1.62 var(--mono);
    tab-size: 4;
    min-width: max-content;
  }
  .line { display: flex; padding-right: 24px; position: relative; }
  .line.cursor { background: var(--accent-soft); }
  .gutter {
    flex: none;
    width: 52px;
    padding-right: 16px;
    text-align: right;
    color: var(--faint);
    user-select: none;
  }
  .line.cursor .gutter { color: var(--accent); }
  .text { white-space: pre; }
  .line.flash::before {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, color-mix(in srgb, var(--good) 28%, transparent), color-mix(in srgb, var(--good) 8%, transparent));
    box-shadow: inset 3px 0 0 var(--good);
    animation: glow 2.4s ease-out forwards;
    pointer-events: none;
  }
  @keyframes glow {
    0% { opacity: 1; }
    60% { opacity: 0.55; }
    100% { opacity: 0; }
  }
  .caret {
    display: inline-block;
    width: 2px;
    height: 1.15em;
    margin-left: 2px;
    vertical-align: text-bottom;
    background: var(--accent);
    animation: pulse 0.9s steps(1) infinite;
  }

  .keyword { color: var(--syn-keyword); }
  .string { color: var(--syn-string); }
  .string.doc { color: var(--syn-comment); font-style: italic; }
  .number { color: var(--syn-number); }
  .comment { color: var(--syn-comment); font-style: italic; }
  .fn { color: var(--syn-fn); }
  .fn.def { font-weight: 600; }
  .host { color: var(--syn-host); font-weight: 600; }
  .builtin { color: var(--syn-builtin); }
  .op { color: var(--syn-op); }
  .const { color: var(--syn-const); }
  .name { color: var(--ink); }

  .empty {
    height: 100%;
    display: grid;
    place-content: center;
    justify-items: center;
    text-align: center;
    gap: 6px;
    padding: 32px;
    color: var(--muted);
  }
  .empty h2 { margin: 8px 0 0; font-size: 15px; color: var(--ink); }
  .empty p { margin: 0; max-width: 360px; }
  .glyph svg { width: 84px; height: 84px; }
  .glyph circle { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 1.5; }
  .glyph path { stroke: var(--line-2); stroke-width: 1.5; stroke-dasharray: 3 3; }
</style>
