import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { isValidElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/app/[locale]/settings/profile/page.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const names = ["username", "bio", "originalUsername", "originalBio", "saving", "loading", "message", "avatarUrl",
  "originalAvatarUrl", "pendingAvatarFile", "pendingAvatarPreview", "pendingAvatarDelete", "discordUsername",
  "baekjoonHandle", "baekjoonTier", "bjInput", "bjLinking", "bjVerifyCode", "bjVerifying"];
const locales = ["ko", "en", "ja", "zh"];
const settingKeys = ["notLinked", "link", "unlink", "refresh", "baekjoon", "baekjoonLinked", "baekjoonUnlinked",
  "tierRefreshed", "baekjoonHandle", "baekjoonVerificationInstruction", "baekjoonVerify", "baekjoonActionFailed", "baekjoonVerifyFailed"];
const tokens = (node) => new Set((node.props.className ?? "").split(/\s+/));
const clean = (value) => JSON.parse(JSON.stringify(value));

function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  return isValidElement(node) ? [node, ...elements(node.props.children)] : [];
}
function visibleText(node) {
  if (Array.isArray(node)) return node.map(visibleText).join("");
  if (!isValidElement(node)) return typeof node === "string" ? node : "";
  return node.props["aria-hidden"] === true ? "" : visibleText(node.props.children);
}
class ApiError extends Error {}

function render(locale = "en", state = {}, response = async () => ({})) {
  const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
  const values = { username: "OwnedProfile", bio: "", originalUsername: "OwnedProfile", originalBio: "", saving: false,
    loading: false, message: null, avatarUrl: null, originalAvatarUrl: null, pendingAvatarFile: null,
    pendingAvatarPreview: null, pendingAvatarDelete: false, discordUsername: null, baekjoonHandle: null,
    baekjoonTier: null, bjInput: "  owned_handle  ", bjLinking: false, bjVerifyCode: null, bjVerifying: false, ...state };
  let index = 0;
  const calls = [], writes = [];
  const context = { exports: {}, require: (name) => {
    if (name === "react") return { ...require("react"), useState: () => {
      const key = names[index++];
      return [values[key], (value) => writes.push([key, value])];
    }, useRef: (value) => ({ current: value }), useEffect() {}, useLayoutEffect() {} };
    if (name === "next-intl") return { useTranslations: (namespace) => (key) => {
      const value = messages[namespace][key];
      assert.equal(typeof value, "string", `missing ${locale}.${namespace}.${key}`);
      return value;
    } };
    if (name === "@/providers/AuthProvider") return { useAuth: () => ({ user: { id: "owned-profile" } }) };
    if (name === "@/components/ui/Button") return { Button: "button" };
    if (name === "@/components/ui/RichTextEditor") return { RichTextEditor: () => null };
    if (name === "@/lib/rich-content.mjs") return { richContentVisibleLength: (value) => value.length };
    if (name === "@/lib/api-client") return { ApiError, apiFetch: async (...args) => {
      calls.push(clean(args)); return response(...args);
    }, apiFormFetch: () => { throw Error("unexpected avatar action"); } };
    return require(name);
  } };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  const element = context.exports.default();
  assert.equal(index, names.length);
  assert.equal(calls.length, 0);
  const section = elements(element).find((node) => node.props["data-testid"] === "baekjoon-settings");
  assert(section);
  const tree = elements(section);
  return { section, tree, calls, writes, messages, html: renderToStaticMarkup(section),
    button: (label) => {
      const matches = tree.filter((node) => node.type === "button" && visibleText(node) === label);
      assert.equal(matches.length, 1, `unique button ${label}`);
      return matches[0];
    } };
}

