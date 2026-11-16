<script lang="ts">
  /**
   * The Sessions sidebar's foot, where an app keeps its account row. The harness has no accounts,
   * so it holds the console's links to the project: the releases, a way to report an issue, and
   * the resources the home desk's status line shows, here whenever that line isn't on screen.
   */
  import { ArrowUpRight, Bug, Package, Settings2 } from "@lucide/svelte";
  import { RELEASES, REPO, RESOURCES } from "$lib/components/chat/HomeView.svelte";

  interface Props {
    /** The home desk's status line is showing the resources itself. */
    resourcesShown?: boolean;
  }

  let { resourcesShown = false }: Props = $props();

  const id = $props.id();
  const ISSUES = `${REPO}/issues/new`;
  let menu = $state<HTMLElement | null>(null);
  let open = $state(false);

  /* Above its button, its right edge on the button's: the foot is at the bottom of the window. */
  function place(event: ToggleEvent): void {
    open = event.newState === "open";
    const trigger = menu?.previousElementSibling;
    if (!open || !menu || !(trigger instanceof HTMLElement)) return;
    const box = trigger.getBoundingClientRect();
    menu.style.left = `${Math.max(8, box.right - menu.offsetWidth)}px`;
    menu.style.top = `${box.top - menu.offsetHeight - 4}px`;
  }

  const close = () => menu?.hidePopover();
</script>

<footer class="sessions-footer">
  <!-- The header's own icon button, so it reads as the strip's other icons do. -->
  <button
    type="button"
    class={["head-button", open && "on"]}
    aria-label="Links and help"
    data-tip="Links and help"
    data-tip-align="end"
    aria-expanded={open}
    popovertarget={`${id}-menu`}
  >
    <Settings2 size={13} aria-hidden="true" />
  </button>
  <div bind:this={menu} id={`${id}-menu`} class="footer-menu" popover="auto" ontoggle={place}>
    <a href={RELEASES} target="_blank" rel="noopener noreferrer" onclick={close}><Package size={12} aria-hidden="true" /><span>Releases</span><ArrowUpRight class="trail" size={12} aria-hidden="true" /></a>
    <a href={ISSUES} target="_blank" rel="noopener noreferrer" onclick={close}><Bug size={12} aria-hidden="true" /><span>Report an issue</span><ArrowUpRight class="trail" size={12} aria-hidden="true" /></a>
    {#if !resourcesShown}
      <hr />
      {#each RESOURCES as link (link.href)}
        {@const Icon = link.icon}
        <a href={link.href} target="_blank" rel="noopener noreferrer" onclick={close}><Icon size={12} aria-hidden="true" /><span>{link.name}</span><ArrowUpRight class="trail" size={12} aria-hidden="true" /></a>
      {/each}
    {/if}
  </div>
</footer>

<style>
  /* Drawn as the home desk's status line beside it is, so the two read as one strip: its button
     as tall as that line's chips. */
  .sessions-footer {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    min-width: 0;
    padding: var(--gap-xs) var(--gutter);
    border-top: 1px solid var(--border);
    background: color-mix(in srgb, var(--surface-1) 92%, var(--surface-0));
  }

  .sessions-footer > .head-button {
    width: var(--control-height-xs);
    height: var(--control-height-xs);
  }

  .footer-menu {
    position: fixed;
    inset: auto;
    margin: 0;
    width: max-content;
    min-width: 220px;
    max-width: calc(100vw - 16px);
    padding: var(--gap-xs);
    border: 0;
    background: var(--surface-3);
    box-shadow: var(--shadow-dropdown);
    color: var(--text-2);
    font-family: var(--font-sans);
    font-size: var(--font-sm);
    font-weight: 500;
  }

  .footer-menu:popover-open {
    display: flex;
    flex-direction: column;
  }

  .footer-menu a {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    height: var(--control-height);
    padding: 0 var(--gap-md) 0 var(--gap-sm);
    color: inherit;
    text-decoration: none;
  }

  .footer-menu a > span {
    flex: 1;
  }

  .footer-menu :global(.trail) {
    flex: none;
    margin-inline-start: var(--gap-md);
    color: var(--text-4);
  }

  .footer-menu hr {
    height: 1px;
    margin: var(--gap-xs) var(--gap-sm);
    border: 0;
    background: var(--border);
  }

  .footer-menu a:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  @media (hover: hover) and (pointer: fine) {
    .footer-menu a:hover {
      background: var(--control-hover);
      color: var(--text-1);
    }
  }
</style>
