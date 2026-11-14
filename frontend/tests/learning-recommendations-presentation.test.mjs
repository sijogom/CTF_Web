import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { isValidElement } from "react";
import ts from "typescript";
import { internalContentHref } from "../src/lib/map-ui-state.mjs";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/components/learn/LearningRecommendations.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;

function render(auth, state = null) {
  let stateIndex = 0;
  const context = { exports: {}, require(name) {
    if (name === "react") return { ...require(name), useCallback: (fn) => fn, useEffect: () => {},
      useRef: (value) => ({ current: value }), useState: (initial) => [stateIndex++ === 0 ? state : initial, () => {}] };
    if (name === "next-intl") return { useTranslations: () => (key) => key };
    if (name === "@/providers/AuthProvider") return { useAuth: () => auth };
    if (name === "@/i18n/navigation") return { Link: "a" };
    if (name === "@/lib/api-client") return { apiFetch: () => { throw Error("No request is permitted during render"); } };
    if (name === "@/lib/map-ui-state.mjs") return { internalContentHref };
    return require(name);
  } };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  return context.exports.LearningRecommendations();
}

function elements(element) {
  if (Array.isArray(element)) return element.flatMap(elements);
  if (!isValidElement(element)) return [];
  if (typeof element.type === "function") return elements(element.type(element.props));
  return [element, ...elements(element.props.children)];
}

for (const auth of [
  { loading: true, user: null, sessionStatus: "unknown" },
  { loading: false, user: null, sessionStatus: "unauthenticated" },
  { loading: false, user: { id: "previous-account" }, sessionStatus: "unknown" },
]) {
  test(`no empty recommendation or sign-in block for ${auth.sessionStatus}/${auth.loading}`, () => {
    assert.equal(render(auth), null);
  });
}

const auth = { loading: false, user: { id: "current-account" }, sessionStatus: "authenticated" };
function ready(items) {
  return { owner: auth.user.id, consent: { enabled: true, policy_version: "test" },
    recommendations: { enabled: true, consent_required: false, items } };
}

test("authenticated recommendations retain their source action but do not repeat it as learn-first", () => {
  const item = { type: "course", id: "owned", name: "공개 과정", href: "/learn/owned", evidence: [],
    next_action: { type: "course", id: "owned", name: "공개 과정", href: "/learn/owned" } };
  const links = elements(render(auth, ready([item]))).filter((element) => element.type === "a");
  assert.equal(links.filter((element) => element.props.href === item.href).length, 1);
});

test("a locked recommendation names and links the distinct prerequisite", () => {
  const item = { type: "course", id: "owned", name: "공개 과정", href: "/learn/owned", evidence: [], is_locked: true,
    next_action: { type: "lesson", id: "prerequisite", name: "필수 설치 실습", href: "/learn/required/lesson" } };
  const tree = elements(render(auth, ready([item])));
  const link = tree.find((element) => element.type === "a" && element.props.href === item.next_action.href);
  assert.ok(link);
  assert.ok(elements(link).some((element) => element.type === "span" && element.props.children === "필수 설치 실습"));
});

test("a previous account's recommendations never become the current account's list", () => {
  const tree = elements(render(auth, { ...ready([{ type: "course", id: "old", name: "old", href: "/learn/old", evidence: [] }]), owner: "old-account" }));
  assert.equal(tree.filter((element) => element.type === "a" && element.props.href === "/learn/old").length, 0);
});
