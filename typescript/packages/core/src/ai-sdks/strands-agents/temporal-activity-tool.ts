import {
  JsonBlock,
  TextBlock,
  Tool,
  ToolResultBlock,
  type JSONSchema,
  type JSONValue,
  type ToolContext,
  type ToolSpec,
  type ToolStreamGenerator,
} from '@strands-agents/sdk';
import type { z } from 'zod';
import * as workflow from '@temporalio/workflow';
import type { ActivityOptions } from '@temporalio/workflow';
import { ActivityFailure, ApplicationFailure } from '@temporalio/common';
import { type InterruptDetails, STRANDS_INTERRUPT_TYPE } from './strands-errors.ts';
import { toJsonSchema } from './json-schema.ts';

/**
 * Options accepted by {@link activityAsTool}.
 *
 * `name` defaults to the activity function's `.name`. `description` defaults
 * to an empty string and SHOULD be supplied so the model has signal about
 * when to call the tool. `inputSchema` describes the activity's single
 * argument; pass either a literal JSON Schema object or a Zod schema, which
 * is converted to JSON Schema for you.
 */
export interface ActivityAsToolOptions {
  name?: string;
  description?: string;
  inputSchema?: JSONSchema | z.ZodType;
  activityOptions?: ActivityOptions;
}

/**
 * A Strands {@link Tool} whose body dispatches a Temporal activity via
 * {@link workflow.executeActivity}.
 *
 * When the activity fails with an {@link ApplicationFailure} whose `type`
 * matches {@link STRANDS_INTERRUPT_TYPE} — {@link StrandsPlugin} turns an
 * interrupt the activity throws into one —
 * `stream()` raises each interrupt in its details, in order, so the agent loop
 * handles them the same way as hook- or tool-body interrupts: the agent pauses
 * at the first one without a response. Other failures, including a malformed
 * interrupt, are converted to an error {@link ToolResultBlock}.
 */
export class TemporalActivityTool extends Tool {
  readonly name: string;
  readonly description: string;
  readonly toolSpec: ToolSpec;
  private readonly activityName: string;
  private readonly activityOptions: ActivityOptions;

  constructor(
    activityName: string,
    options: {
      name?: string;
      description?: string;
      inputSchema?: JSONSchema | z.ZodType;
      activityOptions?: ActivityOptions;
    }
  ) {
    super();
    this.activityName = activityName;
    this.name = options.name ?? activityName;
    this.description = options.description ?? '';
    this.toolSpec = {
      name: this.name,
      description: this.description,
      inputSchema: toJsonSchema(options.inputSchema) ?? { type: 'object', properties: {} },
    };
    this.activityOptions = options.activityOptions ?? {};
  }

  // eslint-disable-next-line require-yield
  async *stream(toolContext: ToolContext): ToolStreamGenerator {
    const { toolUseId, input } = toolContext.toolUse;
    const activities = workflow.proxyActivities<{
      [key: string]: (input: JSONValue) => Promise<unknown>;
    }>({
      startToCloseTimeout: '10 minutes',
      summary: this.name,
      ...this.activityOptions,
    });
    let activityResult: unknown;
    try {
      activityResult = await activities[this.activityName]!(input);
    } catch (err) {
      if (
        err instanceof ActivityFailure &&
        err.cause instanceof ApplicationFailure &&
        err.cause.type === STRANDS_INTERRUPT_TYPE
      ) {
        const details = (err.cause.details ?? []) as InterruptDetails[];
        // Route each interrupt through the agent's interrupt() helper, in order: the first one with
        // no response throws InterruptError and halts; on resume the answered ones return theirs.
        const responses = (details.length > 0 ? details : [{ name: 'interrupt' }]).map((d: Partial<InterruptDetails>) =>
          toolContext.interrupt<unknown>({ name: d.name ?? 'interrupt', reason: d.reason as JSONValue })
        );
        return new ToolResultBlock({
          toolUseId,
          status: 'success',
          content: [toResultContent(responses.length === 1 ? responses[0] : responses)],
        });
      }
      return activityErrorResult(toolUseId, err);
    }
    return new ToolResultBlock({
      toolUseId,
      status: 'success',
      content: [toResultContent(activityResult)],
    });
  }
}

/**
 * The error result for a tool call whose activity failed. An `ActivityFailure`'s own message is
 * the generic "Activity task failed", so the result carries its cause instead: what the activity
 * threw, or the timeout or cancellation that ended it. The error is attached for hooks.
 */
export function activityErrorResult(toolUseId: string, err: unknown): ToolResultBlock {
  const cause = err instanceof ActivityFailure && err.cause ? err.cause : err;
  const error = cause instanceof Error ? cause : new Error(String(cause));
  return new ToolResultBlock({
    toolUseId,
    status: 'error',
    content: [new TextBlock(error.message)],
    error,
  });
}

function toResultContent(value: unknown): TextBlock | JsonBlock {
  if (typeof value === 'string') return new TextBlock(value);
  return new JsonBlock({ json: value as JSONValue });
}
