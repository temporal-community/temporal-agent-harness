import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentApi } from "$lib/api/client";
import type { AudioGenerationControlTransport } from "./leaderRuntime";
import {
  ChroniclerAudioApiError,
  AudioFolderBindingMismatchError,
  HttpBrowserBridgeApi,
  HttpChroniclerAudioApi,
  WorkflowDiscovery
} from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("HttpBrowserBridgeApi", () => {
  it("uses canonical opaque-ID transport and advertises only safe capabilities", async () => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({
      workflow_id: "workflow/1",
      bridge_id: "browser-local",
      root_id: "campaign-root",
      operations: []
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);

    await new HttpBrowserBridgeApi().listOperations(
      "workflow/1",
      "browser-local",
      "campaign-root"
    );

    const url = new URL(String(fetch.mock.calls[0]?.[0]), "https://example.test/");
    expect(url.pathname).toBe("/api/local-operations");
    expect(url.searchParams.get("workflow_id")).toBe("workflow/1");
    expect(url.searchParams.getAll("capability")).toEqual([
      "create_audio_artifact",
      "inspect_audio_artifact"
    ]);
    expect(url.searchParams.getAll("capability")).not.toContain("save_recording");
    expect(url.searchParams.getAll("capability")).not.toContain("delete_file");
    expect(url.searchParams.getAll("capability")).not.toContain("grep");
  });

  it("posts opaque workflow and operation IDs in the canonical result body", async () => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({
      operation_id: "job/1",
      accepted: true
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);

    await new HttpBrowserBridgeApi().submitResult(
      "workflow/1",
      "job/1",
      {
        bridge_id: "browser-local",
        root_id: "campaign-root",
        outcome: "success",
        result: "saved"
      }
    );

    expect(fetch.mock.calls[0]?.[0]).toBe("api/local-operation-results");
    const init = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      workflow_id: "workflow/1",
      operation_id: "job/1",
      bridge_id: "browser-local",
      root_id: "campaign-root",
      outcome: "success",
      result: "saved"
    });
  });

  it("keeps a missing workflow retryable instead of treating its operations as settled", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: "Workflow 'workflow/1' was not found."
    }), { status: 404, headers: { "Content-Type": "application/json" } })));

    const api = new HttpBrowserBridgeApi();

    await expect(api.listOperations(
      "workflow/1",
      "browser-local",
      "campaign-root"
    )).rejects.toThrow("was not found");
    await expect(api.submitResult(
      "workflow/1",
      "job/1",
      {
        bridge_id: "browser-local",
        root_id: "campaign-root",
        outcome: "success",
        result: "saved"
      }
    )).rejects.toThrow("was not found");
  });

  it("treats 410 as authoritative proof that an operation has settled", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: "Local operation 'job/1' is not pending."
    }), { status: 410, headers: { "Content-Type": "application/json" } })));

    await expect(new HttpBrowserBridgeApi().submitResult(
      "workflow/1",
      "job/1",
      {
        bridge_id: "browser-local",
        root_id: "campaign-root",
        outcome: "success",
        result: "saved"
      }
    )).resolves.toEqual({
      operation_id: "job/1",
      accepted: false,
      settled: true
    });
  });
});

