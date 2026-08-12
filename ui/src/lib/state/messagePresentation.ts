import type { AgentInboundMessage } from "$lib/api/types";

export interface AgentPresentationAdapter {
  messageText(value: AgentInboundMessage | string): string;
  replyText?(data: { text?: unknown; output?: unknown }): string | undefined;
}

interface MessageShape {
  type?: string;
  payload?: {
    name?: string;
    arg?: string;
    text?: string;
    script?: string;
  };
  script?: string;
}

function structuredMessageDisplayText(message: MessageShape): string | null {
  if (typeof message.payload?.text === "string") return message.payload.text;
  if (typeof message.payload?.script === "string") return message.payload.script;
  if (typeof message.script === "string") return message.script;
  if (
    (message.type === "slash" || message.type === "slash_command")
    && message.payload?.name
  ) {
    const command = message.payload.name === "set-model" ? "model" : message.payload.name;
    return `/${command}${message.payload.arg ? ` ${message.payload.arg}` : ""}`;
  }
  return null;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) =>
      `${JSON.stringify(key)}:${stableJson(record[key])}`
    ).join(",")}}`;
  }
  return JSON.stringify(value) ?? String(value);
}

export function messageIdentity(value: AgentInboundMessage | string): string {
  if (typeof value !== "string") return stableJson(value);
  const normalized = value.trim();
  if (!normalized.startsWith("{")) return stableJson(normalized);
  try {
    return stableJson(JSON.parse(normalized));
  } catch {
    return stableJson(normalized);
  }
}

export function messageDisplayText(value: AgentInboundMessage | string): string {
  if (typeof value !== "string") {
    return structuredMessageDisplayText(value) ?? JSON.stringify(value);
  }
  const fallback = value.trim();
  if (!fallback.startsWith("{")) return fallback;
  try {
    const message = JSON.parse(fallback) as MessageShape;
    return structuredMessageDisplayText(message) ?? fallback;
  } catch {
    return fallback;
  }
}

export const defaultAgentPresentationAdapter: AgentPresentationAdapter = {
  messageText: messageDisplayText
};
