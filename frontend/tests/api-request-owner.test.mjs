import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { AuthRequestBlockedError, createAuthRequestCoordinator } from "../src/lib/auth-request-coordinator.mjs";
import { normalizeApiErrorPayload } from "../src/lib/api-error.mjs";

function source(relative) {
  const text = readFileSync(new URL(relative, import.meta.url), "utf8");
  const ast = ts.createSourceFile(relative, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  assert.equal(ast.parseDiagnostics.length, 0);
  return { text, ast };
}
function callback(product, name) {
  const matches = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(product.ast) === name) matches.push(node.initializer);
    ts.forEachChild(node, visit);
  }
  visit(product.ast);
  assert.equal(matches.length, 1, `actual ${name} callback is unique`);
  const value = matches[0];
  return (ts.isCallExpression(value) ? value.arguments[0] : value).getText(product.ast);
}
function execute(text, context) {
  vm.runInNewContext(ts.transpileModule(text, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  } }).outputText, context, { timeout: 1000 });
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function response(value = {}, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => value };
}
const provider = source("../src/providers/AuthProvider.tsx");
const profile = source("../src/app/[locale]/settings/profile/page.tsx");
const client = source("../src/lib/api-client.ts");

function harness() {
  const coordinator = createAuthRequestCoordinator();
  const calls = [], stateWrites = [], renderUpdates = [];
  const owner = {
    requestId: 1, requestIdRef: { current: 1 }, epochChanged: () => false,
    userRef: { current: { id: "A" } }, sessionEpochRef: { current: 1 }, sessionStatusRef: { current: "authenticated" },
    wasLoggedInRef: { current: true }, setUser: (update) => renderUpdates.push(update), setSessionStatus: () => {},
  };
  execute(`globalThis.applyAuthenticated = ${callback(provider, "applyAuthenticated")};
    globalThis.snapshot = ${callback(provider, "requestSessionSnapshot")};`, owner);
  let checks = 0;
  coordinator.register({ snapshot: owner.snapshot, verify: async () => { checks += 1; return owner.sessionStatusRef.current; } });
  const apiContext = {
    exports: {}, process: { env: {} }, URL, FormData, document: { cookie: "ctf_csrf_token=fixture" },
    window: { location: { origin: "http://fixture.test" } },
    require: (name) => {
      if (name === "@/lib/auth-request-coordinator.mjs") return { AuthRequestBlockedError, authRequestCoordinator: coordinator };
      if (name === "@/lib/api-error.mjs") return { normalizeApiErrorPayload };
      throw new Error(`Unexpected product dependency: ${name}`);
    },
    fetch: async (path, options) => {
      calls.push({ path, options, cookieOwner: owner.userRef.current?.id });
      return apiContext.respond(path, options);
    },
    respond: async () => response(),
  };
  execute(client.text, apiContext);
  const api = apiContext.exports;
  const page = {
    ...api, userId: "A", saving: false, hasChanges: true, ownerGeneration: { current: 1 },
    pendingAvatarDelete: false, pendingAvatarFile: new Blob(["fixture"]), pendingAvatarPreview: null,
    username: "A private draft", originalUsername: "A original", bio: "A private bio", originalBio: "A old bio",
    FormData, URL: { revokeObjectURL: () => {} }, t: (key) => key,
  };
  for (const name of ["Saving", "Message", "AvatarUrl", "OriginalAvatarUrl", "PendingAvatarDelete", "PendingAvatarFile", "PendingAvatarPreview", "OriginalUsername", "OriginalBio"]) {
    page[`set${name}`] = (value) => { page[name[0].toLowerCase() + name.slice(1)] = value; stateWrites.push({ name, value }); };
  }
  execute(`globalThis.save = ${callback(profile, "handleSave")};`, page);
  return { api, apiContext, page, calls, stateWrites, renderUpdates, owner, coordinator, checks: () => checks,
    switchToB() { owner.applyAuthenticated({ id: "B" }); assert.equal(owner.sessionEpochRef.current, 2); assert.equal(page.ownerGeneration.current, 1, "React commit has not run"); },
  };
}

for (const outcome of ["success", "failure"]) {
  test(`actual profile upload JSON ${outcome} after pre-commit account change never PATCHes the new account`, async () => {
    const h = harness(), started = deferred(), body = deferred();
    h.apiContext.respond = async () => ({ ...response(), json: () => { started.resolve(); return body.promise; } });
    const saving = h.page.save();
    await started.promise;
    h.switchToB();
    const writesAtSwitch = h.stateWrites.length;
    if (outcome === "success") body.resolve({ avatar_url: "fixture/A.png" }); else body.reject(new Error("late body failure"));
    await saving;
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].options.method, "POST");
    assert.equal(h.calls[0].cookieOwner, "A");
    assert.deepEqual(h.stateWrites.slice(writesAtSwitch), [{ name: "Saving", value: false }], "only the still-mounted operation releases its busy state");
    assert.equal(h.renderUpdates.length, 1, "actual AuthProvider scheduled B without running React commit");
  });
}

test("actual profile DELETE 204 fulfillment followed by pre-commit account change cannot continue to PATCH", async () => {
  const h = harness();
  h.page.pendingAvatarDelete = true; h.page.pendingAvatarFile = null;
  h.apiContext.respond = async () => response(undefined, 204);
  const actualFetch = h.api.apiFetch;
  h.page.apiFetch = async (...args) => { const result = await actualFetch(...args); h.switchToB(); return result; };
  await h.page.save();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.method, "DELETE");
  assert.equal(h.stateWrites.filter((item) => item.name === "OriginalUsername").length, 0);
});