describe("HttpChroniclerAudioApi", () => {
  it("gets typed status for the exact opaque child workflow ID", async () => {
    const fetch = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit
    ) => new Response(JSON.stringify({
      child_workflow_id: "chronicler-audio--agent/session 1",
      state: "running",
      status: {
        generation_id: "generation/1",
        child_workflow_id: "chronicler-audio--agent/session 1",
        phase: "saving_wav",
        detail: "Writing the approved WAV."
      },
      result: null,
      receipts: []
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);

    await expect(new HttpChroniclerAudioApi().getAudioStatus(
      "chronicler-audio--agent/session 1"
    )).resolves.toEqual({
      child_workflow_id: "chronicler-audio--agent/session 1",
      state: "running",
      status: {
        generation_id: "generation/1",
        child_workflow_id: "chronicler-audio--agent/session 1",
        phase: "saving_wav",
        detail: "Writing the approved WAV."
      },
      result: null,
      receipts: [],
      pending_destination_revision: null
    });

    const url = new URL(String(fetch.mock.calls[0]?.[0]), "https://example.test/");
    expect(url.pathname).toBe("/api/chronicler/audio/status");
    expect(url.searchParams.get("workflow_id")).toBe(
      "chronicler-audio--agent/session 1"
    );
  });

  it("reports a missing audio child with its opaque workflow identity", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: "Audio child was not found."
    }), { status: 404, headers: { "Content-Type": "application/json" } })));

    const failure = await new HttpChroniclerAudioApi().getAudioStatus(
      "chronicler-audio--agent/session 1"
    ).catch((error) => error);

    expect(failure).toMatchObject({
      name: "WorkflowNotFoundError",
      workflowId: "chronicler-audio--agent/session 1",
      message: "Audio child was not found."
    });
  });

  it("rejects status returned for a different child workflow", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      child_workflow_id: "chronicler-audio--other",
      state: "running",
      status: {
        generation_id: "generation/1",
        child_workflow_id: "chronicler-audio--other",
        phase: "generating_audio",
        detail: ""
      },
      result: null,
      receipts: []
    }), { status: 200, headers: { "Content-Type": "application/json" } })));

    await expect(new HttpChroniclerAudioApi().getAudioStatus(
      "chronicler-audio--expected"
    )).rejects.toThrow("different child workflow");
  });

  it("posts a typed destination approval with the opaque workflow ID in the body", async () => {
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent/session 1",
      state: "running",
      status: {
        generation_id: "generation/1",
        child_workflow_id: "chronicler-audio--agent/session 1",
        phase: "saving_wav",
        detail: "Destination approved."
      },
      result: null,
      receipts: []
    } as const;
    const fetch = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit
    ) => new Response(JSON.stringify(snapshot), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetch);

    await expect(new HttpChroniclerAudioApi().approveAudioDestination(
      "chronicler-audio--agent/session 1",
      {
        generation_id: "generation/1",
        content_digest: "content+digest/1",
        destination_revision: 2,
        wav_path: "recaps/session 1.wav",
        synthetic_markdown_path: null,
        bridge_id: "bridge/1",
        root_id: "root 1",
        folder_binding_id: "binding+1"
      }
    )).resolves.toEqual({ ...snapshot, pending_destination_revision: null });

    expect(fetch.mock.calls[0]?.[0]).toBe("api/chronicler/audio/destination");
    const init = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      workflow_id: "chronicler-audio--agent/session 1",
      generation_id: "generation/1",
      content_digest: "content+digest/1",
      destination_revision: 2,
      wav_path: "recaps/session 1.wav",
      synthetic_markdown_path: null,
      bridge_id: "bridge/1",
      root_id: "root 1",
      folder_binding_id: "binding+1"
    });
  });

  it("reports a destination folder binding mismatch as a typed conflict", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: {
        error: "audio_binding_mismatch",
        message: "AudioBindingMismatch: destination approval binding does not match the audio package"
      }
    }), { status: 409, headers: { "Content-Type": "application/json" } })));

    const failure = await new HttpChroniclerAudioApi().approveAudioDestination(
      "chronicler-audio--agent/session 1",
      {
        generation_id: "generation/1",
        content_digest: "content-digest",
        destination_revision: 2,
        wav_path: "recaps/session-1.wav",
        synthetic_markdown_path: null,
        bridge_id: "bridge-1",
        root_id: "root-1",
        folder_binding_id: "wrong-binding"
      }
    ).catch((error) => error);

    expect(failure).toBeInstanceOf(AudioFolderBindingMismatchError);
    expect(failure).toMatchObject({
      workflowId: "chronicler-audio--agent/session 1",
      errorCode: "audio_binding_mismatch",
      message: "AudioBindingMismatch: destination approval binding does not match the audio package"
    });
  });

  it("posts cancellation with only the opaque workflow ID and returns authoritative status", async () => {
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent/session 1",
      state: "canceled",
      status: {
        generation_id: "generation/1",
        child_workflow_id: "chronicler-audio--agent/session 1",
        phase: "canceled",
        detail: "Cancellation completed."
      },
      result: {
        generation_id: "generation/1",
        outcome: "canceled",
        status: {
          generation_id: "generation/1",
          child_workflow_id: "chronicler-audio--agent/session 1",
          phase: "canceled",
          detail: "Cancellation completed."
        }
      },
      receipts: []
    } as const;
    const fetch = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit
    ) => new Response(JSON.stringify(snapshot), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetch);

    await expect(new HttpChroniclerAudioApi().requestAudioCancellation(
      "chronicler-audio--agent/session 1"
    )).resolves.toEqual({
      ...snapshot,
      pending_destination_revision: null,
      result: { ...snapshot.result, duration_s: null, approved_package: null }
    });

    expect(fetch.mock.calls[0]?.[0]).toBe("api/chronicler/audio/cancel");
    const init = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      workflow_id: "chronicler-audio--agent/session 1"
    });
  });

  it("implements the leader runtime cancellation transport without changing child identity", async () => {
    const snapshot = {
      child_workflow_id: "chronicler-audio--agent/session 1",
      state: "completed",
      status: {
        generation_id: "generation/1",
        child_workflow_id: "chronicler-audio--agent/session 1",
        phase: "complete",
        detail: "Completion won the cancellation race."
      },
      result: null,
      receipts: []
    } as const;
    const fetch = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit
    ) => new Response(JSON.stringify(snapshot), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetch);
    const transport: AudioGenerationControlTransport = new HttpChroniclerAudioApi();

    await expect(transport.cancelAudioGeneration(
      "chronicler-audio--agent/session 1"
    )).resolves.toBeUndefined();
    await expect(transport.getAudioGenerationStatus(
      "chronicler-audio--agent/session 1"
    )).resolves.toMatchObject({
      childWorkflowId: "chronicler-audio--agent/session 1",
      state: "completed"
    });

    expect(fetch.mock.calls.map(([input]) => String(input))).toEqual([
      "api/chronicler/audio/cancel",
      expect.stringContaining("api/chronicler/audio/status?")
    ]);
  });

  it("preserves typed HTTP failure details for audio controls", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: "Temporal service is unavailable."
    }), { status: 503, headers: { "Content-Type": "application/json" } })));

    const failure = await new HttpChroniclerAudioApi().requestAudioCancellation(
      "chronicler-audio--agent/session 1"
    ).catch((error) => error);

    expect(failure).toBeInstanceOf(ChroniclerAudioApiError);
    expect(failure).toMatchObject({
      status: 503,
      workflowId: "chronicler-audio--agent/session 1",
      message: "Temporal service is unavailable."
    });
  });

  it("reports a missing child from cancellation as a workflow lookup failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: "Audio child was not found."
    }), { status: 404, headers: { "Content-Type": "application/json" } })));

    const failure = await new HttpChroniclerAudioApi().requestAudioCancellation(
      "chronicler-audio--missing/child"
    ).catch((error) => error);

    expect(failure).toMatchObject({
      name: "WorkflowNotFoundError",
      workflowId: "chronicler-audio--missing/child",
      message: "Audio child was not found."
    });
  });
});

