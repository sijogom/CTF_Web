import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url), React = require("react");
const source = readFileSync(new URL("../src/app/[locale]/wargame/[id]/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("wargame-page.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
const stateNames = [], callbackNames = [];
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.initializer && ts.isCallExpression(node.initializer)) {
    if (node.initializer.expression.getText(ast) === "useState") stateNames.push(node.name.elements[0].getText(ast));
    if (node.initializer.expression.getText(ast) === "useCallback") callbackNames.push(node.name.getText(ast));
  }
  ts.forEachChild(node, visit);
}
visit(ast);
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const USER = "11111111-1111-4111-8111-111111111111", OTHER = "22222222-2222-4222-8222-222222222222";
const detail = (extra = {}) => ({ id: 7, name: "Owned challenge", category: "Web", value: 100, solves: 0,
  env_type: "dockerfile", environment_ready: true, description: "Owned description", hints: [], files: [], solved: false, ...extra });
const instance = (extra = {}) => ({ id: "owned-instance", challenge_id: 7, port: "30123", status: "running",
  expires_at: "2099-01-01T00:00:00Z", extend_count: "0", max_extend_count: "3", ...extra });
const flush = async () => { for (let index = 0; index < 24; index++) await Promise.resolve(); };
const plain = (value) => JSON.parse(JSON.stringify(value));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  return React.isValidElement(node) ? [node, ...elements(node.props.children)] : [];
}
class ApiError extends Error {}
function harness({ challenge = detail(), user = { id: USER }, locale = "ko" } = {}) {
  const values = new Map(), refs = [], callbacks = [], effects = [], translations = new Map(), calls = [], toasts = [], timers = new Map();
  let stateIndex = 0, refIndex = 0, callbackIndex = 0, effectIndex = 0, timerId = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const h = { id: 7, locale, user, sessionStatus: user ? "authenticated" : "anonymous", epoch: 1,
    challenge, values, calls, toasts, blockOwner: false, refreshCalls: 0 };
  h.defaultResponse = (url, options = {}) => {
    if (/^\/api\/learn\/challenges\/\d+\?lang=/.test(url)) return detail({ ...h.challenge, id: Number(url.match(/challenges\/(\d+)/)[1]) });
    if (url === "/api/env/instances") return [instance({ challenge_id: h.id, id: `owned-${h.id}-${h.user?.id ?? "anonymous"}` })];
    if (url.endsWith("/create")) return instance({ id: "created-instance", extend_count: 0, max_extend_count: 3 });
    if (url.endsWith("/stop")) return undefined;
    if (url.endsWith("/extend")) return { expires_at: "2099-01-02T00:00:00Z", extend_count: 1, max_extend_count: 3 };
    if (url.endsWith("/submit")) return { status: "correct", message: "Confirmed" };
    throw new Error(`Unexpected test API: ${url} ${options.method ?? "GET"}`);
  };
  h.respond = (...args) => h.defaultResponse(...args);
  const mockReact = { ...React,
    useState(initial) {
      const name = stateNames[stateIndex++]; assert(name);
      if (!values.has(name)) values.set(name, typeof initial === "function" ? initial() : initial);
      return [values.get(name), (update) => values.set(name, typeof update === "function" ? update(values.get(name)) : update)];
    },
    useRef(initial) { const index = refIndex++; return refs[index] ??= { current: initial }; },
    useCallback(fn, deps) {
      const index = callbackIndex++;
      if (!callbacks[index] || !same(callbacks[index].deps, deps)) callbacks[index] = { fn, deps };
      return callbacks[index].fn;
    },
    useEffect(fn, deps) {
      const index = effectIndex++;
      if (!effects[index] || !same(effects[index].deps, deps)) effects[index] = { fn, deps, dirty: true, cleanup: effects[index]?.cleanup };
    },
  };
  const components = {
    "@/components/ui/Badge": { CategoryBadge: "category-badge" },
    "@/components/ui/Button": { Button: "button" },
    "@/components/wargame/FlagSubmitForm": { FlagSubmitForm: "flag-form" },
    "@/components/wargame/EnvInstanceSection": { EnvInstanceSection: "environment" },
    "@/components/wargame/HintSection": { HintSection: "hints" },
    "@/components/wargame/AttachmentSection": { AttachmentSection: "attachments" },
    "@/components/wargame/LearningPathPanel": { LearningPathPanel: "learning-path" },
    "@/components/learn/MarkdownRenderer": { MarkdownRenderer: "markdown" },
  };
  const context = { exports: {}, process: { env: {} },
    setInterval(fn) { timers.set(++timerId, fn); return timerId; }, clearInterval(id) { timers.delete(id); },
    require(name) {
      if (name === "react") return mockReact;
      if (name === "next/navigation") return { useParams: () => ({ id: String(h.id) }) };
      if (name === "next-intl") return { useLocale: () => h.locale, useTranslations(namespace) {
        const key = `${h.locale}:${namespace}`;
        if (!translations.has(key)) translations.set(key, (message) => `${key}:${message}`);
        return translations.get(key);
      } };
      if (name === "@/lib/knowledge-release-stage.mjs") return { knowledgeExtensionsEnabled: () => false };
      if (name === "@/providers/AuthProvider") return { useAuth: () => ({ user: h.user, sessionStatus: h.sessionStatus,
        refresh: async () => { h.refreshCalls++; } }) };
      if (name === "@/providers/ToastProvider") return { useToast: () => ({ toast: (...args) => toasts.push(args) }) };
      if (name === "@/lib/api-client") return { ApiError,
        captureApiRequestOwner(id) {
          if (h.blockOwner || h.sessionStatus !== "authenticated" || !id || id !== h.user?.id) throw new ApiError("Session unavailable");
          return { id, epoch: h.epoch };
        },
        isCurrentApiRequestOwner: (owner) => owner.id === h.user?.id && owner.epoch === h.epoch && h.sessionStatus === "authenticated",
        apiFetch(url, options = {}) { calls.push({ url, options }); return Promise.resolve().then(() => h.respond(url, options)); },
      };
      if (name === "@/i18n/navigation") return { Link: "a" };
      return components[name] ?? require(name);
    },
  };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  h.render = () => { stateIndex = refIndex = callbackIndex = effectIndex = 0; return elements(context.exports.default()); };
  h.commit = async () => {
    for (const effect of effects) if (effect.dirty) { effect.cleanup?.(); effect.dirty = false; effect.cleanup = effect.fn(); }
    await flush();
  };
  h.settle = async () => { for (let index = 0; index < 5; index++) { h.render(); await h.commit(); } return h.render(); };
  h.callback = (name) => { const callback = callbacks[callbackNames.indexOf(name)]; assert(callback, name); return callback.fn; };
  h.env = () => h.render().find((node) => node.type === "environment")?.props;
  h.unmount = () => { for (const effect of effects) effect.cleanup?.(); };
  h.instanceCalls = () => calls.filter(({ url }) => url === "/api/env/instances");
  return h;
}

