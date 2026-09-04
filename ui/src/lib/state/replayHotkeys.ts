/**
 * Every keyboard binding in the console, transport and panes alike.
 *
 * The table below is the only place a binding is written down: the window
 * handler matches against it and the help overlay renders from it, so a key
 * that works is a key that is documented.
 *
 * `resolveReplayAction` is deliberately DOM-free. Everything that decides
 * whether a key should act at all — is the user typing, is an IME mid-word, is
 * the scrubber focused — is expressed in a plain object, which is what makes
 * the guard testable in a script with no browser.
 *
 * The pane bindings — Alt+Arrows, Cmd/Ctrl+Shift+Arrows, F, Escape — used to be
 * an if-chain in App.svelte beside the call into this module. That asymmetry
 * was the defect, not a style preference: those keys appeared in neither the
 * help overlay nor check-replay-hotkeys.mjs, so Alt+Left quietly eating the
 * OS-standard word-jump inside the chat composer was a bug nothing in the repo
 * could have caught. They are rows here now, guarded by the same `typing` test
 * as everything else, and the check drives them through the same resolver.
 */

import type { AgentRunController } from "./agentRun.svelte";

export type ReplayAction =
  | "stepBack"
  | "stepForward"
  | "previousStep"
  | "nextStep"
  | "previousTurn"
  | "nextTurn"
  | "first"
  | "last"
  | "togglePlay"
  | "toggleHelp"
  | "escape"
  | "railFocusPrevious"
  | "railFocusNext"
  | "railFocusPreviousTab"
  | "railFocusNextTab"
  | "railMovePrevious"
  | "railMoveNext"
  | "railMovePreviousTab"
  | "railMoveNextTab"
  | "railToggleBleed";

/** Which surface a binding drives, and the heading it is filed under in help. */
export type ReplayScope = "replay" | "rail";

export interface ReplayBinding {
  action: ReplayAction;
  scope: ReplayScope;
  /** `KeyboardEvent.key` to match. Single characters are compared lowercased. */
  key: string;
  /** Required Shift state, or `null` when Shift takes no part in the match. */
  shift: boolean | null;
  /** Required Alt state. Absent means the chord is only Alt's if nothing else claims it. */
  alt?: boolean;
  /** Required Ctrl-or-Meta state — one field, because they are one chord on two platforms. */
  mod?: boolean;
  /** How the chord reads in the help overlay. */
  chord: string;
  label: string;
}

/**
 * No transport binding carries Ctrl, Meta or Alt. That is not an oversight:
 * those belong to the browser (find, reload, tab switching), and the cheapest
 * way not to fight it is to leave the whole modifier space alone.
 *
 * The pane bindings do carry them, and that is what keeps the two halves off
 * each other: the arrows are the only key both spell, and every pane row below
 * requires Alt or Ctrl/Meta+Shift, which no transport row will match.
 */
export const REPLAY_BINDINGS: readonly ReplayBinding[] = [
  { action: "stepBack", scope: "replay", key: "ArrowLeft", shift: false, chord: "←", label: "Previous event" },
  { action: "stepForward", scope: "replay", key: "ArrowRight", shift: false, chord: "→", label: "Next event" },
  /* The step-sized jump between the event and the turn. `,` and `.` because the
     arrows are spoken for three times over and these two are on every layout. */
  {
    action: "previousStep",
    scope: "replay",
    key: ",",
    shift: false,
    chord: ",",
    label: "Previous step — a model call, tool call or approval starting or ending"
  },
  {
    action: "nextStep",
    scope: "replay",
    key: ".",
    shift: false,
    chord: ".",
    label: "Next step — a model call, tool call or approval starting or ending"
  },
  {
    action: "previousTurn",
    scope: "replay",
    key: "ArrowLeft",
    shift: true,
    chord: "Shift ←",
    label: "Previous turn"
  },
  { action: "nextTurn", scope: "replay", key: "ArrowRight", shift: true, chord: "Shift →", label: "Next turn" },
  { action: "first", scope: "replay", key: "Home", shift: null, chord: "Home", label: "First event" },
  /* Landing on the last event *is* following the live edge: goTo() sets `following` from
     `viewIndex === total`, so there is one behaviour here and one row for it. */
  { action: "last", scope: "replay", key: "End", shift: null, chord: "End", label: "Latest event, and follow live" },
  { action: "togglePlay", scope: "replay", key: " ", shift: false, chord: "Space", label: "Play / pause" },
  { action: "toggleHelp", scope: "replay", key: "?", shift: null, chord: "?", label: "Show this list" },
  /* One row, two layers. The help sheet is the topmost surface and takes Escape
     while it is up; the bleeding pane takes it underneath. With neither on
     screen the resolver declines, so Escape stays the browser's. */
  {
    action: "escape",
    scope: "replay",
    key: "Escape",
    shift: null,
    chord: "Esc",
    label: "Close this list, or leave full screen"
  },

  { action: "railFocusPrevious", scope: "rail", key: "ArrowLeft", shift: false, alt: true, chord: "Alt ←", label: "Focus the pane to the left" },
  { action: "railFocusNext", scope: "rail", key: "ArrowRight", shift: false, alt: true, chord: "Alt →", label: "Focus the pane to the right" },
  { action: "railFocusPreviousTab", scope: "rail", key: "ArrowUp", shift: false, alt: true, chord: "Alt ↑", label: "Focus the pane above, or the previous tab" },
  { action: "railFocusNextTab", scope: "rail", key: "ArrowDown", shift: false, alt: true, chord: "Alt ↓", label: "Focus the pane below, or the next tab" },
  { action: "railMovePrevious", scope: "rail", key: "ArrowLeft", shift: true, mod: true, chord: "Cmd Shift ←", label: "Move the focused pane left" },
  { action: "railMoveNext", scope: "rail", key: "ArrowRight", shift: true, mod: true, chord: "Cmd Shift →", label: "Move the focused pane right" },
  { action: "railMovePreviousTab", scope: "rail", key: "ArrowUp", shift: true, mod: true, chord: "Cmd Shift ↑", label: "Move the focused pane up, or into the column before" },
  { action: "railMoveNextTab", scope: "rail", key: "ArrowDown", shift: true, mod: true, chord: "Cmd Shift ↓", label: "Move the focused pane down, or into the column after" },
  /* Unmodified, because Cmd+F is the browser's and Alt+F is a menu. */
  { action: "railToggleBleed", scope: "rail", key: "f", shift: false, chord: "F", label: "Full-screen the focused pane, and back" }
];

