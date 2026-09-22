/**
 * HTTP entry point plus, for Discord, a Gateway listener.
 *
 * Runs only when you want to reach your agents from a chat platform; the Python web app
 * remains the primary surface and is unaffected by this process.
 *
 * Adapters are enabled by whether their credentials are present, so a Discord-only or
 * Slack-only deployment needs no extra switch.
 */

import { createServer } from 'node:http';

import { createDiscordAdapter } from '@chat-adapter/discord';
import { createSlackAdapter } from '@chat-adapter/slack';
import { ConsoleLogger, type Adapter, type LogLevel } from 'chat';

import { createChatServer } from './app.tsx';
import { HarnessClient } from './harness.ts';
import { parseRoutes } from './routing.ts';

/**
 * Read an env var, treating an EMPTY value as absent.
 *
 * `.env.example` ships these keys blank for the user to fill in, and a sourced `.env.local`
 * therefore exports empty strings — which `??` would happily accept, leaving `new URL(path,
 * '')` to throw and `Number('')` to bind port 0. Anything read from that file goes through
 * here.
 */
const env = (key: string, fallback: string): string => {
  const value = process.env[key];
  return value === undefined || value.trim() === '' ? fallback : value.trim();
};

/**
 * How long one Gateway session runs before the listener disconnects and we reconnect.
 *
 * Capped by `setTimeout`, whose delay is a SIGNED 32-BIT INT (~24.85 days). Node does not
 * error on a larger value — it silently clamps it to 1 ms, so a "run for a year" duration
 * makes the listener connect and drop immediately, which looks exactly like a credentials
 * problem. Stay just under the limit and reconnect in a loop instead.
 */
const GATEWAY_SESSION_MS = 2_147_483_647;

/**
 * A session shorter than this did not end on schedule — it failed (bad token, Discord
 * unreachable, disallowed intents). Reconnecting immediately in that case is a tight loop
 * against Discord's API, so failures back off and only genuine scheduled endings reconnect at
 * once.
 */
const GATEWAY_FAILFAST_MS = 10_000;
const GATEWAY_BACKOFF_START_MS = 1_000;
const GATEWAY_BACKOFF_MAX_MS = 60_000;

/** Sleep that wakes early on abort, so Ctrl-C is never held up by a backoff. */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', done, { once: true });
    function done() {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    }
  });
}

const HARNESS_URL = env('HARNESS_URL', 'http://127.0.0.1:8000');
const PORT = Number(env('PORT', '3000'));

/**
 * `BOT_AGENT_ROUTES` maps a bot to the agent it speaks for: `"support-agent"` for the
 * single-bot case, or `"discord=support,slack=research"` when two bots address two agents.
 * The value is an `AgentDescriptor.key` from `GET /api/agents`.
 */
const routing = parseRoutes(env('BOT_AGENT_ROUTES', ''));

/**
 * `LOG_LEVEL=debug` turns on Chat SDK's own per-event tracing — which adapter received what,
 * which handler matched — alongside this server's. Worth reaching for first when a platform
 * event appears not to arrive at all.
 */
const logger = new ConsoleLogger(env('LOG_LEVEL', 'info') as LogLevel, 'chat-server');

const adapters: Record<string, Adapter> = {};
if (env('DISCORD_BOT_TOKEN', '')) adapters.discord = createDiscordAdapter({ logger });
if (env('SLACK_BOT_TOKEN', '')) {
  // `mode: 'socket'` makes the adapter expect an app-level token and an outbound WebSocket;
  // without SLACK_APP_TOKEN it stays on webhooks, which need a public URL.
  const socket = env('SLACK_APP_TOKEN', '') !== '' && env('SLACK_SOCKET_MODE', '1') !== '0';
  adapters.slack = createSlackAdapter({ logger, ...(socket ? { mode: 'socket' as const } : {}) });
}

if (Object.keys(adapters).length === 0) {
  console.error(
    'No adapter credentials found. Set DISCORD_BOT_TOKEN (+ DISCORD_PUBLIC_KEY, ' +
      'DISCORD_APPLICATION_ID) and/or SLACK_BOT_TOKEN.',
  );
  process.exit(1);
}

