// The workflow-side host-call driver that runs one Code Mode script to completion.
//
// Each step runs in the sandbox as an activity (the worker package's code-mode/monty-stepper.ts):
// the first type-checks the script and runs it to its first awaited batch of host calls, and each
// later one resumes the suspended script with that batch's results, until the script finishes. Its
// output and final value (or a script error) come back as text.
//
// The host-call surface is generic: the driver dispatches each call the script makes to the
// matching harness tool via `runner.runTool`, so every host call inherits that tool's approval
// policy and tool_start/tool_end lifecycle. A file operation on one of the tool's mounts is carried
// out the same way, as fs_* tool calls against the mount's backend (see vfs.ts). Calls the script
// made but has not awaited yet start as soon as a step hands them over, and keep running across
// steps until the script awaits them.
//
// Workflow-safe: the sandbox only runs in activities; this side drives it by name.

import {
  ActivityFailure,
  ApplicationFailure,
  CancellationScope,
  ChildWorkflowFailure,
  TemporalFailure,
  isCancellation,
  log,
  proxyActivities,
  sleep,
  uuid4,
} from "@temporalio/workflow";
import type { AnyHarnessTool, HarnessTool } from "../agent.ts";
import type { AgentWorkflowRunner } from "../agent-workflow.ts";
import { pyRepr, type Schema, type TypeCheckStubs } from "./stubs.ts";
import { performFileOp, type FileOpCall, type Mount } from "./vfs.ts";

export const CODE_START_BATCH_ACTIVITY = "code_start_batch";
export const CODE_RESUME_BATCH_ACTIVITY = "code_resume_batch";
export const CODE_TYPE_CHECK_ACTIVITY = "code_type_check";
export const DEFAULT_STEP_TIMEOUT = "30 seconds";
// A step that times out three times won't finish on a fourth.
export const STEP_RETRY = { maximumAttempts: 3 };

export interface PendingCall {
  /** Monty's call id. */
  call_id: number;
  function_name: string;
  args: unknown[];
  kwargs: Record<string, unknown>;
}
/** A pending call that is the script's `await asyncio.sleep(seconds)`, answered with a durable timer. */
export const SLEEP_CALL = "asyncio.sleep";
/** The workflow's clock (epoch ms) and the script's random seed: a script observes nothing of the worker. */
export interface HostClock {
  now: number;
  seed: number;
}
export interface CodeBatchStep {
  done: boolean;
  stdout: string;
  /** Opaque resume state while awaiting a batch: Monty's sealed snapshot dump. */
  snapshot: string | null;
  /** Every call the script made in this step, in call order. Each starts at once. */
  calls: PendingCall[];
  /** The calls the script is blocked on, in call order (made in this step or earlier). */
  awaiting: number[];
  /** Set when the step stopped at a file operation on a mount: the next resume answers it. */
  file_op?: FileOpCall;
  output_json: string | null;
  error: string | null;
}
export interface CallResult {
  call_id: number;
  return_value: unknown;
  /** Set when the host call failed or was denied: the script sees it as an exception it can catch. */
  error?: string;
  /** The Python exception the script raises for `error`. */
  error_type?: string;
}
export interface ResumeBatchInput {
  snapshot: string;
  results: CallResult[];
  type_names: string[];
  clock: HostClock;
  /** The script has mounts, so its file operations end steps. */
  files?: boolean;
}

export interface CodeModeActivities {
  code_start_batch(
    script: string,
    stubs: TypeCheckStubs | null,
    clock: HostClock,
    files?: boolean,
  ): Promise<CodeBatchStep>;
  code_resume_batch(input: ResumeBatchInput): Promise<CodeBatchStep>;
  code_type_check(script: string, stubs: TypeCheckStubs): Promise<string>;
}

// Python's built-in exceptions a host failure keeps as its own type inside the script.
const PYTHON_EXCEPTIONS = new Set([
  "ArithmeticError",
  "AssertionError",
  "AttributeError",
  "BufferError",
  "ConnectionError",
  "EOFError",
  "Exception",
  "FileExistsError",
  "FileNotFoundError",
  "FloatingPointError",
  "ImportError",
  "IndexError",
  "IsADirectoryError",
  "KeyError",
  "LookupError",
  "MemoryError",
  "ModuleNotFoundError",
  "NameError",
  "NotADirectoryError",
  "NotImplementedError",
  "OSError",
  "OverflowError",
  "PermissionError",
  "RecursionError",
  "RuntimeError",
  "TimeoutError",
  "TypeError",
  "UnicodeError",
  "ValueError",
  "ZeroDivisionError",
]);

