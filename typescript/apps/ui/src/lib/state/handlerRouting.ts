import { describeSchema, singleStringField } from "@temporalio/agent-harness-client";
import type { AgentInterfaceFunction } from "$lib/api/types";

const NATURAL_LANGUAGE = /\b(?:description|instruction|message|prompt|question|query|request|text)\b/i;
const SCRIPT = /\bscript\b/i;

function singleStringSchemaText(handler: AgentInterfaceFunction): string | null {
  const fieldName = singleStringField(handler.parameters);
  if (!fieldName) return null;
  const field = describeSchema(handler.parameters).find((candidate) => candidate.name === fieldName);
  return [fieldName, field?.title, field?.description].filter(Boolean).join(" ");
}

/**
 * Whether the handler's one string field is a script, read from its schema alone. The handler's
 * own description is prose about the agent: Monty's chat handler says it runs "a script-runner
 * subagent", and that made it a Python box.
 */
export function takesScript(handler: AgentInterfaceFunction): boolean {
  const schemaText = singleStringSchemaText(handler);
  const modelText = handler.parameters.description;
  return (
    schemaText != null &&
    (SCRIPT.test(schemaText) || (typeof modelText === "string" && SCRIPT.test(modelText)))
  );
}

function naturalLanguageScore(handler: AgentInterfaceFunction): number {
  const schemaText = singleStringSchemaText(handler) ?? "";
  const handlerText = [handler.name, handler.description].join(" ");
  return (NATURAL_LANGUAGE.test(schemaText) ? 2 : 0) + (NATURAL_LANGUAGE.test(handlerText) ? 1 : 0);
}

/**
 * Whether the handler sets one thing from a list: one required enum field, anything else
 * optional. Read from the schema alone. Its messages read as a setting change in the chat.
 */
export function takesOneChoice(handler: AgentInterfaceFunction): boolean {
  const required = describeSchema(handler.parameters).filter((field) => field.required);
  return required.length === 1 && required[0]!.kind === "enum";
}

/**
 * Whether the handler needs something from the user. One whose fields are all optional is a
 * command (start over, refresh): sent once, not the composer's resting handler.
 */
export function takesInput(handler: AgentInterfaceFunction): boolean {
  return describeSchema(handler.parameters).some((field) => field.required);
}

/**
 * Whether a form handler's card is dealt. While idle, always: the agent can only be waiting on
 * the reader. Mid-turn, once the reader picks it, whatever its `mid_turn`: the agent decides,
 * and a `reject` handler's refusal comes back as the send's error.
 */
export function offersActionCard(
  handler: AgentInterfaceFunction,
  selectedName: string | null,
  idle: boolean
): boolean {
  return idle || handler.name === selectedName;
}

/**
 * Whether a sent pick has done its job: the turn it started is under way, or already over. A
 * settings change can open and end its turn between two frames, so the run is never seen busy;
 * a turn newer than the one at the send counts too, or the card stays offered for another send.
 */
export function pickSettled(sentAtTurn: number, idle: boolean, turn: number): boolean {
  return !idle || turn > sentAtTurn;
}

/**
 * The attention queue: requests the agent is blocked on first, in its order, then the offered
 * card; a card the reader picked goes in front, where it can be filled in.
 */
export function actionQueue<Row>(pending: readonly Row[], card: Row | null, picked: boolean): Row[] {
  if (card == null) return [...pending];
  return picked ? [card, ...pending] : [...pending, card];
}

/**
 * Explicit choices win. Otherwise prose: a one-string handler that is not a script, the
 * uniquely chat-shaped one by schema vocabulary when there are several. A script handler is
 * the default only when nothing else takes text. With no prose at all, the first handler that
 * takes input beats a command: tic-tac-toe's `play` over `new_game`. The protocol has no
 * default flag, so ties keep declared order.
 */
export function composerHandler(
  handlers: readonly AgentInterfaceFunction[],
  selectedName: string | null
): AgentInterfaceFunction | null {
  if (handlers.length === 0) return null;
  const selected = handlers.find((handler) => handler.name === selectedName);
  if (selected) return selected;

  const proseHandlers = handlers.filter(
    (handler) => singleStringField(handler.parameters) != null && !takesScript(handler)
  );
  const fallback = proseHandlers[0] ?? handlers.find(takesInput) ?? handlers[0]!;
  if (proseHandlers.length < 2) return fallback;

  const ranked = proseHandlers
    .map((handler) => ({ handler, score: naturalLanguageScore(handler) }))
    .sort((left, right) => right.score - left.score);
  return ranked[0]!.score > 0 && ranked[0]!.score > ranked[1]!.score
    ? ranked[0]!.handler
    : fallback;
}
