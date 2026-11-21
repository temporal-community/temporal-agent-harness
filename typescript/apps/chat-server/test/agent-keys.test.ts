import assert from 'node:assert/strict';
import { test } from 'node:test';

import { AgentKeys, type KeyedAgent } from '../src/agent-keys.ts';

/** A registry whose contents a test changes, counting how often it is read. */
function registry(agents: KeyedAgent[]) {
  const state = { agents, reads: 0 };
  const keys = new AgentKeys(async () => {
    state.reads += 1;
    return state.agents;
  });
  return { state, keys };
}

test('a key is read from the registry once, not on every message', async () => {
  const { state, keys } = registry([{ key: 'monty', workflow_type: 'MontyAgent' }]);

  for (let i = 0; i < 5; i++) assert.equal(await keys.workflowType('monty'), 'MontyAgent');

  assert.equal(state.reads, 1);
});

test('a key not seen yet reads the registry again, since agents are registered while the bot runs', async () => {
  const { state, keys } = registry([{ key: 'monty', workflow_type: 'MontyAgent' }]);
  await keys.workflowType('monty');

  state.agents = [...state.agents, { key: 'react', workflow_type: 'ReactAgent' }];

  assert.equal(await keys.workflowType('react'), 'ReactAgent');
  assert.equal(state.reads, 2);
});

test('an unknown key names the keys there are', async () => {
  const { keys } = registry([{ key: 'monty', workflow_type: 'MontyAgent' }]);

  await assert.rejects(keys.workflowType('nobody'), /No agent with key "nobody"\. The Agent Kit server offers: monty/);
});

test('a start that fails is retried once when the key now names another workflow type', async () => {
  const { state, keys } = registry([{ key: 'monty', workflow_type: 'MontyAgent' }]);
  await keys.workflowType('monty');
  state.agents = [{ key: 'monty', workflow_type: 'MontyAgentV2' }];
  const started: string[] = [];

  const result = await keys.withWorkflowType('monty', async (workflowType) => {
    started.push(workflowType);
    if (workflowType === 'MontyAgent') throw new Error('422 Unknown agent type');
    return 'started';
  });

  assert.equal(result, 'started');
  assert.deepEqual(started, ['MontyAgent', 'MontyAgentV2']);
});

test("a start that fails for another reason keeps its error", async () => {
  const { keys } = registry([{ key: 'monty', workflow_type: 'MontyAgent' }]);
  let starts = 0;

  await assert.rejects(
    keys.withWorkflowType('monty', async () => {
      starts += 1;
      throw new Error('409 retired');
    }),
    /409 retired/,
  );
  assert.equal(starts, 1);
});
