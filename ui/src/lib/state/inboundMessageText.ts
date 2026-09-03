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
 * NOTE: transcript.ts and replayLog.ts each carry their own older copy of
 * renderUserMessage() — identical to each other, narrower than this one (no
 * `payload.script`, and their own inline slash formatting). Not consolidated
 * here because doing so would change what those two render, which is a
 * behaviour change rather than a move.
 *
 * That divergence is a live rendering bug, not a harmless difference, and it is
 * this copy that is right. A MontyDynamicAgent session wraps a typed line as
 * `{type:"run_script", payload:{script}}` (see #messageForSession), and the
 * workflow echoes that envelope back verbatim as `turn_started.user_message`
 * (agent_workflow.py `_render_message`). Because the narrow copies check
 * top-level `script` but not `payload.script`, and the type is "run_script"
 * rather than a slash, they fall through to returning the raw value: the chat
 * bubble and the replay log both show
 * `{"type":"run_script","payload":{"script":"book_flight(\"SFO\", \"LHR\")"}}`
 * where the session list shows `book_flight("SFO", "LHR")`. Escaped quotes and
 * all, so it does not even read as the script it is. Slash commands render
 * identically on all three, which is why this hid.
 *
 * The server agrees with this copy, not with them: web/app.py's
 * `_display_user_message` checks `payload.script` in the same order. So the fix
 * is to widen those two, not to narrow this one — left as a decision to make
 * deliberately rather than as a side effect of a move.
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
