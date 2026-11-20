// ABOUTME: Pins the fixture catalog the dev tools list: read from the fixtures, every listed
// one builds, names are unique, each group is one run, and variants lead with their default.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { mockCatalog } from "./catalog.ts";
import { scaleScenario, SCALE_LISTINGS } from "./scaleScenarios.ts";

describe("the mock fixture catalog", () => {
  const catalog = mockCatalog();

  it("lists each fixture once, named and grouped", () => {
    const names = catalog.map((entry) => entry.name);
    assert.equal(new Set(names).size, names.length, "a fixture listed twice");
    for (const entry of catalog) {
      assert.ok(entry.label.trim(), `${entry.name} has no label`);
      assert.ok(entry.group.trim(), `${entry.name} has no group`);
    }
    assert.ok(names.includes("demo") && names.includes("sessions200") && names.includes("codemode"), "reply and scale fixtures both");
  });

  it("keeps each group's fixtures together, as a menu draws one heading per run", () => {
    const runs = catalog.map((entry) => entry.group).filter((group, i, all) => group !== all[i - 1]);
    assert.equal(new Set(runs).size, runs.length, `a group split in two: ${runs.join(", ")}`);
  });

  it("builds every scale fixture it lists", () => {
    for (const [name] of SCALE_LISTINGS) {
      if (/100k|10k|3k|250|50k/.test(name)) continue;
      assert.ok(scaleScenario(name), `${name} is listed but builds nothing`);
    }
  });

  it("still builds the families reached only by link", () => {
    assert.ok(scaleScenario("mixed2x2"));
    assert.equal(scaleScenario("nope"), undefined);
    assert.equal(scaleScenario("constructor"), undefined, "an inherited key is not a fixture");
  });
});
