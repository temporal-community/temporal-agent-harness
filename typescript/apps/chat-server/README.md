# chat-server

An **optional** front end that puts your harness agents on Slack or Discord.

Nothing about the harness depends on this directory. The Python web app is the primary
surface; it runs, builds and packages without ever entering here, and this process is one you
start only when you want a chat platform in the loop. It needs Node — no Python, no pnpm.

```
Slack ───socket mode──┐
                      ├─> chat-server ──HTTP──> web/app.py ──Temporal──> agent workflow
Discord ──gateway─────┘
```

Both platforms run over a socket the bot opens **outbound** — Slack calls it Socket Mode,
Discord calls it the Gateway — so neither needs a public URL or a tunnel to try locally.

**Slack is the fuller experience**: it is the only platform here where modals work, so a typed
handler opens a real form instead of asking you to type `field=value`. Discord works well too,
and is a little quicker to set up. Both are documented below; do whichever you need, or both.

---

# Step 1 — run the harness

This server is a client of the harness API, so that has to be up before any of it means
anything. From the **repo root**, four terminals:

```bash
cp .env.example .env.local     # repo root — set GEMINI_API_KEY
```

```bash
just temporal           # 1. Temporal dev server
just session-manager    # 2. session manager worker
just server             # 3. harness API on :8000
just worker-monty       # 4. the agent itself
```

`monty-chat` is the example worth pointing at first: it is the one with a real handler
surface — `ask` (free text), `set_model`, and `set_approval_policy` (an enum) — so you can see
typed handlers projected onto the platform rather than just chat.

Confirm before continuing:

```bash
curl -s localhost:8000/api/agents | jq -r '.agents[].key'   # should list monty-chat
```

# Step 2 — create the bot

Do whichever platform you want; they are independent, and both can run at once.

## Slack

### 1. Create the app

At <https://api.slack.com/apps> → **Create New App** → **From scratch**. Name it, pick your
workspace.

### 2. Add bot scopes

**OAuth & Permissions → Scopes → Bot Token Scopes.** Add:

| scope | why |
|---|---|
| `app_mentions:read` | see messages that @-mention the bot |
| `chat:write` | post and edit replies (editing is how streaming works) |
| `channels:history` | read thread context in public channels |
| `groups:history` | same, for private channels — skip if you only use public ones |
| `im:history` | same, for DMs |
| `users:read` | resolve the display names shown on replies |
| `assistant:write` | render tool calls as native task cards (the spinner) — see below |

`commands` is only needed if you want a platform slash command. You don't: a leading `/` in an
ordinary message already routes to a handler.

`assistant:write` is what buys the good version of the tool-activity UI. Slack renders a tool
call as a task card only when the app has the **Agents & AI Apps** feature enabled *and* that
scope; without either, the adapter logs `Structured streaming chunk failed, falling back to
text-only streaming` and drops every card. Approvals still read correctly — the prompt carries
its own arguments — you just lose the live card that replaces the prompt once you approve.

### 3. Turn on Socket Mode

**Socket Mode → Enable Socket Mode.** Slack will prompt you to create an app-level token —
give it the `connections:write` scope. Copy the `xapp-…` token; that is `SLACK_APP_TOKEN`.

You can also create it under **Basic Information → App-Level Tokens**.

### 4. Subscribe to events

**Event Subscriptions → Enable Events.** With Socket Mode on, Slack does *not* ask for a
Request URL. Under **Subscribe to bot events**, add:

| event | why |
|---|---|
| `app_mention` | the bot is @-mentioned in a channel |
| `message.im` | someone DMs the bot |
| `message.channels` | follow-up messages in a thread the bot is in — optional |

Also switch on **Interactivity & Shortcuts**. With Socket Mode there is no URL to enter, but it
must be enabled or button clicks and modal submissions are never delivered — this is the Slack
equivalent of Discord's "Interactions Endpoint URL must be empty" trap, inverted.

### 5. Install and collect credentials

**Install App → Install to Workspace**, approve the scopes.

| value | where |
|---|---|
| `SLACK_BOT_TOKEN` | OAuth & Permissions → **Bot User OAuth Token** (`xoxb-…`) |
| `SLACK_APP_TOKEN` | the `xapp-…` token from Slack step 3 |
| `SLACK_SIGNING_SECRET` | Basic Information → **Signing Secret** |

`SLACK_SIGNING_SECRET` is unused in Socket Mode — it verifies webhook requests — but set it
now so switching to a public URL later needs no extra trip to the console.

