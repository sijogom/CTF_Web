import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { readTagDeletionPreview } from "../src/lib/knowledge-tag-deletion.mjs";

const valid = () => ({
  tag: { id: 42, name: "Web" },
  counts: { challenge: 2, course: 1, lesson: 3, children: 4, relations: 5, proposals: 6, audit_events: 7 },
  confirmation: "a".repeat(64),
});

test("deletion preview preserves the selected tag and exact impact counts", () => {
  const input = valid();
  assert.deepEqual(readTagDeletionPreview(input, 42), input);
  assert.throws(() => readTagDeletionPreview(input, 43));
  input.tag.name = " ";
  assert.throws(() => readTagDeletionPreview(input, 42));
});

test("invalid, missing and negative impact counts cannot enable deletion", () => {
  for (const key of Object.keys(valid().counts)) {
    for (const invalid of [-1, 1.5, "2", null, undefined, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
      const input = valid();
      input.counts[key] = invalid;
      assert.throws(() => readTagDeletionPreview(input, 42));
    }
  }
  for (const confirmation of ["", "a".repeat(63), "A".repeat(64), "z".repeat(64), null]) {
    assert.throws(() => readTagDeletionPreview({ ...valid(), confirmation }, 42));
  }
});

test("UI asks for a fresh impact preview and reads uncertain outcomes before retry", () => {
  const source = readFileSync(new URL("../src/app/[locale]/admin/tags/page.tsx", import.meta.url), "utf8");
  const deletion = source.slice(source.indexOf("const confirmDelete ="), source.indexOf("const autoSlug ="));
  assert.equal((deletion.match(/method: "DELETE"/g) ?? []).length, 1);
  assert.match(deletion, /deleteRequestBusy\.current \|\| deletePreviewLoading/);
  assert.match(deletion, /encodeURIComponent\(deletePreview\.confirmation\)/);
  assert.match(deletion, /await loadDeletePreview\(target,/);
  assert.match(source, /controller\.signal\.aborted \|\| deleteTargetRef\.current !== target/);
  assert.doesNotMatch(source, /confirm\("이 태그/);
});