for (const locale of locales) {
  test(`actual ${locale} Baekjoon states retain translated accessible names and responsive controls`, () => {
    for (const state of [{}, { bjLinking: true }, { bjVerifyCode: "OWNED-CODE-".repeat(12) },
      { bjVerifyCode: "OWNED-CODE", bjVerifying: true }, { baekjoonHandle: "owned_handle", baekjoonTier: 1 }]) {
      const h = render(locale, state), t = h.messages.settings;
      for (const key of settingKeys) {
        assert.equal(typeof t[key], "string"); assert(t[key].trim() && t[key] !== key);
      }
      assert.equal(h.section.type, "section");
      assert.equal(h.section.props["aria-labelledby"], "baekjoon-settings-title");
      const title = h.tree.filter((node) => node.props.id === "baekjoon-settings-title");
      assert.equal(title.length, 1); assert.equal(title[0].props.children, t.baekjoon);
      const row = h.section.props.children;
      for (const token of ["flex", "min-w-0", "flex-col", "gap-3", "sm:flex-row", "sm:flex-wrap"]) assert(tokens(row).has(token));
      const controls = [row.props.children].flat().filter(isValidElement)[1];
      for (const token of ["w-full", "min-w-0", "sm:w-auto"]) assert(tokens(controls).has(token));
      for (const button of h.tree.filter((node) => node.type === "button")) {
        for (const token of ["min-h-11", "shrink-0", "whitespace-nowrap"]) assert(tokens(button).has(token));
      }
      const input = h.tree.find((node) => node.type === "input");
      if (input) {
        assert.equal(input.props.id, "baekjoon-handle");
        assert.equal(input.props["aria-label"], t.baekjoonHandle);
        assert.equal(input.props.placeholder, t.baekjoonHandle);
        for (const token of ["w-full", "min-w-0", "flex-1", "min-h-11", "sm:w-48"]) assert(tokens(input).has(token));
        assert(!tokens(input).has("sm:w-36"));
        assert(tokens(controls).has("flex-wrap"));
      }
      if (state.bjVerifyCode) {
        const instruction = h.tree.find((node) => node.props.children === t.baekjoonVerificationInstruction);
        assert(tokens(instruction).has("break-words"));
        assert(h.html.includes(t.baekjoonVerificationInstruction));
        assert(h.tree.some((node) => tokens(node).has("flex-wrap") && tokens(node).has("justify-end")));
        assert.equal(h.button(state.bjVerifying ? h.messages.common.processing : t.baekjoonVerify).props.disabled, Boolean(state.bjVerifying));
      } else if (!state.baekjoonHandle) {
        assert.equal(h.button(state.bjLinking ? h.messages.common.processing : t.link).props.disabled, Boolean(state.bjLinking));
      }
      assert(!h.html.includes("settings."));
    }
  });

  test(`actual ${locale} link and verify labels reserve the same two intrinsic widths while busy`, () => {
    for (const [state, busyKey, idleLabel] of [[{}, "bjLinking", "link"], [{ bjVerifyCode: "OWNED-CODE" }, "bjVerifying", "baekjoonVerify"]]) {
      const idle = render(locale, state), busy = render(locale, { ...state, [busyKey]: true });
      const idleButton = idle.button(idle.messages.settings[idleLabel]), busyButton = busy.button(busy.messages.common.processing);
      const labelCells = (button) => elements(button).filter((node) => tokens(node).has("col-start-1") && tokens(node).has("row-start-1"));
      const a = labelCells(idleButton), b = labelCells(busyButton);
      assert.deepEqual(a.map((node) => node.props.children), [idle.messages.settings[idleLabel], idle.messages.common.processing]);
      assert.deepEqual(b.map((node) => node.props.children), a.map((node) => node.props.children));
      assert(a.every((node, index) => tokens(node).has("invisible") === Boolean(index)));
      assert(b.every((node, index) => tokens(node).has("invisible") === !index));
      assert.deepEqual(a.map((node) => node.props["aria-hidden"]), [false, true]);
      assert.deepEqual(b.map((node) => node.props["aria-hidden"]), [true, false]);
    }
  });

  test(`actual ${locale} action errors retain ApiError details before localized generic fallbacks`, async () => {
    for (const action of ["start-link", "verify", "refresh", "unlink"]) {
      const state = action === "verify" ? { bjVerifyCode: "OWNED-CODE" } : ["refresh", "unlink"].includes(action) ? { baekjoonHandle: "owned_handle" } : {};
      for (const error of [new ApiError("owned server detail"), new Error("private transport detail")]) {
        const h = render(locale, state, async () => { throw error; });
        const label = h.messages.settings[{ "start-link": "link", verify: "baekjoonVerify", refresh: "refresh", unlink: "unlink" }[action]];
        await h.button(label).props.onClick();
        assert.equal(h.calls.length, 1);
        assert.equal(h.calls[0][0], `/api/auth/baekjoon/${action}`);
        assert.equal(h.calls[0][1].method, "POST");
        const message = h.writes.find(([name]) => name === "message")[1];
        assert.deepEqual(clean(message), { type: "error", text: error instanceof ApiError ? error.message
          : h.messages.settings[action === "verify" ? "baekjoonVerifyFailed" : "baekjoonActionFailed"] });
        if (action === "start-link" || action === "verify") {
          const name = action === "start-link" ? "bjLinking" : "bjVerifying";
          assert.deepEqual(clean(h.writes.filter(([key]) => key === name)), [[name, true], [name, false]]);
        }
      }
    }
  });
}

