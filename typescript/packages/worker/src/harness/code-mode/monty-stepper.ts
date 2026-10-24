// Code Mode's sandbox activities: compile + run a script to its first awaited batch of host calls,
// and resume a suspended script with the batch's results. Monty suspends at every host call; each
// call is deferred as a future (no host work happens here) until the script awaits, and that
// awaited batch is handed back to the workflow, which runs the calls as durable tool calls.
//
// Each step is given the workflow's clock and answers the session's clock, environment and
// entropy calls from it, so nothing the script observes comes from the worker: `datetime.now()`,
// `date.today()` and `time.time()` read the workflow's time, `random` is seeded per script, the
// environment is empty, and `asyncio.sleep` comes back to the workflow as a durable timer. A step
// hands back every call the script made, not only the awaited ones, so the workflow can start
// them at once and resolve one awaited in a later step.

import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { ApplicationFailure, Context, log } from "@temporalio/activity";
import {
  CollectString,
  FunctionSnapshot,
  FutureSnapshot,
  Monty,
  MontyComplete,
  MontyCrashedError,
  MontyError,
  MontyFileHandle,
  MontyRuntimeError,
  MontySyntaxError,
  MontyTypingError,
  type ResourceLimits,
  type Snapshot,
} from "@pydantic/monty";
import {
  CODE_RESUME_BATCH_ACTIVITY,
  CODE_START_BATCH_ACTIVITY,
  CODE_TYPE_CHECK_ACTIVITY,
  SLEEP_CALL,
  type CallResult,
  type CodeBatchStep,
  type CodeModeActivities,
  type HostClock,
  type PendingCall,
  type ResumeBatchInput,
  type TypeCheckStubs,
} from "@temporalio/agent-harness/harness/code-mode";
import { continuationSecret } from "./secret.ts";
import {
  FILE_OPERATIONS,
  encodeBytes,
  type FileOpCall,
} from "@temporalio/agent-harness/harness/code-mode/vfs";

/** Seconds of execution a step may run between host calls, as in the Python harness. */
export const STEP_TIME_LIMIT_SECS = 1;
/**
 * Monty's own default is unlimited, so a `while True: pass` would pin a CPU until the step's
 * activity timed out.
 */
export const DEFAULT_CODE_MODE_LIMITS: ResourceLimits = { maxTurnDurationSecs: STEP_TIME_LIMIT_SECS };

/** How long `codeTypeCheck` lets a clean script run before discarding it, as in Python. */
export const TYPE_CHECK_RUN_LIMIT_SECS = 0.05;

// One worker-process pool for the whole process, created on first use and shared by every Code
// Mode activity of every worker in it; Monty recycles crashed workers. It is never closed: its
// processes end with this one, as Python's pool does at exit. A failed creation is not kept, so
// the next step tries again.
let pool: Promise<Monty> | undefined;
function montyPool(): Promise<Monty> {
  if (pool === undefined) {
    const created = Monty.create();
    pool = created;
    created.catch(() => {
      if (pool === created) pool = undefined;
    });
  }
  return pool;
}

/**
 * `limits` with the execution budget cut to fit inside this step's start-to-close, so a runaway
 * script fails on its limit instead of timing out, retrying, and spinning on in the pool. The
 * budget is per stretch between host calls, so a step that resumes many times can still overrun.
 */
function withinStep(limits: ResourceLimits): ResourceLimits {
  const timeoutMs = Context.current().info.startToCloseTimeoutMs;
  if (!timeoutMs) return limits;
  return {
    ...limits,
    maxTurnDurationSecs: Math.min(limits.maxTurnDurationSecs ?? Infinity, (timeoutMs * 0.8) / 1000),
  };
}

const timedOut = (e: MontyError) =>
  (e instanceof MontyCrashedError && e.timedOut) ||
  (e instanceof MontyRuntimeError &&
    e.exception.typeName === "TimeoutError" &&
    e.exception.message.includes("limit exceeded"));

/** What a script that ran past its time limit ends with: an error the model reads, as in Python. */
export const TIMED_OUT_ERROR = `TimeoutError: the script ran for more than ${STEP_TIME_LIMIT_SECS}s without awaiting a host function, and was stopped`;

