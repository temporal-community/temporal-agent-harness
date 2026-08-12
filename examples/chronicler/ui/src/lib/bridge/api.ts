import type { AgentApi } from "$lib/api/client";
import { HttpAgentApi } from "$lib/api/httpClient";
import { browserOperationAllowlist } from "./executor";
import type {
  BrowserBridgeApi,
  LocalOperationResultAck,
  LocalOperationResultRequest,
  LocalOperationsResponse
} from "./types";

function apiPath(path: string): string {
  return `api/${path.replace(/^\/+/, "")}`;
}

export class WorkflowNotFoundError extends Error {
  constructor(readonly workflowId: string, detail: string) {
    super(detail);
    this.name = "WorkflowNotFoundError";
  }
}

export class ChroniclerAudioApiError extends Error {
  constructor(
    readonly workflowId: string,
    readonly status: number,
    detail: string,
    readonly errorCode?: string
  ) {
    super(detail);
    this.name = "ChroniclerAudioApiError";
  }
}

export class AudioFolderBindingMismatchError extends ChroniclerAudioApiError {
  constructor(readonly workflowId: string, detail: string, errorCode: string) {
    super(workflowId, 409, detail, errorCode);
    this.name = "AudioFolderBindingMismatchError";
  }
}

export type AudioGenerationPhase =
  | "generating_audio"
  | "saving_wav"
  | "saving_synthetic_transcript"
  | "destination_approval_needed"
  | "waiting_for_folder"
  | "canceling"
  | "complete"
  | "failed"
  | "canceled";

export interface ChroniclerAudioStatus {
  generation_id: string;
  child_workflow_id: string;
  phase: AudioGenerationPhase;
  detail: string;
}

export interface ChroniclerAudioResult {
  generation_id: string;
  outcome: "completed" | "failed" | "canceled" | "needs_recovery";
  status: ChroniclerAudioStatus;
  duration_s: number | null;
  approved_package: ChroniclerAudioApprovedPackage | null;
}

export interface ChroniclerAudioApprovedPackage {
  package_revision: number;
  generation_id: string;
  source_kind: "existing" | "synthetic";
  source_identity: string;
  source_content: string;
  source_hash: string;
  recap_script: string;
  voice: "Charon";
  wav_path: string;
  synthetic_markdown_path: string | null;
  bridge_id: string;
  root_id: string;
  folder_binding_id: string;
  content_digest: string;
  destination_digest: string;
  package_digest: string;
}

export interface ChroniclerAudioDestinationRevision {
  generation_id: string;
  content_digest: string;
  destination_revision: number;
  wav_path: string;
  synthetic_markdown_path: string | null;
  destination_digest: string | null;
  package_digest: string | null;
}

export interface ChroniclerAudioArtifactReceipt {
  generation_id: string;
  artifact_role: "wav" | "synthetic_transcript";
  relative_path: string;
  content_hash: string;
  content_size: number;
  package_revision: number;
  operation_id: string;
  folder_binding_id: string;
}

export interface ChroniclerAudioSnapshot {
  child_workflow_id: string;
  state: "running" | "completed" | "canceled" | "failed";
  status: ChroniclerAudioStatus;
  result: ChroniclerAudioResult | null;
  approved_package?: ChroniclerAudioApprovedPackage | null;
  receipts: ChroniclerAudioArtifactReceipt[];
  pending_destination_revision: ChroniclerAudioDestinationRevision | null;
}

export interface ChroniclerAudioDestinationApproval {
  generation_id: string;
  content_digest: string;
  destination_revision: number;
  wav_path: string;
  synthetic_markdown_path: string | null;
  bridge_id: string;
  root_id: string;
  folder_binding_id: string;
}

export function chroniclerAudioChildWorkflowId(parentWorkflowId: string): string {
  return `chronicler-audio--${parentWorkflowId}`;
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  return (await errorDetail(response, fallback)).message;
}

async function errorDetail(
  response: Response,
  fallback: string
): Promise<{ errorCode?: string; message: string }> {
  const text = await response.text();
  if (!text) return { message: fallback };
  try {
    const body = JSON.parse(text) as { detail?: unknown; message?: unknown };
    if (typeof body.message === "string") return { message: body.message };
    if (typeof body.detail === "string") return { message: body.detail };
    if (typeof body.detail === "object" && body.detail !== null) {
      const detail = body.detail as { error?: unknown; message?: unknown };
      if (typeof detail.message === "string") {
        return {
          errorCode: typeof detail.error === "string" ? detail.error : undefined,
          message: detail.message
        };
      }
    }
    return { message: text };
  } catch {
    return { message: text };
  }
}

