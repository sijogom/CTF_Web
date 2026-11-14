import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url), React = require("react");
const source = readFileSync(new URL("../src/components/admin/challenges/ChallengeCleanupJobs.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("ChallengeCleanupJobs.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
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
const flush = async () => { for (let index = 0; index < 24; index++) await Promise.resolve(); };
const plain = (value) => JSON.parse(JSON.stringify(value));
const job = (extra = {}) => ({ challenge_id: 7, job_id: "owned-job", status: "failed", attempt_count: 3,
  last_error_code: "env_orchestrator_unavailable", ...extra });
const list = (items = []) => ({ items, total: items.length });
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
function harness() {
  const values = new Map(), refs = [], callbacks = [], effects = [], calls = [], timers = new Map(), toasts = [];
  let stateIndex = 0, refIndex = 0, callbackIndex = 0, effectIndex = 0, timerId = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const h = { user: { id: "owner-1" }, sessionStatus: "authenticated", isSuperAdmin: true, epoch: 1,
    refreshVersion: 0, values, calls, timers, toasts, blockOwner: false };
  h.respond = () => list();
  const t = (key, values = {}) => `admin:${key}${values.id === undefined ? "" : `:${values.id}`}`;
  const toast = (...args) => toasts.push(args);
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
  const context = { exports: {}, AbortController,
    setInterval(fn, ms) { timers.set(++timerId, { fn, ms }); return timerId; }, clearInterval(id) { timers.delete(id); },
    require(name) {
      if (name === "react") return mockReact;
      if (name === "next-intl") return { useTranslations: () => t };
      if (name === "lucide-react") return { RotateCw: "rotate-icon" };
      if (name === "@/providers/AuthProvider") return { useAuth: () => h };
      if (name === "@/providers/ToastProvider") return { useToast: () => ({ toast }) };
      if (name === "@/lib/api-client") return { ApiError,
        captureApiRequestOwner(id) {
          if (h.blockOwner || h.sessionStatus !== "authenticated" || id !== h.user?.id) throw new ApiError("Owner changed");
          return { id, epoch: h.epoch };
        },
        isCurrentApiRequestOwner: (owner) => owner.id === h.user?.id && owner.epoch === h.epoch && h.sessionStatus === "authenticated",
        apiFetch(url, options = {}) { calls.push({ url, options }); return Promise.resolve().then(() => h.respond(url, options)); },
      };
      return require(name);
    },
  };
  vm.runInNewContext(compiled, context, { timeout: 1000 });
  h.wrapper = () => context.exports.ChallengeCleanupJobs({ refreshVersion: h.refreshVersion });
  h.render = () => {
    stateIndex = refIndex = callbackIndex = effectIndex = 0;
    const wrapper = h.wrapper();
    return wrapper ? elements(wrapper.type(wrapper.props)) : [];
  };
  h.commit = async () => {
    for (const effect of effects) if (effect.dirty) { effect.cleanup?.(); effect.dirty = false; effect.cleanup = effect.fn(); }
    await flush();
  };
  h.settle = async () => { for (let index = 0; index < 4; index++) { h.render(); await h.commit(); } return h.render(); };
  h.callback = (name) => { const callback = callbacks[callbackNames.indexOf(name)]; assert(callback, name); return callback.fn; };
  h.unmount = () => { for (const effect of effects) effect.cleanup?.(); };
  h.gets = () => calls.filter(({ options }) => options.method !== "POST");
  h.posts = () => calls.filter(({ options }) => options.method === "POST");
  h.refresh = () => h.render().find((node) => node.props["aria-label"] === "admin:cleanupRefresh");
  h.retry = () => h.render().find((node) => node.props["aria-label"] === "admin:cleanupRetry:7");
  return h;
}

test("only an authenticated super-admin mounts an owner-keyed cleanup view", async () => {
  for (const auth of [{ user: null }, { sessionStatus: "anonymous" }, { sessionStatus: "unknown" }, { isSuperAdmin: false }]) {
    const h = harness(); Object.assign(h, auth);
    assert.equal(h.wrapper(), null); await h.settle(); assert.equal(h.calls.length, 0);
  }
  const h = harness(); assert.equal(h.wrapper().key, "owner-1");
  h.user = { id: "owner-2" }; assert.equal(h.wrapper().key, "owner-2");
});

test("successful empty cleanup lists stay hidden, while a failed lookup remains visible and can recover", async () => {
  const h = harness(); assert.deepEqual(await h.settle(), []);
  assert.equal(h.gets().length, 1); assert.deepEqual(plain(h.gets()[0].options.requestOwner), { id: "owner-1", epoch: 1 });
  h.respond = () => { throw new ApiError("Private server detail"); };
  await h.callback("fetchJobs")(); let tree = await h.settle();
  assert.equal(tree.find((node) => node.props.role === "alert").props.children, "admin:cleanupLoadFailed");
  assert(h.refresh()); assert.equal(h.values.get("loading"), false);
  h.respond = () => list(); h.refresh().props.onClick(); await flush();
  assert.deepEqual(await h.settle(), []);
});

test("active and failed rows retain readable status and only failed rows have a retry command", async () => {
  const h = harness(); h.respond = () => list([job(), job({ job_id: "waiting", challenge_id: 8, status: "pending" }),
    job({ job_id: "working", challenge_id: 9, status: "processing" })]);
  const tree = await h.settle();
  assert(tree.find((node) => node.type === "table").props.className.includes("min-w-[20rem]"));
  const statuses = tree.filter((node) => node.type === "td" && String(node.props.children).startsWith("admin:cleanupStatus_"));
  assert.equal(statuses.length, 3); for (const status of statuses) assert(status.props.className.includes("whitespace-nowrap"));
  assert.equal(tree.filter((node) => String(node.props["aria-label"]).startsWith("admin:cleanupRetry:")).length, 1);
  assert.equal(h.retry().props.title, "admin:cleanupRetry:7");
  assert.equal(h.timers.size, 1); assert.equal([...h.timers.values()][0].ms, 5000);
});

test("failed polls preserve the last known rows rather than disguising failure as an empty list", async () => {
  const h = harness(); h.respond = () => list([job({ status: "processing" })]); await h.settle();
  h.respond = () => { throw new Error("unavailable"); };
  [...h.timers.values()][0].fn(); await flush(); const tree = await h.settle();
  assert.equal(h.values.get("jobs").length, 1); assert(tree.find((node) => node.props.role === "alert"));
  assert.equal(h.timers.size, 1);
});

test("a post-delete refresh cancels old list requests and ignores their later success or failure", async () => {
  for (const outcome of ["resolve", "reject"]) {
    const first = deferred(), next = deferred(), h = harness(); let ordinal = 0;
    h.respond = () => (++ordinal === 1 ? first : next).promise;
    h.render(); await h.commit(); const oldSignal = h.gets()[0].options.signal;
    h.refreshVersion++; h.render(); await h.commit(); assert.equal(oldSignal.aborted, true);
    first[outcome](outcome === "resolve" ? list([job({ challenge_id: 999 })]) : new Error("old failure")); await flush();
    assert.equal(h.values.get("jobs").length, 0); assert.equal(h.values.get("error"), null);
    next.resolve(list([job()])); await flush(); await h.settle();
    assert.equal(h.values.get("jobs")[0].challenge_id, 7); assert.equal(h.values.get("loading"), false);
  }
});

test("synchronous repeated retry clicks issue one owner-bound POST and completion removes the row", async () => {
  const response = deferred(), h = harness(); let complete = false;
  h.respond = (url, options) => options.method === "POST" ? response.promise : list(complete ? [] : [job()]);
  await h.settle(); const retry = h.retry(); retry.props.onClick(); retry.props.onClick(); await flush();
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].url, "/api/learn/challenges/7/cleanup/retry?job_id=owned-job");
  assert.deepEqual(plain(h.posts()[0].options.requestOwner), { id: "owner-1", epoch: 1 });
  assert.equal(h.retry().props.disabled, true);
  complete = true; response.resolve(job({ status: "completed", attempt_count: 4 })); await flush();
  assert.deepEqual(await h.settle(), []); assert.equal(h.posts().length, 1); assert.equal(h.toasts.length, 0);
});