function scriptFailure(stdout: CollectString, e: unknown): CodeBatchStep {
  if (!(e instanceof MontyError)) throw e;
  if (timedOut(e)) return { ...failed(stdout, e), error: TIMED_OUT_ERROR };
  return failed(stdout, e);
}

// As Python's `f"{type(e).__name__}: {e}"`: a typing error's whole report, a syntax error's message.
const errorText = (e: unknown) =>
  e instanceof MontyTypingError || e instanceof MontySyntaxError
    ? `${e.name}: ${e.display()}`
    : e instanceof Error
      ? `${e.name}: ${e.message}`
      : String(e);

/** Monty hands Python dicts to the host as Maps; the workflow and tools want plain JSON. */
function toJson(value: unknown): unknown {
  if (value instanceof Map)
    return Object.fromEntries([...value].map(([k, v]) => [String(k), toJson(v)]));
  if (Array.isArray(value)) return value.map(toJson);
  if (typeof value === "object" && value !== null)
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, toJson(v)]));
  return value;
}

const pyError = (name: string, message: string) => Object.assign(new Error(message), { name });

/** The session's clock, environment and entropy calls, answered from the workflow's clock. */
function answerOs(
  name: string,
  args: unknown[],
  clock: HostClock,
  callId: number,
): { value: unknown } | { error: Error } | null {
  if (name === "datetime.now") {
    const zone = args[0] as { offsetSeconds: number; name?: string } | null | undefined;
    const d = new Date(clock.now + (zone?.offsetSeconds ?? 0) * 1000);
    return {
      value: {
        __monty_type__: "DateTime",
        year: d.getUTCFullYear(),
        month: d.getUTCMonth() + 1,
        day: d.getUTCDate(),
        hour: d.getUTCHours(),
        minute: d.getUTCMinutes(),
        second: d.getUTCSeconds(),
        microsecond: d.getUTCMilliseconds() * 1000,
        ...(zone
          ? { offsetSeconds: zone.offsetSeconds, ...(zone.name ? { timezoneName: zone.name } : {}) }
          : {}),
      },
    };
  }
  if (name === "date.today") {
    const d = new Date(clock.now);
    return {
      value: {
        __monty_type__: "Date",
        year: d.getUTCFullYear(),
        month: d.getUTCMonth() + 1,
        day: d.getUTCDate(),
      },
    };
  }
  if (name === "time.time") return { value: clock.now / 1000 };
  if (name === "os.getenv") return { value: args[1] ?? null };
  if (name === "os.environ") return { value: {} };
  if (name === "os.urandom") {
    const n = args[0];
    if (typeof n !== "number" || !Number.isInteger(n))
      return { error: pyError("TypeError", "os.urandom() takes an integer size") };
    if (n < 0) return { error: pyError("ValueError", "negative argument not allowed") };
    // Deterministic per script and call: the seed is the workflow's, the call id Monty's.
    const bytes: Buffer[] = [];
    for (let i = 0; bytes.length * 32 < n; i++)
      bytes.push(createHash("sha256").update(`${clock.seed}:${callId}:${i}`).digest());
    return { value: Buffer.concat(bytes).subarray(0, n) };
  }
  return null;
}

// A paused interpreter crosses workflow history (and may be offloaded), and Monty restores only
// what a trusted producer dumped: each snapshot is sealed with an HMAC over its bytes and what it
// was dumped under (the stub type names), and opened only if it matches.
const SNAPSHOT_TAG = "monty.snapshot.v1\0";
const SEALED = "msig1.";

function snapshotMac(secret: string, dump: Uint8Array, binding: string): Buffer {
  return createHmac("sha256", secret)
    .update(SNAPSHOT_TAG)
    .update(binding)
    .update("\0")
    .update(dump)
    .digest();
}

const bindingOf = (typeNames: readonly string[]) => JSON.stringify(typeNames);

export function sealSnapshot(secret: string, dump: Uint8Array, binding: string): string {
  return `${SEALED}${snapshotMac(secret, dump, binding).toString("base64url")}.${Buffer.from(dump).toString("base64")}`;
}

