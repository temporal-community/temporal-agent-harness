export type PaneKind =
  | "chat"
  | "graph"
  | "decisions"
  | "logs"
  | "latency"
  | "timeline"
  | "usage"
  | "state"
  | "files"
  | "script";

export interface PaneMeta {
  /** Uppercase eyebrow shown in the pane header and on the collapsed spine. */
  kindLabel: string;
  /** CSS custom property supplying the pane's accent hue. */
  accent: string;
  /** Size along the stacking axis, in px. */
  defaultSize: number;
  minSize: number;
  /**
   * A viewport is a window onto data with no end — a transcript, a canvas, a
   * log — so it keeps a bounded size and scrolls itself whichever way the rail
   * is stacked. A document is a stack of sections that finishes. Nothing reads
   * this now that columns are the only arrangement; it said which panes grew to
   * their content in the rows mode that was removed.
   */
  content: "viewport" | "document";
  /** The column a first visit widens to fill the rail; nothing resizes it after that. */
  flexible?: boolean;
  /**
   * What the title this kind is described by actually names.
   *
   * "kind" means it restates the badge: "Replay log" under LOGS, "Tokens and
   * cost" under TOKENS. Two readings of one fact, and the header was spending a
   * row on the second one. "subject" means it names which of several things the
   * pane is about, and is the only thing telling four NODE panes apart.
   */
  titleNames: "kind" | "subject";
  /**
   * Only an agent built with it has anything to show: an approval policy, file
   * mounts, a script. The launcher lists these after the views every run has,
   * below a divider, so the list reads from what you always need to what you
   * might.
   */
  optIn?: boolean;
}

/*
 * Key order is the launcher's order, and the empty rail's: what every run needs
 * first, the opt-in kinds last. It is one fixed list rather than one per agent,
 * so a row never moves when the agent changes. Layouts keep their own order.
 */
export const PANE_META: Record<PaneKind, PaneMeta> = {
  chat: {
    kindLabel: "Agent chat",
    accent: "--accent",
    defaultSize: 404,
    minSize: 320,
    content: "viewport",
    /* Which subagent the composer is addressing; see chatPaneTitle. */
    titleNames: "subject"
  },
  logs: {
    kindLabel: "Logs",
    accent: "--accent",
    defaultSize: 440,
    minSize: 320,
    content: "viewport",
    titleNames: "kind"
  },
  graph: {
    kindLabel: "State flow",
    accent: "--accent",
    defaultSize: 760,
    minSize: 420,
    content: "viewport",
    flexible: true,
    /* "Session flow" under STATE FLOW. Focused on a layer it is titled after
       that layer instead — and the crumb bar in the canvas is already standing
       there naming it, which is the better place for it. */
    titleNames: "kind"
  },
  timeline: {
    kindLabel: "Timeline",
    accent: "--reasoning",
    defaultSize: 470,
    minSize: 320,
    content: "viewport",
    titleNames: "kind"
  },
  latency: {
    kindLabel: "Latency",
    accent: "--reasoning",
    defaultSize: 470,
    minSize: 320,
    content: "viewport",
    titleNames: "kind"
  },
  usage: {
    kindLabel: "Tokens",
    accent: "--model",
    defaultSize: 400,
    minSize: 300,
    content: "document",
    titleNames: "kind"
  },
  state: {
    kindLabel: "Agent state",
    /* --accent, not a hue of its own. The semantic hues name what an event IS —
       --model, --reasoning, --tool — and agent state is not an event kind; it is
       whatever the workflow author declared. Latency and tokens took the two
       hues that did fit; inventing a sixth for this one would be colour spent on
       nothing the reader can act on. */
    accent: "--accent",
    /* Wider than the log at rest: a state document is JSON, and a nested value
       wrapping at 320px costs more rows than the extra 100px costs columns. */
    defaultSize: 420,
    minSize: 320,
    /* A plan an agent keeps appending to has no end, so the pane holds its size
       and scrolls itself rather than growing the rail. */
    content: "viewport",
    titleNames: "kind"
  },
  decisions: {
    kindLabel: "Approval decisions",
    accent: "--accent",
    /* Sized like the log. The panel stacks the evaluation index above the
       policy tree until the reader widens it enough for both. */
    defaultSize: 440,
    minSize: 320,
    content: "viewport",
    titleNames: "kind",
    optIn: true
  },
  files: {
    kindLabel: "VFS File Mounts",
    /* --accent, like agent state: the mounts are state an author opted into, and --tool
       is kept for tool calls themselves. */
    accent: "--accent",
    defaultSize: 440,
    minSize: 320,
    /* A tree above a file's contents, each scrolling itself. */
    content: "viewport",
    titleNames: "kind",
    optIn: true
  },
  script: {
    kindLabel: "Script",
    accent: "--tool",
    defaultSize: 420,
    minSize: 320,
    content: "viewport",
    titleNames: "kind",
    optIn: true
  }
};