/**
 * A keyboard event reduced to the things a binding decision depends on.
 */
export interface ReplayKeyContext {
  key: string;
  shiftKey: boolean;
  altKey: boolean;
  /** Ctrl or Meta is down. One flag, because they are one chord on two platforms. */
  modKey: boolean;
  /** An IME is composing a character and these keystrokes are spelling it. */
  composing: boolean;
  /** Focus is in a text field, a select, or a contenteditable region. */
  typing: boolean;
  /** Focus is on something the browser activates with Space, such as a button. */
  spaceActivates: boolean;
  /**
   * Keys the focused element has declared it handles, read from
   * `aria-keyshortcuts`. A control that says which keys are its own gets them.
   */
  claimedKeys: readonly string[];
  /** The help sheet is up, so Escape has it to close. */
  helpOpen: boolean;
  /** A pane is full-screen, so Escape has it to leave. */
  bleeding: boolean;
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

/*
 * A focused range input gets no deference of its own, and wants none.
 *
 * The scrubber is a range, and the table already spells what its navigation
 * keys should do — `←` is one event either way, `Home` is the first event
 * either way. Where the table has a row, the row wins and the caller's
 * `preventDefault` cancels the native step, so the two never both land. Where
 * it has none — `↑`, `↓`, PageUp, PageDown — the resolver returns null and the
 * slider keeps the key. Free platform behaviour survives by not being bound,
 * which needs no list to maintain and cannot drift out of date against one.
 */

/**
 * Elements the browser activates on Space. Pressing Space just after clicking
 * one of the transport buttons would otherwise press the button again *and*
 * toggle playback.
 */
const SPACE_ACTIVATED_INPUT_TYPES = new Set(["button", "checkbox", "radio", "reset", "submit"]);

/** What is on screen that a key could act on, which the DOM cannot be asked. */
export interface ReplaySurfaceState {
  helpOpen: boolean;
  bleeding: boolean;
}

export function describeReplayKeyEvent(
  event: KeyboardEvent,
  surfaces: ReplaySurfaceState
): ReplayKeyContext {
  const target = event.target as HTMLElement | null;
  const tag = target?.tagName?.toLowerCase() ?? null;
  const type =
    tag === "input" ? ((target as HTMLInputElement).type || "text").toLowerCase() : null;

  return {
    key: event.key,
    shiftKey: event.shiftKey,
    altKey: event.altKey,
    modKey: event.ctrlKey || event.metaKey,
    helpOpen: surfaces.helpOpen,
    bleeding: surfaces.bleeding,
    /* `isComposing` is the right question, but Chromium answers it late for the
       first keystroke of a composition and reports the legacy 229 sentinel
       instead. Both readings mean the user is mid-word. */
    composing: event.isComposing || event.keyCode === 229,
    typing:
      tag === "textarea" ||
      tag === "select" ||
      target?.isContentEditable === true ||
      (tag === "input" && !NON_TEXT_INPUT_TYPES.has(type ?? "text")),
    spaceActivates:
      tag === "button" ||
      tag === "summary" ||
      (tag === "a" && target?.hasAttribute("href") === true) ||
      (tag === "input" && SPACE_ACTIVATED_INPUT_TYPES.has(type ?? "text")),
    claimedKeys: (target?.getAttribute("aria-keyshortcuts") ?? "").split(/\s+/).filter(Boolean)
  };
}

/**
 * Whether the surface a binding acts on is actually there.
 *
 * Only Escape has anything to say here, and it is the reason Escape is one row
 * rather than two: with no help sheet and no bleeding pane the key is nobody's,
 * so the resolver declines and the browser keeps it. Every other binding is
 * free to be a no-op at the far end — `→` at the live edge moves nothing and
 * that is still `→` doing its job.
 */
function isLive(binding: ReplayBinding, context: ReplayKeyContext): boolean {
  return binding.action !== "escape" || context.helpOpen || context.bleeding;
}

export function resolveReplayAction(context: ReplayKeyContext): ReplayAction | null {
  if (context.composing) return null;
  /* The one guard that covers every binding in the table, transport and pane
     alike. Inside a field, arrows and word-jump are the field's: Option+Left is
     the OS's "back one word" long before it is this console's "pane to the
     left". */
  if (context.typing) return null;

  /* The focused control said these keys are its own — the panel resizer
     declares `aria-keyshortcuts="ArrowLeft ArrowRight Home End"` for exactly
     this reason. Honouring the attribute is what lets a focused pane shadow a
     global binding without this module knowing the pane exists.

     A claim covers the bare chord only, because that is the only chord these
     controls actually handle: the resizer's own handler hands anything modified
     straight back, Shift included. Shadowing on the key alone and ignoring the
     modifiers was the scrubber bug — a focused slider ate Shift+Left, a chord
     it does not distinguish and the table reads as "previous turn", and quietly
     nudged one event instead. It would equally have stranded a reader on a
     focused gutter with no chord left to walk off it. */
  const bare = !context.altKey && !context.modKey && !context.shiftKey;
  if (bare) {
    if (context.claimedKeys.some((claim) => claim.split("+").at(-1) === context.key)) return null;
    if (context.spaceActivates && context.key === " ") return null;
  }

  const key = context.key.length === 1 ? context.key.toLowerCase() : context.key;
  const binding = REPLAY_BINDINGS.find(
    (candidate) =>
      candidate.key === key &&
      (candidate.shift === null || candidate.shift === context.shiftKey) &&
      (candidate.alt ?? false) === context.altKey &&
      (candidate.mod ?? false) === context.modKey &&
      isLive(candidate, context)
  );

  return binding?.action ?? null;
}

/** Along the rail's columns, or across the panes stacked inside one column. */
export type RailAxis = "along" | "across";

/**
 * The pane rail, reduced to the four things a key does to it.
 *
 * Deliberately not `PaneStack`. There are two stacks on screen — the rail and
 * the bottom drawer — and which one a key acts on is the app shell's question,
 * answered by where the reader last put their hands; this module only needs
 * somewhere to send the verb. It is also what lets the check drive these keys
 * against a stub rail instead of a live desk.
 */
export interface RailSurface {
  /** Walk focus one pane, and put DOM focus on where it landed. */
  focus(axis: RailAxis, delta: -1 | 1): void;
  /** Carry the focused pane one place the same two ways. */
  move(axis: RailAxis, delta: -1 | 1): void;
  toggleBleed(): void;
  exitBleed(): void;
}

/**
 * Everything a key can touch. The run holds the transport state; the overlay flag
 * and the rail belong to the app shell, which passes them in as accessors.
 */
export interface ReplaySurface {
  run: AgentRunController;
  helpOpen: boolean;
  rail: RailSurface;
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
  const { run, rail } = surface;
  const before = run.viewIndex;

