// What the studio reads out of the agent's stream, as plain functions: a tool's result, a policy
// decision, an answer with charts in it. Plain JavaScript (typed with JSDoc), so the page loads it
// as-is and analysis.test.mjs runs it under Node.

/**
 * A value read from a repr or JSON. Its items and fields are values too, which JSDoc can't say:
 * a type alias here can't refer to itself.
 * @typedef {null | boolean | number | string | unknown[] | { [key: string]: unknown }} PyValue
 */

// -- tool results ---------------------------------------------------------------------------------

/**
 * A Python value's repr, as JSON-like data. A tool's `tool_output` is `str(result)`: for a
 * pydantic model that is `field=value field=value`, its nested models are `Name(field=value)`,
 * and lists, dicts, strings and numbers are Python's own reprs.
 * @param {string} text
 * @returns {PyValue}
 */
export function parsePyRepr(text) {
  let i = 0;
  const fail = (/** @type {string} */ what) => {
    throw new SyntaxError(`${what} at ${i} in ${JSON.stringify(text.slice(Math.max(0, i - 20), i + 20))}`);
  };
  const space = () => {
    while (i < text.length && /\s/.test(text[i] ?? "")) i++;
  };
  const ident = () => {
    const m = /^[A-Za-z_][\w.]*/.exec(text.slice(i));
    return m ? m[0] : null;
  };
  /** `field=value` pairs up to `end` (or the end of the text): a model's fields. */
  const fields = (/** @type {string | null} */ end) => {
    /** @type {{ [key: string]: PyValue }} */
    const out = {};
    /** @type {PyValue[]} */
    const positional = [];
    space();
    while (i < text.length && text[i] !== end) {
      const name = ident();
      if (name && text[i + name.length] === "=") {
        i += name.length + 1;
        out[name] = value();
      } else {
        positional.push(value());
      }
      space();
      if (text[i] === ",") i++;
      space();
    }
    if (end !== null) {
      if (text[i] !== end) fail(`expected ${end}`);
      i++;
    }
    return Object.keys(out).length === 0 && positional.length > 0 ? positional : out;
  };
  /** @returns {PyValue[]} */
  const sequence = (/** @type {string} */ end) => {
    /** @type {PyValue[]} */
    const items = [];
    i++;
    space();
    while (text[i] !== end) {
      if (i >= text.length) fail(`expected ${end}`);
      items.push(value());
      space();
      if (text[i] === ",") i++;
      space();
    }
    i++;
    return items;
  };
  const string = () => {
    const quote = text[i];
    let out = "";
    i++;
    while (text[i] !== quote) {
      if (i >= text.length) fail("unterminated string");
      const c = text[i++];
      if (c !== "\\") {
        out += c;
        continue;
      }
      const e = text[i++] ?? "";
      const hex = (/** @type {number} */ n) => {
        const code = parseInt(text.slice(i, i + n), 16);
        i += n;
        return String.fromCodePoint(code);
      };
      if (e === "n") out += "\n";
      else if (e === "t") out += "\t";
      else if (e === "r") out += "\r";
      else if (e === "0") out += "\0";
      else if (e === "x") out += hex(2);
      else if (e === "u") out += hex(4);
      else if (e === "U") out += hex(8);
      else out += e;
    }
    i++;
    return out;
  };
  /** @returns {PyValue} */
  const value = () => {
    space();
    const c = text[i];
    if (c === "[") return sequence("]");
    if (c === "(") return sequence(")");
    if (c === "'" || c === '"') return string();
    if (c === "{") {
      /** @type {{ [key: string]: PyValue }} */
      const out = {};
      i++;
      space();
      while (text[i] !== "}") {
        if (i >= text.length) fail("expected }");
        const key = value();
        space();
        if (text[i] !== ":") fail("expected :");
        i++;
        out[String(key)] = value();
        space();
        if (text[i] === ",") i++;
        space();
      }
      i++;
      return out;
    }
    const number = /^-?(?:inf|nan|\d+(?:\.\d*)?(?:e[+-]?\d+)?|\.\d+(?:e[+-]?\d+)?)(?![\w.])/i.exec(text.slice(i));
    if (number) {
      i += number[0].length;
      const n = number[0].toLowerCase();
      if (n.endsWith("inf")) return n.startsWith("-") ? -Infinity : Infinity;
      return n.endsWith("nan") ? NaN : Number(n);
    }
    const name = ident();
    if (!name) return fail("unexpected character");
    i += name.length;
    if (name === "None") return null;
    if (name === "True") return true;
    if (name === "False") return false;
    if (text[i] === "(") {
      i++;
      return fields(")");
    }
    return name;
  };

  space();
  const result = /^[A-Za-z_]\w*=/.test(text.slice(i)) ? fields(null) : value();
  space();
  if (i < text.length) fail("trailing text");
  return result;
}

