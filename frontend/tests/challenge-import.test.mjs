import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";
import vm from "node:vm";
import { importChallengeFile, InvalidChallengeImportFile, parseChallengeImport, parseChallengeImportResult } from "../src/lib/challenge-import.mjs";

const require = createRequire(import.meta.url), ts = require("typescript");
const frontend = path.dirname(path.dirname(path.dirname(require.resolve("typescript/package.json"))));
const { createAuthRequestCoordinator, AuthRequestBlockedError } = await import(pathToFileURL(path.join(frontend, "src/lib/auth-request-coordinator.mjs")));
const { normalizeApiErrorPayload } = await import(pathToFileURL(path.join(frontend, "src/lib/api-error.mjs")));
const clientSource = readFileSync(path.join(frontend, "src/lib/api-client.ts"), "utf8");
const pageSource = readFileSync(new URL("../src/app/[locale]/admin/data/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("page.tsx", pageSource, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
const jsonFile = (value) => ({ text: async () => JSON.stringify(value) });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function declaration(name) {
  let found;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(ast) === name) found = node;
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert(found, name);
  return found.initializer.getText(ast);
}
class TestApiError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}
function effectCallback() {
  const matches = [];
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(ast) === "useEffect") matches.push(node.arguments[0]);
    ts.forEachChild(node, visit);
  }
  visit(ast); assert.equal(matches.length, 1); return matches[0].getText(ast);
}
function handlers(apiFetch, options = {}) {
  const state = { importing: false, exporting: false, lastResult: null, importNotice: null, toasts: [], downloads: [], revoked: [], writes: [] };
  const operationRef = { current: null }, mountedRef = { current: false }, requestGenerationRef = { current: 0 };
  const session = options.session ?? { status: "authenticated", epoch: 1, authenticated: true, ownerId: "synthetic-A" };
  const coordinator = options.coordinator ?? createAuthRequestCoordinator();
  if (!options.coordinator) coordinator.register({ snapshot: () => ({ ...session }), verify: async () => session.status });
  const set = (key, value) => { state[key] = value; state.writes.push({ key, value }); };
  const context = {
    operationRef, mountedRef, requestGenerationRef, user: { id: session.ownerId }, isSuperAdmin: true, AbortController, importMode: "overwrite", importChallengeFile, InvalidChallengeImportFile, ApiError: options.ApiError ?? TestApiError, apiFetch,
    captureApiRequestOwner: (id) => coordinator.captureOwner(id), isCurrentApiRequestOwner: (owner) => coordinator.isCurrentOwner(owner),
    setImporting: (value) => set("importing", value), setExporting: (value) => set("exporting", value),
    setLastResult: (value) => set("lastResult", value), setImportNotice: (value) => set("importNotice", value),
    t: (key, values) => values ? `${key}:${JSON.stringify(values)}` : key, tc: (key) => key,
    toast: (kind, message) => { state.toasts.push({ kind, message }); }, Blob, Date,
    URL: { createObjectURL: () => "blob:owned-test", revokeObjectURL: (url) => state.revoked.push(url) },
    document: { createElement: () => { const anchor = { click: () => { state.downloads.push({ href: anchor.href, filename: anchor.download }); } }; return anchor; } },
  };
  const evaluate = (name) => vm.runInNewContext(ts.transpile(`(${declaration(name)})`, { target: ts.ScriptTarget.ES2022 }), context);
  for (const name of ["beginOperation", "isCurrentOperation", "finishOperation"]) context[name] = evaluate(name);
  let cleanup;
  const mount = () => { cleanup = vm.runInNewContext(ts.transpile(`(${effectCallback()})`, { target: ts.ScriptTarget.ES2022 }), context)(); };
  mount();
  return { state, operationRef, mountedRef, requestGenerationRef, session, coordinator, context, importFile: evaluate("handleImport"), exportFile: evaluate("handleExport"),
    unmount() { cleanup(); },
    switchSession(ownerId = "synthetic-B") { session.ownerId = ownerId; session.epoch++; },
    commitSession() { cleanup(); context.user = { id: session.ownerId }; mount(); },
  };
}
function actualOwnerHandlers() {
  const coordinator = createAuthRequestCoordinator(), session = { status: "authenticated", epoch: 1, authenticated: true, ownerId: "synthetic-A" }, executions = [];
  coordinator.register({ snapshot: () => ({ ...session }), verify: async () => session.status });
  const apiContext = { exports: {}, process: { env: {} }, URL, FormData, document: { cookie: "ctf_csrf_token=synthetic" }, window: { location: { origin: "http://pure.test" } },
    require: (name) => {
      if (name === "@/lib/auth-request-coordinator.mjs") return { AuthRequestBlockedError, authRequestCoordinator: coordinator };
      if (name === "@/lib/api-error.mjs") return { normalizeApiErrorPayload };
      throw Error(`Unexpected dependency: ${name}`);
    },
    fetch: async (url, options) => { executions.push({ url, options, ownerId: session.ownerId }); return apiContext.respond(url, options); },
    respond: async () => ({ ok: true, status: 200, json: async () => ({ created: 0, updated: 0, skipped: 0 }) }),
  };
  vm.runInNewContext(ts.transpileModule(clientSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, apiContext, { timeout: 1000 });
  return { ...handlers(apiContext.exports.apiFetch, { coordinator, session, ApiError: apiContext.exports.ApiError }), executions, apiContext };
}

test("legacy arrays and explicit v1/v2 envelopes preserve version, names and all server-owned record fields", () => {
  const record = { name: " keep name ", id: 12, flags: [{ id: 6, value: "synthetic-answer" }], hints: { unknownServerShape: true }, prerequisite_ids: [4], extra: { nested: [null, 3] } };
  for (const version of [1, 2]) {
    const result = parseChallengeImport(JSON.stringify({ version, challenges: [record] }), "overwrite");
    assert.equal(result.version, version); assert.equal(result.mode, "overwrite"); assert.deepEqual(result.challenges, [record]);
  }
  assert.deepEqual(parseChallengeImport(JSON.stringify([record]), "skip"), { challenges: [record], mode: "skip" });
  for (const value of [[], { version: 1, challenges: [] }, { version: 2, challenges: [] }]) assert.equal(parseChallengeImport(JSON.stringify(value), "skip").challenges.length, 0);
});
test("legacy version-omitted envelopes preserve omission while explicit unsupported versions remain POST0", async () => {
  const value = { challenges: [{ name: "legacy one", hints: { serverOwnedShape: true }, prerequisite_ids: [5] }] }, calls = [];
  const result = await importChallengeFile(jsonFile(value), "overwrite", async (payload) => { calls.push(payload); return { created: 1, skipped: 0 }; });
  assert.deepEqual(calls, [{ ...value, mode: "overwrite" }]); assert.equal(Object.hasOwn(calls[0], "version"), false);
  assert.deepEqual(result, { created: 1, updated: 0, skipped: 0 });
  assert.deepEqual(parseChallengeImport('{"challenges":[]}', "skip"), { challenges: [], mode: "skip" });
  for (const version of [null, true, false, "1", "2", 0, 3, {}, []]) {
    await assert.rejects(importChallengeFile(jsonFile({ version, challenges: [] }), "skip", async (payload) => { calls.push(payload); }), InvalidChallengeImportFile);
  }
  assert.equal(calls.length, 1);
});
test("malformed/unsupported/nonarray/nonrecord/blank-name input is explicit invalid file before submit", async () => {
  const invalid = ["{", "null", "0", '"text"', "true", "{}", '{"version":3,"challenges":[]}', '{"version":"2","challenges":[]}',
    '{"version":1,"challenges":null}', '{"version":2,"challenges":{}}', '[null]', '[[]]', '[1]', '["name"]', '[{}]',
    '[{"name":""}]', '[{"name":" \\t\\n"}]', '[{"name":7}]', '[{"name":"valid"},{"name":""}]'];
  let submits = 0;
  for (const text of invalid) await assert.rejects(importChallengeFile({ text: async () => text }, "skip", async () => { submits++; }), InvalidChallengeImportFile);
  await assert.rejects(importChallengeFile({ text: async () => { throw Error("read failure"); } }, "skip", async () => { submits++; }), InvalidChallengeImportFile);
  assert.throws(() => parseChallengeImport("[]", "unknown"), InvalidChallengeImportFile);
  assert.equal(submits, 0);
});
test("result counts are nonnegative safe integers and only omitted updated defaults to zero", () => {
  assert.deepEqual(parseChallengeImportResult({ created: 1, skipped: 2 }), { created: 1, updated: 0, skipped: 2 });
  assert.deepEqual(parseChallengeImportResult({ created: 0, updated: Number.MAX_SAFE_INTEGER, skipped: 0 }), { created: 0, updated: Number.MAX_SAFE_INTEGER, skipped: 0 });
  for (const field of ["created", "updated", "skipped"]) for (const count of [-1, 1.5, "1", null, undefined, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => parseChallengeImportResult({ created: 0, updated: 0, skipped: 0, [field]: count }));
  }
  for (const value of [null, [], {}, { created: 0 }, { skipped: 0 }]) assert.throws(() => parseChallengeImportResult(value));
});
test("real file callback submits preserved payload exactly once and validates the returned counts", async () => {
  const calls = [], value = { version: 2, challenges: [{ name: "one", id: 17, prerequisite_ids: [19], flags: "server-validates-this" }] };
  const result = await importChallengeFile(jsonFile(value), "overwrite", async (payload) => { calls.push(payload); return { created: 1, updated: 2, skipped: 3 }; });
  assert.deepEqual(calls, [{ ...value, mode: "overwrite" }]); assert.deepEqual(result, { created: 1, updated: 2, skipped: 3 });
});
test("POST network/result uncertainty is never retried or recast as an invalid file", async () => {
  for (const submit of [async () => { throw TypeError("network lost"); }, async () => ({ created: "1", skipped: 0 }), async () => ({ created: 1, updated: null, skipped: 0 })]) {
    let attempts = 0;
    await assert.rejects(importChallengeFile(jsonFile([]), "skip", async (payload) => { attempts++; return submit(payload); }), (error) => !(error instanceof InvalidChallengeImportFile));
    assert.equal(attempts, 1);
  }
  const serverError = new TestApiError("server guidance", "import_outcome_unknown");
  await assert.rejects(importChallengeFile(jsonFile([]), "skip", async () => { throw serverError; }), (error) => error === serverError);
});
test("actual page callback localizes invalid files and makes POST0 for every local failure", async () => {
  let posts = 0;
  const h = handlers(async () => { posts++; return {}; });
  for (const file of [{ text: async () => "bad" }, jsonFile({ version: 2, challenges: {} }), { text: async () => { throw Error("read"); } }]) {
    await h.importFile(file); assert.equal(h.state.toasts.at(-1).message, "importInvalidFile"); assert.equal(h.state.toasts.at(-1).kind, "error");
    assert.equal(h.state.lastResult, null); assert.equal(h.state.importNotice, null); assert.equal(h.operationRef.current, null);
  }
  assert.equal(posts, 0);
});
test("actual callback shows created/updated/skipped and old server omission only displays updated0", async () => {
  for (const response of [{ created: 1, updated: 2, skipped: 3 }, { created: 1, skipped: 3 }]) {
    const requests = [], h = handlers(async (url, options) => { requests.push({ url, options }); return response; });
    await h.importFile(jsonFile({ version: 2, challenges: [{ name: "one" }] }));
    assert.equal(requests.length, 1); assert.equal(requests[0].url, "/api/learn/challenges/import/v2"); assert.equal(requests[0].options.method, "POST");
    assert.deepEqual(requests[0].options.body, { version: 2, challenges: [{ name: "one" }], mode: "overwrite" });
    assert.deepEqual(h.state.lastResult, { created: 1, updated: response.updated ?? 0, skipped: 3 });
    assert.equal(h.state.toasts[0].kind, "success"); assert.match(h.state.toasts[0].message, /"updated":/); assert.equal(h.state.importing, false);
  }
});
test("old server404 or405 never falls back to its destructive legacy import endpoint", async () => {
  for (const status of [404, 405]) {
    const h = actualOwnerHandlers();
    h.apiContext.respond = async () => ({ ok: false, status, json: async () => ({ detail: "preserving import unavailable" }) });
    await h.importFile(jsonFile([]));
    assert.equal(h.executions.length, 1);
    assert.equal(new URL(h.executions[0].url, "http://pure.test").pathname.endsWith("/api/learn/challenges/import/v2"), true);
    assert.equal(h.state.lastResult, null);
    assert.deepEqual(h.state.toasts, [{ kind: "error", message: "preserving import unavailable" }]);
    assert.equal(h.operationRef.current, null);
  }
});
test("actual callback keeps uncertain POST outcome separate, with no success/failure claim or automatic replay", async () => {
  for (const failure of [TypeError("network"), SyntaxError("response JSON")]) {
    let posts = 0; const h = handlers(async () => { posts++; throw failure; }); await h.importFile(jsonFile([]));
    assert.equal(posts, 1); assert.equal(h.state.lastResult, null); assert.equal(h.state.importNotice, "importOutcomeUnknown");
    assert.deepEqual(h.state.toasts, [{ kind: "warning", message: "importOutcomeUnknown" }]); assert.equal(h.operationRef.current, null);
  }
  const h = handlers(async () => ({ created: 0, updated: -1, skipped: 0 })); await h.importFile(jsonFile([]));
  assert.equal(h.state.importNotice, "importOutcomeUnknown"); assert.equal(h.state.toasts[0].kind, "warning");
});
test("actual ApiError message is preserved, including server outcome-unknown guidance", async () => {
  for (const code of ["validation_error", "import_outcome_unknown"]) {
    const h = handlers(async () => { throw new TestApiError("current server guidance", code); }); await h.importFile(jsonFile([]));
    assert.equal(h.state.toasts[0].message, "current server guidance"); assert.equal(h.state.toasts[0].kind, code === "import_outcome_unknown" ? "warning" : "error");
    assert.equal(h.state.importNotice, code === "import_outcome_unknown" ? "current server guidance" : null);
  }
});
test("same-tick shared lease blocks duplicate import/export through file read and POST completion", async () => {
  const reading = deferred(), submitted = deferred(), response = deferred(); let reads = 0, posts = 0;
  const h = handlers(async () => { posts++; submitted.resolve(); return response.promise; });
  const first = h.importFile({ text: () => { reads++; return reading.promise; } });
  await h.importFile({ text: async () => { reads++; return "[]"; } }); await h.exportFile();
  assert.equal(reads, 1); assert.equal(posts, 0); assert.equal(h.operationRef.current.kind, "import");
  reading.resolve("[]"); await submitted.promise; await h.importFile(jsonFile([])); await h.exportFile(); assert.equal(posts, 1);
  response.resolve({ created: 0, updated: 0, skipped: 0 }); await first; assert.equal(h.operationRef.current, null);
});
test("export retains JSON download flow while its synchronous lease blocks all other operations", async () => {
  const response = deferred(); let reads = 0;
  const h = handlers(async (url) => { reads++; assert.equal(url, "/api/learn/challenges/export"); return response.promise; });
  const first = h.exportFile(); await h.exportFile(); await h.importFile(jsonFile([])); assert.equal(reads, 1);
  response.resolve({ version: 2, challenges: [] }); await first;
  assert.equal(h.state.downloads.length, 1); assert.equal(h.state.downloads[0].href, "blob:owned-test"); assert.match(h.state.downloads[0].filename, /^challenges-export-\d{4}-\d{2}-\d{2}\.json$/);
  assert.deepEqual(h.state.revoked, ["blob:owned-test"]); assert.equal(h.state.toasts[0].message, "exportSuccess"); assert.equal(h.operationRef.current, null);
});
test("actual callback plus real coordinator sends POST0 when account or epoch changes during file read", async () => {
  for (const change of ["account", "same_user_epoch", "registration"]) {
    const h = actualOwnerHandlers(), reading = deferred();
    const running = h.importFile({ text: () => reading.promise }), operation = h.operationRef.current;
    assert.equal(operation.owner.ownerId, "synthetic-A"); assert.equal(operation.controller.signal.aborted, false);
    if (change === "registration") h.coordinator.register({ snapshot: () => ({ ...h.session }), verify: async () => h.session.status });
    else h.switchSession(change === "account" ? "synthetic-B" : "synthetic-A");
    assert.equal(h.requestGenerationRef.current, 0, "React effect cleanup has not run");
    reading.resolve("[]"); await running;
    assert.equal(h.executions.length, 0, change); assert.equal(operation.controller.signal.aborted, true);
    assert.equal(h.state.toasts.length, 0); assert.equal(h.state.downloads.length, 0); assert.equal(h.state.lastResult, null); assert.equal(h.state.importNotice, null);
  }
});
test("unmount during file read sends POST0 and late success/parse/read errors make UI0", async () => {
  for (const outcome of ["valid", "malformed", "read_error"]) {
    const h = actualOwnerHandlers(), reading = deferred(), running = h.importFile({ text: () => reading.promise }), operation = h.operationRef.current;
    h.unmount(); const writes = h.state.writes.length;
    if (outcome === "read_error") reading.reject(Error("late read")); else reading.resolve(outcome === "valid" ? "[]" : "bad");
    await running; assert.equal(h.executions.length, 0); assert.equal(operation.controller.signal.aborted, true); assert.equal(h.state.writes.length, writes);
    assert.equal(h.state.toasts.length, 0); assert.equal(h.state.downloads.length, 0);
  }
});
test("late export response under changed owner/epoch or unmounted page never downloads or toasts", async () => {
  for (const change of ["account", "same_user_epoch", "unmount"]) {
    const h = actualOwnerHandlers(), bodyStarted = deferred(), body = deferred();
    h.apiContext.respond = async () => ({ ok: true, status: 200, json: () => { bodyStarted.resolve(); return body.promise; } });
    const running = h.exportFile(), operation = h.operationRef.current; await bodyStarted.promise;
    assert.equal(h.executions.length, 1); assert.equal(h.executions[0].options.method, "GET");
    if (change === "unmount") h.unmount(); else h.switchSession(change === "account" ? "synthetic-B" : "synthetic-A");
    const writes = h.state.writes.length; body.resolve({ version: 2, challenges: [{ name: "synthetic A export" }] }); await running;
    assert.equal(h.executions.length, 1); assert.equal(h.state.downloads.length, 0); assert.equal(h.state.toasts.length, 0); assert.equal(operation.controller.signal.aborted, true);
    assert.equal(h.state.writes.slice(writes).some((write) => ["lastResult", "importNotice"].includes(write.key)), false);
    if (change === "unmount") assert.equal(h.state.writes.length, writes);
  }
});
test("old import finally cannot release or clear the new account operation lease", async () => {
  const h = actualOwnerHandlers(), oldReading = deferred(), newReading = deferred();
  const oldRun = h.importFile({ text: () => oldReading.promise }), oldOperation = h.operationRef.current;
  h.switchSession(); h.commitSession(); assert.equal(oldOperation.controller.signal.aborted, true);
  const newRun = h.importFile({ text: () => newReading.promise }), newOperation = h.operationRef.current, writes = h.state.writes.length;
  assert.notEqual(newOperation, oldOperation); assert.equal(newOperation.owner.ownerId, "synthetic-B");
  oldReading.resolve("[]"); await oldRun;
  assert.equal(h.operationRef.current, newOperation); assert.equal(newOperation.controller.signal.aborted, false); assert.equal(h.state.importing, true);
  assert.equal(h.state.writes.length, writes); assert.equal(h.executions.length, 0); assert.equal(h.state.toasts.length, 0);
  newReading.resolve("[]"); await newRun; assert.equal(h.executions.length, 1); assert.equal(h.executions[0].ownerId, "synthetic-B");
  assert.equal(h.executions[0].options.signal, newOperation.controller.signal); assert.equal(Object.hasOwn(h.executions[0].options, "requestOwner"), false);
  assert.equal(h.operationRef.current, null); assert.equal(h.state.importing, false);
});
test("stale rendered owner cannot even read a new file, and changed epoch during real session verification executes POST0", async () => {
  const stale = actualOwnerHandlers(); stale.switchSession(); let reads = 0; const writes = stale.state.writes.length;
  await stale.importFile({ text: async () => { reads++; return "[]"; } });
  assert.equal(reads, 0); assert.equal(stale.executions.length, 0); assert.equal(stale.state.writes.length, writes);
  const h = actualOwnerHandlers(), started = deferred(), verified = deferred();
  h.coordinator.register({ snapshot: () => ({ ...h.session }), verify: () => { started.resolve(); return verified.promise; } });
  const running = h.importFile(jsonFile([])); await started.promise; h.switchSession(); verified.resolve("authenticated"); await running;
  assert.equal(h.executions.length, 0); assert.equal(h.state.toasts.length, 0); assert.equal(h.state.lastResult, null);
});
test("late import response is discarded after account change without a successor POST or late result", async () => {
  const h = actualOwnerHandlers(), started = deferred(), body = deferred();
  h.apiContext.respond = async () => ({ ok: true, status: 200, json: () => { started.resolve(); return body.promise; } });
  const running = h.importFile(jsonFile([])); await started.promise; h.switchSession(); body.resolve({ created: 1, updated: 2, skipped: 3 }); await running;
  assert.equal(h.executions.length, 1); assert.equal(h.executions[0].ownerId, "synthetic-A"); assert.equal(h.state.toasts.length, 0); assert.equal(h.state.lastResult, null); assert.equal(h.state.importNotice, null);
});
test("moderator and null user cannot read files, execute APIs or download from actual callbacks", async () => {
  for (const identity of ["moderator", "null_user", "null_user_inconsistent_admin_flag"]) {
    const h = actualOwnerHandlers(); let fileReads = 0;
    h.context.user = identity === "moderator" ? { id: h.session.ownerId, role: "moderator" } : null;
    h.context.isSuperAdmin = identity === "null_user_inconsistent_admin_flag";
    const writes = h.state.writes.length;
    await h.importFile({ text: async () => { fileReads++; return "[]"; } }); await h.exportFile();
    assert.equal(fileReads, 0, identity); assert.equal(h.executions.length, 0, identity); assert.equal(h.state.downloads.length, 0, identity);
    assert.equal(h.state.toasts.length, 0); assert.equal(h.state.writes.length, writes); assert.equal(h.operationRef.current, null);
  }
});
test("page wiring locks every radio/file/button and uses the validated callback rather than empty-array coercion", () => {
  assert.equal(ast.parseDiagnostics.length, 0); assert.match(pageSource, /importChallengeFile, InvalidChallengeImportFile, type ChallengeImportResult/);
  assert.match(pageSource, /const busy = importing \|\| exporting/); assert.equal(pageSource.includes("Array.isArray(challenges) ? challenges : []"), false);
  for (const name of ["handleImport", "handleExport"]) {
    const code = declaration(name); assert(code.indexOf("beginOperation(") < code.indexOf("await"));
    assert.match(code, /if \(!operation\) return/); assert.match(code, /finally[\s\S]*finishOperation\(operation\)/);
    assert.match(code, /requestOwner: operation\.owner/); assert.match(code, /signal: operation\.controller\.signal/);
  }
  assert.match(declaration("beginOperation"), /captureApiRequestOwner\(user\.id\)/);
  assert.match(declaration("beginOperation"), /!isSuperAdmin/);
  assert.match(declaration("isCurrentOperation"), /isCurrentApiRequestOwner\(operation\.owner\)/);
  assert.match(effectCallback(), /operationRef\.current\?\.controller\.abort\(\)/);
  assert.match(pageSource, /const \{ user, isSuperAdmin \} = useAuth\(\)/);
  const controls = [];
  function visit(node) {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && ["Button", "input"].includes(node.tagName.getText(ast))) controls.push(node);
    ts.forEachChild(node, visit);
  }
  visit(ast); assert.equal(controls.length, 5);
  for (const control of controls) assert.equal(control.attributes.properties.find((item) => item.name?.getText(ast) === "disabled")?.initializer?.getText(ast), "{busy || !isSuperAdmin}");
  assert.match(pageSource, /onClick=\{\(\) => \{ if \(!operationRef\.current\) fileInputRef\.current\?\.click\(\); \}\}/);
  assert.match(pageSource, /updated: lastResult\.updated/); assert.match(pageSource, /importNotice && <p role="status"/);
  const declarationSource = readFileSync(new URL("../src/lib/challenge-import.d.mts", import.meta.url), "utf8");
  assert.equal(ts.createSourceFile("challenge-import.d.mts", declarationSource, ts.ScriptTarget.ES2022, true).parseDiagnostics.length, 0);
});
test("four locale notices and both result strings contain all three validated counts", () => {
  const notices = [];
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).admin;
    for (const key of ["importSuccess", "importResult"]) for (const count of ["created", "updated", "skipped"]) assert(messages[key].includes(`{${count}}`), `${locale}:${key}:${count}`);
    assert.equal(typeof messages.importInvalidFile, "string"); assert(messages.importInvalidFile.trim()); assert(messages.importOutcomeUnknown.trim());
    notices.push(messages.importOutcomeUnknown);
  }
  assert.equal(new Set(notices).size, 4);
});
