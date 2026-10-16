<script lang="ts">
  import Chip, { type ChipSize, type ChipTone } from "$lib/components/primitives/Chip.svelte";

  /**
   * Inline metadata tag. Same shape as StatusChip — both are Chips — so a badge
   * and a status chip at the same size are the same size.
   */
  interface Props {
    label: string;
    tone?:
      | "neutral"
      | "agent"
      | "model"
      | "reasoning"
      | "tool"
      | "approval"
      | "done"
      | "error"
      | "queue";
    size?: ChipSize;
  }

  let { label, tone = "neutral", size = "xs" }: Props = $props();

  const TONES: Record<NonNullable<Props["tone"]>, ChipTone> = {
    neutral: "neutral",
    agent: "accent",
    model: "model",
    reasoning: "reasoning",
    tool: "tool",
    approval: "queue",
    queue: "queue",
    done: "success",
    error: "error"
  };
</script>

<!-- No pip: a filled chip already states its tone in the border, the wash and the
     text, and the log row states it a fourth time on the actor glyph. The pip is
     for chips whose words are not the status — the session anchor, which is
     named after the agent — and those pass it themselves. -->
<Chip {label} {size} tone={TONES[tone]} />
