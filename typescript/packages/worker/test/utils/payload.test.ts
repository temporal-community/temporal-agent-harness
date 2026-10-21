// Oversized payloads: a stream poll base64-encodes its items, so a 1.6 MB value anywhere in a turn
// used to push a poll result past Temporal's 2 MB limit and fail every later workflow task. With
// external storage (payload-storage.ts) each of these completes and the session stays usable.

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import {
  chmodSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  statSync,
  truncateSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { S3Client } from "@aws-sdk/client-s3";
import { defaultPayloadConverter } from "@temporalio/common";
import { NativeConnection, Worker } from "@temporalio/worker";
import { after, before, describe, it } from "node:test";
import { localFileStorageDriver, localPayloadStorage } from "../../src/utils/large-payload.ts";
import { s3PayloadStorage } from "../../src/utils/s3.ts";
import { AgentHarnessPlugin } from "../../src/index.ts";
import { assertMatch } from "../match.ts";
import { collect, replyText, send, startEnv, type TestEnv } from "../support.ts";

const MB = 1_000_000;

let env: TestEnv;
before(async () => {
  env = await startEnv("payload");
});
after(async () => {
  await env.stop();
});

describe("the local-file driver", () => {
  it("round-trips a payload by content hash and refuses a key outside its directory or a tampered file", async () => {
    const dir = mkdtempSync(join(tmpdir(), "offload-"));
    const driver = localFileStorageDriver(dir);
    const payload = defaultPayloadConverter.toPayload({ big: "x".repeat(100) });
    const [claim] = await driver.store({}, [payload, payload]);
    assertMatch(claim!.claimData, {
      hash_algorithm: "sha256",
      key: `${claim!.claimData.hash_value}.bin`,
    });
    assert.deepStrictEqual(readdirSync(dir), [claim!.claimData.key]);
    const [back] = await driver.retrieve({}, [claim!]);
    assert.deepStrictEqual(defaultPayloadConverter.fromPayload(back!), { big: "x".repeat(100) });
    await assert.rejects(
      driver.retrieve({}, [{ claimData: { key: "../etc/passwd" } }]),
      /invalid storage claim key/,
    );
    await assert.rejects(
      driver.retrieve({}, [{ claimData: { ...claim!.claimData, hash_value: "0".repeat(64) } }]),
      /integrity check failed/,
    );
  });

  it("touches a payload stored again, replaces a truncated one, and keeps both to its owner", async () => {
    const dir = join(mkdtempSync(join(tmpdir(), "offload-")), "store");
    const driver = localFileStorageDriver(dir);
    const payload = defaultPayloadConverter.toPayload("x".repeat(1000));
    const [claim] = await driver.store({}, [payload]);
    const path = join(dir, claim!.claimData.key!);
    const lastYear = new Date(Date.now() - 365 * 86_400_000);
    utimesSync(path, lastYear, lastYear);
    await driver.store({}, [payload]);
    assert.ok(statSync(path).mtimeMs > Date.now() - 60_000);
    const size = statSync(path).size;
    truncateSync(path, 10);
    await driver.store({}, [payload]);
    assert.equal(statSync(path).size, size);
    assert.equal(statSync(dir).mode & 0o777, 0o700);
    assert.equal(statSync(path).mode & 0o777, 0o600);
  });

  it("fails a missing or corrupt payload for good, and removes the corrupt one so it can be stored again", async () => {
    const dir = mkdtempSync(join(tmpdir(), "offload-"));
    const driver = localFileStorageDriver(dir);
    const payload = defaultPayloadConverter.toPayload("y".repeat(1000));
    const [claim] = await driver.store({}, [payload]);
    const path = join(dir, claim!.claimData.key!);
    const bytes = readFileSync(path);
    bytes.writeUInt8(bytes.readUInt8(bytes.length - 1) ^ 1, bytes.length - 1);
    writeFileSync(path, bytes);
    await assert.rejects(
      driver.retrieve({}, [claim!]),
      (e) => (
        assertMatch(e, {
          type: "PayloadCorrupt",
          nonRetryable: true,
        }),
        true
      ),
    );
    await assert.rejects(
      driver.retrieve({}, [claim!]),
      (e) => (
        assertMatch(e, {
          type: "PayloadMissing",
          nonRetryable: true,
        }),
        true
      ),
    );
    await driver.store({}, [payload]);
    assert.equal(
      defaultPayloadConverter.fromPayload((await driver.retrieve({}, [claim!]))[0]!),
      "y".repeat(1000),
    );
  });

  it("fails a worker at startup when its directory isn't writable", async () => {
    const dir = mkdtempSync(join(tmpdir(), "offload-"));
    chmodSync(dir, 0o500);
    const connection = await NativeConnection.connect({ address: env.address });
    try {
      const created = Worker.create({
        connection,
        namespace: env.namespace,
        taskQueue: `unwritable-${randomUUID()}`,
        plugins: [
          new AgentHarnessPlugin({
            largePayloadOffload: localPayloadStorage({ baseDir: join(dir, "store") }),
          }),
        ],
      });
      await assert.rejects(created, /isn't writable/);
    } finally {
      chmodSync(dir, 0o700);
      await connection.close();
    }
  });
});

describe("s3PayloadStorage", () => {
  // Just enough of S3 for the driver: HEAD, PUT and GET of path-style objects.
  const objects = new Map<string, Buffer>();
  const server = createServer((req, res) => {
    const key = decodeURIComponent(req.url!.split("?")[0]!);
    if (req.method === "PUT") {
      const chunks: Buffer[] = [];
      req
        .on("data", (c: Buffer) => chunks.push(c))
        .on("end", () => {
          objects.set(key, Buffer.concat(chunks));
          res.writeHead(200).end();
        });
      return;
    }
    const body = objects.get(key);
    if (!body)
      return void res
        .writeHead(404, { "Content-Type": "application/xml" })
        .end(req.method === "HEAD" ? undefined : "<Error><Code>NoSuchKey</Code></Error>");
    res
      .writeHead(200, { "Content-Length": body.length })
      .end(req.method === "HEAD" ? undefined : body);
  });
  let endpoint: string;
  before(async () => {
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    endpoint = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });
  after(() => {
    server.close();
  });

  it("round-trips a payload through the bucket with Python's driver name and claim keys", async () => {
    const client = new S3Client({
      endpoint,
      region: "us-east-1",
      forcePathStyle: true,
      credentials: { accessKeyId: "test", secretAccessKey: "test" },
      requestChecksumCalculation: "WHEN_REQUIRED",
      responseChecksumValidation: "WHEN_REQUIRED",
    });
    const storage = s3PayloadStorage({ bucket: "harness", client });
    const [driver] = storage.drivers;
    assertMatch(driver, { name: "aws.s3driver", type: "aws.s3driver" });
    assert.equal(storage.payloadSizeThreshold, 1_500_000);
    const payload = defaultPayloadConverter.toPayload({ big: "z".repeat(2 * MB) });
    const target = { kind: "workflow", namespace: "ns", id: "wf-1", type: "Agent" } as const;
    const [claim] = await driver!.store({ target }, [payload]);
    assert.deepStrictEqual(Object.keys(claim!.claimData).sort(), [
      "bucket",
      "hash_algorithm",
      "hash_value",
      "key",
    ]);
    assert.equal(objects.has(`/harness/${claim!.claimData.key}`), true);
    const [back] = await driver!.retrieve({}, [claim!]);
    assert.deepStrictEqual(defaultPayloadConverter.fromPayload(back!), { big: "z".repeat(2 * MB) });
  });
});

describe("oversized payloads on a running agent", () => {
  const pyHold = `import asyncio\nasync def main():\n    big = "z" * ${3 * MB}\n    r = await big_result(n=1)\n    return len(big) + len(r)\nasyncio.run(main())`;
  for (const [name, text, check] of [
    ["a 1.6 MB reply", `reply:${1.6 * MB}`, (t: string) => assert.equal(t.length, 1.6 * MB)],
    ["a 3 MB reply", `reply:${3 * MB}`, (t: string) => assert.equal(t.length, 3 * MB)],
    [
      "a 3 MB activity tool result",
      `tool:${3 * MB}`,
      (t: string) => assert.equal(t, `len:${3 * MB}`),
    ],
    [
      "a 1.6 MB user message",
      "m".repeat(1.6 * MB),
      (t: string) => assert.equal(t, `len:${1.6 * MB}`),
    ],
    [
      "a 3 MB value held across a Monty host call",
      `py:${pyHold}`,
      (t: string) => assert.equal(t, `result: ${3 * MB + 1}`),
    ],
  ] as const)
    it(`${name} completes with a reader attached, and the session stays usable`, async () => {
      const h = await env.start("PayloadProbeAgent");
      await send(h, "run", { text });
      check(replyText(await collect(env.client, h.workflowId)));
      await send(h, "run", { text: "still here" });
      const events = await collect(env.client, h.workflowId, {
        until: (e) => e.event.type === "turn_end" && e.turn_number === 2,
      });
      assertMatch(events.at(-2)?.event, {
        type: "message_handler_end",
        output: { text: "len:10" },
      });
    });
});
