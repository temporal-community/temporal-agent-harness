/* Dev-only fixtures for the structured-reply surfaces (JsonReply, the log's "Handler reply",
   the graph's Output node). Selected with `?mock=<name>` in a dev build; see main.ts. */
import type { AgentInterfaceFunction, AgentSseFrame, FileCitationAnnotation, Session, TokenUsage } from "$lib/api/types";
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
    description: "Ask a free-form **question**; the reply is `TextReply` prose.",
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
  const attachments = {
    ...empty,
    sessions: empty.sessions.map((session) => ({ ...session, workflow_id: `${session.workflow_id}-attachments`, label: "Attachments" })),
    agentInterface: [
      {
        name: "ask_with_files",
        description: "Ask with supporting files.",
        parameters: {
          type: "object",
          properties: {
            text: { type: "string", title: "Text" },
            attachments: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  type: { enum: ["image", "file"] },
                  name: { type: "string" },
                  mediaType: { type: "string" },
                  data: { type: "string", contentEncoding: "base64" }
                }
              }
            }
          },
          required: ["text"]
        },
        output: handlers[0]!.output,
        mid_turn: "enqueue",
        model_callable: true
      }
    ]
  } satisfies MockScenario;
  const script = {
    ...empty,
    sessions: empty.sessions.map((session) => ({ ...session, workflow_id: `${session.workflow_id}-script`, label: "Script composer" })),
    agentInterface: [
      {
        name: "run_script",
        description: "Execute a Python script.",
        parameters: {
          type: "object",
          properties: { script: { type: "string", title: "Script" } },
          required: ["script"]
        },
        output: handlers[0]!.output,
        mid_turn: "enqueue",
        model_callable: true
      }
    ]
  } satisfies MockScenario;
  const queue = scenario("queue", "Queued messages", [
    frame("message_accepted", {
      type: "message_accepted",
      ...meta(1, 0),
      message_id: "queue-current",
      handler: "ask",
      payload: { text: "Start a deliberately long reply." },
      disposition: "opened"
    }),
    frame("turn_started", { type: "turn_started", ...meta(1, 0) }),
    frame("model_interaction_started", {
      type: "model_interaction_started",
      ...meta(1, 1),
      model: "gpt-5.4-mini"
    }),
    frame("message_accepted", {
      type: "message_accepted",
      ...meta(2, 2),
      message_id: "queue-follow-up",
      handler: "ask",
      payload: { text: "Follow up after this reply with the deployment checklist." },
      disposition: "queued"
    })
  ]);

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

  /* The token chart's worst cases, one model call each: an unbroken 300-character model id,
     nine-digit counts, all zeros, no usage at all, a call that took no time, one whose start
     was never seen (so no throughput), every token category, and a decision model's call. */
  function usageTurn(n: number): AgentSseFrame[] {
    const m = (d = 0.5) => ({ ...meta(n, (clock += d)), message_id: `msg-${n}-0` });
    const call = (model: string, spent: TokenUsage | null, took = 2, started = true) => [
      ...(started ? [frame("model_interaction_started", { type: "model_interaction_started", ...m(), model })] : []),
      frame("model_interaction_ended", { type: "model_interaction_ended", ...m(took), model, usage: spent })
    ];
    return [
      frame("message_accepted", { type: "message_accepted", ...m(), handler: "ask", payload: { text: "Spend tokens every awkward way." }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...m() }),
      frame("message_handler_start", { type: "message_handler_start", ...m() }),
      ...call(`models/${"Gemini38FlashExperimental".repeat(12)}`, usage(3100, 269, 0, 0)),
      ...call("gpt-5.4-mini", usage(987_654_321, 123_456_789, 98_765_432, 876_543_210, 45_678_901), 40),
      ...call("gpt-5.4-mini", usage(0, 0, 0, 0, 0)),
      ...call("gpt-5.4-mini", null),
      ...call("gemini-3.8-flash", usage(3100, 269, 120, 900, 60), 0),
      ...call("نموذج‌عربي 🧑‍💻 模型 <b>x</b>", usage(2400, 310, 80, 0, 0), 2, false),
      frame("auto_approval_evaluation_ended", {
        type: "auto_approval_evaluation_ended",
        ...m(),
        tool_id: "tool-usage-eval",
        tool_name: "spend_tokens",
        evaluation_id: "eval-usage-1",
        evaluator: "jev_evaluator",
        verdict: "approve",
        reason: "Read-only.",
        details: { verdict: "approve", model: "gemini-3.5-flash", usage: { input_tokens: 820, output_tokens: 40, total_tokens: 860 } }
      }),
      frame("message_handler_end", { type: "message_handler_end", ...m(), output: { text: "Spent." } }),
      frame("turn_end", { type: "turn_end", ...meta(n, (clock += 0.5)) })
    ];
  }

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
    ]),
    ...usageTurn(13)
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
  /* Markers stream in before their item's text, numbered items are split by nested bullets and
     blank lines, and a later list starts at 7. */
  const lists =
    "Here's the rollout plan:\n\n1. **Prepare**\n   - Back up the database\n   - Freeze deploys\n\n" +
    "2. **Ship**\n   - Deploy to staging\n     - Smoke test `/healthz`\n     - Check the error rate\n   - Promote to production\n\n" +
    "3. **Verify**\n\nThen, next week:\n\n7. Seventh step keeps its number\n8. Eighth\n\n- [ ] open task\n- [x] done task\n\n" +
    "1) Paren style\n2) Second\n\n+ plus bullet\n+ another\n\nDone.";
  /* Markdown outside the reply: agent and handler descriptions, the session's first message, a
     thought summary, a question being approved, an evaluator's reason and a mount description. */
  const at = () => ({ ...meta(4, (clock += 0.5)), message_id: "msg-4-0" });
  const plan = "Plan the **Lisbon** offsite and `book` it — see [the brief](https://example.com/brief)";
  const markdown = scenario("markdown", "Markdown replies", [
    ...turn(1, "Show me every awkward markdown shape.", [{ stream: hostile, output: { text: hostile }, chunk: 7, annotations: citations }], "ask"),
    ...turn(2, "Forty code blocks, please.", [{ stream: codeHeavy, output: { text: codeHeavy }, chunk: 400 }], "ask"),
    ...turn(3, "Lay out the rollout as lists.", [{ stream: lists, output: { text: lists }, chunk: 2 }], "ask"),
    frame("message_accepted", { type: "message_accepted", ...at(), handler: "ask", payload: { text: plan }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...at() }),
    frame("state_snapshot", {
      type: "state_snapshot",
      ...at(),
      state_id: "workspace",
      version: 0,
      value: { kind: "file_tree", mount: "/workspace", description: "Scratch space for **drafts**; `README.md` is kept.", directories: [], files: { "README.md": { content: "# Offsite\n\n- **Where:** Lisbon\n- *When:* May", size: 40 } } }
    }),
    frame("model_interaction_started", { type: "model_interaction_started", ...at(), model: "gpt-5.4-mini" }),
    frame("thought_summary", {
      type: "thought_summary",
      ...at(),
      delta: { content: { text: "**Weighing the options**\n\nCompare `TAP` and *Lufthansa* fares:\n\n- direct flights first\n- then price" } }
    }),
    frame("auto_approval_evaluation_ended", {
      type: "auto_approval_evaluation_ended",
      ...at(),
      tool_id: "tool-md-search",
      tool_name: "search_fares",
      evaluation_id: "eval-md-1",
      evaluator: "jev_evaluator",
      verdict: "approve",
      reason: "**Read-only.** `search_fares` only queries prices:\n\n- no booking\n- no <script>payment</script> data",
      details: { verdict: "approve", confidence: 0.9, model: "gemini-3.5-flash" }
    }),
    frame("tool_requested", { type: "tool_requested", ...at(), tool_id: "tool-md-ask", tool_name: "ask_user", tool_input: { question: "Book **TAP 1234** to Lisbon?\n\n- window seat\n- `€312` total" } }),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...at(), tool_id: "tool-md-ask", tool_name: "ask_user", tool_input: { question: "Book **TAP 1234** to Lisbon?\n\n- window seat\n- `€312` total" } })
  ]);
  markdown.agents = agents.map((agent) => ({ ...agent, description: `${agent.description} Uses a *durable* **workflow** per session; see \`agents.toml\`.` }));
  markdown.sessions = [{ ...markdown.sessions[0]!, initial_user_message: plan }];

  const pastedScript = Array.from({ length: 200 }, (_, i) => `print("pasted line ${i + 1}: ${i % 3 ? "שלום עולם" : "مرحبا بالعالم"}")`).join("\n");
  const longMarkdown = Array.from(
    { length: 300 },
    (_, i) => `${i + 1}. **Rendered line ${i + 1}** — ${i % 4 === 0 ? "שלום עולם" : i % 4 === 1 ? "مرحبا بالعالم" : i % 4 === 2 ? "顧客データ" : "<script>literal text</script>"}`
  ).join("\n");
  const toolOutput = JSON.stringify({
    rows: Array.from({ length: 500 }, (_, i) => ({ id: i, path: `/workspace/${"segment/".repeat(8)}record-${i}.json`, value: "x".repeat(80) }))
  });
  const longAt = (turnNumber: number, delta = 0.2) => ({ ...meta(turnNumber, (clock += delta)), message_id: `long-${turnNumber}` });
  const longcontent = scenario("longcontent", "Long content", [
    frame("message_accepted", { type: "message_accepted", ...longAt(1), handler: "ask", payload: { text: pastedScript }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...longAt(1) }),
    frame("message_handler_start", { type: "message_handler_start", ...longAt(1) }),
    frame("model_interaction_started", { type: "model_interaction_started", ...longAt(1), model: "gpt-5.4-mini" }),
    frame("tool_requested", { type: "tool_requested", ...longAt(1), tool_id: "long-tool", tool_name: "inspect_records", tool_input: { script: pastedScript } }),
    frame("tool_start", { type: "tool_start", ...longAt(1), tool_id: "long-tool", tool_name: "inspect_records", tool_input: { script: pastedScript } }),
    frame("tool_end", { type: "tool_end", ...longAt(1), tool_id: "long-tool", tool_name: "inspect_records", tool_output: toolOutput }),
    frame("reply_delta", { type: "reply_delta", ...longAt(1), text: longMarkdown }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...longAt(1), model: "gpt-5.4-mini", usage: usage(1800, 420, 0, 600) }),
    frame("message_handler_end", { type: "message_handler_end", ...longAt(1), output: { text: longMarkdown } }),
    frame("turn_end", { type: "turn_end", ...meta(1, (clock += 0.2)) }),
    frame("message_accepted", { type: "message_accepted", ...longAt(2), handler: "ask", payload: { text: "Keep streaming this reply." }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...longAt(2) }),
    frame("message_handler_start", { type: "message_handler_start", ...longAt(2) }),
    frame("model_interaction_started", { type: "model_interaction_started", ...longAt(2), model: "gpt-5.4-mini" }),
    frame("reply_delta", { type: "reply_delta", ...longAt(2), text: longMarkdown.slice(0, 8_000) })
  ]);

  /* Images: tools that return a blob_ref and an inline image part, a blob the API no longer has, an
     RTL alt, an inline image over the size cap, then a streamed reply holding an inline PNG beside
     a remote image and an SVG that must never load. Only the checkered blob is served (by the QA
     dev server); the zero-hash one fails like an expired blob. */
  const checkered =
    "iVBORw0KGgoAAAANSUhEUgAAAKAAAACgCAIAAAAErfB6AAABl0lEQVR42u3csQkAIAxFwYxg48CO5Q5OZJUdUgSUA/sP79pgjL3K755ZfnZ7dkNowEIDFhowYMB2AdsFbBcwYKEBCw1YaMB2AdsFbBcwYKEBCw1YaMCAW4HF+nsXMGChAQsNGDBgu4DtArYLGLDQgIUGLDRgu4DtArYLGLDQgIUGLDRgwL3AYrmqFBqw0IABA7YL2C5gu4ABCw1YaMBCA7YL2C5gu4ABCw1YaMBCAwYM2K5/suy6qrQLGLDQgIUGLDRgu4DtArYLGLDQgIUGLDRgu4DtArYLGLDQgIUGLLR/siC5qrQL2C5gu4ABCw1YaMBCA7YL2C5gu4ABCw1YaMBCAwYM2C5gu4ABC+2fLLFcVQoNGDBgu4DtArYLGLDQgIUGLDRgu4DtArYLGLDQgIUGLDRgwIDtArbrnyy7rioBCw1YaMBCA7YL2C5gu4ABCw1YaMBCA7YL2C5gu4ABCw1YaMBCAwbsnyy7rirtArYLGLDQgIUGLDRgu4DtArYLGLDQgIUGLDRgu4DtArYLGLDQgIV+djcBB/FDNLHVarsAAAAASUVORK5CYII=";
  const chart =
    "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";
  const checkeredBlob = "/api/blobs/ce9d2a52f9f5ea24736f8cacae021acc0e9bfde3e2f7b7d2c7b20c4e0a4a85a0.bin";
  const goneBlob = `/api/blobs/${"0".repeat(64)}.bin`;
  const blobRef = (url: string) => ({ blob_ref: { key: url.split("/").at(-1), bytes: 465, media_type: "image/png", url } });
  function toolTurn(n: number): AgentSseFrame[] {
    const m = (d = 0.3) => ({ ...meta(n, (clock += d)), message_id: `msg-${n}-0` });
    const tool = (id: string, name: string, input: Record<string, unknown>, output: unknown) => [
      frame("tool_requested", { type: "tool_requested", ...m(), tool_id: id, tool_name: name, tool_input: input }),
      frame("tool_start", { type: "tool_start", ...m(), tool_id: id, tool_name: name, tool_input: input }),
      frame("tool_end", { type: "tool_end", ...m(1.2), tool_id: id, tool_name: name, tool_output: JSON.stringify(output) })
    ];
    const reply = "Generated a checkered tile and took a screenshot. One older render has expired.";
    return [
      frame("message_accepted", { type: "message_accepted", ...m(), handler: "ask", payload: { text: "Make a checkered tile and screenshot it." }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...m() }),
      frame("message_handler_start", { type: "message_handler_start", ...m() }),
      ...tool("tool-image-gen", "image_generation", { prompt: "a checkered tile" }, { status: "completed", result: blobRef(checkeredBlob), previous: blobRef(goneBlob) }),
      ...tool("tool-screenshot", "take_screenshot", { url: "about:blank" }, { content: [{ type: "text", text: "Captured 160×160." }, { type: "image", data: checkered, mimeType: "image/png" }] }),
      frame("reply_delta", { type: "reply_delta", ...m(), text: reply }),
      frame("message_handler_end", { type: "message_handler_end", ...m(), output: { text: reply } }),
      frame("turn_end", { type: "turn_end", ...meta(n, (clock += 0.5)) })
    ];
  }
  const rtl = `هذا هو الرسم البياني الأسبوعي:\n\n![مخطط الاشتراكات الأسبوعية — ${"تسمية طويلة جدًا ".repeat(6)}](data:image/png;base64,${chart})\n\nשלום עולם.`;
  const tooLarge = `This one is too big to preview:\n\n![The full-size render](data:image/png;base64,${"A".repeat(8_000_000)})`;
  const reply =
    `Here is the weekly signups chart:\n\n![Weekly signups, last 12 weeks](data:image/png;base64,${chart}) ` +
    "The pink bar is this week: 340 signups, up 13% on last week and the highest of the twelve weeks shown. " +
    "Weeks five and seven dipped after the pricing page change, and the climb since week eight follows the onboarding email. " +
    "This caption streams in the same paragraph as the image, so the image's block is re-rendered on every delta.\n\n" +
    "A remote image that must not load: ![Remote tracking pixel](https://tracker.example/pixel.png?session=7f3c1a)\n\n" +
    "An SVG that must not render: ![Inline SVG with a script](data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIG9ubG9hZD0iYWxlcnQoMSkiLz4=)\n\n" +
    `And a render the API no longer has: ![Expired render](${goneBlob})`;
  const images = scenario("images", "Images", [
    ...toolTurn(1),
    ...turn(2, "Show the chart with an Arabic caption, then the full-size render.", [{ stream: rtl, output: { text: rtl }, chunk: 600 }, { output: { text: tooLarge } }], "ask"),
    ...turn(3, "Show me the weekly signups chart.", [{ stream: reply, output: { text: reply }, chunk: 160 }], "ask")
  ]);

  return { demo, worst, empty, one, huge, markdown, longcontent, images, attachments, queue, script };
}
