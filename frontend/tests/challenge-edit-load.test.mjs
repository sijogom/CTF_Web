import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import {
  registerUnsavedNavigationGuard, requestCoordinatedNavigation, resetUnsavedNavigationCoordinatorForTests,
} from "../src/lib/unsaved-navigation-coordinator.mjs";

const require = createRequire(import.meta.url);
const text = readFileSync(new URL("../src/app/[locale]/admin/challenges/[id]/edit/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("challenge-edit.tsx", text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const response = (id, name = `Challenge ${id}`) => ({ id, name, description: "draft", flags: [], thumbnail_url: `image-${id}.png` });

function harness() {
  resetUnsavedNavigationCoordinatorForTests();
  const states = [], memos = [], callbacks = [], layouts = [], effects = [], requests = [], stateWrites = [];
  let stateIndex, memoIndex, callbackIndex, layoutIndex, effectIndex, id = "1", userId = "A", epoch = 1, captureFailure = false;
  const same = (left, right) => left?.length === right.length && left.every((value, index) => value === right[index]);
  const hookEffect = (entries, index, callback, dependencies) => {
    if (!same(entries[index]?.dependencies, dependencies)) entries[index] = {
      dependencies, callback, oldCleanup: entries[index]?.cleanup,
    };
  };
  const context = { exports: {}, AbortController, require: (name) => {
    if (name === "react") return {
      useState: (initial) => { const index = stateIndex++; if (!(index in states)) states[index] = initial;
        return [states[index], (value) => { states[index] = value; stateWrites.push(value); }]; },
      useMemo: (factory, dependencies) => { const index = memoIndex++;
        if (!same(memos[index]?.dependencies, dependencies)) memos[index] = { dependencies, value: factory() };
        return memos[index].value; },
      useCallback: (callback, dependencies) => { const index = callbackIndex++;
        if (!same(callbacks[index]?.dependencies, dependencies)) callbacks[index] = { dependencies, value: callback };
        return callbacks[index].value; },
      useLayoutEffect: (callback, dependencies) => hookEffect(layouts, layoutIndex++, callback, dependencies),
      useEffect: (callback, dependencies) => hookEffect(effects, effectIndex++, callback, dependencies),
    };
    if (name === "react/jsx-runtime") return require(name);
    if (name === "next/navigation") return { useParams: () => ({ id }) };
    if (name === "next-intl") return { useTranslations: () => (key) => key };
    if (name === "@/providers/AuthProvider") return { useAuth: () => ({ user: userId ? { id: userId } : null }) };
    if (name === "@/components/admin/challenges/ChallengeWizard") return { ChallengeWizard: "wizard",
      createDefaultDynamicFlagPolicy: () => ({ enabled: true, delivery: "file", runtimePath: "/flag.txt", ttlSeconds: 3600, hmacKeyVersion: "v1" }) };
    if (name === "@/lib/api-client") return {
      captureApiRequestOwner: (expectedId) => {
        assert.equal(expectedId, userId);
        if (captureFailure || !userId) throw new Error("session unavailable");
        return { ownerId: expectedId, epoch };
      },
      isCurrentApiRequestOwner: (owner) => owner.ownerId === userId && owner.epoch === epoch,
      apiFetch: (path, options) => { const pending = deferred(); requests.push({ path, options, ...pending }); return pending.promise; },
    };
    if (name === "@/lib/use-unsaved-changes-guard") return { runGuardedNavigation: requestCoordinatedNavigation };
    throw new Error(name);
  } };
  vm.runInNewContext(ts.transpileModule(text, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, context, { timeout: 1000 });
  const commit = (entries) => { for (const entry of entries) if (entry.callback) {
    entry.oldCleanup?.(); entry.cleanup = entry.callback(); entry.callback = null;
  } };
  const render = ({ commitEffects = true } = {}) => {
    stateIndex = 0; memoIndex = 0; callbackIndex = 0; layoutIndex = 0; effectIndex = 0;
    const tree = context.exports.default();
    if (commitEffects) { commit(layouts); commit(effects); }
    return tree;
  };
  return { render, requests, stateWrites, states, callbacks,
    load: () => callbacks[0].value(), requestLoad: () => callbacks[1].value(),
    route: (value) => { id = value; }, account: (value) => { userId = value; epoch += 1; },
    newEpoch: () => { epoch += 1; }, failCapture: (value) => { captureFailure = value; },
    unmount: () => { for (const entry of [...layouts, ...effects]) entry.cleanup?.(); resetUnsavedNavigationCoordinatorForTests(); },
  };
}
function wizard(tree) { return tree.type === "wizard" ? tree : null; }

test("the editor exposes no Wizard before a current owner-bound GET succeeds", async () => {
  const h = harness();
  try {
    assert.equal(wizard(h.render()), null);
    assert.equal(h.requests[0].path, "/api/learn/challenges/1");
    assert.equal(h.requests[0].options.requestOwner.ownerId, "A");
    assert.equal(h.requests[0].options.signal.aborted, false);
    h.requests[0].resolve(response(1)); await tick();
    const ready = wizard(h.render());
    assert.equal(ready.props.challengeId, 1); assert.equal(ready.props.initialData.thumbnailUrl, "image-1.png");
    assert.equal(ready.props.mode, "edit");
  } finally { h.unmount(); }
});
for (const outcome of ["success", "failure"]) {
  test(`an old route GET ${outcome} cannot overwrite a newer editor or its error`, async () => {
    const h = harness();
    try {
      h.render(); const old = h.requests[0];
      h.route("2"); assert.equal(wizard(h.render()), null); assert.equal(old.options.signal.aborted, true);
      h.requests[1].resolve(response(2)); await tick(); const expected = wizard(h.render());
      if (outcome === "success") old.resolve(response(1, "STALE")); else old.reject(new Error("old error"));
      await tick(); const current = wizard(h.render());
      assert.equal(current.key, expected.key); assert.equal(current.props.initialData.title, "Challenge 2");
      assert.equal(current.props.challengeId, 2);
    } finally { h.unmount(); }
  });
}
test("committed route changes mask old data before the new passive GET starts and remount the Wizard", async () => {
  const h = harness();
  try {
    h.render(); h.requests[0].resolve(response(1)); await tick(); const first = wizard(h.render());
    h.route("2"); assert.equal(wizard(h.render({ commitEffects: false })), null);
    assert.equal(h.requests.length, 1); h.render();
    h.requests[1].resolve(response(2)); await tick(); const second = wizard(h.render());
    assert.notEqual(second.key, first.key); assert.equal(second.props.initialData.title, "Challenge 2");
  } finally { h.unmount(); }
});
test("account changes hide old private content and abort old requests even for the same challenge ID", async () => {
  const h = harness();
  try {
    h.render(); const old = h.requests[0];
    h.account("B"); assert.equal(wizard(h.render()), null); assert.equal(old.options.signal.aborted, true);
    assert.equal(h.requests[1].options.requestOwner.ownerId, "B");
    old.resolve(response(1, "A PRIVATE")); await tick(); assert.equal(wizard(h.render()), null);
    h.requests[1].resolve(response(1, "B CONTENT")); await tick();
    assert.equal(wizard(h.render()).props.initialData.title, "B CONTENT");
  } finally { h.unmount(); }
});
test("same-user owner epoch changes before response consumption fail closed but permit a manual retry", async () => {
  const h = harness();
  try {
    h.render(); h.newEpoch(); h.requests[0].resolve(response(1)); await tick();
    assert.equal(wizard(h.render()), null); assert.equal(h.states[0].status, "error");
    h.requestLoad(); assert.equal(h.requests.length, 2);
    h.requests[1].resolve(response(1)); await tick(); assert.ok(wizard(h.render()));
  } finally { h.unmount(); }
});
test("same-route requests abort earlier generations and remount only from the newest successful read", async () => {
  const h = harness();
  try {
    h.render(); h.requests[0].resolve(response(1)); await tick(); const first = wizard(h.render());
    h.requestLoad(); h.requestLoad(); assert.equal(h.requests.length, 3);
    assert.equal(h.requests[1].options.signal.aborted, true); assert.equal(wizard(h.render()), null);
    h.requests[2].resolve(response(1, "CURRENT")); await tick(); const latest = wizard(h.render());
    h.requests[1].resolve(response(1, "OLD RELOAD")); await tick();
    assert.equal(wizard(h.render()).props.initialData.title, "CURRENT");
    assert.equal(wizard(h.render()).key, latest.key); assert.notEqual(latest.key, first.key);
  } finally { h.unmount(); }
});
test("same-route reload shares the existing dirty/busy coordinator instead of discarding drafts directly", async () => {
  const h = harness(); let dirty = true, busy = false, confirm, cancel, confirmations = 0;
  let unregister;
  try {
    h.render(); h.requests[0].resolve(response(1)); await tick(); const initial = wizard(h.render());
    unregister = registerUnsavedNavigationGuard({ isDirty: () => dirty, isBusy: () => busy,
      requestConfirmation: (yes, no) => { confirmations += 1; confirm = yes; cancel = no; } });
    h.requestLoad(); h.requestLoad(); assert.equal(confirmations, 1); assert.equal(h.requests.length, 1);
    assert.equal(wizard(h.render()).key, initial.key);
    cancel(); assert.equal(h.requests.length, 1); assert.equal(wizard(h.render()).key, initial.key);
    busy = true; h.requestLoad(); assert.equal(h.requests.length, 1); assert.equal(confirmations, 1);
    busy = false; h.requestLoad(); assert.equal(confirmations, 2); confirm();
    assert.equal(h.requests.length, 2); dirty = false;
    h.requests[1].resolve(response(1, "RELOADED")); await tick();
    assert.notEqual(wizard(h.render()).key, initial.key);
  } finally { unregister?.(); h.unmount(); }
});
test("ordinary rerenders with fresh translation function identities preserve loaded drafts without another GET", async () => {
  const h = harness();
  try {
    h.render(); h.requests[0].resolve(response(1)); await tick(); const first = wizard(h.render());
    for (let index = 0; index < 3; index += 1) assert.equal(wizard(h.render()).key, first.key);
    assert.equal(h.requests.length, 1);
  } finally { h.unmount(); }
});
for (const outcome of ["success", "failure"]) {
  test(`unmount aborts a GET and its late ${outcome} performs no state writes`, async () => {
    const h = harness(); h.render(); const count = h.stateWrites.length;
    h.unmount(); assert.equal(h.requests[0].options.signal.aborted, true);
    if (outcome === "success") h.requests[0].resolve(response(1)); else h.requests[0].reject(new Error("late"));
    await tick(); assert.equal(h.stateWrites.length, count);
  });
}
test("capture failures and failed reads show recoverable errors rather than an empty or writable editor", async () => {
  const h = harness();
  try {
    h.failCapture(true); h.render(); await tick();
    assert.equal(h.requests.length, 0); assert.equal(h.states[0].status, "error"); assert.equal(wizard(h.render()), null);
    h.failCapture(false); h.requestLoad(); h.requests[0].reject(new Error("read failed")); await tick();
    assert.equal(h.states[0].status, "error"); h.requestLoad();
    h.requests[1].resolve(response(1)); await tick(); assert.ok(wizard(h.render()));
  } finally { h.unmount(); }
});
test("a mismatched response ID never becomes the editor's save target", async () => {
  const h = harness();
  try {
    h.render(); h.requests[0].resolve(response(2)); await tick();
    assert.equal(h.states[0].status, "error"); assert.equal(wizard(h.render()), null);
  } finally { h.unmount(); }
});
test("invalid route IDs perform no API calls and display a finite error state", () => {
  for (const id of ["0", "-1", "1.5", "NaN", "Infinity", "garbage"]) {
    const h = harness();
    try { h.route(id); const tree = h.render(); assert.equal(h.requests.length, 0); assert.equal(wizard(tree), null);
      assert.equal(tree.props.children[0].props.role, "alert"); }
    finally { h.unmount(); }
  }
});
