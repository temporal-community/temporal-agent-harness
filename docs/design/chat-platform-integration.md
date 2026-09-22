# Chat platforms: the bridge is thin, but it is a Node process

> Status: **assessment only — nothing here is implemented, and nothing here is approved.**
> The question asked was how hard it would be for the harness to natively support
> [Chat SDK](https://chat-sdk.dev/docs) so that a harness agent can be dropped onto Slack,
> Discord, Teams and friends with minimal setup.
>
> Short answer: the protocol work everyone expects is **not required**, the two harness-side
> endpoints that *are* missing are small and useful on their own, and the real cost is
> operational — you take on a Node deployment and a Redis/Postgres state store.
>
> Every factual claim about Chat SDK below was verified against the shipped `chat@4.41.0`
> tarball (`npm pack chat@4.41.0`) — its `dist/*.d.ts` and its bundled `docs/*.mdx` — not
> against the website. Where the shipped types and the shipped prose disagree, the types win
> and the disagreement is called out.

## Nothing here touches the rich event stream

The harness's SSE stream is the differentiator and it stays exactly as it is: unrestricted,
fully-typed, replayable from a durable offset, and the thing we want people building products
on. Chat-platform support is **additive reach for the lowest-effort case**, not a second
first-class surface and certainly not a migration of `web/app.py`. Nothing proposed below
changes an existing endpoint, an existing event, or an existing consumer.

## First, a name collision that will waste your afternoon

`chat-sdk.dev` used to host Vercel's Next.js chatbot **template**. That template has been
renamed and moved to `chatbot.ai-sdk.dev` (repo `vercel/chatbot`, formerly `vercel/ai-chatbot`).

`chat-sdk.dev` today documents a *different, newer* product: the `chat` npm package — a unified
bot framework with platform adapters. It is not the AI SDK, it is not an agent framework, and it
has no opinion about how you produce text.

Anyone who researches this from memory or from a stale search index will find the template and
conclude the task is "implement Vercel's AI SDK UI Message Stream protocol in FastAPI". That
conclusion is wrong, and it is wrong in an expensive direction — it invents a protocol
implementation we do not need.

- `chat@4.41.0`, MIT, `github.com/vercel/chat`. First Vercel release 2026-01-02.
- 15 official adapters: slack, teams, discord, telegram, whatsapp, gchat, github, linear,
  notion, gmail, instagram, messenger, twilio, x, web.
- Peer deps `ai`, `workflow`, `zod` are **all `optional: true`** (verified in `package.json`).
  You can use it with no AI SDK anywhere in the tree.
- It is a **webhook handler**, not a daemon: `bot.webhooks.slack(request, { waitUntil })` takes
  a web-standard `Request` and returns a `Response`. Vercel hosting is not required; Vercel
  Connect (managed OAuth) is optional convenience.

## The seam already exists

`AgentClient.attach(on_item=…)` and `send_message(on_item=…)`
(`temporal_agent_harness/harness/agent_client.py:538,401`) take an encoder callback. `_yield_item`
(`temporal_agent_harness/web/app.py:652`) is the **only** thing binding the harness event model to
the current SSE wire shape. A second consumer with a different encoder is a pure addition — it
does not fork the stream, gate it, or degrade it.

This is worth stating plainly because it is the reason the estimate is small: the extension point
we would need was designed in already.

## Why the bridge is thin: `thread.post()` is duck-typed

The load-bearing discovery. From `dist/types-994wmjht.d.ts:1940`:

```ts
type PostableMessage =
  | AdapterPostableMessage
  | AsyncIterable<string | StreamChunk | StreamEvent>
  | PostableObject;
```

and, in the doc comment immediately below it:

> Duck-typed stream event compatible with AI SDK's `fullStream` and with AG-UI protocol streams
> such as TanStack AI's `chat()`. […] All other event types (tool calls, tool results, run
> lifecycle, reasoning, etc.) are silently skipped.

The bundled `docs/streaming.mdx` puts it even more bluntly: *"Chat SDK accepts any
`AsyncIterable<string>` as a message."*

Three consequences, in order of how much they save us:

1. **We do not implement Vercel's AI SDK UI Message Stream protocol.** Not a subset of it, not a
   compatible dialect. The premise of the original question dissolves.
2. **The minimum viable bridge is `reply_delta.text` → `yield text`.** An async generator of
   plain strings is a valid Chat SDK message.
3. **The richer target is Chat SDK's own `StreamChunk` vocabulary**, not the AI SDK's. That
   vocabulary is small — three types — and maps onto harness events almost one-to-one.

### Mapping harness events onto `StreamChunk`

`StreamChunk` is `markdown_text` (`text`), `task_update`
(`id`, `title`, `status` ∈ `pending|in_progress|complete|error`, `details?`, `output?`), and
`plan_update` (`title`).