for (const condition of ["none", "anonymous", "locked", "not-ready", "unknown-session", "unknown-env-type"]) {
  test(`actual page effects do not restore environments for ${condition}`, async () => {
    const h = harness({ challenge: detail(condition === "none" ? { env_type: "none" }
      : condition === "locked" ? { locked: true } : condition === "not-ready" ? { environment_ready: false }
        : condition === "unknown-env-type" ? { env_type: "unsupported" } : {}), user: condition === "anonymous" ? null : { id: USER } });
    if (condition === "unknown-session") h.sessionStatus = "unknown";
    await h.settle();
    assert.equal(h.instanceCalls().length, 0);
    assert.equal(h.values.get("envInstance"), null);
    if (condition === "none" || condition === "locked" || condition === "not-ready") assert.equal(h.env(), undefined);
    if (condition === "anonymous") assert(h.env(), "Existing anonymous environment presentation remains visible");
  });
}

for (const envType of ["dockerfile", "image"]) {
  test(`current authenticated ${envType} detail restores only its running instance after the detail resolves`, async () => {
    const ready = deferred(), h = harness({ challenge: detail({ env_type: envType, environment_ready: undefined, locked: undefined }) });
    h.respond = (url) => url.includes("/challenges/") ? ready.promise : [instance({ challenge_id: 99 }), instance({ status: "stopped" }), instance()];
    h.render(); await h.commit(); assert.equal(h.instanceCalls().length, 0);
    ready.resolve(h.challenge); await flush(); await h.settle();
    assert.equal(h.instanceCalls().length, 1);
    assert.deepEqual(plain(h.instanceCalls()[0].options.requestOwner), { id: USER, epoch: 1 });
    assert.deepEqual(plain(h.values.get("envInstance")), { id: "owned-instance", port: 30123, status: "running",
      expires_at: "2099-01-01T00:00:00Z", extend_count: 0, max_extend_count: 3 });
    assert.equal(h.env().extendCount, 0); assert.equal(h.env().maxExtendCount, 3);
  });
}

