import type { Attachment } from "svelte/attachments";

export interface DismissOptions {
  /** Escape, or a press that lands outside the layer. */
  ondismiss: () => void;
  /**
   * A press inside a match counts as inside the layer. Two things want this and
   * they want the same behaviour, which is why it is one option: the anchor that
   * opened the layer, because it toggles for itself and being told twice would
   * close and reopen in one press, and a companion surface the reader keeps using
   * while the layer is up — the transport under the now-card, where scrubbing must
   * not shut the card that is reporting the scrub.
   */
  keep?: string;
}

interface Layer {
  node: Element;
  options: DismissOptions;
}

/* Innermost last. Escape and an outside press are answered by the top layer
   alone, so a menu inside a dialog closes without taking the dialog with it. */
const stack: Layer[] = [];

function within(layer: Layer, target: EventTarget | null, selector?: string): boolean {
  if (!(target instanceof Node)) return false;
  if (layer.node.contains(target)) return true;
  if (!selector) return false;
  const element = target instanceof Element ? target : target.parentElement;
  return Boolean(element?.closest(selector));
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  const layer = stack.at(-1);
  if (!layer) return;
  /* Claimed, so a window-level Escape handler behind this one — the way out of
     full screen, the way up a level in a graph — does not fire on the same
     press that closed the layer sitting on top of it. */
  event.stopPropagation();
  layer.options.ondismiss();
}

/* pointerdown, not click: a press is the moment the reader has moved on, and
   waiting for the release leaves a menu open under a drag that started on the
   page behind it. A drag that starts inside and releases outside is one
   gesture and is already ignored, because the press was inside. */
function onPointerDown(event: PointerEvent): void {
  const layer = stack.at(-1);
  if (!layer) return;
  if (within(layer, event.target, layer.options.keep)) return;
  layer.options.ondismiss();
}

/**
 * Escape, press-outside, and focus return for a layer that is rendered only
 * while it is open. Attach it to the layer, not to the anchor: mounting is
 * opening and unmounting is closing, so there is no open flag to keep in sync.
 */
export function dismissable(options: DismissOptions): Attachment<Element> {
  return (node) => {
    const layer: Layer = { node, options };
    stack.push(layer);
    if (stack.length === 1) {
      /* Capture, so a handler inside the layer that stops propagation cannot
         leave a stack entry that never hears Escape again. */
      window.addEventListener("keydown", onKeydown, true);
      window.addEventListener("pointerdown", onPointerDown, true);
    }

    const opener = document.activeElement;

    return () => {
      stack.splice(stack.indexOf(layer), 1);
      if (stack.length === 0) {
        window.removeEventListener("keydown", onKeydown, true);
        window.removeEventListener("pointerdown", onPointerDown, true);
      }
      /* Give the keyboard back what it came in on, but only if the layer still
         had it: a press on something else is a move to somewhere else, and
         pulling the caret home would undo it. A removed active element leaves
         focus on the body, which is the case that needs restoring. */
      const landed = document.activeElement;
      const stranded = landed === null || landed === document.body || node.contains(landed);
      if (stranded && opener instanceof HTMLElement && opener.isConnected) {
        opener.focus();
      }
    };
  };
}