| Harness event (`AgentEventType`) | Chat SDK rendering |
|---|---|
| `reply_delta` | `{type:"markdown_text", text}` — or just the bare string |
| `tool_requested` / `tool_start` | `task_update` `status:"in_progress"`, `id=tool_id`, `title=tool_name` |
| `tool_end` | `task_update` `status:"complete"`, `output=tool_output` |
| `tool_error` | `task_update` `status:"error"`, `details=message` |
| `tool_approval_requested` | interrupt the stream, post an approval card (below) |
| `message_handler_end` | end of stream; `output` is the typed reply if we want a final post |
| `turn_end` | close the generator |

Events with **no slot on a chat surface**, which we should say out loud rather than pretend to
carry: `thought_summary`, `text_annotation`, `state_snapshot` / `state_patch`, the whole
`auto_approval_evaluation_*` family, and the subagent brackets
(`subagent_started` / `subagent_message_sent` / `subagent_reply_received`). These are exactly the
things the harness UI exists to render. **That asymmetry is the argument for the rich stream, not
a gap to close** — a chat platform is a lossy projection by nature, and the doc should not
pretend otherwise.

One sharp limit: structured `StreamChunk` rendering is **Slack and Linear only**. Everywhere else
the chunks degrade to their text. Tool progress cards are a Slack feature we get for free, not a
cross-platform capability.

### Streaming degrades per platform, and that is fine

Verified from `docs/streaming.mdx`: native streaming on Slack (`chatStream`) and Teams DMs;
post-and-edit (throttled, `streamingUpdateIntervalMs`, default 500 ms) on Discord, Telegram,
Google Chat and Notion; **buffered single post** on GitHub, WhatsApp and Messenger. "Streaming on
WhatsApp" means one message at the end.

## Approvals are the part that genuinely fits

This is the most interesting finding, and it is the one that makes the whole idea worth doing.

Any Chat SDK `Button` takes a `callbackUrl`. On click, the action is POSTed to that URL:

```json
{
  "type": "action",
  "actionId": "approve",
  "user": { "id": "U123", "name": "alice" },
  "threadId": "slack:C123:1234567890.123",
  "messageId": "1234567890.456"
}
```

Tokens are bound to the button and conversation, single-use, and expire after seven days.

Crucially — and this is easy to miss — the URL **never reaches the platform**. From
`docs/contributing/building.mdx:425`:

> the `callbackUrl` is stored in the state adapter under a short token, and the button's `value`
> field is replaced with `__cb:<16-hex-chars>` (21 characters total).

Two things follow:

1. **No Node process stays resident while a human deliberates.** The FastAPI endpoint is the
   direct POST target. The bot can be cold, redeployed, or scaled to zero when someone clicks
   three hours later. Indefinite human pauses are the harness's native strength and the typical
   bot framework's worst weakness; here they line up.
2. **`tool_id` goes in the URL path, not in the action id.** Discord's `custom_id` is capped at
   100 characters and Telegram's `callback_data` at 64 bytes, and Chat SDK's own 21-character
   token shares that budget — but only with `actionId`, which stays `"approve"` / `"deny"`. A URL
   like `/api/chat-callback/{session_id}/{tool_id}/approve` is unconstrained. An earlier draft of
   this assessment flagged a required short-id indirection; **that concern is void.**

### Do not import `chat/workflow`

The documented helper `buildApprovalCard(options, webhookUrl)` is exported from `chat/workflow`,
and `docs/approvals.mdx` recommends it for exactly our case. Do not use it. `dist/workflow/index.js`
opens with a **top-level** `import { createWebhook, sleep } from "workflow"`, so reaching for the
card builder drags in `workflow@5.0.0-beta.35` — a beta dependency whose entire purpose is durable
suspension, which is the one thing we already have.

The builder itself is ~20 lines of `Card` / `Actions` / `Button`, and all three are exported from
the main `chat` entry, which imports no `workflow` at all (verified). Build the card directly.

## Session identity: `thread.id` is the workflow id

A chat thread has a stable composite id (`slack:C123:1699…`). A harness session is a workflow id.
Mapping one to the other is the whole of session management — no database, no mapping table.

The primitive already exists and is already proven:
`AgentClient.start_and_submit_message` (`agent_client.py:358`) does update-with-start with
`WorkflowIDConflictPolicy.USE_EXISTING`. `nexus_agent_adapter/handler.py:130-174` is the precedent
to copy nearly verbatim — it derives the workflow id as `prefix + session_id` and passes an
`update_id` as an idempotency key.

That idempotency key is not optional here. Slack and Discord both redeliver webhooks, and Chat
SDK's own `deduplicate` option guards its state store, not our workflow. Keying the update on the
platform event id makes a redelivery a no-op instead of a second dispatch.

## What is actually missing on the harness side

Four items. The first is the only one that is more than glue.

1. **No HTTP create-or-reuse-session-by-external-id.** `POST /api/messages` (`web/app.py:290`)
   requires a `session_id` that already exists; `POST /api/sessions` (`:219`) mints
   `agent-session-{uuid4}`. A bot has a stable thread id and needs get-or-create in one call.
   The primitive exists (above); it is simply not reachable over HTTP. **This is the
   highest-value change in the whole assessment, it is an endpoint rather than new machinery, and
   it is useful to every non-Chat-SDK integration too.**