async function audioSnapshot(
  response: Response,
  childWorkflowId: string,
  fallback: string,
  bindingConflict = false
): Promise<ChroniclerAudioSnapshot> {
  if (!response.ok) {
    const failure = await errorDetail(response, fallback);
    if (response.status === 404) {
      throw new WorkflowNotFoundError(childWorkflowId, failure.message);
    }
    if (
      bindingConflict
      && response.status === 409
      && failure.errorCode === "audio_binding_mismatch"
    ) {
      throw new AudioFolderBindingMismatchError(
        childWorkflowId,
        failure.message,
        failure.errorCode
      );
    }
    throw new ChroniclerAudioApiError(
      childWorkflowId,
      response.status,
      failure.message,
      failure.errorCode
    );
  }
  const raw = await response.json() as Omit<ChroniclerAudioSnapshot, "pending_destination_revision" | "approved_package" | "result"> & {
    pending_destination_revision?: ChroniclerAudioDestinationRevision | null;
    approved_package?: ChroniclerAudioApprovedPackage | null;
    result: (Omit<ChroniclerAudioResult, "duration_s" | "approved_package"> & {
      duration_s?: number | null;
      approved_package?: ChroniclerAudioApprovedPackage | null;
    }) | null;
  };
  const snapshot: ChroniclerAudioSnapshot = {
    ...raw,
    pending_destination_revision: raw.pending_destination_revision ?? null,
    result: raw.result ? {
      ...raw.result,
      duration_s: raw.result.duration_s ?? null,
      approved_package: raw.result.approved_package ?? null
    } : null
  };
  if (snapshot.child_workflow_id !== childWorkflowId) {
    throw new Error("Audio status was returned for a different child workflow.");
  }
  return snapshot;
}

export class HttpBrowserBridgeApi implements BrowserBridgeApi {
  async listOperations(
    workflowId: string,
    bridgeId: string,
    rootId: string,
    signal?: AbortSignal
  ) {
    const query = new URLSearchParams({
      workflow_id: workflowId,
      bridge_id: bridgeId,
      root_id: rootId
    });
    for (const capability of [...browserOperationAllowlist].sort()) {
      query.append("capability", capability);
    }
    const response = await fetch(
      apiPath(`local-operations?${query}`),
      { signal, headers: { Accept: "application/json" } }
    );
    if (!response.ok) {
      const failure = await errorMessage(response, `Operation poll failed (${response.status})`);
      if (response.status === 404) throw new WorkflowNotFoundError(workflowId, failure);
      throw new Error(failure);
    }
    const body = await response.json() as LocalOperationsResponse;
    return body.operations;
  }

  async submitResult(
    workflowId: string,
    operationId: string,
    request: LocalOperationResultRequest,
    signal?: AbortSignal
  ): Promise<LocalOperationResultAck> {
    const response = await fetch(
      apiPath("local-operation-results"),
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          workflow_id: workflowId,
          operation_id: operationId,
          ...request
        }),
        signal
      }
    );
    // A 410 is authoritative: the workflow exists but this operation is no longer pending.
    // A 404 describes a missing workflow and remains retryable, so it must retain the outbox.
    if (response.status === 410) {
      return { operation_id: operationId, accepted: false, settled: true };
    }
    if (!response.ok) {
      throw new Error(await errorMessage(response, `Operation result failed (${response.status})`));
    }
    return response.json() as Promise<LocalOperationResultAck>;
  }
}

export class HttpChroniclerAudioApi {
  async getAudioStatus(
    childWorkflowId: string,
    signal?: AbortSignal
  ): Promise<ChroniclerAudioSnapshot> {
    const query = new URLSearchParams({ workflow_id: childWorkflowId });
    const response = await fetch(
      apiPath(`chronicler/audio/status?${query}`),
      { signal, headers: { Accept: "application/json" } }
    );
    return audioSnapshot(
      response,
      childWorkflowId,
      `Audio status failed (${response.status})`
    );
  }

  async approveAudioDestination(
    childWorkflowId: string,
    approval: ChroniclerAudioDestinationApproval,
    signal?: AbortSignal
  ): Promise<ChroniclerAudioSnapshot> {
    const response = await fetch(
      apiPath("chronicler/audio/destination"),
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ workflow_id: childWorkflowId, ...approval }),
        signal
      }
    );
    return audioSnapshot(
      response,
      childWorkflowId,
      `Audio destination approval failed (${response.status})`,
      true
    );
  }

  async requestAudioCancellation(
    childWorkflowId: string,
    signal?: AbortSignal
  ): Promise<ChroniclerAudioSnapshot> {
    const response = await fetch(
      apiPath("chronicler/audio/cancel"),
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ workflow_id: childWorkflowId }),
        signal
      }
    );
    return audioSnapshot(
      response,
      childWorkflowId,
      `Audio cancellation failed (${response.status})`
    );
  }

  async cancelAudioGeneration(
    childWorkflowId: string,
    signal?: AbortSignal
  ): Promise<void> {
    await this.requestAudioCancellation(childWorkflowId, signal);
  }

  async getAudioGenerationStatus(childWorkflowId: string, signal?: AbortSignal) {
    const snapshot = await this.getAudioStatus(childWorkflowId, signal);
    return {
      ...snapshot,
      childWorkflowId: snapshot.child_workflow_id,
      state: snapshot.state
    };
  }
}

export class WorkflowDiscovery {
  constructor(private readonly agentApi: AgentApi = new HttpAgentApi()) {}

  async activeWorkflowIds(signal?: AbortSignal): Promise<string[]> {
    if (signal?.aborted) return [];
    const sessions = await this.agentApi.listSessions();
    const candidates = sessions
      .filter((session) => !session.closed && session.agent_workflow_type === "ChroniclerAgent")
      .map((session) => chroniclerAudioChildWorkflowId(session.workflow_id));
    const states = await Promise.all(candidates.map(async (workflowId) => ({
      workflowId,
      state: await this.agentApi.workflowStatus(workflowId)
    })));
    return states
      .filter(({ state }) => !state.closed && state.execution_status === "RUNNING")
      .map(({ workflowId }) => workflowId);
  }
}