if (!routing.fallback && Object.keys(routing.routes).length === 0) {
  console.warn(
    'BOT_AGENT_ROUTES is not set — every message will answer "No agent is configured for ' +
      'this bot." Set it to an agent key from GET /api/agents (e.g. monty-chat).',
  );
}

const harness = new HarnessClient(HARNESS_URL);
/**
 * Where un-prefixed text goes. Most agents spell it `ask(TextMessage)` with a `text` field,
 * but nothing in the harness enforces either name — an agent whose entry point is
 * `chat(prompt=...)` sets DEFAULT_HANDLER=chat and DEFAULT_HANDLER_FIELD=prompt.
 */
const defaultHandler = {
  name: env('DEFAULT_HANDLER', 'ask'),
  field: env('DEFAULT_HANDLER_FIELD', 'text'),
};

const chat = createChatServer({ harness, adapters, routing, logger, defaultHandler });

/** Slack always needs a public webhook; Discord needs one only in non-Gateway mode. */
const server = createServer(async (req, res) => {
  const path = req.url?.split('?')[0] ?? '';
  const name = path === '/api/slack' ? 'slack' : path === '/api/discord' ? 'discord' : undefined;
  const webhook = name
    ? (chat.webhooks as Record<string, ((r: Request, o: object) => Promise<Response>) | undefined>)[
        name
      ]
    : undefined;

  if (!webhook) {
    res.writeHead(404).end();
    return;
  }

  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);

  const request = new Request(new URL(path, `http://${req.headers.host}`), {
    method: req.method,
    headers: req.headers as Record<string, string>,
    body: chunks.length > 0 ? Buffer.concat(chunks) : undefined,
  });

  // Both platforms want an ack within ~3 seconds while an agent turn takes far longer, so the
  // handler runs on past the response. `deduplicate: false` because the harness update's
  // idempotency key already makes a redelivery a no-op — durably, unlike a 10-minute TTL.
  const pending: Promise<unknown>[] = [];
  const response = await webhook(request, {
    waitUntil: (task: Promise<unknown>) => pending.push(task),
    deduplicate: false,
  });

  const headers: Record<string, string> = {};
  response.headers.forEach((value, key) => {
    headers[key] = value;
  });
  res.writeHead(response.status, headers);
  res.end(response.body ? Buffer.from(await response.arrayBuffer()) : undefined);

  await Promise.allSettled(pending);
});

/**
 * Registering a SIGINT handler REPLACES Node's default, which is to exit. The HTTP server
 * keeps the event loop alive on its own, so without closing it here Ctrl-C would abort the
 * Gateway loop and then hang forever — the process has to be told to leave.
 */
const shutdownSignal = new AbortController();
let shuttingDown = false;

