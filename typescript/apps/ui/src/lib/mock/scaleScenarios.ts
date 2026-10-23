/* Dev-only fixtures for production-scale load: a session list and a transcript far longer than
   any demo. Selected with `?mock=<name>` in a dev build; see main.ts. */
import type { AgentDescriptor, AgentInterfaceFunction, AgentSseFrame, JsonRecord, OKFConcept, OKFGraph, OKFLink, Session, SubagentUnavailableCode } from "$lib/api/types";
import { agents, frame, meta, usage, type MockScenario } from "./scenarios";

const startedAt = 1_789_126_400;

const firstMessages = [
  "When should I use a local activity versus a normal activity?",
  "Christopher Alexander Montgomery III asked why the Kraków EMEA shared-services export keeps timing out after 30 minutes of retries",
  "王秀英: 顧客データの重複を削除してください",
  "نور الهدى عبد الرحمن — لماذا فشل سير العمل؟",
  "👩🏽‍💻 Priya: book Lisbon → Tokyo, 3 nights, window seat",
  "x",
  `Trace ${"9f8e7d6c5b4a".repeat(40)}`,
  "<script>alert(1)</script> **bold** # heading"
];
const statuses = ["RUNNING", "COMPLETED", "FAILED", "TERMINATED", "CANCELED", "TIMED_OUT", "NOT_FOUND"];

function sessionList(count: number): Session[] {
  return Array.from({ length: count }, (_, i) => ({
    workflow_id: `agent-session-scale-${String(i).padStart(5, "0")}`,
    created_at: startedAt - i * 37,
    label: `Session ${i + 1}`,
    agent_workflow_type: i % 3 === 0 ? "MontyDynamicAgent" : "QaAgent",
    initial_user_message: firstMessages[i % firstMessages.length],
    execution_status: i === 0 ? "RUNNING" : statuses[i % statuses.length],
    closed: i !== 0 && i % statuses.length !== 0
  }));
}

const prose = (n: number) =>
  `### Step ${n}\n\nUse a **local activity** when the work is short and idempotent; a normal activity when it needs its own retry policy or heartbeats. See [the docs](https://docs.temporal.io/activities).\n\n- retries are per attempt\n- heartbeats need a normal activity\n\n\`\`\`python\n@activity.defn\nasync def step_${n}(input: str) -> str:\n    return input.upper()\n\`\`\`\n`;

/* One user message and one reply per turn, so 2,500 turns make 5,000 transcript messages. Every
   tenth reply is structured, every fiftieth turn asks for an approval, and the last reply is left
   open so the transcript is still streaming when the backlog has landed. */
function longTranscript(turns: number, tail = 400): AgentSseFrame[] {
  const out: AgentSseFrame[] = [];
  let clock = 0;
  for (let n = 1; n <= turns; n += 1) {
    const m = () => meta(n, (clock += 0.2));
    const structured = n % 10 === 0;
    const handler = structured ? "clean" : "ask";
    out.push(frame("message_accepted", { type: "message_accepted", ...m(), handler, payload: { text: firstMessages[n % firstMessages.length] }, disposition: "opened" }));
    out.push(frame("turn_started", { type: "turn_started", ...m() }));
    out.push(frame("message_handler_start", { type: "message_handler_start", ...m() }));
    out.push(frame("model_interaction_started", { type: "model_interaction_started", ...m(), model: "gpt-5.4-mini" }));
    if (n % 50 === 0) {
      const tool = { tool_id: `tool-scale-${n}`, tool_name: "write_session_note", tool_input: { title: `Note ${n}` } };
      out.push(frame("tool_requested", { type: "tool_requested", ...m(), ...tool }));
      out.push(frame("tool_approval_requested", { type: "tool_approval_requested", ...m(), ...tool }));
    }
    const output = structured ? { records: [{ name: "Ana Souza", email: "ana@example.com", turn: n }], skipped: [] } : { text: prose(n) };
    const text = structured ? JSON.stringify(output, null, 2) : prose(n);
    out.push(frame("reply_delta", { type: "reply_delta", ...m(), text }));
    if (n === turns && tail > 0) {
      for (let i = 0; i < tail; i += 1) {
        out.push(frame("reply_delta", { type: "reply_delta", ...m(), text: ` still streaming ${i}.` }));
      }
      break;
    }
    out.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...m(), model: "gpt-5.4-mini", usage: usage(1800, 420, 0, 600) }));
    out.push(frame("message_handler_end", { type: "message_handler_end", ...m(), output }));
    out.push(frame("turn_end", { type: "turn_end", ...m() }));
  }
  return out;
}

/* Everything but the 400-delta tail replays at once, the way a reattach backfills. */
function streamingTranscript(turns: number, label: string): MockScenario {
  const frames = longTranscript(turns);
  return {
    agents,
    /* One id per size, so a run cached from one fixture is never restored into the other, and a
       Q&A agent, so boot reopens this session rather than creating a new one on every load. */
    sessions: [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-transcript-${turns}`, agent_workflow_type: "QaAgent", label }],
    frames,
    instantFrames: frames.length - 400
  };
}

const toolNames = [
  "search_docs",
  "read_file",
  `fetch_krakow_emea_shared_services_export_${"with_retries_".repeat(8)}`,
  "検索ドキュメント",
  "قراءة_الملف",
  "👩🏽‍💻_run"
];

/* Three turns, the middle one dense: `rounds` rounds of one model call then three overlapping
   tool calls, so 12,500 rounds are 50,000 spans (~100k frames) in a single row. Every 997th tool
   fails and the last round's tools never end, so the row is held open twice over. frames100k is
   the many-turn case: 12,500 rows. */
function denseTurns(rounds: number): AgentSseFrame[] {
  const out: AgentSseFrame[] = [];
  let clock = 0;
  const m = (n: number, dt = 0.05) => meta(n, (clock += dt));
  const small = (n: number) => {
    out.push(frame("message_accepted", { type: "message_accepted", ...m(n), handler: "ask", payload: { text: firstMessages[n % firstMessages.length] }, disposition: "opened" }));
    out.push(frame("turn_started", { type: "turn_started", ...m(n) }));
    out.push(frame("model_interaction_started", { type: "model_interaction_started", ...m(n), model: "gpt-5.4-mini" }));
    out.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...m(n, 2), model: "gpt-5.4-mini", usage: usage(1800, 420, 0, 600) }));
    out.push(frame("turn_end", { type: "turn_end", ...m(n) }));
  };
  small(1);
  out.push(frame("message_accepted", { type: "message_accepted", ...m(2), handler: "ask", payload: { text: firstMessages[1] }, disposition: "opened" }));
  out.push(frame("turn_started", { type: "turn_started", ...m(2) }));
  for (let r = 0; r < rounds; r += 1) {
    out.push(frame("model_interaction_started", { type: "model_interaction_started", ...m(2, 0.02), model: "gpt-5.4-mini" }));
    out.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...m(2, 0.3), model: "gpt-5.4-mini", usage: usage(900, 60, 0, 300) }));
    const ids = [0, 1, 2].map((k) => ({ tool_id: `dense-${r}-${k}`, tool_name: toolNames[(r + k) % toolNames.length] }));
    for (const tool of ids) out.push(frame("tool_start", { type: "tool_start", ...m(2, 0.01), ...tool, tool_input: {} }));
    if (r === rounds - 1) break;
    ids.forEach((tool, k) => {
      const at = m(2, 0.1 + ((r * 7 + k * 13) % 40) / 100);
      if ((r * 3 + k) % 997 === 0) out.push(frame("tool_error", { type: "tool_error", ...at, ...tool, message: "Upstream 503 after 3 attempts" }));
      else out.push(frame("tool_end", { type: "tool_end", ...at, ...tool, tool_output: "{}" }));
    });
  }
  small(3);
  /* Marked as a reattach backfill, so it commits on the catch-up schedule rather than per paint. */
  for (const item of out) Object.assign(item.data, { replay: true });
  return out;
}

/* An OKF bundle of `count` concepts across eight types: each links to the concept a third of
   its index down (a tree) and to one pseudo-random other, and every 20th also links to a concept
   that does not exist, so ~5% more nodes are ghosts. Titles cycle through the worst cases. */
function okfBundle(count: number): OKFGraph {
  const types = ["Person", "Plan", "Preference", "Project", "Place", "Decision", "Tool", "Unknown"];
  const id = (i: number) => `${types[i % types.length]!.toLowerCase()}/c-${i}`;
  const concepts: OKFConcept[] = Array.from({ length: count }, (_, i) => ({
    id: id(i),
    path: `${id(i)}.md`,
    type: types[i % types.length]!,
    title: i % 50 === 0 ? firstMessages[(i / 50) % firstMessages.length]! : `Concept ${i}`,
    description: "",
    tags: i % 7 === 0 ? ["scale"] : [],
    status: i % 31 === 0 ? "deprecated" : i % 17 === 0 ? "draft" : "stable",
    trust_tier: i % 11 === 0 ? "human-reviewed" : "unverified",
    stale: i % 13 === 0,
    size: (i * 97) % 4000,
    problem: i % 101 === 0 ? "Frontmatter is missing a type" : null
  }));
  const links: OKFLink[] = [];
  for (let i = 1; i < count; i += 1) {
    links.push({ source: id(i), target: id(Math.floor(i / 3)), dangling: false });
    const other = (i * 7919) % count;
    if (other !== i && other !== Math.floor(i / 3)) links.push({ source: id(i), target: id(other), dangling: false });
    if (i % 20 === 0) links.push({ source: id(i), target: `missing/ghost-${i}`, dangling: true });
  }
  return { concepts, links, truncated: false, walked_at: startedAt };
}

/* One settled turn that publishes an OKF bundle mount; open Files and its graph to draw it. */
function okfScenario(count: number): MockScenario {
  const frames = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: "Map the bundle" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("state_snapshot", {
      type: "state_snapshot",
      ...meta(1, 2),
      state_id: "memory",
      version: 0,
      value: {
        kind: "file_index",
        mount: "/memory",
        description: `${count.toLocaleString()}-concept bundle`,
        read_only: true,
        okf_version: "0.2",
        source: { filesystem: "local-disk", config: { directory: "scale" }, task_queue: "q" },
        entries: {}
      }
    }),
    frame("turn_end", { type: "turn_end", ...meta(1, 3) })
  ];
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-okf${count}` }];
  return { agents, sessions, frames, instantFrames: frames.length, okfGraph: okfBundle(count) };
}

const approvalInputs = [
  { path: "/srv/reports/q3.csv" },
  { query: `${"9f8e7d6c5b4a".repeat(30)}` },
  { recipient: "نور الهدى عبد الرحمن", note: "لماذا فشل سير العمل؟" },
  { title: "<script>alert(1)</script> **bold** # heading", body: "line one\n\tline two" },
  {},
  { [`key_${"x".repeat(300)}`]: `https://example.com/${"a1b2c3d4".repeat(250)}`, nested: { deep: { deeper: { deepest: [1, 2, 3] } } } },
  /* A Code Mode script taller than the card's cap, so the queue scrolls between its sticky header and footer. */
  {
    script: [
      "import asyncio",
      "",
      "async def main():",
      '    trip_id = "TRIP-7379C8"',
      "",
      "    # Complete the search task",
      '    await complete_trip_tasks({"trip_id": trip_id, "tasks": ["Search flights from LAS to ABQ"]})',
      "",
      ...Array.from({ length: 12 }, (_, i) => [
        `    # Book flight FL-LASABQ-00${i} for 10 passengers`,
        `    booking_${i} = await book_flight({"flight_id": "FL-LASABQ-00${i}", "passengers": [f"Traveler {n + 1}" for n in range(10)]})`,
        ""
      ]).flat(),
      "asyncio.run(main())"
    ].join("\n")
  }
];

/* One answered turn, then a turn blocked on `count` approvals, its tool names and arguments
   cycling through the worst cases. Answering one streams its `tool_approval_resolved`. */
function approvalsScenario(count: number): MockScenario {
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: firstMessages[0] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("reply_delta", { type: "reply_delta", ...meta(1, 2), text: prose(1) }),
    frame("message_handler_end", { type: "message_handler_end", ...meta(1, 3), output: { text: prose(1) } }),
    frame("turn_end", { type: "turn_end", ...meta(1, 3) }),
    frame("message_accepted", { type: "message_accepted", ...meta(2, 4), handler: "ask", payload: { text: firstMessages[1] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(2, 4) })
  ];
  for (let i = 0; i < count; i += 1) {
    const tool = { tool_id: `tool-approval-${i}`, tool_name: toolNames[i % toolNames.length]!, tool_input: approvalInputs[i % approvalInputs.length]! };
    frames.push(frame("tool_requested", { type: "tool_requested", ...meta(2, 5 + i * 0.01), ...tool }));
    frames.push(frame("tool_approval_requested", { type: "tool_approval_requested", ...meta(2, 5 + i * 0.01), ...tool }));
  }
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-approvals${count}`, agent_workflow_type: "QaAgent", label: `${count} pending approvals` }];
  return { agents, sessions, frames, instantFrames: frames.length, answersApprovals: true };
}

/* A Code Mode script waiting on its approval; over the live stream it is approved, and its host
   calls at once ask for `count` approvals in one burst, each escalated by Jev, the way a group
   booking fans out. The burst lands while the answered queue is still closing. */
function approvalBurstScenario(count: number): MockScenario {
  const host = { tool_id: "burst-host", tool_name: "run_travel_code", tool_input: { script: "for n in range(10):\n    book_flight(...)" } };
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: "Book flights for 10 people from Las Vegas to New York" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("tool_requested", { type: "tool_requested", ...meta(1, 2), ...host }),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...meta(1, 2), ...host })
  ];
  const instantFrames = frames.length;
  frames.push(
    frame("tool_approval_resolved", { type: "tool_approval_resolved", ...meta(1, 3), tool_id: host.tool_id, tool_name: host.tool_name, approved: true, reason: null, remember: false }),
    frame("tool_start", { type: "tool_start", ...meta(1, 3), ...host })
  );
  const calls = Array.from({ length: count }, (_, i) => ({
    tool_id: `burst-book-${i}`,
    tool_name: "book_flight",
    tool_input: { request: { flight_id: "FL-LASJFK-001", passenger_name: `Traveler ${i + 1}` } }
  }));
  const judged = (tool: (typeof calls)[number]) => ({ tool_id: tool.tool_id, tool_name: tool.tool_name, evaluation_id: `eval-${tool.tool_id}`, evaluator: "jev_evaluator" });
  for (const tool of calls) {
    frames.push(
      frame("tool_approval_requested", { type: "tool_approval_requested", ...meta(1, 4), ...tool }),
      frame("auto_approval_evaluation_started", { type: "auto_approval_evaluation_started", ...meta(1, 4), ...judged(tool) })
    );
  }
  for (const tool of calls) {
    frames.push(frame("auto_approval_evaluation_ended", { type: "auto_approval_evaluation_ended", ...meta(1, 5), ...judged(tool), verdict: "escalate", reason: "jev/jev-1.13.0: verdict=escalate", details: { verdict: "escalate" } }));
  }
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-burst${count}`, agent_workflow_type: "QaAgent", label: `${count} approvals arriving live` }];
  return { agents, sessions, frames, instantFrames, answersApprovals: true };
}

