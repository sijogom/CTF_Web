import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/app/[locale]/admin/tiers/page.tsx", import.meta.url), "utf8");
const previewSource = readFileSync(new URL("../src/components/admin/tiers/TierImagePreview.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("admin-tiers.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
function findNodes(predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  return matches;
}
function callback(name) {
  const matches = findNodes((node) => ts.isVariableDeclaration(node) && node.name.getText(ast) === name);
  assert.equal(matches.length, 1);
  const value = matches[0].initializer;
  return (ts.isCallExpression(value) ? value.arguments[0] : value).getText(ast);
}
function lifecycle() {
  const matches = findNodes((node) => ts.isCallExpression(node) && node.expression.getText(ast) === "useEffect"
    && node.arguments[0].getText(ast).includes("mountedRef.current = true"));
  assert.equal(matches.length, 1);
  return matches[0].arguments[0].getText(ast);
}
const payloadFunction = findNodes((node) => ts.isFunctionDeclaration(node) && node.name?.text === "tierPayload")[0].getText(ast);
const plain = (value) => JSON.parse(JSON.stringify(value));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const tier = (name, required_xp = 0, image_url = `${name}.png`) => ({ name, required_xp, image_url });

function harness() {
  const state = { tiers: [], loading: false, loaded: true, loadError: null, saving: false, uploadingRowId: null };
  const requests = [], uploads = [], toasts = [];
  let currentOwner = { ownerId: "admin", epoch: 1 };
  const context = { AbortController, state, currentUser: { id: "admin" }, superAdmin: true,
    tiersRef: { current: [] }, nextRowIdRef: { current: 0 }, loadedForUserRef: { current: "admin" },
    mountedRef: { current: true }, loadControllerRef: { current: null }, operationRef: { current: null },
    requestGenerationRef: { current: 0 }, t: (key) => key, toast: (...args) => toasts.push(args),
    ApiError: class ApiError extends Error {},
    FormData: class FormData { values = []; append(...args) { this.values.push(args); } },
    captureApiRequestOwner: (id) => {
      if (id !== currentOwner.ownerId) throw new Error("owner_changed");
      return currentOwner;
    },
    isCurrentApiRequestOwner: (owner) => owner === currentOwner,
    apiFetch: (pathname, options = {}) => {
      const pending = deferred(); requests.push({ pathname, options, ...pending }); return pending.promise;
    },
    apiFormFetch: (pathname, data, options) => {
      const pending = deferred(); uploads.push({ pathname, data, options, ...pending }); return pending.promise;
    },
  };
  for (const key of Object.keys(state)) context[`set${key[0].toUpperCase()}${key.slice(1)}`] = (value) => {
    state[key] = typeof value === "function" ? value(state[key]) : value;
  };
  const names = ["replaceTiers", "createTierRow", "loadTiers", "canEdit", "addTier", "removeTier", "updateTier",
    "beginOperation", "isCurrentOperation", "finishOperation", "handleUpload", "handleSave"];
  const program = `${payloadFunction}
    globalThis.bind = (user, isSuperAdmin) => {
      ${names.map((name) => `const ${name} = ${callback(name)};`).join("\n")}
      return { ${names.join(", ")}, lifecycle: ${lifecycle()} };
    };`;
  vm.runInNewContext(ts.transpileModule(program, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  } }).outputText, context, { timeout: 1000 });
  const render = () => context.bind(context.currentUser, context.superAdmin);
  return { state, context, requests, uploads, toasts, render,
    rows(...values) {
      const actions = render(); actions.replaceTiers(values.map(actions.createTierRow));
      return state.tiers.map((row) => row.rowId);
    },
    changeOwner(id = "other-admin") {
      currentOwner = { ownerId: id, epoch: currentOwner.epoch + 1 };
    },
    setUser(id) { context.currentUser = { id }; },
    async readyLoad(values) {
      const pending = render().loadTiers(); requests.at(-1).resolve({ tiers: values }); await pending;
    },
  };
}

