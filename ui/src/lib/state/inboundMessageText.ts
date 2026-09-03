/**
 * How a message the operator sent is shown back to them.
 *
 * Two directions, which is why both live here. displayTextForMessage() takes
 * the structured message about to be submitted; renderUserMessage() takes the
 * `user_message` string a `turn_started` frame carries back, which may be the
 * JSON of that same structured message and must be unwrapped again. They agree
 * on the slash spelling by sharing slashCommandDisplayText() rather than by
 * both getting it right.
 *
 * renderUserMessage() is the only copy. transcript.ts and replayLog.ts each
 * carried their own, byte-identical to each other and narrower than this one:
 * they checked top-level `script` but not `payload.script`. A MontyDynamicAgent
 * session wraps a typed line as `{type:"run_script", payload:{script}}` (see
 * #messageForSession) and the workflow echoes that envelope back verbatim as
 * `turn_started.user_message` (agent_workflow.py `_render_message`), so the type
 * is "run_script" rather than a slash and those two fell through to returning
 * the raw value: the chat bubble and the replay log showed
 * `{"type":"run_script","payload":{"script":"book_flight(\"SFO\", \"LHR\")"}}`
 * where the session list showed `book_flight("SFO", "LHR")`. Escaped quotes and
 * all, so it did not even read as the script it is. Slash commands rendered
 * identically on all three, which is why it hid.
 *
 * They import this now rather than each growing the missing branch, because the
 * divergence was the bug: three copies, one of which had been fixed. The server
 * agrees with this one — web/app.py's `_display_user_message` checks
 * `payload.text` then `payload.script` then the slash spelling, same order.
 *
 * Its one remaining difference from the server is unreachable: the server tries
 * the slash branch before top-level `script` and this tries them the other way
 * round, which can only be told apart by a message carrying BOTH a top-level
 * `script` and a slash payload. `AgentMessage` is `{type, payload, expected_turn}`
 * and `_render_message` emits `include={type, payload}`, so no top-level `script`
 * can ever reach either function from the wire. check-user-message-rendering.mjs
 * pins that.
 */
import type { AgentInboundMessage, AgentMessageObject } from "$lib/api/types";

export function renderUserMessage(value: string): string {
  if (!value.startsWith("{")) return value;
  try {
    const message = JSON.parse(value) as {
      type?: string;
      payload?: { name?: string; arg?: string; text?: string; script?: string };
      script?: string;
    };
    if (typeof message.payload?.text === "string") return message.payload.text;
    if (typeof message.payload?.script === "string") return message.payload.script;
    if (typeof message.script === "string") return message.script;
    if (
      (message.type !== "slash" && message.type !== "slash_command") ||
      !message.payload?.name
    ) {
      return value;
    }
    return slashCommandDisplayText(message.payload.name, message.payload.arg);
  } catch {
    return value;
  }
}

export function isAgentMessageObject(
  message: AgentInboundMessage
): message is AgentMessageObject {
  return typeof message === "object" && message !== null;
}

function slashCommandDisplayText(name: string, arg?: string): string {
  const command = name === "set-model" ? "model" : name;
  return `/${command}${arg ? ` ${arg}` : ""}`;
}

export function displayTextForMessage(message: AgentInboundMessage): string {
  if (typeof message === "string") return message.trim();
  if (
    message.type === "slash" &&
    typeof message.payload === "object" &&
    message.payload != null &&
    "name" in message.payload &&
    typeof message.payload.name === "string"
  ) {
    const arg =
      "arg" in message.payload && typeof message.payload.arg === "string"
        ? message.payload.arg
        : undefined;
    return slashCommandDisplayText(message.payload.name, arg);
  }
  if (
    message.type === "run_script" &&
    typeof message.payload === "object" &&
    message.payload != null &&
    "script" in message.payload &&
    typeof message.payload.script === "string"
  ) {
    return message.payload.script.trim();
  }
  return JSON.stringify(message);
}
