/**
 * Read an ordinary chat message and decide what the agent should be asked.
 *
 * Platform slash commands are deliberately NOT the way in. On Discord they must be registered
 * against the application ahead of time — a setup step per bot — and a single registered
 * command forces a clumsy `<command> <handler> <text>` shape on the thing people do most:
 * talking to the agent. So the primary interface is just talking. Plain text goes to the
 * conversational handler, and a leading `/` addresses a specific one.
 *
 *   @bot how's the deploy going      -> ask
 *   @bot /help                       -> the handler guide
 *   @bot /set_approval_policy p=x    -> that handler
 *
 * A registered platform slash command still routes here if one exists, so the two agree by
 * construction — but nothing requires you to register one.
 */

/** The conversational handler almost every agent exposes, used for un-prefixed text. */
export const DEFAULT_HANDLER = 'ask';

/** `/help`, `/?` and a bare `/` all mean "what can you do". */
const GUIDE_WORDS = new Set(['help', 'handlers', 'commands', '?', '']);

export type Intent =
  | { kind: 'guide' }
  | { kind: 'ask'; text: string }
  | { kind: 'handler'; name: string; args: string };

/**
 * Remove leading `<@id>` mentions.
 *
 * Both Discord and Slack deliver the raw body, so an @-addressed message arrives as
 * `<@1475…> hey` and would otherwise be sent to the agent verbatim — mention markup and all.
 */
export function stripLeadingMentions(text: string): string {
  return text.replace(/^(\s*<@[!&]?[^>]+>\s*)+/, '').trim();
}

export function parseIntent(rawText: string): Intent {
  const text = stripLeadingMentions(rawText ?? '');

  if (!text.startsWith('/')) {
    return { kind: 'ask', text };
  }

  const body = text.slice(1).trim();
  const space = body.search(/\s/);
  const name = space === -1 ? body : body.slice(0, space);
  const args = space === -1 ? '' : body.slice(space + 1).trim();

  if (GUIDE_WORDS.has(name.toLowerCase())) return { kind: 'guide' };
  return { kind: 'handler', name, args };
}
