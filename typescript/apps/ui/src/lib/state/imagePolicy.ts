/* Which image sources the console will load. Only ones that cannot phone home: a raster data
   URL, or a content-addressed blob the API serves from this origin. A remote URL could be a
   tracking pixel or carry data out in its query, and an SVG can carry script, so neither loads. */

/** About 4 MB decoded. A bigger inline image is shown as "too large to preview", never decoded. */
export const MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024;

export type ImagePolicy = { status: "ok"; src: string } | { status: "too-large"; bytes: number } | null;

const DATA_IMAGE = /^data:image\/(?:png|jpeg|gif|webp);base64,([A-Za-z0-9+/]*={0,2})$/i;
const BLOB_PATH = /^\/api\/blobs\/[0-9a-f]{64}\.bin$/;

/** The decoded size of a base64 string, without decoding it. */
function base64Bytes(base64: string): number {
  const padding = base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0;
  return Math.floor((base64.length * 3) / 4) - padding;
}

/** The `src` to load for `url`, a too-large verdict, or `null` for anything not loaded as an image.
 *  A blob path comes back relative (`api/blobs/…`), as every API call is built, so a console
 *  served under a path prefix fetches it under that prefix. */
export function imagePolicy(url: string): ImagePolicy {
  const data = DATA_IMAGE.exec(url);
  if (data) {
    const bytes = base64Bytes(data[1]!);
    return bytes > MAX_INLINE_IMAGE_BYTES ? { status: "too-large", bytes } : { status: "ok", src: url };
  }
  return BLOB_PATH.test(url) ? { status: "ok", src: url.slice(1) } : null;
}

/** "5.2 MB" for the too-large notice. */
export function formatImageBytes(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
