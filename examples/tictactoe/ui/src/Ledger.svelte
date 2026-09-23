<!-- The session's raw frames, as they land: each state patch's ops, the TypeSafe call and its
     answer, and the agent's replies. -->
<script lang="ts">
  import {
    AgentSession,
    type AgentSseFrame,
    type SessionTransport
  } from "@temporal-agent-harness/svelte";

  import type { TicTacToeAgent } from "../../client_sdk/TicTacToeAgent";
  import { percent, type Judgment } from "./game";

  let { sessionId, transport }: { sessionId: string; transport: SessionTransport } = $props();

  const agent = new AgentSession<TicTacToeAgent>({
    get sessionId() {
      return sessionId;
    },
    // svelte-ignore state_referenced_locally
    transport
  });

  interface Entry {
    kind: "patch" | "tool" | "reply" | "error";
    label: string;
    body: string | { op: string; path: string; value?: unknown }[];
    timestamp: number;
  }

  let clearedAt = $state(0);
  $effect(() => {
    void sessionId;
    clearedAt = 0;
  });

  const entries = $derived(
    agent.frames
      .slice(clearedAt)
      .flatMap((frame) => {
        const entry = describe(frame);
        return entry ? [entry] : [];
      })
  );

  function describe(frame: AgentSseFrame): Entry | null {
    if (frame.event === "stream_error") {
      return { kind: "error", label: frame.data.kind, body: frame.data.message, timestamp: Date.now() / 1000 };
    }
    const d = frame.data;
    if (d.agent_id !== agent.rootAgentId) return null;
    const at = d.timestamp;
    switch (frame.event) {
      case "state_snapshot":
        return { kind: "patch", label: `snapshot v${frame.data.version}`, body: JSON.stringify(frame.data.value), timestamp: at };
      case "state_patch":
        return { kind: "patch", label: `patch v${frame.data.version}`, body: frame.data.ops, timestamp: at };
      case "tool_start": {
        const input = frame.data.tool_input as { request?: { questions?: Record<string, { criteria?: object }>; model?: string } };
        if (frame.data.tool_name === "typesafe_system_one" && input.request) {
          const questions = input.request.questions ?? {};
          const candidates = Object.keys(questions.move?.criteria ?? {}).length;
          const asked = Object.keys(questions).join(", ");
          return { kind: "tool", label: "typesafe →", body: `asking ${asked} over ${candidates} candidate cells (${input.request.model})`, timestamp: at };
        }
        return { kind: "tool", label: `${frame.data.tool_name} →`, body: JSON.stringify(frame.data.tool_input), timestamp: at };
      }
      case "tool_end": {
        if (frame.data.tool_name === "typesafe_system_one") {
          try {
            const move = (JSON.parse(frame.data.tool_output) as Judgment).choices.move;
            if (move) {
              const top = Object.entries(move.probabilities)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 3)
                .map(([k, v]) => `${k.replace("cell_", "")}:${percent(v)}`)
                .join(" ");
              return { kind: "tool", label: "typesafe ←", body: `${move.choice} (${percent(move.confidence)} conf) · ${top}`, timestamp: at };
            }
          } catch {
            // Not the shape this page reads: show it as it came.
          }
        }
        return { kind: "tool", label: `${frame.data.tool_name} ←`, body: frame.data.tool_output.split("\n")[0] ?? "", timestamp: at };
      }
      case "tool_error":
        return { kind: "error", label: `${frame.data.tool_name} ✕`, body: frame.data.message, timestamp: at };
      case "message_handler_end": {
        const text = frame.data.output.text;
        if (typeof text !== "string") return null;
        return { kind: "reply", label: "agent", body: text.replace(/```[\s\S]*?```\n?/g, "").trim(), timestamp: at };
      }
      case "message_handler_error":
        return { kind: "error", label: "handler", body: frame.data.message, timestamp: at };
      default:
        return null;
    }
  }

  function stamp(timestamp: number): string {
    return new Date(timestamp * 1000).toTimeString().slice(0, 8);
  }

  let log: HTMLDivElement | undefined = $state();
  $effect(() => {
    void entries.length;
    if (log) log.scrollTop = log.scrollHeight;
  });
</script>

<section class="ledger">
  <h2>
    <span>Ledger</span>
    <button type="button" onclick={() => (clearedAt = agent.frames.length)}>clear</button>
  </h2>
  <div class="log" bind:this={log}>
    {#each entries as entry, i (i)}
      <div class="entry" data-kind={entry.kind}>
        <span class="t">{stamp(entry.timestamp)}</span>
        <span class="kind">{entry.label}</span>
        <div class="body">
          {#if typeof entry.body === "string"}
            {entry.body}
          {:else}
            {#each entry.body as op, j (j)}
              <span class="op"><span class="path">{op.op} {op.path}</span>{#if "value" in op}<span class="val">{` ${JSON.stringify(op.value)}`}</span>{/if}</span>
            {/each}
          {/if}
        </div>
      </div>
    {:else}
      <div class="empty">{agent.connection === "connecting" ? "Replaying…" : "Nothing yet."}</div>
    {/each}
  </div>
</section>