  switch (action) {
    case "stepBack":
      run.stepBack();
      break;
    case "stepForward":
      run.stepForward();
      break;
    case "previousStep":
      run.previousStep();
      break;
    case "nextStep":
      run.nextStep();
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
    /* Topmost surface first: the sheet covers the bleeding pane, so it is what
       the reader is escaping from while it is up. The resolver has already
       established that one of the two is there. */
    case "escape":
      if (surface.helpOpen) surface.helpOpen = false;
      else rail.exitBleed();
      break;
    case "railFocusPrevious":
      rail.focus("along", -1);
      break;
    case "railFocusNext":
      rail.focus("along", 1);
      break;
    case "railFocusPreviousTab":
      rail.focus("across", -1);
      break;
    case "railFocusNextTab":
      rail.focus("across", 1);
      break;
    case "railMovePrevious":
      rail.move("along", -1);
      break;
    case "railMoveNext":
      rail.move("along", 1);
      break;
    case "railMovePreviousTab":
      rail.move("across", -1);
      break;
    case "railMoveNextTab":
      rail.move("across", 1);
      break;
    case "railToggleBleed":
      rail.toggleBleed();
      break;
    default: {
      const unhandled: never = action;
      void unhandled;
    }
  }

  noteKeyboardSeek(before, run.viewIndex);
}