/** The dump inside `snapshot`, or a non-retryable failure if it was not sealed by `secret` under `binding`. */
export function openSnapshot(secret: string, snapshot: string, binding: string): Buffer {
  const refuse = (why: string) =>
    ApplicationFailure.create({
      type: "CodeModeSnapshotRefused",
      nonRetryable: true,
      message: `refusing to resume a Code Mode script: ${why}`,
    });
  if (!snapshot.startsWith(SEALED)) throw refuse("its snapshot is not sealed");
  const [signature = "", body = ""] = snapshot.slice(SEALED.length).split(".");
  const dump = Buffer.from(body, "base64");
  const received = Buffer.from(signature, "base64url");
  const expected = snapshotMac(secret, dump, binding);
  if (received.length !== expected.length || !timingSafeEqual(received, expected)) {
    throw refuse(
      "its snapshot's seal doesn't match (tampered, sealed under other stubs, or by another RUN_CONTINUATION_SECRET)",
    );
  }
  return dump;
}

async function driveToBatch(
  start: Snapshot,
  stdout: CollectString,
  typeNames: readonly string[],
  clock: HostClock,
  seal: (dump: Uint8Array) => string,
  files = false,
): Promise<CodeBatchStep> {
  const made = () => [...seen.values()].sort((a, b) => Number(a.call_id) - Number(b.call_id));
  const seen = new Map<number, PendingCall>();
  let snap = start;
  while (true) {
    if (snap instanceof MontyComplete) {
      // Calls made but never awaited are dropped, as in Python.
      return {
        done: true,
        stdout: stdout.output,
        snapshot: null,
        calls: [],
        awaiting: [],
        output_json: JSON.stringify(toJson(snap.output) ?? null),
        error: null,
      };
    }
    if (snap instanceof FunctionSnapshot && snap.isOsFunction) {
      if (files && FILE_OPERATIONS.has(snap.functionName)) {
        // A file operation ends the step; the workflow carries it out against the mount.
        const [path, ...rest] = snap.args;
        const file_op: FileOpCall = {
          call_id: snap.callId,
          operation: snap.functionName,
          path: path instanceof MontyFileHandle ? path.path : String(path),
          args: rest.map((a) =>
            a instanceof Uint8Array
              ? encodeBytes(a)
              : a instanceof MontyFileHandle
                ? a.path
                : toJson(a),
          ),
          kwargs: toJson(snap.kwargs) as Record<string, unknown>,
        };
        return {
          done: false,
          stdout: stdout.output,
          snapshot: seal(await snap.dump()),
          calls: made(),
          awaiting: [snap.callId],
          file_op,
          output_json: null,
          error: null,
        };
      } else if (snap.functionName === "asyncio.sleep") {
        seen.set(snap.callId, {
          call_id: snap.callId,
          function_name: SLEEP_CALL,
          args: [Number(snap.args[0])],
          kwargs: {},
        });
        snap = await snap.resumeFuture();
      } else if (snap.functionName === "time.sleep") {
        snap = await snap.resumeError(
          pyError("RuntimeError", "time.sleep is unavailable; use `await asyncio.sleep(...)`"),
        );
      } else {
        const answer = answerOs(snap.functionName, snap.args, clock, snap.callId);
        snap =
          answer === null
            ? await snap.resumeNotHandled()
            : "error" in answer
              ? await snap.resumeError(answer.error)
              : await snap.resume(answer.value);
      }
      continue;
    }
    if (snap instanceof FunctionSnapshot && typeNames.includes(snap.functionName)) {
      // Calling a stub TypedDict builds a plain dict, as in CPython.
      const first = toJson(snap.args[0]);
      if (
        snap.args.length > 1 ||
        (snap.args.length === 1 &&
          (typeof first !== "object" || first === null || Array.isArray(first)))
      ) {
        snap = await snap.resumeError(
          new TypeError(`${snap.functionName}() takes at most one mapping argument`),
        );
        continue;
      }
      snap = await snap.resume({
        ...(typeof first === "object" && first !== null ? first : {}),
        ...(toJson(snap.kwargs) as object),
      });
      continue;
    }
    if (snap instanceof FunctionSnapshot) {
      seen.set(snap.callId, {
        call_id: snap.callId,
        function_name: snap.functionName,
        args: toJson(snap.args) as unknown[],
        kwargs: toJson(snap.kwargs) as Record<string, unknown>,
      });
      snap = await snap.resumeFuture();
      continue;
    }
    if (snap instanceof FutureSnapshot) {
      const snapshot = seal(await snap.dump());
      // Monty lists pending ids in an order that differs between processes; ids are assigned in
      // call order, so sorting runs a batch in the order the script made its calls.
      const awaiting = [...snap.pendingCallIds].sort((a, b) => a - b);
      return {
        done: false,
        stdout: stdout.output,
        snapshot,
        calls: made(),
        awaiting,
        output_json: null,
        error: null,
      };
    }
    // NameLookupSnapshot: leave the name undefined, so the script raises NameError, as in Python.
    snap = await snap.resume();
  }
}

