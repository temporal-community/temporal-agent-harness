/** Opens a `.table-menu` popover under the control that opened it, its end on the control's end,
 *  kept inside the viewport. Wired to the popover's `toggle` event. */
export function placeMenu(event: ToggleEvent, anchor: HTMLElement | null): void {
  const menu = event.currentTarget as HTMLElement;
  if (event.newState !== "open" || !anchor) return;
  const box = anchor.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(box.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.min(box.bottom + 4, innerHeight - menu.offsetHeight - 8)}px`;
}