test("an uncertain or rejected retry reads status back without repeating the POST", async () => {
  const h = harness(); let retried = false;
  h.respond = (url, options) => {
    if (options.method === "POST") { retried = true; throw new ApiError("Already processing"); }
    return list([job({ status: retried ? "processing" : "failed" })]);
  };
  await h.settle(); h.retry().props.onClick(); await flush(); await h.settle();
  assert.equal(h.posts().length, 1); assert.equal(h.gets().length, 2);
  assert.deepEqual(h.toasts, [["error", "Already processing"]]);
  assert.equal(h.values.get("jobs")[0].status, "processing"); assert.equal(h.retry(), undefined);
});

test("retry binds the exact ledger row and rejects mismatched or invalid responses before updating it", async () => {
  for (const change of [{ job_id: "newer-job", status: "completed" }, { challenge_id: 99 }, { status: "unknown" }]) {
    const h = harness();
    const original = job({ job_id: "old/job?owned" });
    h.respond = (_url, options) => options.method === "POST" ? { ...original, ...change } : list([original]);
    await h.settle(); h.retry().props.onClick(); await flush(); await h.settle();
    assert.equal(h.posts()[0].url, "/api/learn/challenges/7/cleanup/retry?job_id=old%2Fjob%3Fowned");
    assert.equal(h.posts().length, 1); assert.equal(h.gets().length, 2);
    assert.deepEqual(plain(h.values.get("jobs")), [original]);
    assert.deepEqual(h.toasts, [["error", "admin:cleanupRetryFailed"]]);
  }
});

