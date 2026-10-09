/* Dev-only fixtures for the structured-reply surfaces (JsonReply, the log's "Handler reply",
   the graph's Output node). Selected with `?mock=<name>` in a dev build; see main.ts. */
import type { AgentInterfaceFunction, AgentSseFrame, FileCitationAnnotation, Session } from "$lib/api/types";
import { agents, frame, meta, usage, type MockScenario } from "./scenarios";

interface Reply {
  /** Text the model streams before the handler returns; omitted = no reply_delta at all. */
  stream?: string;
  output?: unknown;
  /** message_handler_error instead of message_handler_end. */
  error?: string;
  chunk?: number;
  annotations?: FileCitationAnnotation[];
}

const rootId = "7f3c1a";
let clock = 0;

/* `ask` is declared prose and `clean` data, so the UI decides from the schema. `legacy` is in
   no interface — an old run — so its replies exercise the payload fallback and the strict sniff. */
const handlers: AgentInterfaceFunction[] = [
  {
    name: "ask",
    description: "Ask a free-form question.",
    parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
    output: { title: "TextReply", type: "object", properties: { text: { type: "string" } }, required: ["text"] },
    mid_turn: "enqueue",
    model_callable: true
  },
  {
    name: "clean",
    description: "Clean CRM rows into records.",
    parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
    output: {
      title: "CleanedRecords",
      type: "object",
      properties: { text: { type: "string" }, records: { type: "array" }, skipped: { type: "array" } }
    },
    mid_turn: "enqueue",
    model_callable: true
  }
];

function turn(n: number, ask: string, replies: Reply[], handler = "clean"): AgentSseFrame[] {
  const out: AgentSseFrame[] = [];
  replies.forEach((reply, index) => {
    const m = () => ({ ...meta(n, (clock += 0.5)), message_id: `msg-${n}-${index}` });
    out.push(frame("message_accepted", { type: "message_accepted", ...m(), handler, payload: { text: ask }, disposition: "opened" }));
    if (index === 0) out.push(frame("turn_started", { type: "turn_started", ...m() }));
    out.push(frame("message_handler_start", { type: "message_handler_start", ...m() }));
    out.push(frame("model_interaction_started", { type: "model_interaction_started", ...m(), model: "gpt-5.4-mini" }));
    const text = reply.stream ?? "";
    const size = reply.chunk ?? 48;
    for (let at = 0; at < text.length; at += size) {
      out.push(frame("reply_delta", { type: "reply_delta", ...m(), text: text.slice(at, at + size) }));
    }
    if (reply.annotations) out.push(frame("text_annotation", { type: "text_annotation", ...m(), delta: { annotations: reply.annotations } }));
    out.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...m(), model: "gpt-5.4-mini", usage: usage(1800, 420, 0, 600) }));
    out.push(
      reply.error !== undefined
        ? frame("message_handler_error", { type: "message_handler_error", ...m(), message: reply.error })
        : frame("message_handler_end", { type: "message_handler_end", ...m(), output: reply.output as Record<string, unknown> })
    );
  });
  out.push(frame("turn_end", { type: "turn_end", ...meta(n, (clock += 0.5)) }));
  return out;
}

const pretty = (value: unknown) => JSON.stringify(value, null, 2);

function scenario(name: string, label: string, frames: AgentSseFrame[]): MockScenario {
  const session: Session = {
    workflow_id: `agent-session-mock-json-${name}`,
    created_at: 1_789_126_400,
    label,
    agent_workflow_type: "QaAgent",
    initial_user_message: "Clean up these CRM rows."
  };
  return { agents, sessions: [session], frames, agentInterface: handlers };
}

/* Built on call: frame() advances a shared offset counter, so module-level fixtures would
   be a side effect production builds could not drop. */