Put them in `.env.local` alongside `BOT_AGENT_ROUTES`, then invite the bot to a channel
(`/invite @YourBot`) and `just run`.

Expect `slack: connecting over Socket Mode (no public URL needed)`.

### Running Slack on a public URL instead

Leave `SLACK_APP_TOKEN` blank (or set `SLACK_SOCKET_MODE=0`). Then Slack needs a public HTTPS
endpoint, and **both** of these point at `https://<your-host>/api/slack`:

- Event Subscriptions → **Request URL**
- Interactivity & Shortcuts → **Request URL**

Slack verifies the Request URL immediately, so `just run` must already be reachable when you
save it. `SLACK_SIGNING_SECRET` becomes load-bearing here.

## Discord

At <https://discord.com/developers/applications>, open (or create) your app.

### 1. Turn on the message content intent

**Bot → Privileged Gateway Intents → MESSAGE CONTENT INTENT.**
Not optional. The adapter requests the `MessageContent` intent and Discord refuses the
Gateway connection outright without it — the symptom is a bot that connects and immediately
drops.

### 2. Get the bot token

**Bot → Reset Token**, copy it. That is `DISCORD_BOT_TOKEN`.

### 3. Get the application id and public key

**General Information** → copy **Application ID** and **Public Key**.
The public key is only used to verify webhook signatures, which Gateway mode never does — but
the adapter validates it at construction, so it still has to be set.

### 4. Leave the interactions endpoint empty

**General Information → Interactions Endpoint URL must be EMPTY.**
If it is set, Discord delivers slash commands and button clicks over HTTP to that URL instead
of over the Gateway, and this server never sees them. Messages still work, because those
arrive over the Gateway either way — only button clicks go missing.

### 5. Invite the bot

OAuth2 → URL Generator → scope `bot`; bot permissions **Send
Messages**, **Read Message History**, **Embed Links**. Open the generated URL and add the bot
to your server.

# Step 3 — configure and run

```bash
cd third-party-platforms/chat-server
cp .env.example .env.local
```

Fill in `BOT_AGENT_ROUTES=monty-chat` and the credentials for whichever platform you set
up. Every variable is annotated in `.env.example`; the rest can stay blank. Both platforms
can run side by side in one process — set both sets of credentials and each connects.

This file is separate from the harness's own `.env.local` on purpose — nothing here leaks into
the harness, and the harness never reads it.

```bash
just run
```

Expect a line naming the transport for each adapter you configured, then the connection
coming up:

```
slack: will connect over Socket Mode (no public URL needed)
chat-server on :3000 -> harness http://127.0.0.1:8000
adapters: slack | routes: {"routes":{},"fallback":{"agentKey":"monty-chat"}}
[chat-server] Slack socket mode connected
```

# Talking to the agent

Just talk to it. **No slash command registration is required** — the bot reads ordinary
messages, and a leading `/` addresses a specific handler.

| what to type | what should happen |
|---|---|
| `@bot hello` | first contact posts the agent's guide, then streams the reply |
| `@bot how is the deploy going` | goes to `DEFAULT_HANDLER` (`ask` by default) |
| `@bot /help` | lists the agent's handlers on demand |
| `@bot /set_approval_policy new_policy=allow_safe` | that handler runs — works mid-turn |
| `@bot /set_approval_policy new_policy=nonsense` | `must be one of: ...`, plus the usage template |

While a turn is running you get a **typing indicator**, and each tool call is shown as it
happens — whether or not it needed approval. On Slack that is a native task card per call,
updating in place through in-progress → complete, with the arguments under `details`. On
Discord it is a small subtext line per transition (`🔧 get_weather…` / `✅ get_weather`),
because streamed text cannot be revised once written.

## A tool approval is part of the timeline, not a comment on it

The thread should read as an accurate record of when the agent did what. So the prompt that
asks is a message in its own right, and once you answer it, **it gets out of the way**:

```
[ reply text · finished tool cards ]          the message ends here, at the gate
[ 🔐 Run `delete_file`?                       its own message — nothing is streaming
  { "path": "/etc/passwd" }
  Approve once | Always allow | Deny ]
        │
        ├── approved → that message is DELETED, and the call's own card
        │              appears in its place: ⏳ → ⚙️ → ✅ delete_file,
        │              followed by the rest of the reply
        │
        └── denied   → it becomes the verdict:
                       [ 🚫 Denied · delete_file · { "path": … } · by alice ]
```

