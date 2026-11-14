import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const keys = {
  knowledgeMap: ["autoArrange", "undoAutoArrange", "layoutSaveFailed", "layoutRecoveryFailed"],
  admin: [
    "thumbnailUploadFailed", "tierManagement", "tierName", "tierImageUrl", "requiredXp",
    "addTier", "removeTier", "tierSaved", "tierSaveFailed", "noTiers", "settingsLoadFailed",
    "tierImagePreviewFailed", "tierImageUpload", "tierImageUploaded", "tierImageUploadFailed",
    "tierImageUrlPlaceholder", "tierAssignmentDescription",
  ],
};

for (const locale of ["ko", "en", "ja", "zh"]) {
  test(`${locale}: image editing and map commands have nonempty translations`, () => {
    const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
    for (const [namespace, names] of Object.entries(keys)) {
      for (const name of names) {
        assert.equal(typeof messages[namespace][name], "string", `${namespace}.${name}`);
        assert.ok(messages[namespace][name].trim(), `${namespace}.${name}`);
        assert.notEqual(messages[namespace][name], `${namespace}.${name}`);
      }
    }
  });
}

test("Korean commands preserve plain action names and distinguish persistence from recovery", () => {
  const messages = JSON.parse(readFileSync(new URL("../messages/ko.json", import.meta.url), "utf8"));
  assert.equal(messages.knowledgeMap.autoArrange, "자동 정렬");
  assert.equal(messages.knowledgeMap.undoAutoArrange, "정렬 되돌리기");
  assert.notEqual(messages.knowledgeMap.layoutSaveFailed, messages.knowledgeMap.layoutRecoveryFailed);
});
