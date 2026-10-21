// The scripted demo's "model" and activity-tool bodies. The model streams its reply onto the turn
// stream from the activity, as a real model activity does.

import { setTimeout as sleep } from "node:timers/promises";
import { publisherFromActivity, toolActivities, toolActivity } from "../../../src/index.ts";
import {
  bookFlight,
  searchFlights,
  type ModelStep,
  type ScriptedModelInput,
  type ToolCall,
} from "./scripted-demo.ts";

const CODE = `import asyncio
async def main():
    a, b = await asyncio.gather(search_flights("SFO", "JFK"), search_flights("SFO", "BOS"))
    cheapest = min(a + b, key=lambda f: f["price_usd"])
    print("cheapest:", cheapest["flight_id"])
    return await book_flight(cheapest["flight_id"])
asyncio.run(main())`;

let counter = 0;
const call = (name: string, args: Record<string, unknown>): ToolCall => ({
  id: `call_${counter++}`,
  name,
  arguments: args,
});

/** Pick the next step like a model would: tools by keyword, then a reply that uses their results. */
function decide(prompt: string, round: number, results: ScriptedModelInput["results"]): ModelStep {
  const p = prompt.toLowerCase();
  if (round === 0) {
    const calls: ToolCall[] = [];
    if (p.includes("weather"))
      calls.push(
        call("get_weather", { city: /in ([a-z ]+)/i.exec(prompt)?.[1]?.trim() ?? "Paris" }),
      );
    if (p.includes("search"))
      calls.push(call("search_flights", { origin: "SFO", destination: "JFK" }));
    if (p.includes("book")) calls.push(call("book_flight", { flight_id: "FL-SFOJFK-001" }));
    if (p.includes("file")) calls.push(call("read_local_file", { path: "notes.txt" }));
    if (p.includes("code")) calls.push(call("run_travel_code", { script: CODE }));
    if (p.includes("echo")) calls.push(call("start_echo", {}));
    if (calls.length) return { text: "", toolCalls: calls };
  }
  const started = results.find((r) => r.call.name === "start_echo");
  if (started)
    return {
      text: "",
      toolCalls: [call("echo_echo", { subagent: started.output, message: { text: prompt } })],
    };
  const echoed = results.find((r) => r.call.name === "echo_echo");
  if (echoed) {
    const handle = echoed.call.arguments.subagent as string;
    return { text: "", toolCalls: [call("stop_echo", { subagent: handle })] };
  }
  const summary = results.map((r) => `${r.call.name} → ${r.output}`).join("\n");
  const text = summary ? `Here is what I found:\n${summary}` : `You said: ${prompt}.`;
  return { text, toolCalls: [] };
}

export const scriptedDemoActivities = {
  async scripted_model({
    prompt,
    round,
    results,
    streamContext,
  }: ScriptedModelInput): Promise<ModelStep> {
    const step = decide(prompt, round, results);
    await using pub = publisherFromActivity(streamContext);
    pub.publish({ type: "model_interaction_started", model: "scripted-1" });
    for (const word of step.text.split(/(\s+)/)) {
      if (!word) continue;
      pub.publish({ type: "reply_delta", text: word });
      await sleep(15);
    }
    for (const c of step.toolCalls)
      pub.publish({
        type: "tool_requested",
        tool_id: c.id,
        tool_name: c.name,
        tool_input: c.arguments,
      });
    const words = step.text.split(/\s+/).filter(Boolean).length;
    pub.publish({
      type: "model_interaction_ended",
      model: "scripted-1",
      usage: {
        input_tokens: prompt.length,
        output_tokens: words,
        thought_tokens: null,
        cached_tokens: null,
        tool_use_tokens: null,
        total_tokens: prompt.length + words,
      },
    });
    return step;
  },
  ...toolActivities(
    toolActivity(searchFlights, async ({ origin, destination }) => [
      { flight_id: `FL-${origin}${destination}-001`, price_usd: destination === "BOS" ? 199 : 249 },
      { flight_id: `FL-${origin}${destination}-002`, price_usd: destination === "BOS" ? 329 : 289 },
    ]),
    toolActivity(bookFlight, async ({ flight_id }) => `AIR-${flight_id.slice(-6).toUpperCase()}`),
  ),
};
