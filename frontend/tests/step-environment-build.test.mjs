import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const sourcePath = new URL("../src/components/admin/challenges/StepEnvironment.tsx", import.meta.url);
const source = readFileSync(sourcePath, "utf8");
const syntax = ts.createSourceFile(sourcePath.pathname, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(syntax.parseDiagnostics.length, 0);

function findNode(predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(syntax);
  assert.equal(matches.length, 1, "the production callback must be uniquely identifiable");
  return matches[0];
}

function productionCallback(name) {
  const node = findNode((candidate) => ts.isVariableDeclaration(candidate) && candidate.name.getText(syntax) === name);
  assert.ok(ts.isCallExpression(node.initializer) && node.initializer.expression.getText(syntax) === "useCallback");
  const callback = node.initializer.arguments[0];
  assert.ok(ts.isArrowFunction(callback));
  return `const ${name} = ${callback.getText(syntax)};`;
}

function productionEffect(marker) {
  const node = findNode((candidate) => (
    ts.isCallExpression(candidate)
    && candidate.expression.getText(syntax) === "useEffect"
    && candidate.arguments[0].getText(syntax).includes(marker)
  ));
  return node.arguments[0].getText(syntax);
}

const callbackNames = [
  "beginEnvironmentOperation", "isCurrentTarget", "isCurrentEnvironmentResult", "endEnvironmentOperation",
  "applyEnvironmentDetails", "refreshEnvironmentDetails", "syncEnvSettings", "handleVerifyEnvironment", "handleBuild",
];
const signature = findNode((node) => ts.isFunctionDeclaration(node) && node.name?.text === "environmentConfigSignature");
// Execute the actual TS AST callback bodies. Only state, clocks, API transport, and notifications are mocked.
const program = ts.transpileModule([
  signature.getText(syntax),
  ...callbackNames.map(productionCallback),
  `const autoBuildEffect = ${productionEffect("void handleBuild()")};`,
  `const resetTargetEffect = ${productionEffect("previousChallengeIdRef.current === challengeId")};`,
  `const configChangeEffect = ${productionEffect("const signature = environmentConfigSignature(data)")};`,
  `const mountEffect = ${productionEffect("mountedRef.current = true")};`,
  `globalThis.callbacks = { ${callbackNames.join(", ")}, autoBuildEffect, resetTargetEffect, configChangeEffect, mountEffect };`,
].join("\n"), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

function harness() {
  const timers = new Map();
  const apiCalls = [];
  const notifications = [];
  const changes = [];
  const writes = [];
  const servers = new Map();
  let nextTimer = 0;
  class ApiError extends Error {
    constructor(message, code, log) { super(message); this.code = code; this.log = log; }
  }
  const context = {
    challengeId: 42,
    data: { envType: "dockerfile", dockerfile: "FROM fixture\nEXPOSE 8080", imageName: "", containerPort: 8080, flagType: "static", flagValue: "fixture", flags: [], dynamicFlag: null },
    environmentRequestGenerationRef: { current: 0 }, environmentOperationSequenceRef: { current: 0 },
    activeEnvironmentOperationRef: { current: null }, buildTimerRef: { current: null },
    environmentConfigSignatureRef: { current: "" }, hasAutoBuildAttemptedRef: { current: false },
    mountedRef: { current: true }, challengeIdRef: { current: 42 }, previousChallengeIdRef: { current: 42 },
    buildFilesRequestRef: { current: 0 }, buildFiles: [{ name: "Dockerfile", size: 40 }],
    filesLoaded: true, buildFilesLoading: false, buildFilesError: null, uploading: false, deletingFile: null,
    building: false, verifyingEnvironment: false, buildLog: null, buildElapsed: 0, environmentVerificationState: "idle",
    ApiError,
    Date: { now: () => context.now }, now: 10000,
    t: (key) => key,
    toast: (...args) => notifications.push(args),
    onChange: (partial) => { changes.push(JSON.parse(JSON.stringify(partial))); Object.assign(context.data, partial); },
    setInterval: (callback) => { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearInterval: (id) => timers.delete(id),
  };
  for (const name of ["Building", "BuildLog", "DynamicFlagRequired", "BuildElapsed", "VerifyingEnvironment", "EnvironmentVerificationState", "BuildFiles", "BuildFilesError", "BuildFilesLoading", "Uploading", "DeletingFile", "Dragging", "FilesLoaded"]) {
    const property = name[0].toLowerCase() + name.slice(1);
    context[`set${name}`] = (value) => {
      context[property] = typeof value === "function" ? value(context[property]) : value;
      writes.push([property, context[property]]);
    };
  }
  function server(target) {
    if (!servers.has(target)) servers.set(target, {
      env_type: "dockerfile", dockerfile_content: "FROM fixture\nEXPOSE 8080", docker_image: "", container_port: 8080,
      environment_revision: 3, environment_verified_revision: null, environment_ready: false,
    });
    return servers.get(target);
  }
  function ready(target = 42, revision = 3) {
    Object.assign(server(target), { docker_image: `fixture-image-${target}`, environment_revision: revision, environment_verified_revision: revision, environment_ready: true });
    return { status: "ready", checked_at: "2026-10-13T00:00:00Z", revision };
  }
  function buildSuccess(target = 42, revision = 3) {
    return { image_name: `fixture-image-${target}`, log: `build log ${target}`, verification: ready(target, revision) };
  }
  context.apiFetch = async (path, options = {}) => {
    apiCalls.push({ path, method: options.method ?? "GET", body: options.body });
    const target = Number(path.match(/challenges\/(\d+)/)?.[1]);
    if (options.method === "PATCH") {
      if (context.syncFailure) throw new ApiError("sync failed");
      return {};
    }
    if (path.endsWith("/build")) return context.buildBehavior ? context.buildBehavior(target) : buildSuccess(target);
    if (path.endsWith("/verify-environment")) return context.verifyBehavior ? context.verifyBehavior(target) : ready(target);
    return { ...server(target) };
  };
  vm.runInNewContext(program, context, { filename: "StepEnvironment.actual-callbacks.js", timeout: 1000 });
  const cleanup = context.callbacks.mountEffect();
  return {
    context, callbacks: context.callbacks, timers, apiCalls, notifications, changes, writes, ApiError, server, ready, buildSuccess, unmount: cleanup,
    buildCalls: () => apiCalls.filter((call) => call.path.endsWith("/build")),
    verifyCalls: () => apiCalls.filter((call) => call.path.endsWith("/verify-environment")),
  };
}

function assertReleased(h) {
  assert.equal(h.context.building, false);
  assert.equal(h.context.activeEnvironmentOperationRef.current, null);
  assert.equal(h.context.buildTimerRef.current, null);
  assert.equal(h.timers.size, 0);
}

test("a failed automatic build runs once and a manual retry can succeed", async () => {
  const h = harness();
  h.context.buildBehavior = async () => { throw new Error("injected build failure"); };
  h.callbacks.autoBuildEffect();
  await tick();
  assert.equal(h.buildCalls().length, 1);
  assert.ok(h.notifications.some((item) => item[0] === "error" && item[1] === "buildFailed"));
  assertReleased(h);
  h.callbacks.autoBuildEffect();
  await tick();
  assert.equal(h.buildCalls().length, 1, "a render after failure must not loop the automatic request");
  h.context.buildBehavior = (target) => h.buildSuccess(target);
  assert.equal(await h.callbacks.handleBuild(), true);
  assert.equal(h.buildCalls().length, 2);
  assert.ok(h.notifications.some((item) => item[0] === "success" && item[1] === "buildSuccess"));
  assertReleased(h);
});

test("failed settings synchronization sends no build and releases the operation for retry", async () => {
  const h = harness();
  h.context.syncFailure = true;
  assert.equal(await h.callbacks.handleBuild(), false);
  assert.equal(h.buildCalls().length, 0);
  assert.equal(h.context.environmentVerificationState, "failed");
  assertReleased(h);
  h.context.syncFailure = false;
  assert.equal(await h.callbacks.handleBuild(), true);
  assertReleased(h);
});

test("an image build without matching canonical verification is never successful", async () => {
  const h = harness();
  h.context.buildBehavior = (target) => ({ image_name: "unverified", log: "built", verification: h.ready(target, 4) });
  const original = h.context.apiFetch;
  h.context.apiFetch = async (path, options) => {
    const result = await original(path, options);
    return path.endsWith("/42") && !options?.method && h.buildCalls().length > 0
      ? { ...result, environment_verified_revision: 3 }
      : result;
  };
  assert.equal(await h.callbacks.handleBuild(), false);
  assert.equal(h.context.environmentVerificationState, "failed");
  assert.equal(h.notifications.some((item) => item[0] === "success"), false);
  assertReleased(h);
});

test("the operation gate blocks build/build and build/verify before React rerenders", async () => {
  const h = harness();
  const build = deferred();
  h.context.buildBehavior = () => build.promise;
  const first = h.callbacks.handleBuild();
  await tick();
  assert.equal(await h.callbacks.handleBuild(), false);
  await h.callbacks.handleVerifyEnvironment();
  assert.equal(h.buildCalls().length, 1);
  assert.equal(h.verifyCalls().length, 0);
  assert.equal(h.timers.size, 1);
  h.context.now += 2500;
  [...h.timers.values()][0]();
  assert.equal(h.context.buildElapsed, 2);
  build.resolve(h.buildSuccess());
  assert.equal(await first, true);
  assertReleased(h);
});

test("verification blocks a concurrent build and releases its own busy state", async () => {
  const h = harness();
  const verification = deferred();
  h.context.verifyBehavior = () => verification.promise;
  const active = h.callbacks.handleVerifyEnvironment();
  await tick();
  assert.equal(h.context.verifyingEnvironment, true);
  assert.equal(await h.callbacks.handleBuild(), false);
  assert.equal(h.buildCalls().length, 0);
  verification.resolve(h.ready());
  await active;
  assert.equal(h.context.verifyingEnvironment, false);
  assert.equal(h.context.activeEnvironmentOperationRef.current, null);
});

test("structured build errors preserve the safe log and permit a manual retry", async () => {
  const h = harness();
  h.context.buildBehavior = () => { throw new h.ApiError("injected", "dynamic_flag_required", "safe build log"); };
  assert.equal(await h.callbacks.handleBuild(), false);
  assert.equal(h.context.buildLog, "safe build log");
  assert.equal(h.context.dynamicFlagRequired, true);
  assertReleased(h);
  h.context.buildBehavior = (target) => h.buildSuccess(target);
  assert.equal(await h.callbacks.handleBuild(), true);
  assert.equal(h.context.dynamicFlagRequired, false);
  assertReleased(h);
});

test("late build results after a configuration change cannot update the current draft", async () => {
  for (const fail of [false, true]) {
    const h = harness();
    const build = deferred();
    h.context.buildBehavior = () => build.promise;
    const active = h.callbacks.handleBuild();
    await tick();
    h.context.data.dockerfile = "FROM changed-fixture";
    h.callbacks.configChangeEffect();
    const changeCount = h.changes.length;
    const toastCount = h.notifications.length;
    if (fail) build.reject(new Error("late failure"));
    else build.resolve(h.buildSuccess());
    assert.equal(await active, false);
    assert.equal(h.changes.length, changeCount);
    assert.equal(h.notifications.length, toastCount);
    assert.equal(h.context.buildLog, null);
    assertReleased(h);
  }
});

test("an old challenge result cannot release a newer build or clear its timer", async () => {
  const h = harness();
  const oldBuild = deferred();
  const newBuild = deferred();
  h.context.buildBehavior = (target) => target === 42 ? oldBuild.promise : newBuild.promise;
  const old = h.callbacks.handleBuild();
  await tick();
  h.context.challengeId = 43;
  h.context.challengeIdRef.current = 43;
  h.callbacks.resetTargetEffect();
  const current = h.callbacks.handleBuild();
  await tick();
  const currentTimer = h.context.buildTimerRef.current;
  const currentOperation = h.context.activeEnvironmentOperationRef.current.id;
  oldBuild.resolve(h.buildSuccess(42));
  assert.equal(await old, false);
  assert.equal(h.context.building, true);
  assert.equal(h.context.activeEnvironmentOperationRef.current.id, currentOperation);
  assert.equal(h.context.buildTimerRef.current, currentTimer);
  assert.equal(h.timers.has(currentTimer), true);
  assert.equal(h.context.buildLog, null);
  newBuild.resolve(h.buildSuccess(43));
  assert.equal(await current, true);
  assert.equal(h.context.buildLog, "build log 43");
  assertReleased(h);
});

test("unmount clears the timer and ignores the late build response", async () => {
  const h = harness();
  const build = deferred();
  h.context.buildBehavior = () => build.promise;
  const active = h.callbacks.handleBuild();
  await tick();
  h.unmount();
  const changeCount = h.changes.length;
  const writeCount = h.writes.length;
  build.resolve(h.buildSuccess());
  assert.equal(await active, false);
  assert.equal(h.changes.length, changeCount);
  assert.equal(h.writes.length, writeCount);
  assert.equal(h.notifications.length, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.context.buildTimerRef.current, null);
});
