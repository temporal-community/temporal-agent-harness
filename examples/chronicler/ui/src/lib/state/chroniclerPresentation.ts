import {
  messageDisplayText,
  type AgentPresentationAdapter
} from "$lib/state/messagePresentation";

function parseSerializedMessage(value: string): unknown | null {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return null;
  }
}

type ChroniclerMessage = {
  type?: unknown;
  payload?: {
    change_request?: unknown;
    source?: {
      source_kind?: unknown;
      source_identity?: unknown;
      topic?: unknown;
    };
  };
};

function chroniclerMessage(value: unknown): ChroniclerMessage | null {
  const message = typeof value === "string" ? parseSerializedMessage(value) : value;
  if (!message || typeof message !== "object") return null;
  return message as ChroniclerMessage;
}

function chroniclerMessageText(value: unknown): string | undefined {
  const message = chroniclerMessage(value);
  if (!message) return undefined;

  const source = message.payload?.source;
  if (message.type === "prepare_audio" && source?.source_kind === "synthetic") {
    return typeof source.topic === "string"
      ? `Draft a spoken recap from topic: ${source.topic}`
      : undefined;
  }
  if (message.type === "prepare_audio" && source?.source_kind === "existing") {
    return typeof source.source_identity === "string"
      ? `Create a spoken recap from transcript: ${source.source_identity}`
      : undefined;
  }
  if (message.type === "prepare_audio" && typeof message.payload?.change_request === "string") {
    return `Revise the audio package: ${message.payload.change_request}`;
  }
  if (message.type === "start_audio") return "Approve and generate audio";
  if (message.type === "recover_audio") return "Recover audio generation";
  return undefined;
}

function draftReplyText(data: { output?: unknown }): string | undefined {
  const output = data.output;
  if (!output || typeof output !== "object" || !("draft" in output)) return undefined;
  return typeof output.draft === "object" && output.draft != null
    ? "Audio review package prepared."
    : undefined;
}

export const chroniclerPresentationAdapter: AgentPresentationAdapter = {
  messageText(value) {
    return chroniclerMessageText(value) ?? messageDisplayText(value);
  },
  replyText: draftReplyText
};