An approved prompt is deleted because it has a successor. The card that replaces it starts
spinning the moment you click, not whenever the call reaches a worker — that gap is exactly
the part of the timeline worth explaining — and runs through to the result, so what is left on
screen is the work and when it happened. A denial has no successor: the call never runs and
emits no further event, so its prompt stays, rewritten as the record that it was asked for and
refused. See `settleApprovalPrompt` in `src/app.tsx`.

Prompts are settled by whichever path gets there first — the click, which is the only one that
knows *who* decided, or the turn's own `tool_approval_resolved`. The second path is what makes
a decision taken **somewhere else** land here too: approving in the harness UI, the cascade
behind an earlier "Always allow", and the auto-denial when a session closes all retire their
prompt instead of leaving live buttons on an answered question.

Three rules shape the messages around it, and each is load-bearing:

- **The message ends at the gate, and the wait happens with nothing open.** The wait is a
  human's and has no bound, while Slack expires a native stream held open across one — after
  which the adapter reopens it and *replays every still-open task card into a second message*,
  rendering one tool call twice. There is no typing indicator during that wait either: the
  agent is not working, it is waiting on a person.
- **The prompt carries the arguments itself.** It has to be its own message — buttons cannot be
  attached to a stream (`StreamOptions.stopBlocks` exists, but `post()` takes no options) — so
  it cannot lean on anything rendered above it. And it must not: on Slack the tool card is a
  *native* stream chunk that renders only with the agent feature, the `assistant:write` scope
  and a live native stream, and the adapter silently skips it otherwise. "Run `delete_file`?"
  with no arguments is a question nobody can answer.
- **An approved call's card lives inside one message.** Slack keys task cards by id *within* a
  message, so a call split across two messages renders as two cards. The card opens in the
  message that follows the deleted prompt and its phases stay there.

**A segment that has nothing to say posts nothing at all.** This is not an optimization:
finalizing an empty stream costs a real, blank message — Slack's adapter calls
`chat.startStream` then `chat.stopStream` with no content, and Chat SDK's post-and-edit
fallback posts a single space — and a gated turn produces two empty segments, one when the
reply pauses at the gate and one after the call finishes with nothing left to render. Those
were the two blank loading bubbles that used to bracket the buttons. `openStream` pulls the
first chunk before anything is posted, which is the only way to know: a generator reading a
live event stream cannot say in advance whether it will produce output.

A turn also rolls over into a new message once it accumulates enough tool output, because
platforms cap message size — Slack answers `msg_too_long`, which loses the whole reply rather
than degrading. That split waits for every in-flight call to finish: `shouldRollOver` treats an
open card as a hard veto.

The guide appears on first contact because a chat surface has no equivalent of the harness
UI's always-visible interface — without it, every typed handler would be invisible unless you
already knew its name.

`DEFAULT_HANDLER` / `DEFAULT_HANDLER_FIELD` decide where un-prefixed text goes. Most agents
spell it `ask(TextMessage)` with a `text` field, which is the default, but nothing in the
harness enforces either name.

# When it doesn't work

**First, read the startup log.** Each adapter announces the transport it will use, then
whether the connection came up — `Slack socket mode connected` / `Discord Gateway
connected`. Until that line appears the platform is delivering nothing, and the reason is
logged right where it failed.

Then set `LOG_LEVEL=debug` to trace individual events. Every inbound event logs as
`-> mention` / `-> slash` / `-> action` with its ids, and every failure as `<- ... FAILED`
with the reason — which is also posted back into the channel, so a broken bot says so
instead of going quiet.

| symptom | cause |
|---|---|
| an error posted AND a normal reply | two independent harness calls; the optional first-contact guide is no longer fatal, so this should not recur |
| bot goes silent, no `-> mention` log line | a message was routed away before the mention handler — check `isSubscribed` is still constant `false` |
| the guide reappears on every message | `isSessionLive` could not see the session — check the harness is up and `GET /api/workflow-status/<session>` returns `closed: false` |
| a handler succeeds but says nothing | it emits no `reply_delta`; its reply is `message_handler_end.output`, which is rendered only when nothing streamed |
| "No agent is configured for this bot." | `BOT_AGENT_ROUTES` is unset or names an unknown key |
| `Unknown agent key` | the key is not in `GET /api/agents`, or that worker is not running |
| both adapters dead, one credential bad | adapters initialize together; a bad token for one stops the others |
| `TemporalState.<method> ... needs a real store` | a genuinely durable key was reached — see **No state store** below and send the key |

## Slack

