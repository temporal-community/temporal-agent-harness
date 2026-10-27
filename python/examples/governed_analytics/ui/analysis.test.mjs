// The studio's readers of the agent's stream (analysis.js), on the shapes the agent publishes.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  chartProblem,
  dataOrigin,
  highlightPython,
  highlightSql,
  markdown,
  niceTicks,
  parsePyRepr,
  policyOf,
  readReport,
  readResult,
  readScriptOutput,
  reportRoute,
  reportUrl,
  splitAnswer,
  tableKind
} from "./analysis.js";

test("a pydantic model's str() reads as its fields", () => {
  // A SQL tool's result: its data's origin first (the fields it inherits), then its own.
  const text = `tables_read=['analytics.branch_deposits'] restricted_data=False columns=['branch_name', 'drop'] rows=[["O'Hare", -0.31], ['Main \\'St\\'', None], ['x', 1e-05]] truncated=False`;
  assert.deepEqual(parsePyRepr(text), {
    tables_read: ["analytics.branch_deposits"],
    restricted_data: false,
    columns: ["branch_name", "drop"],
    rows: [["O'Hare", -0.31], ["Main 'St'", null], ["x", 1e-5]],
    truncated: false
  });
});

test("nested models, lists of models, dicts and special floats read as data", () => {
  assert.deepEqual(parsePyRepr("tables_read=['risk.loan_applications'] restricted_data=True column='amount' count=3 bins=[HistogramBin(lower=0.0, upper=1.5, count=2)]"), {
    tables_read: ["risk.loan_applications"],
    restricted_data: true,
    column: "amount",
    count: 3,
    bins: [{ lower: 0, upper: 1.5, count: 2 }]
  });
  assert.deepEqual(parsePyRepr("[TableInfo(database='analytics', table='branches', rows=40), Info(x=1)]"), [
    { database: "analytics", table: "branches", rows: 40 },
    { x: 1 }
  ]);
  assert.deepEqual(parsePyRepr("{'chart_id': 'abc', 'n': (1, 2)}"), { chart_id: "abc", n: [1, 2] });
  assert.deepEqual(parsePyRepr("welch_t=inf welch_df=None group='a\\nb'"), { welch_t: Infinity, welch_df: null, group: "a\nb" });
});

test("readResult takes JSON first, a repr next, and gives up on anything else", () => {
  assert.deepEqual(readResult('{"a": [1, 2]}'), { a: [1, 2] });
  assert.deepEqual(readResult("chart_id='t1'"), { chart_id: "t1" });
  assert.equal(readResult("not a value ("), undefined);
  assert.equal(readResult(null), undefined);
});

test("a script's output splits into what it printed and what it returned", () => {
  assert.deepEqual(readScriptOutput("hello\nresult: 'six branches\\ndropped'"), {
    printed: "hello",
    result: "six branches\ndropped",
    error: null
  });
  assert.equal(readScriptOutput("Script error (NameError: x)").error, "NameError: x");
});

test("a denied call reads as denied, with the query's facts and its table checks", () => {
  const view = policyOf({
    state: "denied",
    approval: { approved: false, reason: "Risk tables need a person to grant access" },
    evaluations: [
      {
        status: "ended",
        verdict: "deny",
        reason: "Risk tables need a person to grant access",
        error: null,
        details: {
          action: "bank-analyst/run_sql",
          parameter: { statement: "SELECT", tables: ["risk.loan_applications"], table_functions: [] },
          checks: [{ action: "bank-analyst/read_table", parameter: { table: "risk.loan_applications", tool: "run_sql" } }],
          decision: "DECISION_DENIED"
        }
      }
    ]
  });
  assert.equal(view.verdict, "denied");
  assert.deepEqual(view.tables, ["risk.loan_applications"]);
  assert.deepEqual(view.checks, [{ action: "bank-analyst/read_table", table: "risk.loan_applications" }]);
  assert.equal(view.reason, "Risk tables need a person to grant access");
});

test("a call still being judged, or left to a person, says so", () => {
  const running = { status: "running", verdict: null, reason: null, details: {}, error: null };
  assert.equal(policyOf({ state: "evaluating", approval: null, evaluations: [running] }).verdict, "checking");
  const failed = { status: "error", verdict: null, reason: null, details: {}, error: "endpoint not found" };
  const view = policyOf({ state: "awaiting_approval", approval: null, evaluations: [failed] });
  assert.equal(view.verdict, "needs you");
  assert.equal(view.reason, "endpoint not found");
});

test("a SQL result says which tables it read and whether any is restricted", () => {
  assert.deepEqual(dataOrigin(readResult("tables_read=['risk.credit_scores', 'analytics.branches'] restricted_data=True columns=[] rows=[] truncated=False")), {
    tablesRead: ["risk.credit_scores", "analytics.branches"],
    restricted: true
  });
  assert.equal(dataOrigin(readResult("[TableInfo(database='analytics', table='branches', rows=40)]")), null);
});

test("a publish_report call reads as a report, or says what is missing", () => {
  const { report } = readReport({ title: "Q3 deposits", summary: "Down 30%.", sections: [{ heading: "Branches", body: "[chart:c1]" }] });
  assert.deepEqual(report, { title: "Q3 deposits", summary: "Down 30%.", sections: [{ heading: "Branches", body: "[chart:c1]" }] });
  assert.match(readReport({ title: "", sections: [] }).problem ?? "", /title/);
  assert.match(readReport({ title: "x", sections: [{ heading: "h" }] }).problem ?? "", /body/);
});

