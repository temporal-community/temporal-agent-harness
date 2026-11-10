import { ContextWindowOverflowError, MaxTokensError, ModelError, StructuredOutputError } from '@strands-agents/sdk';
import {
  ApplicationFailure,
  ApplicationFailureCategory,
  type ApplicationFailureOptions,
  TemporalFailure,
} from '@temporalio/common';

export const STRANDS_INTERRUPT_TYPE = 'StrandsInterrupt';

/** The failure type of an interrupt an activity threw that the plugin cannot read. */
export const STRANDS_MALFORMED_INTERRUPT_TYPE = 'StrandsMalformedInterrupt';

/** The failure type of a Strands context overflow, from the model activity to `TemporalModel`. */
export const CONTEXT_WINDOW_OVERFLOW_TYPE = 'ContextWindowOverflowError';

// Strands does not re-export `InterruptError` from its public index, so detect
// it structurally instead of via `instanceof`.
interface InterruptLike {
  toJSON(): unknown;
}

/** What each entry in a `StrandsInterrupt` failure's details holds. */
export interface InterruptDetails {
  id?: string;
  name: string;
  reason?: unknown;
  response?: unknown;
  source?: string;
}

// Any value with a non-empty `interrupts` array is taken as an interrupt, so a malformed one fails
// as such instead of being retried as an ordinary error.
function isInterruptAttempt(err: unknown): err is { interrupts: unknown[] } {
  if (err === null || typeof err !== 'object') return false;
  const { interrupts } = err as { interrupts?: unknown };
  return Array.isArray(interrupts) && interrupts.length > 0;
}

const TERMINAL_ERROR_TYPES = [
  { ctor: ContextWindowOverflowError, name: CONTEXT_WINDOW_OVERFLOW_TYPE },
  { ctor: MaxTokensError, name: 'MaxTokensError' },
  { ctor: StructuredOutputError, name: 'StructuredOutputError' },
];

// The failure type of a Strands terminal model error, or undefined for any other value. The name
// matches an error from another copy of `@strands-agents/sdk`, or any error with that name;
// `instanceof` also matches a subclass that sets a name of its own.
function terminalType(err: unknown): string | undefined {
  if (!(err instanceof Error)) return undefined;
  return TERMINAL_ERROR_TYPES.find(({ ctor, name }) => err instanceof ctor || err.name === name)?.name;
}

// `ApplicationFailure.fromError` keeps the error's message, stack and cause, but returns an
// `ApplicationFailure` it is given, and one decoded earlier would still be encoded from its original
// proto, without these overrides. Start a fresh failure for that case.
function retype(err: unknown, overrides: ApplicationFailureOptions): ApplicationFailure {
  if (err instanceof ApplicationFailure) {
    return ApplicationFailure.create({ message: err.message, ...overrides, cause: err });
  }
  return ApplicationFailure.fromError(err, overrides);
}

/**
 * Rewrite an error an activity threw. A Strands interrupt becomes a typed,
 * non-retryable failure that the worker treats as benign, and a malformed one a
 * non-retryable failure of type {@link STRANDS_MALFORMED_INTERRUPT_TYPE}. A
 * Strands terminal model error becomes a typed, non-retryable failure, benign
 * for a context overflow when `benignOverflow` is set. Each keeps the error's
 * stack and cause. Any other value is returned as it is.
 */
export function rewriteError(err: unknown, options: { benignOverflow?: boolean } = {}): unknown {
  if (isInterruptAttempt(err)) {
    const details = interruptDetails(err.interrupts);
    if (details === undefined) {
      return retype(err, {
        message: `malformed interrupt (${String((err as { message?: unknown }).message)}): each entry needs a toJSON() that returns an object with a unique string name, and JSON-encodable values`,
        type: STRANDS_MALFORMED_INTERRUPT_TYPE,
        nonRetryable: true,
      });
    }
    return retype(err, {
      message: `interrupt:${details.map((d) => d.name).join(',')}`,
      type: STRANDS_INTERRUPT_TYPE,
      nonRetryable: true,
      details,
      category: ApplicationFailureCategory.BENIGN,
    });
  }
  const type = terminalType(err);
  if (type === undefined) return err;
  const benign = options.benignOverflow === true && type === CONTEXT_WINDOW_OVERFLOW_TYPE;
  return retype(err, { type, nonRetryable: true, ...(benign ? { category: ApplicationFailureCategory.BENIGN } : {}) });
}

/**
 * The error a `TemporalAgent` turn ends with in workflow code, in place of one
 * that would only fail the workflow task on every attempt. A Strands terminal
 * model error becomes a typed, non-retryable failure with the original error as
 * its cause. A Strands `ModelError` that wraps a Temporal failure, such as a
 * model activity that failed or was cancelled, becomes that failure, so the run
 * fails or is cancelled. Any other value is returned as it is.
 */
export function workflowErrorFor(err: unknown): unknown {
  const type = terminalType(err);
  if (type !== undefined) {
    return ApplicationFailure.create({
      message: (err as Error).message,
      type,
      nonRetryable: true,
      cause: err as Error,
    });
  }
  if (err instanceof ModelError && err.cause instanceof TemporalFailure) return err.cause;
  return err;
}

/**
 * The application failure type of a decoded failure. It also reads the type from
 * the failure's proto when the failure converter returned another kind of
 * `TemporalFailure`.
 */
export function applicationFailureType(failure: unknown): string | undefined {
  if (failure instanceof ApplicationFailure) return failure.type ?? undefined;
  if (failure instanceof TemporalFailure) return failure.failure?.applicationFailureInfo?.type ?? undefined;
  return undefined;
}

// The details of each interrupt, or undefined when an entry is missing or has no toJSON(), returns
// no object with a string name, repeats another entry's name, or holds a value JSON cannot encode.
// `Array.from` visits the holes in a sparse array, which `map` and `every` skip and the worker cannot
// encode as details. Strands keys a tool's interrupts by name, so a repeated name would get the first
// entry's response. The JSON check applies whatever payload converter the worker uses.
function interruptDetails(interrupts: unknown[]): InterruptDetails[] | undefined {
  try {
    const details = Array.from(interrupts, (i) => (i as InterruptLike).toJSON());
    const named = details.every(
      (d) => d !== null && typeof d === 'object' && typeof (d as { name?: unknown }).name === 'string'
    );
    if (!named) return undefined;
    if (new Set(details.map((d) => (d as InterruptDetails).name)).size !== details.length) return undefined;
    JSON.stringify(details);
    return details as InterruptDetails[];
  } catch {
    return undefined;
  }
}
