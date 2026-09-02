export type PaneKind =
  | "guide"
  | "chat"
  | "graph"
  | "field"
  | "logs"
  | "latency"
  | "usage"
  | "node"
  | "turn"
  | "tool"
  | "event"
  | "subagent"
  | "session"
  | "model";

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
   * every drill-in, and the guide on its walkthrough. The chrome then carries
   * only the kind and the controls, so the name is stated once rather than twice
   * within 20px, and the body's is the pane's one heading.
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
  guide: {
    kindLabel: "Guide",
    accent: "--accent",
    defaultSize: 440,
    minSize: 340,
    content: "document",
    /* The brief opens on the walkthrough's own title, so the chrome saying it
       again 20px above was the same duplication the drill-ins avoid. */
    headline: true,
    titleNames: "subject"
  },
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
  field: {
    kindLabel: "Field",
    accent: "--accent",
    /* Wider than the graph, and the widest default in the rail. The graph draws
       one session, where this draws every session there is — at the graph's 760
       the far side of a use case is off-screen before the reader has descended
       into anything, which defeats the one thing this view is for. */
    defaultSize: 880,
    minSize: 480,
    content: "viewport",
    flexible: true,
    /* Like the graph: the crumb bar inside the canvas already names the level
       the reader is standing on, and it is the better place for it. */
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
  },
  node: {
    kindLabel: "Node",
    accent: "--accent",
    defaultSize: 400,
    minSize: 300,
    content: "document",
    headline: true,
    titleNames: "subject"
  },
  turn: {
    kindLabel: "Turn",
    accent: "--warning",
    defaultSize: 420,
    minSize: 300,
    content: "document",
    headline: true,
    titleNames: "subject"
  },
  tool: {
    kindLabel: "Tool call",
    accent: "--queue",
    defaultSize: 440,
    minSize: 300,
    content: "document",
    headline: true,
    titleNames: "subject"
  },
  event: {
    kindLabel: "Event",
    accent: "--text-3",
    defaultSize: 440,
    minSize: 300,
    content: "document",
    headline: true,
    titleNames: "subject"
  },
  subagent: {
    kindLabel: "Subagent",
    accent: "--model",
    defaultSize: 640,
    minSize: 420,
    content: "viewport",
    flexible: true,
    /* A canvas, not a document, so nothing in it is the subagent's name. The
       chrome is the only place it gets said. */
    titleNames: "subject"
  },
  session: {
    kindLabel: "Session",
    accent: "--accent",
    defaultSize: 400,
    minSize: 300,
    content: "document",
    headline: true,
    titleNames: "subject"
  },
  model: {
    kindLabel: "Model",
    accent: "--model",
    defaultSize: 380,
    minSize: 300,
    content: "document",
    headline: true,
    titleNames: "subject"
  }
};

/** Every kind there is, which is the vocabulary a written token is judged against. */
const PANE_KINDS = Object.keys(PANE_META) as PaneKind[];

/** A folded label. Wide enough for a sideways kind and a status pixel. */
export const SPINE_SIZE = 42;

/**
 * Kinds that can take the whole screen on their own.
 *
 * A canvas is the only thing worth bleeding: it is laid out in two dimensions,
 * so every pixel it is given is read, and a session with a dozen tool calls in
 * it does not fit a column. A document handed the same width just grows a line
 * length nobody wants, which is why the guide and the log are not here.
 */
const BLEED_KINDS = new Set<PaneKind>(["graph", "subagent"]);

export function canBleed(kind: PaneKind): boolean {
  return BLEED_KINDS.has(kind);
}

/**
 * Views with no parent to drill in from, so they need a launcher of their own.
 * Everything else is reached by clicking the thing it describes.
 */
export const ROOT_KINDS: PaneKind[] = ["chat", "graph", "logs", "latency"];

/**
 * Kinds that exist at most once. Logs and latency are deliberately absent: they
 * can be opened unscoped ("logs") or scoped to a workflow ("logs:wf-123"), and
 * both may be on screen at the same time.
 *
 * The field is absent for the same reason, and it is the reason its level is a
 * key rather than a param: comparing two use cases side by side is the point of
 * a board that spans them, and only pane ids travel in a shared link — a level
 * held in a param would be lost the moment anybody sent the desk to someone else.
 */
const SINGLETON_KINDS = new Set<PaneKind>(["guide", "chat", "graph", "usage"]);

export function isSingletonKind(kind: PaneKind): boolean {
  return SINGLETON_KINDS.has(kind);
}

/**
 * Kinds whose key is the workflow they scope to. Only pane ids travel in a
 * shared link, so their scope has to be recoverable from the id alone.
 */