/**
 * Every kind there is: the vocabulary a written token is judged against, and
 * the set the rail offers a launcher for.
 *
 * Those were two lists until they were found to hold the same five names. The
 * launcher list was the views with no parent to drill in from — and there is
 * nothing else, so "root" had stopped distinguishing anything. Derived from
 * PANE_META rather than restated, so a new kind is in both by existing.
 */
export const PANE_KINDS = Object.keys(PANE_META) as PaneKind[];

/** A folded label. Wide enough for a sideways kind and a status pixel. */
export const SPINE_SIZE = 42;

/**
 * Kinds that exist at most once. Logs and latency are deliberately absent: they
 * can be opened unscoped ("logs") or scoped to a workflow ("logs:wf-123"), and
 * both may be on screen at the same time.
 */
const SINGLETON_KINDS = new Set<PaneKind>([
  "chat",
  "graph",
  "decisions",
  "timeline",
  "usage",
  "state",
  "files",
  "script"
]);

export function isSingletonKind(kind: PaneKind): boolean {
  return SINGLETON_KINDS.has(kind);
}

/**
 * Whether a pane still means anything once a different session is on screen.
 *
 * A root view does: "the logs" is the logs of whatever is running, so it follows
 * the reader from one session to the next. A keyed root view — `logs:wf-123`,
 * scoped to one workflow of the run it was opened in — is as run-bound as any
 * drill-in, and closing it is kinder than leaving it titled after a workflow
 * that is no longer on screen.
 */
export function survivesSessionChange(id: string): boolean {
  const parsed = parsePaneId(id);
  if (!parsed) return false;
  return parsed.key == null;
}

/**
 * Whether the pane's chrome still has to say its title, or the badge has
 * already said it.
 *
 * The answer is no when the title restates the kind the badge is showing.
 * Everything left over has a title that is the only thing telling it from the
 * pane beside it.
 *
 * Asked of the id rather than of the kind, because a kind-titled view acquires a
 * subject by being scoped to one: `logs` and `logs:wf-123` are both badged LOGS,
 * and which workflow is all that tells them apart.
 */
export function titleBelongsInHead(id: string): boolean {
  const parsed = parsePaneId(id);
  /* A pane the registry cannot place has nothing but its title to go on. */
  if (!parsed) return true;
  return PANE_META[parsed.kind].titleNames === "subject" || parsed.key != null;
}

/**
 * The chat pane's title: the subagent the composer is addressing, or the kind
 * itself — which the header drops — because the top bar names the session's agent.
 */
export function chatPaneTitle(
  targets: readonly { workflowId: string; role: string; label: string }[],
  targetId: string | null
): string {
  const target = targets.find((item) => item.workflowId === targetId);
  return target?.role === "subagent" ? target.label : PANE_META.chat.kindLabel;
}

export function paneIdFor(kind: PaneKind, key?: string | null): string {
  if (isSingletonKind(kind) || !key) return kind;
  return `${kind}:${key}`;
}

/**
 * The kind half of a written pane token — everything before the key, whether or
 * not it names a kind that exists. The only half the registry can judge: a key
 * is an id from the run, so `logs:wf-99` is a scope that may not exist, while
 * `logz` is not a pane at all.
 */
export function paneKindToken(id: string): string {
  const separator = id.indexOf(":");
  return separator === -1 ? id : id.slice(0, separator);
}

export function parsePaneId(id: string): { kind: PaneKind; key: string | null } | null {
  const kind = paneKindToken(id) as PaneKind;
  if (!(kind in PANE_META)) return null;
  const keyed = id.length > kind.length;
  return { kind, key: keyed ? id.slice(kind.length + 1) : null };
}

/**
 * The kind a rejected token was probably reaching for, or nothing.
 *
 * Only ever asked about a token the registry has already turned down, and the
 * answer is only ever shown to the reader as a guess — the desk never opens it.
 * Prefix only: a truncation (`log` for `logs`) is the common miss. A tie, or
 * anything that is not a unique prefix of exactly one kind, returns
 * nothing — the cost of no guess is that the reader reads the list of kinds,
 * and the cost of a wrong one is that they go and try it.
 */
export function nearestPaneKind(token: string): PaneKind | null {
  const written = token.toLowerCase();
  if (!written) return null;
  const prefixed = PANE_KINDS.filter((kind) => kind.startsWith(written));
  return prefixed.length === 1 ? prefixed[0] : null;
}
