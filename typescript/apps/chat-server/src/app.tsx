/**
 * Chat SDK wiring: discover an agent's interface at runtime and project it onto the platform.
 *
 * The rule the whole file follows: the agent workflow receives only COMPLETE, fully-structured
 * messages. Nothing is accumulated here or there. A modal is filled in the platform and
 * arrives whole; a slash line is parsed whole; a text fallback is parsed whole. That is why
 * this server needs no store, and why no new behaviour is required of the agent.
 */

import { Actions, Button, Card, CardText, Chat, ConsoleLogger, toCardElement } from 'chat';
import type {
  ActionEvent,
  Adapter,
  Logger,
  TaskUpdateChunk,
  ChatElement,
  ModalElement,
  ModalSubmitEvent,
  SlashCommandEvent,
  Thread,
} from 'chat';

import { HarnessClient, type AcceptedFunction } from './harness.ts';
import {
  decodeModalValues,
  fieldsToModalChildren,
  planFields,
  type JsonSchema,
} from './schema-to-modal.ts';
import { resolveAgentKey, type RoutingTable, type BotRoute } from './routing.ts';
import {
  formatToolInput,
  keepTyping,
  openStream,
  renderToolActivity,
  renderedSize,
  rendersTaskUpdates,
  shouldRollOver,
} from './activity.ts';
import { parseIntent } from './command.ts';
import { fieldsToTemplate, parseFieldText } from './text-form.ts';
import { TemporalState } from './state.ts';

/**
 * Thread id -> session id. A pure function, which is the whole point: the harness lets the
 * caller choose the session id (`POST /api/sessions` with `session_id`), so this server never
 * keeps an external-id -> session-id table and can re-derive the session on every event.
 *
 * The prefix only namespaces these away from the manager's own `agent-session-{uuid4}`.
 */
const SESSION_PREFIX = 'chat-';
export const sessionIdForThread = (threadId: string): string => SESSION_PREFIX + threadId;

/**
 * Whether an edit has to supply replacement buttons in order to remove the old ones.
 *
 * Discord PATCHes `components` only when the new payload contains some, so editing with a
 * card that has no actions leaves the original buttons live and clickable — the embed updates
 * and the buttons do not. Supplying one DISABLED button replaces them, which Discord renders
 * greyed out.
 *
 * Slack is the opposite: `chat.update` replaces `blocks` wholesale, and Block Kit has no
 * disabled state, so there the actions must simply be absent or the "resolved" button would
 * render as a live one.
 */
export const needsDisabledButtons = (adapterName: string | undefined): boolean =>
  adapterName === 'discord';

/** One tool call awaiting, or just released from, a human decision. */
export interface Gate {
  toolId: string;
  toolName: string;
  toolInput?: Record<string, unknown>;
}

/**
 * The part of a posted message an approval prompt needs: retire it, or rewrite it.
 *
 * Structural rather than a `SentMessage`, because the two paths that settle a prompt hold
 * different handles on it — the turn holds what `post` returned, a button click holds only its
 * own `threadId`/`messageId`.
 */
export interface PromptMessage {
  delete(): Promise<void>;
  edit(card: ChatElement): Promise<unknown>;
}

/**
 * The approval prompt: what is about to run, and the three answers.
 *
 * The arguments are IN this card, not merely near it. They are the whole question — "run
 * `delete_file`?" is unanswerable without knowing which file — and the tool card that would
 * otherwise carry them is a Slack-native stream chunk that renders only when the app has the
 * agent feature, the `assistant:write` scope, and a live native stream; without all three the
 * adapter skips the chunk and the prompt degrades to a bare tool name. A question nobody can
 * answer is worse than no buttons at all, so the prompt stands on its own.
 */
export function pendingApprovalCard(input: {
  toolName: string;
  toolInput?: Record<string, unknown>;
  /** Short alias for the tool id — what fits in a button payload. */
  shortId: string;
}) {
  const args = formatToolInput(input.toolInput);
  return (
    <Card title={`🔐 Run \`${input.toolName}\`?`}>
      <CardText>{args ?? '_Called with no arguments._'}</CardText>
      <Actions>
        <Button id="approve" label="Approve once" value={input.shortId} style="primary" />
        <Button id="approve-remember" label="Always allow" value={input.shortId} />
        <Button id="deny" label="Deny" value={input.shortId} style="danger" />
      </Actions>
    </Card>
  );
}

/**
 * What a prompt becomes when the answer leaves nothing else behind.
 *
 * An APPROVED prompt is deleted instead (see `settlePrompt`): the call's own card appears where
 * the prompt stood, so the transcript reads as the timeline of what the agent did rather than a
 * settled question parked above the work. A DENIAL has no such successor — the call never runs
 * and never emits another event — so its prompt stays, as the only record that it was asked for
 * and refused. This is also the fallback when a platform refuses to delete: a verdict is still
 * better than live buttons on a decided question.
 */
