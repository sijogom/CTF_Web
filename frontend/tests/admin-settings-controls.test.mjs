import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import vm from "node:vm";

const require = createRequire(import.meta.url), ts = require("typescript");
const frontend = path.dirname(path.dirname(path.dirname(require.resolve("typescript/package.json"))));
const { createAuthRequestCoordinator, AuthRequestBlockedError } = await import(pathToFileURL(path.join(frontend, "src/lib/auth-request-coordinator.mjs")));
const { normalizeApiErrorPayload } = await import(pathToFileURL(path.join(frontend, "src/lib/api-error.mjs")));
const source = readFileSync(new URL("../src/app/[locale]/admin/settings/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
const initial = { site_name: "Existing site", registration_open: false, max_containers: 73, container_timeout: 97, cache_refresh_pending: false };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function declaration(name) {
  const matches = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(ast) === name) matches.push(node.initializer);
    ts.forEachChild(node, visit);
  }
  visit(ast); assert.equal(matches.length, 1, name);
  return (ts.isCallExpression(matches[0]) ? matches[0].arguments[0] : matches[0]).getText(ast);
}
function effectCallback() {
  const matches = [];
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(ast) === "useEffect") matches.push(node.arguments[0]);
    ts.forEachChild(node, visit);
  }
  visit(ast); assert.equal(matches.length, 1); return matches[0].getText(ast);
}
function inputCallback(id, attribute = "onChange") {
  let found;
  function visit(node) {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && node.attributes.properties.some((item) => item.name?.getText(ast) === "id" && item.initializer.text === id)) found = node;
    if (id === "registration" && ts.isJsxOpeningElement(node) && node.attributes.properties.some((item) => item.name?.getText(ast) === "role" && item.initializer.text === "switch")) found = node;
    ts.forEachChild(node, visit);
  }
  visit(ast); assert(found, id);
  return found.attributes.properties.find((item) => item.name?.getText(ast) === attribute).initializer.expression.getText(ast);
}
const response = (value, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => value });
function harness(options = {}) {
  const coordinator = createAuthRequestCoordinator(), session = { status: "authenticated", authenticated: true, ownerId: "synthetic-A", epoch: 1 };
  coordinator.register({ snapshot: () => ({ ...session }), verify: async () => session.status });
  const calls = [], writes = [], events = [], loads = [];
  let stored = { ...initial };
  const apiContext = { exports: {}, process: { env: {} }, URL, FormData, document: { cookie: "ctf_csrf_token=synthetic" }, window: { location: { origin: "http://pure.test" } },
    require: (name) => {
      if (name === "@/lib/auth-request-coordinator.mjs") return { AuthRequestBlockedError, authRequestCoordinator: coordinator };
      if (name === "@/lib/api-error.mjs") return { normalizeApiErrorPayload };
      throw Error(`Unexpected dependency: ${name}`);
    },
    fetch: async (url, request) => { calls.push({ url, request, ownerId: session.ownerId }); return apiContext.respond(url, request); },
    respond: options.respond ?? (async (_url, request) => {
      if (request.method === "PATCH") { stored = { ...stored, ...JSON.parse(request.body) }; return response({ cache_refresh_pending: stored.cache_refresh_pending }); }
      return response({ ...stored });
    }),
  };
  vm.runInNewContext(ts.transpileModule(readFileSync(path.join(frontend, "src/lib/api-client.ts"), "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, apiContext, { timeout: 1000 });
  const context = { ...apiContext.exports, user: options.nullUser ? null : { id: session.ownerId }, isSuperAdmin: options.isSuperAdmin ?? true,
    siteName: "Default site", registrationOpen: true, maxContainers: 50, containerTimeout: 30, cacheRefreshPending: false,
    loading: true, loadedForUser: null, loadError: null, saving: false, message: null, DEFAULT_SITE_NAME: "Default site", SITE_SETTINGS_UPDATED_EVENT: "settings-updated",
    mountedRef: { current: false }, requestGenerationRef: { current: 0 }, loadRequestIdRef: { current: 0 }, loadingRef: { current: true },
    loadedForUserRef: { current: null }, loadControllerRef: { current: null }, operationRef: { current: null }, AbortController, Event,
    t: (key) => key, window: { dispatchEvent: (event) => events.push(event.type) },
  };
  for (const name of ["SiteName", "RegistrationOpen", "MaxContainers", "ContainerTimeout", "CacheRefreshPending", "Loading", "LoadedForUser", "LoadError", "Saving", "Message"]) {
    const key = name[0].toLowerCase() + name.slice(1);
    context[`set${name}`] = (value) => { context[key] = typeof value === "function" ? value(context[key]) : value; writes.push({ key, value: context[key] }); };
  }
  const evaluate = (code) => vm.runInNewContext(ts.transpile(`(${code})`, { target: ts.ScriptTarget.ES2022 }), context);
  context.isCurrentOperation = evaluate(declaration("isCurrentOperation"));
  const loadSettings = evaluate(declaration("loadSettings"));
  context.loadSettings = (...args) => { const promise = loadSettings(...args); loads.push(promise); return promise; };
  context.canEdit = evaluate(declaration("canEdit"));
  const save = evaluate(declaration("handleSave"));
  const edit = Object.fromEntries(["site-name", "max-containers", "container-timeout"].map((id) => [id, evaluate(inputCallback(id))]));
  edit.registration = evaluate(inputCallback("registration", "onClick"));
  let cleanup;
  const mount = () => { cleanup = evaluate(effectCallback())(); };
  mount();
  return { context, calls, writes, events, loads, session, coordinator, apiContext, save, edit, initialLoad: loads[0],
    unmount() { cleanup(); },
    switchOwner() { session.ownerId = "synthetic-B"; session.epoch++; },
    commitOwner() { cleanup(); context.user = { id: session.ownerId }; mount(); },
  };
}
const values = (h) => ({ site_name: h.context.siteName, registration_open: h.context.registrationOpen, max_containers: h.context.maxContainers, container_timeout: h.context.containerTimeout });
const mutations = (h) => h.calls.filter((call) => call.request.method === "PATCH");

test("moderator keeps actual GET values readonly and null user sends GET/PATCH0", async () => {
  const moderator = harness({ isSuperAdmin: false }); await moderator.initialLoad;
  assert.equal(moderator.calls.length, 1); assert.equal(moderator.calls[0].request.method, "GET");
  const before = values(moderator);
  moderator.edit["site-name"]({ target: { value: "forbidden edit" } }); moderator.edit.registration();
  moderator.edit["max-containers"]({ target: { value: "9" } }); moderator.edit["container-timeout"]({ target: { value: "10" } }); await moderator.save();
  assert.deepEqual(values(moderator), before); assert.equal(mutations(moderator).length, 0);
  for (const isSuperAdmin of [false, true]) {
    const anonymous = harness({ nullUser: true, isSuperAdmin }); await anonymous.initialLoad; await anonymous.save();
    assert.equal(anonymous.calls.length, 0); assert.equal(anonymous.context.operationRef.current, null);
  }
});
test("loading and failed GET are PATCH0 and preserve existing values", async () => {
  const body = deferred(), loading = harness({ respond: async () => ({ ok: true, status: 200, json: () => body.promise }) });
  const before = values(loading); await loading.save(); loading.edit["site-name"]({ target: { value: "early" } });
  assert.equal(mutations(loading).length, 0); assert.deepEqual(values(loading), before);
  body.resolve(initial); await loading.initialLoad;
  const failed = harness({ respond: async () => response({ detail: "existing server guidance" }, 503) }); await failed.initialLoad;
  assert.equal(failed.context.loadError, "existing server guidance"); const failedBefore = values(failed); await failed.save();
  assert.equal(mutations(failed).length, 0); assert.deepEqual(values(failed), failedBefore);
});
test("actual save preserves all four submitted values and uses the same owner/signal contract", async () => {
  const h = harness(); await h.initialLoad;
  h.edit["site-name"]({ target: { value: "Changed site" } }); h.edit.registration();
  h.edit["max-containers"]({ target: { value: "81" } }); h.edit["container-timeout"]({ target: { value: "123" } });
  const expected = values(h); await h.save();
  assert.equal(mutations(h).length, 1); assert.deepEqual(JSON.parse(mutations(h)[0].request.body), expected); assert.deepEqual(values(h), expected);
  assert.equal(mutations(h)[0].url.endsWith("/api/auth/admin/settings"), true); assert.equal(mutations(h)[0].ownerId, "synthetic-A");
  assert.equal(Object.hasOwn(mutations(h)[0].request, "requestOwner"), false); assert(mutations(h)[0].request.signal instanceof AbortSignal);
  assert.deepEqual(h.events, ["settings-updated"]); assert.equal(h.context.message.type, "success"); assert.equal(h.context.message.text, "settingsSaved");
});
test("synchronous lease rejects duplicate save and blocks same-tick edits until PATCH and readback finish", async () => {
  const h = harness(); await h.initialLoad; const patchStarted = deferred(), patch = deferred(), readbackStarted = deferred(), readback = deferred();
  h.apiContext.respond = async (_url, request) => {
    if (request.method === "PATCH") { patchStarted.resolve(); return patch.promise; }
    readbackStarted.resolve(); return { ok: true, status: 200, json: () => readback.promise };
  };
  const before = values(h), saving = h.save(); await h.save(); h.edit["site-name"]({ target: { value: "lost draft" } }); h.edit.registration();
  await patchStarted.promise; assert.equal(mutations(h).length, 1); assert.deepEqual(values(h), before);
  patch.resolve(response({ cache_refresh_pending: true })); await readbackStarted.promise; await h.save();
  h.edit["max-containers"]({ target: { value: "999" } }); h.edit["container-timeout"]({ target: { value: "999" } }); assert.deepEqual(values(h), before);
  readback.resolve({ ...initial, cache_refresh_pending: true }); await saving;
  assert.equal(mutations(h).length, 1); assert.deepEqual(values(h), before); assert.equal(h.context.saving, false);
  assert.equal(h.context.message.text, "settingsSavedCachePending");
});
test("failed PATCH keeps the draft and current server message without readback or false success", async () => {
  const h = harness(); await h.initialLoad; h.edit["site-name"]({ target: { value: "Unsaved draft" } }); const before = values(h);
  h.apiContext.respond = async () => response({ detail: "server rejected settings" }, 422); await h.save();
  assert.equal(mutations(h).length, 1); assert.equal(h.calls.length, 2); assert.deepEqual(values(h), before);
  assert.equal(h.context.message.type, "error"); assert.equal(h.context.message.text, "server rejected settings"); assert.equal(h.events.length, 0);
});
test("failed save readback keeps values and blocks further PATCH without claiming success", async () => {
  const h = harness(); await h.initialLoad; const before = values(h);
  h.apiContext.respond = async (_url, request) => request.method === "PATCH" ? response({ cache_refresh_pending: false }) : response({ detail: "readback unavailable" }, 503);
  await h.save(); await h.save();
  assert.equal(mutations(h).length, 1); assert.deepEqual(values(h), before); assert.equal(h.context.loadError, "readback unavailable");
  assert.equal(h.context.message, null); assert.equal(h.events.length, 0); assert.equal(h.context.loadedForUser, null);
});
test("stale owner cannot start PATCH, and late PATCH response cannot reload or update a replacement owner", async () => {
  const stale = harness(); await stale.initialLoad; stale.switchOwner(); await stale.save(); assert.equal(mutations(stale).length, 0);
  const h = harness(); await h.initialLoad; const started = deferred(), body = deferred();
  h.apiContext.respond = async () => ({ ok: true, status: 200, json: () => { started.resolve(); return body.promise; } });
  const saving = h.save(), operation = h.context.operationRef.current; await started.promise; h.switchOwner(); body.resolve({ cache_refresh_pending: false }); await saving;
  assert.equal(h.calls.length, 2); assert.equal(mutations(h).length, 1); assert.equal(h.context.message, null); assert.equal(h.events.length, 0); assert.equal(operation.controller.signal.aborted, true);
});
test("unmounted or superseded GET cannot overwrite current values or the new loading lease", async () => {
  const oldBody = deferred(), oldStarted = deferred(), newBody = deferred(), h = harness({ respond: async () => ({ ok: true, status: 200, json: () => { oldStarted.resolve(); return oldBody.promise; } }) });
  await oldStarted.promise;
  h.switchOwner(); h.apiContext.respond = async () => ({ ok: true, status: 200, json: () => newBody.promise }); h.commitOwner();
  const newLoad = h.loads.at(-1); oldBody.resolve({ ...initial, site_name: "stale A value" }); await h.initialLoad;
  assert.equal(h.context.loading, true); assert.notEqual(h.context.siteName, "stale A value");
  newBody.resolve({ ...initial, site_name: "current B value" }); await newLoad; assert.equal(h.context.siteName, "current B value");
  const late = deferred(), lateStarted = deferred(), unmounted = harness({ respond: async () => ({ ok: true, status: 200, json: () => { lateStarted.resolve(); return late.promise; } }) });
  await lateStarted.promise; unmounted.unmount();
  const writes = unmounted.writes.length; late.resolve(initial); await unmounted.initialLoad; assert.equal(unmounted.writes.length, writes);
});
test("old save finally cannot release the new owner save operation", async () => {
  const h = harness(); await h.initialLoad; const oldStarted = deferred(), oldBody = deferred();
  h.apiContext.respond = async () => ({ ok: true, status: 200, json: () => { oldStarted.resolve(); return oldBody.promise; } });
  const oldSave = h.save(); await oldStarted.promise; h.switchOwner();
  h.apiContext.respond = async () => response(initial); h.commitOwner(); await h.loads.at(-1);
  const newStarted = deferred(), newPatch = deferred(); h.apiContext.respond = async (_url, request) => {
    if (request.method === "PATCH") { newStarted.resolve(); return newPatch.promise; } return response(initial);
  };
  const newSave = h.save(), newOperation = h.context.operationRef.current; await newStarted.promise;
  oldBody.resolve({ cache_refresh_pending: false }); await oldSave;
  assert.equal(h.context.operationRef.current, newOperation); assert.equal(h.context.saving, true); assert.equal(newOperation.controller.signal.aborted, false);
  newPatch.resolve(response({ cache_refresh_pending: false })); await newSave; assert.equal(h.context.saving, false); assert.equal(h.context.operationRef.current, null);
});
test("source wiring keeps numeric policies and API values, locks editable controls and restricts SMTP link to admin", () => {
  assert.equal(ast.parseDiagnostics.length, 0); assert.match(source, /const \{ user, isSuperAdmin \} = useAuth\(\)/);
  assert.match(declaration("canEdit"), /loading \|\| loadingRef\.current \|\| loadError \|\| saving \|\| operationRef\.current \|\| !isSuperAdmin/);
  assert.match(declaration("handleSave"), /operationRef\.current = operation/); assert.match(declaration("handleSave"), /if \(operationRef\.current === operation\)/);
  for (const name of ["loadSettings", "handleSave"]) { assert.match(declaration(name), /requestOwner: /); assert.match(declaration(name), /signal: /); }
  assert.match(source, /isSuperAdmin && user\?\.id && <Link[\s\S]*href="\/admin\/email"/);
  const controls = [];
  function visit(node) {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && (node.tagName.getText(ast) === "input"
      || node.attributes.properties.some((item) => item.name?.getText(ast) === "role" && item.initializer.text === "switch")
      || node.attributes.properties.some((item) => item.name?.getText(ast) === "onClick" && item.initializer.getText(ast) === "{handleSave}"))) controls.push(node);
    ts.forEachChild(node, visit);
  }
  visit(ast); assert.equal(controls.length, 5);
  for (const control of controls) assert.equal(control.attributes.properties.find((item) => item.name?.getText(ast) === "disabled")?.initializer.getText(ast), "{controlsDisabled}");
  for (const id of ["site-name", "max-containers", "container-timeout"]) assert.match(inputCallback(id), /if \(canEdit\(\)\)/);
  assert.match(inputCallback("registration", "onClick"), /if \(canEdit\(\)\)/);
  assert.match(source, /min=\{1\}[\s\S]*max=\{100000\}/); assert.match(source, /min=\{1\}[\s\S]*max=\{1440\}/);
  assert.match(source, /data\.max_containers \?\? 50/); assert.match(source, /data\.container_timeout \?\? 30/); assert.match(source, /data\.registration_open \?\? true/);
});