// The errors a filesystem raises across a Temporal boundary as an ApplicationFailure of that type,
// which the script sees as the built-in exception of that name.
const OS_ERRORS = new Set([
  "OSError",
  "FileNotFoundError",
  "FileExistsError",
  "IsADirectoryError",
  "NotADirectoryError",
  "PermissionError",
]);

type ScriptError = Pick<CallResult, "error" | "error_type">;

/**
 * What a failed host call is to a script, as in the Python harness: a failed activity is unwrapped
 * to its cause, which carries the tool's own message. A Python built-in keeps its type: an error
 * named for one, or an ApplicationFailure of that type raised in workflow code (Python raises the
 * built-in itself there). Anything else, an activity's failure included, arrives as
 * `Exception("TypeName: message")` under its Python SDK name (`ApplicationError`, ...).
 */
function scriptError(e: unknown): ScriptError {
  const fromActivity = e instanceof ActivityFailure && e.cause !== undefined;
  const error = fromActivity ? e.cause : e;
  if (!(error instanceof Error)) return { error: String(error), error_type: "Exception" };
  const builtin =
    error instanceof ApplicationFailure
      ? fromActivity
        ? undefined
        : error.type
      : error instanceof TemporalFailure
        ? undefined
        : error.name;
  if (builtin && PYTHON_EXCEPTIONS.has(builtin))
    return { error: error.message, error_type: builtin };
  // Temporal's failures are Python's `*Error`s: ApplicationFailure is ApplicationError.
  const name =
    error instanceof TemporalFailure ? error.name.replace(/Failure$/, "Error") : error.name;
  return { error: `${name}: ${error.message}`, error_type: "Exception" };
}

/**
 * A failed file operation as the script sees it: an OS error its filesystem raised across an
 * activity or child workflow boundary is the built-in of that name, as in Python; anything else
 * is any host call's failure.
 */
function fileOpError(e: unknown): ScriptError {
  let cause: unknown = e;
  while (cause instanceof ActivityFailure || cause instanceof ChildWorkflowFailure)
    cause = cause.cause;
  if (cause instanceof ApplicationFailure && OS_ERRORS.has(cause.type ?? ""))
    return { error: cause.message, error_type: cause.type! };
  return scriptError(e);
}

/** A host call's positional and keyword arguments, bound to the tool's parameter names. */
function bindArgs(tool: AnyHarnessTool, { args, kwargs }: PendingCall): Record<string, unknown> {
  const names = Object.keys((tool.parameters.properties as Schema | undefined) ?? {});
  if (args.length > names.length) {
    throw ApplicationFailure.create({
      type: "HostCallBinding",
      nonRetryable: true,
      message: `host call '${tool.name}': too many positional arguments`,
    });
  }
  const bound: Record<string, unknown> = {};
  args.forEach((value, i) => (bound[names[i]!] = value));
  for (const [k, v] of Object.entries(kwargs)) {
    if (k in bound)
      throw ApplicationFailure.create({
        type: "HostCallBinding",
        nonRetryable: true,
        message: `host call '${tool.name}': multiple values for '${k}'`,
      });
    bound[k] = v;
  }
  return bound;
}

/**
 * Runs a Code Mode script to completion, dispatching its host calls to real harness tools.
 *
 * Constructed per invocation by the `codeModeTool` closure, with the live runner. `toolsByName` is
 * validated once by the factory; `stubs` are the generated stubs the sandbox type-checks the
 * script against before running it.
 */
export class CodeModeDriver {
  readonly #runner: AgentWorkflowRunner;
  readonly #toolsByName: ReadonlyMap<string, AnyHarnessTool>;
  readonly #injections: Readonly<Record<string, unknown>>;
  readonly #stubs: TypeCheckStubs;
  readonly #mounts: readonly Mount[];
  readonly #acts: CodeModeActivities;
  // Every call starts as soon as its step stops, so one awaited in a later step is already
  // running, and calls the script never awaits are cancelled when it ends.
  readonly #running = new Map<number, { scope: CancellationScope; result: Promise<CallResult> }>();