const longToken = "9f8e7d6c5b4a".repeat(40);
const verdicts = ["approve", "deny", "escalate"] as const;

/* What the State, Files and Decisions panes read: a declared state, an in-memory mount, an OKF
   bundle mount and completed approval evaluations. `worst` swaps every name for the worst cases
   and multiplies the counts; the small run declares one state, so the state bar has no choice. */
/* `count` sizes the decisions, files and plan steps at once, and adds a commit touching every step. */
function panesScenario(worst: boolean, count?: number): MockScenario {
  const files: Record<string, { content: string; size: number }> = {};
  const fileCount = count ?? (worst ? 300 : 8);
  for (let i = 0; i < fileCount; i += 1) {
    const name = worst && i % 9 === 0 ? `${longToken}.py` : worst && i % 9 === 1 ? "ملف_عربي.md" : `file_${i}.py`;
    files[`dir_${i % (worst ? 12 : 2)}/${i}-${name}`] = { content: `print(${i})\n`, size: i * 1200 };
  }
  const steps = Array.from({ length: count ?? (worst ? 120 : 3) }, (_, i) => ({ name: worst && i % 7 === 0 ? firstMessages[i % firstMessages.length]! : `step ${i}`, done: i < 2 }));
  const plan = { goal: worst ? firstMessages[1]! : "Ship it", steps, ...(worst ? { "مفتاح": "قيمة عربية", token: longToken } : {}) };
  const firstFile = Object.keys(files)[0]!;
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: firstMessages[0] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("state_snapshot", { type: "state_snapshot", ...meta(1, 2), state_id: "plan", version: 0, value: { ...plan, steps: steps.map((s) => ({ ...s, done: false })) } }),
    ...(worst
      ? [frame("state_snapshot", { type: "state_snapshot", ...meta(1, 2), state_id: `notes_${longToken}`, version: 0, value: { notes: [] } })]
      : []),
    frame("state_snapshot", {
      type: "state_snapshot",
      ...meta(1, 2),
      state_id: "workspace",
      version: 0,
      value: { kind: "file_tree", mount: worst ? `/workspace/${longToken}` : "/workspace", description: worst ? firstMessages[2]! : "Scratch space", directories: [], files }
    }),
    frame("state_snapshot", {
      type: "state_snapshot",
      ...meta(1, 2),
      state_id: "memory",
      version: 0,
      value: {
        kind: "file_index",
        mount: "/memory",
        description: worst ? firstMessages[2]! : "Long-term notes",
        read_only: true,
        okf_version: "0.2",
        source: { filesystem: "local-disk", config: { directory: "notes" }, task_queue: "q" },
        entries: { "people/ana.md": { is_dir: false, size: 900 } }
      }
    }),
    frame("state_patch", { type: "state_patch", ...meta(1, 3), state_id: "plan", version: 1, ops: [{ op: "replace", path: "/steps/0/done", value: true }, { op: "replace", path: "/steps/1/done", value: true }] }),
    frame("state_patch", { type: "state_patch", ...meta(1, 3), state_id: "workspace", version: 1, ops: [{ op: "replace", path: `/files/${firstFile.replaceAll("~", "~0").replaceAll("/", "~1")}`, value: { content: "print('changed')\n", size: 17 } }] })
  ];
  if (count) {
    const ops = [
      { op: "add" as const, path: "/review", value: { owner: "ana", checks: ["lint", "tests"] } },
      ...steps.map((_, i) => ({ op: "replace" as const, path: `/steps/${i}/done`, value: true }))
    ];
    frames.push(frame("state_patch", { type: "state_patch", ...meta(1, 3.5), state_id: "plan", version: 2, ops }));
  }
  const decisionCount = count ?? (worst ? 60 : 3);
  for (let i = 0; i < decisionCount; i += 1) {
    const verdict = verdicts[i % 3]!;
    frames.push(
      frame("auto_approval_evaluation_ended", {
        type: "auto_approval_evaluation_ended",
        ...meta(1, 4 + i * (count ? 0.6 / count : 0.01)),
        tool_id: `tool-decision-${i}`,
        tool_name: worst ? toolNames[i % toolNames.length]! : ["read_file", "write_file", "send_email"][i]!,
        evaluation_id: `eval-${i}`,
        evaluator: "jev_evaluator",
        verdict,
        reason: worst && i % 2 === 0 ? `${firstMessages[3]} ${longToken}` : "Policy criteria passed for a read-only call.",
        details: {
          verdict,
          confidence: 0.6 + (i % 4) * 0.1,
          irreversible: 0.2,
          thresholds: { min_confidence: 0.7, escalate_if_irreversible_above: 0.5 },
          probabilities: { approve: 0.7, deny: 0.2, escalate: 0.1 },
          criteria_set: worst ? longToken : "default",
          model: "gemini-3.5-flash",
          request_id: `req-${i}`
        }
      })
    );
  }
  frames.push(frame("turn_end", { type: "turn_end", ...meta(1, 5) }));
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-panes${worst ? "-worst" : ""}`, agent_workflow_type: "QaAgent" }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* One answered turn, then one held at `stage` — thinking, a tool in flight, an approval, the
   reply streaming — or settled, or fifty tool calls deep. Stamped against the load's own clock,
   so the live timer reads seconds rather than the days since the fixtures' epoch. */
function replyScenario(stage: string): MockScenario {
  if (stage === "travel" || stage === "failed") return traceScenario(stage);
  const base = Date.now() / 1000 - 12.3;
  const at = (n: number, dt: number) => ({ ...meta(n, 0), timestamp: base + dt });
  const longArg = "9f8e7d6c5b4a".repeat(25);
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...at(1, -40), handler: "ask", payload: { text: firstMessages[0] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...at(1, -40) }),
    frame("model_interaction_started", { type: "model_interaction_started", ...at(1, -40), model: "gpt-5.4-mini" }),
    frame("tool_start", { type: "tool_start", ...at(1, -39), tool_id: "t-0", tool_name: "search_docs", tool_input: { q: "local activity" } }),
    frame("tool_end", { type: "tool_end", ...at(1, -38), tool_id: "t-0", tool_name: "search_docs", tool_output: "3 hits" }),
    frame("reply_delta", { type: "reply_delta", ...at(1, -36), text: prose(1) }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...at(1, -35.8), model: "gpt-5.4-mini", usage: usage(1800, 420, 0, 600) }),
    frame("message_handler_end", { type: "message_handler_end", ...at(1, -35.8), output: { text: prose(1) } }),
    frame("turn_end", { type: "turn_end", ...at(1, -35.8) }),
    frame("message_accepted", { type: "message_accepted", ...at(2, 0), handler: "ask", payload: { text: firstMessages[1] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...at(2, 0) }),
    frame("model_interaction_started", { type: "model_interaction_started", ...at(2, 0.2), model: "gpt-5.4-mini" })
  ];
  const tools = stage === "tools50" ? 50 : stage === "thinking" ? 0 : 3;
  for (let i = 0; i < tools; i += 1) {
    const tool = { tool_id: `t-${i + 1}`, tool_name: i === 1 ? `fetch_${longArg}` : toolNames[i % toolNames.length]!, tool_input: { path: i === 2 ? longArg : `/srv/reports/q${i}.csv` } };
    frames.push(frame("tool_start", { type: "tool_start", ...at(2, 1 + i * 0.2), ...tool }));
    if (stage === "tool" && i === tools - 1) break;
    if (stage === "failed" && i === tools - 1) {
      frames.push(frame("tool_error", { type: "tool_error", ...at(2, 1.1 + i * 0.2), ...tool, message: "Upstream 503 after 3 attempts" }));
      break;
    }
    frames.push(frame("tool_end", { type: "tool_end", ...at(2, 1.1 + i * 0.2), ...tool, tool_output: "ok" }));
  }
  if (stage === "approval") {
    const tool = { tool_id: "t-gate", tool_name: "refund_customer", tool_input: { amount: 42 } };
    frames.push(frame("tool_requested", { type: "tool_requested", ...at(2, 2), ...tool }));
    frames.push(frame("tool_approval_requested", { type: "tool_approval_requested", ...at(2, 2), ...tool }));
  }
  if (stage === "callback") {
    const tool = { tool_id: "t-ask", tool_name: "ask_user", tool_input: { question: "Which Springfield do you mean?" } };
    frames.push(frame("tool_start", { type: "tool_start", ...at(2, 2), ...tool }));
    frames.push(frame("callback_requested", { type: "callback_requested", ...at(2, 2), ...tool, output_schema: { type: "string" } }));
  }
  if (["replying", "settled", "tools50"].includes(stage)) {
    frames.push(frame("model_interaction_started", { type: "model_interaction_started", ...at(2, 11), model: "gpt-5.4-mini" }));
    frames.push(frame("reply_delta", { type: "reply_delta", ...at(2, 12), text: prose(2).slice(0, 180) }));
  }
  if (stage === "settled" || stage === "tools50") {
    frames.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...at(2, 12.2), model: "gpt-5.4-mini", usage: usage(1800, 420, 0, 600) }));
    frames.push(frame("message_handler_end", { type: "message_handler_end", ...at(2, 12.2), output: { text: prose(2).slice(0, 180) } }));
    frames.push(frame("turn_end", { type: "turn_end", ...at(2, 12.2) }));
  }
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-reply-${stage}`, agent_workflow_type: "QaAgent", label: `Reply ${stage}` }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* A Jev auto-mode travel turn as its trace arrives: gated tools the evaluator approves, Code Mode
   scripts whose host calls are gated too, a state change mid-script, held with an approval
   check open (`travel`); or settled after a failed tool and a denied one (`failed`). */
function traceScenario(stage: "travel" | "failed"): MockScenario {
  const base = Date.now() / 1000 - 13.3;
  const at = (n: number, dt: number) => ({ ...meta(n, 0), timestamp: base + dt });
  const model = "gemini-3.8-flash";
  const frames: AgentSseFrame[] = [
    frame("state_snapshot", { type: "state_snapshot", ...at(1, -60), state_id: "trip_board", version: 0, value: { trips: [] } }),
    frame("message_accepted", { type: "message_accepted", ...at(2, 0), handler: "ask", payload: { text: "Plan 4 nights in Paris from SFO in May, under $3k" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...at(2, 0) })
  ];
  let evaluation = 0;
  /* One gated call: requested, judged by Jev, then decided. A host call carries no `script`. */
  const gate = (tool: { tool_id: string; tool_name: string; tool_input: Record<string, unknown> }, dt: number, verdict: "approve" | "deny" = "approve") => {
    const evaluation_id = `eval-${(evaluation += 1)}`;
    const judged = { tool_id: tool.tool_id, tool_name: tool.tool_name, evaluation_id, evaluator: "jev_evaluator" };
    const reason = verdict === "approve" ? "jev/jev-1.13.0: verdict approve, read-only within budget" : "jev/jev-1.13.0: verdict deny, books a non-refundable fare";
    frames.push(
      frame("tool_approval_requested", { type: "tool_approval_requested", ...at(2, dt), ...tool }),
      frame("auto_approval_evaluation_started", { type: "auto_approval_evaluation_started", ...at(2, dt), ...judged }),
      frame("auto_approval_evaluation_ended", { type: "auto_approval_evaluation_ended", ...at(2, dt + 0.3), ...judged, verdict, reason, details: { verdict, confidence: 0.94 } }),
      frame("tool_approval_resolved", { type: "tool_approval_resolved", ...at(2, dt + 0.32), tool_id: tool.tool_id, tool_name: tool.tool_name, approved: verdict === "approve", reason, remember: false })
    );
  };
  const script = (id: string, body: string) => ({ tool_id: id, tool_name: "run_travel_script", tool_input: { script: body } });
  const host = (id: string, name: string, input: Record<string, unknown>) => ({ tool_id: id, tool_name: name, tool_input: { request: input } });

  const first = script("s-1", "board = read_trip_board()\nprint(board)");
  frames.push(
    frame("model_interaction_started", { type: "model_interaction_started", ...at(2, 0.1), model }),
    frame("tool_requested", { type: "tool_requested", ...at(2, 4.4), ...first }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...at(2, 4.7), model, usage: usage(2100, 600, 0, 64) })
  );
  gate(first, 4.7);
  const read = host("h-1", "read_trip_board", {});
  frames.push(
    frame("tool_start", { type: "tool_start", ...at(2, 5.1), ...first }),
    frame("tool_start", { type: "tool_start", ...at(2, 5.15), ...read }),
    frame("tool_end", { type: "tool_end", ...at(2, 5.2), ...read, tool_output: JSON.stringify({ trips: [] }) }),
    frame("tool_end", { type: "tool_end", ...at(2, 5.3), ...first, tool_output: "{'trips': []}" })
  );

  const second = script("s-2", 'trip = open_trip({"description": "Paris, May"})\nflights = search_flights({"origin": "SFO", "destination": "CDG"})\nhotels = search_hotels({"city": "Paris", "nights": 4})');
  frames.push(
    frame("model_interaction_started", { type: "model_interaction_started", ...at(2, 5.5), model }),
    frame("tool_requested", { type: "tool_requested", ...at(2, 12.9), ...second }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...at(2, 13.0), model, usage: usage(3100, 590, 0, 62) })
  );
  gate(second, 13.0);
  const open = host("h-2", "open_trip", { description: "Paris, May, 4 nights, under $3k" });
  const flights = host("h-3", "search_flights", { origin: "SFO", destination: "CDG", month: "2027-05" });
  const hotels = host("h-4", "search_hotels", { city: "Paris", nights: 4 });
  frames.push(
    frame("tool_start", { type: "tool_start", ...at(2, 13.4), ...second }),
    frame("tool_start", { type: "tool_start", ...at(2, 13.45), ...open }),
    frame("state_patch", { type: "state_patch", ...at(2, 13.5), state_id: "trip_board", version: 1, ops: [{ op: "add", path: "/trips/-", value: { city: "Paris" } }] }),
    frame("tool_end", { type: "tool_end", ...at(2, 13.55), ...open, tool_output: JSON.stringify({ trip_id: "trip-7" }) })
  );
  if (stage === "travel") {
    gate(flights, 13.6);
    frames.push(
      frame("tool_approval_requested", { type: "tool_approval_requested", ...at(2, 13.62), ...hotels }),
      frame("auto_approval_evaluation_started", { type: "auto_approval_evaluation_started", ...at(2, 13.62), tool_id: "h-4", tool_name: "search_hotels", evaluation_id: "eval-hotels", evaluator: "jev_evaluator" }),
      frame("tool_start", { type: "tool_start", ...at(2, 13.95), ...flights })
    );
    const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-reply-travel", agent_workflow_type: "QaAgent", label: "Travel agent (Jev auto mode)" }];
    return { agents, sessions, frames, instantFrames: frames.length };
  }
  gate(flights, 13.6);
  frames.push(
    frame("tool_start", { type: "tool_start", ...at(2, 14), ...flights }),
    frame("tool_error", { type: "tool_error", ...at(2, 15.2), ...flights, message: "FlightSearchTimeout: upstream did not answer in 1.2s" })
  );
  gate(hotels, 15.3, "deny");
  frames.push(
    frame("tool_error", { type: "tool_error", ...at(2, 15.8), ...second, message: "Script raised FlightSearchTimeout" }),
    frame("model_interaction_started", { type: "model_interaction_started", ...at(2, 16), model }),
    frame("reply_delta", { type: "reply_delta", ...at(2, 17), text: "Flight search timed out and the hotel booking was denied, so nothing is booked yet." }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...at(2, 17.2), model, usage: usage(3300, 120, 0, 0) }),
    frame("message_handler_end", { type: "message_handler_end", ...at(2, 17.2), output: { text: "Flight search timed out and the hotel booking was denied, so nothing is booked yet." } }),
    frame("turn_end", { type: "turn_end", ...at(2, 17.2) })
  );
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-reply-failed", agent_workflow_type: "QaAgent", label: "Travel agent (failed tools)" }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* Each example's handlers and states, as its schema document (python/examples, X.schema.json)
   declares them: what /api/agents serves once it reads more than the init data. */
