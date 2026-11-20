/* Dev only: mounts the dev tools button beside the console. DevTools.svelte draws it. */
import { mount } from "svelte";
import type { ComponentProps } from "svelte";
import DevTools from "./DevTools.svelte";

/** Like TanStack's `hideInAutomation`, it stays out of a page a test browser drives, so
 *  screenshots show only the console; `&devtools` brings it back for checking the dev tools themselves. */
export function mountDevTools(props: ComponentProps<typeof DevTools>): void {
  if (navigator.webdriver && !new URLSearchParams(location.search).has("devtools")) return;
  mount(DevTools, { target: document.body, props });
}