test("local tier identity survives edits, preceding-row deletion and XP sorting without entering the API payload", async () => {
  const h = harness(); const [first, second] = h.rows(tier("A", 200), tier("B", 100));
  h.render().updateTier(second, "name", "Edited B");
  h.render().updateTier(second, "image_url", "valid-B.png");
  h.render().removeTier(first); h.render().addTier();
  const third = h.state.tiers[1].rowId;
  assert.notEqual(third, first); assert.notEqual(third, second);
  h.render().updateTier(third, "name", "C"); h.render().updateTier(third, "required_xp", 10);
  const save = h.render().handleSave();
  assert.equal(h.requests[0].pathname, "/api/auth/admin/settings");
  assert.equal(h.requests[0].options.method, "PATCH");
  assert.deepEqual(plain(h.requests[0].options.body), { tiers: [tier("C", 10, ""), tier("Edited B", 100, "valid-B.png")] });
  h.requests[0].resolve({}); await save;
  assert.deepEqual(plain(h.state.tiers.map((row) => row.rowId)), [third, second]);
});

test("unnamed rows retain the existing save filter and do not consume another row's identity", async () => {
  const h = harness(); const [first, second] = h.rows(tier("  "), tier("Named", 20));
  const save = h.render().handleSave();
  assert.deepEqual(plain(h.requests[0].options.body), { tiers: [tier("Named", 20)] });
  h.requests[0].resolve({}); await save;
  assert.deepEqual(plain(h.state.tiers.map((row) => row.rowId)), [second]);
  h.render().addTier(); assert.notEqual(h.state.tiers[1].rowId, first);
});

test("same-tick upload duplicate, save, and draft mutations are blocked by the synchronous operation lease", async () => {
  const h = harness(); const [id] = h.rows(tier("A")); const actions = h.render();
  const upload = actions.handleUpload(id, { name: "one.png" });
  await actions.handleUpload(id, { name: "two.png" }); await actions.handleSave();
  actions.addTier(); actions.removeTier(id); actions.updateTier(id, "name", "blocked");
  assert.equal(h.uploads.length, 1); assert.equal(h.requests.length, 0);
  assert.equal(h.state.tiers.length, 1); assert.equal(h.state.tiers[0].name, "A");
  assert.equal(h.state.uploadingRowId, id);
  h.uploads[0].resolve({ url: "uploaded.png" }); await upload;
  assert.equal(h.state.tiers[0].image_url, "uploaded.png"); assert.equal(h.state.uploadingRowId, null);
});

test("same-tick save duplicate and upload cannot overlap the captured save", async () => {
  const h = harness(); const [id] = h.rows(tier("A")); const actions = h.render();
  const save = actions.handleSave(); await actions.handleSave(); await actions.handleUpload(id, {});
  assert.equal(h.requests.length, 1); assert.equal(h.uploads.length, 0); assert.equal(h.state.saving, true);
  h.requests[0].resolve({}); await save; assert.equal(h.state.saving, false);
});

test("upload after a preceding-row removal targets the surviving identity, not its former array index", async () => {
  const h = harness(); const [first, second] = h.rows(tier("A"), tier("B"));
  h.render().removeTier(first); await h.render().handleUpload(first, {});
  assert.equal(h.uploads.length, 0);
  const upload = h.render().handleUpload(second, {});
  h.uploads[0].resolve({ url: "B-upload.png" }); await upload;
  assert.deepEqual(plain(h.state.tiers), [{ ...tier("B", 0, "B-upload.png"), rowId: second }]);
});

for (const outcome of ["failure", "invalid-response"]) {
  test(`current upload ${outcome} preserves the previous URL and draft, then permits explicit retry`, async () => {
    const h = harness(); const [id] = h.rows(tier("A", 42, "previous.png"));
    const before = plain(h.state.tiers); const first = h.render().handleUpload(id, {});
    if (outcome === "failure") h.uploads[0].reject(new Error("upload_failure"));
    else h.uploads[0].resolve({ url: " " });
    await first; assert.deepEqual(plain(h.state.tiers), before);
    assert.equal(h.state.uploadingRowId, null); assert.deepEqual(h.toasts[0], ["error", "tierImageUploadFailed"]);
    assert.equal(h.uploads.length, 1);
    const retry = h.render().handleUpload(id, {}); h.uploads[1].resolve({ url: "recovered.png" }); await retry;
    assert.equal(h.state.tiers[0].image_url, "recovered.png");
  });
}

