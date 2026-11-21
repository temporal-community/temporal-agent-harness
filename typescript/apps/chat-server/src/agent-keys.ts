/**
 * Agent keys and the workflow types they name, read from the server's registry once rather than on
 * every message. The registry can change while the bot runs, so a key not seen yet reads it again,
 * and so does a start that fails: an agent deleted and registered again under the same key may name
 * a new workflow type.
 */

/** The fields of an `/api/agents` entry this needs. */
export interface KeyedAgent {
  key: string;
  workflow_type: string;
}

export class AgentKeys {
  #workflowTypes: Map<string, string> | null = null;

  constructor(private readonly listAgents: () => Promise<KeyedAgent[]>) {}

  /** The workflow type `key` names. Throws, listing the keys there are, when none matches. */
  async workflowType(key: string): Promise<string> {
    const known = this.#workflowTypes?.get(key);
    if (known !== undefined) return known;
    const types = await this.#read();
    const found = types.get(key);
    if (found === undefined) {
      throw new Error(
        `No agent with key "${key}". The Agent Kit server offers: ${[...types.keys()].join(', ') || '(none)'}`,
      );
    }
    return found;
  }

  /**
   * Run `start` with the workflow type `key` names. If it fails, read the registry again, and
   * run it once more when `key` now names another workflow type; otherwise the failure stands.
   */
  async withWorkflowType<T>(key: string, start: (workflowType: string) => Promise<T>): Promise<T> {
    const workflowType = await this.workflowType(key);
    try {
      return await start(workflowType);
    } catch (error) {
      const current = (await this.#read()).get(key);
      if (current === undefined || current === workflowType) throw error;
      return start(current);
    }
  }

  async #read(): Promise<Map<string, string>> {
    const agents = await this.listAgents();
    this.#workflowTypes = new Map(agents.map((agent) => [agent.key, agent.workflow_type]));
    return this.#workflowTypes;
  }
}
