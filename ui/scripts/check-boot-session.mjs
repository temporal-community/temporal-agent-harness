import assert from "node:assert/strict";

import { chooseBootSession } from "../src/lib/state/bootSession.ts";

const at = (workflow_id, over = {}) => ({
  workflow_id,
  agent_workflow_type: "QaAgentWorkflow",
  is_message_queuing_enabled: true,
  execution_status: "RUNNING",
  closed: false,
  ...over
});

const gone = { execution_status: "NOT_FOUND", closed: true };
const done = { execution_status: "COMPLETED", closed: true };

const live = at("live");
const gone1 = at("gone1", gone);
const done1 = at("done1", done);

assert.equal(
  chooseBootSession([live, done1], "live", "QaAgentWorkflow"),
  live,
  "the stored session should win when it is live"
);

assert.equal(
  chooseBootSession([live, done1], "done1", "QaAgentWorkflow"),
  done1,
  "a completed stored session should still reopen, since its frames are cached"
);

assert.equal(
  chooseBootSession([live, gone1], "gone1", "QaAgentWorkflow"),
  live,
  "a NOT_FOUND stored session should be skipped for a live one"
);

assert.equal(
  chooseBootSession([gone1, done1], "gone1", "QaAgentWorkflow"),
  null,
  "with nothing live left, boot should start a fresh session rather than a dead one"
);

assert.equal(
  chooseBootSession([done1, gone1], null, "QaAgentWorkflow"),
  null,
  "auto-boot should never pick a closed session: attach is skipped and there is no cache"
);

assert.equal(
  chooseBootSession([at("other", { agent_workflow_type: "OtherWorkflow" })], null, "QaAgentWorkflow"),
  null,
  "auto-boot should ignore sessions belonging to another agent"
);

const newer = at("newer");
assert.equal(
  chooseBootSession([live, newer], null, "QaAgentWorkflow"),
  newer,
  "auto-boot should prefer the most recent live session"
);

assert.equal(
  chooseBootSession([], null, "QaAgentWorkflow"),
  null,
  "an empty session list should start a fresh session"
);

console.log("boot session choice OK");