test("a report's link carries its session and routes back to the report", () => {
  const page = { origin: "http://127.0.0.1:5173", pathname: "/", search: "?api=http%3A%2F%2Fh%2Fapi%2F&session=old" };
  const url = reportUrl(page, "s 1", "tool/1");
  assert.equal(url, "http://127.0.0.1:5173/?api=http%3A%2F%2Fh%2Fapi%2F&session=s+1#/reports/tool%2F1");
  assert.equal(reportRoute(new URL(url).hash), "tool/1");
  assert.equal(reportRoute(""), null);
  assert.equal(reportRoute("#/other"), null);
});

test("links render and open in a new tab, a report's public link included", () => {
  assert.equal(
    markdown("Read it at http://127.0.0.1:5173/?session=s#/reports/t1."),
    '<p>Read it at <a href="http://127.0.0.1:5173/?session=s#/reports/t1" target="_blank" rel="noreferrer">http://127.0.0.1:5173/?session=s#/reports/t1</a>.</p>'
  );
  assert.equal(markdown("[docs](https://example.com/a)"), '<p><a href="https://example.com/a" target="_blank" rel="noreferrer">docs</a></p>');
});

test("tables are open, restricted or PII by database", () => {
  assert.equal(tableKind("analytics.branches"), "open");
  assert.equal(tableKind("risk.credit_scores"), "restricted");
  assert.equal(tableKind("customer_pii.customers"), "pii");
});

test("chart data that can't be drawn is refused with a reason the agent can act on", () => {
  assert.equal(chartProblem("render_bar_chart", { categories: ["a", "b"], series: [{ name: "s", values: [1, 2] }] }), null);
  assert.match(chartProblem("render_bar_chart", { categories: ["a", "b"], series: [{ name: "s", values: [1] }] }) ?? "", /1 values for 2 categories/);
  assert.equal(chartProblem("render_line_chart", { x_values: ["Q1", "Q2"], series: [{ name: "s", values: [1, 2] }] }), null);
  assert.equal(chartProblem("render_pie_chart", { labels: ["a"], values: [1] }), null);
  assert.match(chartProblem("render_pie_chart", { labels: ["a"], values: [-1] }) ?? "", /negative/);
  assert.equal(chartProblem("render_scatter_chart", { points: [{ x: 1, y: 2 }] }), null);
  assert.match(chartProblem("render_scatter_chart", { points: [{ x: "1", y: 2 }] }) ?? "", /numeric/);
});

test("an answer splits around the charts it places on their own lines", () => {
  assert.deepEqual(splitAnswer("Deposits fell.\n\n[chart:tool-1]\n\nDefaults rose.\n`[chart: tool-2]`"), [
    { type: "text", text: "Deposits fell.\n" },
    { type: "chart", id: "tool-1" },
    { type: "text", text: "\nDefaults rose." },
    { type: "chart", id: "tool-2" }
  ]);
  assert.deepEqual(splitAnswer("see [chart:x] inline"), [{ type: "text", text: "see [chart:x] inline" }]);
});

test("ticks are round numbers covering the range", () => {
  assert.deepEqual(niceTicks(0, 97), [0, 20, 40, 60, 80, 100]);
  assert.deepEqual(niceTicks(-0.3, 0.1, 4), [-0.3, -0.2, -0.1, 0, 0.1]);
  assert.deepEqual(niceTicks(5, 5), [0, 1, 2, 3, 4, 5]);
});

test("Markdown is escaped, and tables render", () => {
  assert.equal(markdown("<b>x</b>"), "<p>&lt;b&gt;x&lt;/b&gt;</p>");
  assert.equal(
    markdown("| Branch | Drop |\n|---|---|\n| **Elm** | 31% |"),
    "<table><thead><tr><th>Branch</th><th>Drop</th></tr></thead><tbody><tr><td><b>Elm</b></td><td>31%</td></tr></tbody></table>"
  );
});

test("without highlight.js, code is only escaped", () => {
  assert.equal(highlightSql("select a from t where b = 'x<y'"), "select a from t where b = 'x<y'".replace("<", "&lt;"));
  assert.equal(highlightPython('rows = await run_sql("SELECT 1 < 2")'), 'rows = await run_sql("SELECT 1 &lt; 2")');
});

test("a script's queries are highlighted as SQL, and its awaited host calls are marked", () => {
  // highlight.js's markup for the script, and a stand-in for its SQL highlighting.
  const python = [
    '<span class="hljs-keyword">await</span> run_sql(<span class="hljs-string">&quot;&quot;&quot;SELECT &#x27;a&lt;b&#x27;&quot;&quot;&quot;</span>)',
    '<span class="hljs-keyword">await</span> helper(<span class="hljs-string">&quot;amount&quot;</span>)'
  ].join("\n");
  /** @type {string[]} */
  const queries = [];
  globalThis.hljs = {
    highlight(code, { language }) {
      if (language === "sql") queries.push(code);
      return { value: language === "sql" ? "<i>sql</i>" : python };
    }
  };
  try {
    assert.equal(
      highlightPython("ignored"),
      [
        '<span class="hljs-keyword">await</span> <span class="host-call">run_sql</span>(<span class="hljs-string">&quot;&quot;&quot;</span><span class="embedded-sql"><i>sql</i></span><span class="hljs-string">&quot;&quot;&quot;</span>)',
        '<span class="hljs-keyword">await</span> helper(<span class="hljs-string">&quot;amount&quot;</span>)'
      ].join("\n")
    );
    assert.deepEqual(queries, ["SELECT 'a<b'"]);
  } finally {
    delete globalThis.hljs;
  }
});
