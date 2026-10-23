// The console's half of the schema form. Describing, validating and building the payload are in
// @temporalio/agent-harness-client; these two are DOM and wording only the console needs.

import { tick } from "svelte";

/** After a refused submit, move focus to the first marked field so the reader lands on it. */
export async function focusFirstProblem(form: HTMLElement | null | undefined): Promise<void> {
  await tick();
  form?.querySelector<HTMLElement>('[aria-invalid="true"], [data-invalid]')?.focus();
}

/** One line for the whole form, since each problem is shown beside its own field. */
export function problemSummary(problems: Record<string, string>): string | null {
  const count = Object.keys(problems).length;
  if (count === 0) return null;
  return count === 1 ? "Fix the marked field." : `Fix the ${count} marked fields.`;
}
