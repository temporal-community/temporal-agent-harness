import {
  canBleed,
  defaultPaneParams,
  nearestPaneKind,
  PANE_META,
  paneIdFor,
  paneKindToken,
  parsePaneId,
  SPINE_SIZE,
  survivesSessionChange,
  type PaneKind
} from "$lib/panes/registry";
import { edgeShares, SPLIT_MIN, type PaneDropEdge } from "$lib/panes/paneDrop";

export type { PaneDropEdge };

export interface PaneDescriptor {
  kind: PaneKind;
  /** Entity key for non-singleton kinds, e.g. a tool id or turn number. */
  key?: string | null;
  params?: Record<string, string>;
}

export interface Pane {
  id: string;
  kind: PaneKind;
  params: Record<string, string>;
  collapsed: boolean;
  /** Pinned panes keep their place: no close button, skipped by "close others". */
  pinned: boolean;
  /** Column width in px; null means the kind default. */
  size: number | null;
  /**
   * When this pane was last brought forward. The frontmost tab in a slot is the
   * one with the highest number, which makes "showing" and "focused" the same
   * fact rather than two that can disagree — no active-tab flag to keep true as
   * panes are dropped in, pulled out, or closed.
   */
  touchedAt: number;
  /**
   * When the pane joined the column it is in. Set when it is opened and again
   * when it is dropped somewhere else, and by nothing else — `slotKey` rests on
   * that, so focusing, folding and reordering must all leave it alone.
   */
  joinedAt: number;
  /**
   * How the column this pane is in shows what it holds: stacked with everything
   * visible, or as tabs with one in front.
   *
   * A property of the column rather than of the pane, and held on every pane in
   * it, which is how `size` and `collapsed` are held too. A column is a run of
   * panes in an array, so it has nowhere of its own to keep anything; the rule
   * that keeps this honest is that only the methods which write it to the whole
   * run are allowed to touch it.
   */
  split: boolean;
  /**
   * Height in px inside a split column. Null is an equal share of it, and it is
   * meaningless in a column showing tabs.
   */
  share: number | null;
}

export interface PaneLocation {
  group: number;
  index: number;
}

/** A pane kind that was asked for by name and does not exist. */
export interface UnknownPaneToken {
  /** The kind as it was written, so the reader can find it in what they typed. */
  written: string;
  /** The kind it is a typo away from, when exactly one is that close. */
  meant: PaneKind | null;
}

export interface UnknownPaneReport {
  tokens: UnknownPaneToken[];
  /** Nothing asked for existed, so what is on screen is the default desk. */
  fellBack: boolean;
}

const QUERY_PANES = "p";
const QUERY_COLLAPSED = "c";
const QUERY_CURSOR = "i";
const QUERY_BLEED = "b";
/**
 * Shortest gap between two writes to the address bar.
 *
 * Four a second is far below the rate a browser objects to, and far above what
 * a reader can notice on a control they are still holding — the link only has
 * to be right by the time they let go and reach for it.
 */
const URL_WRITE_MS = 250;
/** Marks the frontmost tab of a column in the pane list, e.g. `chat|*logs`. */
const ACTIVE_MARK = "*";
/** Joins panes sharing a column: tabbed, and stacked as a split. */
const TAB_JOIN = "|";
const SPLIT_JOIN = "+";
/**
 * A literal `+` in a query string decodes to a space, so a hand-written or
 * hand-edited link arrives with spaces where its splits were. Both are read back
 * as the same thing rather than leaving the reader with a link that looks right
 * and silently loses a column.
 */
const PANE_JOIN = /[|+ ]/;

let touchCounter = 0;

function makePane(descriptor: PaneDescriptor): Pane {
  return {
    id: paneIdFor(descriptor.kind, descriptor.key),
    kind: descriptor.kind,
    params: descriptor.params ?? defaultPaneParams(descriptor.kind, descriptor.key),
    collapsed: false,
    pinned: false,
    size: null,
    touchedAt: ++touchCounter,
    joinedAt: ++touchCounter,
    split: false,
    share: null
  };
}

