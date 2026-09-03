/**
 * Keyboard bindings for the replay transport.
 *
 * The table below is the only place a binding is written down: the window
 * handler matches against it and the help overlay renders from it, so a key
 * that works is a key that is documented.
 *
 * `resolveReplayAction` is deliberately DOM-free. Everything that decides
 * whether a key should act at all — is the user typing, is an IME mid-word, is
 * the scrubber focused — is expressed in a plain object, which is what makes
 * the guard testable in a script with no browser.
 */

import type { AgentRunController } from "./agentRun.svelte";

export type ReplayAction =
  | "stepBack"
  | "stepForward"
  | "previousTurn"
  | "nextTurn"
  | "first"
  | "last"
  | "togglePlay"
  | "toggleHelp"
  | "closeHelp";

export interface ReplayBinding {
  action: ReplayAction;
  /** `KeyboardEvent.key` to match. Single characters are compared lowercased. */
  key: string;
  /** Required Shift state, or `null` when Shift takes no part in the match. */
  shift: boolean | null;
  /** How the chord reads in the help overlay. */
  chord: string;
  label: string;
}

/**
 * No binding carries Ctrl, Meta or Alt. That is not an oversight: those belong
 * to the browser (find, reload, tab switching), and the cheapest way not to
 * fight it is to leave the whole modifier space alone.
 */
export const REPLAY_BINDINGS: readonly ReplayBinding[] = [
  { action: "stepBack", key: "ArrowLeft", shift: false, chord: "←", label: "Previous event" },
  { action: "stepForward", key: "ArrowRight", shift: false, chord: "→", label: "Next event" },
  {
    action: "previousTurn",
    key: "ArrowLeft",
    shift: true,
    chord: "Shift ←",
    label: "Previous turn"
  },
  { action: "nextTurn", key: "ArrowRight", shift: true, chord: "Shift →", label: "Next turn" },
  { action: "first", key: "Home", shift: null, chord: "Home", label: "First event" },
  /* Landing on the last event *is* following the live edge: goTo() sets `following` from
     `viewIndex === total`, so there is one behaviour here and one row for it. */
  { action: "last", key: "End", shift: null, chord: "End", label: "Latest event, and follow live" },
  { action: "togglePlay", key: " ", shift: false, chord: "Space", label: "Play / pause" },
  { action: "toggleHelp", key: "?", shift: null, chord: "?", label: "Show this list" },
  { action: "closeHelp", key: "Escape", shift: null, chord: "Esc", label: "Close this list" }
];

/**
 * A keyboard event reduced to the things a binding decision depends on.
 */
export interface ReplayKeyContext {
  key: string;
  shiftKey: boolean;
  /** Ctrl, Meta or Alt is down, so the chord belongs to the browser or the OS. */
  modified: boolean;
  /** An IME is composing a character and these keystrokes are spelling it. */
  composing: boolean;
  /** Focus is in a text field, a select, or a contenteditable region. */
  typing: boolean;
  /** Focus is on a range input, which moves itself on the navigation keys. */
  rangeFocused: boolean;
  /** Focus is on something the browser activates with Space, such as a button. */
  spaceActivates: boolean;
  /**
   * Keys the focused element has declared it handles, read from
   * `aria-keyshortcuts`. A control that says which keys are its own gets them.
   */
  claimedKeys: readonly string[];
}

/**
 * Input types that hold no text, so a keystroke over one is not typing.
 */
const NON_TEXT_INPUT_TYPES = new Set([
  "button",
  "checkbox",
  "color",
  "file",
  "hidden",
  "image",
  "radio",
  "range",
  "reset",
  "submit"
]);

/**
 * Keys a focused range input already acts on. The scrubber's arrow stepping is
 * free platform behaviour and the point of this set is to let it stay that way:
 * without it, one Right press would step the native input *and* run the global
 * binding, moving the playhead two events instead of one.
 */
const RANGE_NATIVE_KEYS = new Set([
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "ArrowDown",
  "Home",
  "End",
  "PageUp",
  "PageDown"
]);

/**
 * Elements the browser activates on Space. Pressing Space just after clicking
 * one of the transport buttons would otherwise press the button again *and*
 * toggle playback.
 */
const SPACE_ACTIVATED_INPUT_TYPES = new Set(["button", "checkbox", "radio", "reset", "submit"]);