export function jsonReplyScenarios(): Record<string, MockScenario> {
  const ana = { name: "Ana Souza", email: "ana@example.com", company: "Northwind", status: "active" };

  const demo = scenario("demo", "Demo JSON", turn(1, "Clean up these CRM rows.", [
    { stream: pretty({ records: [ana], skipped: [] }), output: { records: [ana], skipped: [] } }
  ]));

  const empty = scenario("empty", "Empty JSON", turn(1, "Clean up these CRM rows.", [
    { stream: "{}", output: {} },
    { stream: pretty({ records: [], skipped: [] }), output: { records: [], skipped: [] } }
  ]));

  const one = scenario("one", "One JSON", turn(1, "Clean up this CRM row.", [
    { output: { records: [{ name: "Jo", email: "a@b.co", company: "X", status: "prospect" }], skipped: ["1 row"] } }
  ]));

  /* ~1 MB: 1,000 records the size a real CRM export row reaches. Streamed minified, the way a
     model emits structured output, in 8 KB deltas. */
  const names = [
    "Aleksandra Wiśniewska-Kowalczyk", "Christopher Alexander Montgomery III", "Jo", "王秀英",
    "نور الهدى عبد الرحمن", "Đặng Thị Ngọc Hân", "👩🏽‍💻 Priya", "Seán O'Brien-Ó Súilleabháin"
  ];
  const hugeOutput = {
    records: Array.from({ length: 1000 }, (_, i) => ({
      name: names[i % names.length],
      email: `bartholomew.fitzgerald+${i}@northwind-industries-holdings.example.com`,
      company: "Northwind Industries Holdings — EMEA Shared Services (Kraków)",
      status: ["active", "churned", "prospect"][i % 3],
      notes:
        "Imported from the legacy CSV. Duplicate of an earlier row with a different casing of the email; merged phone numbers, kept the newest address, dropped the fax field. ".repeat(4),
      tags: ["import-2026-q3", "dedupe", "needs-review", "emea"]
    })),
    skipped: Array.from({ length: 40 }, (_, i) => `row ${i + 1001}: email missing`)
  };
  const huge = scenario("huge", "Huge JSON (1,000 records)", turn(1, "Clean up all 1,040 CRM rows.", [
    { stream: JSON.stringify(hugeOutput), output: hugeOutput, chunk: 8192 }
  ]));

  /* Exactly 14 lines (no fold) and 15 lines (folds), pretty-printed. */
  const keys = (count: number) => Object.fromEntries(Array.from({ length: count }, (_, i) => [`field_${i + 1}`, i]));

  let deep: unknown = { leaf: "twelve levels down" };
  for (let level = 11; level >= 1; level -= 1) deep = { [`level_${level}`]: deep };

  const kitchenSink = {
    empty_object: {},
    empty_array: [],
    nothing: null,
    yes: true,
    no: false,
    zero: 0,
    negative_zero: -0,
    float: 0.1 + 0.2,
    huge: 1.7976931348623157e308,
    tiny: 5e-324,
    beyond_safe_integer: 9007199254740993,
    "customer_lifetime_value_after_discounts_and_refunds_in_reporting_currency_including_tax_adjustments_for_prior_periods": 12345678.9,
    multiline: "Line one\nLine two\n\tindented with a tab",
    html: "<script>alert(1)</script> &amp; <b>bold</b>",
    markdown: "# Heading\n**bold** and `code` and [link](https://example.com)",
    emoji: "👩🏽‍💻 🏳️‍🌈 👨‍👩‍👧‍👦",
    cjk: "顧客データの重複を削除しました。王秀英",
    rtl: "نور الهدى عبد الرحمن — שלום עולם",
    combining: "Z̴̡̧a̷l̸g̶o̵ Đặng Thị Ngọc Hân",
    zero_width: "zero\u200bwidth\u200djoiner\ufeffbom",
    url: `https://example.com/workspaces/acme/exports/${"9f8e7d6c5b4a".repeat(160)}?tab=comments`,
    deep
  };

  const partial = '{\n  "records": [\n    {\n      "name": "Aleksandra Wiśniewska-Kowalczyk",\n      "email": "bartholomew.fitzgerald@northwind-ind';

  const subagentId = `${rootId}-c0ffee`;
  const subagentWf = "agent-session-mock-json-worst-cleaner";
  function subagentTurn(): AgentSseFrame[] {
    const parent = (d: number) => ({ ...meta(11, (clock += d)), message_id: "msg-11-0" });
    const child = () => ({ ...meta(1, (clock += 0.1)), agent_id: subagentId, turn_id: "cleaner-turn-001", message_id: "cleaner-msg-1" });
    const result = { records: [ana, { ...ana, name: "Jo", email: "a@b.co" }], skipped: ["row 3: no email"] };
    return [
      frame("message_accepted", { type: "message_accepted", ...parent(0.5), handler: "clean", payload: { text: "Ask the cleaner subagent." }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...parent(0.1) }),
      frame("subagent_started", { type: "subagent_started", ...parent(0.1), subagent_id: subagentId, agent_key: "cleaner", workflow_id: subagentWf }),
      frame("subagent_message_sent", { type: "subagent_message_sent", ...parent(0.1), subagent_id: subagentId, agent_key: "cleaner", workflow_id: subagentWf, handler: "clean", subagent_turn: 1, from_offset: 0 }),
      frame("message_accepted", { type: "message_accepted", ...child(), handler: "clean", payload: { text: "Clean these rows." }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...child() }),
      frame("reply_delta", { type: "reply_delta", ...child(), text: pretty(result) }),
      frame("message_handler_end", { type: "message_handler_end", ...child(), output: result }),
      frame("turn_end", { type: "turn_end", ...child() }),
      frame("subagent_reply_received", { type: "subagent_reply_received", ...parent(0.1), subagent_id: subagentId, agent_key: "cleaner", workflow_id: subagentWf, handler: "clean", subagent_turn: 1, outcome: "ok" }),
      frame("subagent_stopped", { type: "subagent_stopped", ...parent(0.1), subagent_id: subagentId, agent_key: "cleaner", workflow_id: subagentWf }),
      frame("message_handler_end", { type: "message_handler_end", ...parent(0.1), output: result }),
      frame("turn_end", { type: "turn_end", ...parent(0.1) })
    ];
  }

  const textAmongFields = {
    text: "Cleaned 3 rows; 1 skipped.",
    score: 0.92,
    labels: ["dedupe", "needs-review"],
    records: [ana, { ...ana, name: "Christopher Alexander Montgomery III" }],
    skipped: ["row 4: email missing"]
  };
  const footnote = "[1] See the footnote at the end: rows 4 and 9 were skipped because their email field was empty.";
  const placeholder = "{name} is a placeholder in your mail-merge template; I left it as-is in 12 rows.";

  const worst = scenario("worst", "Worst-case JSON", [
    ...turn(1, "Score and clean these rows (output has a text field).", [{ stream: pretty(textAmongFields), output: textAmongFields }]),
    ...turn(2, "Explain the plan (output has a message field).", [
      { output: { message: "Merged 2 duplicates.", next_steps: ["Review row 9", "Re-import"], confidence: 0.4 } }
    ]),
    ...turn(3, "Clean (handler returned an empty text).", [{ output: { text: "", records: [ana] } }]),
    ...turn(4, "Why were rows skipped?", [{ stream: footnote, output: { text: footnote } }], "legacy"),
    ...turn(5, "What is {name}?", [{ stream: placeholder, output: { text: placeholder } }], "legacy"),
    ...turn(6, "Clean (model fenced its JSON).", [
      { stream: "```json\n" + pretty({ records: [ana], skipped: [] }) + "\n```", output: { records: [ana], skipped: [] } }
    ], "legacy"),
    ...turn(7, "Clean (model led with a newline).", [
      { stream: "\n  " + pretty({ records: [ana], skipped: [] }), output: { records: [ana], skipped: [] } }
    ], "legacy"),
    ...turn(8, "List as a bare array.", [{ stream: pretty([ana, ana, ana]), output: [ana, ana, ana] }]),
    ...turn(9, "Every awkward value at once.", [{ stream: pretty(kitchenSink), output: kitchenSink, chunk: 400 }]),
    ...turn(10, "Several replies in one turn: {}, empty arrays, 14 lines, 15 lines.", [
      { output: {} },
      { output: { records: [], skipped: [], next_cursor: null, done: true, count: 0 } },
      { stream: pretty(keys(12)), output: keys(12) },
      { stream: pretty(keys(13)), output: keys(13) }
    ]),
    ...subagentTurn(),
    ...turn(12, "Clean (handler fails mid-stream).", [
      { stream: partial, error: "ValidationError: 1 validation error for CleanedRecords\nrecords.0.status\n  Input should be 'active', 'churned' or 'prospect'" }
    ])
  ]);

  /* Prose replies streamed in small chunks, so scrubbing lands mid-marker, mid-fence and mid-link. */
  const hostile = [
    "## Plan — streaming worst case",
    "",
    "Hours are 9 to 5 on weekdays. Intro with **bold**, _em_, `inline code`, a [safe link](https://example.com) and a [blocked link](javascript:alert(1)).",
    "",
    "- first item",
    `- an unbroken token ${"A".repeat(300)}`,
    "- <script>alert(1)</script> stays text",
    "",
    "1. Install:",
    "    ```bash",
    "    pip install temporal-agent-harness",
    "    ```",
    "2. Run it",
    "",
    "~~~ts",
    'const s = text.replace(/\'/g, "");',
    "const n: number = 1; // still a keyword after the regex",
    "~~~",
    "",
    "שלום עולם — هذا نص عربي في فقرة كاملة.",
    "",
    "| Col | Value |",
    "|---|---|",
    "| **a** | 1 |",
    "",
    "````md",
    "```js",
    "inner fence stays code",
    "```",
    "````"
  ].join("\n");
  const citations: FileCitationAnnotation[] = [
    { type: "file_citation", file_name: "hours.md", document_uri: "https://example.com/hours", start_index: 32, end_index: 61, custom_metadata: { heading: "Opening hours" } }
  ];
  const codeHeavy = Array.from({ length: 40 }, (_, i) =>
    `### Step ${i + 1}\n\nRun this:\n\n\`\`\`${["ts", "python", "json", "bash"][i % 4]}\n${Array.from({ length: 12 }, (_, j) => `const v${j} = await load("item-${i}-${j}", { retries: ${j} }); // ${i}.${j}`).join("\n")}\n\`\`\`\n`
  ).join("\n");
  const markdown = scenario("markdown", "Markdown replies", [
    ...turn(1, "Show me every awkward markdown shape.", [{ stream: hostile, output: { text: hostile }, chunk: 7, annotations: citations }], "ask"),
    ...turn(2, "Forty code blocks, please.", [{ stream: codeHeavy, output: { text: codeHeavy }, chunk: 400 }], "ask")
  ]);

  return { demo, worst, empty, one, huge, markdown };
}
