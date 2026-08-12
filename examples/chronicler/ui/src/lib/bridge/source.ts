export interface ChroniclerTranscriptSource {
  sessionId: string;
  campaignId: string;
  title: string;
  content: string;
  contentHash: string;
  folderBindingId: string;
}

interface SessionRecord {
  session_id: string;
  campaign_id: string;
  title: string;
  recorded_at: string;
  number: number;
  audio_file: string;
}

interface TranscriptSegment {
  speaker: string;
  start_s: number;
  end_s: number;
  text: string;
}

interface ChroniclerTranscript {
  session_id: string;
  model: string;
  duration_s: number;
  full_text: string;
  segments: TranscriptSegment[];
}

export type ChroniclerTranscriptOption =
  | { status: "selectable"; source: ChroniclerTranscriptSource }
  | { status: "unavailable"; sessionId: string; title: string; reason: string };

export interface ChroniclerTranscriptDiscovery {
  status: "ready";
  sessions: ChroniclerTranscriptOption[];
}

export interface ChroniclerSourceContext {
  root: FileSystemDirectoryHandle;
  folderBindingId: string;
}

export class ChroniclerSourceService {
  constructor(
    private readonly context: () => Promise<ChroniclerSourceContext>
  ) {}

  async discover(): Promise<ChroniclerTranscriptDiscovery> {
    const context = await this.context();
    return discoverChroniclerTranscriptSources(
      context.root,
      context.folderBindingId
    );
  }

  async read(sessionId: string): Promise<ChroniclerTranscriptSource> {
    const context = await this.context();
    return readChroniclerTranscriptSource(
      context.root,
      context.folderBindingId,
      sessionId
    );
  }

  async isCurrent(source: ChroniclerTranscriptSource): Promise<boolean> {
    const context = await this.context();
    return isChroniclerTranscriptSourceCurrent(
      context.root,
      context.folderBindingId,
      source
    );
  }
}

async function readFile(
  root: FileSystemDirectoryHandle,
  path: string
): Promise<string> {
  const parts = path.split("/");
  const name = parts.pop();
  if (!name) throw new TypeError("a file path is required");
  let directory = root;
  for (const part of parts) directory = await directory.getDirectoryHandle(part);
  return (await (await directory.getFileHandle(name)).getFile()).text();
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}