2. **No approval endpoint shaped like a button callback.** `POST /api/approve` (`:267`) expects
   `{session_id, tool_id, approved, …}` from our own UI. The Chat SDK callback body carries
   `threadId` / `actionId` and no `tool_id`. A thin translating endpoint that reads `tool_id` from
   the path and `approved` from `actionId`, then forwards to the existing `tool_approval` update.
3. **Agent selection.** `POST /api/sessions` selects by `agent_workflow_type`. A bot needs a
   configured default per bot or per channel. Configuration, not code.
4. **Handler routing beyond free text.** Most examples expose `ask(TextMessage) -> TextReply`
   (`examples/react_agent/workflow.py:109`), which maps directly onto a chat message.
   `GET /api/agent-interface/{session_id}` returns a JSON Schema per handler (`AcceptedFunction`),
   which is a tempting source for richer entry points — but note that Chat SDK's
   `SlashCommandEvent` gives you a raw `text: string`, not typed options. So schema-driven input
   means rendering a **modal** from the JSON Schema, and modals are Slack/Teams only. Worth doing
   later; not phase one.

### Two non-issues, recorded so they are not re-litigated

- **Streams terminating at `turn_end` is correct here.** A bot posts once per turn; it wants the
  stream to end.
- **The Temporal 10-concurrent-in-flight-updates-per-workflow cap is not a constraint.** A bot
  holds at most one stream per thread, and one thread is one workflow.

## What we would be taking on

The honest cost, none of which is protocol work:

- **A Node deployment.** `chat` is a Node library. `web/app.py` cannot host it in-process, and no
  amount of framing changes that. The accurate claim is *"the harness makes the bridge trivial"*,
  never *"the harness speaks Chat SDK"*.
- **A state store.** `ChatConfig.state: StateAdapter` is **required** (no `?` — verified in
  `dist/types-994wmjht.d.ts`). Options are memory (dev only), Redis, ioredis, or Postgres. The
  harness currently requires no such dependency, and this would be the first.
- **Platform time windows.** `WebhookOptions.waitUntil?: (task: Promise<unknown>) => void`
  answers the 3-second ack — note the jsdoc at `dist/chat-UnQNS1vx.d.ts:128` still shows the stale
  name `backgroundTask`; the real field is `waitUntil`. But Discord's 15-minute interaction token
  and Slack's `response_url` limits are **not** abstracted. Minutes-long *streaming* turns on
  Discord will need chunked re-posts we write ourselves.
- **Workspace install/OAuth**, unless we adopt Vercel Connect. "Minimal dev setup" is real for
  scaffolding; it is not a hosted multi-tenant install story.

## Proposal

**Phase 0 — two FastAPI endpoints.** Create-or-reuse a session by external id, and a
button-callback approval endpoint. Both are small, both reuse `start_and_submit_message` and the
existing `tool_approval` update, and both are **framework-neutral**: they unlock any bot runtime,
not just Chat SDK. This phase has standalone value even if Chat SDK is never adopted, which is
precisely why it should land first and separately.

**Phase 1 — `examples/slack_bot/`.** A TS shim proving Slack end-to-end: SSE parse → yield
strings and `task_update` chunks → approval card with `callbackUrl`. Slack first, because it is
the only platform where both native streaming and structured task cards actually render. Expect
roughly 200–250 lines.

**Phase 2 — generalize.** Discord and Teams; decide then whether the shim is worth publishing as
an npm package or should stay an example people copy.

## Recommendation

Do Phase 0 regardless. It is a couple of endpoints over a primitive we already ship, it removes
the only real harness-side blocker to *any* chat integration, and it costs nothing in surface area
we would regret.

Treat Phase 1 as the actual decision point, and scope it as one example on one platform. The
engineering risk is low and well understood; the thing to weigh is whether we want to own a Node
deployment and a state store as part of the project's supported footprint. That is a product
question, not a technical one, and this document deliberately does not answer it.

What should not happen is an AI SDK protocol implementation in `web/app.py`. It is the intuitive
reading of "support Vercel's chat sdk", it is a substantial amount of work, and the shipped types
show it buys nothing.

## Verification notes

Claims here were checked as follows, and should be re-checked against a newer `chat` if this is
picked up later — the package has shipped 41 minor releases in nine months:

- `npm pack chat@4.41.0`, then read `dist/types-994wmjht.d.ts` (`PostableMessage`, `ChatConfig`,
  `WebhookOptions`, `SlashCommandEvent`, `Thread`), `dist/workflow/index.js` (the `workflow`
  import), `package.json` (optional peers), and the bundled `docs/*.mdx`.
- Harness side: `web/app.py`, `harness/agent_client.py`, `harness/agent_protocol/events.py`,
  `nexus_agent_adapter/handler.py`.
- **Do not cite `ui/docs/api.md` as the event contract.** It is stale — it documents `reply` and
  `error` event types that no longer exist in `AgentEventType`, and omits `callback_requested`,
  `callback_resolved`, `auto_approval_evaluation_superseded`, `state_snapshot` and `state_patch`.
  `events.py` is the source of truth.
