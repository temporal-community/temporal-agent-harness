<script lang="ts" module>
  import type { CodeBlockNode, InlineNode, InlineParseResult, MarkdownExtension, ParseOptions } from "@tanstack/markdown";
  import { renderHtml } from "@tanstack/markdown/html";
  import { parseMarkdown } from "@tanstack/markdown/parser";
  import remend from "remend";
  import type { Attachment } from "svelte/attachments";
  import type { FileCitationAnnotation } from "$lib/api/types";
  import { escapeHtml, highlight } from "$lib/highlight";
  import { atEnd } from "./JsonReply.svelte";

  export interface CitationLink {
    number: number;
    title: string;
    url: string;
  }

  /** A top-level block as drawn: markup, or a code block the component draws with its own chrome. */
  export type RenderedBlock =
    | { kind: "html"; html: string }
    | { kind: "code"; code: string; language: string; open: boolean };

  /* Only what this renderer draws. `katex` would add `$$` it shows literally; `htmlTags` cuts
     "a <b then" to "a". The finished reply draws `- > 25` as a quote and `a~b~c` struck through, and
     has no setext headings: escaping those, or a zero-width space after `--`, would draw the live
     block differently from the same text once it ends. A half-typed link or image is its text. */
  const REMEND_OPTIONS = {
    comparisonOperators: false,
    htmlTags: false,
    katex: false,
    linkMode: "text-only",
    setextHeadings: false,
    singleTilde: false
  } as const;

  function escapeAttribute(value: string): string {
    return escapeHtml(value).replaceAll("`", "&#96;");
  }

  function citationUrl(citation: FileCitationAnnotation): string {
    return citation.custom_metadata?.deep_url ?? citation.document_uri ?? "#";
  }

  function citationTitle(citation: FileCitationAnnotation): string {
    return (
      citation.custom_metadata?.heading ??
      citation.custom_metadata?.title ??
      citation.file_name ??
      "Source"
    );
  }

  const LOCAL = "http://local.invalid";

  /** The normalized URL a link may follow, unescaped: http(s) or mailto, or a path or fragment
   *  that stays on this origin (`//host`, `/\host` and `/.//host` resolve elsewhere), else `null`. */
  function safeHref(value: string): string | null {
    const href = value.trim();
    if (URL.canParse(href)) {
      const url = new URL(href);
      return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
    }
    if (!/^[/#]/.test(href)) return null;
    const url = new URL(href, LOCAL);
    const local = href.startsWith("#") ? url.hash : url.pathname + url.search + url.hash;
    return url.origin === LOCAL && local && !local.startsWith("//") ? local : null;
  }

  export function citationLinks(sourceCitations: FileCitationAnnotation[]): CitationLink[] {
    return sourceCitations.map((citation, index) => ({
      number: index + 1,
      title: citationTitle(citation),
      url: citationUrl(citation)
    }));
  }

  function markerForCitation(citation: CitationLink, glued: boolean): string {
    const href = escapeAttribute(safeHref(citation.url) ?? "#");
    const title = escapeAttribute(citation.title);
    return `${glued ? "&nbsp;" : ""}<a class="md-citation" href="${href}" target="_blank" rel="noreferrer" aria-label="${title}" data-cite-title="${title}" data-cite-url="${href}">[${citation.number}]</a>`;
  }

  function isWordCharacter(value: string | undefined): boolean {
    return value != null && /^[A-Za-z0-9_]$/.test(value);
  }

  function isSentencePunctuation(value: string | undefined): boolean {
    return value != null && /^[.,;:!?)]$/.test(value);
  }

  function citationInsertionIndex(value: string, index: number): number {
    let next = index;
    const before = value[index - 1];
    const after = value[index];

    if (isWordCharacter(before) && isWordCharacter(after)) {
      while (next < value.length && isWordCharacter(value[next])) next += 1;
      return next;
    }

    while (next < value.length && isSentencePunctuation(value[next])) next += 1;
    return next;
  }

  function hasExplicitCitationMarkers(value: string, links: CitationLink[]): boolean {
    return links.some((citation) => value.includes(`[${citation.number}]`));
  }

  function withCitationMarkers(value: string, sourceCitations: FileCitationAnnotation[]): string {
    const links = citationLinks(sourceCitations);
    if (!links.length || hasExplicitCitationMarkers(value, links)) return value;

    const positioned = sourceCitations
      .map((citation, index) => ({
        index,
        start: citation.start_index,
        end: citation.end_index
      }))
      .filter(
        (citation): citation is { index: number; start: number; end: number } =>
          typeof citation.start === "number" &&
          typeof citation.end === "number" &&
          citation.start >= 0 &&
          citation.end > citation.start &&
          citation.end <= value.length
      );

    if (positioned.some((citation) => citation.start > 0)) {
      const insertions = new Map<number, string[]>();
      for (const citation of positioned) {
        const insertionIndex = citationInsertionIndex(value, citation.end);
        const markers = insertions.get(insertionIndex) ?? [];
        markers.push(`[${citation.index + 1}]`);
        insertions.set(insertionIndex, markers);
      }

      let next = value;
      for (const [index, markers] of [...insertions.entries()].sort((a, b) => b[0] - a[0])) {
        next = `${next.slice(0, index)} ${markers.join(" ")}${next.slice(index)}`;
      }
      return next;
    }

    const suffix = links.map((citation) => `[${citation.number}]`).join(" ");
    return value.trimEnd() ? `${value.trimEnd()} ${suffix}` : suffix;
  }

  /* TanStack's line patterns stop at U+2028 and U+2029 (a quote holding one never ends). Private-use
     stand-ins of the same length parse as text and keep citation offsets; they draw as the originals. */
  const hideSeparators = (text: string) => text.replaceAll("\u2028", "\uE028").replaceAll("\u2029", "\uE029");
  const restoreSeparators = (text: string) => text.replaceAll("\uE028", "\u2028").replaceAll("\uE029", "\u2029");

  const isCitation = (node: InlineNode | undefined) => node?.type === "inlineComponent" && node.name === "citation";
  const isSpace = (node: InlineNode | undefined) => node?.type === "text" && !node.value.trim();

  /** `[n]` naming a known citation is a marker. */
  function citation(source: string, index: number, links: CitationLink[]): InlineParseResult | undefined {
    const match = /^\[(\d+)\]/.exec(source.slice(index, index + 12));
    if (!match?.[1] || !links[Number(match[1]) - 1]) return undefined;
    return { length: match[0].length, node: { type: "inlineComponent", name: "citation", attributes: { n: match[1] }, children: [] } };
  }

  /** A bare or `<angled>` http(s) or mailto URL, less trailing punctuation and an unbalanced `)`. */
  function autolink(source: string, index: number): InlineParseResult | undefined {
    const angled = source[index] === "<";
    if (!angled && /\w/.test(source[index - 1] ?? "")) return undefined;
    const pattern = /(?:https?:\/\/|mailto:)[^\s<>[\]\uE028\uE029]+/iy;
    pattern.lastIndex = index + Number(angled);
    let url = pattern.exec(source)?.[0] ?? "";
    if (angled && source[index + 1 + url.length] !== ">") return undefined;
    while (!angled && (/[.,;:!?'"*~]$/.test(url) || (url.endsWith(")") && url.split(")").length > url.split("(").length))) url = url.slice(0, -1);
    const href = url && safeHref(url);
    if (!href) return undefined;
    return { length: url.length + 2 * Number(angled), node: { type: "link", href, children: [{ type: "text", value: url }] } };
  }

  /** Each line's leading citation run moved after its text, then each marker glued to what comes
   *  before it. New nodes throughout: the parser's own are left as it made them. */
  function placeCitations(nodes: InlineNode[]): InlineNode[] {
    const lines: InlineNode[][] = [[]];
    for (const node of nodes) {
      if (node.type !== "text") lines.at(-1)!.push(node);
      else
        node.value.split("\n").forEach((value, i) => {
          if (i) lines.push([]);
          if (value) lines.at(-1)!.push({ type: "text", value });
        });
    }
    return glue(
      lines.flatMap((line, i): InlineNode[] => {
        let lead = 0;
        while (isCitation(line[lead]) || isSpace(line[lead])) lead += 1;
        const moved = lead < line.length && line.slice(0, lead).some(isCitation) ? [...line.slice(lead), ...line.slice(0, lead).filter(isCitation)] : line;
        return i ? [{ type: "text", value: "\n" }, ...moved] : moved;
      })
    );
  }

  function glue(nodes: InlineNode[]): InlineNode[] {
    return nodes.map((node, i): InlineNode => {
      if (node.type === "inlineComponent") return i > 0 && isCitation(node) ? { ...node, attributes: { ...node.attributes, glued: "" } } : node;
      if (node.type === "text") return isCitation(nodes[i + 1]) ? { type: "text", value: node.value.trimEnd() } : node;
      return "children" in node ? { ...node, children: glue(node.children) } : node;
    });
  }

  function fenceCode(node: CodeBlockNode): { code: string; language: string } {
    /* Trailing blank lines come and go with each chunk; drawn and copied without them. */
    return { code: restoreSeparators(node.value.trimEnd()), language: (node.lang ?? "").toLowerCase() };
  }

  /* Raw HTML is escaped as text (no `allowHtml`). An image is its alt text, so a reply can't load a
     remote URL, and a link we won't follow is its label. */
  function markdownOptions(links: CitationLink[]): ParseOptions {
    const chat: MarkdownExtension = {
      name: "chat",
      inlineParser: {
        markers: "[<hHmM",
        parse: ({ source, index, inLink }) => (inLink ? undefined : source[index] === "[" ? citation(source, index, links) : autolink(source, index))
      },
      transformInline: placeCitations,
      renderHtml(node, { renderInline }) {
        switch (node.type) {
          case "link": {
            const href = safeHref(node.href);
            const label = node.children.map(renderInline).join("");
            return href ? `<a class="md-link" href="${escapeAttribute(href)}" target="_blank" rel="noreferrer">${label}</a>` : label;
          }
          case "inlineComponent": {
            const link = links[Number(node.attributes.n) - 1];
            return link && markerForCitation(link, "glued" in node.attributes);
          }
          /* A chat bubble's headings sit under the pane's: # is h3. */
          case "heading": {
            const tag = `h${Math.min(node.depth + 2, 6)}`;
            return `<${tag}>${node.children.map(renderInline).join("")}</${tag}>`;
          }
          /* A fence inside a list or quote: the codeBlock snippet's chrome, less its copy control. */
          case "code": {
            const { code, language } = fenceCode(node);
            const tag = language ? `<div class="md-code-head"><span class="code-language-tag">${escapeHtml(language)}</span></div>` : "";
            return `<div class="md-code-wrap" dir="ltr">${tag}<pre class="md-code-block"><code>${highlight(code, language)}</code></pre></div>`;
          }
        }
        return undefined;
      }
    };
    return { frontmatter: false, headingIds: false, urlTransform: (url, kind) => (kind === "link" ? safeHref(url) : null), extensions: [chat] };
  }

  /** Each line TanStack would lift out as a link or footnote definition, escaped so it stays in the
   *  reply as text: nothing here links by reference, and mid-stream the line would vanish as its `]:`
   *  arrived. Fences are tracked the way TanStack's own definition pass tracks them. */
  function keepDefinitions(lines: string[]): string[] {
    let fence = "";
    return lines.map((line) => {
      const marker = (!fence || line.includes(fence)) && /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
      if (marker) fence = !fence ? marker[1]! : marker[1]!.startsWith(fence) && !marker[2]!.trim() ? "" : fence;
      else if (!fence && /^ {0,3}\[[^\]\n]+\]:/.test(line)) return line.replace("[", "\\[");
      return line;
    });
  }

  /** The source of each top-level block, citations already placed. An append changes only the last. */
  export function markdownBlocks(value: string | null | undefined, sourceCitations: FileCitationAnnotation[]): string[] {
    const text = withCitationMarkers(hideSeparators((value ?? "").replace(/\r\n?/g, "\n")), sourceCitations).replace(/^===\s+"[^"]+"\s*$/gm, "");
    /* TanStack's AST has no source positions. Its top-level parser offers each block start to
       `parseBlock` on its own line array; nested blocks are parsed from arrays of their own. */
    let top: string[] | undefined;
    const starts: number[] = [];
    const probe: MarkdownExtension = { name: "block-starts", parseBlock: ({ lines, index }) => void ((top ??= lines) === lines && starts.push(index)) };
    parseMarkdown(keepDefinitions(text.split("\n")).join("\n"), { frontmatter: false, extensions: [probe] });
    if (!top) return [];
    /* A last line that is only a marker may yet be text ("1.5", "#tag", "***bold"): it is drawn with
       the block before it until the line settles, so no block but the last ever changes. */
    if (starts.length > 1 && starts.at(-1) === top.length - 1 && /^ {0,3}(?:[-+*]|\d{1,9}[.)]|#{1,6}|([-*_])(?:[ \t]*\1){2,})$/.test(top.at(-1)!)) starts.pop();
    return starts.map((start, i) => top!.slice(start, starts[i + 1]).join("\n").replace(/(\n[ \t]*)+$/, ""));
  }

  /** A fence closes on a run of its own character at least as long as the one that opened it. */
  function closesFence(opener: string, line: string | undefined): boolean {
    const markup = /^ {0,3}(`{3,}|~{3,})/.exec(opener)?.[1] ?? "```";
    const run = line?.trim() ?? "";
    return run.length >= markup.length && run[0] === markup[0] && /^(`+|~+)$/.test(run);
  }

  /** One block from `markdownBlocks`. `live` is the block still streaming: its unfinished
   *  markup is closed, and an unclosed fence is drawn as it arrives rather than highlighted. */
  export function renderBlock(source: string, links: CitationLink[], live: boolean): RenderedBlock {
    const options = markdownOptions(links);
    const document = parseMarkdown(live ? remend(source, REMEND_OPTIONS) : source, options);
    const [first] = document.children;
    if (document.children.length === 1 && first?.type === "code") {
      const lines = source.split("\n");
      const closed = lines.length > 1 && closesFence(lines[0]!, lines.at(-1));
      return { kind: "code", ...fenceCode(first), open: live && !closed };
    }
    /* Text is escaped, so a `<table>` here is a table. */
    const html = renderHtml(document, options).replaceAll("<table>", '<div class="md-table-wrap"><table>').replaceAll("</table>", "</table></div>");
    return { kind: "html", html: restoreSeparators(html) };
  }

  /** Where each streaming block was last tailed to, or `null` once its reader scrolled up. */
  const tailedTo = new WeakMap<Element, number | null>();

  /** Tail a streaming block's scroller, as JsonReply does; `code` is read only so new text re-runs it. */
  function followTail(code: string): Attachment<HTMLElement> {
    void code;
    return (pre) => {
      const frame = requestAnimationFrame(() => {
        if (tailedTo.get(pre) === null) return;
        pre.scrollTop = pre.scrollHeight;
        tailedTo.set(pre, pre.scrollTop);
      });
      const onScroll = () => {
        const own = tailedTo.get(pre);
        if (own != null && Math.abs(pre.scrollTop - own) <= 1) return;
        tailedTo.set(pre, atEnd(pre) ? pre.scrollTop : null);
      };
      pre.addEventListener("scroll", onScroll);
      return () => {
        cancelAnimationFrame(frame);
        pre.removeEventListener("scroll", onScroll);
      };
    };
  }
</script>

<script lang="ts">
  import Copyable from "$lib/components/primitives/Copyable.svelte";

  interface Props {
    text?: string | null;
    citations?: FileCitationAnnotation[];
    /** Render ``text`` as one code block in this language (``"py"``, ``"json"``, ...) instead of
     *  parsing it as Markdown, so a file's contents can't escape its fence. */
    language?: string | null;
    /** The reply is still arriving, so its last block may be cut off mid-markup. */
    streaming?: boolean;
  }

  let { text, citations = [], language = null, streaming = false }: Props = $props();

  /* Every delta brings a new but equal citations array; keyed on content, blocks re-render
     only when the links themselves change. */
  const linksJson = $derived(JSON.stringify(citationLinks(citations)));
  const links: CitationLink[] = $derived(JSON.parse(linksJson));
  const blocks = $derived(language === null ? markdownBlocks(text, citations) : []);
  const liveIndex = $derived(streaming ? blocks.length - 1 : -1);
  let codeElements = $state<HTMLElement[]>([]);
</script>

{#snippet codeBlock(code: string, lang: string, open: boolean, key: number)}
  <div class="md-code-wrap" class:open dir="ltr">
    <div class="md-code-head">
      {#if !open}
        <Copyable value={code} label="Copy code" select={() => codeElements[key]} data-tip-align="end" data-tip-below />
      {/if}
      {#if lang}<span class="code-language-tag">{lang}</span>{/if}
    </div>
    <pre class="md-code-block" {@attach open && followTail(code)}><code bind:this={codeElements[key]}
        >{#if open}{code}{:else}{@html highlight(code, lang)}{/if}</code
      ></pre>
  </div>
{/snippet}

<!-- Keyed by position: while a reply streams only its last block changes, so the blocks above
     keep their nodes, and with them a selection, a citation's hover and a code block's scroll. -->
<div class="markdown-message">
  {#if language !== null}
    {@render codeBlock((text ?? "").replace(/\r\n?/g, "\n"), language.trim().toLowerCase(), false, 0)}
  {:else}
    {#each blocks as source, index (index)}
      {@const live = index === liveIndex}
      {@const block = renderBlock(source, links, live)}
      {#if block.kind === "code"}
        {@render codeBlock(block.code, block.language, block.open, index)}
      {:else}
        <div dir="auto">{@html block.html}</div>
      {/if}
    {/each}
  {/if}
</div>

<style>
  /* An implicit `auto` column would size to a wide table's min-content, not the bubble. */
  .markdown-message {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--markdown-block-gap, 9px);
    font-family: var(--markdown-font-family, inherit);
    overflow-wrap: anywhere;
  }

	  .markdown-message :global(p),
	  .markdown-message :global(ul),
	  .markdown-message :global(ol),
	  .markdown-message :global(blockquote),
	  .markdown-message :global(pre),
	  .markdown-message :global(.md-table-wrap),
	  .markdown-message :global(h3),
	  .markdown-message :global(h4),
	  .markdown-message :global(h5),
  .markdown-message :global(h6) {
    margin: 0;
  }

  .markdown-message :global(h3),
  .markdown-message :global(h4),
  .markdown-message :global(h5),
  .markdown-message :global(h6) {
    color: var(--text-1);
    font-size: var(--markdown-heading-size, 13px);
    line-height: var(--markdown-heading-line-height, 1.35);
  }

  .markdown-message :global(p),
  .markdown-message :global(li),
  .markdown-message :global(blockquote) {
    font-size: var(--markdown-body-size, 13px);
    line-height: var(--markdown-body-line-height, 1.5);
  }

  .markdown-message :global(strong) {
    font-weight: var(--markdown-strong-weight, 700);
  }

  .markdown-message :global(ul),
  .markdown-message :global(ol) {
    display: grid;
    gap: var(--markdown-list-gap, 5px);
    padding-left: 20px;
  }

  .markdown-message :global(blockquote) {
    padding-left: 10px;
    border-left: 3px solid var(--border-strong);
    color: var(--text-2);
  }

  .markdown-message :global(pre) {
    max-width: 100%;
    overflow-x: auto;
    padding: 12px;
    border: 1px solid var(--code-block-border);
    border-radius: var(--radius-md);
    background: var(--code-block-bg);
    box-shadow: var(--code-block-shadow);
  }

  .markdown-message :global(.md-code-wrap) {
    position: relative;
    min-width: 0;
  }

  .markdown-message :global(pre.md-code-block) {
    padding-top: 34px;
  }

  /* Copy, then the language tag (drawn by app.css), over the block's top-right corner. */
  .markdown-message :global(.md-code-head) {
    position: absolute;
    top: 6px;
    right: 8px;
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
  }

  .markdown-message :global(.md-code-wrap:hover .copy) {
    opacity: 1;
  }

  /* A fence still streaming: capped, and tailed by followTail. */
  .open .md-code-block {
    max-height: min(320px, 50vh);
    overflow-y: auto;
  }

  .markdown-message :global(code) {
    border: 1px solid var(--code-inline-border);
    border-radius: var(--radius-sm);
    padding: 1px 4px;
    background: var(--code-inline-bg);
    color: var(--code-inline-text);
    font-family: var(--font-mono);
    font-size: var(--font-md);
  }

  .markdown-message :global(pre code) {
    display: block;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--code-block-text);
    line-height: 1.55;
    tab-size: 2;
    white-space: pre-wrap;
    overflow-wrap: break-word;
  }

	  .markdown-message :global(.md-table-wrap) {
	    max-width: 100%;
	    overflow-x: auto;
	    border: 1px solid var(--border);
	    border-radius: var(--radius-md);
	  }

	  .markdown-message :global(table) {
	    width: 100%;
	    min-width: 520px;
	    border-collapse: collapse;
	    font-size: var(--font-md);
	    line-height: 1.4;
	  }

	  .markdown-message :global(th),
	  .markdown-message :global(td) {
	    /* Not the `anywhere` inherited from the message: that sets a cell's min-content width to
	       one character, so auto layout squeezes a column until every word breaks mid-letter. */
	    overflow-wrap: break-word;
	    padding: 7px 9px;
	    border-bottom: 1px solid var(--border);
	    border-right: 1px solid var(--border);
	    vertical-align: top;
	  }

	  .markdown-message :global(th:last-child),
	  .markdown-message :global(td:last-child) {
	    border-right: 0;
	  }

	  .markdown-message :global(tbody tr:last-child td) {
	    border-bottom: 0;
	  }

	  .markdown-message :global(th) {
	    color: var(--text-1);
	    background: color-mix(in srgb, var(--surface-3) 78%, transparent);
	    font-weight: 750;
	    text-align: start;
	  }

	  .markdown-message :global(td) {
	    color: var(--text-2);
	    background: color-mix(in srgb, var(--surface-0) 52%, transparent);
	  }

	  .markdown-message :global(.md-link),
  .markdown-message :global(.md-citation) {
    color: var(--accent);
    text-decoration-line: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 3px;
  }

  .markdown-message :global(.md-citation) {
    position: relative;
    display: inline-flex;
    margin-left: 3px;
    font-size: var(--font-sm);
    font-weight: 750;
    vertical-align: baseline;
    white-space: nowrap;
  }

  .markdown-message :global(.md-citation::before),
  .markdown-message :global(.md-citation::after) {
    position: absolute;
    left: 50%;
    pointer-events: none;
    opacity: 0;
    transition: opacity var(--duration-fast) var(--ease-ui), transform var(--duration-fast) var(--ease-ui);
  }

  .markdown-message :global(.md-citation::before) {
    content: "";
    bottom: calc(100% + 3px);
    z-index: 24;
    border: 5px solid transparent;
    border-top-color: var(--surface-3);
    transform: translate(-50%, -2px);
  }

  .markdown-message :global(.md-citation::after) {
    content: attr(data-cite-title) "\A" attr(data-cite-url);
    bottom: calc(100% + 12px);
    z-index: 25;
    width: max-content;
    max-width: min(340px, 72vw);
    padding: 8px 10px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-md);
    background: var(--surface-3);
    color: var(--text-1);
    box-shadow: var(--shadow-floating);
    font-size: var(--font-sm);
    font-weight: 600;
    line-height: 1.35;
    text-align: left;
    text-decoration: none;
    transform: translate(-50%, 4px);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .markdown-message :global(.md-citation:focus-visible::before),
  .markdown-message :global(.md-citation:focus-visible::after) {
    opacity: 1;
    transform: translate(-50%, 0);
  }

  /* A tooltip is the worst thing to leave on a sticky hover: tapping a citation
     follows the link, and the card would still be open underneath on the way
     back. Keyboard focus above reaches it without a pointer. */
  @media (hover: hover) and (pointer: fine) {
    .markdown-message :global(.md-citation:hover::before),
    .markdown-message :global(.md-citation:hover::after) {
      opacity: 1;
      transform: translate(-50%, 0);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    /* Same bargain the global tooltip strikes: it still fades in, it just
       stops sliding into place. */
    .markdown-message :global(.md-citation::before),
    .markdown-message :global(.md-citation::after) {
      transition: opacity var(--duration-fast) var(--ease-ui);
      transform: translate(-50%, 0);
    }
  }
</style>
