import { readdir, readFile } from "node:fs/promises";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const genericUiRoot = fileURLToPath(new URL("../../../../ui/src/", import.meta.url));
const genericPackagePath = fileURLToPath(new URL("../../../../ui/package.json", import.meta.url));

const chroniclerContracts: ReadonlyArray<readonly [string, RegExp]> = [
  ["Chronicler product name or import", /\bchronicler\b/i],
  ["Chronicler workflow type", /\bChroniclerAgent\b/],
  ["Chronicler audio child-workflow ID", /chronicler-audio--/],
  ["Chronicler audio API route", /chronicler\/audio\//],
  ["Chronicler transcript archive file", /[\"']sessions\.json[\"']/],
  ["Chronicler transcript archive path", /[\"']transcripts\//],
  ["Chronicler archive contract field", /\b(?:campaign_id|recorded_at|audio_file)\b/],
  ["Chronicler audio contract field", /\b(?:approved_package|pending_destination_revision|source_identity)\b/],
  ["Chronicler synthetic-transcript contract or copy", /\bsynthetic[_ ]transcript\b/i],
  ["Chronicler-specific audio-generation copy", /\baudio generation child\b/i]
];

const exampleContracts: ReadonlyArray<readonly [string, RegExp]> = [
  ["Chronicler identifier", /\bchronicler\b/i],
  ["audio generation tool", /\bgenerate_audio\b/],
  ["audio preparation tool", /\bprepare_audio\b/],
  ["audio start tool", /\bstart_audio\b/],
  ["audio recovery tool", /\brecover_audio\b/],
  ["Chronicler API route", /\/api\/chronicler\b/],
  ["Chronicler audio workflow contract", /\bchronicler-audio\b/]
];

const specializedFiles = [
  "lib/bridge/api.ts",
  "lib/bridge/controller.ts",
  "lib/bridge/playback.ts",
  "lib/bridge/source.ts",
  "lib/components/agent/AudioApprovalPanel.svelte",
  "lib/components/agent/AudioGenerationCard.svelte",
  "lib/components/agent/ChroniclerAudioFeature.svelte",
  "lib/components/agent/ChroniclerAudioWorkspace.svelte",
  "lib/components/agent/audioDiscovery.ts",
  "lib/components/agent/audioLive.ts",
  "lib/components/agent/audioPreflight.ts",
  "lib/components/agent/audioPresentation.ts",
  "lib/components/agent/audioUi.ts"
];

async function sourcesMatching(
  directory: string,
  matches: (name: string) => boolean
): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourcesMatching(path, matches);
    if (!entry.isFile() || !matches(entry.name)) return [];
    return [path];
  }));

  return nested.flat();
}

function productionSources(directory = genericUiRoot): Promise<string[]> {
  return sourcesMatching(
    directory,
    (name) => /\.(?:ts|svelte)$/.test(name) && !/\.(?:test|spec)\.ts$/.test(name)
  );
}

function genericTestSources(): Promise<string[]> {
  return sourcesMatching(genericUiRoot, (name) => /\.(?:test|spec)\.ts$/.test(name));
}

describe("generic production UI isolation", () => {
  it("contains no Chronicler-specific files, imports, names, workflow/API contracts, or copy", async () => {
    const sources = await productionSources();
    const violations: string[] = [];

    for (const source of sources) {
      const contents = await readFile(source, "utf8");
      const sourcePath = relative(genericUiRoot, source);

      for (const [description, pattern] of chroniclerContracts) {
        if (pattern.test(contents)) {
          violations.push(`${sourcePath}: ${description}`);
        }
      }
    }

    for (const specializedFile of specializedFiles) {
      if (sources.some((source) => relative(genericUiRoot, source) === specializedFile)) {
        violations.push(`${specializedFile}: Chronicler-specific production file remains`);
      }
    }

    expect(violations, `Chronicler UI leakage:\n${violations.join("\n")}`).toEqual([]);
  });

  it("keeps example-specific identifiers and contracts out of generic package and test sources", async () => {
    const sources = await genericTestSources();
    const violations: string[] = [];
    const packageContents = await readFile(genericPackagePath, "utf8");
    const packageFields = Object.entries(
      JSON.parse(packageContents) as Record<string, unknown>
    );

    for (const [field, value] of packageFields) {
      const contents = typeof value === "string" ? value : JSON.stringify(value);
      for (const [description, pattern] of exampleContracts) {
        if (pattern.test(contents)) {
          violations.push(`package.json ${field}: ${description}`);
        }
      }
    }

    for (const source of sources) {
      const contents = await readFile(source, "utf8");
      const sourcePath = relative(genericUiRoot, source);

      for (const [description, pattern] of exampleContracts) {
        if (pattern.test(contents)) {
          violations.push(`${sourcePath}: ${description}`);
        }
      }
    }

    expect(violations, `Generic UI example leakage:\n${violations.join("\n")}`).toEqual([]);
  });
});