export function resolvedApprovalCard(input: {
  toolName: string;
  toolInput?: Record<string, unknown>;
  approved: boolean;
  /** Known only when the decision was a click here; a harness-side decision has no name. */
  decidedBy?: string;
  /** The harness's own note — how an auto-denial on session close explains itself. */
  reason?: string;
  adapterName: string | undefined;
}) {
  const verdict = input.approved ? 'Approved' : 'Denied';
  const icon = input.approved ? '✅' : '🚫';
  const args = formatToolInput(input.toolInput);
  return (
    <Card title={`${icon} ${verdict} · ${input.toolName}`}>
      {args ? <CardText>{args}</CardText> : undefined}
      <CardText>
        {(input.decidedBy ? `${verdict} by ${input.decidedBy}` : `${verdict}.`) +
          (input.reason ? ` — ${input.reason}` : '')}
      </CardText>
      {needsDisabledButtons(input.adapterName) ? (
        <Actions>
          <Button id="approval-resolved" label={verdict} disabled />
        </Actions>
      ) : undefined}
    </Card>
  );
}

/** An approval prompt on screen, and the call it is asking about. */
export interface LivePrompt {
  message: PromptMessage;
  gate: Gate;
  adapterName?: string;
}

/**
 * Retire an approval prompt once its question has an answer.
 *
 * An APPROVED call DELETES its prompt. The call's own card takes that place in the thread a
 * moment later, live, so what a reader sees is when the agent did the work — not a settled
 * question parked where the work belongs. A DENIED call keeps its prompt, rewritten as the
 * verdict: nothing follows a denial, so deleting it would erase the request from the timeline
 * altogether.
 *
 * Deleting can also be refused — an old message, a workspace that forbids it — and a verdict
 * is still better than live buttons on a decided question, so that falls back to the rewrite.
 * Both are cosmetic: the decision is already recorded in Temporal, so neither failure may
 * surface as a failed approval.
 */
export async function settleApprovalPrompt(
  prompt: LivePrompt,
  outcome: { approved: boolean; decidedBy?: string; reason?: string },
  log?: Pick<Logger, 'warn'>,
): Promise<void> {
  if (outcome.approved) {
    try {
      await prompt.message.delete();
      return;
    } catch (err) {
      log?.warn('approval: could not delete the settled prompt, rewriting it instead', {
        error: String(err),
      });
    }
  }

  try {
    await prompt.message.edit(
      resolvedApprovalCard({
        toolName: prompt.gate.toolName,
        ...(prompt.gate.toolInput ? { toolInput: prompt.gate.toolInput } : {}),
        approved: outcome.approved,
        ...(outcome.decidedBy ? { decidedBy: outcome.decidedBy } : {}),
        ...(outcome.reason ? { reason: outcome.reason } : {}),
        adapterName: prompt.adapterName,
      }),
    );
  } catch (err) {
    log?.warn('approval: could not update the prompt', { error: String(err) });
  }
}

/**
 * The action ids the approval prompt can answer with.
 *
 * `approve-remember` is "approve, and stop asking": it allow-lists the tool for the session,
 * so later calls skip the gate entirely. Both ids stay far inside the tightest callback budget
 * (Telegram's 64 bytes), so no indirection is needed to carry them.
 */
const APPROVAL_ACTIONS = new Set(['approve', 'approve-remember', 'deny']);

/** Carried in the modal's `privateMetadata`, which survives the Slack round trip intact. */
interface ModalContext {
  threadId: string;
  agentKey: string;
  handler: string;
}

/**
 * Where un-prefixed text goes.
 *
 * Configurable because "the conversational handler" is a per-agent convention, not a harness
 * one: most agents call it `ask(TextMessage)` with a `text` field, but nothing enforces either
 * name, and an agent whose main entry point is `chat(prompt=...)` should still be usable by
 * just talking to it.
 */
export interface DefaultHandler {
  /** The `@agent.accepts` handler name. */
  name: string;
  /** The field of that handler's input model the message body becomes. */
  field: string;
}

export interface ChatServerOptions {
  harness: HarnessClient;
  /** Defaults to `ask` / `text`. */
  defaultHandler?: DefaultHandler;
  /** Where this server's own tracing goes. Shared with Chat SDK and the adapters. */
  logger?: Logger;
  adapters: Record<string, Adapter>;
  routing: { routes: RoutingTable; fallback?: BotRoute };
  userName?: string;
}

