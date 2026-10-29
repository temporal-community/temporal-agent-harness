import type { Action } from "svelte/action";

type ClearableField = HTMLInputElement | HTMLTextAreaElement;

interface DissolveController {
  clear(commit: () => void): void;
  destroy(): void;
}

const controllers = new WeakMap<ClearableField, DissolveController>();

function durationMs(node: Element): number {
  const value = getComputedStyle(node).getPropertyValue("--duration-pane").trim();
  if (value.endsWith("ms")) return Number.parseFloat(value);
  if (value.endsWith("s")) return Number.parseFloat(value) * 1000;
  return 220;
}

function prefersReducedMotion(): boolean {
  return globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

export function dissolveClear(node: ClearableField | null, commit: () => void): void {
  if (!node) {
    commit();
    return;
  }
  const controller = controllers.get(node);
  if (controller) controller.clear(commit);
  else commit();
}

export const inputClearDissolve: Action<ClearableField> = (node) => {
  const wrapper = node.parentElement;
  if (!wrapper) return;
  wrapper.classList.add("clearable");
  let cleanup: (() => void) | undefined;

  const controller: DissolveController = {
    clear(commit) {
      const outgoing = node.value;
      node.value = "";
      commit();
      cleanup?.();
      if (!outgoing || prefersReducedMotion()) return;

      const ghost = document.createElement("span");
      const style = getComputedStyle(node);
      wrapper.classList.add("has-leaving-text");
      ghost.className = "leaving-text";
      ghost.ariaHidden = "true";
      ghost.textContent = outgoing;
      ghost.style.insetInlineStart = `${node.offsetLeft}px`;
      ghost.style.insetBlockStart = `${node.offsetTop}px`;
      ghost.style.width = `${node.clientWidth}px`;
      ghost.style.height = `${node.clientHeight}px`;
      ghost.style.font = style.font;
      ghost.style.letterSpacing = style.letterSpacing;
      ghost.style.lineHeight = style.lineHeight;
      ghost.style.textAlign = style.textAlign;
      ghost.style.direction = style.direction;
      ghost.style.padding = style.padding;
      wrapper.append(ghost);

      const remove = () => {
        ghost.remove();
        wrapper.classList.remove("has-leaving-text");
        window.clearTimeout(timer);
        if (cleanup === remove) cleanup = undefined;
      };
      const timer = window.setTimeout(remove, durationMs(node));
      ghost.addEventListener("animationend", remove, { once: true });
      cleanup = remove;
    },
    destroy() {
      cleanup?.();
      wrapper.classList.remove("clearable");
      controllers.delete(node);
    }
  };

  controllers.set(node, controller);
  return { destroy: () => controller.destroy() };
};
