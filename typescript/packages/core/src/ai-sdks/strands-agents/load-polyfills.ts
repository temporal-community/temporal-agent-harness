import { Headers } from 'headers-polyfill';
import structuredClone from '@ungap/structured-clone';
import * as webStreams from 'web-streams-polyfill';
import { inWorkflowContext, uuid4 } from '@temporalio/workflow';

// Harness: the workflow-safe package compiles without the DOM's or Node's global types, so the
// globals installed here are written through this view of `globalThis`.
const sandboxGlobals = globalThis as unknown as Record<string, unknown> & {
  crypto?: { randomUUID?: unknown };
};

if (inWorkflowContext()) {
  if (typeof sandboxGlobals.Headers === 'undefined') {
    sandboxGlobals.Headers = Headers;
  }

  // Harness: ESM has no conditional require, and `web-streams-polyfill/polyfill` replaces the
  // globals unconditionally, which would replace Node's own streams wherever this module loads.
  // Install the ponyfill's classes here, inside a workflow, only where a global is missing.
  for (const [name, value] of Object.entries(webStreams)) {
    if (!(name in sandboxGlobals)) {
      Object.defineProperty(sandboxGlobals, name, { value, writable: true, configurable: true });
    }
  }

  if (!('structuredClone' in sandboxGlobals)) {
    sandboxGlobals.structuredClone = structuredClone;
  }

  // `@ungap/structured-clone` calls `crypto.randomUUID()` internally, but the
  // workflow sandbox bans the real `crypto` module. Use the SDK's
  // deterministic `uuid4` so the polyfill still works at replay.
  if (typeof sandboxGlobals.crypto?.randomUUID !== 'function') {
    sandboxGlobals.crypto = { ...(sandboxGlobals.crypto ?? {}), randomUUID: uuid4 };
  }
}