test("current save failure keeps the entire draft and stable IDs for a deliberate retry", async () => {
  const h = harness(); h.rows(tier("B", 200), tier("A", 10)); const before = plain(h.state.tiers);
  const save = h.render().handleSave(); h.requests[0].reject(new Error("save_failure")); await save;
  assert.deepEqual(plain(h.state.tiers), before); assert.equal(h.state.saving, false);
  assert.deepEqual(h.toasts, [["error", "tierSaveFailed"]]);
  const retry = h.render().handleSave(); h.requests[1].resolve({}); await retry;
  assert.deepEqual(plain(h.state.tiers.map((row) => row.rowId)), [before[1].rowId, before[0].rowId]);
});

test("an owner change before React commit blocks old callbacks from uploading, saving or editing", async () => {
  const h = harness(); const [id] = h.rows(tier("A")); const old = h.render(); const before = plain(h.state.tiers);
  h.changeOwner(); old.updateTier(id, "image_url", "wrong.png"); old.removeTier(id); old.addTier();
  await old.handleUpload(id, {}); await old.handleSave();
  assert.equal(h.uploads.length, 0); assert.equal(h.requests.length, 0); assert.deepEqual(plain(h.state.tiers), before);
});

for (const kind of ["upload", "save"]) for (const outcome of ["success", "failure"]) {
  test(`late ${kind} ${outcome} from a previous owner cannot affect the current draft or show a toast`, async () => {
    const h = harness(); const [id] = h.rows(tier("A"));
    const old = kind === "upload" ? h.render().handleUpload(id, {}) : h.render().handleSave();
    const request = kind === "upload" ? h.uploads[0] : h.requests[0];
    h.changeOwner(); const before = plain(h.state.tiers);
    if (outcome === "success") request.resolve({ url: "wrong-owner.png" }); else request.reject(new Error("late_failure"));
    await old; assert.deepEqual(plain(h.state.tiers), before); assert.equal(h.toasts.length, 0);
  });
}

test("unmount aborts an in-flight upload and discards its late completion", async () => {
  const h = harness(); const actions = h.render(); const unmount = actions.lifecycle();
  h.requests[0].resolve({ tiers: [tier("A")] }); await tick();
  const id = h.state.tiers[0].rowId; const before = plain(h.state.tiers);
  const upload = actions.handleUpload(id, {}); unmount();
  assert.equal(h.uploads[0].options.signal.aborted, true); assert.equal(h.requests[0].options.signal.aborted, true);
  h.uploads[0].resolve({ url: "unmounted.png" }); await upload;
  assert.deepEqual(plain(h.state.tiers), before); assert.equal(h.toasts.length, 0);
});

test("an old upload finally cannot release a new owner's operation lease after cleanup and reload", async () => {
  const h = harness(); const unmount = h.render().lifecycle(); h.requests[0].resolve({ tiers: [tier("A")] }); await tick();
  const old = h.render().handleUpload(h.state.tiers[0].rowId, {}); unmount();
  h.changeOwner(); h.setUser("other-admin"); h.render().lifecycle();
  h.requests[1].resolve({ tiers: [tier("B")] }); await tick();
  const currentId = h.state.tiers[0].rowId; const current = h.render().handleUpload(currentId, {});
  h.uploads[0].resolve({ url: "old.png" }); await old;
  assert.equal(h.state.uploadingRowId, currentId); assert.notEqual(h.context.operationRef.current, null);
  h.uploads[1].resolve({ url: "current.png" }); await current;
  assert.equal(h.state.tiers[0].image_url, "current.png"); assert.equal(h.state.uploadingRowId, null);
});

test("failed loading cannot save an empty replacement, and a retry enables editing only after a valid response", async () => {
  const h = harness(); const load = h.render().loadTiers(); h.requests[0].reject(new Error("unavailable")); await load;
  await h.render().handleSave(); h.render().addTier();
  assert.equal(h.state.loaded, false); assert.equal(h.state.loadError, "settingsLoadFailed");
  assert.equal(h.requests.length, 1); assert.equal(h.state.tiers.length, 0);
  await h.readyLoad([tier("A")]); const save = h.render().handleSave(); h.requests[2].resolve({}); await save;
  assert.equal(h.state.loaded, true); assert.equal(h.state.loadError, null);
});

test("superseded and wrong-owner settings responses cannot replace a newer owner's rows", async () => {
  const h = harness(); const first = h.render().loadTiers(); const second = h.render().loadTiers();
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.requests[1].resolve({ tiers: [tier("current")] }); await second;
  const before = plain(h.state.tiers); h.requests[0].resolve({ tiers: [tier("stale")] }); await first;
  assert.deepEqual(plain(h.state.tiers), before);
  const third = h.render().loadTiers(); h.changeOwner(); h.requests[2].resolve({ tiers: [tier("other")] }); await third;
  assert.deepEqual(plain(h.state.tiers), before); await h.render().handleSave(); assert.equal(h.requests.length, 3);
});

