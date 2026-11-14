import assert from "node:assert/strict";
import test from "node:test";

import { parseListPage, parseOffsetListPage } from "../src/lib/list-response.mjs";

test("accepts a complete page envelope", () => {
  const page = parseListPage({ items: [{ id: 1 }], total: 21, page: 2, size: 10 }, 2, 10);
  assert.equal(page.total, 21);
});

test("does not reinterpret malformed response shapes as zero results", () => {
  for (const value of [
    { total: 0, page: 1, size: 10 },
    { items: [], page: 1, size: 10 },
    { items: [], total: -1, page: 1, size: 10 },
    [],
  ]) {
    assert.throws(() => parseListPage(value, 1, 10));
  }
});

test("validates offset ledgers against the requested slice", () => {
  const page = parseOffsetListPage(
    { items: [{ id: "relation-1" }], total: 31, offset: 20, limit: 20 },
    20,
    20,
  );
  assert.equal(page.total, 31);
  assert.throws(() => parseOffsetListPage({ items: [], total: 0, offset: 0, limit: 20 }, 20, 20));
  assert.throws(() => parseOffsetListPage({ items: [], total: 0, offset: 0 }, 0, 20));
});
