/* Every `?mock=` fixture the dev tools can list, read from the fixtures themselves: a reply
   fixture by its key and its session's label, a scale fixture by the listing beside its builder. */
import { JSON_REPLY_GROUP, jsonReplyScenarios } from "./jsonReplyScenarios";
import { SCALE_LISTINGS, type ScenarioListing } from "./scaleScenarios";

export type MockListing = ScenarioListing & { name: string };

export function mockCatalog(): MockListing[] {
  const replies = Object.entries(jsonReplyScenarios()).map(([name, scenario]) => ({
    name,
    label: scenario.sessions[0]?.label ?? name,
    group: JSON_REPLY_GROUP
  }));
  const all = [...replies, ...SCALE_LISTINGS.map(([name, listing]) => ({ name, ...listing }))];
  /* A menu draws one heading per run of a group, so each group's fixtures sit together, in the
     order its first one appears. */
  const groups = [...new Set(all.map((entry) => entry.group))];
  return all.sort((a, b) => groups.indexOf(a.group) - groups.indexOf(b.group));
}
