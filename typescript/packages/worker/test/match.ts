// Partial deep matching for node:assert tests: `expected` names only the fields a test cares about.

import assert from "node:assert/strict";

function mismatch(actual: unknown, expected: unknown, path: string): string | undefined {
  if (Array.isArray(expected)) {
    if (!Array.isArray(actual)) return `${path || "value"} is not an array`;
    if (actual.length !== expected.length)
      return `${path || "value"} has ${actual.length} items, expected ${expected.length}`;
    for (const [i, item] of expected.entries()) {
      const problem = mismatch(actual[i], item, `${path}[${i}]`);
      if (problem) return problem;
    }
    return undefined;
  }
  if (expected instanceof RegExp)
    return typeof actual === "string" && expected.test(actual)
      ? undefined
      : `${path || "value"} does not match ${expected}`;
  if (expected !== null && typeof expected === "object") {
    if (actual === null || typeof actual !== "object") return `${path || "value"} is not an object`;
    for (const [key, value] of Object.entries(expected)) {
      if (!(key in actual)) return `${path}.${key} is missing`;
      const problem = mismatch((actual as Record<string, unknown>)[key], value, `${path}.${key}`);
      if (problem) return problem;
    }
    return undefined;
  }
  return Object.is(actual, expected) ? undefined : `${path || "value"} differs`;
}

/**
 * Asserts `actual` has every field of `expected`, recursively: objects may carry more keys, arrays
 * must have the same length and match item by item, a RegExp matches a string, anything else is
 * compared with Object.is.
 */
export function assertMatch(actual: unknown, expected: unknown, message?: string): void {
  const problem = mismatch(actual, expected, "");
  if (problem)
    throw new assert.AssertionError({
      message: message ?? problem,
      actual,
      expected,
      operator: "assertMatch",
    });
}
