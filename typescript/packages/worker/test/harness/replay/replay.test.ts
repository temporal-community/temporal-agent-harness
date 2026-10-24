// Replay: today's workflow code must reproduce the commands of histories recorded by earlier code,
// or every running session fails its next workflow task after a deploy. histories/ holds the
// scripted demo (a turn, an approval, a callback, Code Mode, a subagent) and a raw-events publish
// in a run's first task. Re-record them only on purpose:
// bazel run //typescript/packages/worker/test/harness/replay:histories_update

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { WorkflowHandle } from "@temporalio/client";
import { historyFromJSON } from "@temporalio/common/lib/proto-utils.js";
import { Worker, type ReplayWorkerOptions } from "@temporalio/worker";
import { after, before, describe, it } from "node:test";
import { TOOL_APPROVAL_UPDATE } from "@temporalio/agent-harness/harness/agent-protocol";
import { collect, replyText, send, startEnv, type TestEnv } from "../../support.ts";
import { BUNDLES, type Bundle } from "./bundles.ts";

const HISTORIES = fileURLToPath(new URL("./histories/", import.meta.url));

async function replayErrors(
  options: ReplayWorkerOptions,
  histories: { workflowId: string; history: unknown }[],
) {
  const results = [];
  for await (const result of Worker.runReplayHistories(options, histories))
    results.push({ workflowId: result.workflowId, error: result.error?.message });
  return results;
}

describe("recorded histories replay against the current code", () => {
  const bundles: Bundle[] = ["scripted-demo", "fixtures"];
  for (const bundle of bundles)
    it(`every ${bundle} history replays without nondeterminism`, async () => {
      const dir = `${HISTORIES}${bundle}`;
      const histories = readdirSync(dir)
        .filter((f) => f.endsWith(".json"))
        .map((f) => ({
          workflowId: f.replace(/\.json$/, ""),
          history: historyFromJSON(JSON.parse(readFileSync(`${dir}/${f}`, "utf8"))),
        }));
      assert.ok(histories.length > 0, `no histories in ${dir}`);
      assert.deepStrictEqual(
        await replayErrors(BUNDLES[bundle], histories),
        histories.map((h) => ({ workflowId: h.workflowId, error: undefined })),
      );
    });
});

describe("the current code replays its own histories", () => {
  let env: TestEnv;
  before(async () => {
    env = await startEnv("replay");
  });
  after(async () => {
    await env.stop();
  });

  async function replay(h: WorkflowHandle) {
    const history = await h.fetchHistory();
    assert.deepStrictEqual(
      await replayErrors(BUNDLES.fixtures, [{ workflowId: h.workflowId, history }]),
      [{ workflowId: h.workflowId, error: undefined }],
    );
  }

  for (const scenario of ["retry", "abandon"])
    it(`a raw-streamed model call whose activity is ${scenario === "retry" ? "retried" : "abandoned"} replays`, async () => {
      const h = await env.start("RawStreamProbeAgent");
      await send(h, "run", { text: scenario });
      await collect(env.client, h.workflowId);
      await replay(h);
    });

  it("a Code Mode script resumed after an approval replays", async () => {
    const h = await env.start("CodeModeProbeAgent");
    const script =
      'import asyncio\nasync def main():\n    a = await search(origin="SFO", n=1)\n    b = await book(id=a["items"][0]["id"])\n    await search(origin="BOS", n=1)\n    return b\nasyncio.run(main())';
    await send(h, "run", { text: script });
    let approvals = 0;
    const events = await collect(env.client, h.workflowId, {
      onEvent: async (e) => {
        if (e.event.type !== "tool_approval_requested") return;
        approvals++;
        await h.executeUpdate(TOOL_APPROVAL_UPDATE, {
          args: [{ tool_id: e.event.tool_id, approved: true }],
        });
      },
    });
    assert.equal(approvals, 1);
    assert.equal(replyText(events), "result: 'BOOKED-SFO-0'");
    await replay(h);
  });

  const py = (body: string) =>
    `import asyncio\nasync def main():\n${body.replace(/^/gm, "    ")}\nasyncio.run(main())`;
  const scripts: [string, string, string | RegExp][] = [
    [
      "the workflow's clock and a seeded random",
      py(
        "import random\nfrom datetime import datetime\nreturn [datetime.now().year > 2000, random.random() < 1]",
      ),
      "result: [True, True]",
    ],
    [
      "a durable sleep",
      py(
        'await asyncio.sleep(0.1)\nr = await search(origin="SFO", n=1)\nreturn r["items"][0]["id"]',
      ),
      "result: 'SFO-0'",
    ],
    [
      "a call awaited after a sleep",
      py(
        'later = search(origin="BOS", n=1)\nawait asyncio.sleep(0.1)\nr = await later\nreturn r["items"][0]["id"]',
      ),
      "result: 'BOS-0'",
    ],
    [
      "a failed call caught in the script",
      py('try:\n    await failing_tool(text="x")\nexcept Exception as e:\n    return str(e)'),
      "result: 'ApplicationError: tool broke'",
    ],
    [
      "a runaway step",
      py('await search(origin="SFO", n=1)\nwhile True:\n    pass'),
      /^Script error \(TimeoutError: /,
    ],
  ];
  for (const [name, script, reply] of scripts)
    it(`a Code Mode script using ${name} replays`, async () => {
      const h = await env.start("CodeModeProbeAgent");
      await send(h, "run", { text: script });
      const text = replyText(await collect(env.client, h.workflowId));
      if (typeof reply === "string") assert.equal(text, reply);
      else assert.match(text, reply);
      await replay(h);
    });
});