const exampleInterfaces: Record<string, Pick<AgentDescriptor, "handlers" | "states">> = {
  "DagBuilderAgent": {
    "handlers": [
      {
        "name": "execute",
        "description": "Run a flow script. Each `run_agent` call starts a step agent as a subagent; the reply is\nwhat the script printed and returned.",
        "mid_turn": "enqueue"
      },
      {
        "name": "generate",
        "description": "Describe the flow you want, or a change to the current one. The agent writes it into\nyour editor, one edit at a time.",
        "mid_turn": "enqueue"
      }
    ],
    "states": []
  },
  "AutoModeTravelAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Chat with the travel assistant. Describe the trip you want (flights, hotels,\ndates, traveler name) in plain text; the assistant converses, writes and runs Python\nscripts against a simulated travel backend as needed, and replies with the results.",
        "mid_turn": "enqueue"
      },
      {
        "name": "set_approval_policy",
        "description": "Set the agent's approval posture: what gets allow-listed, and therefore what still\ngoes to a human.\n\n`human_approval_only` allow-lists nothing — every call waits for you, and an auto mode\nevaluator is not consulted even though one is wired. `allow_safe` allow-lists tools\nthat declared themselves inherently safe. `auto_mode` hands each remaining call to the\nJev evaluator, which may allow it, deny it, or leave it to you anyway.\n`dangerously_skip_all` gates nothing at all.\n\nA posture REPLACES the policy, allow-list included — including anything an \"approve and\nstop asking\" added this session. That is the point of it being a posture rather than a\npatch: you get exactly what you asked for and nothing left over.",
        "mid_turn": "accept"
      },
      {
        "name": "set_model",
        "description": "Set the model this session uses for subsequent turns. Takes effect on the next\nmodel call, so a turn already in flight finishes on the model it started with.",
        "mid_turn": "accept"
      }
    ],
    "states": [
      "trip_board"
    ]
  },
  "CodeModeMemoryAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Chat with the assistant. Tell it something to remember, or ask about something you\ntold it before, in this session or another one sharing its memory.",
        "mid_turn": "enqueue"
      }
    ],
    "states": [
      "skills",
      "memory"
    ]
  },
  "MontyChatAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Chat with the travel assistant. Describe the trip you want (flights, hotels,\ndates, traveler name) in plain text; the assistant converses, writes and runs Python\nscripts against a simulated travel backend as needed, and replies with the results.",
        "mid_turn": "enqueue"
      },
      {
        "name": "set_approval_policy",
        "description": "Update the agent's tool approval policy.",
        "mid_turn": "accept"
      },
      {
        "name": "set_model",
        "description": "Set the model this session uses for subsequent turns. Takes effect on the next\nmodel call, so a turn already in flight finishes on the model it started with.",
        "mid_turn": "accept"
      }
    ],
    "states": [
      "trip_board"
    ]
  },
  "MontyChatSubagentAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Chat with the travel assistant. Describe the trip you want (flights, hotels,\ndates, traveler name) in plain text; the assistant converses, writes Python scripts,\nand runs them against a simulated travel backend via a script-runner subagent, then\nreplies with the results.",
        "mid_turn": "enqueue"
      },
      {
        "name": "set_model",
        "description": "Set the model this session uses for subsequent turns. Takes effect on the next\nmodel call, so a turn already in flight finishes on the model it started with.",
        "mid_turn": "accept"
      }
    ],
    "states": []
  },
  "NexusHelloAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Ask the agent a question; it may use its Nexus-brokered tools to answer.",
        "mid_turn": "reject"
      }
    ],
    "states": []
  },
  "OpenAIHelloAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Chat with the assistant. Ask it anything; ask about the weather in a city and it\ncalls its `get_weather` tool and tells you what it found.",
        "mid_turn": "reject"
      }
    ],
    "states": []
  },
  "PydanticAIHelloAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Chat with the assistant. Ask it anything; ask about the weather in a city and it calls\nits `get_weather` tool and tells you what it found.",
        "mid_turn": "reject"
      }
    ],
    "states": []
  },
  "ReactAgent": {
    "handlers": [
      {
        "name": "ask",
        "description": "Chat with the assistant. Ask about the weather in a city, or where you are, and it\nchains its tools (coordinates / IP-location / weather) and tells you what it found.",
        "mid_turn": "reject"
      }
    ],
    "states": []
  },
  "TicTacToeAgent": {
    "handlers": [
      {
        "name": "new_game",
        "description": "Reset the board and start a new game. You are X and open unless\n`agent_goes_first` is set, in which case the agent plays X and opens.",
        "mid_turn": "enqueue"
      },
      {
        "name": "play",
        "description": "Play your mark on an empty cell (1-9, left-to-right, top-to-bottom). The agent\nthen judges the position with TypeSafe and answers with its own move.",
        "mid_turn": "enqueue"
      }
    ],
    "states": [
      "board"
    ]
  }
};

/* The New session dialog's registry: the examples' eleven agents (each example's agents.toml
   plus the Q&A agent), two of them without a worker and one unchecked, and with `worst` some
   290 more with long, unbroken, RTL, emoji, CJK, markup and missing names and descriptions. */
function agentsScenario(worst: boolean): MockScenario {
  const worker = (task_queue: string, status: "ready" | "no_worker" | "unknown" = "ready") => ({
    status,
    task_queue,
    poller_count: status === "ready" ? 1 : 0,
    last_seen: status === "ready" ? startedAt : null,
    error: status === "unknown" ? "DescribeTaskQueue timed out" : null
  });
  const entry = (key: string, workflow_type: string, task_queue: string, label: string, description: string, status?: "ready" | "no_worker" | "unknown"): AgentDescriptor => ({
    key, workflow_type, task_queue, label, description, worker: worker(task_queue, status), ...exampleInterfaces[workflow_type]
  });
  const matchSettings = {
    required: true,
    schema: { title: "MatchSettings", description: "Who the agent is playing.", type: "object", properties: { player_name: { type: "string", title: "Player Name" } }, required: ["player_name"] }
  };
  const memorySettings = {
    required: false,
    schema: { title: "MemorySettings", description: "Where the memory bundle lives. Two sessions with the same `memory_dir` share what they remember.", type: "object", properties: { memory_dir: { type: "string", title: "Memory Dir" } } }
  };
  const real: AgentDescriptor[] = [
    agents[0]!,
    entry("agent-dag", "DagBuilderAgent", "agent-dag", "Agent DAG Studio", "Describe a job and an OpenAI agent writes a *flow*: a Python script wiring fresh agents into a\nDAG, each set up by the script with its own name, system prompt, tools and model. The script\nlives in your editor, which the agent changes through callback tools. `execute` runs it in Code\nMode, where each `run_agent` call starts a DagStepAgent subagent. Best driven from the studio UI\n(`just studio`)."),
    entry("auto-mode-travel", "AutoModeTravelAgent", "auto-mode-agent", "Travel agent (Jev Auto mode)", "The Monty conversational travel agent under AUTO MODE: each gated tool call is first judged\nby Jev against named criteria sets — lookups are approved, bookings are escalated to you, and\nanything Jev isn't sure about comes to you exactly as it would without auto mode."),
    { ...entry("code-mode-memory", "CodeModeMemoryAgent", "code-mode-memory", "Code Mode Memory", "An assistant with long-term memory: its Code Mode scripts keep /memory as an OKF knowledge\nbundle in a directory of the worker's disk, following a memory skill under /skills. Start two\nsessions with the same memory_dir and the second one remembers what you told the first; open\nthe Graph in the VFS File Mounts pane to see how the memories link up."), init_data: memorySettings },
    entry("monty-chat", "MontyChatAgent", "monty-dynamic-agent", "Monty (Conversational)", "A conversational travel-booking assistant. Chat in plain text; an LLM converses to\ngather your trip details, then WRITES its own pydantic-monty Python script and runs\nit via a tool to orchestrate durable flight/hotel search + booking activities,\nreplying with the results."),
    entry("monty-chat-subagent", "MontyChatSubagentAgent", "monty-dynamic-agent", "Monty (Conversational, Subagent)", "Same conversational travel-booking experience as Monty (Conversational), but instead\nof running its Python scripts inline it drives the barebones Monty (Dynamic) agent as\na SUBAGENT — starting a script-runner instance and sending it each script over the\nharness subagent toolset. The first end-to-end demo of agents-as-subagents."),
    entry("nexus-hello", "NexusHelloAgent", "nexus-hello", "Nexus Hello", "A hello-world OpenAI Agents SDK agent using nexus_tools_gateway() and nexus_native_mcp_server():\n\n- demo_get_fun_fact (a 3rd-party MCP server, proxied through the Durable Tools Gateway)\n- demo-nexus_get_lucky_number (a native Nexus tool service, reached directly)\n- demo-nexus_get_delayed_lucky_number (a workflow-backed Nexus operation)\n\nNeeds `just setup-nexus`, the gateway, both tool services/servers, and `just\nregister-third-party` run first — see examples/nexus_hello/README.md.", "no_worker"),
    entry("openai-hello", "OpenAIHelloAgent", "openai-hello", "OpenAI Hello", "A hello-world OpenAI Agents SDK agent. Chat in plain text; it replies in prose and can\ncall one tool (get_weather) for a city's weather.", "unknown"),
    entry("pydantic-ai-hello", "PydanticAIHelloAgent", "pydantic-ai-hello", "Pydantic AI Hello", "A hello-world Pydantic AI agent. Chat in plain text; it replies in prose and can call one\ntool (get_weather) for a city's weather."),
    entry("react-agent", "ReactAgent", "react-agent", "ReAct Agent", "A ReAct-style agent on the OpenAI Agents SDK: it reasons, then acts by chaining real tools,\nlooping on each result until it can answer. Chat in plain text and it finds the weather for a\nnamed city (coordinates -> weather) or your current location (IP -> location -> weather), and\npulls Formula 1 data over an MCP server — chaining tools as needed and replying in prose. Shows\nthe harness OpenAI streaming path with a multi-tool agentic loop end to end — model call →\nstreaming activity → live turn stream (reply_delta / tool_requested / tool_start / tool_end /\nmodel_interaction_*)."),
    { ...entry("tictactoe", "TicTacToeAgent", "tictactoe-agent", "Tic-Tac-Toe (TypeSafe)", "Play tic-tac-toe against an agent with NO LLM in the loop. Each of its turns is one\nTypeSafe System One call (jev) answering three typed questions over the board.", "no_worker"), init_data: matchSettings }
  ];
  const names = [
    `Customer ${"onboarding and reconciliation ".repeat(10)}agent`,
    "x".repeat(2000),
    "وكيل خدمة العملاء للمطالبات",
    "👩🏽‍💻🧑‍🤝‍🧑 顧客サポート Agent",
    "<script>alert(1)</script> **Agent** # heading",
    "",
    "  padded\tname\n  ",
    "ab\u200b\u200bcd zero-width"
  ];
  const descriptions = [
    `Reconciles ${"ledger entries across regions ".repeat(30)}nightly.`,
    "y".repeat(2000),
    "يجيب عن أسئلة العملاء حول المطالبات ويحيلها إلى الفريق المناسب.",
    "顧客の質問に答えます。🧾 請求書と 📦 配送の状況を確認します。",
    "# Heading\n\n<script>alert(1)</script> **bold** and `code`",
    ""
  ];
  const statuses = ["ready", "ready", "ready", "no_worker", "unknown"] as const;
  /* Most of the fleet names no schema, so it says nothing of its handlers or state; every
     fourth declares some, with long and unbroken handler names among them. */
  const handlerSets: AgentDescriptor["handlers"][] = [
    [{ name: "ask", description: "Ask anything.", mid_turn: "enqueue" }, { name: "set_model", description: "Switch models.", mid_turn: "accept" }],
    [{ name: `reconcile_${"ledger_".repeat(40)}entries`, description: `Reconciles ${"entries ".repeat(60)}`, mid_turn: "reject" }],
    [{ name: "سؤال", description: "يجيب عن الأسئلة.", mid_turn: null }, { name: "質問", description: "質問に答えます。", mid_turn: "enqueue" }, { name: "x".repeat(300), description: "", mid_turn: "enqueue" }]
  ];
  const extra = worst
    ? Array.from({ length: 289 }, (_, i) => {
        const label = i < names.length ? names[i]! : `Fleet agent ${String(i).padStart(3, "0")}`;
        const agent = entry(`fleet-${i}`, `FleetAgent${i}`, `fleet-${i % 7}`, label, descriptions[i % descriptions.length]!, statuses[i % statuses.length]);
        if (i % 4 !== 0) return agent;
        return { ...agent, handlers: handlerSets[(i / 4) % handlerSets.length], states: i % 8 === 0 ? ["board"] : [], init_data: i % 12 === 0 ? matchSettings : null };
      })
    : [];
  const sessions = sessionList(1).map((s) => ({ ...s, agent_workflow_type: "ReactAgent" }));
  return { agents: [...real, ...extra], sessions, frames: [] };
}

