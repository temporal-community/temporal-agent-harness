/* Dev only: the page's uncaught errors and unhandled promise rejections, collected from before the
   console mounts, for the dev tools to count and list. A message that repeats is one entry with a
   count, so a render loop reads as one error ×120, not 120 rows. */
export interface DevError {
  message: string;
  count: number;
  last: number;
}

/* Enough to show what is going wrong; the browser console keeps the rest. */
const KEPT = 50;

export const devErrors: DevError[] = $state([]);

export function describeReason(reason: unknown): string {
  if (reason instanceof Error) return reason.message || reason.name;
  if (typeof reason === "string") return reason;
  try {
    return JSON.stringify(reason) ?? String(reason);
  } catch {
    return String(reason);
  }
}

export function recordDevError(message: string, now = Date.now()): void {
  const at = devErrors.findIndex((entry) => entry.message === message);
  const entry = at >= 0 ? devErrors.splice(at, 1)[0]! : { message, count: 0, last: now };
  devErrors.unshift({ ...entry, count: entry.count + 1, last: now });
  devErrors.splice(KEPT);
}

export function clearDevErrors(): void {
  devErrors.splice(0);
}

export function collectDevErrors(target: Window = window): void {
  target.addEventListener("error", (event) => recordDevError(event.message || describeReason(event.error)));
  target.addEventListener("unhandledrejection", (event) => recordDevError(`Unhandled rejection: ${describeReason(event.reason)}`));
}
