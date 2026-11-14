import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const expected = {
  ko: { manual: "수동 연결", import: "가져온 연결", rule: "자동 연결", model_candidate: "모델 후보" },
  en: { manual: "Manual link", import: "Imported link", rule: "Automatic link", model_candidate: "Model candidate" },
  ja: { manual: "手動の関連付け", import: "インポートした関連付け", rule: "自動の関連付け", model_candidate: "モデル候補" },
  zh: { manual: "手动关联", import: "导入的关联", rule: "自动关联", model_candidate: "模型候选" },
};

for (const [locale, origins] of Object.entries(expected)) {
  const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
  for (const [origin, label] of Object.entries(origins)) {
    test(`${locale} displays the localized ${origin} mapping origin`, () => {
      assert.equal(messages.knowledgeMapping.origins[origin], label);
      assert.notEqual(label, origin);
    });
  }
  test(`${locale} covers every mapping origin without changing its API keys`, () => {
    assert.deepEqual(Object.keys(messages.knowledgeMapping.origins).sort(), Object.keys(origins).sort());
  });
}

test("the shared mapping editor translates the origin instead of rendering its raw enum", () => {
  const component = readFileSync(new URL("../src/components/admin/knowledge/KnowledgeTagMappingEditor.tsx", import.meta.url), "utf8");
  assert.match(component, /\{tm\(`origins\.\$\{mapping\.origin\}`\)\}/);
  assert.doesNotMatch(component, /<span[^>]*>\{mapping\.origin\}<\/span>/);
});