| symptom | cause |
|---|---|
| no `connecting over Socket Mode` line | `SLACK_APP_TOKEN` is blank or `SLACK_SOCKET_MODE=0` |
| `invalid_auth` at startup | the bot token is wrong, or the app was never installed (Slack step 5) |
| mentions ignored | missing `app_mentions:read`, or the bot is not in the channel — `/invite @YourBot` |
| replies never appear | missing `chat:write` |
| buttons and modals do nothing | Interactivity & Shortcuts is off (Slack step 4) |
| tool calls appear as text, with no task card | the app lacks the Agents & AI Apps feature or `assistant:write`; the log says `Structured streaming chunk failed` |
| an approved prompt stays on screen | `chat.delete` was refused — the card is rewritten as the verdict instead, and the log says which |

## Discord

| symptom | cause |
|---|---|
| "The application did not respond" | no `Discord Gateway connected` line — see above |
| connects, then `session ended after <n>ms` | a real connection failure; the message names the usual causes, and the loop backs off 1s→60s rather than hammering Discord |
| bot connects then drops | MESSAGE CONTENT INTENT is off (Discord step 1) |
| mentions work, buttons do nothing | an Interactions Endpoint URL is set (Discord step 4) |

# How it works

## Platforms differ, and the difference is visible

| | Slack | Discord |
|---|---|---|
| transport | Socket Mode, or webhook | Gateway, or webhook |
| setup | app scopes + Socket Mode | bot token + message intent |
| modals | yes | **no** — the adapter ships no `openModal` |
| typed handler entry | button → form, or `/<handler> field=value` | `/<handler> field=value` |
| slash commands | app manifest (optional, buys a modal trigger) | not needed at all |
| streaming | native | post-and-edit, throttled |
| tool activity | task cards, args under `details` | one subtext line per transition |
| typing indicator | yes | yes (re-asserted; expires after ~10s) |

Because the Discord adapter has no `openModal`, the guide renders a copy-pasteable text
template per handler instead of buttons, and `src/text-form.ts` parses what the user types
back into the same `Record<string, string>` a modal submission produces — so
`decodeModalValues` validates and restores types for both paths. There is one place where a
field's type is restored, not two.

Buttons are rendered only where a modal can actually open. A button that silently does nothing
is worse than no button.

The same split governs tool activity. Chat SDK's `StreamChunk` objects (`task_update`) are
rendered natively by Slack only; everywhere else the fallback renderer does
`accumulated += chunk`, so a chunk object would land in the message as `[object Object]`.
`src/activity.ts` therefore emits chunks where they render and markdown where they do not —
a hard constraint, not a style choice.

## An agent becomes a UI with no configuration

`GET /api/agent-interface/{session_id}` returns every `@agent.accepts` handler with its JSON
Schema. `src/schema-to-modal.ts` projects that onto a form and decodes the submission back
into the payload the schema describes.

Messages are sent with `submit_message` (`POST /api/messages`) and observed on a separate
`attach` stream, filtered to the dispatch's own `message_id`. That is the harness's documented
client contract, and following it is what makes **mid-turn handlers work**: `POST /api/chat`
streams the turn it opens and therefore rejects a `mid_turn: accept` message with
`joined_turn` the moment it joins a running turn — exactly when you want one, e.g. changing
the approval policy while the agent works. Filtering on `message_id` also means all three
dispositions (opened / joined / queued) render identically.

`POST /api/sessions` accepts a caller-chosen `session_id`, so the session is derived from the
thread id and created idempotently — this server keeps no external-id → session-id table.
Creating a session sends the agent nothing, so the interface is readable before anyone has
said a word, which is what lets `/help` work in a brand-new channel.

The agent workflow only ever receives **complete, structured messages**. Nothing is
accumulated here or there: the platform holds the draft until submit, and one whole message
goes out.

Limits worth knowing, all in `schema-to-modal.ts`:

- A modal is flat. Nested objects and arrays have no native element and degrade to a JSON
  text field, which the plan reports as `jsonFallbackReason`.
- Every submitted value arrives as a string, so types are restored on decode. Bad input comes
  back as per-field errors that keep the modal open.
