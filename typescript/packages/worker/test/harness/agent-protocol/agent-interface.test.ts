// The input models parse what pydantic's lax mode parses on the Python side: booleans from 0/1
// and the strings pydantic reads as booleans, numbers from numeric strings and booleans.

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import fc from "fast-check";
import {
  AgentConfigSchema,
  AutoApprovalCriteria,
  ToolApprovalPolicy,
} from "@temporalio/agent-harness/harness/agent-protocol";

const TRUE_VALUES = [
  true,
  1,
  "true",
  "TRUE",
  "True",
  "1",
  "yes",
  "YES",
  "on",
  "On",
  "t",
  "T",
  "y",
  "Y",
];
const FALSE_VALUES = [
  false,
  0,
  "false",
  "FALSE",
  "0",
  "no",
  "No",
  "off",
  "OFF",
  "f",
  "F",
  "n",
  "N",
];
const NOT_BOOLEANS = [2, -1, 0.5, "", " true", "true ", "1.0", "maybe", "tru", null, [], {}];

const BOOL_FIELDS = [
  "dangerously_skip_all_approvals",
  "auto_approve_inherently_safe",
  "auto_mode_enabled",
] as const;

describe("ToolApprovalPolicy booleans", () => {
  for (const field of BOOL_FIELDS) {
    it(`${field} reads every value pydantic reads as true or false`, () => {
      for (const value of TRUE_VALUES)
        assert.equal(ToolApprovalPolicy.parse({ [field]: value })[field], true, String(value));
      for (const value of FALSE_VALUES)
        assert.equal(ToolApprovalPolicy.parse({ [field]: value })[field], false, String(value));
    });

    it(`${field} rejects what pydantic rejects`, () => {
      for (const value of NOT_BOOLEANS)
        assert.throws(
          () => ToolApprovalPolicy.parse({ [field]: value }),
          Error,
          JSON.stringify(value),
        );
    });
  }

  it("checks the cross-field invariant on the coerced values", () => {
    assert.throws(() =>
      ToolApprovalPolicy.parse({ dangerously_skip_all_approvals: "yes", auto_mode_enabled: 1 }),
    );
    assert.deepStrictEqual(
      ToolApprovalPolicy.parse({ dangerously_skip_all_approvals: "on", auto_mode_enabled: "off" }),
      ToolApprovalPolicy.dangerouslySkipAll(),
    );
  });

  it("matches a case-insensitive word list for any string", () => {
    const words = new Map([
      ...["true", "1", "yes", "on", "t", "y"].map((w) => [w, true] as const),
      ...["false", "0", "no", "off", "f", "n"].map((w) => [w, false] as const),
    ]);
    fc.assert(
      fc.property(fc.string({ maxLength: 6 }), (s) => {
        const expected = words.get(s.toLowerCase());
        const parsed = (() => {
          try {
            return ToolApprovalPolicy.parse({ auto_approve_inherently_safe: s })
              .auto_approve_inherently_safe;
          } catch {
            return undefined;
          }
        })();
        assert.equal(parsed, expected);
      }),
    );
  });
});

const NUMBERS: [unknown, number][] = [
  [0.5, 0.5],
  ["0.5", 0.5],
  [" 0.5 ", 0.5],
  ["1e-1", 0.1],
  [".5", 0.5],
  ["1.", 1],
  ["+0.25", 0.25],
  ["0_0.5", 0.5],
  ["1", 1],
  [true, 1],
  [false, 0],
];
const NOT_NUMBERS = ["", "  ", "half", "0x1", "1__0", "_1", "1_", "nan", "inf", [], {}];
const OUT_OF_RANGE = [1.5, -0.1, "2", "-1", "1e400"];

describe("AutoApprovalCriteria thresholds", () => {
  const parsers: [string, (value: unknown) => number | null][] = [
    [
      "criteria min_confidence",
      (v) => AutoApprovalCriteria.parse({ min_confidence: v }).min_confidence,
    ],
    [
      "criteria escalate_if_irreversible_above",
      (v) =>
        AutoApprovalCriteria.parse({ escalate_if_irreversible_above: v })
          .escalate_if_irreversible_above,
    ],
    [
      "set min_confidence",
      (v) => AutoApprovalCriteria.criteriaSet({ min_confidence: v as number }).min_confidence,
    ],
    [
      "set escalate_if_irreversible_above",
      (v) =>
        AutoApprovalCriteria.criteriaSet({ escalate_if_irreversible_above: v as number })
          .escalate_if_irreversible_above,
    ],
  ];
  for (const [name, parse] of parsers) {
    it(`${name} reads numbers, numeric strings and booleans`, () => {
      for (const [value, expected] of NUMBERS) assert.equal(parse(value), expected, String(value));
    });

    it(`${name} rejects non-numbers and values outside [0, 1]`, () => {
      for (const value of [...NOT_NUMBERS, ...OUT_OF_RANGE])
        assert.throws(() => parse(value), Error, JSON.stringify(value));
    });
  }

  it("keeps the defaults and the nullable fields' null", () => {
    const c = AutoApprovalCriteria.parse({ escalate_if_irreversible_above: null });
    assert.equal(c.min_confidence, 0.8);
    assert.equal(c.escalate_if_irreversible_above, null);
    assert.equal(AutoApprovalCriteria.criteriaSet({ min_confidence: null }).min_confidence, null);
    assert.throws(() => AutoApprovalCriteria.parse({ min_confidence: null }));
  });

  it("coerces inside named sets", () => {
    const c = AutoApprovalCriteria.parse({ sets: { s: { min_confidence: "0.9" } } });
    assert.equal(c.sets.s!.min_confidence, 0.9);
  });
});

describe("AgentConfig", () => {
  it("coerces the policy and criteria it carries", () => {
    const config = AgentConfigSchema.parse({
      approval_policy: { auto_approve_inherently_safe: "yes", auto_mode_enabled: "1" },
      auto_approval_criteria: { min_confidence: "0.7", escalate_if_irreversible_above: false },
    });
    assert.deepStrictEqual(
      config.approval_policy,
      ToolApprovalPolicy.autoMode({ preApproveInherentlySafe: true }),
    );
    assert.equal(config.auto_approval_criteria?.min_confidence, 0.7);
    assert.equal(config.auto_approval_criteria?.escalate_if_irreversible_above, 0);
  });
});
