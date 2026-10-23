/* Images a tool returned, found in its output: a `blob_ref` the API serves from /api/blobs, or an
   inline base64 image part. Each is only a candidate; SafeImage runs imagePolicy before loading. */

import { imagePolicy } from "./imagePolicy";

export interface ToolImage {
  src: string;
  alt: string;
}

const DATA_IMAGE = /^data:image\//i;

function imageIn(value: Record<string, unknown>): string | null {
  const ref = value.blob_ref;
  if (ref && typeof ref === "object") {
    const { url, media_type } = ref as Record<string, unknown>;
    return typeof url === "string" && typeof media_type === "string" && media_type.startsWith("image/") ? url : null;
  }
  /* MCP's `{type: "image", data, mimeType}`, or the same with `media_type`. */
  const mime = value.mimeType ?? value.media_type;
  if (value.type === "image" && typeof value.data === "string" && typeof mime === "string") return `data:${mime};base64,${value.data}`;
  return null;
}

function collect(value: unknown, out: string[]): void {
  if (typeof value === "string") {
    if (DATA_IMAGE.test(value)) out.push(value);
  } else if (Array.isArray(value)) {
    for (const item of value) collect(item, out);
  } else if (value && typeof value === "object") {
    const found = imageIn(value as Record<string, unknown>);
    if (found) out.push(found);
    else for (const item of Object.values(value)) collect(item, out);
  }
}

/** The images in a tool's output (JSON, or a bare data URL), de-duplicated, that imagePolicy
 *  would load or call too large; a remote URL or an SVG is left in the output text. */
export function toolImages(output: string | undefined, toolName = ""): ToolImage[] {
  if (!output || !(output.includes("blob_ref") || output.includes("data:image/") || output.includes('"image"'))) return [];
  let parsed: unknown = output.trim();
  try {
    parsed = JSON.parse(output);
  } catch {
    /* Not JSON: only a bare data URL counts. */
  }
  const found: string[] = [];
  collect(parsed, found);
  const alt = toolName ? `Image from ${toolName}` : "";
  return [...new Set(found)].filter((src) => imagePolicy(src)).map((src) => ({ src, alt }));
}
