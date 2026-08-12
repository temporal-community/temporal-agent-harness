import type { ToolPresentation } from "$lib/components/agent/AgentChatPanel.svelte";
import type { ReplayLogRow } from "$lib/state/replayLog";
import ChroniclerGenerationAttachment from "./ChroniclerGenerationAttachment.svelte";
import type { AudioGenerationPresentation } from "./audioPresentation";

let audioPresentation = $state<AudioGenerationPresentation | null>(null);

export function setChroniclerAudioPresentation(
  presentation: AudioGenerationPresentation | null
): void {
  audioPresentation = presentation;
}

export function clearChroniclerAudioPresentation(): void {
  audioPresentation = null;
}

export function currentChroniclerAudioPresentation(): AudioGenerationPresentation | null {
  return audioPresentation;
}

function generationHostRow(
  presentation: AudioGenerationPresentation,
  rows: ReplayLogRow[]
): ReplayLogRow | undefined {
  const candidates = rows.filter((row) => row.toolName === "generate_audio");
  const matches = presentation.toolId
    ? candidates.filter((row) => row.toolId === presentation.toolId)
    : candidates.filter((row) => row.input?.generation_id === presentation.generationId);
  return matches.at(-1);
}

export function isCurrentChroniclerAudioPresentationRow(row: ReplayLogRow): boolean {
  const presentation = currentChroniclerAudioPresentation();
  if (!presentation || row.toolName !== "generate_audio") return false;
  return presentation.toolId
    ? row.toolId === presentation.toolId
    : row.input?.generation_id === presentation.generationId;
}

export const chroniclerToolPresentation: ToolPresentation = {
  attachment: ChroniclerGenerationAttachment,
  isHost: (row, rows) => {
    const presentation = currentChroniclerAudioPresentation();
    return presentation != null && generationHostRow(presentation, rows)?.id === row.id;
  }
};