test("mismatched challenge detail cannot trigger restore; current failure supports a fresh manual retry", async () => {
  const h = harness(); h.respond = () => detail({ id: 8 });
  let tree = await h.settle(); assert.equal(h.instanceCalls().length, 0);
  assert.equal(h.values.get("error"), "ko:wargame:loadFailed"); assert.equal(h.values.get("loading"), false);
  h.respond = (...args) => h.defaultResponse(...args);
  await tree.find((node) => node.type === "button").props.onClick(); tree = await h.settle();
  assert.equal(h.values.get("error"), null); assert.equal(h.instanceCalls().length, 1); assert(h.env());
});

test("detail request ordinal protects both success, error and finally from a superseded same-view GET", async () => {
  for (const outcome of ["resolve", "reject"]) {
    const first = deferred(), second = deferred(), h = harness(); let count = 0;
    h.respond = () => (++count === 1 ? first : second).promise;
    h.render(); await h.commit(); const retry = h.callback("fetchChallenge")(); await flush();
    first[outcome](outcome === "resolve" ? detail({ name: "Older" }) : new ApiError("Older failure"));
    await flush(); h.render();
    assert.equal(h.values.get("loadedChallenge"), null); assert.equal(h.values.get("error"), null);
    assert.equal(h.values.get("loading"), true); assert.equal(h.instanceCalls().length, 0);
    second.resolve(detail({ env_type: "none", name: "Current" })); await retry; await h.settle();
    assert.equal(h.values.get("loadedChallenge").name, "Current"); assert.equal(h.values.get("loading"), false);
  }
});

for (const transition of ["route", "account", "locale", "session", "unmount"]) {
  test(`late detail success/failure after ${transition} cannot replace the current detail or loading state`, async () => {
    for (const outcome of ["resolve", "reject"]) {
      const old = deferred(), fresh = deferred(), h = harness(); let count = 0;
      h.respond = () => (++count === 1 ? old : fresh).promise;
      h.render(); await h.commit();
      if (transition === "route") h.id = 8;
      if (transition === "account") { h.user = { id: OTHER }; h.epoch++; }
      if (transition === "locale") h.locale = "ja";
      if (transition === "session") { h.sessionStatus = "anonymous"; h.user = null; h.epoch++; }
      if (transition === "unmount") h.unmount(); else { h.render(); await h.commit(); }
      old[outcome](outcome === "resolve" ? detail({ name: "Old owner" }) : new ApiError("Old owner failure"));
      await flush();
      assert.equal(h.values.get("loadedChallenge"), null); assert.equal(h.values.get("error"), null);
      assert.equal(h.values.get("loading"), true); assert.equal(h.instanceCalls().length, 0);
      if (transition !== "unmount") {
        fresh.resolve(detail({ id: h.id, env_type: "none", name: "Current owner" })); await flush(); await h.settle();
        assert.equal(h.values.get("loadedChallenge").name, "Current owner"); assert.equal(h.values.get("loading"), false);
      }
    }
  });
}

test("owner epoch changes before React renders already block detail and restore responses", async () => {
  const first = deferred(), h = harness(); h.respond = () => first.promise;
  h.render(); await h.commit(); h.epoch++; first.resolve(detail()); await flush();
  assert.equal(h.values.get("loadedChallenge"), null); assert.equal(h.instanceCalls().length, 0);
  const restore = deferred(), second = harness();
  second.respond = (url, options) => url === "/api/env/instances" ? restore.promise : second.defaultResponse(url, options);
  await second.settle(); second.epoch++; restore.resolve([instance()]); await flush();
  assert.equal(second.values.get("envInstance"), null);
});

