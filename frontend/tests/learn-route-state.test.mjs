import assert from "node:assert/strict";
import test from "node:test";

import {
  getLearnHydrationSearch,
  parseMapFocus,
} from "../src/lib/learn-route-state.mjs";

test("parses the requested challenge as the initial map focus", () => {
  assert.deepEqual(
    parseMapFocus("view=map&focus_type=challenge&focus_id=3"),
    { type: "challenge", id: "3" },
  );
});

test("rejects incomplete or unsupported map focus queries", () => {
  assert.equal(parseMapFocus("view=map&focus_type=challenge"), null);
  assert.equal(parseMapFocus("view=map&focus_type=event&focus_id=3"), null);
  assert.equal(parseMapFocus(`focus_type=challenge&focus_id=${"x".repeat(65)}`), null);
});

test("does not let the previous browser URL replace a committed route query", () => {
  assert.equal(
    getLearnHydrationSearch(
      "view=map&focus_type=challenge&focus_id=3",
      "/ctf/ko/learn",
      "?category=unreal",
    ),
    null,
  );
});

test("uses the current Learn URL only to supplement an empty hydration query", () => {
  const currentSearch = "?view=map&focus_type=challenge&focus_id=3";
  assert.equal(getLearnHydrationSearch("", "/ctf/ko/learn", currentSearch), currentSearch);
  assert.equal(getLearnHydrationSearch("", "/ctf/ko/learn/", ""), "");
  assert.equal(getLearnHydrationSearch("", "/ctf/ko/wargame/3", currentSearch), null);
});