function recordValue(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function nonEmptyString(
  value: Record<string, unknown>,
  key: string,
  label: string
): string {
  const candidate = value[key];
  if (typeof candidate !== "string" || !candidate.trim()) {
    throw new TypeError(`${label} ${key} must be a non-empty string`);
  }
  return candidate;
}

function finiteNonNegativeNumber(
  value: Record<string, unknown>,
  key: string,
  label: string
): number {
  const candidate = value[key];
  if (typeof candidate !== "number" || !Number.isFinite(candidate) || candidate < 0) {
    throw new TypeError(`${label} ${key} must be a non-negative number`);
  }
  return candidate;
}

function validateSessionRecord(value: unknown): SessionRecord {
  const record = recordValue(value, "session");
  const number = finiteNonNegativeNumber(record, "number", "session");
  if (!Number.isSafeInteger(number)) {
    throw new TypeError("session number must be a safe integer");
  }
  return {
    session_id: nonEmptyString(record, "session_id", "session"),
    campaign_id: nonEmptyString(record, "campaign_id", "session"),
    title: nonEmptyString(record, "title", "session"),
    recorded_at: nonEmptyString(record, "recorded_at", "session"),
    number,
    audio_file: nonEmptyString(record, "audio_file", "session")
  };
}

function validateTranscript(value: unknown, sessionId: string): ChroniclerTranscript {
  const record = recordValue(value, "transcript");
  const transcriptSessionId = nonEmptyString(record, "session_id", "transcript");
  if (transcriptSessionId !== sessionId) {
    throw new TypeError("transcript session_id must match the selected session");
  }
  const rawSegments = record.segments;
  if (!Array.isArray(rawSegments)) {
    throw new TypeError("transcript segments must be an array");
  }
  const segments = rawSegments.map((value, index) => {
    const segment = recordValue(value, `transcript segment ${index}`);
    const start = finiteNonNegativeNumber(segment, "start_s", `transcript segment ${index}`);
    const end = finiteNonNegativeNumber(segment, "end_s", `transcript segment ${index}`);
    if (end < start) {
      throw new TypeError(`transcript segment ${index} end_s must not precede start_s`);
    }
    return {
      speaker: nonEmptyString(segment, "speaker", `transcript segment ${index}`),
      start_s: start,
      end_s: end,
      text: nonEmptyString(segment, "text", `transcript segment ${index}`)
    };
  });
  return {
    session_id: transcriptSessionId,
    model: nonEmptyString(record, "model", "transcript"),
    duration_s: finiteNonNegativeNumber(record, "duration_s", "transcript"),
    full_text: nonEmptyString(record, "full_text", "transcript"),
    segments
  };
}

async function readChroniclerTranscriptSource(
  root: FileSystemDirectoryHandle,
  folderBindingId: string,
  sessionId: string
): Promise<ChroniclerTranscriptSource> {
  const registry = recordValue(
    JSON.parse(await readFile(root, "sessions.json")),
    "sessions.json"
  );
  if (!Array.isArray(registry.sessions)) {
    throw new TypeError("sessions.json must contain a sessions array");
  }
  const rawSession = registry.sessions.find((candidate) =>
    typeof candidate === "object"
    && candidate !== null
    && "session_id" in candidate
    && candidate.session_id === sessionId
  );
  if (!rawSession) throw new Error(`unknown Chronicler session ${JSON.stringify(sessionId)}`);
  const session = validateSessionRecord(rawSession);
  return transcriptSourceFromSession(root, folderBindingId, session);
}

async function transcriptSourceFromSession(
  root: FileSystemDirectoryHandle,
  folderBindingId: string,
  session: SessionRecord
): Promise<ChroniclerTranscriptSource> {
  const transcript = validateTranscript(
    JSON.parse(await readFile(root, `transcripts/${session.session_id}.json`)),
    session.session_id
  );
  return {
    sessionId: session.session_id,
    campaignId: session.campaign_id,
    title: session.title,
    content: transcript.full_text,
    contentHash: await sha256(transcript.full_text),
    folderBindingId
  };
}

async function discoverChroniclerTranscriptSources(
  root: FileSystemDirectoryHandle,
  folderBindingId: string
): Promise<ChroniclerTranscriptDiscovery> {
  const registry = recordValue(
    JSON.parse(await readFile(root, "sessions.json")),
    "sessions.json"
  );
  if (!Array.isArray(registry.sessions)) {
    throw new TypeError("sessions.json must contain a sessions array");
  }
  const sessions: ChroniclerTranscriptOption[] = [];
  for (const [index, value] of registry.sessions.entries()) {
    try {
      const session = validateSessionRecord(value);
      sessions.push({
        status: "selectable",
        source: await transcriptSourceFromSession(
          root,
          folderBindingId,
          session
        )
      });
    } catch (error) {
      const raw = typeof value === "object" && value !== null
        ? value as Record<string, unknown>
        : {};
      sessions.push({
        status: "unavailable",
        sessionId: typeof raw.session_id === "string" ? raw.session_id : `session-${index + 1}`,
        title: typeof raw.title === "string" ? raw.title : "Invalid session",
        reason: error instanceof Error ? error.message : String(error)
      });
    }
  }
  return { status: "ready", sessions };
}

async function isChroniclerTranscriptSourceCurrent(
  root: FileSystemDirectoryHandle,
  activeFolderBindingId: string,
  source: ChroniclerTranscriptSource
): Promise<boolean> {
  if (source.folderBindingId !== activeFolderBindingId) return false;
  try {
    const current = await readChroniclerTranscriptSource(
      root,
      activeFolderBindingId,
      source.sessionId
    );
    return current.contentHash === source.contentHash;
  } catch {
    return false;
  }
}
