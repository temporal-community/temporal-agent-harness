import { ApplicationFailure, CancellationScope, isCancellation } from "@temporalio/workflow";
import type { AutoModeEvaluator, ToolApprovalPolicy } from "../agent-protocol/index.ts";
import { portabilityProblem } from "../state/index.ts";

export class ToolApprovalDenied extends Error {
  override name = "ToolApprovalDenied";
  readonly toolName: string;
  readonly reason: string | null;
  constructor(toolName: string, reason: string | null) {
    super(`tool '${toolName}' was not approved${reason ? `: ${reason}` : ""}`);
    this.toolName = toolName;
    this.reason = reason;
  }
}

export class CallbackToolError extends Error {
  override name = "CallbackToolError";
  readonly toolName: string;
  readonly reason: string | null;
  constructor(toolName: string, reason: string | null) {
    super(`callback tool '${toolName}' failed${reason ? `: ${reason}` : ""}`);
    this.toolName = toolName;
    this.reason = reason;
  }
}

/** What Python's `str(e)` gives for an exception: its message alone. */
export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Whether `e` is the cancellation of the scope this code runs in (Python's CancelledError, which
 * `except Exception` lets through), not a cancellation it observed from an inner scope, such as an
 * author's own timeout, which is a failure like any other.
 */
export function isOwnCancellation(e: unknown): boolean {
  return isCancellation(e) && CancellationScope.current().consideredCancelled;
}

/** An error and its causes, deduplicated: an ActivityFailure's own message says only "Activity task failed". */
export function errorText(e: unknown): string {
  const parts: string[] = [];
  let current: unknown = e;
  while (current instanceof Error && parts.length < 5) {
    const text = current.message || current.name;
    if (!parts.includes(text)) parts.push(text);
    current = current.cause;
  }
  return parts.length ? parts.join(": ") : String(e);
}

/** Rejects JSON a Python peer can't read back as sent. */
export function assertPortableJson(value: unknown, what: string): void {
  const problem = portabilityProblem(value);
  if (problem !== undefined)
    throw ApplicationFailure.create({
      type: "NonPortableJson",
      nonRetryable: true,
      message: `${what} ${problem}`,
    });
}

export function assertAutoModeHasEvaluator(
  policy: ToolApprovalPolicy,
  evaluator: AutoModeEvaluator | undefined,
): void {
  if (evaluator !== undefined || !policy.auto_mode_enabled) return;
  throw ApplicationFailure.create({
    type: "AutoModeWithoutEvaluator",
    nonRetryable: true,
    message:
      "ToolApprovalPolicy has auto_mode_enabled=True, but this agent has no autoModeEvaluator wired, so " +
      "nothing could ever judge a call while the policy reported auto mode on. Either pass " +
      "autoModeEvaluator (jevEvaluator() is the builtin), or use a policy with auto mode off.",
  });
}