/**
 * A tool's result as data: JSON, or a Python repr. `null` when it is neither.
 * @param {string | null} text
 * @returns {PyValue | undefined}
 */
export function readResult(text) {
  if (text === null) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    // Not JSON: a repr, as an activity tool's result is.
  }
  try {
    return parsePyRepr(text);
  } catch {
    return undefined;
  }
}

/**
 * What a Code Mode script printed and returned, with a returned string unwrapped from its repr.
 * @param {string} text
 * @returns {{ printed: string; result: string | null; error: string | null }}
 */
export function readScriptOutput(text) {
  const error = /^Script error \(([\s\S]*)\)$/.exec(text.trim());
  if (error) return { printed: "", result: null, error: error[1] ?? "" };
  const at = text.lastIndexOf("result: ");
  if (at < 0) return { printed: text, result: null, error: null };
  let result = text.slice(at + 8).trim();
  if (/^(['"])[\s\S]*\1$/.test(result)) {
    try {
      result = String(parsePyRepr(result));
    } catch {
      // Leave it quoted.
    }
  }
  return { printed: text.slice(0, at).trim(), result, error: null };
}

// -- the policy ------------------------------------------------------------------------------------

/** The data tools whose calls carry a query. */
export const SQL_TOOLS = ["run_sql", "summary_stats", "calc_distribution", "correlate", "group_compare", "detect_outliers"];
/** Every tool a script calls to read data: each call is put to the policy. */
export const DATA_TOOLS = ["list_tables", "describe_table", ...SQL_TOOLS];
export const CHART_TOOLS = ["render_bar_chart", "render_line_chart", "render_pie_chart", "render_scatter_chart", "render_heatmap_chart"];
/** Publishing a report: a callback tool the policy judges first, like a data call. */
export const REPORT_TOOL = "publish_report";

/**
 * Where a SQL tool's result says its data came from: the tables it read, and whether any of
 * them is restricted. Null for a result that doesn't say.
 * @param {PyValue | undefined} result
 * @returns {{ tablesRead: string[]; restricted: boolean } | null}
 */
export function dataOrigin(result) {
  if (!isRecord(result) || !("restricted_data" in result)) return null;
  return {
    tablesRead: Array.isArray(result.tables_read) ? result.tables_read.map(String) : [],
    restricted: result.restricted_data === true
  };
}

/**
 * How sensitive a table is: `open` analytics data, `restricted` risk data a person grants, or
 * `pii`, which is never readable.
 * @param {string} table
 * @returns {"open" | "restricted" | "pii"}
 */
export function tableKind(table) {
  if (table.startsWith("customer_pii.")) return "pii";
  if (table.startsWith("risk.")) return "restricted";
  return "open";
}

/**
 * @typedef {object} PolicyView
 * @property {"checking" | "approved" | "denied" | "needs you" | "not checked"} verdict
 * @property {string | null} reason
 * @property {string | null} action        What the policy was asked, e.g. `bank-analyst/run_sql`.
 * @property {string | null} statement     The query's statement type, as the policy saw it.
 * @property {string[]} tables             Every table the query reads.
 * @property {string[]} tableFunctions
 * @property {string | null} queryError    Why the query doesn't plan, when it doesn't.
 * @property {{ action: string; table: string }[]} checks
 * @property {string | null} decision      The policy's own decision name.
 */

/**
 * What the policy made of one tool call, from its evaluations and its gate's resolution.
 * @param {{ state: string; approval: { approved: boolean; reason: string | null } | null;
 *   evaluations: readonly { status: string; verdict: string | null; reason: string | null;
 *   details: Record<string, unknown>; error: string | null }[] }} part
 * @returns {PolicyView}
 */
export function policyOf(part) {
  const ended = part.evaluations.filter((e) => e.status === "ended").at(-1);
  const failed = part.evaluations.filter((e) => e.status === "error").at(-1);
  const details = ended?.details ?? {};
  const facts = /** @type {Record<string, unknown>} */ (isRecord(details.parameter) ? details.parameter : {});
  const checks = Array.isArray(details.checks)
    ? details.checks.flatMap((c) => {
        if (!isRecord(c)) return [];
        const parameter = isRecord(c.parameter) ? c.parameter : {};
        return [{ action: String(c.action ?? ""), table: String(parameter.table ?? "") }];
      })
    : [];

  /** @type {PolicyView["verdict"]} */
  let verdict;
  if (part.state === "denied" || part.approval?.approved === false) verdict = "denied";
  else if (part.state === "evaluating" || part.evaluations.some((e) => e.status === "running")) verdict = "checking";
  else if (part.state === "awaiting_approval") verdict = "needs you";
  else if (part.approval?.approved || ended?.verdict === "approve") verdict = "approved";
  else verdict = part.evaluations.length ? "approved" : "not checked";

  return {
    verdict,
    reason: part.approval?.reason ?? ended?.reason ?? failed?.error ?? null,
    action: typeof details.action === "string" ? details.action : null,
    statement: typeof facts.statement === "string" ? facts.statement : null,
    tables: Array.isArray(facts.tables) ? facts.tables.map(String) : [],
    tableFunctions: Array.isArray(facts.table_functions) ? facts.table_functions.map(String) : [],
    queryError: typeof facts.error === "string" ? facts.error : null,
    checks,
    decision: typeof details.decision === "string" ? details.decision : null
  };
}

/** @param {unknown} v @returns {v is Record<string, unknown>} */
function isRecord(v) {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// -- charts ---------------------------------------------------------------------------------------

/**
 * Why a chart call's data can't be drawn, or null when it can. Sent back to the agent as the
 * call's error, so it can fix the data and draw again.
 * @param {string} tool
 * @param {Record<string, unknown>} input
 * @returns {string | null}
 */
export function chartProblem(tool, input) {
  const numbers = (/** @type {unknown} */ v) => Array.isArray(v) && v.every((x) => typeof x === "number" && Number.isFinite(x));
  const seriesOf = (/** @type {unknown} */ s, /** @type {number} */ n, /** @type {string} */ along) => {
    if (!Array.isArray(s) || s.length === 0) return "`series` needs at least one series.";
    for (const one of s) {
      if (!isRecord(one) || !numbers(one.values)) return "Each series needs a `name` and numeric `values`.";
      if (/** @type {unknown[]} */ (one.values).length !== n) {
        return `Series "${String(one.name)}" has ${/** @type {unknown[]} */ (one.values).length} values for ${n} ${along}.`;
      }
    }
    return null;
  };
  if (tool === "render_bar_chart") {
    const categories = input.categories;
    if (!Array.isArray(categories) || categories.length === 0) return "`categories` needs at least one category.";
    return seriesOf(input.series, categories.length, "categories");
  }
  if (tool === "render_line_chart") {
    const xs = input.x_values;
    if (!Array.isArray(xs) || xs.length < 2) return "`x_values` needs at least two values.";
    return seriesOf(input.series, xs.length, "x values");
  }
  if (tool === "render_pie_chart") {
    const { labels, values } = input;
    if (!Array.isArray(labels) || !numbers(values) || labels.length !== /** @type {number[]} */ (values).length || labels.length === 0) {
      return "`labels` and `values` need the same, non-zero, number of entries.";
    }
    if (/** @type {number[]} */ (values).some((v) => v < 0)) return "A pie chart can't show negative values.";
    if (/** @type {number[]} */ (values).every((v) => v === 0)) return "Every value is zero.";
    return null;
  }
  if (tool === "render_scatter_chart") {
    const points = input.points;
    if (!Array.isArray(points) || points.length === 0) return "`points` needs at least one point.";
    if (!points.every((p) => isRecord(p) && Number.isFinite(p.x) && Number.isFinite(p.y))) return "Each point needs numeric `x` and `y`.";
    return null;
  }
  if (tool === "render_heatmap_chart") {
    const { row_labels: rows, column_labels: columns, values } = input;
    if (!Array.isArray(rows) || rows.length === 0) return "`row_labels` needs at least one label.";
    if (!Array.isArray(columns) || columns.length === 0) return "`column_labels` needs at least one label.";
    if (!Array.isArray(values) || values.length !== rows.length) {
      return `\`values\` has ${Array.isArray(values) ? values.length : 0} rows for ${rows.length} row labels.`;
    }
    for (const [r, row] of values.entries()) {
      if (!numbers(row)) return `Row "${String(rows[r])}" needs numeric values.`;
      if (/** @type {number[]} */ (row).length !== columns.length) {
        return `Row "${String(rows[r])}" has ${/** @type {number[]} */ (row).length} values for ${columns.length} column labels.`;
      }
    }
    return null;
  }
  return `${tool} is not a chart this page draws.`;
}

/**
 * An answer, split around its charts: each `[chart:<id>]` on a line of its own is a chart.
 * @param {string} text
 * @returns {({ type: "text"; text: string } | { type: "chart"; id: string })[]}
 */
export function splitAnswer(text) {
  /** @type {({ type: "text"; text: string } | { type: "chart"; id: string })[]} */
  const out = [];
  let lines = /** @type {string[]} */ ([]);
  const flush = () => {
    const chunk = lines.join("\n");
    if (chunk.trim()) out.push({ type: "text", text: chunk });
    lines = [];
  };
  for (const line of text.split("\n")) {
    const ref = /^\s*`?\[chart:\s*([^\]\s]+)\s*\]`?\s*$/.exec(line);
    if (ref) {
      flush();
      out.push({ type: "chart", id: ref[1] ?? "" });
    } else {
      lines.push(line);
    }
  }
  flush();
  return out;
}

/**
 * Round tick values spanning [min, max], about `count` of them.
 * @param {number} min
 * @param {number} max
 * @param {number} [count]
 * @returns {number[]}
 */
export function niceTicks(min, max, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) {
    if (min === 0) return [0, 1];
    [min, max] = min > 0 ? [0, min] : [min, 0];
  }
  const raw = (max - min) / Math.max(1, count);
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((m) => m * power >= raw) ?? 10) * power;
  // Multiples of the step, so no rounding error accumulates.
  const ticks = [];
  const first = Math.floor(min / step + 1e-9);
  const last = Math.ceil(max / step - 1e-9);
  for (let k = first; k <= last; k++) ticks.push(Number((k * step).toPrecision(12)) || 0);
  return ticks;
}

/**
 * A number for an axis or a label: compact for large magnitudes.
 * @param {number} v
 * @returns {string}
 */
export function formatNumber(v) {
  if (Number.isNaN(v)) return "—";
  if (!Number.isFinite(v)) return v > 0 ? "∞" : "−∞";
  const abs = Math.abs(v);
  if (abs >= 1e4) return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
  if (abs >= 100 || Number.isInteger(v)) return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (abs >= 1) return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  return v.toLocaleString("en-US", { maximumSignificantDigits: 3 });
}

/**
 * A result cell, as text.
 * @param {unknown} v
 * @returns {string}
 */
export function formatCell(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-US") : v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  if (typeof v === "string") return v;
  if (typeof v === "boolean") return v ? "true" : "false";
  return JSON.stringify(v);
}

// -- reports --------------------------------------------------------------------------------------

/**
 * @typedef {object} Report
 * @property {string} title
 * @property {string} summary
 * @property {{ heading: string; body: string }[]} sections
 */

/**
 * A publish_report call's input as a report, or why it isn't one.
 * @param {Record<string, unknown>} input
 * @returns {{ report: Report; problem: null } | { report: null; problem: string }}
 */
export function readReport(input) {
  const { title, summary, sections } = input;
  if (typeof title !== "string" || !title.trim()) return { report: null, problem: "A report needs a `title`." };
  if (!Array.isArray(sections) || !sections.every((s) => isRecord(s) && typeof s.heading === "string" && typeof s.body === "string")) {
    return { report: null, problem: "Each section needs a `heading` and a markdown `body`." };
  }
  return {
    report: {
      title,
      summary: typeof summary === "string" ? summary : "",
      sections: sections.map((s) => ({ heading: String(s.heading), body: String(s.body) }))
    },
    problem: null
  };
}

/**
 * A report's public link: this page, the session it was published from, and the report's route.
 * Opening it in this page is a hash change, so the page and its state stay; opened afresh, it
 * loads the session and finds the report on its stream.
 * @param {{ origin: string; pathname: string; search: string }} page
 * @param {string} sessionId
 * @param {string} reportId
 */
export function reportUrl(page, sessionId, reportId) {
  const params = new URLSearchParams(page.search);
  params.set("session", sessionId);
  return `${page.origin}${page.pathname}?${params}#/reports/${encodeURIComponent(reportId)}`;
}

/**
 * The report a location hash routes to, or null.
 * @param {string} hash
 * @returns {string | null}
 */
export function reportRoute(hash) {
  const m = /^#\/reports\/([^/?#]+)$/.exec(hash);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1] ?? "");
  } catch {
    return null;
  }
}

