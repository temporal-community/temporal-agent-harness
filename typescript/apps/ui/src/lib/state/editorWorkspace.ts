import type { AgentSseFrame, JsonRecord } from "$lib/api/types";

export interface EditorWorkspaceEdit {
  toolId: string;
  toolName: "write_code" | "edit_code";
  input: JsonRecord;
  result?: string;
  version: number;
  script: string;
}

export interface EditorWorkspace {
  script: string;
  version: number;
  edits: EditorWorkspaceEdit[];
}

type Terminal = { ok: boolean; result?: string };

function toolKey(frame: AgentSseFrame): string | null {
  if (!("type" in frame.data) || !("tool_id" in frame.data)) return null;
  return `${frame.data.agent_id}:${frame.data.tool_id}`;
}

function occurrenceCount(text: string, search: string): number {
  return search ? text.split(search).length - 1 : 0;
}

/** Rebuild browser-held editor state from successful callback calls in stream order. */
export function reconstructEditorWorkspace(
  frames: readonly AgentSseFrame[]
): EditorWorkspace {
  const terminals = new Map<string, Terminal>();
  for (const frame of frames) {
    const key = toolKey(frame);
    if (!key) continue;
    if (frame.event === "tool_end") {
      if (terminals.get(key)?.ok !== false) {
        terminals.set(key, { ok: true, result: frame.data.tool_output });
      }
    } else if (frame.event === "callback_resolved") {
      if (frame.data.outcome === "ok" && terminals.get(key)?.ok !== false) {
        terminals.set(key, { ok: true, result: terminals.get(key)?.result });
      } else if (frame.data.outcome !== "ok") {
        terminals.set(key, { ok: false });
      }
    } else if (frame.event === "tool_error") {
      terminals.set(key, { ok: false });
    }
  }

  let script = "";
  const edits: EditorWorkspaceEdit[] = [];
  const seen = new Set<string>();
  for (const frame of frames) {
    if (frame.event !== "callback_requested") continue;
    if (frame.data.tool_name !== "write_code" && frame.data.tool_name !== "edit_code") {
      continue;
    }
    const key = toolKey(frame)!;
    const terminal = terminals.get(key);
    if (seen.has(key) || !terminal?.ok) continue;
    seen.add(key);

    const input = frame.data.tool_input;
    if (frame.data.tool_name === "write_code") {
      if (typeof input.code !== "string") continue;
      script = input.code;
    } else {
      const { old_text: oldText, new_text: newText } = input;
      if (
        typeof oldText !== "string" ||
        typeof newText !== "string" ||
        occurrenceCount(script, oldText) !== 1
      ) {
        continue;
      }
      script = script.replace(oldText, () => newText);
    }

    edits.push({
      toolId: frame.data.tool_id,
      toolName: frame.data.tool_name,
      input,
      ...(terminal.result === undefined ? {} : { result: terminal.result }),
      version: edits.length + 1,
      script
    });
  }

  return { script, version: edits.length, edits };
}
