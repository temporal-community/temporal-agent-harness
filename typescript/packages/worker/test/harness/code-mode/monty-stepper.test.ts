import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { MockActivityEnvironment } from "@temporalio/testing";
import {
  renderTypeCheckStubs,
  type CodeBatchStep,
  type HostClock,
} from "@temporalio/agent-harness/harness/code-mode";
import {
  DEFAULT_CODE_MODE_LIMITS,
  STEP_TIME_LIMIT_SECS,
  TIMED_OUT_ERROR,
  codeModeActivities,
  codeResumeBatch,
  codeStartBatch,
  codeTypeCheck,
} from "../../../src/harness/code-mode/monty-stepper.ts";
import { searchItems } from "../../fixtures/workflows.ts";
import { assertMatch } from "../../match.ts";
import { pack } from "./tools.ts";

const SECRET = "test-only-continuation-secret-0123456789";
const CLOCK: HostClock = { now: 0, seed: 1 };
const searchStubs = renderTypeCheckStubs([searchItems]);
const script = (body: string) =>
  `import asyncio\nasync def main():\n${body}\nasyncio.run(main())\n`;

const start = (
  env: MockActivityEnvironment,
  src: string,
  stubs: Parameters<typeof codeStartBatch>[1],
  clock: HostClock = CLOCK,
): Promise<CodeBatchStep> =>
  env.run(codeStartBatch, src, stubs, DEFAULT_CODE_MODE_LIMITS, clock, SECRET);

const resume = (
  env: MockActivityEnvironment,
  input: Parameters<typeof codeResumeBatch>[0],
): Promise<CodeBatchStep> => env.run(codeResumeBatch, input, DEFAULT_CODE_MODE_LIMITS, SECRET);