// -- the demo -------------------------------------------------------------------------------------

/**
 * The demo, as the questions a presenter sends in order: find and chart the deposit drop in open
 * data, and publish a report on it; then ask about loan defaults, which needs a person's grant, and
 * try to publish again, which the policy now refuses. Each step comes with two other plausible
 * questions, so the follow-ups read as choices rather than a script.
 * @type {readonly { text: string; others: readonly string[] }[]}
 */
export const DEMO_STEPS = [
  {
    text: "Which branches had the biggest drop in deposit volume last quarter, and is it unusual? Chart them against the rest of the network.",
    others: []
  },
  {
    text: "Publish a report on this I can share with the regional managers.",
    others: ["Is the difference statistically significant?", "Break it down by region"]
  },
  {
    text: "Do those branches also have higher loan default rates? Show how credit scores, loan amounts and defaults correlate there as a heatmap.",
    others: ["How did new accounts change at those branches?", "Summarize this for an executive email"]
  },
  {
    text: "Publish a report on the default rates at those branches for the regional managers.",
    others: ["Which loan products default most?", "Is this driven by mortgages?"]
  }
];

/** Follow-ups for a conversation that isn't on the demo's path, or has finished it. */
export const FOLLOW_UPS = ["Chart that", "Is the difference statistically significant?", "Break it down by region"];