const failed = (stdout: CollectString, e: unknown): CodeBatchStep => ({
  done: true,
  stdout: stdout.output,
  snapshot: null,
  calls: [],
  awaiting: [],
  output_json: null,
  error: errorText(e),
});

const osPolicy = (clock: HostClock) =>
  ({
    datetime: "call_host",
    timezone: "utc",
    sleep: "call_host",
    randomStart: { seed: clock.seed },
  }) as const;

/**
 * Type-check `script` and run it to its first awaited batch (or a file operation, with `files`).
 * Paused scripts are sealed with `secret`, checked as `codeModeActivities` checks it.
 */
export async function codeStartBatch(
  script: string,
  stubs: TypeCheckStubs | null,
  limits: ResourceLimits,
  clock: HostClock,
  secret = process.env.RUN_CONTINUATION_SECRET,
  files = false,
): Promise<CodeBatchStep> {
  return startBatch(script, stubs, limits, clock, continuationSecret(secret, "Code Mode"), files);
}

async function startBatch(
  script: string,
  stubs: TypeCheckStubs | null,
  limits: ResourceLimits,
  clock: HostClock,
  secret: string,
  files: boolean,
): Promise<CodeBatchStep> {
  log.info(
    `code_start_batch: compiling + starting (script_len=${script.length}, type_check=${stubs !== null})`,
  );
  const stdout = new CollectString();
  const fitted = withinStep(limits);
  const typeCheck = stubs === null ? {} : { typeCheck: true, typeCheckStubs: stubs.source };
  await using session = await (
    await montyPool()
  ).checkout({ limits: fitted, ...typeCheck, osPolicy: osPolicy(clock) });
  try {
    const typeNames = stubs?.type_names ?? [];
    const seal = (dump: Uint8Array) => sealSnapshot(secret, dump, bindingOf(typeNames));
    return await driveToBatch(
      await session.feedStart(script, { printCallback: stdout }),
      stdout,
      typeNames,
      clock,
      seal,
      files,
    );
  } catch (e) {
    return scriptFailure(stdout, e);
  }
}

/** Resume a paused script with its batch's results; `secret` is checked as in `codeStartBatch`. */
export async function codeResumeBatch(
  input: ResumeBatchInput,
  limits = DEFAULT_CODE_MODE_LIMITS,
  secret = process.env.RUN_CONTINUATION_SECRET,
): Promise<CodeBatchStep> {
  return resumeBatch(input, limits, continuationSecret(secret, "Code Mode"));
}