/* The Session Manager's worst cases: agent names that are long, unbroken, RTL, emoji, markup or
   empty, and sessions with no message, no label, a zero or far-future time, over several days. */
function sessionsWorst(): MockScenario {
  const names = [
    `Customer ${"onboarding and reconciliation ".repeat(10)}agent`,
    "x".repeat(2000),
    "وكيل خدمة العملاء للمطالبات",
    "👩🏽‍💻🧑‍🤝‍🧑 顧客サポート Agent",
    "<script>alert(1)</script> **Agent** # heading",
    ""
  ];
  const worstAgents = names.map((label, i) => ({ ...agents[0]!, key: `worst-${i}`, workflow_type: `WorstAgent${i}`, label }));
  /* The server's own id shape, and one at Temporal's default 1,000-character limit. */
  const uuidId = (i: number) => `agent-session-${String(i).padStart(8, "0")}-7953-45d8-b4f4-0ed6e3303391`;
  const sessions = sessionList(60).map((session, i) => ({
    ...session,
    workflow_id: i === 3 ? `${uuidId(i)}-${"9f8e7d6c5b4a".repeat(90)}`.slice(0, 1000) : uuidId(i),
    agent_workflow_type: worstAgents[i % worstAgents.length]!.workflow_type,
    initial_user_message: i % 5 === 4 ? null : session.initial_user_message,
    label: i % 10 === 9 ? "" : session.label,
    created_at: i === 1 ? 0 : i === 2 ? 4_102_444_800 : session.created_at - Math.floor(i / 6) * 86_400
  }));
  /* One turn whose events carry every field the replay readout can show, each at its worst. */
  const m = (dt: number) => meta(1, dt);
  const token = "9f8e7d6c5b4a".repeat(167).slice(0, 2000);
  const tool = { tool_id: `tool-${token.slice(0, 64)}`, tool_name: toolNames[2]! };
  const model = "provider/claude-opus-4-1-20250805-with-a-very-long-deployment-name-eu-west-1";
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...m(0), handler: "ask", payload: { text: token }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...m(0.1) }),
    frame("model_interaction_started", { type: "model_interaction_started", ...m(0.2), model }),
    frame("tool_start", { type: "tool_start", ...m(1.4), ...tool, tool_input: { customer: "نور الهدى عبد الرحمن", 顧客: "王秀英", who: "👩🏽‍💻🧑‍🤝‍🧑 Priya", note: "<script>alert(1)</script> **bold**", token } }),
    frame("tool_error", { type: "tool_error", ...m(3_725), ...tool, message: `UpstreamTimeout ${token}` }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...m(3_726), model, usage: usage(9_876_543_210, 1_234_567_890, 456_789_012, 8_765_432_109) }),
    frame("turn_end", { type: "turn_end", ...m(3_727) })
  ];
  return { agents: worstAgents, sessions, frames, instantFrames: frames.length };
}

/* `?mock=sessionsbreak&data=demo|worst|empty|allarchived|single|one|many|huge`: the Session
   Manager with every lifecycle state, pin and archive mixed down its first screenful, times on
   each relative-age boundary, and the stored pins and archives each variant needs. Times are
   relative to load, so "just now" stays just now. */
function sessionsBreak(data: string): MockScenario {
  const now = Math.floor(Date.now() / 1000);
  const longAgent = "Quarterly revenue reconciliation agent for EMEA shared services, Kraków office, with FX adjustments";
  const breakAgents: AgentDescriptor[] = [
    ...agents,
    { ...agents[0]!, key: "long", workflow_type: "RevenueReconciliationAgent", label: `${longAgent} v2 (beta)`.slice(0, 120) },
    { ...agents[0]!, key: "rtl", workflow_type: "ArabicSupportAgent", label: "وكيل خدمة العملاء للمطالبات" },
    { ...agents[0]!, key: "same", workflow_type: "ResearchAgent", label: "ResearchAgent" },
    { ...agents[0]!, key: "off", workflow_type: "BillingAgent", task_queue: "billing-agent", label: "Billing Agent", description: "Answers billing questions.", worker: { status: "no_worker", task_queue: "billing-agent", poller_count: 0, last_seen: null, error: null } }
  ];
  const id = (tag: string) => `agent-session-break-${tag}`;
  const row = (tag: string, at: number, status: string, message: string | null, type = "QaAgent", label = `Session ${tag}`): Session => ({
    workflow_id: id(tag),
    created_at: at,
    label,
    agent_workflow_type: type,
    initial_user_message: message,
    execution_status: status,
    closed: status !== "RUNNING"
  });
  const title300 = `${firstMessages[1]}; also reconcile the Q3 Board Deck — FINAL (revised) v12 [approved by legal].pdf against bartholomew.fitzgerald@northwind-industries-holdings.example.com's export and flag every FX adjustment over €10,000 for the Kraków office`.slice(0, 300);
  const approval = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: title300 }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("tool_requested", { type: "tool_requested", ...meta(1, 2), tool_id: "break-approval", tool_name: "refund_customer", tool_input: { amount: 312 } }),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...meta(1, 2), tool_id: "break-approval", tool_name: "refund_customer", tool_input: { amount: 312 } })
  ];
  const callback = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: "Which Springfield do you mean?" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("callback_requested", { type: "callback_requested", ...meta(1, 2), tool_id: "break-callback", tool_name: "ask_user", tool_input: { question: "Which Springfield?" }, output_schema: { type: "string" } })
  ];
  const quiet = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: "Hello" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("message_handler_end", { type: "message_handler_end", ...meta(1, 2), output: { text: "Hi." } }),
    frame("turn_end", { type: "turn_end", ...meta(1, 2) })
  ];
  const streams: Record<string, AgentSseFrame[] | "silent"> = { [id("approval")]: approval, [id("callback")]: callback, [id("workeroff")]: "silent", [id("workeroff2")]: "silent" };
  const base = { agents: breakAgents, frames: quiet, instantFrames: 10_000, streams, answersApprovals: true };

  if (data === "empty") return { ...base, sessions: [], prefs: { pinnedSessions: [], archivedSessions: [], sessionArchiveOpen: false } };
  if (data === "single") return { ...base, sessions: [row("single", now - 120, "RUNNING", "Summarize yesterday's failed workflows")], prefs: { pinnedSessions: [], archivedSessions: [], sessionArchiveOpen: false } };
  if (data === "one") {
    const sessions = [row("today", now - 120, "RUNNING", "Today's only session"), row("pin", now - 86_400 * 3, "COMPLETED", "The only pinned one"), row("arch", now - 86_400 * 9, "NOT_FOUND", "The only archived one")];
    return { ...base, sessions, prefs: { pinnedSessions: [id("pin")], archivedSessions: [id("arch")], sessionArchiveOpen: true } };
  }
  if (data === "allarchived") {
    const sessions = statuses.map((status, i) => row(`all-${i}`, now - i * 4_000, status, firstMessages[i % firstMessages.length]!));
    return { ...base, sessions, prefs: { pinnedSessions: [id("all-1")], archivedSessions: sessions.map((s) => s.workflow_id), sessionArchiveOpen: false } };
  }
  if (data === "many" || data === "huge") {
    const count = data === "many" ? 200 : 10_000;
    const sessions = sessionList(count).map((s, i) => ({ ...s, created_at: now - 30 - i * 1_800 }));
    /* Three in four of 200, one in five of 10,000, spread through the list. */
    const archivedIds = sessions.filter((_, i) => (data === "many" ? i % 4 !== 0 : i % 5 === 1)).map((s) => s.workflow_id);
    /* One over each cap, so readers of the store must trim it: 101 pins, 501 archives. */
    const extra = (n: number, tag: string) => Array.from({ length: n }, (_, i) => `agent-session-unlisted-${tag}-${i}`);
    const pinnedSessions = data === "many" ? [...sessions.slice(0, 5).map((s) => s.workflow_id), ...extra(96, "pin")] : sessions.slice(0, 12).map((s) => s.workflow_id);
    const archivedSessions = data === "many" ? [...archivedIds, ...extra(501 - archivedIds.length, "arch")] : archivedIds;
    return { ...base, sessions, prefs: { pinnedSessions, archivedSessions, sessionArchiveOpen: data === "huge" } };
  }
  if (data === "worst") {
    const sessions = [
      row("approval", now - 5, "RUNNING", title300, "RevenueReconciliationAgent"),
      row("workeroff", now - 59, "RUNNING", firstMessages[2]!, "BillingAgent"),
      row("gone", now - 60, "NOT_FOUND", firstMessages[3]!, "ArabicSupportAgent"),
      row("failed", now - 3_540, "FAILED", firstMessages[4]!),
      row("pin-open", now - 3_600, "RUNNING", "x", "MontyDynamicAgent"),
      row("completed", now - 7_200, "COMPLETED", "<script>alert(1)</script> **bold** # heading"),
      row("callback", now - 9_000, "RUNNING", "Which Springfield do you mean?", "ResearchAgent"),
      row("timedout", now - 86_400 * 35, "TIMED_OUT", null, "QaAgent", ""),
      row("terminated", 86_400, "TERMINATED", "Rebuild the 1970 epoch report", "ResearchAgent"),
      row("canceled", now + 3_600, "CANCELED", "Clock skew: this started an hour from now"),
      row("skew30", now + 30, "RUNNING", "Started 30 seconds in the future"),
      row("nostart", 0, "RUNNING", "No start time on this one"),
      row("pin-closed", now - 86_400 * 2, "COMPLETED", "Pinned and completed"),
      row("pin-gone", now - 86_400 * 3, "NOT_FOUND", "Pinned, and Temporal lost it", "RevenueReconciliationAgent"),
      row("pin-arch", now - 86_400 * 4, "RUNNING", "Pinned, then archived, still running"),
      row("arch-open", now - 86_400 * 5, "RUNNING", "Archived while still running", "MontyDynamicAgent"),
      row("arch-gone", now - 86_400 * 6, "NOT_FOUND", "Archived after the dev server restarted"),
      row("arch-closed", now - 86_400 * 7, "FAILED", "Archived after it failed"),
      row("workeroff2", now - 86_400, "RUNNING", "Billing worker is down for this one too", "BillingAgent"),
      row("dup-a", now - 86_400 - 60, "COMPLETED", "When should I use a local activity versus a normal activity?"),
      row("dup-b", now - 86_400 - 120, "NOT_FOUND", "When should I use a local activity versus a normal activity?"),
      ...Array.from({ length: 24 }, (_, i) => row(`fill-${i}`, now - 86_400 * (8 + i), statuses[i % statuses.length]!, firstMessages[i % firstMessages.length]!, ["QaAgent", "MontyDynamicAgent", "ResearchAgent"][i % 3]))
    ];
    return {
      ...base,
      sessions,
      prefs: { pinnedSessions: [id("pin-open"), id("pin-closed"), id("pin-gone"), id("pin-arch")], archivedSessions: [id("pin-arch"), id("arch-open"), id("arch-gone"), id("arch-closed")], sessionArchiveOpen: true }
    };
  }
  const sessions = [
    row("demo-1", now - 300, "RUNNING", "When should I use a local activity versus a normal activity?"),
    row("demo-2", now - 2_400, "RUNNING", "Book Lisbon to Tokyo, 3 nights", "MontyDynamicAgent"),
    row("demo-3", now - 86_400, "COMPLETED", "Summarize the release notes"),
    row("demo-4", now - 86_400 * 2, "NOT_FOUND", "Why did the export time out?"),
    row("demo-5", now - 86_400 * 3, "FAILED", "Reconcile the Q3 invoices")
  ];
  return { ...base, sessions, prefs: { pinnedSessions: [id("demo-2")], archivedSessions: [id("demo-4")], sessionArchiveOpen: false } };
}

const choice = (...options: string[]) => ({ type: "string", enum: options });
/* Output schemas as pydantic emits them for `Literal[...]`, a model of several questions, and
   `str` (react_agent's ask_user): what a callback asking the user something can return. */
const callbackQuestions = [
  { tool_name: "choose_city", tool_input: { question: "Which Portland did you mean?" }, output_schema: choice("Portland, Oregon", "Portland, Maine", `Portland_${"Oregon_Multnomah_County_".repeat(6)}`) },
  {
    tool_name: "confirm_trip",
    tool_input: { question: "👩🏽‍💻 Priya: book Lisbon → Tokyo, 3 nights — a few details first." },
    output_schema: {
      type: "object",
      properties: {
        cabin: { title: "Cabin", ...choice("Economy", "Premium economy", "Business") },
        seat: { title: "Seat", ...choice("Window", "Aisle", "No preference") },
        extras: { title: "Extras", type: "array", items: choice("Lounge access", "Extra legroom", "Vegetarian meal", "قناة الفريق 📟") },
        notes: { title: "Anything else", anyOf: [{ type: "string" }, { type: "null" }], default: null }
      },
      required: ["cabin", "seat", "extras"]
    }
  },
  { tool_name: "ask_user", tool_input: { question: "نور الهدى عبد الرحمن — لماذا فشل سير العمل؟ What should I tell her?" }, output_schema: { type: "string" } }
];

