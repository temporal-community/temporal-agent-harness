import { describe, expect, it, vi } from "vitest";
import {
  AudioPlaybackSession,
  AudioPlaybackLoadLifecycle,
  verifyLocalAudioArtifact,
  verifyLocalAudioArtifactReceipt
} from "./playback";

async function hash(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes.buffer as ArrayBuffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

describe("verified local audio playback", () => {
  it("verifies a synthetic transcript against its own artifact-role receipt", async () => {
    const bytes = new TextEncoder().encode("# Synthetic transcript");
    const contentHash = await hash(bytes);
    const receipt = {
      generation_id: "generation-1",
      artifact_role: "synthetic_transcript" as const,
      relative_path: "audio/recap.md",
      content_hash: contentHash,
      content_size: bytes.byteLength,
      package_revision: 1,
      operation_id: "audio/generation-1/synthetic_transcript/1",
      folder_binding_id: "binding-1"
    };
    const root = {
      getDirectoryHandle: vi.fn(async () => ({
        getFileHandle: vi.fn(async () => ({
          getFile: vi.fn(async () => new Blob([bytes], { type: "text/markdown" }))
        }))
      }))
    } as unknown as FileSystemDirectoryHandle;
    const repository = {
      loadAudioArtifactReceipt: vi.fn(async () => ({
        key: "generation-1::synthetic_transcript",
        generationId: "generation-1",
        artifactRole: "synthetic_transcript" as const,
        relativePath: "audio/recap.md",
        contentHash,
        contentSize: bytes.byteLength,
        folderBindingId: "binding-1",
        packageRevision: 1,
        operationId: "audio/generation-1/synthetic_transcript/1"
      }))
    };

    await expect(verifyLocalAudioArtifactReceipt(
      root,
      repository,
      receipt,
      "binding-1"
    )).resolves.toMatchObject({ size: bytes.byteLength, type: "text/markdown" });
    expect(repository.loadAudioArtifactReceipt).toHaveBeenCalledWith(
      "generation-1",
      "synthetic_transcript"
    );
  });

  it("opens a WAV only after its active-handle hash and ownership receipt match", async () => {
    const bytes = new TextEncoder().encode("RIFF\u0004\u0000\u0000\u0000WAVEfmt ");
    const contentHash = await hash(bytes);
    const receipt = {
      generation_id: "generation-1",
      artifact_role: "wav" as const,
      relative_path: "audio/recap.wav",
      content_hash: contentHash,
      content_size: bytes.byteLength,
      package_revision: 1,
      operation_id: "audio/generation-1/wav/1",
      folder_binding_id: "binding-1"
    };
    const root = {
      getDirectoryHandle: vi.fn(async () => ({
        getFileHandle: vi.fn(async () => ({
          getFile: vi.fn(async () => new Blob([bytes], { type: "audio/wav" }))
        }))
      }))
    } as unknown as FileSystemDirectoryHandle;
    const repository = {
      loadAudioArtifactReceipt: vi.fn(async () => ({
        key: "generation-1::wav",
        generationId: "generation-1",
        artifactRole: "wav" as const,
        relativePath: "audio/recap.wav",
        contentHash,
        contentSize: bytes.byteLength,
        folderBindingId: "binding-1",
        packageRevision: 1,
        operationId: "audio/generation-1/wav/1"
      }))
    };

    await expect(verifyLocalAudioArtifact(
      root,
      repository,
      receipt,
      "binding-1"
    )).resolves.toMatchObject({ size: bytes.byteLength, type: "audio/wav" });
  });

  it("supports playback retry and revokes every replaced or disposed object URL", async () => {
    const verifier = vi.fn()
      .mockRejectedValueOnce(new Error("Playback verification failed."))
      .mockResolvedValue(new Blob(["RIFF....WAVE"], { type: "audio/wav" }));
    const urls = {
      createObjectURL: vi.fn()
        .mockReturnValueOnce("blob:verified-1")
        .mockReturnValueOnce("blob:verified-2"),
      revokeObjectURL: vi.fn()
    };
    const session = new AudioPlaybackSession(verifier, urls);
    const receipt = { generation_id: "generation-1" } as never;

    await expect(session.load(receipt)).rejects.toThrow("verification failed");
    expect(urls.createObjectURL).not.toHaveBeenCalled();
    await expect(session.load(receipt)).resolves.toBe("blob:verified-1");
    await expect(session.load(receipt)).resolves.toBe("blob:verified-2");
    expect(urls.revokeObjectURL).toHaveBeenCalledWith("blob:verified-1");

    session.dispose();
    expect(urls.revokeObjectURL).toHaveBeenCalledWith("blob:verified-2");
  });

  it("revokes a late object URL when the session is disposed during verification", async () => {
    let finishVerification!: (blob: Blob) => void;
    const verifier = vi.fn(() => new Promise<Blob>((resolve) => {
      finishVerification = resolve;
    }));
    const urls = {
      createObjectURL: vi.fn(() => "blob:late"),
      revokeObjectURL: vi.fn()
    };
    const session = new AudioPlaybackSession(verifier, urls);
    const loading = session.load({ generation_id: "generation-1" } as never);

    session.dispose();
    finishVerification(new Blob(["RIFF....WAVE"], { type: "audio/wav" }));

    await expect(loading).resolves.toBeNull();
    expect(session.url).toBeNull();
    expect(urls.revokeObjectURL).toHaveBeenCalledWith("blob:late");
  });

  it("ignores a verification rejection after the playback lifecycle is disposed", async () => {
    let failVerification!: (error: Error) => void;
    const verifier = vi.fn(() => new Promise<Blob>((_resolve, reject) => {
      failVerification = reject;
    }));
    const session = new AudioPlaybackSession(verifier, {
      createObjectURL: vi.fn(),
      revokeObjectURL: vi.fn()
    });
    const loading = session.load({ generation_id: "generation-1" } as never);

    session.dispose();
    failVerification(new Error("stale folder read failed"));

    await expect(loading).resolves.toBeNull();
    expect(session.url).toBeNull();
  });

  it("keeps the newer playback URL when an older overlapping load finishes last", async () => {
    const resolutions: Array<(blob: Blob) => void> = [];
    const verifier = vi.fn(() => new Promise<Blob>((resolve) => {
      resolutions.push(resolve);
    }));
    const urls = {
      createObjectURL: vi.fn()
        .mockReturnValueOnce("blob:newer")
        .mockReturnValueOnce("blob:older-late"),
      revokeObjectURL: vi.fn()
    };
    const session = new AudioPlaybackSession(verifier, urls);
    const older = session.load({ generation_id: "generation-1" } as never);
    const newer = session.load({ generation_id: "generation-2" } as never);

    resolutions[1]?.(new Blob(["newer"]));
    await newer;
    resolutions[0]?.(new Blob(["older"]));
    await older;

    expect(session.url).toBe("blob:newer");
    expect(urls.revokeObjectURL).toHaveBeenCalledWith("blob:older-late");
  });

  it("preserves a newer playback error when an older stale load completes afterward", async () => {
    const finishes: Array<{
      resolve: (url: string | null) => void;
      reject: (error: Error) => void;
    }> = [];
    const playback = {
      load: vi.fn(() => new Promise<string | null>((resolve, reject) => {
        finishes.push({ resolve, reject });
      })),
      dispose: vi.fn()
    };
    let playbackUrl: string | null = null;
    let error: string | null = null;
    const lifecycle = new AudioPlaybackLoadLifecycle(
      playback,
      (url) => { playbackUrl = url; },
      (detail) => { error = detail; }
    );
    const older = lifecycle.load({ generation_id: "generation-1" } as never);
    const newer = lifecycle.load({ generation_id: "generation-2" } as never);

    finishes[1]?.reject(new Error("newer verification failed"));
    await newer;
    finishes[0]?.resolve(null);
    await older;

    expect(error).toBe("newer verification failed");
    expect(playbackUrl).toBeNull();
  });
});