test("non-super-admin callbacks cannot change draft or issue settings/upload writes", async () => {
  const h = harness(); const [id] = h.rows(tier("A")); h.context.superAdmin = false;
  h.render().removeTier(id); h.render().updateTier(id, "name", "wrong"); h.render().addTier();
  await h.render().handleUpload(id, {}); await h.render().handleSave();
  assert.equal(h.state.tiers[0].name, "A"); assert.equal(h.requests.length, 0); assert.equal(h.uploads.length, 0);
});

function previewHarness() {
  const states = new Map(); let activeKey;
  const exports = {};
  const context = { exports, require(name) {
    if (name === "react") return { useState(initial) {
      const key = activeKey; if (!states.has(key)) states.set(key, { value: initial });
      const cell = states.get(key);
      return [cell.value, (value) => { cell.value = value; }];
    } };
    if (name === "lucide-react") return { Award: "Award", ImageOff: "ImageOff" };
    if (name === "react/jsx-runtime") return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
    throw new Error(`unexpected module: ${name}`);
  } };
  vm.runInNewContext(ts.transpileModule(previewSource, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, context, { timeout: 1000 });
  const previewNode = findNodes((node) => ts.isJsxSelfClosingElement(node) && node.tagName.getText(ast) === "TierImagePreview")[0];
  const key = previewNode.attributes.properties.find((attribute) => attribute.name?.text === "key").initializer.expression.getText(ast);
  const keyFor = vm.runInNewContext(`(tier) => ${key}`);
  return (rows) => {
    const keys = new Set(rows.map(keyFor));
    for (const key of states.keys()) if (!keys.has(key)) states.delete(key);
    return rows.map((row) => {
      activeKey = keyFor(row);
      return exports.TierImagePreview({ imageUrl: row.image_url, name: row.name, errorLabel: "preview_failed" }).props.children;
    });
  };
}

test("declarative preview resets a failed source on a new URL and retry, without direct DOM styling", () => {
  const render = previewHarness(); const row = { ...tier("A", 0, "bad.png"), rowId: "A" };
  const [bad] = render([row]); assert.equal(bad.type, "img"); bad.props.onError();
  const [failed] = render([row]); assert.equal(failed.type, "ImageOff"); assert.equal(failed.props["aria-label"], "preview_failed");
  const [good] = render([{ ...row, image_url: "good.png" }]); assert.equal(good.type, "img"); assert.equal(good.props.src, "good.png");
  const [retry] = render([row]); assert.equal(retry.type, "img");
  bad.props.onError(); assert.equal(render([row])[0].type, "img");
  assert.doesNotMatch(source + previewSource, /\.style\.|key=\{index\}/);
});

test("removing an earlier failed row cannot reuse its preview state for the surviving good row", () => {
  const render = previewHarness(); const first = { ...tier("A", 0, "bad.png"), rowId: "A" };
  const second = { ...tier("B", 0, "good.png"), rowId: "B" };
  render([first, second])[0].props.onError(); assert.equal(render([first, second])[0].type, "ImageOff");
  assert.equal(render([second])[0].type, "img"); assert.equal(render([second])[0].props.src, "good.png");
  const rowKey = findNodes((node) => ts.isJsxAttribute(node) && node.name.getText(ast) === "key")
    .map((node) => node.initializer.getText(ast));
  assert.ok(rowKey.includes("{tier.rowId}"));
});

test("tier upload is keyboard reachable, resets file selection immediately, and wraps rows on narrow screens", () => {
  const input = findNodes((node) => ts.isJsxSelfClosingElement(node) && node.tagName.getText(ast) === "input"
    && node.attributes.getText(ast).includes('type="file"'))[0];
  assert.match(input.attributes.getText(ast), /className="sr-only"/);
  assert.match(input.attributes.getText(ast), /e\.currentTarget\.value = ""/);
  assert.match(input.attributes.getText(ast), /handleUpload\(tier\.rowId, file\)/);
  assert.match(source, /flex min-w-0 flex-wrap items-start/);
  assert.match(source, /focus-within:ring-2/);
});
