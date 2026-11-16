// ABOUTME: Pins the console's table: a semantic table whose sortable headers say their sort,
// whose rows are named by a row header holding the row's button, drawn in the rows' own order
// until a reader sorts, with the columns in the order the caller's atom holds.

import assert from "node:assert/strict";
import { createAtom } from "@tanstack/svelte-store";
import { createRawSnippet } from "svelte";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import DataTable from "./DataTable.svelte";

const text = (pick) => createRawSnippet((row) => ({ render: () => `<span>${pick(row())}</span>` }));
const rows = [
  { key: "b", name: "Beta", runs: 2 },
  { key: "a", name: "Alpha", runs: 10 }
];
const columns = [
  { id: "name", header: "Agent", value: (row) => row.name, cell: text((row) => row.name), rowHeader: true },
  { id: "runs", header: "Running", value: (row) => row.runs, cell: text((row) => row.runs), align: "end", class: "c-runs", size: 64 },
  { id: "note", header: "Note", value: () => "", cell: text(() => "—"), sortable: false }
];
const draw = (props = {}) =>
  stripComments(
    render(DataTable, {
      props: { rows, columns, rowKey: (row) => row.key, onRowClick() {}, label: "Agents", ...props }
    }).body
  );
const textOf = (html) => [...html.matchAll(/>([^<]*)</g)].map((m) => m[1]).join("").trim();
const headers = (body) => [...body.matchAll(/<th scope="col"[^>]*>[\s\S]*?<\/th>/g)].map((m) => textOf(m[0]));

describe("DataTable", () => {
  it("is a table, named for assistive tech", () => {
    const body = draw();
    assert.match(body, /<table aria-label="Agents"[^>]*>[\s\S]*<thead[^>]*>[\s\S]*<tbody[^>]*>/);
  });

  it("names each column header for its column alone, with aria-sort on those that sort", () => {
    const body = draw();
    assert.match(body, /<th scope="col" aria-label="Agent" class="th-cell[^"]*" aria-sort="none">\s*<span class="th-inner[^"]*">\s*<button type="button" class="th[^"]*" aria-describedby="[^"]+-sort-help">Agent/);
    assert.match(body, /<th scope="col" aria-label="Running" class="th-cell c-runs end[^"]*" aria-sort="none">/);
    assert.match(body, /<th scope="col" aria-label="Note" class="th-cell[^"]*">\s*<span class="th-inner[^"]*">\s*<span class="th[^"]*">Note<\/span>/, "a column that cannot sort keeps a plain label and no aria-sort");
    assert.match(body, /<span id="[^"]+-sort-help" class="visually-hidden[^"]*">Sorts by this column\.<\/span>/, "how to sort is said once, read with the focused header");
  });

  it("names each row with a row header holding its button, in the rows' order", () => {
    const body = draw({ rowProps: (row) => ({ title: `${row.name} agent`, "aria-haspopup": "dialog" }) });
    assert.equal(body.match(/<th scope="row"[^>]*>\s*<button type="button" class="row-button[^"]*"[^>]*aria-haspopup="dialog"/g)?.length, 2);
    assert.match(body, /<tr class="trow[^"]*"[^>]*title="Beta agent"[\s\S]*?Beta[\s\S]*?title="Alpha agent"/, "unsorted, the rows keep the order they came in");
    assert.match(body, /<td class="td c-runs end[^"]*"><span>2<\/span>/, "a column's class and alignment reach its cells");
  });

  it("sizes columns through the colgroup, from the caller's sizing atom when it has one", () => {
    assert.match(draw(), /<colgroup><col\/><col style="width: calc\(64px \+ var\(--gutter\)\);"\/><col\/><\/colgroup>/, "a size plus the gap its cells' padding draws; no size takes the rest");
    assert.doesNotMatch(draw(), /role="separator"/, "without an atom to write to, nothing resizes");
    const columnSizing = createAtom({ runs: 120 });
    const body = draw({ columnSizing });
    assert.match(body, /<col style="width: calc\(120px \+ var\(--gutter\)\);"\/>/);
    assert.equal(body.match(/role="separator"/g)?.length, 1, "only a sized column has a handle");
    assert.match(body, /<div class="resize[^"]*" role="separator" aria-orientation="vertical" aria-label="Resize Running" aria-valuenow="120" aria-valuemin="64" aria-valuemax="192" tabindex="0"/);
  });

  it("draws a row's menu over its last cell, not in a column of its own", () => {
    const body = draw({ rowActions: () => [{ label: "Copy", run() {} }] });
    assert.deepEqual(headers(body), ["Agent", "Running", "Note"], "no header for the menu, so the table ends at the pane's inset");
    assert.equal(body.match(/<col\b/g)?.length, 3);
    assert.match(body, /<td class="td[^"]*"><span>—<\/span><span class="row-actions[^"]*" title="">\s*<button type="button" class="head-button row-more[^"]*" aria-label="More actions"/, "inside the last cell, without the row's title");
  });

  it("names the caller's class on the table's wrapper", () => {
    assert.match(draw({ class: "bay-cols" }), /<div class="data-table bay-cols[^"]*">/);
  });
});
