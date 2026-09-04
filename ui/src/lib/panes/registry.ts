export type PaneKind = "chat" | "graph" | "logs" | "latency" | "usage";

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
   * is stacked. A document is a stack of sections that finishes, so in rows
   * mode it grows to its content and leaves the rail as the only scroller.
   * Two scrollers in one axis is what makes a stacked pane swallow the wheel.
   */
  content: "viewport" | "document";
  /**
   * Flexible panes absorb leftover rail space, so collapsing the chat gives the
   * graph the full viewport instead of leaving a gap.
   */
  flexible?: boolean;
  /**
   * The pane opens on its subject's name, in the body, at a size worth reading —
   * every drill-in. The chrome then carries only the kind and the controls, so
   * the name is stated once rather than twice within 20px, and the body's is
   * the pane's one heading.
   */
  headline?: boolean;
  /**
   * What the title this kind is described by actually names.
   *
   * "kind" means it restates the badge: "Replay log" under LOGS, "Tokens and
   * cost" under TOKENS. Two readings of one fact, and the header was spending a
   * row on the second one. "subject" means it names which of several things the
   * pane is about, and is the only thing telling four NODE panes apart.
   *
   * A separate fact from `headline`, which says the body already states the
   * subject. Both are reasons the chrome need not, and they are not the same
   * reason — see `titleBelongsInHead`.
   */
  titleNames: "kind" | "subject";
}

export const PANE_META: Record<PaneKind, PaneMeta> = {
  chat: {
    kindLabel: "Agent chat",
    accent: "--accent",
    defaultSize: 404,
    minSize: 320,
    content: "viewport",
    /* Which agent is answering. The transcript below never says it. */
    titleNames: "subject"
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
  logs: {
    kindLabel: "Logs",
    accent: "--accent",
    defaultSize: 440,
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
  }
};

/** Every kind there is, which is the vocabulary a written token is judged against. */
const PANE_KINDS = Object.keys(PANE_META) as PaneKind[];

/** A folded label. Wide enough for a sideways kind and a status pixel. */
export const SPINE_SIZE = 42;

/**
 * Views with no parent to drill in from, so they need a launcher of their own.
 * Everything else is reached by clicking the thing it describes.
 */
export const ROOT_KINDS: PaneKind[] = ["chat", "graph", "logs", "latency", "usage"];

/**
 * Kinds that exist at most once. Logs and latency are deliberately absent: they
 * can be opened unscoped ("logs") or scoped to a workflow ("logs:wf-123"), and
 * both may be on screen at the same time.
 */
const SINGLETON_KINDS = new Set<PaneKind>(["chat", "graph", "usage"]);

export function isSingletonKind(kind: PaneKind): boolean {
  return SINGLETON_KINDS.has(kind);
}

/**
 * Kinds whose key is the workflow they scope to. Only pane ids travel in a
 * shared link, so their scope has to be recoverable from the id alone.
 */
const WORKFLOW_KEYED_KINDS = new Set<PaneKind>(["logs", "latency"]);

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
 * Whether the pane's chrome still has to say its title, or the badge and the
 * body between them have already said it.
 *
 * Two ways the answer is no, and they are different facts about the pane: the
 * title restates the kind the badge is already showing, or the body states the
 * subject at a size worth reading. Everything left over has a title that is the
 * only thing telling it from the pane beside it.
 *
 * Asked of the id rather than of the kind, because a kind-titled view acquires a
 * subject by being scoped to one: `logs` and `logs:wf-123` are both badged LOGS,
 * and which workflow is all that tells them apart.
 */
export function titleBelongsInHead(id: string): boolean {
  const parsed = parsePaneId(id);
  /* A pane the registry cannot place has nothing but its title to go on. */
  if (!parsed) return true;
  const meta = PANE_META[parsed.kind];
  if (meta.headline) return false;
  return meta.titleNames === "subject" || parsed.key != null;
}

export function defaultPaneParams(
  kind: PaneKind,
  key?: string | null
): Record<string, string> {
  if (!key) return {};
  if (!WORKFLOW_KEYED_KINDS.has(kind)) return {};
  return { workflowId: key };
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
 * anything that is not a unique prefix of one of the five kinds, returns
 * nothing — the cost of no guess is that the reader reads the list of kinds,
 * and the cost of a wrong one is that they go and try it.
 */
export function nearestPaneKind(token: string): PaneKind | null {
  const written = token.toLowerCase();
  if (!written) return null;
  const prefixed = PANE_KINDS.filter((kind) => kind.startsWith(written));
  return prefixed.length === 1 ? prefixed[0] : null;
}
