// Mid-turn steering's value types (Python's steering.py): one queued steering message, and the
// batch `AgentWorkflowRunner.claimSteering` hands to the code building model input, which renders
// it as the XML block appended to a tool result.

export const STEERING_PREAMBLE =
  "The user sent the following while you were working. " +
  "Take them into account before continuing.";

/** One piece of steering text: its own id, and the message whose handler queued it. */
export interface SteeringMessage {
  readonly steering_id: string;
  readonly message_id: string;
  readonly text: string;
}

function escapeXml(text: string): string {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

/** Every steering message that was queued when `claimSteering` ran, in arrival order. */
export class ClaimedSteering {
  readonly messages: readonly SteeringMessage[];

  constructor(messages: readonly SteeringMessage[]) {
    this.messages = messages;
  }

  get steeringIds(): string[] {
    return this.messages.map((m) => m.steering_id);
  }

  /**
   * The block to append to model input. The explanatory line is inside the block because the
   * harness does not own the system prompt: without it the model sees an unfamiliar tag at the end
   * of a tool result and has to guess whether the tool produced it. Message ids are left out; the
   * model can do nothing with them, and events carry them instead.
   */
  render(): string {
    const body = this.messages.map((m) => `<message>${escapeXml(m.text)}</message>`).join("\n");
    return `<steering_messages>\n${STEERING_PREAMBLE}\n${body}\n</steering_messages>`;
  }

  /** `text` with the rendered block appended after a blank line. */
  appendTo(text: string): string {
    return text ? `${text}\n\n${this.render()}` : this.render();
  }
}