/* A turn blocked on callback questions. Answering one streams its `callback_resolved`. */
function questionsScenario(): MockScenario {
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: firstMessages[4] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    ...callbackQuestions.map((call, i) => frame("callback_requested", { type: "callback_requested", ...meta(1, 2), tool_id: `tool-callback-${i}`, ...call }))
  ];
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-scale-questions", agent_workflow_type: "QaAgent", label: "Callback questions" }];
  return { agents, sessions, frames, instantFrames: frames.length, answersApprovals: true };
}

const markdownQuestion = {
  tool_name: "ask_user",
  tool_input: { question: "Refund **order #4417**?\n\n- `€312` total\n- card ending 0042\n\n# not a heading <script>alert(1)</script>" },
  output_schema: { type: "string" }
};

/* A turn blocked on `approvals` approvals and `callbacks` callbacks, interleaved oldest first. */
function mixedScenario(approvals: number, callbacks: number): MockScenario {
  const asks = [...callbackQuestions, markdownQuestion];
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...meta(1, 1), handler: "ask", payload: { text: firstMessages[0] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(1, 1) }),
    frame("reply_delta", { type: "reply_delta", ...meta(1, 2), text: prose(1) }),
    frame("message_handler_end", { type: "message_handler_end", ...meta(1, 3), output: { text: prose(1) } }),
    frame("turn_end", { type: "turn_end", ...meta(1, 3) }),
    frame("message_accepted", { type: "message_accepted", ...meta(2, 4), handler: "ask", payload: { text: firstMessages[1] }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...meta(2, 4) })
  ];
  for (let i = 0, a = 0, c = 0; a < approvals || c < callbacks; i += 1) {
    const at = meta(2, 5 + i * 0.01);
    if (c >= callbacks || (a < approvals && i % 2 === 0)) {
      const tool = { tool_id: `tool-approval-${a}`, tool_name: toolNames[a % toolNames.length]!, tool_input: approvalInputs[a % approvalInputs.length]! };
      frames.push(frame("tool_requested", { type: "tool_requested", ...at, ...tool }));
      frames.push(frame("tool_approval_requested", { type: "tool_approval_requested", ...at, ...tool }));
      a += 1;
    } else {
      frames.push(frame("callback_requested", { type: "callback_requested", ...at, tool_id: `tool-callback-${c}`, ...asks[c % asks.length]! }));
      c += 1;
    }
  }
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-mixed${approvals}x${callbacks}`, agent_workflow_type: "QaAgent", label: `${approvals} approvals, ${callbacks} callbacks` }];
  return { agents, sessions, frames, instantFrames: frames.length, answersApprovals: true };
}

/* Form handlers as attention queue cards, on a run with no turn in flight. `actions` is
   tic-tac-toe's two. `actionsworst` ends the turn with 6 approvals and 6 callbacks still open, so
   the card joins 12 requests, and offers ten form handlers: a 300-character name, a
   2,000-character token, CJK and RTL, markup, no fields and every field kind. */
function actionsScenario(worst: boolean): MockScenario {
  const form = (name: string, properties: Record<string, unknown> = {}, required: string[] = []): AgentInterfaceFunction => ({
    name,
    description: "",
    parameters: { type: "object", properties, required },
    output: textOutput,
    mid_turn: "enqueue",
    model_callable: false
  });
  const play = form("play", { cell: { type: "integer", title: "Cell", description: "1-9, reading left-to-right, top-to-bottom.", minimum: 1, maximum: 9 } }, ["cell"]);
  const newGame = form("new_game", { agent_goes_first: { type: "boolean", title: "Agent Goes First", default: false } });
  const ten = [
    form(`make_a_move_on_the_board_${"and_keep_going_".repeat(18)}`.slice(0, 300), { cell: { type: "integer", title: "Cell" } }, ["cell"]),
    play,
    newGame,
    form("ping"),
    handlerInterface.at(-1)!,
    form("y".repeat(2000), { n: { type: "integer", title: "N" } }, ["n"]),
    form("王秀英_العربية_👩‍👩‍👧‍👦_שלום", { note: { type: "string", title: "ملاحظة" }, n: { type: "integer" } }, ["n"]),
    form("<script>alert(1)</script>", { n: { type: "integer" } }, ["n"]),
    form("reset_everything"),
    form("cancel", { reason: { type: "string", title: "Reason" }, n: { type: "integer" } }, ["n"])
  ];
  const base = mixedScenario(worst ? 6 : 0, worst ? 6 : 0);
  base.frames.push(frame("turn_end", { type: "turn_end", ...meta(2, 6) }));
  base.sessions = [{ ...base.sessions[0]!, workflow_id: `agent-session-scale-actions${worst ? "worst" : ""}`, label: "Action cards" }];
  return { ...base, instantFrames: base.frames.length, agentInterface: worst ? ten : [newGame, play] };
}

const textOutput = { type: "object", properties: { text: { type: "string", title: "Text" } } };
/* Handlers whose arguments the composer draws as a form: the two auto_mode/Monty settings, and
   one that asks for every field kind the schema form has. */
const handlerInterface: AgentInterfaceFunction[] = [
  { name: "ask", description: "Ask a question.", parameters: { type: "object", properties: { text: { type: "string", title: "Text" } }, required: ["text"] }, output: textOutput, mid_turn: "enqueue", model_callable: true },
  {
    name: "set_approval_policy",
    description: "Choose which tool calls wait for a person.",
    parameters: {
      type: "object",
      properties: { posture: { title: "Posture", description: "`human_approval_only` asks before **every** tool call.", enum: ["human_approval_only", "auto_with_review", "auto"] } },
      required: ["posture"]
    },
    output: textOutput,
    mid_turn: "accept",
    model_callable: false
  },
  {
    name: "set_model",
    description: "Set the model for later turns.",
    parameters: { type: "object", properties: { model: { title: "Model", enum: ["gemini-3.5-flash", "gemini-3.1-flash-lite"] } }, required: ["model"] },
    output: textOutput,
    mid_turn: "accept",
    model_callable: false
  },
  {
    name: "plan_trip",
    description: "Plan a trip with every kind of field.",
    parameters: {
      type: "object",
      properties: {
        destination: { title: "Destination", type: "string", description: "City and country, e.g. *Lisbon, Portugal*." },
        nights: { title: "Nights", type: "integer", minimum: 1, maximum: 30 },
        budget: { title: "Budget (EUR)", type: "number" },
        cabin: { title: "Cabin", enum: ["Economy", "Business"] },
        refundable: { title: "Refundable fares only", type: "boolean", default: false },
        notes: { title: "Notes", type: "string", format: "textarea", maxLength: 2000 },
        extras: { title: "Extras", type: "array", items: { enum: ["Lounge", "Wifi"] }, default: ["Lounge"] },
        travellers: {
          title: "Travellers",
          type: "array",
          minItems: 1,
          items: { type: "object", properties: { name: { title: "Name", type: "string" }, email: { title: "Email", type: "string" } }, required: ["name"] }
        },
        contact: { title: "Contact", type: "object", properties: { phone: { title: "Phone", type: "string" } } }
      },
      required: ["destination", "nights", "cabin"]
    },
    output: textOutput,
    mid_turn: "reject",
    model_callable: true
  }
];

/* A settled session offering those handlers, and an agent whose New session form asks for init data. */
function handlersScenario(): MockScenario {
  const withInit = {
    ...agents[1]!,
    init_data: {
      required: true,
      schema: {
        title: "Trip setup",
        type: "object",
        properties: {
          traveller: { title: "Traveller", type: "string", description: "Who the bookings are **for**." },
          posture: { title: "Posture", enum: ["human_approval_only", "auto"] },
          max_spend: { title: "Max spend", type: "number" }
        },
        required: ["traveller", "posture"]
      }
    }
  };
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-scale-handlers", agent_workflow_type: "MontyDynamicAgent", label: "Handler forms", execution_status: "RUNNING", closed: false }];
  return { agents: [agents[0]!, withInit], sessions, frames: [], agentInterface: handlerInterface };
}

/* The composer's choice menus. `choices` is the travel agent's bar: a chat handler, a policy
   titled "Posture" with no default, and a model. `choicesworst` adds a 300-character model id
   as the current value, a long policy option, a field with no schema title, and a 15-option
   locale whose list has to scroll. */
function choicesScenario(worst: boolean): MockScenario {
  const choice = (name: string, field: string, options: string[], extra: Record<string, unknown> = { title: field[0]!.toUpperCase() + field.slice(1) }): AgentInterfaceFunction => ({
    name,
    description: "",
    parameters: { type: "object", properties: { [field]: { type: "string", enum: options, ...extra } }, required: [field] },
    output: textOutput,
    mid_turn: "accept",
    model_callable: false
  });
  const longModel = `claude-sonnet-${"long-model-id-".repeat(20)}2026-09-30`;
  const choiceInterface = worst
    ? [
        handlerInterface[0]!,
        choice("set_approval_policy", "posture", ["human_approval_only", "dangerously_skip_all_approvals_for_every_tool_call_in_this_session_including_irreversible_ones"], { title: "Posture", default: "dangerously_skip_all_approvals_for_every_tool_call_in_this_session_including_irreversible_ones" }),
        choice("set_model", "model", [longModel, "gemini-3.8-flash"], { title: "Model", default: longModel }),
        choice("set_reasoning_effort", "reasoning_effort", ["low", "medium", "high"], {}),
        choice("set_locale", "locale", ["王秀英-中文", "العربية", "👩‍👩‍👧‍👦", ...Array.from({ length: 12 }, (_, i) => `locale_${i + 1}`)], { default: "العربية" })
      ]
    : [handlerInterface[0]!, choice("set_approval_policy", "posture", ["human_approval_only", "allow_safe", "auto_mode", "dangerously_skip_all"]), choice("set_model", "model", ["gemini-3.8-flash", "gemini-3.1-flash-lite"], { title: "Model", default: "gemini-3.8-flash" })];
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-choices${worst ? "worst" : ""}`, label: "Choice menus", execution_status: "RUNNING", closed: false }];
  /* A slow submit, so a send is still in flight when the button is pressed again. */
  return { agents, sessions, frames: [], agentInterface: choiceInterface, submitDelayMs: 1500 };
}

/* Every dropdown under worst-case options: the composer's handler forms and the session drawer's
   agent filter fed hostile agent names. */
function selectsScenario(): MockScenario {
  const intl = ["👩‍👩‍👧‍👦 family 🏳️‍🌈", "王秀英-模型-中文选项", "نموذج-عربي-طويل", "עברית-מודל", "e\u0301\u0301\u0301 combining", "zero\u200bwidth\u200bjoin", "<script>alert(1)</script>", "**bold** # heading", "  padded\twith\ttabs  "];
  const stressInterface: AgentInterfaceFunction[] = [
    handlerInterface[0]!,
    {
      name: "set_model",
      description: "Set the model for later turns.",
      parameters: { type: "object", properties: { model: { title: "Model", enum: ["gemini-3.8-flash", "gemini-3.1-flash-lite"] } }, required: ["model"] },
      output: textOutput,
      mid_turn: "accept",
      model_callable: false
    },
    {
      name: "select_stress",
      description: "Every enum shape a schema can hand the form.",
      parameters: {
        type: "object",
        properties: {
          long: { title: "300-character option", enum: ["short", `${"a long option label that keeps going ".repeat(9)}`.slice(0, 300)] },
          unbroken: { title: "2,000-character token", enum: ["x".repeat(2000), "short"] },
          many: { title: "500 options", enum: Array.from({ length: 500 }, (_, i) => `option-${String(i + 1).padStart(3, "0")}`) },
          none: { title: "Empty enum", enum: [] },
          single: { title: "Single option", enum: ["only-option"] },
          intl: { title: "Emoji, CJK, RTL", enum: intl },
          tags: { title: "Tags", type: "array", items: { enum: intl }, default: ["王秀英-模型-中文选项", ""] }
        },
        required: ["single", "intl"]
      },
      output: textOutput,
      mid_turn: "accept",
      model_callable: false
    }
  ];
  const hostile = [
    { ...agents[0]!, key: "long-agent", workflow_type: "LongNameAgent", label: "Very long agent name ".repeat(15).slice(0, 300) },
    { ...agents[0]!, key: "intl-agent", workflow_type: "IntlAgent", label: "وكيل السفر 王秀英 👩‍👩‍👧‍👦" },
    { ...agents[0]!, key: "token-agent", workflow_type: "TokenAgent", label: "y".repeat(2000) }
  ];
  const sessions = [agents[1]!, ...hostile].map((agent, i) => ({
    ...sessionList(1)[0]!,
    workflow_id: `agent-session-scale-selects-${i}`,
    agent_workflow_type: agent.workflow_type,
    label: i === 0 ? "Select stress" : agent.label
  }));
  return { agents: [agents[0]!, agents[1]!, ...hostile], sessions, frames: [], agentInterface: stressInterface };
}

const tokenModels = [
  "gpt-5.4-mini",
  "gemini-3.5-flash",
  `claude-sonnet-${"long-model-id-".repeat(12)}2026-09-30`,
  "王秀英-model-نموذج"
];

/* Token pane fixtures. `tokens` is a long multi-model run: 400 turns, each a large planning call and
   a few small ones, an automatic approval evaluation every 25th turn, some calls sharing an instant.
   `tokensunknown` is short, and two of its calls end with no usage at all, one of them from a model
   that never reports any. Both carry every token category: gpt-5.4-mini leaves tool-use null as
   OpenAI does, gemini-3.5-flash totals thought and tool-use beside output as Gemini does, and the
   short run has one evaluation too. */
