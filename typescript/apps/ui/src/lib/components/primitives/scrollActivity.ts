/** How long a scroller keeps its thumb after it last moved. The pane rail's overlay bar fades on the same clock. */
export const SCROLL_IDLE_MS = 2000;

type Marked = Pick<Element, "setAttribute" | "removeAttribute">;

/**
 * Marks whatever scrolled with `data-scrolling` until it has been still for
 * `idleMs`, which is what app.css shows a scrollbar's thumb for besides hover.
 * Scroll does not bubble, so this listens in the capture phase on the document.
 */
export function watchScrollActivity(
  doc: Pick<Document, "addEventListener" | "removeEventListener" | "documentElement">,
  idleMs = SCROLL_IDLE_MS
): () => void {
  const timers = new Map<Marked, ReturnType<typeof setTimeout>>();
  const mark = (event: Event) => {
    const target = event.target as Partial<Element> | Document | null;
    const element = (target && "setAttribute" in target ? target : doc.documentElement) as Marked | null;
    if (!element) return;
    clearTimeout(timers.get(element));
    element.setAttribute("data-scrolling", "");
    timers.set(
      element,
      setTimeout(() => {
        element.removeAttribute("data-scrolling");
        timers.delete(element);
      }, idleMs)
    );
  };
  doc.addEventListener("scroll", mark, true);
  return () => {
    doc.removeEventListener("scroll", mark, true);
    for (const timer of timers.values()) clearTimeout(timer);
    timers.clear();
  };
}
