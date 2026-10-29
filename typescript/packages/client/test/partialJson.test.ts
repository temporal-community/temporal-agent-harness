// parsePartialJson against every prefix of a nested reply, as a stream would deliver it.

import assert from "node:assert/strict";
import { test } from "node:test";

import { parsePartialJson } from "../src/partialJson.ts";

const full = JSON.stringify({
  records: [
    { name: 'Aleks "A" W', email: "a@b.c", n: 12, note: "brackets ]} and \\ inside" },
    { name: "Bo", tags: ["x", "y"], ok: true, none: null }
  ],
  done: true
});

test("every prefix of a nested document parses to an object or to nothing yet", () => {
  for (let i = 1; i <= full.length; i++) {
    const value = parsePartialJson(full.slice(0, i));
    assert.ok(value === undefined || (typeof value === "object" && value !== null), `prefix ${i}: ${JSON.stringify(value)}`);
    if (i > 1) assert.notEqual(value, undefined, `prefix ${i} parsed to nothing`);
  }
  assert.deepEqual(parsePartialJson(full), JSON.parse(full));
});

test("closes open strings and brackets, and drops what is half-written", () => {
  assert.deepEqual(parsePartialJson('{"records":[{"name":"Ale'), { records: [{ name: "Ale" }] });
  assert.deepEqual(parsePartialJson('{"records":[{"name":"Bo"},'), { records: [{ name: "Bo" }] });
  assert.deepEqual(parsePartialJson('{"rec'), {});
  assert.deepEqual(parsePartialJson('{"ok":tr'), {});
  assert.deepEqual(parsePartialJson('{"n":12,"tags":["x","y'), { n: 12, tags: ["x"] });
  assert.equal(parsePartialJson(""), undefined);
});