/**
 * Narration beside the thing it narrates.
 *
 * The desk used to open on the chat, which made the chat the way in: a newcomer
 * arrived at a prompt and had to know what to type before the console showed them
 * anything. The example is already running by the time this renders, so the first
 * two columns are the account of that run and the run itself. The chat is one
 * gesture away, and comes forward on its own when a tool needs approving —
 * approvals are answered nowhere else.
 */
function defaultGroups(): Pane[][] {
  return [[makePane({ kind: "graph" })], [makePane({ kind: "chat" })]];
}

/**
 * A slot's identity for keyed rendering: the id of the pane that has been in
 * this column the longest.
 *
 * A changed key means Svelte rebuilds the slot — the graph loses its zoom, the
 * log loses its scroll, the pane replays its open animation — so the key has to
 * survive everything that can happen to a column without the reader thinking it
 * moved. Three fair-looking answers do not. Position in the slot turns over when
 * a tab lands in front of another. The lowest pane id is order-proof but not
 * arrival-proof: a pane whose id sorted first would take the key over as it
 * joined. Least-recently-focused is arrival-proof but not focus-proof, because
 * bringing the eldest tab forward makes it the newest, so switching tabs
 * rebuilt the column that switching tabs was meant to leave alone. Age from when
 * a pane was opened fails the same way as the id: a pane can be older than the
 * column it joins.
 *
 * Seniority within the column is the fact that holds. Joining is always the
 * newest thing to have happened to a slot, so an arrival can never outrank what
 * it found; focusing, folding and reordering do not touch it. The key turns over
 * only when the founding pane leaves, which is the one case where the column the
 * reader was looking at really is gone.
 */
export function slotKey(group: Pane[]): string {
  let senior = group[0];
  for (const pane of group) if (pane.joinedAt < senior.joinedAt) senior = pane;
  return senior.id;
}

/** The frontmost tab: the pane in the slot brought forward most recently. */
export function activeIn(group: Pane[]): Pane {
  let front = group[0];
  for (const pane of group) if (pane.touchedAt > front.touchedAt) front = pane;
  return front;
}

/**
 * Whether a column shows everything it holds at once, stacked, rather than one
 * tab at a time. Read off the column rather than off a pane, because a single
 * pane's flag is only ever half of the answer.
 */
export function isSplit(group: Pane[]): boolean {
  return group.length > 1 && group[0].split;
}

export function createPaneStack() {
  return new PaneStack();
}

export class PaneStack {
  /**
   * Slots along the rail. Every slot is a column; a slot holding several panes
   * is a tabbed column, showing one at a time.
   *
   * Columns are the only arrangement. A rows mode existed and was removed: two
   * ways to lay out the same desk meant every pane, every gutter and every
   * keystroke had to answer "which way are we running today", and the answer
   * readers wanted was always the same one — columns, with the state flow given
   * the whole width when it is the only thing open.
   */
  groups = $state<Pane[][]>(defaultGroups());
  /* The narration, because it is the column that says what to read next. */
  focusedId = $state<string | null>("graph");
  /**
   * Replay index taken from a shared link. Held until enough of the timeline has
   * streamed in to honour it, then cleared.
   */
  pendingCursor = $state<number | null>(null);
  /**
   * Panes that were asked for by name and do not exist, held so the desk can say
   * so rather than open nothing.
   *
   * A pane list is written by hand as often as it is copied — into a README, into
   * a walkthrough step, into an agent's own notes — and `?p=log` is one letter
   * off `?p=logs`. Dropped in silence it opens nothing, which on screen is
   * indistinguishable from a pane that rendered blank, so the reader goes and
   * debugs the console instead of the link. Cleared when the reader dismisses it,
   * and by nothing else: it is a fact about the link they arrived on.
   */
  unknownPanes = $state<UnknownPaneReport | null>(null);
  /**
   * The pane filling the screen on its own, or null for the desk.
   *
   * Held as an id rather than a flag so that focusing another pane while bled
   * does not silently swap what is on screen underneath the reader. Everything
   * reads `bleedingPane` instead, which is what makes a stale id — a pane since
   * closed, or one carried in on a link that cannot bleed — mean the desk rather
   * than an empty screen.
   */
  bleeding = $state<string | null>(null);

