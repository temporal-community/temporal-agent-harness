// An agent's init data, as the console asks for it before starting a session: which agents need
// it, the form it renders as, and the payload a filled form becomes. Pure, so it can be tested
// without a DOM (the component tests render on the server only).

import type { AgentDescriptor } from "$lib/api/types";
import {
  buildPayload,
  describeSchema,
  emptyValues,
  validate,
  type SchemaField
} from "$lib/components/chat/schemaForm";

/** Whether starting `agent` needs a form first: it takes init data, required or not. */
export function takesInitData(agent: AgentDescriptor): boolean {
  return agent.init_data != null;
}

/** Whether a session for `agent` cannot start without init data. */
export function requiresInitData(agent: AgentDescriptor): boolean {
  return agent.init_data?.required === true;
}

/**
 * The agent the console starts a session for on boot, when there is no session to reopen: the
 * `qa` agent, else the first registered one — skipping any that require init data, since boot
 * has nobody to ask for it. `null` when every agent requires it.
 */
export function defaultBootAgent(agents: AgentDescriptor[]): AgentDescriptor | null {
  const startable = agents.filter((agent) => !requiresInitData(agent));
  return startable.find((agent) => agent.key === "qa") ?? startable[0] ?? null;
}

export interface InitDataForm {
  fields: SchemaField[];
  values: Record<string, unknown>;
  required: boolean;
  title: string;
  description: string | null;
}

/** The form `agent`'s init data renders as, or `null` if it takes none. */
export function initDataForm(agent: AgentDescriptor): InitDataForm | null {
  const initData = agent.init_data;
  if (!initData) return null;
  const fields = describeSchema(initData.schema);
  const schemaTitle = initData.schema.title;
  const schemaDescription = initData.schema.description;
  return {
    fields,
    values: emptyValues(fields),
    required: initData.required,
    title: typeof schemaTitle === "string" ? schemaTitle : "Init data",
    description: typeof schemaDescription === "string" ? schemaDescription : null
  };
}

/**
 * The filled form as init data; or what keeps it from being submitted, as `validate()`'s
 * problems keyed by field path, or an `error` when the values cannot be read at all.
 */
export function initDataPayload(
  form: InitDataForm
): { data: Record<string, unknown> } | { problems: Record<string, string> } | { error: string } {
  const problems = validate(form.fields, form.values);
  if (Object.keys(problems).length > 0) return { problems };
  try {
    return { data: buildPayload(form.fields, form.values) };
  } catch (error) {
    return { error: error instanceof Error ? error.message : "Could not read the form." };
  }
}
