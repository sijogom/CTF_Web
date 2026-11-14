import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_SITE_NAME,
  createLatestRequestTracker,
  normalizeSiteName,
  titleWithSiteName,
} from "../src/lib/site-settings.mjs";

test("normalizes only a small non-empty public site name", () => {
  assert.equal(normalizeSiteName("  Security Lab  "), "Security Lab");
  assert.equal(normalizeSiteName("   "), DEFAULT_SITE_NAME);
  assert.equal(normalizeSiteName("x".repeat(101)), DEFAULT_SITE_NAME);
  assert.equal(normalizeSiteName({ secret: true }), DEFAULT_SITE_NAME);
});

test("keeps a page-specific title while replacing the configured brand", () => {
  assert.equal(titleWithSiteName("CUHA Memory", "Security Lab"), "Security Lab");
  assert.equal(
    titleWithSiteName("Course details | CUHA Memory", "Security Lab"),
    "Course details | Security Lab",
  );
  assert.equal(
    titleWithSiteName("Course details", "Security Lab"),
    "Course details | Security Lab",
  );
  assert.equal(
    titleWithSiteName("Course details | Old Lab", "Security Lab", "Old Lab"),
    "Course details | Security Lab",
  );
  assert.equal(
    titleWithSiteName("CUHA Memory 2026", "CUHA Memory 2026"),
    "CUHA Memory 2026",
  );
  assert.equal(
    titleWithSiteName("Course details | CUHA Memory 2026", "CUHA Memory 2026"),
    "Course details | CUHA Memory 2026",
  );
  assert.equal(
    titleWithSiteName("CUHA Memory 2026", "CUHA Memory", "CUHA Memory 2026"),
    "CUHA Memory",
  );
  assert.equal(
    titleWithSiteName("About CUHA Memory", "Security Lab"),
    "About CUHA Memory | Security Lab",
  );

  const first = titleWithSiteName("Course details | CUHA Memory", "CUHA Memory 2026");
  const second = titleWithSiteName(first, "CUHA Memory 2026", "CUHA Memory");
  assert.equal(first, "Course details | CUHA Memory 2026");
  assert.equal(second, first);
  assert.equal(
    titleWithSiteName("학교 | CTF", "CTF", "학교 | CTF"),
    "CTF",
  );
  assert.equal(
    titleWithSiteName("학습 | 학교 | CTF", "CTF", "학교 | CTF"),
    "학습 | CTF",
  );
});

test("a newer settings request aborts and rejects the delayed older response", () => {
  const tracker = createLatestRequestTracker();
  const initial = tracker.begin();
  const afterSave = tracker.begin();

  assert.equal(initial.controller.signal.aborted, true);
  assert.equal(tracker.isCurrent(initial), false);
  assert.equal(tracker.isCurrent(afterSave), true);

  tracker.clear(afterSave);
  assert.equal(tracker.isCurrent(afterSave), false);
});