describe("the Monty sandbox", () => {
  it("type-checks a good script, and reports a bad key or a wrong argument type without running it", async () => {
    assert.equal(
      await codeTypeCheck(
        script('    r = await search(origin="SFO", n=3)\n    return r["items"][0]["price"]'),
        searchStubs,
      ),
      "",
    );
    assert.equal(
      await codeTypeCheck(
        script('    print("ran")\n    return await search(origin="SFO", n=3)'),
        searchStubs,
      ),
      "",
    );
    const badKey = await codeTypeCheck(
      script('    r = await search(origin="SFO", n=3)\n    return r["items"][0]["nope"]'),
      searchStubs,
    );
    assert.match(badKey, /^MontyTypingError/);
    const badArg = await codeTypeCheck(
      script('    return await search(origin="SFO", n="three")'),
      searchStubs,
    );
    assert.match(badArg, /^MontyTypingError/);
    assert.ok(badArg.includes("main.py:3"));
  });

  for (const [name, head, body] of [
    ["an import of asyncio.gather", "from asyncio import gather\n", ""],
    ["an annotated TypedDict literal", "", "    b: Box = {'size': 2}\n"],
    ["a helper taking the TypedDict", "def size(b: Box) -> int:\n    return b['size']\n", ""],
    ["a TypedDict constructor call", "", "    b = Box(size=1)\n"],
  ]) {
    it(`the checker allows what runs: ${name}`, async () => {
      const src = `import asyncio\n${head}async def main():\n${body}    return await pack({'size': 1})\nasyncio.run(main())\n`;
      assert.equal(await codeTypeCheck(src, renderTypeCheckStubs([pack])), "");
    });
  }

  it("calling a stub type builds a dict inside the sandbox, and host calls come back as batches", async () => {
    const stubs = renderTypeCheckStubs([pack]);
    const env = new MockActivityEnvironment();
    const src = script(
      "    first = await pack(Box(size=1))\n    again = Box({'size': first['size'] + 1})\n    return [again, await pack(again)]",
    );
    let step = await start(env, src, stubs);
    assert.equal(step.done, false, step.error ?? "");
    assert.deepEqual(
      step.calls.map((c) => [c.function_name, c.args]),
      [["pack", [{ size: 1 }]]],
    );
    const answer = (size: number) => ({
      snapshot: step.snapshot!,
      results: [{ call_id: step.awaiting[0]!, return_value: { size } }],
      type_names: stubs.type_names,
      clock: CLOCK,
    });
    step = await resume(env, answer(5));
    assert.equal(step.done, false, step.error ?? "");
    assert.deepEqual(
      step.calls.map((c) => [c.function_name, c.args]),
      [["pack", [{ size: 6 }]]],
    );
    step = await resume(env, answer(7));
    assertMatch(step, { done: true, error: null });
    assert.deepEqual(JSON.parse(step.output_json!), [{ size: 6 }, { size: 7 }]);
  });

  for (const [name, src] of [
    ["a runaway loop", "while True:\n    pass\n"],
    [
      "a loop that catches the timeout",
      "while True:\n    try:\n        while True:\n            pass\n    except TimeoutError:\n        pass\n",
    ],
  ]) {
    it(`${name} is stopped at the step time limit, not the activity's, and ends the script with a TimeoutError`, async () => {
      // An hour's start-to-close would let the step run for 48 minutes; the 1 s step limit stops
      // it orders of magnitude sooner on any machine.
      const startToCloseTimeoutMs = 3_600_000;
      const started = Date.now();
      const step = await start(new MockActivityEnvironment({ startToCloseTimeoutMs }), src!, null);
      assertMatch(step, { done: true, error: TIMED_OUT_ERROR });
      assert.ok(Date.now() - started < (startToCloseTimeoutMs * 0.8) / 20);
    });
  }

  it("a step timeout shorter than the time limit still ends the script with a TimeoutError", async () => {
    const env = new MockActivityEnvironment({ startToCloseTimeoutMs: 500 });
    const step = await start(env, "while True:\n    pass\n", null);
    assert.equal(step.error, TIMED_OUT_ERROR);
  });

  it("a script raising TimeoutError itself is an ordinary script error", async () => {
    const step = await start(new MockActivityEnvironment(), "raise TimeoutError('mine')\n", null);
    assert.equal(step.error, "MontyRuntimeError: TimeoutError: mine");
  });

  it("a script sees the workflow's clock and seed, and sleeps through the workflow", async () => {
    const env = new MockActivityEnvironment();
    const clock = { now: Date.UTC(2026, 0, 1, 9, 30), seed: 42 };
    const src = script(
      "    import random, time, os\n    from datetime import datetime\n    before = (datetime.now().isoformat(), random.random(), os.getenv('HOME'))\n    await asyncio.sleep(2.5)\n    try:\n        time.sleep(1)\n    except RuntimeError as e:\n        slept = str(e)\n    return [before, datetime.now().isoformat(), random.random(), slept]",
    );
    let step = await start(env, src, null, clock);
    // Monty numbers the clock and environment calls too, so the sleep is call 2.
    assertMatch(step, {
      done: false,
      awaiting: [2],
      calls: [{ call_id: 2, function_name: "asyncio.sleep", args: [2.5], kwargs: {} }],
    });
    step = await resume(env, {
      snapshot: step.snapshot!,
      results: [{ call_id: 2, return_value: null }],
      type_names: [],
      clock: { ...clock, now: clock.now + 60_000 },
    });
    assert.deepEqual(JSON.parse(step.output_json!), [
      ["2026-01-01T09:30:00", 0.6394267984578837, null],
      "2026-01-01T09:31:00",
      0.025010755222666936,
      "time.sleep is unavailable; use `await asyncio.sleep(...)`",
    ]);
  });

  it("os.urandom is the same for the same seed and call", async () => {
    const src = "import os\n[len(os.urandom(40)), list(os.urandom(8))]\n";
    const run = async (seed: number) =>
      JSON.parse(
        (await start(new MockActivityEnvironment(), src, null, { now: 0, seed })).output_json!,
      ) as [number, number[]];
    const first = await run(1);
    assert.equal(first[0], 40);
    assert.deepEqual(await run(1), first);
    assert.notDeepEqual((await run(2))[1], first[1]);
  });

  it("an OS call the host leaves unhandled is a script error", async () => {
    const step = await start(new MockActivityEnvironment(), "open('/etc/passwd').read()\n", null);
    assert.equal(step.done, true);
    assert.ok(step.error);
  });

  it("a step hands back calls it doesn't await yet, sorted, and a failed call raises its own type", async () => {
    const env = new MockActivityEnvironment();
    const src = script(
      '    later = search(origin="B", n=1)\n    first = await search(origin="A", n=1)\n    try:\n        await later\n    except ValueError as e:\n        return ["caught", str(e), first["items"][0]["id"]]',
    );
    let step = await start(env, src, searchStubs);
    assert.deepEqual(
      step.calls.map((c) => [c.call_id, c.kwargs]),
      [
        [0, { origin: "B", n: 1 }],
        [1, { origin: "A", n: 1 }],
      ],
    );
    // Monty's FutureSnapshot lists every unresolved call, so both resolve before the script resumes.
    assert.deepEqual(step.awaiting, [0, 1]);
    step = await resume(env, {
      snapshot: step.snapshot!,
      results: [
        { call_id: 0, return_value: null, error: "bad origin", error_type: "ValueError" },
        { call_id: 1, return_value: { items: [{ id: "A-0", price: 1 }] } },
      ],
      type_names: searchStubs.type_names,
      clock: CLOCK,
    });
    assert.deepEqual(JSON.parse(step.output_json!), ["caught", "bad origin", "A-0"]);
  });

  it("seals each paused script and refuses one tampered with, sealed under other stubs or another secret, or unsealed", async () => {
    const env = new MockActivityEnvironment({ startToCloseTimeoutMs: 30_000 });
    const acts = codeModeActivities({ continuationSecret: SECRET });
    const paused: CodeBatchStep = await env.run(
      acts.code_start_batch,
      script('    r = await search(origin="SFO", n=1)\n    return r["items"][0]["id"]'),
      searchStubs,
      CLOCK,
    );
    assert.match(paused.snapshot!, /^msig1\./);
    const resumeWith = (
      snapshot: string,
      typeNames: string[] = searchStubs.type_names,
      run = acts.code_resume_batch,
    ): Promise<CodeBatchStep> =>
      env.run(run, {
        snapshot,
        results: [
          { call_id: paused.awaiting[0]!, return_value: { items: [{ id: "SFO-0", price: 1 }] } },
        ],
        type_names: typeNames,
        clock: CLOCK,
      });
    assert.equal(JSON.parse((await resumeWith(paused.snapshot!)).output_json!), "SFO-0");

    const [signature, body] = paused.snapshot!.slice("msig1.".length).split(".") as [
      string,
      string,
    ];
    const dump = Buffer.from(body, "base64");
    dump.writeUInt8(dump.readUInt8(dump.length - 1) ^ 1, dump.length - 1);
    const refusal = { type: "CodeModeSnapshotRefused", nonRetryable: true };
    await assert.rejects(resumeWith(`msig1.${signature}.${dump.toString("base64")}`), refusal);
    await assert.rejects(resumeWith(paused.snapshot!, ["SearchResp", "Other"]), refusal);
    await assert.rejects(resumeWith(body), refusal);
    await assert.rejects(
      resumeWith(
        paused.snapshot!,
        searchStubs.type_names,
        codeModeActivities({ continuationSecret: "another-secret-0123456789abcdefghij" })
          .code_resume_batch,
      ),
      refusal,
    );
    assert.throws(() => codeModeActivities({ continuationSecret: "short" }), /at least 32 bytes/);
  });

  it("bad arguments to a stub type are a script error", async () => {
    const loose = {
      source: "from typing import Any\ndef Box(*args: Any) -> Any: ...\n",
      type_names: ["Box"],
    };
    const step = await start(new MockActivityEnvironment(), "Box(1)\n", loose);
    assert.equal(step.done, true);
    assert.ok(step.error?.includes("TypeError"));
  });

  it("an undefined name raises NameError inside the script, which can catch it", async () => {
    const step = await start(
      new MockActivityEnvironment(),
      "try:\n    nope\nexcept NameError as e:\n    caught = str(e)\ncaught\n",
      null,
    );
    assertMatch(step, { done: true, error: null });
    assert.equal(JSON.parse(step.output_json!), "name 'nope' is not defined");
  });

  it("refuses to run a step directly with a missing or short continuation secret", async () => {
    const env = new MockActivityEnvironment();
    for (const secret of ["", "short"])
      await assert.rejects(
        env.run(codeStartBatch, "1", null, DEFAULT_CODE_MODE_LIMITS, CLOCK, secret),
        /at least 32 bytes/,
      );
    await assert.rejects(
      env.run(
        codeResumeBatch,
        { snapshot: "msig1.x.y", results: [], type_names: [], clock: CLOCK },
        DEFAULT_CODE_MODE_LIMITS,
        "",
      ),
      /at least 32 bytes/,
    );
  });
});