describe("WorkflowDiscovery", () => {
  it("omits an open Chronicler parent when its predicted audio child is not found", async () => {
    const agentApi = {
      listSessions: async () => [
        {
          workflow_id: "agent-session-1",
          created_at: 1,
          label: "Active parent",
          agent_workflow_type: "ChroniclerAgent",
          is_message_queuing_enabled: true,
          closed: false
        }
      ],
      workflowStatus: async () => ({
        workflow_id: "chronicler-audio--agent-session-1",
        execution_status: "NOT_FOUND",
        closed: true
      })
    } as unknown as AgentApi;

    await expect(new WorkflowDiscovery(agentApi).activeWorkflowIds()).resolves.toEqual([]);
  });

  it("derives only the exact fixed audio child", async () => {
    const agentApi = {
      listSessions: async () => [
        {
          workflow_id: "agent-session-1",
          created_at: 1,
          label: "Active",
          agent_workflow_type: "ChroniclerAgent",
          is_message_queuing_enabled: true,
          closed: false
        },
        {
          workflow_id: "agent-session-2",
          created_at: 2,
          label: "Closed",
          agent_workflow_type: "ChroniclerAgent",
          is_message_queuing_enabled: true,
          closed: true
        },
        {
          workflow_id: "monty-session-1",
          created_at: 3,
          label: "Other agent",
          agent_workflow_type: "MontyAgent",
          is_message_queuing_enabled: true,
          closed: false
        }
      ],
      workflowStatus: async (workflowId: string) => ({
        workflow_id: workflowId,
        execution_status: "RUNNING",
        closed: false
      })
    } as AgentApi;

    await expect(new WorkflowDiscovery(agentApi).activeWorkflowIds()).resolves.toEqual([
      "chronicler-audio--agent-session-1"
    ]);
  });
});