async function resumeBatch(
  input: ResumeBatchInput,
  limits: ResourceLimits,
  secret: string,
): Promise<CodeBatchStep> {
  const binding = bindingOf(input.type_names);
  const dump = openSnapshot(secret, input.snapshot, binding);
  const stdout = new CollectString();
  const fitted = withinStep(limits);
  await using session = await (
    await montyPool()
  ).checkout({ limits: fitted, osPolicy: osPolicy(input.clock) });
  const snap = await session.loadSnapshot(dump, { printCallback: stdout });
  const isFileOp = snap instanceof FunctionSnapshot && snap.isOsFunction;
  if (!(snap instanceof FutureSnapshot) && !isFileOp) {
    throw ApplicationFailure.create({
      type: "MontyResumeKind",
      nonRetryable: true,
      message: `code_resume_batch expected a suspended script, got ${snap.constructor.name}`,
    });
  }
  // A failed call raises at its await (or file operation) as `error_type`, a Python exception
  // name, or as RuntimeError when the workflow named none.
  const failure = (r: CallResult) =>
    Object.assign(new Error(r.error), { name: r.error_type ?? "RuntimeError" });
  try {
    let resumed: Snapshot;
    if (snap instanceof FunctionSnapshot) {
      const r = input.results.find((x) => x.call_id === snap.callId);
      if (r === undefined)
        throw ApplicationFailure.create({
          type: "MontyResumeKind",
          nonRetryable: true,
          message: `no result for file operation ${snap.callId}`,
        });
      const value = r.return_value as {
        __monty__?: string;
        path?: string;
        mode?: string;
        base64?: string;
      } | null;
      if (typeof r.error === "string") resumed = await snap.resumeError(failure(r));
      else if (value?.__monty__ === "not_handled") resumed = await snap.resumeNotHandled();
      else if (value?.__monty__ === "handle")
        resumed = await snap.resume(new MontyFileHandle(value.path!, value.mode!));
      else if (value?.__monty__ === "bytes")
        resumed = await snap.resume(Buffer.from(value.base64!, "base64"));
      else resumed = await snap.resume(value);
    } else {
      resumed = await (snap as FutureSnapshot).resume(
        input.results.map((r) =>
          typeof r.error === "string"
            ? { callId: r.call_id, error: failure(r) }
            : { callId: r.call_id, value: r.return_value },
        ),
      );
    }
    return await driveToBatch(
      resumed,
      stdout,
      input.type_names,
      input.clock,
      (next) => sealSnapshot(secret, next, binding),
      input.files,
    );
  } catch (e) {
    return scriptFailure(stdout, e);
  }
}

/**
 * The checker's syntax or type errors for `script` against `stubs`, or "" when it is clean.
 * Monty has no check-only entry point, but it checks a script before running any of it, so a
 * clean script is started and then discarded: it runs only until its first suspension (or a brief
 * time limit), with the clock, entropy and sleeps routed to the host as in a step, so nothing
 * sleeps and no host call is answered. How far it got is never looked at, so a script that raises
 * or loops is clean. A crashed sandbox worker says nothing about the script and is thrown.
 */
export async function codeTypeCheck(script: string, stubs: TypeCheckStubs): Promise<string> {
  await using session = await (
    await montyPool()
  ).checkout({
    typeCheck: true,
    typeCheckStubs: stubs.source,
    limits: { maxTurnDurationSecs: TYPE_CHECK_RUN_LIMIT_SECS },
    osPolicy: osPolicy({ now: 0, seed: 0 }),
  });
  try {
    await session.feedStart(script, { printCallback: new CollectString() });
  } catch (e) {
    if (e instanceof MontySyntaxError || e instanceof MontyTypingError) return errorText(e);
    if (e instanceof MontyCrashedError || !(e instanceof MontyError)) throw e;
  }
  return "";
}

/**
 * The Code Mode activities. Paused scripts are sealed with `continuationSecret` (or
 * RUN_CONTINUATION_SECRET): every worker that runs them needs the same one, at least 32 bytes. The
 * published local-dev value is refused when NODE_ENV is production, and warned about otherwise.
 */
export function codeModeActivities({
  limits = DEFAULT_CODE_MODE_LIMITS,
  continuationSecret: secret = process.env.RUN_CONTINUATION_SECRET,
}: { limits?: ResourceLimits; continuationSecret?: string | undefined } = {}): CodeModeActivities {
  const key = continuationSecret(secret, "Code Mode");
  return {
    [CODE_START_BATCH_ACTIVITY]: (
      script: string,
      stubs: TypeCheckStubs | null,
      clock: HostClock,
      files?: boolean,
    ) => startBatch(script, stubs, limits, clock, key, files ?? false),
    [CODE_RESUME_BATCH_ACTIVITY]: (input: ResumeBatchInput) => resumeBatch(input, limits, key),
    [CODE_TYPE_CHECK_ACTIVITY]: codeTypeCheck,
  } as CodeModeActivities;
}