describe("the type check", () => {
  const none = { source: "", type_names: [] };

  it("passes a clean script that raises, loops, sleeps or reads the clock, without waiting on it", async () => {
    await codeTypeCheck("1\n", none);
    const started = Date.now();
    for (const body of [
      "raise ValueError('at run time')",
      "while True:\n    pass",
      "import time\ntime.sleep(3600)",
      "import asyncio\nasyncio.run(asyncio.sleep(3600))",
      "from datetime import datetime\nimport random\nprint(datetime.now(), random.random())",
    ])
      assert.equal(await codeTypeCheck(`${body}\n`, none), "", body);
    // Cut short by the type check's own budget, well inside one step's.
    assert.ok(Date.now() - started < STEP_TIME_LIMIT_SECS * 1000);
  });

  it("reports a type error or a syntax error with the checker's whole report, as Python does", async () => {
    assert.equal(
      await codeTypeCheck('x: int = "a"\n', none),
      'MontyTypingError: error[invalid-assignment]: Object of type `Literal["a"]` is not assignable to `int`\n --> main.py:1:10\n  |\n1 | x: int = "a"\n  |    ---   ^^^ Incompatible value of type `Literal["a"]`\n  |    |\n  |    Declared type\n\n',
    );
    assert.equal(
      await codeTypeCheck("x = (", none),
      "MontyTypingError: error[invalid-syntax]: unexpected EOF while parsing\n --> main.py:1:6\n  |\n1 | x = (\n  |      ^\n\n",
    );
  });
});