export function createChatServer(opts: ChatServerOptions): Chat {
  const { harness } = opts;
  const log = opts.logger ?? new ConsoleLogger('info', 'chat-server');

  /**
   * Run one platform event, and never let it fail silently.
   *
   * Chat SDK invokes handlers from a background task, so an exception thrown here would
   * otherwise vanish: the user sees a bot that simply never answers, with nothing in the log
   * tying the silence to a cause. Every entry point is wrapped so that both a log line and a
   * visible message reach someone.
   */
  async function handled(
    what: string,
    detail: Record<string, unknown>,
    reply: ((text: string) => Promise<unknown>) | undefined,
    run: () => Promise<void>,
  ): Promise<void> {
    log.info(`-> ${what}`, detail);
    try {
      await run();
      log.debug(`<- ${what} ok`, detail);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      log.error(`<- ${what} FAILED: ${message}`, { ...detail, error: String(err) });
      // Surface it in the channel too. A silent bot is indistinguishable from a broken one,
      // and the most common cause — the harness not running — is invisible from Discord.
      await reply?.(`Something went wrong handling that: ${message}`).catch(() => {});
    }
  }

  const chat = new Chat({
    userName: opts.userName ?? 'harness-agent',
    logger: log,
    // Platforms without native streaming (Discord) fall back to post-then-edit, which by
    // default posts a "..." placeholder the instant a reply is requested and edits it in
    // place. That message is created BEFORE the agent has said anything, so it lands above
    // anything posted while the turn runs — an approval card ends up below a reply that
    // logically preceded it. `null` suppresses the placeholder: no message exists until there
    // is real content, so message order follows causality.
    fallbackStreamingPlaceholderText: null,
    adapters: opts.adapters,
    state: new TemporalState(),
    // No thread lock: the agent workflow already serialises a thread's messages, and this
    // strategy's path makes no state call at all. See state.ts.
    concurrency: { strategy: 'concurrent' },
  });

  const agentKeyFor = (botId: string | undefined) => resolveAgentKey(opts.routing, botId);
  const fallbackHandler: DefaultHandler = opts.defaultHandler ?? { name: 'ask', field: 'text' };

  /**
   * Whether this platform can render a form.
   *
   * `Adapter.openModal` is optional and the Discord adapter does not implement it, so this is
   * a real fork, not a theoretical one: on Discord every typed handler has to be reachable
   * through text or it is not reachable at all. Chat SDK would merely log "does not support
   * modals" and return, which from the user's side is a button that does nothing.
   */
  const supportsModals = (adapter: Adapter | undefined): boolean =>
    typeof adapter?.openModal === 'function';

  // -- Plain message: the `ask` handler, which is what a chat surface is natively -----------

  chat.onNewMention(async (thread, message) => {
    await handled(
      'mention',
      { adapter: thread.adapter.name, threadId: thread.id, text: message.text?.slice(0, 80) },
      (t) => thread.post(t),
      async () => {
        const agentKey = agentKeyFor(thread.adapter.name);
        if (!agentKey) {
          await thread.post('No agent is configured for this bot (set BOT_AGENT_ROUTES).');
          return;
        }

        const sessionId = sessionIdForThread(thread.id);
        // 'unknown' deliberately does NOT count as first contact: guessing wrong there posts
        // a guide nobody asked for and, worse, runs an extra harness call that can fail and
        // report the whole turn as broken while the reply itself succeeds.
        const sessionState = await harness.sessionState(sessionId);
        const isFirstContact = sessionState === 'absent';
        log.debug('mention: session', { sessionId, sessionState, agentKey });
        await ensureSession(sessionId, agentKey);

        const intent = parseIntent(message.text);
        log.info('mention: intent', { kind: intent.kind, ...('name' in intent ? { name: intent.name } : {}) });

        // First message in a thread: lead with what this agent accepts. A chat surface has no
        // equivalent of the harness UI's always-visible interface, so the guide is posted
        // once, here, rather than leaving typed handlers undiscoverable. Posted by THIS
        // server — the agent is sent nothing extra.
        if (intent.kind === 'guide') {
          // Explicitly asked for: a failure here IS the failure, so let it surface.
          const handlers = await harness.agentInterface(sessionId);
          await thread.post(guideCard(handlers, agentKey, supportsModals(thread.adapter)));
          return;
        }

        if (isFirstContact) {
          // Unsolicited enrichment. Never let it take the answer down with it: the user asked
          // a question, and losing the reply to a failed preamble is a strictly worse outcome
          // than simply not showing the preamble.
          try {
            const handlers = await harness.agentInterface(sessionId);
            await thread.post(guideCard(handlers, agentKey, supportsModals(thread.adapter)));
          } catch (err) {
            log.warn('mention: could not post the first-contact guide, continuing', {
              error: String(err),
            });
          }
        }

        if (intent.kind === 'ask') {
          // Un-prefixed text goes to the configured conversational handler.
          await runTurn(
            sessionId,
            { type: fallbackHandler.name, payload: { [fallbackHandler.field]: intent.text } },
            message.id,
            thread,
          );
          return;
        }

        // No `openModal`: a plain message carries no trigger_id on any platform, so a
        // handler invoked this way always uses the text form.
        await runHandler(
          { sessionId, agentKey, threadId: thread.id, thread, adapter: thread.adapter },
          intent.name,
          intent.args,
          message.id,
        );
      },
    );
  });

  // -- Slash command: discovery, then a modal per handler -----------------------------------

  chat.onSlashCommand(async (event: SlashCommandEvent) => {
    await handled(
      'slash',
      { adapter: event.adapter?.name, command: event.command, text: event.text, channel: event.channel.id },
      (t) => event.channel.post(t),
      () => runSlashCommand(event),
    );
  });

  /** Everything a dispatch needs, however the user got here. */
  interface Ctx {
    sessionId: string;
    agentKey: string;
    threadId: string;
    thread: Thread;
    /** Present only when the entry point carries a modal trigger (slash command, button). */
    openModal?: (modal: ModalElement | ChatElement) => Promise<{ viewId: string } | undefined>;
    adapter?: Adapter;
  }

  /**
   * Run one named handler with free-text arguments.
   *
   * Shared by `/name args` typed in a message and by a platform slash command, so the two can
   * never drift — the message form is the one that always works, since it needs no
   * registration anywhere.
   */
  async function runHandler(ctx: Ctx, name: string, argText: string, requestId: string): Promise<void> {
    const handlers = await harness.agentInterface(ctx.sessionId);
    const handler = handlers.find((h) => h.name === name);
    if (!handler) {
      await ctx.thread.post(
        `Unknown handler \`${name}\`. This agent accepts: ${handlers.map((h) => h.name).join(', ')}`,
      );
      return;
    }

    const fields = planFields(handler.parameters as JsonSchema);

    // No arguments: open the form where there is one, print the form where there is not.
    if (argText === '' && fields.length > 0) {
      if (ctx.openModal && supportsModals(ctx.adapter)) {
        await openHandlerModal({ openModal: ctx.openModal }, handler, {
          threadId: ctx.threadId,
          agentKey: ctx.agentKey,
          handler: name,
        });
      } else {
        await ctx.thread.post(fieldsToTemplate(name, fields));
      }
      return;
    }

    // Arguments given: parsed the same way everywhere, including where modals exist — someone
    // who knows the handler should not be forced through a dialog.
    const decoded = decodeModalValues(fields, parseFieldText(fields, argText));
    if (!decoded.ok) {
      const problems = Object.entries(decoded.errors)
        .map(([field, message]) => `• ${field}: ${message}`)
        .join('\n');
      await ctx.thread.post(
        `Could not run \`${name}\`:\n${problems}\n\n${fieldsToTemplate(name, fields)}`,
      );
      return;
    }

    await runTurn(ctx.sessionId, { type: name, payload: decoded.payload }, requestId, ctx.thread);
  }

  /**
   * A platform-native slash command, if the platform has one.
   *
   * Nothing requires this to exist: `/name args` typed in an ordinary message routes to the
   * same `runHandler`, which is why Discord needs no command registration at all. It stays
   * wired because Slack declares its commands in the app manifest — no separate registration
   * step — and a real slash command there carries the `trigger_id` a modal needs.
   */
  async function runSlashCommand(event: SlashCommandEvent): Promise<void> {
    const agentKey = agentKeyFor(event.adapter?.name);
    if (!agentKey) {
      await event.channel.post('No agent is configured for this bot (set BOT_AGENT_ROUTES).');
      return;
    }

    const threadId = event.channel.id;
    const sessionId = sessionIdForThread(threadId);
    await ensureSession(sessionId, agentKey);

    const requested = event.text?.trim() ?? '';
    if (!requested) {
      await event.channel.post(
        guideCard(await harness.agentInterface(sessionId), agentKey, supportsModals(event.adapter)),
      );
      return;
    }

    const space = requested.indexOf(' ');
    const name = space === -1 ? requested : requested.slice(0, space);
    const argText = space === -1 ? '' : requested.slice(space + 1).trim();

    await runHandler(
      {
        sessionId,
        agentKey,
        threadId,
        thread: chat.thread(threadId),
        openModal: (modal) => event.openModal(modal),
        adapter: event.adapter,
      },
      name,
      argText,
      `slash:${threadId}:${name}:${argText}`,
    );
  }

  // -- Button: open a handler's modal, or resolve a tool approval ---------------------------

  chat.onAction(async (event: ActionEvent) => {
    await handled(
      'action',
      { adapter: event.adapter?.name, actionId: event.actionId, value: event.value, threadId: event.threadId },
      (t) => event.thread?.post(t) ?? Promise.resolve(),
      () => runAction(event),
    );
  });

  async function runAction(event: ActionEvent): Promise<void> {
    const agentKey = agentKeyFor(event.adapter?.name);
    if (!agentKey) return;

    if (APPROVAL_ACTIONS.has(event.actionId)) {
      await resolveApproval(event);
      return;
    }
    if (event.actionId.startsWith('handler:')) {
      const name = event.actionId.slice('handler:'.length);
      const sessionId = sessionIdForThread(event.threadId);
      const handler = (await harness.agentInterface(sessionId)).find((h) => h.name === name);
      if (!handler) return;

      if (supportsModals(event.adapter)) {
        await openHandlerModal(event, handler, { threadId: event.threadId, agentKey, handler: name });
      } else {
        // Should be unreachable — we do not render these buttons without modal support —
        // but a button that does nothing is the worst possible failure, so it still answers.
        await event.thread?.post(
          fieldsToTemplate(name, planFields(handler.parameters as JsonSchema)),
        );
      }
    }
  }

  // -- Modal submit: one complete structured message ----------------------------------------

  chat.onModalSubmit(async (event: ModalSubmitEvent) => {
    const context = parseContext(event.privateMetadata);
    if (!context) return;

    const sessionId = sessionIdForThread(context.threadId);
    const handler = (await harness.agentInterface(sessionId)).find((h) => h.name === context.handler);
    if (!handler) return;

    const fields = planFields(handler.parameters as JsonSchema);
    const decoded = decodeModalValues(fields, event.values);
    if (!decoded.ok) {
      // Field-level errors keep the modal open so the user fixes it in place, instead of the
      // mistake surfacing seconds later as an agent-side validation failure.
      return { action: 'errors', errors: decoded.errors } as const;
    }

    // `event.relatedThread` is only populated from Chat SDK's stored modal context, which we
    // deliberately do not keep — so the thread is rebuilt from the id we carried ourselves.
    // This is the piece that lets the whole server run without a state store.
    const thread = chat.thread(context.threadId);

    // The modal was filled in the platform; only now, whole, does it reach the agent.
    // One submission of one modal view is one dispatch, however many times Slack delivers it.
    await runTurn(
      sessionId,
      { type: context.handler, payload: decoded.payload },
      `${event.viewId}:${context.handler}`,
      thread,
    );
    return undefined;
  });

  // -- Helpers -------------------------------------------------------------------------------

  /**
   * The agent's interface, rendered for a human.
   *
   * This is the chat-platform stand-in for the harness UI's handler list. A button per
   * handler is what makes a typed handler reachable at all on Slack: a modal needs a
   * `trigger_id`, which only a slash command or a button click carries — never a plain
   * mention — so without these buttons the only way in would be to already know the
   * command's name.
   */
  function guideCard(handlers: AcceptedFunction[], agentKey: string, modals: boolean) {
    return (
      <Card title={`What ${agentKey} accepts`}>
        <CardText>{'Mention me to chat, or send a specific request:'}</CardText>
        {handlers.map((h) => {
          const fields = planFields(h.parameters as JsonSchema);
          // `mid_turn: accept` handlers work while the agent is mid-answer; the rest wait for
          // it to go idle. Worth saying — it is the difference between a control that works
          // right now and one that appears to do nothing.
          const midTurn = h.mid_turn === 'accept' ? ' · usable mid-turn' : '';
          const usage = fieldsToTemplate(h.name, fields);
          return <CardText>{`*${h.name}* — ${h.description}${midTurn}\n${usage}`}</CardText>;
        })}
        {/*
          Buttons open a modal, so they are rendered only where one can open. On Slack they
          are the ONLY way in for a user who does not already know a handler's name, because
          a modal needs a `trigger_id` that just a mention never carries. On Discord they
          would be inert, so the text template above is the whole interface instead.
        */}
        {modals ? (
          <Actions>
            {handlers.map((h) => (
              <Button id={`handler:${h.name}`} label={h.name} />
            ))}
          </Actions>
        ) : undefined}
      </Card>
    );
  }

  /**
   * Create the session if it does not exist, and do nothing if it does.
   *
   * `POST /api/sessions` is idempotent when handed a `session_id`, which is why this can run
   * on every inbound event with no check and no stored mapping.
   */
  async function ensureSession(sessionId: string, agentKey: string): Promise<void> {
    const agents = await harness.listAgents();
    const agent = agents.find((a) => a.key === agentKey);
    if (!agent) {
      throw new Error(
        `No agent with key "${agentKey}". The harness offers: ${agents.map((a) => a.key).join(', ') || '(none)'}`,
      );
    }
    log.debug('ensureSession', { sessionId, workflowType: agent.workflow_type });
    await harness.ensureSession(sessionId, agent.workflow_type);
  }

  async function openHandlerModal(
    event: { openModal: (modal: ModalElement | ChatElement) => Promise<{ viewId: string } | undefined> },
    handler: AcceptedFunction,
    context: ModalContext,
  ) {
    const fields = planFields(handler.parameters as JsonSchema);
    const modal: ModalElement = {
      type: 'modal',
      callbackId: `handler:${handler.name}`,
      title: handler.name,
      submitLabel: 'Send',
      // Everything the submit handler needs travels here, so no modal context is stored.
      privateMetadata: JSON.stringify(context),
      children: fieldsToModalChildren(fields),
    };
    await event.openModal(modal);
  }

  /**
   * Post one message of a turn, showing a typing indicator for as long as it takes — and
   * posting NOTHING when the segment turns out to have nothing to say.
   *
   * The indicator covers the part no message can: `submitMessage` round-tripping, the model
   * thinking, and any tool call before the first token. With no placeholder message (see
   * `fallbackStreamingPlaceholderText: null`) that window would otherwise be pure silence.
   *
   * The emptiness check is not an optimization. Posting an empty stream costs a real, blank
   * message on both transports (see `openStream`), and a turn that pauses for an approval
   * produces two empty segments — so the transcript grew a blank loading bubble above the
   * buttons and another below them.
   */
  async function postStream(
    thread: Thread,
    stream: AsyncGenerator<string | TaskUpdateChunk>,
  ): Promise<void> {
    const typing = keepTyping(thread, (err) =>
      log.debug('typing indicator failed, continuing', { error: String(err) }),
    );
    try {
      const opened = await openStream(stream);
      if (!opened) return;
      await thread.post(opened);
    } finally {
      typing.stop();
    }
  }

  /**
   * Approval prompts still on screen, keyed by session and tool id.
   *
   * In-flight, per-process and purely cosmetic: it decides what becomes of the MESSAGE that
   * asked, never of the decision, which Temporal owns. Two paths race to settle one prompt —
   * the click that made the decision (the only one that knows WHO clicked) and the turn's own
   * `tool_approval_resolved` (which also fires for a decision made in the harness UI, for the
   * cascade behind "Always allow", and for the auto-denial when a session closes). Whichever
   * claims the entry first does the work; a restart loses only the tidy-up, and the click path
   * can still settle its own message from the event.
   */
  const livePrompts = new Map<string, LivePrompt>();
  const promptKey = (sessionId: string, toolId: string) => `${sessionId} ${toolId}`;

  /** The message a button lives on, as something `settlePrompt` can retire. */
  const promptMessageFor = (event: ActionEvent): PromptMessage => ({
    delete: () => event.adapter.deleteMessage(event.threadId, event.messageId),
    edit: async (card) => {
      const element = toCardElement(card);
      if (!element) throw new Error('could not build the resolved card');
      await event.adapter.editMessage(event.threadId, event.messageId, element);
    },
  });

  /**
   * Claim a prompt and settle it, if it is still ours to settle.
   *
   * The claim is the point: two paths reach here for the same prompt and only one may act.
   * Nothing awaits between the read and the delete, so the claim cannot interleave.
   */
  async function settlePrompt(
    sessionId: string,
    toolId: string,
    outcome: { approved: boolean; decidedBy?: string; reason?: string },
    fallback?: LivePrompt,
  ): Promise<void> {
    const key = promptKey(sessionId, toolId);
    const live = livePrompts.get(key) ?? fallback;
    if (!live) return;
    livePrompts.delete(key);
    await settleApprovalPrompt(live, outcome, log);
  }

  /**
   * Ask about one gated call, and remember the message that asked.
   *
   * Self-contained by design — name, arguments and buttons in one card — so it depends on
   * nothing rendered around it and can therefore be posted while nothing is streaming.
   */
  async function postApprovalPrompt(
    sessionId: string,
    gate: Gate,
    thread: Thread,
  ): Promise<boolean> {
    // The short id lives only on the status query today, so the prompt costs one read.
    const status = await harness.status(sessionId);
    const pending = status.pending_approvals.find((p) => p.tool_id === gate.toolId);
    if (!pending) {
      log.warn('approval: gate reported but no pending approval found', { toolId: gate.toolId });
      return false;
    }

    const message = await thread.post(
      pendingApprovalCard({
        toolName: gate.toolName,
        ...(gate.toolInput ? { toolInput: gate.toolInput } : {}),
        shortId: pending.short_id,
      }),
    );
    livePrompts.set(promptKey(sessionId, gate.toolId), {
      message,
      gate,
      ...(thread.adapter?.name ? { adapterName: thread.adapter.name } : {}),
    });
    return true;
  }

  /**
   * Run one message to completion, as a sequence of streamed messages.
   *
   * Two halves, per the harness's client contract: `submit_message` admits the message and
   * returns where its dispatch begins, then `attach` reads the session stream from there.
   * Streaming the send instead (`POST /api/chat`) cannot express a mid-turn message — it
   * rejects with `joined_turn` the moment a `mid_turn: accept` handler joins a running turn,
   * which is precisely when a user reaches for one.
   *
   * The stream is the whole SESSION, so events are filtered to this message's `message_id`,
   * which the harness stamps on every event of a dispatch. That makes all three dispositions
   * — opened, joined, queued — just work. It terminates on `message_handler_end`/`_error`,
   * which is terminal for the MESSAGE; waiting for `turn_end` would hang a joined message
   * until every other participant finished.
   *
   * The timeline a tool approval produces:
   *
   *     [ reply text, finished tool cards ]          this message ends at the gate
   *     [ Run delete_file? + arguments               its own message, nothing streaming
   *       Approve once | Always allow | Deny ]
   *                  -- approved: that message is DELETED
   *     [ starting -> running -> done, delete_file    one card, spinning from the decision on,
   *       then the rest of the reply ]                followed by the reply that resumes
   *                  -- denied: it becomes the verdict
   *     [ Denied - delete_file - arguments ]
   *
   * Three rules produce it, and each one is load-bearing:
   *
   *   - The message ends at the gate, and the wait happens with NOTHING open. The wait is a
   *     human's and has no bound, while Slack expires a native stream held open across one —
   *     after which the adapter reopens it and REPLAYS every still-open task card into a
   *     second message, which is one tool call rendered twice.
   *   - The prompt carries the arguments itself, so it needs nothing above it and can be its
   *     own message. (It has to be: buttons cannot be attached to a stream — `StreamOptions`
   *     has `stopBlocks`, but `post()` takes no options.)
   *   - An approved call's card opens in the message that follows the deleted prompt, and its
   *     phases stay inside that one message — Slack keys task cards by id WITHIN a message, so
   *     a call split across two messages renders as two cards.
   */
  async function runTurn(
    sessionId: string,
    message: string | { type: string; payload: Record<string, unknown> },
    requestId: string,
    thread: Thread,
  ): Promise<void> {
    const accepted = await harness.submitMessage(sessionId, message, requestId);
    log.info('message: accepted', {
      sessionId,
      messageId: accepted.message_id,
      disposition: accepted.disposition,
      turn: accepted.turn_number,
    });

    // ONE iterator for the whole turn, stepped by hand. A `for await` that exits early calls
    // `.return()` on it, which would close the attach stream at the first gate and lose
    // everything after it.
    const events = harness.attach(sessionId, accepted.accepted_offset);
    interface Frame {
      event: string;
      data: Record<string, unknown>;
    }

    // Only a MODEL-streamed reply arrives as reply_delta; a handler that simply returns its
    // output model emits none, so `message_handler_end.output` covers that case.
    let streamedAnything = false;
    let finished = false;
    /** One frame read while no message was open, handed to the next one to render. */
    let pushedBack: Frame | undefined;
    /** Every gate this message has opened, by tool id: the arguments outlive the prompt. */
    const gates = new Map<string, Gate>();
    /** Gates whose prompt is still to be posted — the message they interrupted closes first. */
    const toPrompt: Gate[] = [];
    /** Tool ids whose prompt is on screen and unanswered. */
    const outstanding = new Set<string>();
    /** Approved gates whose card has not opened yet: the next message leads with them. */
    const toOpen: Gate[] = [];

    /** The next frame belonging to THIS message, or undefined once the stream closes. */
    const nextFrame = async (): Promise<Frame | undefined> => {
      for (;;) {
        if (pushedBack) {
          const frame = pushedBack;
          pushedBack = undefined;
          return frame;
        }
        const next = await events.next();
        if (next.done) {
          log.warn('message: stream closed before the handler finished', { sessionId });
          return undefined;
        }
        // Turn brackets carry no message_id; everything belonging to a dispatch does.
        const owner = next.value.data.message_id;
        if (owner !== undefined && owner !== null && owner !== accepted.message_id) continue;
        return next.value;
      }
    };

    const toolIdOf = (frame: Frame) => String(frame.data.tool_id ?? '');

    /** Record a gate the moment it is requested, and queue the prompt that asks about it. */
    const requested = (frame: Frame): void => {
      const gate: Gate = {
        toolId: toolIdOf(frame),
        toolName: String(frame.data.tool_name ?? 'tool'),
        ...(frame.data.tool_input
          ? { toolInput: frame.data.tool_input as Record<string, unknown> }
          : {}),
      };
      gates.set(gate.toolId, gate);
      toPrompt.push(gate);
    };

    /**
     * A decision landed: retire the prompt, and say whether a card should now open.
     *
     * Reached for every resolution, not just a clicked one — so a decision made in the harness
     * UI, a call swept up by an earlier "Always allow", and the auto-denial when a session
     * closes all settle their prompt too, instead of leaving live buttons on screen.
     */
    const resolved = async (frame: Frame): Promise<Gate | undefined> => {
      const toolId = toolIdOf(frame);
      outstanding.delete(toolId);
      const approved = frame.data.approved === true;
      await settlePrompt(sessionId, toolId, {
        approved,
        ...(typeof frame.data.reason === 'string' && frame.data.reason
          ? { reason: frame.data.reason }
          : {}),
      });
      // A denial renders nothing: its prompt IS the record, and no call follows it.
      return approved ? gates.get(toolId) : undefined;
    };

    /**
     * Whether a just-approved call leads with a card of its own.
     *
     * Only where one card becomes the running and finished states of the same call. Appended
     * text cannot be revised, so elsewhere the call says nothing until it actually starts.
     */
    const opensWithACard = rendersTaskUpdates(thread.adapter?.name);

    const segment = async function* (opening: Gate[]): AsyncGenerator<string | TaskUpdateChunk> {
      let spent = 0;
      let cards = 0;
      // Tool calls whose card is still open in THIS message. A card exists only within one
      // message, so rolling over while a call is in flight would strand it and open a second.
      const openTools = new Set<string>();

      const emit = function* (item: string | TaskUpdateChunk) {
        spent += renderedSize(item);
        if (typeof item !== 'string') cards += 1;
        yield item;
      };

      const open = function* (gate: Gate) {
        // The spinner starts when the human said yes, not when the call happens to reach a
        // worker: that gap is exactly the part of the timeline a card is there to explain.
        openTools.add(gate.toolId);
        yield* emit(renderToolActivity({ phase: 'pending', ...gate }, thread.adapter));
      };

      if (opensWithACard) for (const gate of opening) yield* open(gate);

      while (true) {
        const frame = await nextFrame();
        if (!frame) {
          finished = true;
          return;
        }
        const toolId = toolIdOf(frame);

        switch (frame.event) {
          case 'reply_delta':
            if (typeof frame.data.text === 'string' && frame.data.text !== '') {
              streamedAnything = true;
              yield* emit(frame.data.text);
            }
            break;

          // Tool lifecycle, gated or not: a turn that spends twenty seconds inside a tool
          // should say so rather than look like a hang.
          case 'tool_start':
          case 'tool_end':
          case 'tool_error': {
            if (frame.event === 'tool_start') openTools.add(toolId);
            else openTools.delete(toolId);
            yield* emit(
              renderToolActivity(
                {
                  phase:
                    frame.event === 'tool_start'
                      ? 'start'
                      : frame.event === 'tool_end'
                        ? 'end'
                        : 'error',
                  toolId,
                  toolName: String(frame.data.tool_name ?? 'tool'),
                  toolInput: frame.data.tool_input as Record<string, unknown> | undefined,
                  toolOutput:
                    typeof frame.data.tool_output === 'string' ? frame.data.tool_output : undefined,
                  message: typeof frame.data.message === 'string' ? frame.data.message : undefined,
                },
                thread.adapter,
              ),
            );
            // Roll over only when nothing is mid-flight, and never mid-text.
            if (shouldRollOver(spent, cards, openTools.size)) {
              log.debug('message: rolling over to a new message', { spent, cards });
              return;
            }
            break;
          }

          case 'tool_approval_requested':
            requested(frame);
            // End the message here. Its prompt is posted with nothing streaming, because what
            // follows is an unbounded human wait and a native stream does not survive one.
            return;

          case 'tool_approval_resolved': {
            // A second gated call can be answered while this message is still streaming the
            // first one's output; its card belongs here, where its own events will land.
            const gate = await resolved(frame);
            if (gate && opensWithACard) yield* open(gate);
            break;
          }

          case 'message_handler_end': {
            const rendered = streamedAnything ? '' : renderHandlerOutput(frame.data.output);
            log.info('message: done', { sessionId, streamed: streamedAnything });
            if (rendered) yield* emit(rendered);
            finished = true;
            return;
          }
          case 'message_handler_error':
            log.error('message: handler failed', { sessionId, error: frame.data.message });
            yield* emit(
              `\n_The agent's handler failed: ${String(frame.data.message ?? 'unknown error')}_`,
            );
            finished = true;
            return;
        }
      }
    };

    /**
     * Wait out a human decision with no message open.
     *
     * No typing indicator either: the agent is not working, it is waiting on a person, and
     * claiming otherwise would be a lie about the very timeline this is here to keep honest.
     * Anything that needs rendering ends the wait and is handed to the next message.
     */
    const awaitDecision = async (): Promise<void> => {
      while (outstanding.size > 0 && toOpen.length === 0 && pushedBack === undefined) {
        const frame = await nextFrame();
        if (!frame) {
          finished = true;
          return;
        }
        if (frame.event === 'tool_approval_requested') {
          // Parallel gated calls: ask about this one too before waiting again.
          requested(frame);
          return;
        }
        if (frame.event === 'tool_approval_resolved') {
          const gate = await resolved(frame);
          if (gate) toOpen.push(gate);
          continue;
        }
        pushedBack = frame;
        return;
      }
    };

    try {
      while (!finished) {
        if (toPrompt.length > 0) {
          for (const gate of toPrompt.splice(0)) {
            if (await postApprovalPrompt(sessionId, gate, thread)) outstanding.add(gate.toolId);
          }
          continue;
        }
        // Waiting is only right when there is nothing to show. A frame handed back from a
        // previous wait has to be rendered, or the two would pass it between them forever.
        if (pushedBack === undefined && outstanding.size > 0 && toOpen.length === 0) {
          await awaitDecision();
          continue;
        }
        await postStream(thread, segment(toOpen.splice(0)));
      }
    } finally {
      // The turn is over however it ended; do not leave the attach stream open.
      await events.return?.(undefined);
      // Nor leave entries for prompts this turn will never hear about again. A click still
      // settles its own message: `settlePrompt` takes the event's handle as a fallback.
      for (const toolId of outstanding) livePrompts.delete(promptKey(sessionId, toolId));
    }
  }

  /**
   * Render a handler's return model for a chat surface.
   *
   * `output` is the handler's output type serialized to a dict. A `text` field is the
   * overwhelming convention (`TextReply`), so it is used verbatim; anything else is shown as
   * JSON rather than guessed at, which is honest and still tells the user it worked.
   */
  function renderHandlerOutput(output: unknown): string {
    if (!output || typeof output !== 'object') return '';
    const record = output as Record<string, unknown>;
    if (typeof record.text === 'string') return record.text;
    const keys = Object.keys(record);
    if (keys.length === 0) return '_Done._';
    return '```json\n' + JSON.stringify(record, null, 2) + '\n```';
  }

  async function resolveApproval(event: ActionEvent) {
    const sessionId = sessionIdForThread(event.threadId);
    const shortId = event.value;
    // The button carries the SHORT id; the full provider-minted tool_id would not fit a
    // Discord custom_id or a Telegram callback_data. Temporal is the lookup table.
    const before = await harness.status(sessionId);
    const match = before.pending_approvals.filter((p) => p.short_id === shortId);
    if (match.length !== 1) {
      await event.thread?.post(
        match.length === 0
          ? 'That approval is no longer pending — it may already have been decided, or allowed by an earlier "Always allow".'
          : 'Could not identify that approval unambiguously.',
      );
      return;
    }

    const pending = match[0]!;
    const approved = event.actionId !== 'deny';
    const remember = event.actionId === 'approve-remember';
    await harness.approve(sessionId, pending.tool_id, approved, remember);

    const decidedBy = event.user.fullName || event.user.userName || 'someone';
    log.info('approval: resolved', { toolName: pending.tool_name, approved, remember, decidedBy });

    // Settle from here rather than waiting for the event to come back round: a click should
    // take effect at once, and this is the only path that knows who made the decision. The
    // turn settles whatever this does not — a cascade, or a decision made somewhere else.
    await settlePrompt(
      sessionId,
      pending.tool_id,
      { approved, decidedBy },
      {
        message: promptMessageFor(event),
        gate: {
          toolId: pending.tool_id,
          toolName: pending.tool_name,
          toolInput: pending.tool_input,
        },
        ...(event.adapter?.name ? { adapterName: event.adapter.name } : {}),
      },
    );

    // "Always allow" changes the session's policy, and the prompt that said so is now gone.
    // Every later call of this tool skips the gate — as does any other call of it waiting
    // right now — so leave the one line that explains why the bot stopped asking.
    if (approved && remember) {
      await event.thread
        ?.post(
          `_\`${pending.tool_name}\` is allowed for the rest of this session — ${decidedBy}_`,
        )
        .catch((err: unknown) =>
          log.warn('approval: could not note the allow-list change', { error: String(err) }),
        );
    }
  }

  function parseContext(raw: string | undefined): ModalContext | undefined {
    if (!raw) return undefined;
    try {
      const parsed = JSON.parse(raw) as ModalContext;
      return parsed.threadId && parsed.agentKey && parsed.handler ? parsed : undefined;
    } catch {
      return undefined;
    }
  }

  return chat;
}