function tokenScenario(unknown: boolean): MockScenario {
  const frames: AgentSseFrame[] = [];
  let clock = 0;
  const turns = unknown ? 6 : 400;
  for (let n = 1; n <= turns; n += 1) {
    const m = (dt: number) => meta(n, (clock += dt));
    frames.push(frame("message_accepted", { type: "message_accepted", ...m(1), handler: "ask", payload: { text: firstMessages[n % firstMessages.length] }, disposition: "opened" }));
    frames.push(frame("turn_started", { type: "turn_started", ...m(0.1) }));
    for (let c = 0; c < 1 + (n % 4); c += 1) {
      const model = tokenModels[(n + c) % tokenModels.length]!;
      const big = c === 0;
      const counts = big
        ? usage(8000 + ((n * 977) % 30000), 600 + ((n * 131) % 2400), (n * 53) % 1800, (n * 389) % 6000)
        : usage(400 + ((n * c * 71) % 900), 40 + ((n * 17) % 200), 0, 0, (n * c * 7) % 120);
      const spent =
        model === tokenModels[0]
          ? { ...counts, tool_use_tokens: null }
          : model === tokenModels[1]
            ? { ...counts, total_tokens: counts.input_tokens! + counts.output_tokens! + counts.thought_tokens! + counts.tool_use_tokens! }
            : counts;
      const silent = unknown && c === 0 && (n === 3 || n === 5);
      const called = silent ? "relay-without-usage" : model;
      frames.push(frame("model_interaction_started", { type: "model_interaction_started", ...m(0.2), model: called }));
      frames.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...m(big ? 6 : c === 2 ? 0 : 1.5), model: called, usage: silent ? null : spent }));
    }
    if (n % 25 === 0 || (unknown && n === 4)) {
      frames.push(frame("auto_approval_evaluation_ended", { type: "auto_approval_evaluation_ended", ...m(2), evaluation_id: `eval-${n}`, tool_id: `tool-eval-${n}`, tool_name: "write_session_note", evaluator: "jev_evaluator", verdict: "approve", reason: "Policy criteria passed", details: { model: "gemini-3.1-flash-lite", usage: { input_tokens: 2400, output_tokens: 90, total_tokens: 2490 } } }));
    }
    frames.push(frame("turn_end", { type: "turn_end", ...m(0.5) }));
  }
  const label = unknown ? "Calls with no usage" : "Long multi-model run";
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-${unknown ? "tokensunknown" : "tokens"}`, agent_workflow_type: "QaAgent", label }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* Per-model usage shapes for the token chart, as `?mock=usage-<variant>`: one model, two a thousandfold
   apart, nine, a model that only ever reports zero, very long ids, billions, one call, no calls, and
   two calls either side of an hour-long callback wait that an approval and two tools overlap. */
const usageVariants: Record<string, { models: string[]; calls: number; scale: (model: number) => number; waits?: true }> = {
  one: { models: ["gpt-5.4-mini"], calls: 30, scale: () => 1 },
  ratio: { models: ["gpt-5.4", "gpt-5.4-nano"], calls: 30, scale: (k) => (k === 0 ? 1000 : 1) },
  many: { models: Array.from({ length: 9 }, (_, k) => `model-${k + 1}`), calls: 60, scale: (k) => 4 ** (8 - k) },
  zeros: { models: ["gpt-5.4-mini", "zero-reporter"], calls: 30, scale: (k) => (k === 0 ? 1 : 0) },
  longids: { models: tokenModels.slice(2).concat(`provider/${"x".repeat(300)}`), calls: 30, scale: (k) => 10 ** k },
  billions: { models: ["gpt-5.4", "gemini-3.5-flash"], calls: 30, scale: (k) => (k === 0 ? 4_000_000 : 3_000) },
  single: { models: ["gpt-5.4-mini"], calls: 1, scale: () => 1 },
  empty: { models: [], calls: 0, scale: () => 1 },
  waits: { models: ["jev-1.13.0", "gemini-3.8-flash"], calls: 2, scale: (k) => (k === 0 ? 40 : 1), waits: true }
};

function usageScenario(variant: string): MockScenario | undefined {
  const shape = usageVariants[variant];
  if (!shape) return undefined;
  let clock = 0;
  const m = (dt: number) => meta(1, (clock += dt));
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...m(0), handler: "ask", payload: { text: firstMessages[0]! }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...m(0.1) })
  ];
  for (let n = 0; n < shape.calls; n += 1) {
    const k = n % shape.models.length;
    const model = shape.models[k]!;
    const f = shape.scale(k);
    frames.push(frame("model_interaction_started", { type: "model_interaction_started", ...m(0.5), model }));
    frames.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...m(2 + (n % 3)), model, usage: usage(Math.round((300 + ((n * 37) % 500)) * f), Math.round(60 * f), 0, 0) }));
    if (shape.waits && n === 0) {
      const ask = { tool_id: "t-ask", tool_name: "ask_user", tool_input: { question: "Ship it?" } };
      const gate = { tool_id: "t-gate", tool_name: "deploy", tool_input: {} };
      frames.push(
        frame("tool_start", { type: "tool_start", ...m(1), ...ask }),
        frame("callback_requested", { type: "callback_requested", ...m(0), ...ask, output_schema: { type: "string" } }),
        frame("tool_requested", { type: "tool_requested", ...m(600), ...gate }),
        frame("tool_approval_requested", { type: "tool_approval_requested", ...m(0), ...gate })
      );
      for (const [at, id] of [[840, "t-read-1"], [1800, "t-read-2"]] as const) {
        const read = { tool_id: id, tool_name: "read_file", tool_input: { path: "README.md" } };
        frames.push(frame("tool_start", { type: "tool_start", ...m(at), ...read }), frame("tool_end", { type: "tool_end", ...m(4), ...read, tool_output: "ok" }));
      }
      frames.push(
        frame("tool_approval_resolved", { type: "tool_approval_resolved", ...m(60), ...gate, approved: true, reason: null, remember: false }),
        frame("callback_resolved", { type: "callback_resolved", ...m(800), tool_id: ask.tool_id, tool_name: ask.tool_name, outcome: "ok", error: null }),
        frame("tool_end", { type: "tool_end", ...m(1), ...ask, tool_output: "yes" })
      );
    }
  }
  frames.push(frame("turn_end", { type: "turn_end", ...m(0.5) }));
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-usage-${variant}`, agent_workflow_type: "QaAgent", label: `Usage: ${variant}` }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* The run-wide timeline at its worst: 0s, millisecond, 22-minute and three-hour spans in one run,
   1,000 repeated approval checks, tool names that are unbroken, emoji, CJK, RTL or markup, a
   subagent that starts its own subagent, and a tool still running at the right edge. */
function timelineWorst(): MockScenario {
  const at = (dt: number) => meta(1, dt);
  const child = "7f3c1a-c0ffee";
  const grandchild = `${child}-0ddba1`;
  const inChild = (agent: string, dt: number) => ({ ...meta(1, dt), agent_id: agent, turn_id: `${agent}-turn-001` });
  const model = "gemini-3.8-flash";
  const tool = (tool_id: string, tool_name: string) => ({ tool_id, tool_name, tool_input: {} });
  const span = (t: ReturnType<typeof tool>, start: number, end: number | null, where = at): AgentSseFrame[] => [
    frame("tool_start", { type: "tool_start", ...where(start), ...t }),
    ...(end == null ? [] : [frame("tool_end", { type: "tool_end", ...where(end), ...t, tool_output: "ok" })])
  ];
  /* Three more generations under the grandchild, five subagents deep in all, the deepest
     waiting on a client callback. Each agent id is its parent's plus one segment. */
  const nestedSubagents = (root: string, t0: number): AgentSseFrame[] => {
    const keys = ["seat_mapper", "price_oracle_with_an_unreasonably_long_agent_key_name", "مدقق_الأسعار"];
    let lineage = root;
    const ids = keys.map((_, depth) => (lineage = `${lineage}-a${depth}a${depth}a${depth}`));
    const out: AgentSseFrame[] = [];
    keys.forEach((agent_key, depth) => {
      const t = t0 + depth * 0.1;
      const parent = depth === 0 ? root : ids[depth - 1]!;
      const id = ids[depth]!;
      const here = (dt: number) => inChild(id, dt);
      const lookup = tool(`n-${depth}`, `lookup_level_${depth + 3}`);
      out.push(
        frame("subagent_started", { type: "subagent_started", ...inChild(parent, t), subagent_id: id, agent_key, workflow_id: `wf-nested-${depth}` }),
        frame("turn_started", { type: "turn_started", ...here(t + 0.01) }),
        ...(depth === keys.length - 1
          ? [frame("callback_requested", { type: "callback_requested", ...here(t + 0.015), ...lookup, output_schema: {} })]
          : []),
        ...span(lookup, t + 0.02, t + (depth === keys.length - 1 ? 0.4 : 0.05), here)
      );
    });
    keys.forEach((agent_key, depth) => {
      const t = t0 + 0.6 + (keys.length - depth) * 0.05;
      const id = ids[depth]!;
      if (depth === keys.length - 1) {
        out.push(frame("callback_resolved", { type: "callback_resolved", ...inChild(id, t - 0.2), tool_id: "n-2", tool_name: "lookup_level_5", outcome: "ok", error: null }));
      }
      out.push(frame("turn_end", { type: "turn_end", ...inChild(id, t) }));
    });
    keys.toReversed().forEach((agent_key, reversed) => {
      const depth = keys.length - 1 - reversed;
      const parent = depth === 0 ? root : ids[depth - 1]!;
      out.push(frame("subagent_stopped", { type: "subagent_stopped", ...inChild(parent, t0 + 0.8 + reversed * 0.02), subagent_id: ids[depth]!, agent_key, workflow_id: `wf-nested-${depth}` }));
    });
    return out;
  };
  const host = tool("s-long", "run_travel_code");
  const book = tool("h-book", "book_flight");
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...at(0), handler: "ask", payload: { text: "Book flights for 10 people from Las Vegas to New York" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...at(0) }),
    frame("model_interaction_started", { type: "model_interaction_started", ...at(0.1), model }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...at(8.1), model, usage: usage(2100, 600, 0, 64) }),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...at(8.2), ...host }),
    frame("auto_approval_evaluation_started", { type: "auto_approval_evaluation_started", ...at(8.2), tool_id: host.tool_id, tool_name: host.tool_name, evaluation_id: "eval-host", evaluator: "jev_evaluator" }),
    frame("auto_approval_evaluation_ended", { type: "auto_approval_evaluation_ended", ...at(8.2), tool_id: host.tool_id, tool_name: host.tool_name, evaluation_id: "eval-host", evaluator: "jev_evaluator", verdict: "escalate", reason: "jev: escalate", details: {} }),
    frame("tool_approval_resolved", { type: "tool_approval_resolved", ...at(31.2), tool_id: host.tool_id, tool_name: host.tool_name, approved: true, reason: null, remember: false }),
    frame("tool_start", { type: "tool_start", ...at(31.3), ...host }),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...at(31.4), ...book }),
    ...span(tool("t-ms", "complete_trip_tasks"), 31.5, 31.504)
  ];
  /* The first 200 checks all judge the one book_flight gate, so it has 200 children; the rest
     each sit under a gate of their own, which is 800 repeated approvals. */
  for (let i = 0; i < 1_000; i += 1) {
    const gated = i < 200 ? book : tool(`h-check-${i}`, "book_flight");
    const judged = { tool_id: gated.tool_id, tool_name: gated.tool_name, evaluation_id: `eval-${i}`, evaluator: "jev_evaluator" };
    const t = 40 + i * 1.2;
    if (i >= 200) frames.push(frame("tool_approval_requested", { type: "tool_approval_requested", ...at(t), ...gated }));
    frames.push(
      frame("auto_approval_evaluation_started", { type: "auto_approval_evaluation_started", ...at(t), ...judged }),
      frame("auto_approval_evaluation_ended", { type: "auto_approval_evaluation_ended", ...at(t + (i % 5 === 0 ? 0.004 : 0)), ...judged, verdict: "approve", reason: "jev: approve", details: {} })
    );
    if (i >= 200) frames.push(frame("tool_approval_resolved", { type: "tool_approval_resolved", ...at(t + 0.5), tool_id: gated.tool_id, tool_name: gated.tool_name, approved: true, reason: null, remember: false }));
  }
  frames.push(
    frame("tool_approval_resolved", { type: "tool_approval_resolved", ...at(31.4 + 1_362), tool_id: book.tool_id, tool_name: book.tool_name, approved: true, reason: null, remember: false }),
    frame("tool_end", { type: "tool_end", ...at(31.3 + 1_368), ...host, tool_output: "ok" })
  );
  [longToken.repeat(5).slice(0, 2000), "👩🏽‍💻_予約_フライト", "حجز_الرحلة", "<script>alert(1)</script> **bold**", "  padded\tname\n "].forEach((name, i) => {
    frames.push(...span(tool(`t-worst-${i}`, name), 1_400 + i * 4, 1_402 + i * 4));
  });
  frames.push(
    frame("subagent_started", { type: "subagent_started", ...at(1_430), subagent_id: child, agent_key: "research", workflow_id: "wf-research" }),
    frame("message_accepted", { type: "message_accepted", ...inChild(child, 1_430.1), handler: "ask", payload: { text: "find fares" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...inChild(child, 1_430.1) }),
    ...span(tool("c-1", "search_flights"), 1_431, 1_436, (dt) => inChild(child, dt)),
    frame("subagent_started", { type: "subagent_started", ...inChild(child, 1_437), subagent_id: grandchild, agent_key: "fare_checker", workflow_id: "wf-fare" }),
    frame("message_accepted", { type: "message_accepted", ...inChild(grandchild, 1_437.1), handler: "ask", payload: { text: "check" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...inChild(grandchild, 1_437.1) }),
    ...span(tool("g-1", "approval check · jev_evaluator"), 1_438, 1_438, (dt) => inChild(grandchild, dt)),
    ...nestedSubagents(grandchild, 1_438.1),
    frame("turn_end", { type: "turn_end", ...inChild(grandchild, 1_439) }),
    frame("subagent_stopped", { type: "subagent_stopped", ...inChild(child, 1_440), subagent_id: grandchild, agent_key: "fare_checker", workflow_id: "wf-fare" }),
    frame("turn_end", { type: "turn_end", ...inChild(child, 1_441) }),
    frame("subagent_stopped", { type: "subagent_stopped", ...at(1_442), subagent_id: child, agent_key: "research", workflow_id: "wf-research" }),
    frame("tool_start", { type: "tool_start", ...at(1_450), ...tool("t-hours", "nightly_reconcile_ledger") }),
    ...span(tool("t-open", "stream_ledger_export"), 1_450 + 3 * 3_600 - 30, null),
    frame("tool_end", { type: "tool_end", ...at(1_450 + 3 * 3_600), ...tool("t-hours", "nightly_reconcile_ledger"), tool_output: "ok" })
  );
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-scale-timelineworst", agent_workflow_type: "QaAgent", label: "Timeline worst case" }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* A monty run: a burst of 4–9s calls at 22m, 53 quiet minutes that an approval wait and a
   still-open subagent run straight through, and a second burst at 1h16m. */
function timelineGap(): MockScenario {
  const at = (dt: number) => meta(1, dt);
  const model = "gemini-3.8-flash";
  const script = { tool_id: "s-1", tool_name: "monty_run_script", tool_input: {} };
  const late = { tool_id: "s-2", tool_name: "monty_run_script", tool_input: {} };
  const call = (start: number, seconds: number): AgentSseFrame[] => [
    frame("model_interaction_started", { type: "model_interaction_started", ...at(start), model }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...at(start + seconds), model, usage: usage(1800, 400, 0, 32) })
  ];
  const tool = (t: typeof script, start: number, seconds: number): AgentSseFrame[] => [
    frame("tool_start", { type: "tool_start", ...at(start), ...t }),
    frame("tool_end", { type: "tool_end", ...at(start + seconds), ...t, tool_output: "ok" })
  ];
  const t0 = 22 * 60;
  const t1 = 76 * 60 + 10;
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...at(0), handler: "ask", payload: { text: "Find me a flight to Lisbon" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...at(0) }),
    ...call(1, 5.2),
    ...tool({ tool_id: "f-1", tool_name: "search_flights", tool_input: {} }, t0, 3.9),
    ...call(t0 + 4, 4.1),
    ...tool(script, t0 + 9, 4.7),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...at(t0 + 14), ...late }),
    frame("subagent_started", { type: "subagent_started", ...at(t0 + 15), subagent_id: "watch-1", agent_key: "fare_watcher", workflow_id: "wf-fare-watch" }),
    ...call(t0 + 16, 6.9),
    frame("tool_approval_resolved", { type: "tool_approval_resolved", ...at(t1), tool_id: late.tool_id, tool_name: late.tool_name, approved: true, reason: null, remember: false }),
    ...call(t1 + 1, 9.2),
    ...call(t1 + 11, 6),
    ...tool(late, t1 + 18, 8),
    ...call(t1 + 27, 7.6)
  ];
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-scale-timelinegap", agent_workflow_type: "MontyDynamicAgent", label: "Timeline idle gap" }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* The worst timeline, minus the three-hour tool, plus open tools that start after the idle
   stretch so none of them paints across it. */
