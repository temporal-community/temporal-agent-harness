import { describe, expect, it } from "vitest";
import { ChroniclerSourceService } from "./source";

function directoryWith(files: Record<string, string>): FileSystemDirectoryHandle {
  const directory = (prefix = ""): FileSystemDirectoryHandle => ({
    kind: "directory",
    name: prefix.split("/").filter(Boolean).at(-1) ?? "campaign",
    getDirectoryHandle: async (name: string) => directory(`${prefix}${name}/`),
    getFileHandle: async (name: string) => {
      const path = `${prefix}${name}`;
      if (!(path in files)) throw new DOMException("Missing", "NotFoundError");
      return {
        kind: "file",
        name,
        getFile: async () => new Blob([files[path]]) as File
      } as FileSystemFileHandle;
    },
    entries: async function* () {},
    values: async function* () {},
    isSameEntry: async () => false,
    queryPermission: async () => "granted",
    requestPermission: async () => "granted",
    removeEntry: async () => undefined,
    resolve: async () => null
  });
  return directory();
}

function sourceService(
  root: FileSystemDirectoryHandle,
  folderBindingId = "binding-1"
): ChroniclerSourceService {
  return new ChroniclerSourceService(async () => ({ root, folderBindingId }));
}

function validArchive(): Record<string, string> {
  return {
    "sessions.json": JSON.stringify({
      sessions: [{
        session_id: "session-1",
        campaign_id: "campaign-1",
        title: "The Black Bell",
        recorded_at: "2026-08-01",
        number: 1,
        audio_file: "session-1.wav"
      }]
    }),
    "transcripts/session-1.json": JSON.stringify({
      session_id: "session-1",
      model: "gemini-2.5-flash",
      duration_s: 90,
      full_text: "The party crossed the silent bridge.",
      segments: [{
        speaker: "GM",
        start_s: 0,
        end_s: 3,
        text: "The party crossed the silent bridge."
      }]
    })
  };
}

describe("Chronicler transcript sources", () => {
  it("reads a valid transcript with its exact content hash and active binding", async () => {
    const result = await sourceService(directoryWith(validArchive())).read("session-1");

    expect(result).toEqual({
      sessionId: "session-1",
      campaignId: "campaign-1",
      title: "The Black Bell",
      content: "The party crossed the silent bridge.",
      contentHash: "e5af4edfb0f3403dedac30e30b73d9056ba3964dc9339041874f7aa844510f08",
      folderBindingId: "binding-1"
    });
  });

  it("keeps valid sessions selectable when another transcript is malformed", async () => {
    const archive = validArchive();
    archive["sessions.json"] = JSON.stringify({
      sessions: [
        ...JSON.parse(archive["sessions.json"]).sessions,
        {
          session_id: "session-2",
          campaign_id: "campaign-1",
          title: "Broken Chronicle",
          recorded_at: "2026-08-02",
          number: 2,
          audio_file: "session-2.wav"
        }
      ]
    });
    archive["transcripts/session-2.json"] = JSON.stringify({
      session_id: "session-2",
      model: "gemini-2.5-flash",
      duration_s: 20,
      segments: []
    });

    const result = await sourceService(directoryWith(archive)).discover();

    expect(result).toEqual({
      status: "ready",
      sessions: [
        expect.objectContaining({
          status: "selectable",
          source: expect.objectContaining({ sessionId: "session-1" })
        }),
        {
          status: "unavailable",
          sessionId: "session-2",
          title: "Broken Chronicle",
          reason: "transcript full_text must be a non-empty string"
        }
      ]
    });
  });

  it("invalidates a selected source when its binding or transcript hash changes", async () => {
    const archive = validArchive();
    const root = directoryWith(archive);
    const source = await sourceService(root).read("session-1");

    await expect(
      sourceService(root, "binding-other").isCurrent(source)
    ).resolves.toBe(false);

    archive["transcripts/session-1.json"] = JSON.stringify({
      ...JSON.parse(archive["transcripts/session-1.json"]),
      full_text: "The party crossed a different bridge."
    });
    await expect(
      sourceService(root).isCurrent(source)
    ).resolves.toBe(false);
  });

  it("rejects values outside the known Chronicler transcript contract", async () => {
    const mutations: Array<(archive: Record<string, string>) => void> = [
      (archive) => {
        const transcript = JSON.parse(archive["transcripts/session-1.json"]);
        archive["transcripts/session-1.json"] = JSON.stringify({
          ...transcript,
          session_id: "session-other"
        });
      },
      (archive) => {
        const transcript = JSON.parse(archive["transcripts/session-1.json"]);
        archive["transcripts/session-1.json"] = JSON.stringify({ ...transcript, model: "" });
      },
      (archive) => {
        const transcript = JSON.parse(archive["transcripts/session-1.json"]);
        archive["transcripts/session-1.json"] = JSON.stringify({ ...transcript, duration_s: -1 });
      },
      (archive) => {
        const transcript = JSON.parse(archive["transcripts/session-1.json"]);
        archive["transcripts/session-1.json"] = JSON.stringify({ ...transcript, segments: {} });
      },
      (archive) => {
        const transcript = JSON.parse(archive["transcripts/session-1.json"]);
        archive["transcripts/session-1.json"] = JSON.stringify({
          ...transcript,
          segments: [{ ...transcript.segments[0], end_s: -1 }]
        });
      },
      (archive) => {
        const registry = JSON.parse(archive["sessions.json"]);
        registry.sessions[0].campaign_id = "";
        archive["sessions.json"] = JSON.stringify(registry);
      }
    ];

    for (const mutate of mutations) {
      const archive = validArchive();
      mutate(archive);
      const result = await sourceService(directoryWith(archive)).discover();
      expect(result.sessions[0]).toMatchObject({ status: "unavailable" });
    }
  });
});
