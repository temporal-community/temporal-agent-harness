import type {
  AgentDescriptor,
  AgentSseFrame,
  AgentSseEventMap,
  FileCitationAnnotation,
  Session,
  TokenUsage
} from "$lib/api/types";

export interface MockScenario {
  agents: AgentDescriptor[];
  sessions: Session[];
  frames: AgentSseFrame[];
}

const startedAt = 1_789_126_400;

const agents: AgentDescriptor[] = [
  {
    key: "qa",
    workflow_type: "QaAgent",
    task_queue: "qa-agent",
    label: "Q&A Agent",
    description:
      "Conversational Q&A over Temporal docs and community forum with grounded citations."
  },
  {
    key: "monty",
    workflow_type: "MontyDynamicAgent",
    task_queue: "monty-dynamic-agent",
    label: "Monty (Dynamic)",
    description:
      "Runs sandboxed Python scripts that orchestrate durable travel-booking activities."
  }
];

const sessions: Session[] = [
  {
    workflow_id: "agent-session-mock-qa",
    created_at: startedAt,
    label: "Session 1",
    agent_workflow_type: "QaAgent",
    is_message_queuing_enabled: true,
    initial_user_message:
      "When should I use a local activity versus a normal activity?"
  }
];

const rootAgentId = "qa-root";
const searchSubagentId = "qa-root-search";
const searchSubagentWorkflowId = "agent-session-mock-qa-search";
let resumeOffset = 0;

function frame<T extends keyof AgentSseEventMap>(
  event: T,
  data: Omit<AgentSseEventMap[T], "agent_id" | "resume_offset"> &
    { agent_id?: string; resume_offset?: number }
): AgentSseFrame {
  const agentId = data.agent_id ?? rootAgentId;
  if (agentId === rootAgentId) resumeOffset += 1;
  return {
    event,
    data: {
      ...data,
      agent_id: agentId,
      resume_offset: data.resume_offset ?? resumeOffset
    } as AgentSseEventMap[T]
  } as AgentSseFrame;
}

function meta(turn_number: number, deltaSeconds: number) {
  return {
    turn_id: `turn-${String(turn_number).padStart(3, "0")}`,
    turn_number,
    timestamp: startedAt + deltaSeconds
  };
}

function usage(
  input_tokens: number,
  output_tokens: number,
  thought_tokens: number,
  cached_tokens: number,
  tool_use_tokens = 0
): TokenUsage {
  return {
    input_tokens,
    output_tokens,
    thought_tokens,
    cached_tokens,
    tool_use_tokens
  };
}

function citation(
  file_name: string,
  document_uri: string,
  heading: string,
  title: string,
  path: string[],
  deep_url = document_uri
): FileCitationAnnotation {
  return {
    type: "file_citation",
    file_name,
    document_uri,
    start_index: 0,
    end_index: heading.length,
    custom_metadata: {
      deep_url,
      heading,
      title,
      section_path: path
    }
  };
}