/**
 * The follow-ups to offer after `sent`, the questions asked so far: the demo's next step first,
 * after the furthest step already asked (so a typed question in between doesn't lose the thread),
 * then its two others. Off the demo's path, or past its end, the general follow-ups.
 * @param {readonly string[]} sent
 * @returns {string[]}
 */
export function followUps(sent) {
  const asked = new Set(sent.map((t) => t.trim()));
  const reached = DEMO_STEPS.findLastIndex((step) => asked.has(step.text));
  const next = reached === -1 ? undefined : DEMO_STEPS[reached + 1];
  return next ? [next.text, ...next.others] : FOLLOW_UPS;
}

// -- text -----------------------------------------------------------------------------------------

/** @param {string} s */
const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
/** @param {string} s */
const inline = (s) =>
  s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    // Links, `[text](https://…)` and bare, open in a new tab, a report's public link included.
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)"]+)\)|(^|[\s(])(https?:\/\/[^\s<)"]*[^\s<)".,;:!?'])/g,
      (/** @type {string} */ _, /** @type {string | undefined} */ text, /** @type {string | undefined} */ url, /** @type {string | undefined} */ lead, /** @type {string | undefined} */ bare) => {
        const href = url ?? bare ?? "";
        return `${lead ?? ""}<a href="${href}" target="_blank" rel="noreferrer">${text ?? href}</a>`;
      }
    )
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");

