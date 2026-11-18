// Base64 that runs in the workflow sandbox, which has no atob/btoa or Buffer.

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

export function encodeBase64(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i] ?? 0;
    const b = i + 1 < bytes.length ? (bytes[i + 1] ?? 0) : 0;
    const c = i + 2 < bytes.length ? (bytes[i + 2] ?? 0) : 0;
    const n = (a << 16) | (b << 8) | c;
    out += ALPHABET[(n >> 18) & 63]! + ALPHABET[(n >> 12) & 63]!;
    out += i + 1 < bytes.length ? ALPHABET[(n >> 6) & 63]! : "=";
    out += i + 2 < bytes.length ? ALPHABET[n & 63]! : "=";
  }
  return out;
}

export function decodeBase64(value: string): Uint8Array {
  let length = value.length;
  while (length > 0 && value[length - 1] === "=") length--;
  const clean = value.slice(0, length);
  if (clean.length % 4 === 1) throw new Error("invalid base64");
  const out = new Uint8Array((clean.length * 3) >> 2);
  let n = 0;
  for (let i = 0; i < clean.length; i += 4) {
    const a = ALPHABET.indexOf(clean[i] ?? "");
    const b = ALPHABET.indexOf(clean[i + 1] ?? "");
    const c = i + 2 < clean.length ? ALPHABET.indexOf(clean[i + 2]!) : -1;
    const d = i + 3 < clean.length ? ALPHABET.indexOf(clean[i + 3]!) : -1;
    if (a < 0 || b < 0 || (c < 0 && i + 2 < clean.length) || (d < 0 && i + 3 < clean.length)) {
      throw new Error("invalid base64");
    }
    const chunk = (a << 18) | (b << 12) | ((c < 0 ? 0 : c) << 6) | (d < 0 ? 0 : d);
    out[n++] = (chunk >> 16) & 255;
    if (c >= 0) out[n++] = (chunk >> 8) & 255;
    if (d >= 0) out[n++] = chunk & 255;
  }
  return out.subarray(0, n);
}
