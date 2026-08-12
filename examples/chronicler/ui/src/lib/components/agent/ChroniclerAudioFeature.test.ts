import { describe, expect, it } from "vitest";
import {
  AudioStatusStartupGuard,
  audioControlsAreReadOnly,
  draftInvalidationAfterSubmit,
  resetAudioStatusPollingUnavailable,
  audioStatusFailure,
  isCurrentAudioStatusRequest,
  submitAudioMessage,
  timelineAudioSnapshot
} from "./ChroniclerAudioFeature.svelte";
import { WorkflowNotFoundError } from "$lib/bridge/api";

describe("ChroniclerAudioFeature", () => {
  it("invalidates the current review only after prepare or reprepare is accepted", () => {
    expect(draftInvalidationAfterSubmit(null, "reviewed-digest", false)).toBeNull();
    expect(draftInvalidationAfterSubmit("prior-digest", "reviewed-digest", false)).toBe(
      "prior-digest"
    );
    expect(draftInvalidationAfterSubmit(null, "reviewed-digest", true)).toBe(
      "reviewed-digest"
    );
  });

  it("retries a startup 404, then accepts the child running through completion", () => {
    let now = 1_000;
    const guard = new AudioStatusStartupGuard(5_000, () => now);
    const notFound = new WorkflowNotFoundError(
      "chronicler-audio--session-1",
      "Audio child was not found."
    );
    const running = { state: "running" } as const;
    const completed = { state: "completed" } as const;

    guard.expectChild();
    expect(guard.failure(notFound, true)).toEqual({ stopPolling: false, error: null });
    now += 1_000;
    expect(guard.accept(running)).toBe(running);
    expect(guard.accept(completed)).toBe(completed);
  });

  it("makes audio controls read-only when the current root session is closed", () => {
    expect(audioControlsAreReadOnly(true, true)).toBe(true);
  });

  it("discards an audio status response after its session changes", () => {
    expect(isCurrentAudioStatusRequest(
      { sessionId: "session-1", revision: 4 },
      { sessionId: "session-2", revision: 5 }
    )).toBe(false);
  });

  it("stops status polling and reports an identity mismatch when an expected child is gone", () => {
    expect(audioStatusFailure(
      new WorkflowNotFoundError("chronicler-audio--session-1", "Audio child was not found."),
      true,
      true,
      false
    )).toEqual({
      stopPolling: true,
      error: "The audio generation child no longer matches this session. Start a new audio review."
    });
  });

  it("retries a startup 404 while the child is being scheduled", () => {
    expect(audioStatusFailure(
      new WorkflowNotFoundError("chronicler-audio--session-1", "Audio child was not found."),
      true,
      false,
      true
    )).toEqual({ stopPolling: false, error: null });
  });

  it("reports a missing child once startup grace expires", () => {
    expect(audioStatusFailure(
      new WorkflowNotFoundError("chronicler-audio--session-1", "Audio child was not found."),
      true,
      false,
      false
    )).toEqual({
      stopPolling: true,
      error: "The audio generation child no longer matches this session. Start a new audio review."
    });
  });

  it("keeps a missing pre-child status request quiet", () => {
    expect(audioStatusFailure(
      new WorkflowNotFoundError("chronicler-audio--session-1", "Audio child was not found."),
      false
    )).toEqual({ stopPolling: false, error: null });
  });

  it("re-enables status polling after a new audio start is accepted", () => {
    expect(resetAudioStatusPollingUnavailable()).toBe(false);
  });

  it("hides a live status snapshot when the timeline is scrubbed into history", () => {
    const snapshot = { state: "running", status: { generation_id: "future-generation" } };

    expect(timelineAudioSnapshot(false, snapshot)).toBeNull();
    expect(timelineAudioSnapshot(true, snapshot)).toBe(snapshot);
  });

  it("keeps the source chooser active when completed status refreshes after changing source", () => {
    const completed = { state: "completed", status: { generation_id: "generation-1" } };
    const refreshed = { ...completed, status: { generation_id: "generation-1" } };

    expect(timelineAudioSnapshot(true, completed, true)).toBeNull();
    expect(timelineAudioSnapshot(true, refreshed, true)).toBeNull();
  });

  it.each([
    new Error("HTTP 409: already started"),
    new Error("HTTP 422: stale draft"),
    new TypeError("Failed to fetch")
  ])("keeps an exact reviewed start message unaccepted when submit fails: %s", async (failure) => {
    const message = {
      type: "start_audio",
      payload: { draft_id: "draft-1", draft_digest: "reviewed-digest" }
    } as const;
    let submitted: unknown;

    const result = await submitAudioMessage(async (value) => {
      submitted = value;
      throw failure;
    }, message as never);

    expect(submitted).toBe(message);
    expect(result).toEqual({ accepted: false, error: failure.message });
  });
});
