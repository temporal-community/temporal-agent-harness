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