const frames: AgentSseFrame[] = [
  frame("turn_started", {
    type: "turn_started",
    ...meta(1, 2),
    user_message:
      "I am replacing the static UI with Svelte. What API events should the agent UI model first?"
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(1, 3),
    model: "gemini-3.6-flash"
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(1, 4),
    text: "Start with the session lifecycle, turn boundaries, model spans, tool spans, approvals, and streamed reply deltas. "
  }),
  frame("text_annotation", {
    type: "text_annotation",
    ...meta(1, 5),
    delta: {
      annotations: [
        citation(
          "agent-streaming.md",
          "https://internal.example/docs/agent-streaming",
          "Agent stream event contract",
          "Agent Streaming",
          ["Server", "Agent API", "Streaming"]
        )
      ]
    }
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(1, 7),
    model: "gemini-3.6-flash",
    usage: usage(4020, 210, 310, 940)
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(1, 8),
    tool_id: "tool-api-outline-1",
    tool_name: "get_api_outline",
    tool_input: { route_group: "agent-session" }
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(1, 9),
    tool_id: "tool-api-outline-1",
    tool_name: "get_api_outline",
    tool_input: { route_group: "agent-session" }
  }),
  frame("tool_progress_delta", {
    type: "tool_progress_delta",
    ...meta(1, 10),
    tool_id: "tool-api-outline-1",
    tool_name: "get_api_outline",
    progress_delta: "Loaded registry, session, chat, approval, status, and stream route summaries."
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(1, 12),
    tool_id: "tool-api-outline-1",
    tool_name: "get_api_outline",
    tool_output:
      '{"routes":["GET /api/agents","GET /api/sessions","POST /api/sessions","POST /api/chat","GET /api/status/{session_id}","POST /api/tool-approval","GET /api/stream/{session_id}"]}'
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(1, 13),
    model: "gemini-3.6-flash"
  }),
  frame("thought_summary", {
    type: "thought_summary",
    ...meta(1, 14),
    delta: {
      content: {
        text:
          "Frame the answer around UI primitives and the event sequence rather than backend implementation details."
      }
    }
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(1, 15),
    text:
      "For the first pass, build components around turns, replay offsets, model activity, tool execution, approval gates, and terminal replies."
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(1, 17),
    model: "gemini-3.6-flash",
    usage: usage(2100, 360, 140, 620)
  }),
  frame("reply", {
    type: "reply",
    ...meta(1, 18),
    text:
      "Model the UI around the event stream: `turn_started`, `message_queued`, model spans, tool spans, approval gates, `reply_delta`, annotations, final `reply`, and `turn_end`. That gives you enough surface area to mock realistic sessions without needing the server running."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(1, 19)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(2, 28),
    user_message:
      '{"type":"slash","payload":{"name":"scope","arg":"docs"}}'
  }),
  frame("reply", {
    type: "reply",
    ...meta(2, 29),
    text: "Scope set to docs only."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(2, 30)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(3, 39),
    user_message:
      "Compare signals, updates, and queries for driving a long-running agent session."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(3, 40),
    model: "gemini-3.6-flash"
  }),
  frame("message_queued", {
    type: "message_queued",
    ...meta(4, 41),
    user_message: "Also tell me if any approvals are waiting."
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(3, 42),
    tool_id: "tool-doc-search-3",
    tool_name: "search_docs",
    tool_input: {
      query: "Temporal signals updates queries agent session",
      top_k: 5
    }
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(3, 43),
    tool_id: "tool-forum-search-3",
    tool_name: "search_forum",
    tool_input: {
      query: "Temporal update vs signal query agent UI",
      top_k: 4
    }
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(3, 44),
    model: "gemini-3.6-flash",
    usage: usage(3450, 180, 260, 1120, 40)
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(3, 45),
    tool_id: "tool-doc-search-3",
    tool_name: "search_docs",
    tool_input: {
      query: "Temporal signals updates queries agent session",
      top_k: 5
    }
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(3, 45),
    tool_id: "tool-forum-search-3",
    tool_name: "search_forum",
    tool_input: {
      query: "Temporal update vs signal query agent UI",
      top_k: 4
    }
  }),
  frame("tool_progress_delta", {
    type: "tool_progress_delta",
    ...meta(3, 46),
    tool_id: "tool-doc-search-3",
    tool_name: "search_docs",
    progress_delta: "Ranked workflow message-passing docs above SDK reference snippets."
  }),
  frame("tool_progress_delta", {
    type: "tool_progress_delta",
    ...meta(3, 46),
    tool_id: "tool-forum-search-3",
    tool_name: "search_forum",
    progress_delta: "Found community explanations about using updates for acknowledged commands."
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(3, 47),
    tool_id: "tool-doc-search-3",
    tool_name: "search_docs",
    tool_output:
      '{"hits":[{"title":"Signals","score":0.92},{"title":"Updates","score":0.89},{"title":"Queries","score":0.81}]}'
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(3, 48),
    tool_id: "tool-forum-search-3",
    tool_name: "search_forum",
    tool_output:
      '{"hits":[{"title":"Use Updates for command acknowledgment","score":0.86},{"title":"Signals for fire-and-forget messages","score":0.78}]}'
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(3, 50),
    model: "gemini-3.6-flash"
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(3, 51),
    text:
      "Use signals for fire-and-forget input, updates when the caller needs accepted/rejected semantics, and queries for read-only session status. "
  }),
  frame("text_annotation", {
    type: "text_annotation",
    ...meta(3, 52),
    delta: {
      annotations: [
        citation(
          "message-passing.mdx",
          "https://docs.temporal.io/workflows#message-passing",
          "Message passing",
          "Temporal Workflows",
          ["Workflows", "Message passing"]
        )
      ]
    }
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(3, 53),
    text:
      "For the browser, keep that hidden behind stable HTTP endpoints and an SSE replay stream."
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(3, 55),
    model: "gemini-3.6-flash",
    usage: usage(2280, 430, 120, 760)
  }),
  frame("reply", {
    type: "reply",
    ...meta(3, 56),
    text:
      "Use signals for fire-and-forget user input, updates when the caller needs accepted/rejected semantics, and queries for read-only status. The UI should not care which primitive the worker uses. It should see stable REST actions plus an SSE event stream that can resume by offset."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(3, 57)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(4, 59),
    user_message: "Also tell me if any approvals are waiting."
  }),
  frame("reply", {
    type: "reply",
    ...meta(4, 60),
    text: "No approvals are waiting. One queued message was promoted into this turn."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(4, 61)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(5, 75),
    user_message:
      "Draft a practical rollout runbook for the new Svelte UI, including docs and a community example."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(5, 76),
    model: "gemini-3.6-flash"
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(5, 77),
    tool_id: "tool-runbook-docs-5",
    tool_name: "read_docs_section",
    tool_input: {
      page_url: "https://docs.temporal.io/develop/worker-versioning",
      heading: "Worker Deployments"
    }
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(5, 78),
    tool_id: "tool-runbook-docs-5",
    tool_name: "read_docs_section",
    tool_input: {
      page_url: "https://docs.temporal.io/develop/worker-versioning",
      heading: "Worker Deployments"
    }
  }),
  frame("tool_progress_delta", {
    type: "tool_progress_delta",
    ...meta(5, 80),
    tool_id: "tool-runbook-docs-5",
    tool_name: "read_docs_section",
    progress_delta: "Found deployment-versioning guidance and rollback notes."
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(5, 82),
    tool_id: "tool-runbook-docs-5",
    tool_name: "read_docs_section",
    tool_output:
      "Worker deployments let operators roll traffic between versioned worker pools while old executions continue safely."
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(5, 83),
    tool_id: "tool-community-5",
    tool_name: "read_forum_thread",
    tool_input: {
      thread_url: "https://community.temporal.io/t/safe-worker-rollout"
    }
  }),
  frame("tool_approval_requested", {
    type: "tool_approval_requested",
    ...meta(5, 84),
    tool_id: "tool-community-5",
    tool_name: "read_forum_thread",
    tool_input: {
      thread_url: "https://community.temporal.io/t/safe-worker-rollout"
    }
  }),
  frame("tool_approval_resolved", {
    type: "tool_approval_resolved",
    ...meta(5, 88),
    tool_id: "tool-community-5",
    tool_name: "read_forum_thread",
    approved: true,
    reason: "Read-only lookup approved for this session.",
    remember: true
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(5, 89),
    tool_id: "tool-community-5",
    tool_name: "read_forum_thread",
    tool_input: {
      thread_url: "https://community.temporal.io/t/safe-worker-rollout"
    }
  }),
  frame("tool_progress_delta", {
    type: "tool_progress_delta",
    ...meta(5, 91),
    tool_id: "tool-community-5",
    tool_name: "read_forum_thread",
    progress_delta: "Extracted rollout checklist and post-deploy monitoring concerns."
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(5, 94),
    tool_id: "tool-community-5",
    tool_name: "read_forum_thread",
    tool_output:
      "Community example: deploy the new worker pool first, route a small percentage, watch failures, then ramp gradually."
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(5, 96),
    model: "gemini-3.6-flash",
    usage: usage(5120, 260, 420, 1480, 80)
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(5, 97),
    model: "gemini-3.6-flash"
  }),
  frame("thought_summary", {
    type: "thought_summary",
    ...meta(5, 98),
    delta: {
      content: {
        text:
          "Combine docs and community example into operator steps, then map each step to expected UI event states."
      }
    }
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(5, 100),
    text:
      "Runbook: ship the mock UI first, shadow it against saved event streams, then enable it for a small internal group. "
  }),
  frame("text_annotation", {
    type: "text_annotation",
    ...meta(5, 101),
    delta: {
      annotations: [
        citation(
          "worker-versioning.mdx",
          "https://docs.temporal.io/develop/worker-versioning",
          "Worker Deployments",
          "Worker Versioning",
          ["Develop", "Worker Versioning"],
          "https://docs.temporal.io/develop/worker-versioning#worker-deployments"
        )
      ]
    }
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(5, 102),
    text:
      "Watch replay gaps, pending approvals, failed tool states, and resume-from-offset behavior before widening access."
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(5, 104),
    model: "gemini-3.6-flash",
    usage: usage(3100, 610, 190, 920)
  }),
  frame("reply", {
    type: "reply",
    ...meta(5, 105),
    text:
      "Runbook: build against mock streams, shadow saved production-like sessions, verify resume-from-offset, then release to a small internal group. Watch replay gaps, stuck approvals, failed tool states, and worker rollback behavior. The community pattern is deploy capacity first, move traffic gradually, and keep old workers draining."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(5, 106)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(6, 118),
    user_message:
      '{"type":"slash","payload":{"name":"set-model","arg":"gemini-3.5-flash-lite"}}'
  }),
  frame("reply", {
    type: "reply",
    ...meta(6, 119),
    text: "Model set to **gemini-3.5-flash-lite** for faster iteration."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(6, 120)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(7, 132),
    user_message:
      "Estimate the token budget for a 15-turn mocked session with docs lookups and approval gates."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(7, 133),
    model: "gemini-3.5-flash-lite"
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(7, 134),
    tool_id: "tool-budget-7",
    tool_name: "estimate_usage_budget",
    tool_input: {
      turns: 15,
      model: "gemini-3.5-flash-lite",
      include_tools: true
    }
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(7, 135),
    tool_id: "tool-budget-7",
    tool_name: "estimate_usage_budget",
    tool_input: {
      turns: 15,
      model: "gemini-3.5-flash-lite",
      include_tools: true
    }
  }),
  frame("tool_progress_delta", {
    type: "tool_progress_delta",
    ...meta(7, 136),
    tool_id: "tool-budget-7",
    tool_name: "estimate_usage_budget",
    progress_delta: "Estimated four heavier turns and eleven lightweight turns."
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(7, 137),
    tool_id: "tool-budget-7",
    tool_name: "estimate_usage_budget",
    tool_output:
      '{"expected_total_tokens":62000,"expected_cached_tokens":18000,"expected_model_calls":22}'
  }),
  frame("subagent_started", {
    type: "subagent_started",
    ...meta(7, 138.1),
    subagent_id: searchSubagentId,
    agent_key: "research",
    workflow_id: searchSubagentWorkflowId
  }),
  frame("subagent_message_sent", {
    type: "subagent_message_sent",
    ...meta(7, 138.2),
    subagent_id: searchSubagentId,
    agent_key: "research",
    workflow_id: searchSubagentWorkflowId,
    function: "summarize_budget_examples",
    subagent_turn: 1,
    from_offset: 0
  }),
  frame("turn_started", {
    type: "turn_started",
    ...meta(1, 138.3),
    agent_id: searchSubagentId,
    turn_id: "search-turn-001",
    user_message: '{"type":"summarize_budget_examples","payload":{"turns":15}}'
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(1, 138.4),
    agent_id: searchSubagentId,
    turn_id: "search-turn-001",
    tool_id: "tool-search-budget-1",
    tool_name: "scan_saved_sessions",
    tool_input: { kind: "budget-examples" }
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(1, 138.55),
    agent_id: searchSubagentId,
    turn_id: "search-turn-001",
    tool_id: "tool-search-budget-1",
    tool_name: "scan_saved_sessions",
    tool_output: '{"matching_sessions":4,"largest_token_total":68120}'
  }),
  frame("reply", {
    type: "reply",
    ...meta(1, 138.7),
    agent_id: searchSubagentId,
    turn_id: "search-turn-001",
    text: "Saved budget-heavy sessions cluster between 58k and 68k tokens."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(1, 138.8),
    agent_id: searchSubagentId,
    turn_id: "search-turn-001"
  }),
  frame("subagent_reply_received", {
    type: "subagent_reply_received",
    ...meta(7, 138.9),
    subagent_id: searchSubagentId,
    agent_key: "research",
    workflow_id: searchSubagentWorkflowId,
    function: "summarize_budget_examples",
    subagent_turn: 1,
    outcome: "ok"
  }),
  frame("subagent_stopped", {
    type: "subagent_stopped",
    ...meta(7, 138.95),
    subagent_id: searchSubagentId,
    agent_key: "research",
    workflow_id: searchSubagentWorkflowId
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(7, 139),
    model: "gemini-3.5-flash-lite",
    usage: usage(2400, 120, 95, 540, 50)
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(7, 140),
    model: "gemini-3.5-flash-lite"
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(7, 141),
    text:
      "Plan for roughly 60k to 70k total tokens in the mock, with visible spikes on research and synthesis turns."
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(7, 143),
    model: "gemini-3.5-flash-lite",
    usage: usage(1650, 250, 70, 410)
  }),
  frame("reply", {
    type: "reply",
    ...meta(7, 144),
    text:
      "Use a 60k to 70k token budget for a comprehensive mock session. The useful visual pattern is not a smooth accumulation; it is spikes when model calls finish, especially after document and forum tools."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(7, 145)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(8, 160),
    user_message:
      "Can you inspect the real repo routes before we map the component states?"
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(8, 161),
    model: "gemini-3.5-flash-lite"
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(8, 162),
    tool_id: "tool-repo-8",
    tool_name: "inspect_repo",
    tool_input: {
      path: "/server",
      purpose: "confirm UI route shapes"
    }
  }),
  frame("tool_approval_requested", {
    type: "tool_approval_requested",
    ...meta(8, 163),
    tool_id: "tool-repo-8",
    tool_name: "inspect_repo",
    tool_input: {
      path: "/server",
      purpose: "confirm UI route shapes"
    }
  }),
  frame("tool_approval_resolved", {
    type: "tool_approval_resolved",
    ...meta(8, 168),
    tool_id: "tool-repo-8",
    tool_name: "inspect_repo",
    approved: false,
    reason: "Keep this mock independent of local filesystem reads.",
    remember: false
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(8, 169),
    model: "gemini-3.5-flash-lite",
    usage: usage(1980, 90, 60, 520)
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(8, 170),
    model: "gemini-3.5-flash-lite"
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(8, 171),
    text:
      "I will keep the mock independent and use the API contract we already documented instead of reading the repo."
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(8, 172),
    model: "gemini-3.5-flash-lite",
    usage: usage(1240, 150, 40, 330)
  }),
  frame("reply", {
    type: "reply",
    ...meta(8, 173),
    text:
      "I will keep this mock app independent of local filesystem reads. The component states should come from the documented stream contract and realistic event sequences."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(8, 174)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(9, 188),
    user_message:
      "Use mock file names instead and show what happens when a tool fails then recovers."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(9, 189),
    model: "gemini-3.5-flash-lite"
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(9, 190),
    tool_id: "tool-mock-file-9a",
    tool_name: "read_mock_file",
    tool_input: { path: "docs/agent-ui-state-machine.md" }
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(9, 191),
    tool_id: "tool-mock-file-9a",
    tool_name: "read_mock_file",
    tool_input: { path: "docs/agent-ui-state-machine.md" }
  }),
  frame("tool_error", {
    type: "tool_error",
    ...meta(9, 193),
    tool_id: "tool-mock-file-9a",
    tool_name: "read_mock_file",
    message: "Mock file not found: docs/agent-ui-state-machine.md"
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(9, 194),
    model: "gemini-3.5-flash-lite",
    usage: usage(1820, 70, 70, 460, 30)
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(9, 195),
    model: "gemini-3.5-flash-lite"
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(9, 196),
    tool_id: "tool-mock-file-9b",
    tool_name: "list_mock_files",
    tool_input: { directory: "docs" }
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(9, 197),
    tool_id: "tool-mock-file-9b",
    tool_name: "list_mock_files",
    tool_input: { directory: "docs" }
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(9, 198),
    tool_id: "tool-mock-file-9b",
    tool_name: "list_mock_files",
    tool_output:
      '["docs/api.md","docs/components.md","docs/replay-controller.md"]'
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(9, 200),
    model: "gemini-3.5-flash-lite",
    usage: usage(1640, 110, 50, 390, 20)
  }),
  frame("reply", {
    type: "reply",
    ...meta(9, 201),
    text:
      "The failed file read should appear as a failed tool state, then the recovery tool should show a successful follow-up. That gives the component library examples for both error and recovery without relying on real files."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(9, 202)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(10, 216),
    user_message:
      "Map the agent state diagram nodes we should show in Svelte Flow."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(10, 217),
    model: "gemini-3.5-flash-lite"
  }),
  frame("thought_summary", {
    type: "thought_summary",
    ...meta(10, 218),
    delta: {
      content: {
        text:
          "User wants the visual state vocabulary, not backend internals. Include queue, approval, tool error, and reply states."
      }
    }
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(10, 219),
    text:
      "Use nodes for ingress, queue, agent, model, tool, approval, and egress. "
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(10, 220),
    text:
      "Edges should animate only for the currently active path so long sessions remain scannable."
  }),
  frame("text_annotation", {
    type: "text_annotation",
    ...meta(10, 221),
    delta: {
      annotations: [
        citation(
          "components.md",
          "mock://docs/components",
          "Agent state diagram",
          "Agent UI Components",
          ["Mock docs", "Components"]
        )
      ]
    }
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(10, 223),
    model: "gemini-3.5-flash-lite",
    usage: usage(2100, 340, 95, 580)
  }),
  frame("reply", {
    type: "reply",
    ...meta(10, 224),
    text:
      "Use seven stable nodes: ingress, queue, agent, model, tool, approval, and egress. Animate only the active edge, keep failed and denied states visually distinct, and let the transcript carry detailed tool output."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(10, 225)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(11, 240),
    user_message:
      "Show a transient model failure and recovery in the same session."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(11, 241),
    model: "gemini-3.5-flash-lite"
  }),
  frame("error", {
    type: "error",
    ...meta(11, 242),
    message: "Upstream model stream interrupted after headers were sent."
  } as Omit<
    Extract<AgentSseEventMap["error"], { type: "error" }>,
    "agent_id" | "resume_offset"
  >),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(11, 246),
    model: "gemini-3.5-flash-lite"
  }),
  frame("thought_summary", {
    type: "thought_summary",
    ...meta(11, 247),
    delta: {
      content: {
        text:
          "Recover by starting a new model span and answering with an explicit note about retry semantics."
      }
    }
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(11, 249),
    model: "gemini-3.5-flash-lite",
    usage: usage(1520, 180, 80, 360)
  }),
  frame("reply", {
    type: "reply",
    ...meta(11, 250),
    text:
      "A transient model failure should show as an error event, followed by a new model span if the worker retries. The replay should preserve both so the UI can explain why a response took longer."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(11, 251)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(12, 268),
    user_message:
      "Export the current design notes to a markdown artifact."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(12, 269),
    model: "gemini-3.5-flash-lite"
  }),
  frame("tool_requested", {
    type: "tool_requested",
    ...meta(12, 270),
    tool_id: "tool-export-12",
    tool_name: "write_session_note",
    tool_input: {
      format: "markdown",
      title: "Agent UI mock session notes"
    }
  }),
  frame("tool_approval_requested", {
    type: "tool_approval_requested",
    ...meta(12, 271),
    tool_id: "tool-export-12",
    tool_name: "write_session_note",
    tool_input: {
      format: "markdown",
      title: "Agent UI mock session notes"
    }
  }),
  frame("tool_approval_resolved", {
    type: "tool_approval_resolved",
    ...meta(12, 276),
    tool_id: "tool-export-12",
    tool_name: "write_session_note",
    approved: true,
    reason: "Mock artifact write allowed.",
    remember: false
  }),
  frame("tool_start", {
    type: "tool_start",
    ...meta(12, 277),
    tool_id: "tool-export-12",
    tool_name: "write_session_note",
    tool_input: {
      format: "markdown",
      title: "Agent UI mock session notes"
    }
  }),
  frame("tool_progress_delta", {
    type: "tool_progress_delta",
    ...meta(12, 278),
    tool_id: "tool-export-12",
    tool_name: "write_session_note",
    progress_delta: "Rendered sections for API contract, replay controls, and state diagram examples."
  }),
  frame("tool_end", {
    type: "tool_end",
    ...meta(12, 280),
    tool_id: "tool-export-12",
    tool_name: "write_session_note",
    tool_output:
      '{"artifact_id":"artifact-agent-ui-notes","mime_type":"text/markdown","bytes":4821}'
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(12, 282),
    model: "gemini-3.5-flash-lite",
    usage: usage(1980, 130, 70, 420, 35)
  }),
  frame("reply", {
    type: "reply",
    ...meta(12, 283),
    text:
      "I wrote a mock markdown artifact with sections for the API contract, replay controls, state diagram examples, and known UI states."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(12, 284)
  }),

  frame("turn_started", {
    type: "turn_started",
    ...meta(13, 300),
    user_message:
      "Give me the next frontend tasks and keep it focused on component library quality."
  }),
  frame("model_interaction_started", {
    type: "model_interaction_started",
    ...meta(13, 301),
    model: "gemini-3.5-flash-lite"
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(13, 302),
    text:
      "Next: add empty/loading/error variants, define prop-level story fixtures, and split transcript, graph, and replay controls into reusable packages. "
  }),
  frame("reply_delta", {
    type: "reply_delta",
    ...meta(13, 303),
    text:
      "Then add stress fixtures for many turns, queued turns, denied approvals, failed tools, and model retry spans."
  }),
  frame("model_interaction_ended", {
    type: "model_interaction_ended",
    ...meta(13, 305),
    model: "gemini-3.5-flash-lite",
    usage: usage(1760, 280, 65, 500)
  }),
  frame("reply", {
    type: "reply",
    ...meta(13, 306),
    text:
      "Next tasks: add component states for empty/loading/error/streaming, create fixture-driven examples for transcript, graph, and replay controls, and keep stress cases for queued turns, denied approvals, failed tools, model retries, and long sessions."
  }),
  frame("turn_end", {
    type: "turn_end",
    ...meta(13, 307)
  })
];

export const realisticQaScenario: MockScenario = {
  agents,
  sessions,
  frames
};