- `Optional[T]` (pydantic's `anyOf: [T, null]`) and `$ref` into `$defs` are both resolved —
  without that, ordinary optional and enum fields would render as raw JSON.

`model_callable` is **not** used to decide what to show. It says whether the *model* may call a
handler and implies nothing about whether a person may, so every handler is offered.

## No state store

Chat SDK requires a `StateAdapter`, and its own options are Redis, Postgres or in-memory. We
use none of them: `src/state.ts` implements the interface with Temporal behind the one method
that is actually reachable. The two things the store exists for are already provided, better,
by the agent workflow:

| Chat SDK feature | Why we don't need the store |
|---|---|
| thread subscriptions | `isSubscribed` is constant `false` — it is a routing switch, not a liveness query (see below) |
| adapter metadata caches | Slack memoises `users.info` here; a bounded in-process TTL cache serves it, and a miss costs an API call, not correctness |
| per-thread lock | the workflow queues a thread's messages itself → `concurrency: {strategy: 'concurrent'}`, whose path makes no state call |
| message dedupe | `request_id` → `update_id` makes a redelivery a durable no-op → `deduplicate: false`, and the dedupe key always answers "not seen" (see below) |
| `callbackUrl` tokens | we set `Button.value` ourselves, so no 7-day token is stored |
| modal context | `{threadId, agentKey, handler}` rides in `privateMetadata`, and `chat.thread(id)` rebuilds the handle |
| external-id → session-id table | the harness lets the caller choose the session id, so the mapping is a pure function |

Not everything is refused. Adapters use the state adapter as a **cache** — Slack memoises
`users.info` per user plus a reverse name index — and those get a bounded in-process TTL
cache (5,000 entries, oldest evicted). A cache is per-process by nature: a cold start simply
refetches, and nothing depends on an entry surviving a restart or being visible to another
instance.

What still throws is the set where in-memory would be silently WRONG rather than merely cold:

- `chat:callback:` — button tokens read up to seven days later, possibly by another process.
  Serving them from memory passes in testing and fails after a redeploy. We avoid needing them
  by setting `Button.value` ourselves.
- locks and queues — cross-process coordination. An in-memory lock held by one instance is
  invisible to another, so it would not be a lock.

Two wrinkles found the hard way.

`isSubscribed` looks like "does this thread have a live agent session", and wiring it that way
silently breaks the bot. Chat SDK dispatch reads it as *"deliver to `onSubscribedMessage` and
stop"*, so answering `true` without registering that handler swallows every message before the
mention handler sees it. It would also be wrong on its merits: a Discord thread id for an
ordinary channel message IS the channel, so a subscription would route all channel chatter to
the agent. Mention-free continuation is worth having, but it has to be an explicit opt-in per
thread, not a side effect of the session existing.

 `deduplicate: false` is honoured by `Chat.processMessage`, but
the Discord adapter's Gateway path calls `Chat.handleIncomingMessage` directly, which hardcodes
`deduplicate = true`. So the dedupe key *is* written on every Gateway message, and
`setIfNotExists` answers "not seen before" for it rather than throwing. Never dropping is the
right answer, not a concession: a redelivery is already an exact, durable no-op one layer down,
where the platform's message id travels as `request_id` → `update_id`.

Turn cancellation (`active-turn:` / `abort-turn:`) also wants shared state. Discord never asks
for it — only Slack does, and only with `agentView` — and Chat SDK catches the failure and logs
a warning, so it degrades to "cancellation unavailable" rather than breaking a turn.

That leaves nothing at all on the inbound path: an inbound message touches no store and makes
no extra network call. The first-contact question is asked directly of the harness, where it
is actually needed. Every other method **throws**, so if a future `chat` release starts depending on one
it fails loudly in a test rather than degrading quietly in production. If you hit
`TemporalState.<method> was called`, that error names the exact path that turned out to need a
store.

---

# Recipes

| | |
|---|---|
| `just run` | start the server (installs deps on first use) |
| `just check` | type-check and run the tests — needs no credentials |
| `just install` | install Node dependencies |

Without `just`:

```bash
npm install && npm test
BOT_AGENT_ROUTES=monty-chat DISCORD_BOT_TOKEN=... npm run dev
```

# Known gaps

- **A prompt outlives a restart of THIS server.** Retiring a prompt needs the message it was
  posted as, which is held in memory for the life of the turn (`livePrompts` in
  `src/app.tsx`) — in-flight, per-process and purely cosmetic, since Temporal owns the
  decision itself. Restart mid-approval and the buttons stay on screen; clicking one still
  works and still settles that message, because the click carries its own message id.
- **A second gated call answered mid-stream keeps the stream open.** Parallel gated calls can
  resolve while an earlier one's output is still streaming, and that output has to be rendered
  where its events land — so the message stays open across the second wait. It degrades the
  way a long turn does (Slack rotates the segment), rather than the way the single-gate path,
  which never holds a stream across a wait, is designed to avoid.