for (const transition of ["route", "account", "locale", "unmount"]) {
  test(`late restore after ${transition} cannot expose old instance, counters, errors or confirmation`, async () => {
    const restore = deferred(), h = harness();
    h.respond = (url, options) => url === "/api/env/instances" ? restore.promise : h.defaultResponse(url, options);
    await h.settle(); assert.equal(h.instanceCalls().length, 1);
    h.values.set("envError", "Previous failure"); h.values.set("extendCount", 2); h.values.set("maxExtendCount", 3);
    h.values.set("showStopConfirm", true);
    if (transition === "route") h.id = 8;
    if (transition === "account") { h.user = { id: OTHER }; h.epoch++; }
    if (transition === "locale") h.locale = "en";
    if (transition === "unmount") h.unmount(); else { h.challenge = detail({ id: h.id, env_type: "none" }); await h.settle(); }
    const before = plain(Object.fromEntries(h.values));
    restore.resolve([instance()]); await flush(); assert.deepEqual(plain(Object.fromEntries(h.values)), before);
    if (transition !== "unmount") {
      assert.equal(h.values.get("envInstance"), null); assert.equal(h.values.get("envError"), null);
      assert.equal(h.values.get("extendCount"), null); assert.equal(h.values.get("maxExtendCount"), null);
      assert.equal(h.values.get("showStopConfirm"), false); assert.equal(h.values.get("timeRemaining"), "");
    }
  });
}

test("a silent restore failure leaves start available and a fresh detail retry can restore", async () => {
  const h = harness(); let failed = true;
  h.respond = (url, options) => { if (url === "/api/env/instances" && failed) throw new ApiError("Unavailable"); return h.defaultResponse(url, options); };
  await h.settle(); assert.equal(h.values.get("envInstance"), null); assert.equal(h.values.get("envError"), null);
  assert.equal(h.env().envLoading, false); assert.equal(h.toasts.length, 0);
  failed = false; await h.callback("fetchChallenge")(); await h.settle();
  assert.equal(h.instanceCalls().length, 2); assert(h.values.get("envInstance"));
});

test("the latest restore ordinal wins even within the same owner and loaded challenge", async () => {
  const first = deferred(), second = deferred(), h = harness(); let restores = 0;
  h.respond = (url, options) => url === "/api/env/instances" ? (++restores === 1 ? first : second).promise : h.defaultResponse(url, options);
  await h.settle(); const next = h.callback("fetchExistingInstance")(); await flush();
  second.resolve([instance({ id: "latest", extend_count: 2 })]); await next;
  first.resolve([instance({ id: "older", extend_count: 0 })]); await flush();
  assert.equal(h.values.get("envInstance").id, "latest"); assert.equal(h.values.get("extendCount"), 2);
});

test("a superseded loaded detail cannot restore or send commands while a fresh detail GET is pending or failed", async () => {
  const fresh = deferred(), h = harness(); await h.settle();
  const oldProps = h.env(), oldRestore = h.callback("fetchExistingInstance"), before = h.instanceCalls().length;
  h.respond = (url, options) => url.includes("?lang=") ? fresh.promise : h.defaultResponse(url, options);
  const reload = h.callback("fetchChallenge")(); await flush();
  await oldRestore(); await oldProps.onStart(); await oldProps.onStop(); await oldProps.onExtend();
  assert.equal(h.instanceCalls().length, before); assert.equal(h.calls.filter(({ options }) => options.method === "POST").length, 0);
  assert.equal(h.env(), undefined);
  fresh.reject(new ApiError("Fresh detail unavailable")); await reload; await h.settle();
  await h.callback("fetchExistingInstance")();
  assert.equal(h.instanceCalls().length, before); assert.equal(h.values.get("error"), "Fresh detail unavailable");
  h.respond = (...args) => h.defaultResponse(...args); await h.callback("fetchChallenge")(); await h.settle();
  assert.equal(h.instanceCalls().length, before + 1); assert(h.env());
});

test("same-owner stats object refreshes do not duplicate detail or restore GETs", async () => {
  const h = harness(); await h.settle(); const before = h.calls.length;
  h.user = { id: USER, points: 200 }; await h.settle();
  assert.equal(h.calls.length, before); assert.equal(h.instanceCalls().length, 1);
});

