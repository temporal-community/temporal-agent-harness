import type { AgentSseFrame, JsonRecord } from "$lib/api/types";
import { callbackAlreadyResolved } from "$lib/api/httpClient";
import { reconstructEditorWorkspace } from "./editorWorkspace";

export type ClientToolOutcome = { result: unknown } | { error: string };
export type ClientToolStatus =
  | { state: "answering" }
  | { state: "answered" }
  | { state: "failed"; error: string };

interface ClientToolRequest {
  workflowId: string;
  toolId: string;
  toolName: string;
  input: JsonRecord;
}

interface PreparedAnswer {
  outcome: ClientToolOutcome;
  script: string;
  changedLines?: { from: number; to: number };
}

interface PendingAnswer {
  request: ClientToolRequest;
  prepared: PreparedAnswer;
}

interface ClientToolCoordinatorOptions {
  submit: (request: ClientToolRequest, outcome: ClientToolOutcome) => Promise<void>;
  claim?: (key: string, answer: () => Promise<void>) => Promise<boolean>;
  onChange?: (
    statuses: Readonly<Record<string, ClientToolStatus>>,
    script: string,
    changedLines: { from: number; to: number } | null
  ) => void;
}

const editorToolNames = new Set(["read_code", "write_code", "edit_code", "export_code"]);

export function isProvidedClientTool(name: string | null | undefined): boolean {
  return !!name && editorToolNames.has(name);
}

function numbered(code: string): string {
  return code
    ? code
        .split("\n")
        .map((line, index) => `${String(index + 1).padStart(4)} | ${line}`)
        .join("\n")
    : "(empty)";
}

function occurrences(haystack: string, needle: string): number {
  return needle ? haystack.split(needle).length - 1 : 0;
}

export function answerEditorTool(
  toolName: string,
  input: JsonRecord,
  script: string
): PreparedAnswer | null {
  if (toolName === "read_code") return { outcome: { result: numbered(script) }, script };
  if (toolName === "export_code") return { outcome: { result: script }, script };
  if (toolName === "write_code") {
    const next = String(input.code ?? "");
    return {
      outcome: { result: `Wrote ${next.split("\n").length} lines.` },
      script: next,
      changedLines: { from: 1, to: next.split("\n").length }
    };
  }
  if (toolName !== "edit_code") return null;

  const oldText = String(input.old_text ?? "");
  const newText = String(input.new_text ?? "");
  const count = occurrences(script, oldText);
  if (count === 0) {
    return {
      outcome: {
        error:
          "old_text is not in the script. Call read_code and copy the text exactly, without the line-number prefixes."
      },
      script
    };
  }
  if (count > 1) {
    return {
      outcome: {
        error: `old_text appears ${count} times. Include more surrounding lines so it matches exactly once.`
      },
      script
    };
  }

  const from = script.slice(0, script.indexOf(oldText)).split("\n").length;
  const to = from + Math.max(0, newText.split("\n").length - 1);
  return {
    outcome: {
      result: newText
        ? `Replaced it; the new text is on lines ${from}-${to}.`
        : `Deleted it at line ${from}.`
    },
    script: script.replace(oldText, () => newText),
    changedLines: { from, to }
  };
}

async function browserClaim(key: string, answer: () => Promise<void>): Promise<boolean> {
  if (typeof window === "undefined") {
    await answer();
    return true;
  }
  const storageKey = `agent-harness-client-tool:${key}`;
  const run = async () => {
    if (window.localStorage.getItem(storageKey)) return false;
    await answer();
    window.localStorage.setItem(storageKey, "answered");
    return true;
  };
  return navigator.locks
    ? navigator.locks.request(storageKey, run)
    : run();
}

function terminalToolKeys(frames: readonly AgentSseFrame[]): Set<string> {
  return new Set(
    frames
      .filter((frame) => frame.event === "callback_resolved")
      .map((frame) => `${frame.data.agent_id}:${frame.data.tool_id}`)
  );
}

