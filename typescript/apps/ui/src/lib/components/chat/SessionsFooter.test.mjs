// ABOUTME: Pins the Sessions sidebar's footer: one button opening the console's links to the
// project, the resources only where the home desk's status line isn't already showing them.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import { RESOURCES } from "./HomeView.svelte";
import SessionsFooter from "./SessionsFooter.svelte";

const draw = (props = {}) => stripComments(render(SessionsFooter, { props }).body);
const links = (body) => [...body.matchAll(/<a href="([^"]+)"/g)].map((match) => match[1]);

describe("the Sessions sidebar's footer", () => {
  it("is one button that opens a menu of links", () => {
    const body = draw();
    assert.match(body, /<footer class="sessions-footer[^"]*">\s*<button(?=[^>]*class="head-button)(?=[^>]*aria-label="Links and help")(?=[^>]*popovertarget="[^"]+-menu")[^>]*>/);
    assert.match(body, /<div id="[^"]+-menu" class="footer-menu[^"]*" popover="auto">/);
  });

  it("lists the releases and issues, then every resource, with a session open", () => {
    const hrefs = links(draw());
    assert.match(hrefs[0], /\/releases$/);
    assert.match(hrefs[1], /\/issues\/new$/);
    assert.deepEqual(hrefs.slice(2), RESOURCES.map((link) => link.href), "the status line's own list, not a copy");
  });

  it("leaves the resources to the status line when the home desk shows it", () => {
    const hrefs = links(draw({ resourcesShown: true }));
    assert.equal(hrefs.length, 2);
    for (const link of RESOURCES) assert.ok(!hrefs.includes(link.href), `${link.name} shown twice`);
  });
});