function timelineCaps(): MockScenario {
  const base = timelineWorst();
  const frames = base.frames.filter((item) => {
    const id = "tool_id" in item.data ? item.data.tool_id : undefined;
    return id !== "t-hours" && id !== "t-open";
  });
  const at = (dt: number) => meta(1, dt);
  const tool = (tool_id: string, tool_name: string) => ({ tool_id, tool_name, tool_input: {} });
  const span = (t: ReturnType<typeof tool>, start: number, end: number): AgentSseFrame[] => [
    frame("tool_start", { type: "tool_start", ...at(start), ...t }),
    frame("tool_end", { type: "tool_end", ...at(end), ...t, tool_output: "ok" })
  ];
  /* After the folded host call, nothing runs until these. That hole is the idle gap;
     the open tools start on the far side of it so an ongoing span cannot fill it. */
  const liveStart = 3_000;
  const liveNames = [
    longToken.repeat(5).slice(0, 2000),
    "進行中_导出",
    "تصدير_جارٍ",
    "👩🏽‍💻_live",
    "<script>alert(1)</script> **bold**"
  ];
  frames.push(...span(tool("t-point-0", "point_at_origin"), 0, 0));
  frames.push(...span(tool("t-point-end", "point_at_end"), liveStart + 24, liveStart + 24));
  for (let i = 0; i < 60; i += 1) {
    const name = liveNames[i] ?? `live_export_${i}`;
    frames.push(frame("tool_start", { type: "tool_start", ...at(liveStart + i * 0.4), ...tool(`t-live-${i}`, name) }));
  }
  const sessions = [{
    ...sessionList(1)[0]!,
    workflow_id: "agent-session-scale-timelinecaps",
    agent_workflow_type: "QaAgent",
    label: "Timeline caps"
  }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* The Logs pane at its worst. Turn 1: a Code Mode host whose script makes calls (three levels of
   nesting), an approval still waiting, a denied auto-approval, 120 identical state commits with one
   tool failure in the middle of them, a 2,000-character unbroken output, a multi-line traceback and
   emoji, CJK, RTL and markup in names and messages. Turns 2..9 are routine volume, ~400 rows. */
function logsWorst(): MockScenario {
  let clock = 0;
  const at = (n: number, dt = 0.05) => meta(n, (clock += dt));
  const tool = (tool_id: string, tool_name: string, tool_input: JsonRecord = {}) => ({ tool_id, tool_name, tool_input });
  const call = (n: number, t: ReturnType<typeof tool>, output: string): AgentSseFrame[] => [
    frame("tool_requested", { type: "tool_requested", ...at(n), ...t }),
    frame("tool_start", { type: "tool_start", ...at(n), ...t }),
    frame("tool_end", { type: "tool_end", ...at(n, 0.4), ...t, tool_output: output })
  ];
  const host = tool("lw-host", "run_temporal_code", { script: "for city in cities:\n    search_flights(city)\n" });
  const flaky = tool("lw-flaky", "fetch_krakow_emea_shared_services_export");
  const gated = tool("lw-gated", "write_session_note", { title: "نور الهدى عبد الرحمن — 王秀英 👩🏽‍💻" });
  const denied = tool("lw-denied", "delete_customer_records");
  const frames: AgentSseFrame[] = [
    frame("message_accepted", { type: "message_accepted", ...at(1), handler: "ask", payload: { text: "👩🏽‍💻 Priya: 王秀英 asked نور الهدى عبد الرحمن <script>alert(1)</script> **bold** # heading" }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...at(1) }),
    frame("model_interaction_started", { type: "model_interaction_started", ...at(1), model: "gpt-5.4-mini" }),
    frame("model_interaction_ended", { type: "model_interaction_ended", ...at(1, 2), model: "gpt-5.4-mini", usage: usage(2100, 600, 0, 64) }),
    frame("tool_requested", { type: "tool_requested", ...at(1), ...host }),
    frame("tool_start", { type: "tool_start", ...at(1), ...host }),
    ...call(1, tool("lw-c1", "search_flights"), "LAS → JFK: 12 fares"),
    ...call(1, tool("lw-c2", "検索_フライト_✈️"), "東京 → 大阪"),
    ...call(1, tool("lw-c3", "بحث_الرحلات"), "لا توجد نتائج"),
    frame("tool_end", { type: "tool_end", ...at(1, 1), ...host, tool_output: longToken.repeat(5).slice(0, 2000) })
  ];
  for (let i = 0; i < 120; i += 1) {
    if (i === 60) {
      frames.push(frame("tool_error", { type: "tool_error", ...at(1), ...flaky, message: "Traceback (most recent call last):\n  File \"export.py\", line 41, in run\n    raise TimeoutError(\"Upstream 503 after 3 attempts\")\nTimeoutError: Upstream 503 after 3 attempts" }));
    }
    frames.push(frame("state_patch", { type: "state_patch", ...at(1, 0.01), state_id: "poll", version: i + 1, ops: [{ op: "replace", path: "/status", value: "pending" }] }));
  }
  frames.push(
    frame("tool_approval_requested", { type: "tool_approval_requested", ...at(1), ...denied }),
    frame("auto_approval_evaluation_started", { type: "auto_approval_evaluation_started", ...at(1), tool_id: denied.tool_id, tool_name: denied.tool_name, evaluation_id: "lw-eval", evaluator: "jev_evaluator" }),
    frame("auto_approval_evaluation_ended", { type: "auto_approval_evaluation_ended", ...at(1), tool_id: denied.tool_id, tool_name: denied.tool_name, evaluation_id: "lw-eval", evaluator: "jev_evaluator", verdict: "deny", reason: "jev: deletes customer data", details: {} }),
    frame("tool_approval_resolved", { type: "tool_approval_resolved", ...at(1), tool_id: denied.tool_id, tool_name: denied.tool_name, approved: false, reason: "jev: deletes customer data", remember: false }),
    frame("tool_requested", { type: "tool_requested", ...at(1), ...tool("lw-long", `fetch_${longToken.slice(0, 300)}`) }),
    frame("tool_requested", { type: "tool_requested", ...at(1), ...gated }),
    frame("tool_approval_requested", { type: "tool_approval_requested", ...at(1), ...gated })
  );
  /* Subagents six deep, each on a long workflow id; the deepest one calls a tool with large
     nested input, so its details open six levels in. */
  let by: { agent_id?: string; turn_id?: string; message_id?: string } = {};
  for (let d = 1; d <= 6; d += 1) {
    const id = `lw-sub-${d}`;
    const ids = { subagent_id: id, agent_key: `nested_${d}`, workflow_id: `agent-session-lw-depth-${d}-${longToken.slice(0, 160)}` };
    const scope = { agent_id: id, turn_id: `${id}-turn-001`, message_id: `${id}-msg-1` };
    frames.push(
      frame("subagent_started", { type: "subagent_started", ...at(1), ...by, ...ids }),
      frame("subagent_message_sent", { type: "subagent_message_sent", ...at(1), ...by, ...ids, handler: "research", subagent_turn: 1, from_offset: 0 }),
      frame("message_accepted", { type: "message_accepted", ...at(1), ...scope, handler: "research", payload: { text: `Level ${d}` }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...at(1), ...scope })
    );
    by = scope;
  }
  const deep = tool("lw-deep", "search_flights", {
    query: { from: "東京", to: "القاهرة", who: "👩🏽‍💻 نور الهدى", legs: Array.from({ length: 6 }, (_, i) => ({ leg: i, via: { city: `city_${i}`, notes: ["<script>alert(1)</script>", "**bold**"] } })) },
    path: `/mnt/session/${"very_long_directory_name/".repeat(12)}result.json`,
    token: longToken.repeat(5).slice(0, 2000)
  });
  frames.push(
    frame("tool_requested", { type: "tool_requested", ...at(1), ...by, ...deep }),
    frame("tool_start", { type: "tool_start", ...at(1), ...by, ...deep }),
    frame("tool_end", { type: "tool_end", ...at(1, 0.4), ...by, ...deep, tool_output: "{}" })
  );
  for (let n = 2; n <= 9; n += 1) {
    frames.push(
      frame("message_accepted", { type: "message_accepted", ...at(n), handler: "ask", payload: { text: firstMessages[n % firstMessages.length]! }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...at(n) })
    );
    for (let r = 0; r < 8; r += 1) {
      frames.push(
        frame("model_interaction_started", { type: "model_interaction_started", ...at(n), model: "gpt-5.4-mini" }),
        frame("model_interaction_ended", { type: "model_interaction_ended", ...at(n, 1.2), model: "gpt-5.4-mini", usage: usage(900, 60, 0, 300) }),
        ...call(n, tool(`lw-${n}-${r}`, toolNames[r % toolNames.length]!), "{}")
      );
    }
    frames.push(
      frame("message_handler_end", { type: "message_handler_end", ...at(n), output: { text: "Done." } }),
      frame("turn_end", { type: "turn_end", ...at(n) })
    );
  }
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-scale-logsworst", agent_workflow_type: "QaAgent", label: "Logs worst case" }];
  return { agents, sessions, frames, instantFrames: frames.length };
}

/* `?mock=subagents&data=demo|worst|empty|one|many`: one parent turn fanning out to subagents in
   every state the State Flow's Subagent activity container can show. The unavailable marker is
   shaped exactly as stream_merge/merge.py synthesizes it (turn_id "", timestamp 0, agent_id = the
   subagent's id) and lands just before the parent's reply_received, as the merge orders it. */
type SubagentFate =
  | "done"
  | "running"
  | "failed"
  | "unavailable-ok"
  | "unavailable-error"
  | "unavailable-running"
  | "unavailable-partial"
  | "never-started"
  | "late";
interface SubagentSpec {
  key: string;
  fate: SubagentFate;
  reason?: string;
  /* Not on the wire yet; here so the console's handling of it can be seen. */
  reasonCode?: SubagentUnavailableCode;
  children?: SubagentSpec[];
}
const mergeReason =
  "subagent stream unavailable (its workflow has completed and is not yet replayable, or it stalled without delivering its turn) — refresh to retry";

function subagentScenario(data: string): MockScenario {
  const rootId = "7f3c1a";
  let clock = 0;
  let seq = 0;
  const parent = (dt = 0.05) => meta(1, (clock += dt));
  const frames: AgentSseFrame[] = [];
  const late: AgentSseFrame[] = [];

  function childTurn(id: string, key: string, fate: SubagentFate): AgentSseFrame[] {
    const at = () => ({ ...meta(1, (clock += 0.05)), agent_id: id, turn_id: `${id}-turn-001`, message_id: `${id}-msg-1` });
    const out = [
      frame("message_accepted", { type: "message_accepted", ...at(), handler: "research", payload: { text: `Look into ${key}` }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...at() }),
      frame("model_interaction_started", { type: "model_interaction_started", ...at(), model: "gpt-5.4-mini" })
    ];
    if (fate === "running") return out;
    out.push(frame("model_interaction_ended", { type: "model_interaction_ended", ...at(), model: "gpt-5.4-mini", usage: usage(900, 60, 0, 300) }));
    out.push(
      fate === "failed"
        ? frame("message_handler_error", { type: "message_handler_error", ...at(), message: "ValidationError: 1 validation error for ResearchReply\nsources\n  Field required" })
        : frame("message_handler_end", { type: "message_handler_end", ...at(), output: { text: `Findings for ${key}.` } })
    );
    out.push(frame("turn_end", { type: "turn_end", ...at() }));
    return out;
  }

  function dispatch(spec: SubagentSpec, parentId: string): void {
    const id = `${parentId}-${(0xb00000 + (seq += 1)).toString(16)}`;
    /* Per variant: the console remembers closed workflow ids across reloads. */
    const workflow_id = `agent-session-subagent-${data}-${seq}`;
    const by = parentId === rootId ? {} : { agent_id: parentId, turn_id: `${parentId}-turn-001`, message_id: `${parentId}-msg-1` };
    const ids = { subagent_id: id, agent_key: spec.key, workflow_id };
    frames.push(frame("subagent_started", { type: "subagent_started", ...parent(), ...by, ...ids }));
    if (spec.fate === "never-started") return;
    frames.push(frame("subagent_message_sent", { type: "subagent_message_sent", ...parent(), ...by, ...ids, handler: "research", subagent_turn: 1, from_offset: 0 }));
    const unavailable = spec.fate.startsWith("unavailable") || spec.fate === "late";
    if (!unavailable) {
      const turn = childTurn(id, spec.key, spec.fate);
      frames.push(...turn.slice(0, 3));
      for (const child of spec.children ?? []) dispatch(child, id);
      frames.push(...turn.slice(3));
    }
    if (unavailable) {
      /* Streamed its opening frames, then the merge gave up on it mid-turn. */
      if (spec.fate === "unavailable-partial") frames.push(...childTurn(id, spec.key, "running"));
      frames.push(frame("subagent_stream_unavailable", { type: "subagent_stream_unavailable", agent_id: id, turn_id: "", turn_number: 0, message_id: null, timestamp: 0, subagent_id: id, workflow_id, reason: spec.reason ?? mergeReason, ...(spec.reasonCode ? { reason_code: spec.reasonCode } : {}) }));
      if (spec.fate === "late") late.push(...childTurn(id, spec.key, "done"));
    }
    if (spec.fate === "running" || spec.fate === "unavailable-running") return;
    const outcome = spec.fate === "failed" || spec.fate === "unavailable-error" ? "error" : "ok";
    frames.push(frame("subagent_reply_received", { type: "subagent_reply_received", ...parent(), ...by, ...ids, handler: "research", subagent_turn: 1, outcome }));
    if (spec.fate !== "late") frames.push(frame("subagent_stopped", { type: "subagent_stopped", ...parent(), ...by, ...ids }));
  }

  const fates: SubagentFate[] = ["done", "running", "failed", "unavailable-ok", "unavailable-error", "unavailable-running", "never-started"];
  const specs: SubagentSpec[] =
    data === "worst"
      ? [
          { key: "quarterly_revenue_reconciliation_agent_for_emea_shared_services_kraków_office_including_late_adjustments_and_fx_v2", fate: "done" },
          { key: "顧客データ重複排除エージェント", fate: "running" },
          { key: "🔎 research", fate: "failed" },
          { key: "وكيل_البحث_عن_المصادر", fate: "unavailable-ok" },
          { key: "billing_auditor", fate: "unavailable-error", reason: `${mergeReason}. ${"The child workflow agent-session-subagent-5 completed at 2026-10-22T15:36:28Z after 3 retried model calls; its WorkflowStream has not been archived for replay yet, and the per-workflow concurrent update cap (10) was reached while 9 sibling cursors were still polling. ".repeat(2)}`.slice(0, 500) },
          { key: "summarizer", fate: "unavailable-running", reason: "" },
          { key: "partial_streamer", fate: "unavailable-partial", reasonCode: "ended_mid_turn" },
          { key: "a", fate: "never-started" },
          { key: "late_reader", fate: "late" },
          { key: "planner", fate: "done", children: [{ key: "researcher", fate: "done", children: [{ key: "fetcher", fate: "unavailable-ok" }] }] },
          { key: "<script>alert(1)</script>", fate: "done" },
          { key: "research", fate: "done" },
          { key: "research", fate: "unavailable-ok" }
        ]
      : data === "one"
        ? [{ key: "research", fate: "unavailable-ok" }]
        : data === "many"
          ? Array.from({ length: 60 }, (_, i) => ({ key: `worker_${String(i + 1).padStart(2, "0")}`, fate: fates[i % fates.length]! }))
          : data === "empty"
            ? []
            : [{ key: "research", fate: "done" }, { key: "cleaner", fate: "unavailable-ok" }];

  frames.push(
    frame("message_accepted", { type: "message_accepted", ...parent(), handler: "ask", payload: { text: "Fan this out to the research team." }, disposition: "opened" }),
    frame("turn_started", { type: "turn_started", ...parent() }),
    frame("model_interaction_started", { type: "model_interaction_started", ...parent(), model: "gpt-5.4-mini" })
  );
  for (const spec of specs) dispatch(spec, rootId);
  /* Paced tail: the parent keeps streaming for a few seconds, then the late child's stream reads. */
  const tail: AgentSseFrame[] = late.length
    ? [...Array.from({ length: 120 }, (_, i) => frame("reply_delta", { type: "reply_delta", ...parent(), text: i ? " …" : "Collecting replies" })), ...late]
    : [];
  frames.push(...tail);
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-subagents-${data}`, agent_workflow_type: "QaAgent", label: `Subagents ${data}`, execution_status: "RUNNING", closed: false }];
  return { agents, sessions, frames, instantFrames: frames.length - tail.length };
}

/* ~100,000 frames in one open run of 540 turns: each turn is 30 rounds of one model call and two
   tools, every 10th gates a tool on an approval (every 50th a 30-minute wait), every 20th sends a
   subagent that sends one of its own, and every 40th follows an hour idle. After the backlog,
   a live tail of 1,500 frames lands at 50 a second on the still-open last turn; run100ksettled
   replays the tail with the backlog. */
function run100k(settled = false): MockScenario {
  const models = ["gpt-5.4-mini", "gemini-3.8-flash", "claude-sonnet-4.5"];
  let clock = 0;
  const frames: AgentSseFrame[] = [];
  const rounds = (turn: number, count: number, scope: { agent_id?: string; turn_id?: string; message_id?: string } = {}, step = 0.4) => {
    const at = (dt = step) => ({ ...meta(turn, (clock += dt)), ...scope });
    for (let r = 0; r < count; r += 1) {
      const model = models[(turn + r) % models.length]!;
      frames.push(
        frame("model_interaction_started", { type: "model_interaction_started", ...at(), model }),
        frame("model_interaction_ended", { type: "model_interaction_ended", ...at(1 + (r % 4)), model, usage: usage(1200 + r * 10, 180, r % 3 ? 0 : 90, 400) })
      );
      for (const k of [0, 1]) {
        const tool = { tool_id: `${scope.agent_id ?? "root"}-${turn}-${r}-${k}`, tool_name: toolNames[(turn + r + k) % toolNames.length]!, tool_input: {} };
        frames.push(frame("tool_start", { type: "tool_start", ...at(0.05), ...tool }));
        frames.push((turn * 31 + r * 7 + k) % 499 === 0
          ? frame("tool_error", { type: "tool_error", ...at(0.3 + k), ...tool, message: "Upstream 503 after 3 attempts" })
          : frame("tool_end", { type: "tool_end", ...at(0.3 + k), ...tool, tool_output: "{}" }));
      }
    }
  };
  const turns = 540;
  for (let n = 1; n <= turns; n += 1) {
    if (n % 40 === 0) clock += 3_600;
    const at = (dt = 0.2) => meta(n, (clock += dt));
    frames.push(
      frame("message_accepted", { type: "message_accepted", ...at(), handler: "ask", payload: { text: firstMessages[n % firstMessages.length] }, disposition: "opened" }),
      frame("turn_started", { type: "turn_started", ...at() })
    );
    if (n % 10 === 0) {
      const gated = { tool_id: `gate-${n}`, tool_name: "refund_customer", tool_input: { amount: n } };
      frames.push(
        frame("tool_requested", { type: "tool_requested", ...at(), ...gated }),
        frame("tool_approval_requested", { type: "tool_approval_requested", ...at(), ...gated }),
        frame("tool_approval_resolved", { type: "tool_approval_resolved", ...at(n % 50 === 0 ? 1_800 : 90), tool_id: gated.tool_id, tool_name: gated.tool_name, approved: true, reason: null, remember: false }),
        frame("tool_start", { type: "tool_start", ...at(), ...gated }),
        frame("tool_end", { type: "tool_end", ...at(2), ...gated, tool_output: "ok" })
      );
    }
    if (n % 20 === 0) {
      const child = `7f3c1a-run-${n}`;
      const grandchild = `${child}-fare`;
      const scope = (id: string) => ({ agent_id: id, turn_id: `${id}-turn-001`, message_id: `${id}-msg-1` });
      const ids = (id: string, agent_key: string) => ({ subagent_id: id, agent_key, workflow_id: `wf-${id}` });
      frames.push(frame("subagent_started", { type: "subagent_started", ...at(), ...ids(child, "research") }));
      frames.push(frame("subagent_message_sent", { type: "subagent_message_sent", ...at(), ...ids(child, "research"), handler: "research", subagent_turn: 1, from_offset: 0 }));
      frames.push(frame("turn_started", { type: "turn_started", ...at(), ...scope(child) }));
      rounds(n, 4, scope(child));
      frames.push(frame("subagent_started", { type: "subagent_started", ...at(), ...scope(child), ...ids(grandchild, "fare_checker") }));
      frames.push(frame("turn_started", { type: "turn_started", ...at(), ...scope(grandchild) }));
      rounds(n, 3, scope(grandchild));
      frames.push(frame("turn_end", { type: "turn_end", ...at(), ...scope(grandchild) }));
      frames.push(frame("subagent_stopped", { type: "subagent_stopped", ...at(), ...scope(child), ...ids(grandchild, "fare_checker") }));
      frames.push(frame("turn_end", { type: "turn_end", ...at(), ...scope(child) }));
      frames.push(frame("subagent_reply_received", { type: "subagent_reply_received", ...at(), ...ids(child, "research"), handler: "research", subagent_turn: 1, outcome: "ok" }));
      frames.push(frame("subagent_stopped", { type: "subagent_stopped", ...at(), ...ids(child, "research") }));
    }
    rounds(n, 30);
    if (n === turns) break;
    frames.push(
      frame("reply_delta", { type: "reply_delta", ...at(), text: `Turn ${n} done.` }),
      frame("message_handler_end", { type: "message_handler_end", ...at(), output: { text: `Turn ${n} done.` } }),
      frame("turn_end", { type: "turn_end", ...at() })
    );
  }
  const backlog = frames.length;
  rounds(turns, 250, {}, 0.02);
  const sessions = [{ ...sessionList(1)[0]!, workflow_id: "agent-session-scale-run100k", agent_workflow_type: "QaAgent", label: "100,000-frame run", execution_status: "RUNNING", closed: false }];
  return { agents, sessions, frames, instantFrames: settled ? frames.length : backlog, liveIntervalMs: 20 };
}

/* Built on demand, and only the one asked for: frame() advances a shared offset counter, so
   module-level fixtures would be a side effect production builds could not drop, and the 100k
   transcript is too costly to build for every other fixture. */
export function scaleScenario(name: string): MockScenario | undefined {
  switch (name) {
    case "sessions200":
      return { agents, sessions: sessionList(200), frames: [] };
    case "sessions10k":
      return { agents, sessions: sessionList(10_000), frames: [] };
    case "sessionsworst":
      return sessionsWorst();
    case "sessionsbreak":
      return sessionsBreak(new URLSearchParams(globalThis.location?.search).get("data") ?? "demo");
    case "transcript5k":
      return streamingTranscript(2500, "5,000 messages");
    case "frames100k":
      return streamingTranscript(12_500, "100,000 frames");
    case "run100k":
    case "run100ksettled":
      return run100k(name === "run100ksettled");
    case "dense5k":
    case "dense50k": {
      const frames = denseTurns(name === "dense5k" ? 1_250 : 12_500);
      const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-${name}` }];
      return { agents, sessions, frames, instantFrames: frames.length };
    }
    case "approvals1":
    case "approvals3":
    case "approvals50":
    case "approvals250":
      return approvalsScenario(Number(name.slice("approvals".length)));
    case "burst10":
      return approvalBurstScenario(10);
    case "panes":
    case "panesworst":
      return panesScenario(name === "panesworst");
    case "panes3k":
      return panesScenario(true, 3_000);
    case "questions":
      return questionsScenario();
    case "actions":
    case "actionsworst":
      return actionsScenario(name === "actionsworst");
    case "handlers":
      return handlersScenario();
    case "selects":
      return selectsScenario();
    case "choices":
    case "choicesworst":
      return choicesScenario(name === "choicesworst");
    case "timelineworst":
      return timelineWorst();
    case "timelinecaps":
      return timelineCaps();
    case "timelinegap":
      return timelineGap();
    case "logsworst":
      return logsWorst();
    case "logsempty":
    case "logsone": {
      const frames = name === "logsone" ? [frame("message_accepted", { type: "message_accepted", ...meta(1, 0), handler: "ask", payload: { text: "x" }, disposition: "opened" })] : [];
      const sessions = [{ ...sessionList(1)[0]!, workflow_id: `agent-session-scale-${name}`, agent_workflow_type: "QaAgent" }];
      return { agents, sessions, frames, instantFrames: frames.length };
    }
    case "tokens":
    case "tokensunknown":
      return tokenScenario(name === "tokensunknown");
    case "okf2k":
      return okfScenario(2_000);
    case "okf10k":
      return okfScenario(10_000);
    /* Three settled sessions sharing one history, for switching between them in a soak run. */
    case "soak": {
      const frames = longTranscript(300, 0);
      const sessions = sessionList(3).map((s) => ({ ...s, agent_workflow_type: "QaAgent", execution_status: "RUNNING", closed: false }));
      return { agents, sessions, frames, instantFrames: frames.length };
    }
    case "agents":
    case "agents300":
      return agentsScenario(name === "agents300");
    case "subagents":
      return subagentScenario(new URLSearchParams(globalThis.location?.search).get("data") ?? "demo");
    default: {
      const mixed = name.match(/^mixed(\d+)x(\d+)$/);
      if (mixed) return mixedScenario(Number(mixed[1]), Number(mixed[2]));
      if (name.startsWith("usage-")) return usageScenario(name.slice("usage-".length));
      return name.startsWith("reply-") ? replyScenario(name.slice("reply-".length)) : undefined;
    }
  }
}