export class ClientToolCoordinator {
  #submit: ClientToolCoordinatorOptions["submit"];
  #claim: NonNullable<ClientToolCoordinatorOptions["claim"]>;
  #onChange: ClientToolCoordinatorOptions["onChange"];
  #sessionId: string | null = null;
  #script = "";
  #changedLines: { from: number; to: number } | null = null;
  #queue: Promise<void> = Promise.resolve();
  #pending = new Map<string, PendingAnswer>();
  #statuses: Record<string, ClientToolStatus> = {};

  constructor({ submit, claim = browserClaim, onChange }: ClientToolCoordinatorOptions) {
    this.#submit = submit;
    this.#claim = claim;
    this.#onChange = onChange;
  }

  reset(): void {
    this.#sessionId = null;
    this.#script = "";
    this.#changedLines = null;
    this.#queue = Promise.resolve();
    this.#pending.clear();
    this.#statuses = {};
    this.#notify();
  }

  sync(frames: readonly AgentSseFrame[], workflowId: string): void {
    /* Notified only on a change, so the statuses a commit hands the chat keep their identity. */
    let changed = false;
    if (this.#sessionId !== workflowId) {
      changed = true;
      this.reset();
      this.#sessionId = workflowId;
      const workspace = reconstructEditorWorkspace(frames);
      this.#script = workspace.script;
      const latest = workspace.edits.at(-1);
      if (latest) {
        this.#changedLines =
          answerEditorTool(
            latest.toolName,
            latest.input,
            workspace.edits.at(-2)?.script ?? ""
          )?.changedLines ?? null;
      }
    }
    const terminal = terminalToolKeys(frames);
    for (const frame of frames) {
      if (
        frame.event !== "callback_requested" ||
        !isProvidedClientTool(frame.data.tool_name)
      ) {
        continue;
      }
      const frameKey = `${frame.data.agent_id}:${frame.data.tool_id}`;
      const key = `${workflowId}:${frame.data.tool_id}`;
      if (terminal.has(frameKey) || this.#pending.has(key) || this.#statuses[frame.data.tool_id]) {
        continue;
      }
      const prepared = answerEditorTool(
        frame.data.tool_name,
        frame.data.tool_input,
        this.#script
      )!;
      this.#script = prepared.script;
      if (prepared.changedLines) this.#changedLines = prepared.changedLines;
      const pending = {
        request: {
          workflowId,
          toolId: frame.data.tool_id,
          toolName: frame.data.tool_name,
          input: frame.data.tool_input
        },
        prepared
      };
      this.#pending.set(key, pending);
      this.#statuses[frame.data.tool_id] = { state: "answering" };
      this.#queue = this.#queue.then(() => this.#answer(key, pending));
      changed = true;
    }
    if (changed) this.#notify();
  }

  retry(toolId: string): void {
    const key = this.#sessionId ? `${this.#sessionId}:${toolId}` : "";
    const pending = this.#pending.get(key);
    if (!pending || this.#statuses[toolId]?.state !== "failed") return;
    this.#statuses[toolId] = { state: "answering" };
    this.#notify();
    this.#queue = this.#queue.then(() => this.#answer(key, pending));
  }

  async #answer(key: string, pending: PendingAnswer): Promise<void> {
    try {
      await this.#claim(key, () => this.#submit(pending.request, pending.prepared.outcome));
      this.#statuses[pending.request.toolId] = { state: "answered" };
    } catch (error) {
      if (callbackAlreadyResolved(error)) {
        this.#statuses[pending.request.toolId] = { state: "answered" };
      } else {
        this.#statuses[pending.request.toolId] = {
          state: "failed",
          error: error instanceof Error ? error.message : "Could not send the response."
        };
      }
    }
    this.#notify();
  }

  #notify(): void {
    this.#onChange?.({ ...this.#statuses }, this.#script, this.#changedLines);
  }
}