test("a slow active poll is not restarted every five seconds", async () => {
  const pending = deferred(), h = harness(); h.respond = () => list([job({ status: "processing" })]); await h.settle();
  h.respond = () => pending.promise; const timer = [...h.timers.values()][0]; timer.fn(); await flush();
  const signal = h.gets().at(-1).options.signal; timer.fn(); timer.fn(); await flush();
  assert.equal(h.gets().length, 2); assert.equal(signal.aborted, false);
  pending.resolve(list()); await flush(); assert.deepEqual(await h.settle(), []); assert.equal(h.timers.size, 0);
});

for (const kind of ["list", "retry"]) {
  for (const transition of ["unmount", "owner-epoch"]) {
    test(`late ${kind} responses cannot change state after ${transition}`, async () => {
      for (const outcome of ["resolve", "reject"]) {
        const pending = deferred(), h = harness();
        if (kind === "list") { h.respond = () => pending.promise; h.render(); await h.commit(); }
        else { h.respond = () => list([job()]); await h.settle(); h.respond = () => pending.promise; h.retry().props.onClick(); await flush(); }
        if (transition === "unmount") h.unmount(); else h.epoch++;
        const previousJobs = plain(h.values.get("jobs"));
        const previousLoading = h.values.get("loading"), previousRetrying = plain(h.values.get("retryingIds"));
        pending[outcome](outcome === "reject" ? new ApiError("Old request")
          : kind === "list" ? list([job({ challenge_id: 999 })]) : job({ status: "completed" }));
        await flush(); assert.deepEqual(plain(h.values.get("jobs")), previousJobs); assert.equal(h.toasts.length, 0);
        assert.equal(h.values.get("loading"), previousLoading);
        assert.deepEqual(plain(h.values.get("retryingIds")), previousRetrying);
        if (transition === "unmount") assert.equal(h.calls.at(-1).options.signal.aborted, true);
      }
    });
  }
}

test("a malformed successful list is reported as an error instead of a successful empty state", async () => {
  const h = harness(); h.respond = () => ({ total: 0 }); const tree = await h.settle();
  assert(tree.find((node) => node.props.role === "alert")); assert(h.refresh());
});

test("cleanup texts exist in all four locales and preserve the challenge ID parameter", () => {
  const keys = ["cleanupTitle", "cleanupChallenge", "cleanupAttempts", "cleanupStatus_pending", "cleanupStatus_processing",
    "cleanupStatus_failed", "cleanupStatus_completed", "cleanupRefresh", "cleanupRetry", "cleanupLoadFailed", "cleanupRetryFailed", "cleanupQueued"];
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).admin;
    for (const key of keys) assert.equal(typeof messages[key], "string", `${locale}:${key}`);
    assert(messages.cleanupRetry.includes("{id}"));
  }
});

test("the existing delete confirmation refreshes cleanup jobs only after a successful DELETE", async () => {
  const page = readFileSync(new URL("../src/app/[locale]/admin/challenges/page.tsx", import.meta.url), "utf8");
  const pageAst = ts.createSourceFile("page.tsx", page, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  let handler;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(pageAst) === "handleDeleteConfirm") handler = node.initializer.getText(pageAst);
    ts.forEachChild(node, find);
  }
  find(pageAst); assert(handler);
  for (const fails of [false, true]) {
    const events = [], target = { id: 7 }, context = { isSuperAdmin: true, deleteStep: 3, deleteTarget: target,
      setDeleting: (value) => events.push(["deleting", value]),
      setChallenges: (update) => events.push(["challenges", plain(update([target, { id: 8 }]))]),
      closeDeleteModal: () => events.push(["close"]),
      setCleanupRefreshVersion: (update) => events.push(["cleanup-refresh", update(10)]),
      apiFetch: async (url, options) => { events.push([url, options.method]); if (fails) throw new ApiError("failed"); return { cleanup_status: "pending" }; },
      toast: (...args) => events.push(["toast", ...args]), t: (key) => key, ApiError };
    vm.runInNewContext(ts.transpileModule(`globalThis.run = ${handler}`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
    await context.run();
    assert.equal(events.filter(([event]) => event === "cleanup-refresh").length, fails ? 0 : 1);
    if (!fails) { assert.deepEqual(events.find(([event]) => event === "challenges"), ["challenges", [{ id: 8 }]]); assert(events.some((event) => event[0] === "toast" && event[2] === "cleanupQueued")); }
  }
  assert.match(page, /isSuperAdmin && <ChallengeCleanupJobs refreshVersion=\{cleanupRefreshVersion\}/);
});