  /* Plain fields, not `$state`: the throttle is bookkeeping about writes to the
     address bar, and nothing on screen reads it. */
  private urlWrittenAt = 0;
  private urlTimer: number | null = null;
  private urlPending: string | null = null;

  panes = $derived(this.groups.flat());
  expandedPanes = $derived(this.panes.filter((pane) => !pane.collapsed));
  collapsedPanes = $derived(this.panes.filter((pane) => pane.collapsed));
  bleedingPane = $derived.by(() => {
    const pane = this.bleeding
      ? (this.panes.find((item) => item.id === this.bleeding) ?? null)
      : null;
    return pane && canBleed(pane.kind) ? pane : null;
  });

  /** The session the desk on screen belongs to. */
  activeSessionId = $state<string | null>(null);
  /**
   * The desk each session was left with. Sessions in this harness are separate
   * pieces of work — a coding agent, an incident triage, a hello world — and
   * flipping between them is how they get compared, so a switch that costs the
   * reader the panes they had built is a switch they will avoid making.
   */
  #desks = new Map<string, Pane[][]>();

  locate(id: string): PaneLocation | null {
    for (let group = 0; group < this.groups.length; group++) {
      const index = this.groups[group].findIndex((pane) => pane.id === id);
      if (index !== -1) return { group, index };
    }
    return null;
  }

  indexOf(id: string): number {
    return this.panes.findIndex((pane) => pane.id === id);
  }

  has(id: string): boolean {
    return this.locate(id) !== null;
  }

  sizeOf(pane: Pane): number {
    return pane.size ?? PANE_META[pane.kind].defaultSize;
  }

  /**
   * A column's width. Tabs share it, so a width set by dragging the gutter holds
   * whichever tab is in front — the column is the thing being sized, not the
   * pane. Failing an explicit width, a flexible tab makes the whole column
   * flexible, and otherwise the frontmost tab's default decides.
   */
  sizeOfGroup(group: Pane[]): number {
    const explicit = group.find((pane) => pane.size != null);
    if (explicit) return this.sizeOf(explicit);
    const flexible = group.find((pane) => PANE_META[pane.kind].flexible);
    if (flexible) return this.sizeOf(flexible);
    return this.sizeOf(activeIn(group));
  }

  minOfGroup(group: Pane[]): number {
    return Math.max(...group.map((pane) => PANE_META[pane.kind].minSize));
  }

  /**
   * Point the desk at a session.
   *
   * A desk describes a run, so it cannot outlive the reader's attention to that
   * run. The one they are leaving is parked whole, and coming back to it later
   * restores it, panes, widths, folds and tabs alike. Going somewhere new keeps
   * the shape they have been working in — the columns, their order, their widths
   * — and drops what pointed into the run they left, which is the difference
   * between arriving in a session and arriving in a session littered with another
   * one's leftovers.
   *
   * The first session of a visit is not a switch. Whatever is on screen was built
   * for it, by a shared link or by the defaults, so it is recorded and left alone.
   */
  enterSession(sessionId: string): void {
    if (this.activeSessionId === sessionId) return;
    if (this.activeSessionId == null) {
      this.activeSessionId = sessionId;
      return;
    }

    this.#desks.set(this.activeSessionId, this.groups);
    this.groups = this.#desks.get(sessionId) ?? this.#carriedOver();
    this.activeSessionId = sessionId;
    /* The pane that had focus may have been one of the leftovers. */
    if (!this.focusedId || !this.has(this.focusedId)) {
      this.focusedId = (this.expandedPanes[0] ?? this.panes[0])?.id ?? null;
    }
  }