export function describeReplayKeyEvent(event: KeyboardEvent): ReplayKeyContext {
  const target = event.target as HTMLElement | null;
  const tag = target?.tagName?.toLowerCase() ?? null;
  const type =
    tag === "input" ? ((target as HTMLInputElement).type || "text").toLowerCase() : null;

  return {
    key: event.key,
    shiftKey: event.shiftKey,
    modified: event.ctrlKey || event.metaKey || event.altKey,
    /* `isComposing` is the right question, but Chromium answers it late for the
       first keystroke of a composition and reports the legacy 229 sentinel
       instead. Both readings mean the user is mid-word. */
    composing: event.isComposing || event.keyCode === 229,
    typing:
      tag === "textarea" ||
      tag === "select" ||
      target?.isContentEditable === true ||
      (tag === "input" && !NON_TEXT_INPUT_TYPES.has(type ?? "text")),
    rangeFocused: type === "range",
    spaceActivates:
      tag === "button" ||
      tag === "summary" ||
      (tag === "a" && target?.hasAttribute("href") === true) ||
      (tag === "input" && SPACE_ACTIVATED_INPUT_TYPES.has(type ?? "text")),
    claimedKeys: (target?.getAttribute("aria-keyshortcuts") ?? "").split(/\s+/).filter(Boolean)
  };
}

export function resolveReplayAction(context: ReplayKeyContext): ReplayAction | null {
  if (context.composing) return null;
  if (context.modified) return null;
  if (context.typing) return null;
  /* The focused control said these keys are its own — the panel resizer
     declares `aria-keyshortcuts="ArrowLeft ArrowRight Home End"` for exactly
     this reason. Honouring the attribute is what lets a focused pane shadow a
     global binding without this module knowing the pane exists. */
  if (context.claimedKeys.some((claim) => claim.split("+").at(-1) === context.key)) return null;
  if (context.rangeFocused && RANGE_NATIVE_KEYS.has(context.key)) return null;
  if (context.spaceActivates && context.key === " ") return null;

  const key = context.key.length === 1 ? context.key.toLowerCase() : context.key;
  const binding = REPLAY_BINDINGS.find(
    (candidate) =>
      candidate.key === key && (candidate.shift === null || candidate.shift === context.shiftKey)
  );

  return binding?.action ?? null;
}

/**
 * Everything a replay key can touch. The run holds the transport state; the overlay flag
 * belongs to the app shell, which passes it in as an accessor.
 */
export interface ReplaySurface {
  run: AgentRunController;
  helpOpen: boolean;
}

let keyboardSeeks = 0;

/**
 * How many times a replay *key* has moved the playhead.
 *
 * The number itself means nothing; a change in it does. Keyboard and click both
 * end up calling the same `goTo()`, so a view that follows the cursor cannot ask
 * the run how the cursor got there — and this module is the one place that
 * knows, because every key that seeks reports through `noteKeyboardSeek`. A
 * follower remembers the count it last acted on and compares: a different
 * number means the movement it is reacting to came from a key. TranscriptPanel
 * reads it to scroll instantly for keys and keep the smooth follow-along for
 * clicks.
 *
 * Deliberately not a rune. Reading it must not subscribe the reader to it, or
 * the count would drive the effect instead of merely colouring what it does.
 */
export function keyboardSeekCount(): number {
  return keyboardSeeks;
}

/**
 * Report that a key, not a pointer, just moved the playhead from `from` to `to`.
 *
 * The cursor having moved is the whole test, rather than a list of which
 * gestures are the seeking ones. It answers three awkward cases for free: `→` at
 * the live edge and `←` at the first event move nothing, so they must not leave
 * a mark for the next click to trip over; `?` and `Esc` never touch the
 * playhead; and Space only counts on the press that rewinds a finished run, not
 * for the frames the playback timer then advances through, which are a
 * follow-along and stay smooth.
 *
 * Two callers, one rule. `applyReplayAction` below is the window handler's
 * path. The other is the scrubber, whose arrow keys `resolveReplayAction`
 * deliberately declines so the native range input keeps stepping itself: that
 * movement reaches the run through `onScrub`, never through an action, and
 * without this it would be the one keyboard seek the Logs pane scrolled
 * smoothly for.
 */
export function noteKeyboardSeek(from: number, to: number): void {
  if (from !== to) keyboardSeeks += 1;
}

/**
 * What a key does, in one place. The window handler calls this and so does
 * `check-replay-hotkeys.mjs`, which is the only way that check can compare what two bindings
 * *do* rather than what they are named.
 */
export function applyReplayAction(action: ReplayAction, surface: ReplaySurface): void {
  const { run } = surface;
  const before = run.viewIndex;

  switch (action) {
    case "stepBack":
      run.stepBack();
      break;
    case "stepForward":
      run.stepForward();
      break;
    case "previousTurn":
      run.previousTurn();
      break;
    case "nextTurn":
      run.nextTurn();
      break;
    case "first":
      run.goTo(0);
      break;
    case "last":
      run.goTo(run.total);
      break;
    case "togglePlay":
      if (run.playing) run.pause();
      else run.play();
      break;
    case "toggleHelp":
      surface.helpOpen = !surface.helpOpen;
      break;
    case "closeHelp":
      surface.helpOpen = false;
      break;
    default: {
      const unhandled: never = action;
      void unhandled;
    }
  }

  noteKeyboardSeek(before, run.viewIndex);
}