const WORKFLOW_KEYED_KINDS = new Set<PaneKind>([
  "subagent",
  "logs",
  "latency",
  "session"
]);

/**
 * Kinds that name something inside one run: a turn of it, a node of its graph, a
 * call it made, one of its subagents, a model it spent tokens on.
 */
const RUN_SCOPED_KINDS = new Set<PaneKind>([
  "node",
  "turn",
  "tool",
  "event",
  "subagent",
  "model"
]);

/**
 * Whether a pane still means anything once a different session is on screen.
 *
 * A root view does: "the logs" is the logs of whatever is running, so it follows
 * the reader from one session to the next. A drill-in does not, and the two ways
 * it can be wrong are both worse than closing it. The tombstone is the kinder
 * one — a node pane from an incident triage says the node is not in this run and
 * holds a whole column to say it. The other is silent: `turn:1` answers with the
 * new session's first turn, so the reader ends up reading one run under a
 * heading they opened from another.
 *
 * Session cards are the exception among keyed panes. They describe a session in
 * the list rather than the run on screen, which is as true after a switch as
 * before — and one of them is how the reader switched.
 */
export function survivesSessionChange(id: string): boolean {
  const parsed = parsePaneId(id);
  if (!parsed) return false;
  if (parsed.kind === "session") return true;
  /* The field is the one view that was never about the run on screen. Its key is
     a use case spanning many sessions, so switching between them is exactly the
     thing it is for rather than a reason to close it. */
  if (parsed.kind === "field") return true;
  if (RUN_SCOPED_KINDS.has(parsed.kind)) return false;
  /* A keyed root view — `logs:wf-123`, scoped to one workflow of the run it was
     opened in — is as run-bound as any drill-in. */
  return parsed.key == null;
}

/**
 * Whether the pane's chrome still has to say its title, or the badge and the
 * body between them have already said it.
 *
 * Two ways the answer is no, and they are different facts about the pane: the
 * title restates the kind the badge is already showing, or the body states the
 * subject at a size worth reading. Everything left over has a title that is the
 * only thing telling it from the pane beside it — four NODE panes, or a
 * SUBAGENT pane whose canvas never names the agent it is drawing.
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
  /* The field's key is the level it is standing on, so a pane restored from a
     link knows where it was without the param having travelled. */
  if (kind === "field") return { focus: key };
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
 * is an id from the run, so `turn:99` is a turn that may not have happened,
 * while `trun:1` is not a pane at all.
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
 * A tie returns nothing: the cost of no guess is that the reader reads the list
 * of kinds, and the cost of a wrong one is that they go and try it.
 */
export function nearestPaneKind(token: string): PaneKind | null {
  const written = token.toLowerCase();
  if (!written) return null;

  /* A truncation is the commonest miss — `log` for `logs`, `sess` for `session`
     — and edit distance ranks those no better than an unrelated kind of the
     same length, so prefixes are answered first. */
  const prefixed = PANE_KINDS.filter((kind) => kind.startsWith(written));
  if (prefixed.length === 1) return prefixed[0];

  let nearest: PaneKind | null = null;
  let best = Number.POSITIVE_INFINITY;
  let tied = false;
  for (const kind of PANE_KINDS) {
    const distance = editDistance(written, kind);
    if (distance < best) {
      nearest = kind;
      best = distance;
      tied = false;
    } else if (distance === best) {
      tied = true;
    }
  }
  /* Two edits is a fat-fingered word; on a short one it is a different word. */
  const limit = written.length <= 4 ? 1 : 2;
  return !tied && best <= limit ? nearest : null;
}

/**
 * Edit distance counting a swap of two neighbours as one edit rather than two,
 * because `trun` for `turn` is a single slip of two fingers and the commonest
 * typo there is. The words are a kind name long, so the whole matrix is free.
 */
function editDistance(from: string, to: string): number {
  const rows: number[][] = [];
  for (let i = 0; i <= from.length; i += 1) {
    rows.push(Array.from({ length: to.length + 1 }, (_, j) => (i === 0 ? j : 0)));
    rows[i][0] = i;
  }
  for (let i = 1; i <= from.length; i += 1) {
    for (let j = 1; j <= to.length; j += 1) {
      const same = from[i - 1] === to[j - 1];
      rows[i][j] = Math.min(
        rows[i - 1][j] + 1,
        rows[i][j - 1] + 1,
        rows[i - 1][j - 1] + (same ? 0 : 1)
      );
      if (i > 1 && j > 1 && from[i - 1] === to[j - 2] && from[i - 2] === to[j - 1]) {
        rows[i][j] = Math.min(rows[i][j], rows[i - 2][j - 2] + 1);
      }
    }
  }
  return rows[from.length][to.length];
}
