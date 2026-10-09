/* Dev-only fixtures for production-scale load: a session list and a transcript far longer than
   any demo. Selected with `?mock=<name>` in a dev build; see main.ts. */
import type { AgentSseFrame, Session } from "$lib/api/types";
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
function longTranscript(turns: number): AgentSseFrame[] {
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
    if (n === turns) {
      for (let i = 0; i < 400; i += 1) {
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

/* Built on call: frame() advances a shared offset counter, so module-level fixtures would be a
   side effect production builds could not drop. */
export function scaleScenarios(): Record<string, MockScenario> {
  const transcript = longTranscript(2500);
  return {
    sessions200: { agents, sessions: sessionList(200), frames: [] },
    sessions10k: { agents, sessions: sessionList(10_000), frames: [] },
    /* Everything but the 400-delta tail replays at once, the way a reattach backfills. */
    transcript5k: {
      agents,
      sessions: [{ ...sessionList(1)[0]!, workflow_id: "agent-session-scale-transcript", label: "5,000 messages" }],
      frames: transcript,
      instantFrames: transcript.length - 400
    }
  };
}
