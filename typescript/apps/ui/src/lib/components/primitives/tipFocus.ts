/**
 * Hide a `data-tip` bubble for focus the reader did not move there themselves.
 *
 * The tip shows on `:focus-visible`, and a browser matches that for ANY focus that follows a
 * key press — including focus code hands back after Enter picks a session or Escape closes a
 * menu. That bubble then sat over the pane under it until something else took focus. CSS
 * cannot tell the two apart, so this marks the element `data-tip-quiet` when focus arrives
 * other than by a navigation key, and app.css hides a quiet tip unless it is hovered. Blur
 * clears the mark, so tabbing away and back shows the tip as usual.
 */
const NAVIGATION_KEYS = new Set([
  "Tab",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End"
]);

type TipTarget = EventTarget & Partial<Pick<Element, "hasAttribute" | "setAttribute" | "removeAttribute">>;

export function quietProgrammaticTips(doc: Pick<Document, "addEventListener" | "removeEventListener">): () => void {
  let navigating = false;
  const onKeydown = (event: KeyboardEvent) => {
    navigating = NAVIGATION_KEYS.has(event.key);
  };
  const onPointerdown = () => {
    navigating = false;
  };
  const onFocusin = (event: FocusEvent) => {
    const target = event.target as TipTarget | null;
    if (!navigating && target?.hasAttribute?.("data-tip")) target.setAttribute?.("data-tip-quiet", "");
  };
  const onFocusout = (event: FocusEvent) => {
    (event.target as TipTarget | null)?.removeAttribute?.("data-tip-quiet");
  };

  doc.addEventListener("keydown", onKeydown, true);
  doc.addEventListener("pointerdown", onPointerdown, true);
  doc.addEventListener("focusin", onFocusin, true);
  doc.addEventListener("focusout", onFocusout, true);
  return () => {
    doc.removeEventListener("keydown", onKeydown, true);
    doc.removeEventListener("pointerdown", onPointerdown, true);
    doc.removeEventListener("focusin", onFocusin, true);
    doc.removeEventListener("focusout", onFocusout, true);
  };
}

/** The gap kept between a tip and the window's edge. A bubble is at most 60vw, so both fit. */
export const TIP_MARGIN = 8;
/* app.css draws a tip at 11px × 1.4 a line, up to 8 lines, with 10px of padding and border,
   12px clear of its element. */
const TIP_LINE = 15.4;
const TIP_CHROME = 22;

/**
 * About how much room a tip takes on its side of its element. A rough count — a line per break,
 * plus one per 45 characters a line runs past — because the bubble is a pseudo-element nothing can
 * measure before it shows. It undercounts in a container narrower than the 320px cap.
 */
export function tipHeight(text: string): number {
  const lines = text.split("\n").reduce((sum, line) => sum + Math.max(1, Math.ceil(line.length / 45)), 0);
  return Math.min(lines, 8) * TIP_LINE + TIP_CHROME;
}

type Box = Pick<DOMRect, "left" | "right" | "top" | "bottom">;
type Placeable = Pick<Element, "getBoundingClientRect" | "parentElement" | "getAttribute" | "hasAttribute" | "toggleAttribute"> & {
  style: Pick<CSSStyleDeclaration, "setProperty">;
  querySelector?: (selector: string) => Pick<HTMLElement, "offsetHeight" | "textContent"> | null;
};

/* A TipCard is a real element, so it is measured; the text guess is for one not yet laid out. */
function tipNeeds(element: Placeable): number {
  const card = element.querySelector?.(":scope > .tip-card");
  if (card) return card.offsetHeight || tipHeight(card.textContent ?? "");
  return tipHeight(element.getAttribute("data-tip") ?? "");
}
type View = Pick<Window, "innerWidth" | "innerHeight"> & {
  getComputedStyle: (node: Element) => Pick<CSSStyleDeclaration, "overflowX" | "overflowY">;
  requestAnimationFrame?: (callback: FrameRequestCallback) => number;
};

/**
 * The part of the window that no ancestor of `element` clips, so the part its tip can be seen in.
 * Every ancestor that does not let overflow show is counted, so a card that clips its sliding rows
 * or a queue that scrolls a tall card bounds the tip without being registered anywhere.
 * Counting each one is the ceiling: an absolutely placed ancestor escapes a clip between it and
 * the one it is placed in, and a tip there slides or flips when it need not.
 */