test("the actual link handler trims only the outgoing handle and preserves editing, busy and response transitions", async () => {
  const h = render("en", {}, async () => ({ code: "OWNED-CODE" }));
  h.tree.find((node) => node.type === "input").props.onChange({ target: { value: "  next_handle  " } });
  assert.deepEqual(clean(h.writes), [["bjInput", "  next_handle  "]]);
  h.writes.length = 0;
  await h.button(h.messages.settings.link).props.onClick();
  assert.deepEqual(h.calls, [["/api/auth/baekjoon/start-link", { method: "POST", body: { handle: "owned_handle" } }]]);
  assert.deepEqual(clean(h.writes), [["bjLinking", true], ["bjVerifyCode", "OWNED-CODE"], ["bjLinking", false]]);
  assert.equal(render("en", { bjInput: " \t " }).button(h.messages.settings.link).props.disabled, true);
});

test("actual verification, refresh, unlink and local cancel keep their original request and state contracts", async () => {
  const verify = render("en", { bjVerifyCode: "OWNED-CODE" }, async () => ({ handle: "linked_handle", tier: 7 }));
  await verify.button(verify.messages.settings.baekjoonVerify).props.onClick();
  assert.deepEqual(verify.calls, [["/api/auth/baekjoon/verify", { method: "POST" }]]);
  assert.deepEqual(clean(verify.writes), [["bjVerifying", true], ["baekjoonHandle", "linked_handle"], ["baekjoonTier", 7],
    ["bjVerifyCode", null], ["bjInput", ""], ["message", { type: "success", text: verify.messages.settings.baekjoonLinked }], ["bjVerifying", false]]);
  const cancel = render("en", { bjVerifyCode: "OWNED-CODE" });
  cancel.button(cancel.messages.common.cancel).props.onClick();
  assert.deepEqual(clean(cancel.writes), [["bjVerifyCode", null], ["bjInput", ""]]);
  assert.equal(cancel.calls.length, 0);
  const refresh = render("en", { baekjoonHandle: "linked_handle" }, async () => ({ tier: 8, tier_name: "Silver III" }));
  await refresh.button(refresh.messages.settings.refresh).props.onClick();
  assert.deepEqual(refresh.calls, [["/api/auth/baekjoon/refresh", { method: "POST" }]]);
  assert.deepEqual(clean(refresh.writes), [["baekjoonTier", 8], ["message", { type: "success", text: refresh.messages.settings.tierRefreshed }]]);
  const unlink = render("en", { baekjoonHandle: "linked_handle" });
  await unlink.button(unlink.messages.settings.unlink).props.onClick();
  assert.deepEqual(unlink.calls, [["/api/auth/baekjoon/unlink", { method: "POST" }]]);
  assert.deepEqual(clean(unlink.writes), [["baekjoonHandle", null], ["baekjoonTier", null],
    ["message", { type: "success", text: unlink.messages.settings.baekjoonUnlinked }]]);
});