function shutdown(signal: string): void {
  if (shuttingDown) {
    // A second Ctrl-C means "I am not waiting for graceful" — honour that.
    process.exit(1);
  }
  shuttingDown = true;
  logger.info(`${signal} received, shutting down`);
  shutdownSignal.abort();
  server.close(() => {
    void chat.shutdown().finally(() => process.exit(0));
  });
  // Backstop: never leave a terminal stuck because a socket refused to close.
  setTimeout(() => process.exit(0), 5_000).unref();
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

server.listen(PORT, () => {
  console.log(`chat-server on :${PORT} -> harness ${HARNESS_URL}`);
  console.log(`adapters: ${Object.keys(adapters).join(', ')} | routes: ${JSON.stringify(routing)}`);
  console.log(
    `plain text -> ${defaultHandler.name}(${defaultHandler.field}=...); ` +
      'a leading "/" addresses a handler; "/help" lists them',
  );
});

/**
 * Persistent-socket mode.
 *
 * Both platforms can push events over a socket the bot opens OUTBOUND, instead of receiving
 * webhooks on a public URL — Discord calls it the Gateway, Slack calls it Socket Mode. That is
 * what makes local testing possible with no tunnel, and the reason to prefer it while
 * developing. The two adapters expose the same shape, so one loop drives both.
 *
 * Set `DISCORD_GATEWAY=0` / `SLACK_SOCKET_MODE=0` to use the webhook routes instead, which is
 * what a deployment with a public URL does.
 */
interface SocketListener {
  (
    options: { waitUntil: (task: Promise<unknown>) => void; deduplicate?: boolean },
    durationMs?: number,
    abortSignal?: AbortSignal,
  ): Promise<Response>;
}

function runSocketLoop(name: string, start: SocketListener, signal: AbortSignal): void {
  void (async () => {
    let first = true;
    let backoffMs = GATEWAY_BACKOFF_START_MS;

    // Each pass is a full session that simply reached its scheduled end; a pass that ends
    // early is a failure and backs off. This is a reconnect loop, not a retry loop.
    while (!signal.aborted) {
      const startedAt = Date.now();

      // `waitUntil` is required, not optional: the listener hands its long-running promise to
      // it and returns immediately. Omit it and both adapters answer with a silent 500.
      const held: Promise<unknown>[] = [];
      const response = await start(
        { waitUntil: (task) => held.push(task), deduplicate: false },
        GATEWAY_SESSION_MS,
        signal,
      );

      // Every guard in both listeners returns a non-OK Response instead of throwing.
      if (!response.ok) {
        logger.error(`${name}: listener did not start — ${response.status} ${await response.text()}`);
        return;
      }
      if (first) {
        logger.info(`${name}: listener started; waiting for the connection to come up`);
        first = false;
      }

      await Promise.allSettled(held);
      if (signal.aborted) break;

      const ranForMs = Date.now() - startedAt;
      if (ranForMs < GATEWAY_FAILFAST_MS) {
        logger.warn(
          `${name}: session ended after ${ranForMs}ms — this is a failure, not a scheduled ` +
            `end. Retrying in ${backoffMs}ms. Check the credentials and the app's settings.`,
        );
        await sleep(backoffMs, signal);
        backoffMs = Math.min(backoffMs * 2, GATEWAY_BACKOFF_MAX_MS);
      } else {
        logger.info(`${name}: session ended on schedule, reconnecting`);
        backoffMs = GATEWAY_BACKOFF_START_MS;
      }
    }
    logger.info(`${name}: listener stopped`);
  })().catch((err: unknown) => logger.error(`${name}: listener failed`, { error: String(err) }));
}

void (async () => {
  const discord = adapters.discord as (Adapter & { startGatewayListener?: SocketListener }) | undefined;
  const slack = adapters.slack as (Adapter & { startSocketModeListener?: SocketListener }) | undefined;

  const wantDiscord = discord && env('DISCORD_GATEWAY', '1') !== '0';
  // Socket Mode needs an app-level token; without one, Slack can only be reached by webhook.
  const wantSlack =
    slack && env('SLACK_SOCKET_MODE', '1') !== '0' && env('SLACK_APP_TOKEN', '') !== '';

  // Announce the chosen transport BEFORE initializing, so a credentials failure still tells
  // you what it was trying to do. `initialize()` calls each platform's API, and a bad token
  // throws there — after which nothing below this point runs.
  if (wantDiscord) logger.info('discord: will connect over the Gateway (no public URL needed)');
  if (wantSlack) logger.info('slack: will connect over Socket Mode (no public URL needed)');
  else if (slack) logger.info('slack: webhook mode — point the app at <public-url>/api/slack');

  if (!wantDiscord && !wantSlack) return;

  // REQUIRED before any listener: `initialize()` sets each adapter's back-reference to the
  // Chat instance. Chat calls it itself when handling a webhook, and socket mode never handles
  // one — without this a listener bails with "Chat instance not initialized" and, because it
  // RETURNS that as a Response rather than throwing, does so in complete silence.
  await chat.initialize();

  if (wantDiscord) {
    if (typeof discord!.startGatewayListener !== 'function') {
      logger.error('discord: adapter has no Gateway listener; set DISCORD_GATEWAY=0.');
    } else {
      runSocketLoop('discord', discord!.startGatewayListener.bind(discord), shutdownSignal.signal);
    }
  }

  if (wantSlack) {
    if (typeof slack!.startSocketModeListener !== 'function') {
      logger.error('slack: adapter has no Socket Mode listener; set SLACK_SOCKET_MODE=0.');
    } else {
      runSocketLoop('slack', slack!.startSocketModeListener.bind(slack), shutdownSignal.signal);
    }
  }
})().catch((err: unknown) =>
  logger.error(
    `socket setup failed: ${String(err)} — adapters initialize together, so a bad credential ` +
      'for one stops the others too. Check every *_BOT_TOKEN you have set.',
    { error: String(err) },
  ),
);