function clipBox(element: Pick<Element, "parentElement">, view: View): Box {
  const box = { left: 0, top: 0, right: view.innerWidth, bottom: view.innerHeight };
  for (let node = element.parentElement; node; node = node.parentElement) {
    const { overflowX, overflowY } = view.getComputedStyle(node);
    if (overflowX === "visible" && overflowY === "visible") continue;
    const rect = node.getBoundingClientRect();
    if (overflowX !== "visible") {
      box.left = Math.max(box.left, rect.left);
      box.right = Math.min(box.right, rect.right);
    }
    if (overflowY !== "visible") {
      box.top = Math.max(box.top, rect.top);
      box.bottom = Math.min(box.bottom, rect.bottom);
    }
  }
  return box;
}

/* app.css starts a beside tip 12px clear of its element. Below this much room on the right it
   goes left, if the left has more: a rough floor, half the 320px cap, since a bubble wraps to
   whatever room it is given and so has no width to measure before it shows. */
const TIP_GAP = 12;
const TIP_SIDE_MIN = 160;

/**
 * Place a `data-tip-side="right"` tip, which app.css draws `position: fixed` so the list that
 * clips its row cannot cut it off: the window is its only box. Beside the element, centred on it
 * and held inside the window, on the left (`data-tip-flip`) when the right is too narrow.
 * It is placed as it shows, so a list scrolled under a focused row leaves the tip where it was
 * until focus or the pointer moves; a scroll listener would follow it.
 */
function placeBeside(element: Placeable, { left, right, top, bottom }: Box, view: View): void {
  const roomRight = view.innerWidth - TIP_MARGIN - TIP_GAP - right;
  const roomLeft = left - TIP_MARGIN - TIP_GAP;
  const flip = roomRight < TIP_SIDE_MIN && roomLeft > roomRight;
  const height = tipNeeds(element);
  const middle = (top + bottom) / 2;
  const tipTop = Math.max(TIP_MARGIN, Math.min(middle - height / 2, view.innerHeight - TIP_MARGIN - height));
  element.toggleAttribute("data-tip-flip", flip);
  element.style.setProperty("--tip-side-x", `${flip ? left : right}px`);
  element.style.setProperty("--tip-side-room", `${flip ? roomLeft : roomRight}px`);
  element.style.setProperty("--tip-side-middle", `${middle}px`);
  element.style.setProperty("--tip-side-top", `${tipTop}px`);
}

/**
 * Measure one tip as keepTipsInView does on hover and focus. Called directly for a tip that shows
 * because something around it took focus, such as the timeline row a cap's tip belongs to.
 */
export function placeTip(element: Placeable, view: View = window): void {
  const measure = () => {
    const { left, right, top, bottom } = element.getBoundingClientRect();
    if (element.getAttribute("data-tip-side") === "right") return placeBeside(element, { left, right, top, bottom }, view);
    const clip = clipBox(element, view);
    const centre = (left + right) / 2;
    element.style.setProperty("--tip-room-left", `${centre - clip.left - TIP_MARGIN}px`);
    element.style.setProperty("--tip-room-right", `${clip.right - TIP_MARGIN - centre}px`);
    const below = element.hasAttribute("data-tip-below");
    const ahead = below ? clip.bottom - bottom : top - clip.top;
    const behind = below ? top - clip.top : clip.bottom - bottom;
    element.toggleAttribute("data-tip-flip", ahead < tipNeeds(element) && behind > ahead);
  };
  measure();
  /* A card's element can fill it in as it shows (what its row cuts short), after this runs;
     the tip is still fading in through the frame it takes to measure it again. */
  if (element.querySelector?.(":scope > .tip-card")) view.requestAnimationFrame?.(measure);
}

/**
 * Tell a `data-tip` element, as it is about to show its tip, how far its centre is from each side
 * of the box its tip can be seen in, so app.css can slide a centred bubble inward instead of past
 * the edge; and mark it `data-tip-flip` when its tip's side has no room and the other side has more.
 *
 * A centred bubble is wider than most controls that carry one, and CSS cannot see where its
 * element sits: at 420px the Web UI link's tip hung 12px off the right edge and widened the page,
 * the attention queue clipped the tip of the reason button in its card's top corner, and the
 * Session Manager list cut off its first rows' four-line tips. Hover and focus are the two ways a
 * tip shows, so those are when to measure.
 */
export function keepTipsInView(
  doc: Pick<Document, "addEventListener" | "removeEventListener">,
  view: View = window
): () => void {
  const place = (event: Event) => {
    const element = (event.target as Partial<Element> | null)?.closest?.("[data-tip]") as Placeable | null | undefined;
    if (element?.style) placeTip(element, view);
  };
  doc.addEventListener("pointerover", place, true);
  doc.addEventListener("focusin", place, true);
  return () => {
    doc.removeEventListener("pointerover", place, true);
    doc.removeEventListener("focusin", place, true);
  };
}
