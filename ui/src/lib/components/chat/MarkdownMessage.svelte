<script lang="ts">
  import type { FileCitationAnnotation } from "$lib/api/types";

  interface Props {
    text?: string | null;
    citations?: FileCitationAnnotation[];
  }

  interface CitationLink {
    number: number;
    title: string;
    url: string;
  }

  type CodeSegmentKind = "plain" | "comment" | "string";

  interface CodeSegment {
    kind: CodeSegmentKind;
    value: string;
  }

  let { text, citations = [] }: Props = $props();

  const rendered = $derived(renderMarkdown(text, citations));

  const languageAliases: Record<string, string> = {
    bash: "shell",
    "c#": "csharp",
    "c++": "cpp",
    csharp: "csharp",
    cs: "csharp",
    golang: "go",
    js: "javascript",
    jsx: "javascript",
    md: "markdown",
    py: "python",
    rb: "ruby",
    sh: "shell",
    ts: "typescript",
    tsx: "typescript",
    yml: "yaml",
    zsh: "shell"
  };

  const languageLabels: Record<string, string> = {
    c: "C",
    cpp: "C++",
    csharp: "C#",
    css: "CSS",
    go: "Go",
    html: "HTML",
    java: "Java",
    javascript: "JavaScript",
    json: "JSON",
    markdown: "Markdown",
    python: "Python",
    ruby: "Ruby",
    rust: "Rust",
    shell: "Shell",
    sql: "SQL",
    svelte: "Svelte",
    typescript: "TypeScript",
    yaml: "YAML"
  };

  const keywordSets: Record<string, string[]> = {
    c: [
      "auto",
      "break",
      "case",
      "const",
      "continue",
      "default",
      "do",
      "else",
      "enum",
      "extern",
      "for",
      "goto",
      "if",
      "register",
      "return",
      "sizeof",
      "static",
      "struct",
      "switch",
      "typedef",
      "union",
      "volatile",
      "while"
    ],
    cpp: [
      "break",
      "case",
      "catch",
      "class",
      "const",
      "constexpr",
      "continue",
      "default",
      "delete",
      "do",
      "else",
      "enum",
      "for",
      "if",
      "namespace",
      "new",
      "private",
      "protected",
      "public",
      "return",
      "static",
      "struct",
      "switch",
      "template",
      "this",
      "throw",
      "try",
      "using",
      "virtual",
      "while"
    ],
    csharp: [
      "async",
      "await",
      "break",
      "case",
      "catch",
      "class",
      "const",
      "continue",
      "default",
      "else",
      "enum",
      "for",
      "foreach",
      "if",
      "interface",
      "namespace",
      "new",
      "private",
      "protected",
      "public",
      "return",
      "static",
      "struct",
      "switch",
      "throw",
      "try",
      "using",
      "var",
      "while"
    ],
    css: [
      "align-items",
      "background",
      "border",
      "color",
      "display",
      "flex",
      "grid",
      "height",
      "margin",
      "padding",
      "position",
      "width"
    ],
    go: [
      "break",
      "case",
      "chan",
      "const",
      "continue",
      "default",
      "defer",
      "else",
      "fallthrough",
      "for",
      "func",
      "go",
      "goto",
      "if",
      "import",
      "interface",
      "map",
      "package",
      "range",
      "return",
      "select",
      "struct",
      "switch",
      "type",
      "var"
    ],
    java: [
      "abstract",
      "break",
      "case",
      "catch",
      "class",
      "const",
      "continue",
      "default",
      "else",
      "enum",
      "extends",
      "final",
      "finally",
      "for",
      "if",
      "implements",
      "import",
      "interface",
      "new",
      "package",
      "private",
      "protected",
      "public",
      "return",
      "static",
      "switch",
      "throw",
      "try",
      "while"
    ],
    javascript: [
      "async",
      "await",
      "break",
      "case",
      "catch",
      "class",
      "const",
      "continue",
      "default",
      "else",
      "export",
      "extends",
      "finally",
      "for",
      "from",
      "function",
      "if",
      "import",
      "let",
      "new",
      "return",
      "switch",
      "throw",
      "try",
      "var",
      "while",
      "yield"
    ],
    python: [
      "and",
      "as",
      "async",
      "await",
      "break",
      "class",
      "continue",
      "def",
      "elif",
      "else",
      "except",
      "finally",
      "for",
      "from",
      "global",
      "if",
      "import",
      "in",
      "is",
      "lambda",
      "nonlocal",
      "not",
      "or",
      "pass",
      "raise",
      "return",
      "try",
      "while",
      "with",
      "yield"
    ],
    ruby: [
      "begin",
      "break",
      "case",
      "class",
      "def",
      "do",
      "else",
      "elsif",
      "end",
      "ensure",
      "for",
      "if",
      "in",
      "module",
      "next",
      "redo",
      "rescue",
      "retry",
      "return",
      "then",
      "unless",
      "until",
      "when",
      "while",
      "yield"
    ],
    rust: [
      "async",
      "await",
      "break",
      "const",
      "continue",
      "crate",
      "else",
      "enum",
      "extern",
      "fn",
      "for",
      "if",
      "impl",
      "let",
      "loop",
      "match",
      "mod",
      "move",
      "mut",
      "pub",
      "ref",
      "return",
      "self",
      "static",
      "struct",
      "trait",
      "type",
      "unsafe",
      "use",
      "where",
      "while"
    ],
    shell: [
      "case",
      "do",
      "done",
      "elif",
      "else",
      "esac",
      "export",
      "fi",
      "for",
      "function",
      "if",
      "in",
      "local",
      "read",
      "then",
      "while"
    ],
    sql: [
      "alter",
      "and",
      "as",
      "by",
      "case",
      "create",
      "delete",
      "desc",
      "drop",
      "else",
      "end",
      "from",
      "group",
      "having",
      "insert",
      "into",
      "join",
      "left",
      "limit",
      "not",
      "null",
      "on",
      "or",
      "order",
      "right",
      "select",
      "set",
      "table",
      "then",
      "update",
      "values",
      "when",
      "where"
    ],
    typescript: [
      "as",
      "async",
      "await",
      "break",
      "case",
      "catch",
      "class",
      "const",
      "continue",
      "default",
      "else",
      "enum",
      "export",
      "extends",
      "finally",
      "for",
      "from",
      "function",
      "if",
      "implements",
      "import",
      "interface",
      "let",
      "new",
      "private",
      "protected",
      "public",
      "readonly",
      "return",
      "satisfies",
      "switch",
      "throw",
      "try",
      "type",
      "var",
      "while"
    ],
    yaml: ["false", "null", "true"]
  };

  const typeSets: Record<string, string[]> = {
    c: ["bool", "char", "double", "float", "int", "long", "short", "signed", "unsigned", "void"],
    cpp: ["auto", "bool", "char", "double", "float", "int", "long", "short", "size_t", "string", "void"],
    csharp: ["bool", "decimal", "double", "float", "int", "long", "object", "string", "void"],
    go: [
      "any",
      "bool",
      "byte",
      "comparable",
      "complex64",
      "complex128",
      "error",
      "float32",
      "float64",
      "int",
      "int8",
      "int16",
      "int32",
      "int64",
      "rune",
      "string",
      "uint",
      "uint8",
      "uint16",
      "uint32",
      "uint64",
      "uintptr"
    ],
    java: ["boolean", "byte", "char", "double", "float", "int", "long", "short", "String", "void"],
    javascript: ["Array", "Boolean", "Date", "Error", "Map", "Number", "Object", "Promise", "Set", "String"],
    python: ["bool", "bytes", "dict", "float", "int", "list", "set", "str", "tuple"],
    rust: ["bool", "char", "f32", "f64", "i32", "i64", "isize", "str", "String", "u32", "u64", "usize"],
    typescript: [
      "Array",
      "boolean",
      "Date",
      "Error",
      "Map",
      "number",
      "Promise",
      "Record",
      "Set",
      "string",
      "unknown",
      "void"
    ]
  };

  const literalWords = new Set([
    "False",
    "None",
    "True",
    "false",
    "iota",
    "nil",
    "null",
    "self",
    "super",
    "this",
    "true",
    "undefined"
  ]);

  function escapeHtml(value: string): string {
    return value
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function escapeAttribute(value: string): string {
    return escapeHtml(value).replaceAll("`", "&#96;");
  }

  function normalizeLanguage(value: string | undefined): string {
    const normalized = (value ?? "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_+#.-]/g, "");
    return languageAliases[normalized] ?? normalized;
  }

  function languageLabel(language: string): string {
    return languageLabels[language] ?? (language ? language.toUpperCase() : "");
  }

  function keywordSet(language: string): Set<string> {
    const keywords =
      language === "typescript"
        ? [...keywordSets.javascript, ...keywordSets.typescript]
        : (keywordSets[language] ?? []);
    return new Set(keywords);
  }

  function typeSet(language: string): Set<string> {
    const types =
      language === "typescript"
        ? [...(typeSets.javascript ?? []), ...(typeSets.typescript ?? [])]
        : (typeSets[language] ?? []);
    return new Set(types);
  }

  function lineCommentPrefixes(language: string): string[] {
    if (["python", "ruby", "shell", "yaml"].includes(language)) return ["#"];
    if (language === "sql") return ["--"];
    if (language === "css") return [];
    if (language === "json" || language === "markdown") return [];
    return ["//"];
  }

  function supportsBlockComments(language: string): boolean {
    return !["json", "markdown", "python", "ruby", "shell", "sql", "yaml"].includes(language);
  }

  function supportsBacktickStrings(language: string): boolean {
    return ["go", "javascript", "svelte", "typescript"].includes(language);
  }

  function stringQuotes(language: string): string[] {
    if (language === "json") return ['"'];
    if (language === "shell") return ['"', "'", "`"];
    if (supportsBacktickStrings(language)) return ['"', "'", "`"];
    return ['"', "'"];
  }

  function isTripleQuoted(language: string, code: string, index: number, quote: string): boolean {
    return language === "python" && (quote === '"' || quote === "'") && code.startsWith(quote.repeat(3), index);
  }

  function consumeString(code: string, index: number, language: string): number {
    const quote = code[index] ?? "";
    const tripleQuoted = isTripleQuoted(language, code, index, quote);
    const delimiter = tripleQuoted ? quote.repeat(3) : quote;
    let cursor = index + delimiter.length;

    while (cursor < code.length) {
      if (!tripleQuoted && quote !== "`" && code[cursor] === "\\") {
        cursor += 2;
        continue;
      }

      if (code.startsWith(delimiter, cursor)) {
        return cursor + delimiter.length;
      }

      cursor += 1;
    }

    return code.length;
  }

  function splitCodeSegments(code: string, language: string): CodeSegment[] {
    const segments: CodeSegment[] = [];
    const linePrefixes = lineCommentPrefixes(language);
    const quotes = stringQuotes(language);
    let cursor = 0;
    let plainStart = 0;

    const pushPlain = (end: number) => {
      if (end > plainStart) {
        segments.push({ kind: "plain", value: code.slice(plainStart, end) });
      }
    };

    while (cursor < code.length) {
      const linePrefix = linePrefixes.find((prefix) => code.startsWith(prefix, cursor));
      if (linePrefix) {
        pushPlain(cursor);
        const newlineIndex = code.indexOf("\n", cursor);
        const end = newlineIndex === -1 ? code.length : newlineIndex;
        segments.push({ kind: "comment", value: code.slice(cursor, end) });
        cursor = end;
        plainStart = cursor;
        continue;
      }

      if (supportsBlockComments(language) && code.startsWith("/*", cursor)) {
        pushPlain(cursor);
        const endIndex = code.indexOf("*/", cursor + 2);
        const end = endIndex === -1 ? code.length : endIndex + 2;
        segments.push({ kind: "comment", value: code.slice(cursor, end) });
        cursor = end;
        plainStart = cursor;
        continue;
      }

      if (quotes.includes(code[cursor] ?? "")) {
        pushPlain(cursor);
        const end = consumeString(code, cursor, language);
        segments.push({ kind: "string", value: code.slice(cursor, end) });
        cursor = end;
        plainStart = cursor;
        continue;
      }

      cursor += 1;
    }

    pushPlain(code.length);
    return segments;
  }

  function syntaxSpan(kind: string, value: string): string {
    return `<span class="md-syntax-${kind}">${escapeHtml(value)}</span>`;
  }

  function highlightPlainCode(value: string, language: string): string {
    const keywords = keywordSet(language);
    const types = typeSet(language);
    const tokenPattern =
      /0x[\dA-Fa-f]+|\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?|[A-Za-z_$][\w$]*|:=|=>|==|!=|<=|>=|\+\+|--|&&|\|\||[{}()[\].,;:+\-*/%<>=!&|?]/g;
    let output = "";
    let lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = tokenPattern.exec(value)) !== null) {
      const token = match[0];
      output += escapeHtml(value.slice(lastIndex, match.index));

      if (/^(?:0x[\dA-Fa-f]+|\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)$/.test(token)) {
        output += syntaxSpan("number", token);
      } else if (literalWords.has(token)) {
        output += syntaxSpan("literal", token);
      } else if (keywords.has(token) || keywords.has(token.toLowerCase())) {
        output += syntaxSpan("keyword", token);
      } else if (types.has(token) || types.has(token.toLowerCase())) {
        output += syntaxSpan("type", token);
      } else if (/^[A-Za-z_$][\w$]*$/.test(token) && /^\s*\(/.test(value.slice(tokenPattern.lastIndex))) {
        output += syntaxSpan("function", token);
      } else if (/^[{}()[\].,;:]$/.test(token)) {
        output += syntaxSpan("punctuation", token);
      } else if (/^[:=+\-*/%<>=!&|?]+$/.test(token)) {
        output += syntaxSpan("operator", token);
      } else {
        output += escapeHtml(token);
      }

      lastIndex = tokenPattern.lastIndex;
    }

    output += escapeHtml(value.slice(lastIndex));
    return output;
  }

  function highlightCode(code: string, language: string): string {
    return splitCodeSegments(code, language)
      .map((segment) => {
        if (segment.kind === "comment") return syntaxSpan("comment", segment.value);
        if (segment.kind === "string") return syntaxSpan("string", segment.value);
        return highlightPlainCode(segment.value, language);
      })
      .join("");
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

  function safeHref(value: string): string {
    const href = value.trim();
    if (!href) return "#";
    if (href.startsWith("#") || href.startsWith("/")) return escapeAttribute(href);

    try {
      const url = new URL(href);
      if (url.protocol === "http:" || url.protocol === "https:" || url.protocol === "mailto:") {
        return escapeAttribute(url.toString());
      }
    } catch {
      return "#";
    }
    return "#";
  }

  function citationLinks(sourceCitations: FileCitationAnnotation[]): CitationLink[] {
    return sourceCitations.map((citation, index) => ({
      number: index + 1,
      title: citationTitle(citation),
      url: citationUrl(citation)
    }));
  }

  function markerForCitation(citation: CitationLink): string {
    const label = `[${citation.number}]`;
    const href = safeHref(citation.url);
    const title = escapeAttribute(citation.title);
    return `<a class="md-citation" href="${href}" target="_blank" rel="noreferrer" aria-label="${title}" data-cite-title="${title}" data-cite-url="${href}">${escapeHtml(label)}</a>`;
  }

  function appendCitationMarker(output: string, citation: CitationLink): string {
    if (!output) return markerForCitation(citation);
    return `${output.trimEnd()}&nbsp;${markerForCitation(citation)}`;
  }

  function normalizeCitationRun(value: string): string {
    return value.trim().replace(/\s+/g, " ");
  }

  function moveLeadingCitationsToEnd(value: string): string {
    const match = value.match(/^(\s*)((?:\[\d+\]\s*)+)(.+)$/);
    if (!match?.[2] || !match[3]?.trim()) return value;
    return `${match[1]}${match[3].trimStart().trimEnd()} ${normalizeCitationRun(match[2])}`;
  }

  function moveHeadingCitationsToEnd(line: string): string {
    const match = line.match(/^(#{1,4}\s+)((?:\[\d+\]\s*)+)(.+)$/);
    if (!match?.[1] || !match[2] || !match[3]?.trim()) return line;
    return `${match[1]}${match[3].trimStart().trimEnd()} ${normalizeCitationRun(match[2])}`;
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

  function renderInline(value: string, links: CitationLink[]): string {
    let output = "";
    let index = 0;

    while (index < value.length) {
      const rest = value.slice(index);

      const citationMatch = rest.match(/^\[(\d+)\]/);
      if (citationMatch?.[1]) {
        const citation = links[Number(citationMatch[1]) - 1];
        if (citation) {
          output = appendCitationMarker(output, citation);
          index += citationMatch[0].length;
          continue;
        }
      }

      const markdownLinkMatch = rest.match(/^\[([^\]\n]+)\]\(([^)\s]+)\)/);
      if (markdownLinkMatch?.[1] && markdownLinkMatch[2]) {
        output += `<a class="md-link" href="${safeHref(markdownLinkMatch[2])}" target="_blank" rel="noreferrer">${renderInline(markdownLinkMatch[1], links)}</a>`;
        index += markdownLinkMatch[0].length;
        continue;
      }

      const autoLinkMatch = rest.match(/^(https?:\/\/[^\s<)]+)/);
      if (autoLinkMatch?.[1]) {
        const href = autoLinkMatch[1];
        output += `<a class="md-link" href="${safeHref(href)}" target="_blank" rel="noreferrer">${escapeHtml(href)}</a>`;
        index += href.length;
        continue;
      }

      const codeMatch = rest.match(/^`([^`\n]+)`/);
      if (codeMatch?.[1]) {
        output += `<code>${escapeHtml(codeMatch[1])}</code>`;
        index += codeMatch[0].length;
        continue;
      }

      const strongMatch = rest.match(/^(\*\*|__)(.+?)\1/);
      if (strongMatch?.[2]) {
        output += `<strong>${renderInline(strongMatch[2], links)}</strong>`;
        index += strongMatch[0].length;
        continue;
      }

      const emphasisMatch = rest.match(/^(\*|_)([^\s*_][^*_]*?)\1/);
      if (emphasisMatch?.[2]) {
        output += `<em>${renderInline(emphasisMatch[2], links)}</em>`;
        index += emphasisMatch[0].length;
        continue;
      }

      output += escapeHtml(value[index] ?? "");
      index += 1;
    }

    return output;
  }

  function renderList(
    lines: string[],
    links: CitationLink[],
    ordered: boolean
  ): { html: string; nextIndex: number } {
    const items: string[] = [];
    const pattern = ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*]\s+(.+)$/;
    let index = 0;

    while (index < lines.length) {
      const match = lines[index]?.match(pattern);
      if (!match?.[1]) break;
      items.push(`<li>${renderInline(moveLeadingCitationsToEnd(match[1]), links)}</li>`);
      index += 1;
    }

    const tag = ordered ? "ol" : "ul";
    return { html: `<${tag}>${items.join("")}</${tag}>`, nextIndex: index };
  }

  function splitTableRow(line: string): string[] {
    const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map((cell) => moveLeadingCitationsToEnd(cell.trim()));
  }

  function isTableDivider(line: string): boolean {
    const cells = splitTableRow(line);
    return (
      cells.length > 1 &&
      cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, "")))
    );
  }

  function tableAlignment(cell: string): "left" | "center" | "right" {
    const compact = cell.replace(/\s+/g, "");
    if (compact.startsWith(":") && compact.endsWith(":")) return "center";
    if (compact.endsWith(":")) return "right";
    return "left";
  }

  function isTableStart(lines: string[], index: number): boolean {
    const header = lines[index] ?? "";
    const divider = lines[index + 1] ?? "";
    return header.includes("|") && isTableDivider(divider);
  }

  function isTableRow(line: string): boolean {
    return line.includes("|") && line.trim().length > 0;
  }

  function renderTable(
    lines: string[],
    links: CitationLink[]
  ): { html: string; nextIndex: number } {
    const headers = splitTableRow(lines[0] ?? "");
    const divider = splitTableRow(lines[1] ?? "");
    const alignments = headers.map((_, index) => tableAlignment(divider[index] ?? ""));
    const rows: string[][] = [];
    let index = 2;

    while (index < lines.length && isTableRow(lines[index] ?? "")) {
      rows.push(splitTableRow(lines[index] ?? ""));
      index += 1;
    }

    const alignAttr = (alignment: "left" | "center" | "right") =>
      alignment === "left" ? "" : ` style="text-align: ${alignment}"`;
    const headerHtml = headers
      .map((cell, cellIndex) => `<th${alignAttr(alignments[cellIndex])}>${renderInline(cell, links)}</th>`)
      .join("");
    const bodyHtml = rows
      .map((row) => {
        const cells = headers.map((_, cellIndex) => row[cellIndex] ?? "");
        return `<tr>${cells
          .map(
            (cell, cellIndex) =>
              `<td${alignAttr(alignments[cellIndex])}>${renderInline(cell, links)}</td>`
          )
          .join("")}</tr>`;
      })
      .join("");

    return {
      html: `<div class="md-table-wrap"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`,
      nextIndex: index
    };
  }

  function renderMarkdown(value: string | null | undefined, sourceCitations: FileCitationAnnotation[]): string {
    const normalized = typeof value === "string" ? value : "";
    const textWithCitations = withCitationMarkers(normalized.replace(/\r\n?/g, "\n"), sourceCitations);
    const links = citationLinks(sourceCitations);
    const lines = textWithCitations
      .split("\n")
      .map((line) => moveHeadingCitationsToEnd(moveLeadingCitationsToEnd(line)));
    const blocks: string[] = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index] ?? "";

      if (!line.trim()) {
        index += 1;
        continue;
      }

      const tabMatch = line.match(/^===\s+"([^"]+)"\s*$/);
      if (tabMatch) {
        index += 1;
        continue;
      }

      const fenceMatch = line.match(/^(\s*)```\s*([A-Za-z0-9_+#.-]+)?\s*$/);
      if (fenceMatch) {
        const fenceIndent = fenceMatch[1] ?? "";
        const codeLines: string[] = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index] ?? "")) {
          const codeLine = lines[index] ?? "";
          codeLines.push(fenceIndent && codeLine.startsWith(fenceIndent) ? codeLine.slice(fenceIndent.length) : codeLine);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const language = normalizeLanguage(fenceMatch[2]);
        const label = languageLabel(language);
        const languageClass = language ? ` class="language-${escapeAttribute(language)}"` : "";
        const languageAttribute = label ? ` data-language="${escapeAttribute(label)}"` : "";
        blocks.push(
          `<pre class="md-code-block"${languageAttribute}><code${languageClass}>${highlightCode(codeLines.join("\n"), language)}</code></pre>`
        );
        continue;
      }

      const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
      if (headingMatch?.[1] && headingMatch[2]) {
        const level = Math.min(headingMatch[1].length + 2, 6);
        blocks.push(`<h${level}>${renderInline(headingMatch[2], links)}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s*[-*]\s+/.test(line)) {
        const list = renderList(lines.slice(index), links, false);
        blocks.push(list.html);
        index += list.nextIndex;
        continue;
      }

      if (/^\s*\d+[.)]\s+/.test(line)) {
        const list = renderList(lines.slice(index), links, true);
        blocks.push(list.html);
        index += list.nextIndex;
        continue;
      }

      if (isTableStart(lines, index)) {
        const table = renderTable(lines.slice(index), links);
        blocks.push(table.html);
        index += table.nextIndex;
        continue;
      }

      if (/^>\s?/.test(line)) {
        const quoteLines: string[] = [];
        while (index < lines.length && /^>\s?/.test(lines[index] ?? "")) {
          quoteLines.push((lines[index] ?? "").replace(/^>\s?/, ""));
          index += 1;
        }
        blocks.push(`<blockquote>${renderMarkdown(quoteLines.join("\n"), [])}</blockquote>`);
        continue;
      }

      if (/^---+$/.test(line.trim())) {
        blocks.push("<hr>");
        index += 1;
        continue;
      }

      const paragraphLines = [line.trim()];
      index += 1;
      while (
        index < lines.length &&
        lines[index]?.trim() &&
        !/^\s*```/.test(lines[index] ?? "") &&
        !/^(#{1,4})\s+/.test(lines[index] ?? "") &&
        !/^\s*[-*]\s+/.test(lines[index] ?? "") &&
        !/^\s*\d+[.)]\s+/.test(lines[index] ?? "") &&
        !isTableStart(lines, index) &&
        !/^>\s?/.test(lines[index] ?? "") &&
        !/^---+$/.test((lines[index] ?? "").trim())
      ) {
        paragraphLines.push((lines[index] ?? "").trim());
        index += 1;
      }
      blocks.push(`<p>${renderInline(paragraphLines.join(" "), links)}</p>`);
    }

    return blocks.join("");
  }
</script>

<div class="markdown-message">
  {@html rendered}
</div>

<style>
  .markdown-message {
    min-width: 0;
    display: grid;
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
    border-radius: 8px;
    background: var(--code-block-bg);
    box-shadow: var(--code-block-shadow);
  }

  .markdown-message :global(pre.md-code-block[data-language]) {
    position: relative;
    padding-top: 34px;
  }

  .markdown-message :global(pre.md-code-block[data-language]::before) {
    content: attr(data-language);
    position: absolute;
    top: 9px;
    right: 10px;
    padding: 2px 7px;
    border: 1px solid var(--code-label-border);
    border-radius: 999px;
    background: var(--code-label-bg);
    color: var(--code-label-text);
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    font-size: 10px;
    font-weight: 750;
    line-height: 1.2;
    letter-spacing: 0;
  }

  .markdown-message :global(code) {
    border: 1px solid var(--code-inline-border);
    border-radius: 5px;
    padding: 1px 4px;
    background: var(--code-inline-bg);
    color: #f5f0ff;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    font-size: 12px;
  }

  .markdown-message :global(pre code) {
    display: block;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--code-block-text);
    line-height: 1.55;
    tab-size: 2;
    white-space: pre;
  }

  .markdown-message :global(.md-syntax-comment) {
    color: #9d96b8;
    font-style: italic;
  }

  .markdown-message :global(.md-syntax-string) {
    color: #a7f3d0;
  }

  .markdown-message :global(.md-syntax-keyword) {
    color: #d8b4fe;
    font-weight: 750;
  }

  .markdown-message :global(.md-syntax-type) {
    color: #93c5fd;
  }

  .markdown-message :global(.md-syntax-function) {
    color: #fde68a;
  }

  .markdown-message :global(.md-syntax-number) {
    color: #fdba74;
  }

  .markdown-message :global(.md-syntax-literal) {
    color: #f0abfc;
  }

  .markdown-message :global(.md-syntax-operator),
  .markdown-message :global(.md-syntax-punctuation) {
    color: #c4b5fd;
  }

	  .markdown-message :global(.md-table-wrap) {
	    max-width: 100%;
	    overflow-x: auto;
	    border: 1px solid var(--border);
	    border-radius: 7px;
	  }

	  .markdown-message :global(table) {
	    width: 100%;
	    min-width: 520px;
	    border-collapse: collapse;
	    font-size: 12px;
	    line-height: 1.4;
	  }

	  .markdown-message :global(th),
	  .markdown-message :global(td) {
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
	    text-align: left;
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
    font-size: 11px;
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
    transition: opacity 140ms ease, transform 140ms ease;
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
    border-radius: 7px;
    background: var(--surface-3);
    color: var(--text-1);
    box-shadow: 0 12px 32px rgb(0 0 0 / 34%);
    font-size: 11px;
    font-weight: 600;
    line-height: 1.35;
    text-align: left;
    text-decoration: none;
    transform: translate(-50%, 4px);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .markdown-message :global(.md-citation:hover::before),
  .markdown-message :global(.md-citation:hover::after),
  .markdown-message :global(.md-citation:focus-visible::before),
  .markdown-message :global(.md-citation:focus-visible::after) {
    opacity: 1;
    transform: translate(-50%, 0);
  }
</style>