test("stale rendered A cannot start a save when the real provider owner already changed to B", async () => {
  const h = harness(); h.switchToB();
  await h.page.save();
  assert.equal(h.calls.length, 0);
  assert.equal(h.checks(), 0);
  assert.equal(h.stateWrites.at(-1).value.type, "error");
});

for (const method of ["GET", "POST"]) {
  for (const outcome of ["success", "server-error", "invalid-json"]) {
    test(`ordinary ${method} response ${outcome} body is fenced through JSON decode`, async () => {
      const h = harness(), started = deferred(), body = deferred();
      h.apiContext.respond = async () => ({ ...response({}, outcome === "server-error" ? 403 : 200), json: () => { started.resolve(); return body.promise; } });
      const result = h.api.apiFetch("/api/community/inquiries/my", { method });
      await started.promise; h.switchToB();
      if (outcome === "invalid-json") body.reject(new Error("invalid JSON")); else body.resolve({ detail: "A private result" });
      await assert.rejects(result, (error) => error instanceof h.api.ApiError && error.code === "auth_session_changed" && error.status === 409);
      assert.equal(h.calls.length, 1);
    });
  }
}

test("GET me remains a session-check exception but PATCH me verifies the session", async () => {
  const h = harness();
  await h.api.apiFetch("/api/auth/me?check=1");
  assert.equal(h.checks(), 0);
  await h.api.apiFetch("/api/auth/me", { method: "patch", body: { username: "A new name" } });
  assert.equal(h.checks(), 1);
  h.owner.sessionStatusRef.current = "unknown";
  await assert.rejects(h.api.apiFetch("/api/auth/me", { method: "PATCH", body: {} }), (error) => error.code === "auth_status_unavailable");
  assert.equal(h.calls.length, 2);
});

test("one captured owner survives the complete save but is never forwarded to native fetch", async () => {
  for (const deleting of [false, true]) {
    const h = harness(); h.page.pendingAvatarDelete = deleting; h.page.pendingAvatarFile = deleting ? null : h.page.pendingAvatarFile;
    h.apiContext.respond = async (_path, options) => response({ avatar_url: "fixture/A.png" }, options.method === "DELETE" ? 204 : 200);
    await h.page.save();
    assert.deepEqual(h.calls.map((call) => call.options.method), [deleting ? "DELETE" : "POST", "PATCH"]);
    assert.equal(h.checks(), 2);
    assert.equal(h.stateWrites.at(-1).name, "Saving");
    assert.equal(h.stateWrites.at(-1).value, false);
    assert.ok(h.stateWrites.some((item) => item.name === "Message" && item.value?.type === "success"));
    for (const call of h.calls) {
      assert.equal(Object.hasOwn(call.options, "requestOwner"), false);
      assert.equal(Object.hasOwn(call.options, "sessionRecovery"), false);
      assert.equal(call.options.headers["X-CSRF-Token"], "fixture");
    }
  }
});

test("explicit operation owner rejects stale bypass, same-user new epoch, and replaced registration", async () => {
  for (const change of ["owner", "epoch", "registration"]) {
    const h = harness(), requestOwner = h.api.captureApiRequestOwner("A");
    if (change === "owner") h.switchToB();
    else if (change === "epoch") h.owner.sessionEpochRef.current += 1;
    else h.coordinator.register({ snapshot: h.owner.snapshot, verify: async () => "authenticated" });
    await assert.rejects(h.api.apiFetch("/api/auth/me", { method: "PATCH", body: {}, requestOwner, sessionRecovery: "bypass" }), (error) => error.code === "auth_session_changed");
    assert.equal(h.calls.length, 0);
    assert.equal(h.api.isCurrentApiRequestOwner(requestOwner), false);
  }
});

test("unknown session keeps the original owner and draft but sends no save requests until recovery", async () => {
  const h = harness(); h.owner.sessionStatusRef.current = "unknown";
  const requestOwner = h.api.captureApiRequestOwner("A");
  assert.equal(h.api.isCurrentApiRequestOwner(requestOwner), true);
  await h.page.save();
  assert.equal(h.calls.length, 0);
  assert.equal(h.page.username, "A private draft");
  assert.equal(h.page.pendingAvatarFile.size, 7);
  assert.equal(h.stateWrites.at(-1).value, false);
  h.owner.sessionStatusRef.current = "authenticated";
  h.apiContext.respond = async () => response({ avatar_url: "fixture/A.png" });
  await h.page.save();
  assert.equal(h.calls.length, 2);
});

test("same-owner epoch change during an uncertain logout preserves the draft, releases busy, and permits manual retry", async () => {
  const h = harness(), started = deferred(), body = deferred();
  h.apiContext.respond = async () => ({ ...response(), json: () => { started.resolve(); return body.promise; } });
  const saving = h.page.save();
  await started.promise;
  assert.equal(h.page.saving, true);
  h.owner.sessionEpochRef.current += 1;
  h.owner.sessionStatusRef.current = "unknown";
  body.resolve({ avatar_url: "fixture/old-operation.png" });
  await saving;
  assert.equal(h.calls.length, 1, "old operation must not send the profile PATCH");
  assert.equal(h.page.saving, false);
  assert.equal(h.page.username, "A private draft");
  assert.equal(h.page.pendingAvatarFile.size, 7);
  assert.equal(h.page.ownerGeneration.current, 1, "same owner remains mounted");
  h.owner.sessionStatusRef.current = "authenticated";
  h.apiContext.respond = async () => response({ avatar_url: "fixture/retry.png" });
  await h.page.save();
  assert.deepEqual(h.calls.map((call) => call.options.method), ["POST", "POST", "PATCH"]);
  assert.equal(h.page.saving, false);
  assert.equal(h.page.originalUsername, "A private draft");
});