  constructor(
    runner: AgentWorkflowRunner,
    toolsByName: ReadonlyMap<string, AnyHarnessTool>,
    options: {
      injections: Readonly<Record<string, unknown>>;
      stubs: TypeCheckStubs;
      mounts: readonly Mount[];
      stepTimeout: string | number;
    },
  ) {
    this.#runner = runner;
    this.#toolsByName = toolsByName;
    this.#injections = options.injections;
    this.#stubs = options.stubs;
    this.#mounts = options.mounts;
    this.#acts = proxyActivities<CodeModeActivities>({
      startToCloseTimeout: options.stepTimeout,
      retry: STEP_RETRY,
    });
  }

  /** Drive `script` to completion; return its output and final value (or the error) as text. */
  async runScript(script: string): Promise<string> {
    const files = this.#mounts.length > 0;
    log.info(`code_mode: starting batch run (script_len=${script.length})`);
    // The script reads the workflow's clock and a seed drawn from the workflow's randomness.
    const seed = Math.floor(Math.random() * 2 ** 32);
    const clock = (): HostClock => ({ now: Date.now(), seed });
    const stdout: string[] = [];
    let step = await (files
      ? this.#acts.code_start_batch(script, this.#stubs, clock(), true)
      : this.#acts.code_start_batch(script, this.#stubs, clock()));
    try {
      for (;;) {
        stdout.push(step.stdout);
        if (step.done) break;
        for (const call of step.calls)
          if (!this.#running.has(call.call_id)) this.#start(call);
        let results: CallResult[];
        if (step.file_op) {
          // A file operation: the calls made before it have started, and the operation runs now.
          results = [await this.#runFileOp(step.file_op)];
        } else {
          results = await Promise.all(
            step.awaiting.map((id) => {
              const entry = this.#running.get(id);
              if (entry === undefined)
                throw ApplicationFailure.create({
                  type: "UnsupportedMontyFuture",
                  nonRetryable: true,
                  message: `the script awaits call ${id}, which it never made`,
                });
              this.#running.delete(id);
              return entry.result;
            }),
          );
        }
        step = await this.#acts.code_resume_batch({
          snapshot: step.snapshot!,
          results,
          type_names: this.#stubs.type_names,
          clock: clock(),
          ...(files ? { files } : {}),
        });
      }
    } finally {
      // Calls the script made but never awaited before it ended.
      for (const { scope } of this.#running.values()) scope.cancel();
    }
    if (step.error) return `Script error (${step.error})`;
    const parts: string[] = [];
    const combined = stdout.join("").trim();
    if (combined) parts.push(`output:\n${combined}`);
    parts.push(`result: ${pyRepr(step.output_json ? JSON.parse(step.output_json) : null)}`);
    return parts.join("\n");
  }

  #start(call: PendingCall): void {
    const scope = new CancellationScope();
    const result = scope.run(() => this.#runCall(call));
    result.catch(() => undefined);
    this.#running.set(call.call_id, { scope, result });
  }

  async #runCall(call: PendingCall): Promise<CallResult> {
    if (call.function_name === SLEEP_CALL) {
      await sleep(Number(call.args[0]) * 1000);
      return { call_id: call.call_id, return_value: null };
    }
    try {
      const target = this.#toolsByName.get(call.function_name);
      if (target === undefined) {
        // Type-checking against the stubs should already reject an unknown name; if not, the
        // script sees the failure at its await, as in Python.
        throw ApplicationFailure.create({
          type: "UnknownHostFunction",
          nonRetryable: true,
          message: `script called unknown host function ${pyRepr(call.function_name)}`,
        });
      }
      const result = await this.#runner.runTool(
        uuid4(),
        target as HarnessTool<Record<string, unknown>, unknown>,
        bindArgs(target, call),
        { injections: this.#injections },
      );
      return {
        call_id: call.call_id,
        return_value: JSON.parse(JSON.stringify(result ?? null)) as unknown,
      };
    } catch (e) {
      if (isCancellation(e)) throw e;
      return { call_id: call.call_id, return_value: null, ...scriptError(e) };
    }
  }

  async #runFileOp(op: FileOpCall): Promise<CallResult> {
    try {
      const value = await performFileOp(this.#mounts, op, (t, input, backend) =>
        this.#runner.runTool(uuid4(), t as HarnessTool<Record<string, unknown>, unknown>, input, {
          injections: { backend },
        }),
      );
      return { call_id: op.call_id, return_value: value };
    } catch (e) {
      if (isCancellation(e)) throw e;
      return { call_id: op.call_id, return_value: null, ...fileOpError(e) };
    }
  }
}
