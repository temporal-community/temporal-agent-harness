import {
  buildPayload,
  describeSchema,
  singleStringField,
  type SchemaField
} from "@temporalio/agent-harness-client";
import type { AgentInterfaceFunction, JsonRecord } from "$lib/api/types";

/**
 * The enum field when a handler is a pick from a list: exactly one required field, an enum
 * with choices, and anything else optional so it can be left out. Such a handler is a menu in
 * the composer, not a form. Read from the schema alone, never the handler's name.
 */
export function choiceField(handler: AgentInterfaceFunction): SchemaField | null {
  const required = describeSchema(handler.parameters).filter((field) => field.required);
  const only = required.length === 1 ? required[0]! : null;
  return only?.kind === "enum" && only.choices?.length ? only : null;
}

/** What a choice sets, for its icon and name: read from the handler and field names. */
export type ChoiceKind = "policy" | "model" | "other";

export function choiceKind(handler: AgentInterfaceFunction, field: SchemaField): ChoiceKind {
  const names = `${handler.name} ${field.name}`;
  if (/polic/i.test(names)) return "policy";
  if (/model/i.test(names)) return "model";
  return "other";
}

/**
 * A choice's short name. A policy is "Policy" whatever its field is titled (the travel agent's
 * is "Posture", a word its users never see); anything else is its schema title, else its field
 * name in words ("approval_mode" → "Approval mode").
 */
export function choiceLabel(handler: AgentInterfaceFunction, field: SchemaField): string {
  if (choiceKind(handler, field) === "policy") return "Policy";
  return field.title !== field.name ? field.title : choiceText(field.name);
}

/** An option as read: a snake_case id in words ("auto_mode" → "Auto mode"); anything else, like a model id, as sent. */
export function choiceText(option: string): string {
  if (!/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/.test(option)) return option;
  const words = option.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A picked option's payload, built as the handler form builds one; the optional fields are left out. */
export function choicePayload(field: SchemaField, option: string): JsonRecord {
  return buildPayload([field], { [field.name]: option });
}

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
 * Whether the handler needs something from the user. One whose fields are all optional is a
 * command (start over, refresh): sent once, not the composer's resting handler.
 */
export function takesInput(handler: AgentInterfaceFunction): boolean {
  return describeSchema(handler.parameters).some((field) => field.required);
}

/**
 * Explicit choices win. Otherwise prose: a one-string handler that is not a script, the
 * uniquely chat-shaped one by schema vocabulary when there are several. A script handler is
 * the default only when nothing else takes text. With no prose at all, the first handler that
 * takes input beats a command: tic-tac-toe's `play` over `new_game`. The protocol has no
 * default flag, so ties keep declared order.
 */
export function composerHandler(
  allHandlers: readonly AgentInterfaceFunction[],
  selectedName: string | null
): AgentInterfaceFunction | null {
  const handlers = allHandlers.filter((handler) => !choiceField(handler));
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
