import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import * as React from "react";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/app/[locale]/admin/community/page.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!React.isValidElement(node)) return [];
  return [node, ...elements(node.props.children)];
}
for (const [locale, expected] of Object.entries({ ko: "취소", en: "Cancel", ja: "キャンセル", zh: "取消" })) {
  test(`actual ${locale} community moderation dialog resolves common.cancel without an admin duplicate`, () => {
    const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
    const ModerationActionDialog = () => null;
    const context = { exports: {}, require: (name) => {
      if (name === "react") return { ...React, useState: (initial) => [initial, () => {}], useEffect: () => {}, useCallback: (callback) => callback };
      if (name === "next-intl") return { useLocale: () => locale, useTranslations: (namespace) => (key) => {
        assert.equal(typeof messages[namespace]?.[key], "string", `unresolved ${namespace}.${key}`);
        return messages[namespace][key];
      } };
      if (name === "@/lib/api-client") return { apiFetch() { throw Error("unexpected API"); }, ApiError: class extends Error {} };
      if (name === "@/providers/ToastProvider") return { useToast: () => ({ toast() {} }) };
      if (name === "@/components/admin/ModerationActionDialog") return { ModerationActionDialog };
      return require(name);
    } };
    vm.runInNewContext(compiled, context, { timeout: 1000 });
    const dialog = elements(context.exports.default()).find((node) => node.type === ModerationActionDialog);
    assert(dialog);
    assert.equal(dialog.props.labels.cancel, expected);
    assert.equal(dialog.props.labels.cancel, messages.common.cancel);
    assert(!Object.hasOwn(messages.admin, "cancel"));
    assert.equal(typeof dialog.props.onClose, "function");
  });
}
