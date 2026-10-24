// The process's Monty pool, in a process of its own: nothing else may create the pool first.

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Monty } from "@pydantic/monty";
import { MockActivityEnvironment } from "@temporalio/testing";
import type { CodeBatchStep } from "@temporalio/agent-harness/harness/code-mode";
import {
  DEFAULT_CODE_MODE_LIMITS,
  codeStartBatch,
} from "../../../src/harness/code-mode/monty-stepper.ts";

const SECRET = "test-only-continuation-secret-0123456789";
const step = (): Promise<CodeBatchStep> =>
  new MockActivityEnvironment().run(
    codeStartBatch,
    "1 + 1",
    null,
    DEFAULT_CODE_MODE_LIMITS,
    { now: 0, seed: 0 },
    SECRET,
  );

describe("the Monty pool", () => {
  it("is created again on the next step after its creation fails, then kept", async () => {
    const create = Monty.create.bind(Monty);
    let creations = 0;
    Monty.create = async (options) => {
      creations += 1;
      if (creations === 1) throw new Error("no monty binary today");
      return create(options);
    };
    try {
      await assert.rejects(step(), /no monty binary today/);
      assert.equal((await step()).output_json, "2");
      assert.equal((await step()).output_json, "2");
      assert.equal(creations, 2);
    } finally {
      Monty.create = create;
    }
  });
});