for (const command of ["start", "stop", "extend"]) {
  test(`a late restore cannot undo a completed ${command} command`, async () => {
    const restore = deferred(), h = harness(); await h.settle();
    h.respond = (url, options) => url === "/api/env/instances" ? restore.promise : h.defaultResponse(url, options);
    const oldRestore = h.callback("fetchExistingInstance")(); await flush();
    const props = h.env(); await props[{ start: "onStart", stop: "onStop", extend: "onExtend" }[command]]();
    const expected = plain(Object.fromEntries(h.values));
    restore.resolve([instance({ id: "stale", extend_count: 0 })]); await oldRestore;
    assert.deepEqual(plain(Object.fromEntries(h.values)), expected);
    const call = h.calls.find(({ options }) => options.method === "POST");
    assert.equal(call.url, command === "start" ? "/api/env/instances/create" : `/api/env/instances/owned-7-${USER}/${command}`);
    assert.equal(call.options.requestOwner.id, USER);
    if (command === "start") assert.deepEqual(plain(call.options.body), { challenge_id: 7 });
    if (command === "stop") { assert.equal(h.values.get("envInstance"), null); assert.equal(h.values.get("extendCount"), null); }
    if (command === "extend") { assert.equal(h.values.get("extendCount"), 1); assert.equal(h.values.get("envInstance").expires_at, "2099-01-02T00:00:00Z"); }
  });
}

for (const command of ["start", "stop", "extend"]) {
  test(`late ${command} success/failure/finally cannot overwrite a new account's busy command`, async () => {
    for (const outcome of ["resolve", "reject"]) {
      const old = deferred(), fresh = deferred(), h = harness(); await h.settle(); let commands = 0;
      h.respond = (url, options) => options.method === "POST" ? (++commands === 1 ? old : fresh).promise : h.defaultResponse(url, options);
      const key = { start: "onStart", stop: "onStop", extend: "onExtend" }[command];
      const oldCommand = h.env()[key](); await flush();
      h.user = { id: OTHER }; h.epoch++; await h.settle();
      const nextCommand = h.env()[key](); await flush(); assert.equal(h.values.get("envLoading"), true);
      const before = plain(Object.fromEntries(h.values));
      old[outcome](outcome === "reject" ? new ApiError("Old account failure")
        : command === "extend" ? { expires_at: "2099-02-01T00:00:00Z", extend_count: 2, max_extend_count: 3 } : instance({ id: "old-account" }));
      await oldCommand; assert.deepEqual(plain(Object.fromEntries(h.values)), before); assert.equal(h.toasts.length, 0);
      fresh.resolve(command === "extend" ? { expires_at: "2099-01-03T00:00:00Z", extend_count: 1, max_extend_count: 3 } : instance({ id: "current-account" }));
      await nextCommand; assert.equal(h.values.get("envLoading"), false);
      if (command === "start") assert.equal(h.values.get("envInstance").id, "current-account");
      if (command === "stop") assert.equal(h.values.get("envInstance"), null);
      if (command === "extend") assert.equal(h.values.get("envInstance").expires_at, "2099-01-03T00:00:00Z");
    }
  });
}

test("synchronous duplicate commands are blocked and failure permits an explicit fresh retry", async () => {
  const pending = deferred(), h = harness(); await h.settle();
  h.respond = (url, options) => options.method === "POST" ? pending.promise : h.defaultResponse(url, options);
  const props = h.env(), first = props.onStart(), duplicate = props.onStart(); await duplicate; await flush();
  assert.equal(h.calls.filter(({ options }) => options.method === "POST").length, 1);
  pending.reject(new ApiError("Current start failed")); await first;
  assert.equal(h.values.get("envLoading"), false); assert.equal(h.values.get("envError"), "Current start failed");
  h.respond = (...args) => h.defaultResponse(...args); await h.env().onStart();
  assert.equal(h.values.get("envError"), null); assert.equal(h.values.get("envInstance").id, "created-instance");
});

test("a stale environment callback cannot send after a view transition before effects commit", async () => {
  const h = harness(); await h.settle(); const previous = h.env(); h.id = 8; h.render();
  await previous.onStart(); await previous.onStop(); await previous.onExtend();
  assert.equal(h.calls.filter(({ options }) => options.method === "POST").length, 0);
  assert.equal(h.env(), undefined);
});

test("a late post-submit detail refresh cannot restore an older route or owner", async () => {
  const refreshed = deferred(), h = harness(); await h.settle();
  h.respond = (url, options) => url.includes("?lang=") ? refreshed.promise : h.defaultResponse(url, options);
  const form = h.render().find((node) => node.type === "flag-form"), submitted = form.props.onSubmit(7, "owned-flag"); await flush();
  h.id = 8; h.challenge = detail({ id: 8, env_type: "none" }); h.respond = (...args) => h.defaultResponse(...args); await h.settle();
  refreshed.resolve(detail({ id: 7, name: "Old submitted challenge" })); await submitted;
  assert.equal(h.values.get("loadedChallenge").id, 8); assert.equal(h.values.get("envInstance"), null);
});