  /**
   * The desk for a session the reader has not opened yet: the one they are on,
   * minus every pane that named something in the run they are leaving.
   *
   * Copies, because the desk being left is parked as it stands — panes shared
   * between the two would mean resizing a column here quietly resized it there.
   */
  #carriedOver(): Pane[][] {
    const carried: Pane[][] = [];
    for (const group of this.groups) {
      const kept = group.filter((pane) => survivesSessionChange(pane.id));
      if (kept.length === 0) continue;
      const shared = kept.length > 1;
      carried.push(
        kept.map((pane) => ({
          ...pane,
          /* Params only ever point into the run being left, so a graph focused on
             one of its subagents comes back up to the session level. The field is
             the exception, for the same reason it survives the switch at all: its
             `focus` names a use case spanning every session, so blanking it threw
             away the level the reader chose for a reason that never applied to
             it — and left a change of session looking like it had reset the board
             rather than moved the mark on it. */
          params: pane.kind === "field" ? pane.params : {},
          joinedAt: ++touchCounter,
          /* A column that lost panes is no longer the arrangement it was. */
          split: shared && pane.split,
          share: shared ? pane.share : null
        }))
      );
    }
    /* Closing the last drill-in must not leave the reader facing an empty rail. */
    return carried.length > 0 ? carried : defaultGroups();
  }

  /**
   * Reveal a pane. Following a reference always leaves what it names immediately
   * right of the pane the reference was in, so a chain of drill-ins reads left to
   * right. Already-open panes are never duplicated. Nothing is ever discarded.
   */
  openPane(descriptor: PaneDescriptor, originId?: string | null): string {
    const id = paneIdFor(descriptor.kind, descriptor.key);
    const existing = this.locate(id);
    if (existing) {
      const pane = this.groups[existing.group][existing.index];
      pane.collapsed = false;
      if (descriptor.params) pane.params = descriptor.params;
      /* One rule has to cover a pane that happens to be open already, or the
         same click lands in a different place depending on the desk. It comes to
         the origin, because a reader who follows a reference from the chat and
         finds the answer four columns away has been left to hunt for it — and
         panes accumulate, so "already open, somewhere off to the right" is the
         common case rather than the exception.
         Two things are left where they are. A column the reader built by hand,
         by tabbing or splitting panes together, is not pulled apart by a click
         somewhere else; the named pane just comes to the front of it. And a pane
         already next door is not moved to where it already is, which would cost
         it its render key's neighbours for nothing. */
      const alone = this.groups[existing.group].length === 1;
      const origin = originId ? this.locate(originId) : null;
      if (originId && alone && origin && existing.group !== origin.group + 1) {
        this.placePane(id, originId, "after");
        return id;
      }
      this.focusPane(id);
      return id;
    }

    const pane = makePane(descriptor);
    const origin = originId ? this.locate(originId) : null;
    if (!origin) {
      this.groups = [...this.groups, [pane]];
    } else {
      const next = this.groups.map((group) => [...group]);
      next.splice(origin.group + 1, 0, [pane]);
      this.groups = next;
    }
    this.focusedId = id;
    return id;
  }

  closePane(id: string): void {
    const loc = this.locate(id);
    if (!loc || this.groups[loc.group][loc.index].pinned) return;
    const next = this.groups.map((group) => [...group]);
    next[loc.group] = next[loc.group].filter((pane) => pane.id !== id);
    const sibling = next[loc.group][Math.min(loc.index, next[loc.group].length - 1)];
    if (next[loc.group].length === 0) next.splice(loc.group, 1);
    this.groups = next;
    if (this.focusedId !== id) return;
    const fallback =
      sibling ?? this.groups[Math.min(loc.group, this.groups.length - 1)]?.[0];
    if (fallback) this.focusPane(fallback.id);
    else this.focusedId = null;
  }

  /**
   * Fold the column, not the tab. A half-folded tab strip — a spine for one tab
   * beside a full pane for its neighbour — would be the two-grids problem back
   * again inside a single column, so the whole slot goes down to one spine and
   * comes back with the same tab in front.
   */
  toggleCollapse(id: string): void {
    const loc = this.locate(id);
    if (!loc) return;
    const group = this.groups[loc.group];
    const folding = !group.every((pane) => pane.collapsed);
    for (const pane of group) pane.collapsed = folding;
    if (!folding) this.focusPane(id);
  }

  expand(id: string): void {
    const loc = this.locate(id);
    if (!loc) return;
    for (const pane of this.groups[loc.group]) pane.collapsed = false;
    this.focusPane(id);
  }

  togglePin(id: string): void {
    const pane = this.panes.find((item) => item.id === id);
    if (pane) pane.pinned = !pane.pinned;
  }

  /**
   * Give one canvas the whole screen, or hand it back to the desk.
   *
   * Bleeding is not a fold of every other pane: the desk keeps its columns, its
   * widths and its tabs, and the panes it is hiding stay mounted, so coming back
   * is the arrangement the reader left rather than one rebuilt around them.
   * Folding the pane also brings it forward, because a canvas hidden behind a
   * tab cannot be the thing filling the screen.
   */
  toggleBleed(id: string | null = this.focusedId): void {
    if (!id) return;
    const pane = this.panes.find((item) => item.id === id);
    if (!pane || !canBleed(pane.kind)) return;
    if (this.bleeding === id) {
      this.bleeding = null;
      return;
    }
    this.bleeding = id;
    this.expand(id);
  }

  exitBleed(): void {
    this.bleeding = null;
  }

  /** Per-pane params: graph focus, log scope, and anything else pane-local. */
  setParam(id: string, key: string, value: string | null): void {
    const pane = this.panes.find((item) => item.id === id);
    if (!pane) return;
    const next = { ...pane.params };
    if (value == null) delete next[key];
    else next[key] = value;
    pane.params = next;
  }

  /**
   * The column's width, written to every tab in it so that pulling one out keeps
   * the width the reader set. A null returns it to automatic: the kind's default,
   * or the leftover rail space if the kind is flexible, which is what dragging
   * the gutter took it off in the first place.
   */
  setGroupSize(id: string, size: number | null): void {
    const loc = this.locate(id);
    if (!loc) return;
    const group = this.groups[loc.group];
    const min = this.minOfGroup(group);
    const next = size == null ? null : Math.max(min, Math.round(size));
    for (const pane of group) pane.size = next;
  }

  /**
   * Land a dragged pane. `before`/`after` give it a column of its own on that
   * side of the target; `above`/`below` split the target's column so both stay on
   * screen; `tab` puts it in the target's column, in front.
   *
   * The gesture names the arrangement, so dropping on an edge of a column that
   * was showing tabs stacks the whole column, and dropping in the middle of a
   * split one turns it into tabs. Anything subtler would mean the same drop doing
   * different things depending on state the reader cannot see mid-drag.
   *
   * Committed on drop rather than followed live. Reordering under the pointer
   * meant the dragged pane kept arriving where the pointer was, so by the time
   * you had aimed at a neighbour the neighbour had moved and the pane under you
   * was your own — which is why dropping one pane onto another stopped working
   * at all. The rail now holds still and shows where the pane will go.
   */
  placePane(id: string, targetId: string, edge: PaneDropEdge): void {
    const from = this.locate(id);
    if (!from) return;
    /* Alone in its column, a pane is already where these would put it. */
    if (id === targetId && this.groups[from.group].length === 1) return;

    const next = this.groups.map((group) => [...group]);
    const [moving] = next[from.group].splice(from.index, 1);
    if (next[from.group].length === 0) next.splice(from.group, 1);
    /* Newly arrived wherever it lands, so it cannot outrank the column it joins
       and take that column's render key with it. */
    moving.joinedAt = ++touchCounter;

    const target = this.locateIn(next, targetId);
    if (!target) {
      this.groups = [...next, [moving]];
      this.focusPane(id);
      return;
    }

    if (edgeShares(edge)) {
      const host = next[target.group];
      /* The column keeps its width and the arrival adopts it, including the
         automatic width a flexible column has: a pane joining a column is not a
         reason for that column to change size, and carrying its old width in
         would quietly pin the state flow to the width of a log. */
      if (host[0]) moving.size = host[0].size;
      /* A drop has to show you what you dropped, so landing on a folded column
         unfolds it rather than swallowing the pane into a spine. Folding is a
         property of the column, so it is all of them or none. */
      moving.collapsed = false;
      for (const pane of host) pane.collapsed = false;
      if (edge === "tab") {
        host.push(moving);
      } else {
        host.splice(target.index + (edge === "below" ? 1 : 0), 0, moving);
      }
      /* Whatever the column was showing, the gesture decides what it shows now —
         and every pane in it has to agree, or the column has two answers. Shares
         are dropped so a new arrangement starts even rather than inheriting one
         pane's old height. */
      const split = edge !== "tab";
      for (const pane of host) {
        pane.split = split;
        pane.share = null;
      }
    } else {
      moving.split = false;
      moving.share = null;
      next.splice(target.group + (edge === "after" ? 1 : 0), 0, [moving]);
    }
    this.groups = next;
    this.focusPane(id);
  }

  /**
   * Flip a shared column between stacked and tabbed. The reader's way back from
   * either drop gesture, and the reason neither has to be the right guess: a
   * column of two logs read side by side one minute is two tabs the next.
   */
  setSplit(id: string, split: boolean): void {
    const loc = this.locate(id);
    if (!loc) return;
    const group = this.groups[loc.group];
    if (group.length < 2) return;
    for (const pane of group) {
      pane.split = split;
      pane.share = null;
    }
    /* Coming back to tabs, the pane whose control was clicked is the one the
       reader was looking at, so it is the one left in front. */
    if (!split) this.focusPane(id);
  }

  /** A pane's height inside a split column. Null shares the column evenly. */
  setShare(id: string, share: number | null): void {
    const pane = this.panes.find((item) => item.id === id);
    if (!pane) return;
    pane.share = share == null ? null : Math.max(SPLIT_MIN, Math.round(share));
  }

  /**
   * Move a pane by a signed number of columns. A tab leaves its column first,
   * landing beside it, which is also how the keyboard un-tabs a pane.
   */
  movePane(id: string, delta: number): void {
    const loc = this.locate(id);
    if (!loc) return;
    const group = this.groups[loc.group];
    if (group.length > 1) {
      this.placePane(id, group[0].id, delta < 0 ? "before" : "after");
      return;
    }
    const to = Math.min(Math.max(loc.group + delta, 0), this.groups.length - 1);
    if (to === loc.group) return;
    const next = this.groups.map((item) => [...item]);
    const [slot] = next.splice(loc.group, 1);
    next.splice(to, 0, slot);
    this.groups = next;
  }

  /** Reorder a pane within its tab strip. No-op at the ends. */
  movePaneAcross(id: string, delta: number): void {
    const loc = this.locate(id);
    if (!loc) return;
    const to = loc.index + delta;
    if (to < 0 || to >= this.groups[loc.group].length) return;
    const next = this.groups.map((group) => [...group]);
    const [pane] = next[loc.group].splice(loc.index, 1);
    next[loc.group].splice(to, 0, pane);
    this.groups = next;
  }

  /**
   * Focus a pane and bring it to the front of its column. One act, because a
   * focused pane hidden behind its own tab strip is a focus ring nobody can see.
   */
  focusPane(id: string): void {
    const loc = this.locate(id);
    if (!loc) return;
    this.groups[loc.group][loc.index].touchedAt = ++touchCounter;
    this.focusedId = id;
  }

  /** Step focus to the adjacent expanded pane, expanding a spine if needed. */
  focusAdjacent(delta: number): void {
    if (this.panes.length === 0) return;
    const current = this.focusedId ? this.indexOf(this.focusedId) : -1;
    const start = current === -1 ? (delta > 0 ? -1 : this.panes.length) : current;
    const next = Math.min(Math.max(start + delta, 0), this.panes.length - 1);
    const pane = this.panes[next];
    if (!pane) return;
    /* Unfolds the column, not the tab: half a folded column is a spine beside a
       pane, which is neither of the two things a column is allowed to be. */
    this.expand(pane.id);
  }

  /**
   * Walk to the neighbouring column, landing on whichever of its tabs is in
   * front. Landing on the tab at the same index would mean stepping sideways
   * could change what a column is showing, and walking the rail should not
   * rearrange the desk behind you.
   */
  focusAlongRail(delta: number): void {
    const loc = this.focusedId ? this.locate(this.focusedId) : null;
    if (!loc) {
      this.focusAdjacent(delta);
      return;
    }
    const next = loc.group + delta;
    if (next < 0 || next >= this.groups.length) return;
    const group = this.groups[next];
    for (const pane of group) pane.collapsed = false;
    this.focusPane(activeIn(group).id);
  }

  /** Walk the tab strip of the focused pane's column, bringing each forward. */
  focusAcross(delta: number): void {
    const loc = this.focusedId ? this.locate(this.focusedId) : null;
    if (!loc) return;
    const next = loc.index + delta;
    if (next < 0 || next >= this.groups[loc.group].length) return;
    this.expand(this.groups[loc.group][next].id);
  }

  /** Total folded thickness ahead of a slot, for sticky offsets. */
  stickyOffsetFor(id: string): number {
    let offset = 0;
    for (const group of this.groups) {
      if (group.some((pane) => pane.id === id)) break;
      if (group.length > 0 && group.every((pane) => pane.collapsed)) {
        offset += SPINE_SIZE;
      }
    }
    return offset;
  }

  // --- URL sync ------------------------------------------------------------

  /**
   * Put the desk and the cursor in the address bar, at a rate a browser will
   * actually accept.
   *
   * The URL is written from an effect that tracks the replay cursor, so one
   * drag along the scrub track asks for a write per pointer move — sixty-odd
   * for a single sweep. Chrome rate-limits same-document navigations and starts
   * dropping them on the floor with a console warning, which loses exactly the
   * shareable link the writes existed to keep.
   *
   * Only `replaceState` is held back. The query string is still built on every
   * call, and it has to be: reading `groups`, `collapsedPanes` and
   * `bleedingPane` here is what subscribes the caller's effect to them, and a
   * version that skipped straight out would quietly stop re-running when a pane
   * opened. Leading edge, so anything done after a quiet moment lands at once;
   * trailing edge, so the position a drag ends on is the one the link carries.
   */
  writeQuery(cursor: number): void {
    if (typeof window === "undefined") return;
    const next = this.queryFor(cursor);
    if (next === `${window.location.pathname}${window.location.search}`) return;

    const wait = URL_WRITE_MS - (Date.now() - this.urlWrittenAt);
    if (wait <= 0) {
      this.commitQuery(next);
      return;
    }
    this.urlPending = next;
    this.urlTimer ??= window.setTimeout(() => {
      this.urlTimer = null;
      if (this.urlPending) this.commitQuery(this.urlPending);
    }, wait);
  }

  private commitQuery(next: string): void {
    if (this.urlTimer != null) {
      clearTimeout(this.urlTimer);
      this.urlTimer = null;
    }
    this.urlPending = null;
    this.urlWrittenAt = Date.now();
    if (next !== `${window.location.pathname}${window.location.search}`) {
      window.history.replaceState(null, "", next);
    }
  }

  private queryFor(cursor: number): string {
    const params = new URLSearchParams(window.location.search);
    /* An empty desk carries no pane list; a bare ?p= would only be noise.
       Columns are separated by commas, and what joins a shared column says how it
       is arranged: `|` for tabs, `+` for a split. A tabbed column also stars the
       tab in front, so a shared link opens showing what the sender was looking at
       rather than the leftmost tab of every column. */
    if (this.groups.length > 0) {
      params.set(
        QUERY_PANES,
        this.groups
          .map((group) => {
            if (isSplit(group)) return group.map((pane) => pane.id).join(SPLIT_JOIN);
            const front = group.length > 1 ? activeIn(group).id : null;
            return group
              .map((pane) => (pane.id === front ? `${ACTIVE_MARK}${pane.id}` : pane.id))
              .join(TAB_JOIN);
          })
          .join(",")
      );
    } else {
      params.delete(QUERY_PANES);
    }

    const collapsed = this.collapsedPanes.map((pane) => pane.id);
    if (collapsed.length > 0) params.set(QUERY_COLLAPSED, collapsed.join(","));
    else params.delete(QUERY_COLLAPSED);

    /* Written from the resolved pane rather than the raw id, so a link never
       carries a bleed that would open on the desk anyway. */
    if (this.bleedingPane) params.set(QUERY_BLEED, this.bleedingPane.id);
    else params.delete(QUERY_BLEED);

    if (cursor > 0) params.set(QUERY_CURSOR, String(cursor));
    else params.delete(QUERY_CURSOR);

    const query = params.toString();
    return `${window.location.pathname}${query ? `?${query}` : ""}`;
  }

  hydrateFromQuery(): void {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);

    const raw = params.get(QUERY_PANES);
    if (raw) {
      const collapsed = new Set(
        (params.get(QUERY_COLLAPSED) ?? "").split(",").filter(Boolean)
      );
      const seen = new Set<string>();
      const restored: Pane[][] = [];
      const fronts: Pane[] = [];
      const unknown: string[] = [];
      for (const token of raw.split(",")) {
        if (!token) continue;
        /* Tabs win a token that somehow carries both joiners: a hand-edited link
           is the only way to write one, and one arrangement has to be picked. */
        const split = !token.includes(TAB_JOIN);
        const group: Pane[] = [];
        for (const entry of token.split(PANE_JOIN)) {
          const starred = entry.startsWith(ACTIVE_MARK);
          const id = starred ? entry.slice(ACTIVE_MARK.length) : entry;
          /* An empty entry or a repeat is a link written loosely, not a link
             asking for something that is not there. */
          if (!id || seen.has(id)) continue;
          const parsed = parsePaneId(id);
          if (!parsed) {
            unknown.push(id);
            continue;
          }
          seen.add(id);
          const pane = makePane({ kind: parsed.kind, key: parsed.key });
          pane.collapsed = collapsed.has(pane.id);
          pane.split = split;
          group.push(pane);
          if (starred) fronts.push(pane);
        }
        if (group.length > 0) restored.push(group);
      }
      if (restored.length > 0) {
        this.groups = restored;
        /* Stamped after every pane exists, so a starred tab outranks the tabs
           built after it rather than whichever happened to be made last. */
        for (const pane of fronts) pane.touchedAt = ++touchCounter;
        this.focusedId =
          restored.flat().find((pane) => !pane.collapsed)?.id ?? restored[0][0].id;
      }
      /* Nothing restored means every token was bad, so the desk on screen is the
         default one rather than the arrangement that was asked for. */
      if (unknown.length > 0) this.noteUnknownPanes(unknown, restored.length === 0);
    }

    /* Read after the panes, and left unchecked: a pane that is not on the desk,
       or one whose kind cannot bleed, resolves to no bleeding pane and the next
       write drops it from the link. */
    this.bleeding = params.get(QUERY_BLEED);

    const cursor = Number.parseInt(params.get(QUERY_CURSOR) ?? "", 10);
    if (Number.isFinite(cursor) && cursor > 0) this.pendingCursor = cursor;
  }

  /**
   * Take note of tokens that named no pane, so the desk can name them back.
   *
   * The tokens that were good are already open. A link is usually mostly right,
   * and refusing all of it over one bad token costs the reader the panes they
   * could have had; the one thing that must not happen is opening the guess
   * instead — a desk that quietly shows something else is a wrong answer with
   * nothing on screen to catch it.
   *
   * Reported by the kind rather than the whole token: a key names something in
   * the run, so `turn:99` is a turn that may not have happened and is the pane's
   * business, while `trun:1` is nothing the console has ever had.
   */
  noteUnknownPanes(ids: string[], fellBack = false): void {
    const tokens = [...(this.unknownPanes?.tokens ?? [])];
    for (const id of ids) {
      const written = paneKindToken(id);
      if (!written || tokens.some((token) => token.written === written)) continue;
      tokens.push({ written, meant: nearestPaneKind(written) });
    }
    if (tokens.length === 0) return;
    this.unknownPanes = {
      tokens,
      fellBack: fellBack || (this.unknownPanes?.fellBack ?? false)
    };
  }

  dismissUnknownPanes(): void {
    this.unknownPanes = null;
  }

  private locateIn(groups: Pane[][], id: string): PaneLocation | null {
    for (let group = 0; group < groups.length; group++) {
      const index = groups[group].findIndex((pane) => pane.id === id);
      if (index !== -1) return { group, index };
    }
    return null;
  }
}
