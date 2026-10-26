// Retry posture for Gemini API failures raised inside the plugin's activities (Python's
// _errors.py).
//
// Temporal retries every activity failure by default. A 4xx from the Gemini API says the request
// itself is wrong (a malformed turn order, a bad argument, a missing file, a rejected key), so
// retrying the identical request fails the same way forever and the agent's turn hangs instead of
// failing. `translateGeminiErrors` turns those into non-retryable ApplicationFailures carrying the
// status code and the API's own message. 408 (Request Timeout) and 429 (Too Many Requests) report
// a transient condition, so they propagate unchanged and stay retryable, as do 5xx and network
// errors.
//
// The SDK's errors carry the HTTP status as `status`: `ApiError` for the models, files and
// file-search-store calls, and `APIError` subclasses such as `BadRequestError` for the
// Interactions API, whose `error` is the decoded response body.

import { ApplicationFailure } from "@temporalio/activity";

// 4xx codes that report a transient condition rather than a bad request.
const TRANSIENT_CLIENT_STATUS_CODES = new Set([408, 429]);

/** Run `work`, rethrowing a Gemini client error from it as a non-retryable ApplicationFailure. */
export async function translateGeminiErrors<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work();
  } catch (e) {
    throw translateGeminiError(e);
  }
}

/**
 * A Gemini client error (4xx other than 408/429) as a non-retryable ApplicationFailure: its type
 * is the SDK error's name, its one detail is `{status_code, message}`, and its cause is the
 * original. Any other error comes back as it is.
 */
export function translateGeminiError(e: unknown): unknown {
  if (!(e instanceof Error) || e instanceof ApplicationFailure) return e;
  const status = (e as { status?: unknown }).status;
  if (typeof status !== "number" || status < 400 || status >= 500) return e;
  if (TRANSIENT_CLIENT_STATUS_CODES.has(status)) return e;
  const message = apiMessage(e);
  return ApplicationFailure.create({
    type: e.name,
    message: `Gemini API returned ${status}: ${message}`,
    details: [{ status_code: status, message }],
    nonRetryable: true,
    cause: e,
  });
}

/**
 * The API's `error.message` off the error's response body; the SDK's own message also carries the
 * status and, for some errors, the whole body. Without a body of that shape, the SDK's message.
 */
function apiMessage(e: Error): string {
  const { error, body } = e as { error?: unknown; body?: unknown };
  return (
    bodyMessage(error) ?? bodyMessage(parsed(body)) ?? bodyMessage(parsed(e.message)) ?? e.message
  );
}

function bodyMessage(body: unknown): string | undefined {
  if (body === null || typeof body !== "object" || !("error" in body)) return undefined;
  const { error } = body;
  if (error === null || typeof error !== "object" || !("message" in error)) return undefined;
  return typeof error.message === "string" ? error.message : undefined;
}

function parsed(text: unknown): unknown {
  if (typeof text !== "string") return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}
