<script lang="ts">
  /* The activity pane's script detail, through the real AgentChatPanel, twice: as it
     ships now, and with `position: relative` forced back onto the scrolling `pre` —
     which is what the bug was. The script is long enough to overflow the pane's
     320px max-height, so scrolling it down is the ordinary interaction. */
  import AgentChatPanel from "$lib/components/agent/AgentChatPanel.svelte";
  import { buildReplayLog } from "$lib/state/replayLog";
  import { buildTranscript } from "$lib/state/transcript";

  /* Long enough to overflow the pane's 320px max-height, so scrolling down is the
     ordinary interaction rather than a contrivance. */
  const SCRIPT = [
    'book_flight("SFO", "LHR", date="2026-03-14")',
    'hold_seat("14A")',
    "add_bag(count=1)",
    'quote_fare(currency="GBP")',
    'notify("ops@example.com")',
    'book_hotel("LHR", nights=3)',
    'reserve_car("LHR", days=3)',
    "confirm_itinerary()",
    ...Array.from({ length: 18 }, (_, i) => `check_leg(${i + 1})`),
    'log_audit("booking complete")',
    "close_session()"
  ].join("\n");
  const WIRE = JSON.stringify({ type: "run_script", payload: { script: SCRIPT } });

  const meta = (n: number) => ({
    agent_id: "monty",
    turn_id: "turn-1",
    turn_number: 1,
    timestamp: 1_700_000_000 + n,
    resume_offset: `${n}`,
    event_offset: n
  });

  const frames = [
    { event: "turn_started", data: { ...meta(1), type: "turn_started", user_message: WIRE } },
    /* The row the activity pane renders the script detail for: a tool call whose input
       carries the script. turn_started itself is filtered out of the pane. */
    {
      event: "tool_start",
      data: {
        ...meta(2),
        type: "tool_start",
        tool_id: "call_1",
        tool_name: "run_script",
        tool_input: { script: SCRIPT }
      }
    },
    {
      event: "reply",
      data: { ...meta(2), type: "reply", output: { text: "Booked SFO to LHR on the 14th." } }
    },
    { event: "turn_end", data: { ...meta(3), type: "turn_end" } }
  ] as never[];

  const items = buildTranscript(frames);
  const log = buildReplayLog(
    frames.map((frame) => ({ workflowId: "wf-monty", role: "parent", label: "Monty", frame }))
  );
  const panel = {
    items,
    logs: log.rows,
    agentLabel: "Monty",
    sessionId: "agent-session-monty",
    layout: "chat" as const,
    showHeader: false
  };
</script>

<div class="shots">
  <section data-shot="fixed" class="framed">
    <AgentChatPanel {...panel} />
  </section>
  <section data-shot="unfixed" class="framed forced-relative">
    <AgentChatPanel {...panel} />
  </section>
</div>

<style>
  .shots {
    display: grid;
    grid-template-columns: 520px 520px;
    align-items: start;
    gap: 24px;
    padding: 24px;
    background: var(--surface-0);
  }

  .framed {
    display: grid;
    height: 640px;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-1);
  }

  /* The bug: the scroller is the badge's containing block again. */
  .forced-relative :global(.activity-script-detail) {
    position: relative !important;
  }
</style>