/** @param {string} row */
const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

/**
 * A small, escaping Markdown subset: paragraphs, headings, lists, tables, code spans and fences,
 * bold and italics.
 * @param {string} text
 * @returns {string}
 */
export function markdown(text) {
  const out = [];
  for (const block of escape(text.trim()).split(/\n{2,}/)) {
    const lines = block.split("\n");
    if (block.startsWith("```")) {
      out.push(`<pre>${block.replace(/^```\w*\n?/, "").replace(/```$/, "")}</pre>`);
    } else if (lines.length >= 2 && /^\s*\|/.test(lines[0] ?? "") && /^\s*\|?\s*:?-{2,}/.test(lines[1] ?? "")) {
      const head = cells(lines[0] ?? "").map((c) => `<th>${inline(c)}</th>`).join("");
      const body = lines
        .slice(2)
        .map((row) => `<tr>${cells(row).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
        .join("");
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`);
    } else if (/^\s*([-*]|\d+\.)\s/.test(block)) {
      const items = block.split(/\n(?=\s*(?:[-*]|\d+\.)\s)/).map((i) => `<li>${inline(i.replace(/^\s*([-*]|\d+\.)\s/, ""))}</li>`);
      out.push(/^\s*\d/.test(block) ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
    } else if (/^#{1,4}\s/.test(block)) {
      out.push(`<h4>${inline(block.replace(/^#+\s/, ""))}</h4>`);
    } else {
      out.push(`<p>${inline(block).replace(/\n/g, "<br>")}</p>`);
    }
  }
  return out.join("");
}

// -- code ------------------------------------------------------------------------------------------

/**
 * highlight.js, which index.html loads from a CDN as the `hljs` global.
 * @typedef {{ highlight(code: string, options: { language: string, ignoreIllegals?: boolean }): { value: string } }} Highlighter
 */

/**
 * Code as highlighted HTML, or only escaped when highlight.js didn't load (offline).
 * @param {string} code
 * @param {"python" | "sql"} language
 * @returns {string}
 */
function highlight(code, language) {
  const hljs = /** @type {{ hljs?: Highlighter }} */ (globalThis).hljs;
  if (!hljs) return escape(code);
  try {
    return hljs.highlight(code, { language, ignoreIllegals: true }).value;
  } catch {
    return escape(code);
  }
}

/** @param {string} html */
const unescape = (html) =>
  html.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

/**
 * A query, escaped and highlighted.
 * @param {string} sql
 * @returns {string}
 */
export function highlightSql(sql) {
  return highlight(sql, "sql");
}

/** Every host function a script can call: data, access, charts and reports. */
export const HOST_FUNCTIONS = [...DATA_TOOLS, "request_access", ...CHART_TOOLS, REPORT_TOOL];

/**
 * A script, escaped and highlighted. A plain string holding a query is highlighted as SQL, and
 * an awaited call to one of `hosts` is marked as a host call.
 * @param {string} source
 * @param {readonly string[]} [hosts]
 * @returns {string}
 */
export function highlightPython(source, hosts = HOST_FUNCTIONS) {
  return highlight(source, "python")
    .replace(
      /<span class="hljs-string">([rRuU]?)(&quot;&quot;&quot;|&#x27;&#x27;&#x27;|&quot;|&#x27;)([^<]*)\2<\/span>/g,
      (whole, /** @type {string} */ prefix, /** @type {string} */ quote, /** @type {string} */ body) => {
        const sql = unescape(body);
        if (!/^\s*(SELECT|WITH)\b/i.test(sql)) return whole;
        const q = `<span class="hljs-string">${prefix}${quote}</span>`;
        return `${q}<span class="embedded-sql">${highlightSql(sql)}</span><span class="hljs-string">${quote}</span>`;
      }
    )
    .replace(/(<span class="hljs-keyword">await<\/span>\s+)([A-Za-z_]\w*)(?=\()/g, (whole, /** @type {string} */ awaited, /** @type {string} */ name) =>
      hosts.includes(name) ? `${awaited}<span class="host-call">${name}</span>` : whole
    );
}

