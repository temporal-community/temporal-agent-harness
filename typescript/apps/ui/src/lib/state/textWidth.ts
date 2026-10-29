// ABOUTME: Pixel width of a line of text, measured by a canvas and cached per string, so a bar can
// ask whether its label fits. Where there is no canvas (SSR, Node tests) it falls back to a flat
// per-character estimate, which over-counts Latin, emoji and Arabic and under-counts CJK.

type TextContext = Pick<CanvasRenderingContext2D, "font" | "measureText">;

const CHAR_PX = 6.6;
/* Canvas ignores font-variant-numeric, and tabular digits run slightly wider than proportional ones. */
const TABULAR_SLACK_PX = 2;
/* Durations churn as a session runs; dropping the whole cache at the cap is cheaper than an LRU. */
const CACHE_CAP = 2_000;
const widths = new Map<string, number>();
let shared: TextContext | null | undefined;

function sharedContext(): TextContext | null {
  if (shared === undefined) {
    shared = typeof document === "undefined" ? null : document.createElement("canvas").getContext("2d");
    if (shared) document.fonts?.addEventListener("loadingdone", () => widths.clear());
  }
  return shared;
}

export function textWidth(text: string, font: string, ctx: TextContext | null = sharedContext()): number {
  if (!ctx) return text.length * CHAR_PX;
  const key = `${font}\n${text}`;
  let width = widths.get(key);
  if (width === undefined) {
    if (widths.size >= CACHE_CAP) widths.clear();
    ctx.font = font;
    width = ctx.measureText(text).width + TABULAR_SLACK_PX;
    widths.set(key, width);
  }
  return width;
}
