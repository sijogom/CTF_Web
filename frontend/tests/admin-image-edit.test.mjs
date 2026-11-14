import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { createImageEditOperation, imageFileError, uploadEditorImage } from "../src/lib/image-edit-operation.mjs";
import { createChallengeAttachments } from "../src/lib/challenge-attachments.mjs";

const mib = 1024 * 1024;
const png = () => new File(["fixture"], "image.png", { type: "image/png" });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function gateHarness() {
  let epoch = 1;
  const changes = [];
  const operation = createImageEditOperation({
    captureOwner: () => ({ ownerId: "admin", epoch }),
    isCurrentOwner: (owner) => owner.epoch === epoch,
    onChange: (key, kind) => changes.push({ key, kind }),
  });
  operation.setContext("screen:A"); operation.mount("screen:A");
  return { operation, changes, changeOwner: () => { epoch += 1; } };
}
function product(path) {
  const text = readFileSync(new URL(path, import.meta.url), "utf8");
  const ast = ts.createSourceFile(path, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  assert.equal(ast.parseDiagnostics.length, 0);
  function variable(name) {
    const nodes = [];
    function visit(node) {
      if (ts.isVariableDeclaration(node) && node.name.getText(ast) === name) nodes.push(node.initializer);
      ts.forEachChild(node, visit);
    }
    visit(ast); assert.equal(nodes.length, 1, name);
    return nodes[0];
  }
  return { text, ast, expression: (name) => variable(name).getText(ast), callback: (name) => {
    const node = variable(name);
    return (ts.isCallExpression(node) ? node.arguments[0] : node).getText(ast);
  } };
}
function evaluate(program, context) {
  vm.runInNewContext(ts.transpileModule(program, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  } }).outputText, context, { timeout: 1000 });
}

test("file validation follows the two actual server contracts, including GIF and exact size boundaries", () => {
  for (const type of ["image/png", "image/jpeg", "image/webp", "image/gif"]) {
    assert.equal(imageFileError({ type, name: "x.any", size: 5 * mib }, "banner"), null);
  }
  assert.equal(imageFileError({ type: "image/svg+xml", name: "x.svg", size: 10 }, "banner"), "type");
  assert.equal(imageFileError({ type: "image/png", name: "x.png", size: 5 * mib + 1 }, "banner"), "size");
  for (const extension of ["png", "jpg", "JPEG", "gif", "webp"]) {
    assert.equal(imageFileError({ type: "", name: `x.${extension}`, size: 10 * mib }, "thumbnail"), null);
  }
  assert.equal(imageFileError({ type: "image/png", name: "x.txt", size: 10 }, "thumbnail"), "type");
  assert.equal(imageFileError({ type: "image/png", name: "x.png", size: 10 * mib + 1 }, "thumbnail"), "size");
});

for (const held of ["upload", "save", "autosave", "navigate", "draft"]) {
  test(`a synchronous ${held} lease excludes every competing operation before React renders`, () => {
    const { operation } = gateHarness();
    const lease = operation.claim(held);
    for (const next of ["upload", "save", "autosave", "navigate", "draft"]) assert.equal(operation.claim(next), null);
    assert.equal(operation.busy, true);
    operation.release(lease);
    assert.ok(operation.claim("upload"));
  });
}

for (const change of ["owner", "scope", "unmount"]) {
  for (const outcome of ["success", "failure"]) {
    test(`a late upload ${outcome} after ${change} cannot touch the draft or release a newer lease`, async () => {
      const h = gateHarness(), pending = deferred(), writes = [];
      const uploading = uploadEditorImage({ operation: h.operation, file: png(), kind: "banner",
        upload: () => pending.promise, onSuccess: (url) => writes.push(url), onError: (error) => writes.push(error),
        messages: { type: "invalid", size: "large", failed: "failed" } });
      assert.deepEqual(writes, [null]);
      if (change === "owner") h.changeOwner();
      else if (change === "scope") { h.operation.setContext("screen:B"); h.operation.mount("screen:B"); }
      else { h.operation.unmount("screen:A"); h.operation.mount("screen:A"); }
      const newer = h.operation.claim("save");
      assert.ok(newer);
      if (outcome === "success") pending.resolve("new.png"); else pending.reject(new Error("old failure"));
      await uploading;
      assert.deepEqual(writes, [null]);
      assert.equal(h.operation.isCurrent(newer), true);
      assert.equal(h.operation.busy, true);
    });
  }
}

test("invalid upload and transport failures preserve the previous URL and allow only explicit retry", async () => {
  const { operation } = gateHarness(), errors = [];
  let url = "previous.png", requests = 0;
  const options = { operation, kind: "thumbnail", onSuccess: (value) => { url = value; },
    onError: (error) => errors.push(error), messages: { type: "invalid", size: "large", failed: "failed" },
    upload: async () => { requests += 1; throw new Error("transport"); } };
  await uploadEditorImage({ ...options, file: new File(["bad"], "x.svg", { type: "image/svg+xml" }) });
  assert.equal(requests, 0); assert.equal(errors.at(-1), "invalid");
  await uploadEditorImage({ ...options, file: png() });
  assert.equal(requests, 1); assert.equal(url, "previous.png"); assert.equal(operation.busy, false);
  assert.equal(errors.at(-1), "failed");
  await uploadEditorImage({ ...options, file: png(), upload: async () => { requests += 1; return "retry.png"; } });
  assert.equal(requests, 2); assert.equal(url, "retry.png");
});

test("an empty or malformed API URL cannot replace a previous image", async () => {
  for (const response of [undefined, null, {}, "", " "]) {
    const { operation } = gateHarness(); let url = "previous.png", error;
    await uploadEditorImage({ operation, file: png(), kind: "banner", upload: async () => response,
      onSuccess: (value) => { url = value; }, onError: (value) => { error = value; },
      messages: { type: "invalid", size: "large", failed: "failed" } });
    assert.equal(url, "previous.png"); assert.equal(error, "failed"); assert.equal(operation.busy, false);
  }
});

const competitionPaths = ["../src/app/[locale]/admin/competitions/new/page.tsx", "../src/app/[locale]/admin/competitions/[id]/page.tsx"];
function competitionHarness(path) {
  const code = product(path), h = gateHarness(), requests = [], writes = [];
  const context = { operation: h.operation, formReady: true, loading: false, title: "Title", description: "Draft", mode: "individual",
    startDate: "2026-10-15", startTime: "09:00", endDate: "2026-10-16", endTime: "09:00", freezeMinutes: 60,
    bannerUrlRef: { current: "previous.png" }, compId: "1", ApiError: Error, Date, parseInt,
    t: (key) => key, tc: (key) => key, router: { push: (url) => writes.push(["navigation", url]) },
    toast: (...args) => writes.push(args), setError: (value) => writes.push(["error", value]),
    setBannerUrl: (value) => writes.push(["banner", value]),
    setSavedTitle: (value) => writes.push(["title", value]), setSavedDescription: (value) => writes.push(["description", value]),
    setSavedBannerUrl: (value) => writes.push(["saved-banner", value]), setIsPublished: (value) => writes.push(["published", value]),
    setSavedSettings: (value) => writes.push(["saved-settings", value]),
    apiFetch: (url, options) => { const pending = deferred(); requests.push({ url, options, ...pending }); return pending.promise; } };
  const name = path.includes("/new/") ? "handleSubmit" : "handleSave";
  evaluate(`globalThis.save = ${code.callback(name)}; globalThis.changeBanner = ${code.callback("handleBannerChange")};`, context);
  if (path === competitionPaths[1]) evaluate(`globalThis.publish = ${code.callback("handleTogglePublish")};`, context);
  return { ...h, code, context, requests, writes, save: context.save, changeBanner: context.changeBanner };
}
for (const path of competitionPaths) {
  test(`${path.includes("/new/") ? "new" : "edit"} competition excludes same-tick upload/save duplicates and saves the latest image`, async () => {
    const h = competitionHarness(path);
    const uploading = h.operation.claim("upload");
    await h.save(); assert.equal(h.requests.length, 0);
    h.operation.release(uploading);
    h.changeBanner("uploaded.png");
    const saving = h.save(); await h.save();
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].options.body.banner_url, "uploaded.png");
    assert.equal(h.requests[0].options.requestOwner.ownerId, "admin");
    assert.equal(h.operation.claim("upload"), null);
    h.requests[0].resolve({ id: "1" }); await saving;
    assert.equal(h.operation.busy, false);
  });
  test(`${path.includes("/new/") ? "new" : "edit"} competition late save cannot navigate or mark another account's draft saved`, async () => {
    const h = competitionHarness(path), saving = h.save();
    h.changeOwner(); const newer = h.operation.claim("upload");
    h.requests[0].resolve({ id: "1" }); await saving;
    assert.deepEqual(h.writes, [["error", null]]);
    assert.equal(h.operation.isCurrent(newer), true);
  });
  test(`${path.includes("/new/") ? "new" : "edit"} competition image-only changes are dirty`, () => {
    const code = product(path), context = { formReady: true, title: "", description: "", savedTitle: "", savedDescription: "", bannerUrl: "new.png", savedBannerUrl: "old.png", savedSettings: {} };
    evaluate(`globalThis.dirty = ${code.expression("isDirty")};`, context);
    assert.equal(context.dirty, true);
  });
  test(`${path.includes("/new/") ? "new" : "edit"} competition save failure preserves its image and allows manual retry`, async () => {
    const h = competitionHarness(path);
    h.changeBanner("uploaded.png");
    const saving = h.save(); h.requests[0].reject(new Error("failed")); await saving;
    assert.equal(h.context.bannerUrlRef.current, "uploaded.png");
    assert.equal(h.operation.busy, false);
    assert.equal(h.writes.some(([kind]) => kind === "saved-banner" || kind === "navigation"), false);
    const retry = h.save(); assert.equal(h.requests.length, 2);
    h.requests[1].resolve({ id: "1" }); await retry;
  });
}

const initialCompetitionFields = { title: "", description: "", bannerUrl: null, mode: "individual", startDate: "",
  startTime: "09:00", endDate: "", endTime: "18:00", freezeMinutes: 60 };
const editedCompetitionFields = { title: "Loaded title", description: "Loaded body", bannerUrl: "loaded.png", mode: "team",
  startDate: "2026-10-15", startTime: "10:00", endDate: "2026-10-16", endTime: "17:00", freezeMinutes: 45 };
for (const path of competitionPaths) {
  test(`${path.includes("/new/") ? "new" : "edit"} competition dirty tracks every displayed field and retains the exact original defaults`, () => {
    const code = product(path), editing = path === competitionPaths[1];
    const original = editing ? editedCompetitionFields : initialCompetitionFields;
    const context = { ...original, formReady: true, savedTitle: original.title, savedDescription: original.description,
      savedBannerUrl: original.bannerUrl, savedSettings: { ...original } };
    const dirty = () => { evaluate(`globalThis.dirty = ${code.expression("isDirty")};`, context); return context.dirty; };
    assert.equal(dirty(), false);
    for (const field of Object.keys(original)) {
      const changed = typeof original[field] === "number" ? original[field] + 1 : `${original[field] ?? ""}-changed`;
      context[field] = changed; assert.equal(dirty(), true, field);
      context[field] = original[field]; assert.equal(dirty(), false, `restored ${field}`);
    }
    if (editing) { context.formReady = false; context.title = "old route draft"; assert.equal(dirty(), false); }
  });
}
test("edit competition save success records submitted values only and keeps in-flight new edits dirty", async () => {
  const h = competitionHarness(competitionPaths[1]), submitted = { ...editedCompetitionFields };
  Object.assign(h.context, submitted); h.context.bannerUrlRef.current = submitted.bannerUrl;
  const saving = h.save();
  h.context.title = "Entered while saving";
  h.context.mode = "individual";
  h.context.startDate = "2026-10-17";
  h.context.startTime = "11:00";
  h.context.endDate = "2026-10-18";
  h.context.endTime = "19:00";
  h.context.freezeMinutes = 15;
  h.requests[0].resolve({}); await saving;
  h.context.savedTitle = h.writes.find(([name]) => name === "title")[1];
  h.context.savedDescription = h.writes.find(([name]) => name === "description")[1];
  h.context.savedBannerUrl = h.writes.find(([name]) => name === "saved-banner")[1];
  h.context.savedSettings = h.writes.find(([name]) => name === "saved-settings")[1];
  assert.equal(h.context.savedTitle, submitted.title);
  for (const field of ["mode", "startDate", "startTime", "endDate", "endTime", "freezeMinutes"]) {
    assert.equal(h.context.savedSettings[field], submitted[field]);
  }
  assert.equal(h.requests[0].options.body.mode, submitted.mode);
  assert.equal(h.requests[0].options.body.start_at, `${submitted.startDate}T${submitted.startTime}`);
  assert.equal(h.requests[0].options.body.freeze_minutes, submitted.freezeMinutes);
  evaluate(`globalThis.dirty = ${h.code.expression("isDirty")};`, h.context);
  assert.equal(h.context.dirty, true);
  Object.assign(h.context, submitted);
  evaluate(`globalThis.dirty = ${h.code.expression("isDirty")};`, h.context);
  assert.equal(h.context.dirty, false);
});

test("competition publish excludes same-tick duplicates, save, and upload with the existing owner-bound API", async () => {
  const h = competitionHarness(competitionPaths[1]);
  for (const kind of ["upload", "save", "load"]) {
    const lease = h.operation.claim(kind); await h.context.publish(); h.operation.release(lease);
    assert.equal(h.requests.length, 0);
  }
  const publishing = h.context.publish(); await h.context.publish(); await h.save();
  assert.equal(h.requests.length, 1); assert.equal(h.operation.claim("upload"), null);
  assert.equal(h.requests[0].url, "/api/ctf/competitions/1/publish");
  assert.equal(h.requests[0].options.method, "PATCH");
  assert.equal(h.requests[0].options.requestOwner.ownerId, "admin");
  h.requests[0].resolve({ is_published: true }); await publishing;
  assert.deepEqual(h.writes, [["published", true], ["success", "publishBtn"]]);
  assert.equal(h.operation.busy, false);
});
for (const departure of ["owner", "scope", "unmount"]) {
  for (const outcome of ["success", "failure"]) {
    test(`late competition publish ${outcome} after ${departure} cannot update status, toast, or release newer work`, async () => {
      const h = competitionHarness(competitionPaths[1]), publishing = h.context.publish();
      if (departure === "owner") h.changeOwner();
      else if (departure === "scope") { h.operation.setContext("screen:B"); h.operation.mount("screen:B"); }
      else { h.operation.unmount("screen:A"); h.operation.mount("screen:A"); }
      const newer = h.operation.claim("upload");
      if (outcome === "success") h.requests[0].resolve({ is_published: true }); else h.requests[0].reject(new Error("old failure"));
      await publishing; assert.deepEqual(h.writes, []); assert.equal(h.operation.isCurrent(newer), true);
    });
  }
}
test("competition publish failure retains status and releases the gate for manual retry", async () => {
  const h = competitionHarness(competitionPaths[1]), publishing = h.context.publish();
  h.requests[0].reject(new Error("failed")); await publishing;
  assert.deepEqual(h.writes, [["error", "failed"]]); assert.equal(h.operation.busy, false);
  const retry = h.context.publish(); h.requests[1].resolve({ is_published: false }); await retry;
  assert.deepEqual(h.writes.at(-2), ["published", false]); assert.deepEqual(h.writes.at(-1), ["success", "unpublishBtn"]);
});
test("competition publish is unavailable until this account and route's form load is ready", async () => {
  const h = competitionHarness(competitionPaths[1]); h.context.formReady = false;
  await h.context.publish(); await h.save(); assert.equal(h.requests.length, 0);
});

test("competition initial load is exclusive and a late departed load cannot replace draft or loading", async () => {
  const h = competitionHarness(competitionPaths[1]), pending = deferred();
  for (const name of ["Loading", "Title", "SavedTitle", "Description", "SavedDescription", "Mode", "StartDate", "StartTime", "EndDate", "EndTime", "FreezeMinutes", "BannerUrl", "SavedBannerUrl", "SavedSettings", "IsPublished", "ParticipantCount", "CompStatus", "LoadedKey"]) {
    h.context[`set${name}`] = (value) => h.writes.push([name, value]);
  }
  h.context.splitDateTime = () => ({ date: "2026-10-15", time: "09:00" });
  h.context.apiFetch = () => pending.promise;
  evaluate(`globalThis.load = ${h.code.callback("fetchCompetition")};`, h.context);
  const loading = h.context.load();
  await h.context.load();
  assert.equal(h.writes.filter(([name, value]) => name === "Loading" && value === false).length, 0);
  h.operation.unmount("screen:A"); h.operation.mount("screen:A");
  const newer = h.operation.claim("load");
  pending.resolve({ title: "old", description: "old", banner_url: "old.png" }); await loading;
  assert.deepEqual(h.writes, [["Loading", true], ["error", null]]);
  assert.equal(h.operation.isCurrent(newer), true);
});

function wizardHarness() {
  const code = product("../src/components/admin/challenges/ChallengeWizard.tsx"), h = gateHarness(), requests = [], writes = [];
  const context = { operation: h.operation, mappingController: { busy: false, dirty: false, payload: {} },
    currentMode: "create", challengeIdRef: { current: undefined }, stepRef: { current: 0 },
    dataRef: { current: null }, savedFilesRef: { current: [] }, ApiError: Error, ENV_STEP_INDEX: 3, JSON,
    attachments: { blocked: false, dirty: false, controller: createChallengeAttachments({ operation: h.operation,
      initial: { attachments: [], revision: 0 }, files: new Map(), request: async () => { throw new Error("unexpected attachment request"); }, onChange() {} }) },
    tchall: (key) => key, tm: (key) => key, tc: (key) => key,
    toast: (...args) => writes.push(args), router: { push: (url) => writes.push(["navigation", url]) },
    isRichContentEmpty: (value) => !value.trim(), setData: (value) => writes.push(["data", value]),
    setStep: (value) => writes.push(["step", value]), setCurrentChallengeId: (value) => writes.push(["id", value]),
    setCurrentMode: (value) => writes.push(["mode", value]),
    apiFetch: (url, options) => { const pending = deferred(); requests.push({ url, options, ...pending }); return pending.promise; } };
  const names = ["updateData", "toBackendPayload", "autoSaveForEnvStep", "validateBasicInfo", "validateFlag", "goToStep", "handleSubmit"];
  evaluate(`const INITIAL_DATA = ${code.expression("INITIAL_DATA")}; globalThis.initial = INITIAL_DATA;
    ${names.map((name) => `const ${name} = ${code.callback(name)};`).join("\n")}
    globalThis.callbacks = { ${names.join(", ")} };`, context);
  context.dataRef.current = { ...context.initial, title: "Title", description: "Draft", flagValue: "flag" };
  return { ...h, code, context, requests, writes, ...context.callbacks };
}
async function wizardRequest(h, index = 0) {
  for (let attempt = 0; attempt < 30 && !h.requests[index]; attempt++) await Promise.resolve();
  assert.ok(h.requests[index], `wizard request ${index} registered`); return h.requests[index];
}
test("thumbnail upload excludes wizard save and navigation; the first autosave includes its URL", async () => {
  const h = wizardHarness(), upload = h.operation.claim("upload");
  await h.goToStep(3); await h.handleSubmit(); assert.equal(h.requests.length, 0);
  h.updateData({ thumbnailUrl: "new.png" }); h.operation.release(upload);
  const moving = h.goToStep(3); await h.goToStep(3); await h.handleSubmit();
  await wizardRequest(h);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.body.thumbnail_url, "new.png");
  assert.equal(h.requests[0].options.requestOwner.ownerId, "admin");
  assert.equal(h.operation.claim("upload"), null);
  h.requests[0].resolve({ id: 42, attachments: [], attachment_revision: 0 }); await moving;
  await h.goToStep(4); assert.equal(h.requests.length, 1);
  assert.equal(h.context.challengeIdRef.current, 42); assert.equal(h.context.stepRef.current, 4);
  const saving = h.handleSubmit();
  await wizardRequest(h, 1);
  assert.equal(h.requests[1].url, "/api/learn/challenges/42");
  assert.equal(h.requests[1].options.method, "PATCH");
  h.requests[1].resolve({ id: 42, attachments: [], attachment_revision: 0 }); await saving;
});
test("failed autosave preserves thumbnail and draft, does not advance, and permits a manual retry", async () => {
  const h = wizardHarness(); h.updateData({ thumbnailUrl: "new.png" });
  const moving = h.goToStep(3); await wizardRequest(h); h.requests[0].reject(new Error("failed")); await moving;
  assert.equal(h.context.stepRef.current, 0); assert.equal(h.context.dataRef.current.thumbnailUrl, "new.png");
  assert.equal(h.context.challengeIdRef.current, undefined); assert.equal(h.operation.busy, false);
  const retry = h.goToStep(3); await wizardRequest(h, 1); assert.equal(h.requests.length, 2);
  h.requests[1].resolve({ id: 43, attachments: [], attachment_revision: 0 }); await retry; assert.equal(h.context.stepRef.current, 3);
});
test("a malformed autosave ID is not treated as a saved draft or an available environment target", async () => {
  for (const id of [undefined, null, "42", 0, -1, 1.5]) {
    const h = wizardHarness(), moving = h.goToStep(3);
    await wizardRequest(h);
    h.requests[0].resolve({ id }); await moving;
    assert.equal(h.context.challengeIdRef.current, undefined); assert.equal(h.context.stepRef.current, 0);
    assert.equal(h.writes.some(([kind]) => kind === "id" || kind === "mode"), false);
    assert.equal(h.operation.busy, false);
  }
});
for (const outcome of ["success", "failure"]) {
  test(`a late wizard autosave ${outcome} cannot change identity, toast, or step`, async () => {
    const h = wizardHarness(), moving = h.goToStep(3);
    await wizardRequest(h);
    h.operation.unmount("screen:A"); h.operation.mount("screen:A");
    const newer = h.operation.claim("upload");
    if (outcome === "success") h.requests[0].resolve({ id: 42 }); else h.requests[0].reject(new Error("old failure"));
    await moving;
    assert.deepEqual(h.writes, []); assert.equal(h.context.challengeIdRef.current, undefined);
    assert.equal(h.operation.isCurrent(newer), true);
  });
}
test("thumbnail-only edits are dirty without invalidating canonical environment verification", () => {
  const h = wizardHarness(), initial = { ...h.context.dataRef.current, environmentReady: true,
    environmentVerifiedRevision: 7, environmentVerifiedAt: "verified", environmentLastFailureCode: null };
  h.context.dataRef.current = initial; h.updateData({ thumbnailUrl: "new.png" });
  const changed = h.context.dataRef.current;
  assert.equal(changed.environmentVerifiedRevision, 7); assert.equal(changed.environmentVerifiedAt, "verified");
  h.context.data = changed; h.context.initialData = initial;
  evaluate(`globalThis.dirty = (${h.code.callback("isDirty")})();`, h.context);
  assert.equal(h.context.dirty, true);
});
test("wizard unchanged legacy JSONB is omitted on edit while explicit removal remains a save mutation", () => {
  const h = wizardHarness(); h.context.challengeIdRef.current = 42; h.context.savedFilesRef.current = ["old.zip"]; h.context.dataRef.current.files = ["old.zip"];
  assert.equal(Object.hasOwn(h.toBackendPayload(), "files"), false); h.context.dataRef.current.files = [];
  assert.equal(JSON.stringify(h.toBackendPayload().files), "[]");
});
for (const handler of ["goToStep", "handleSubmit"]) test(`wizard ${handler} cannot dispatch after owner changes during attachment preparation`, async () => {
  const h = wizardHarness(), pending = deferred(); h.context.attachments.controller.prepareSave = () => pending.promise;
  const work = handler === "goToStep" ? h.goToStep(3) : h.handleSubmit(); h.changeOwner(); const newer = h.operation.claim("upload"); assert.ok(newer);
  pending.resolve({}); await work; assert.equal(h.requests.length, 0); assert.equal(h.operation.isCurrent(newer), true); assert.deepEqual(h.writes, []);
});

test("thumbnail removal cannot race an upload or a wizard save and retains synchronous draft state", () => {
  const code = product("../src/components/admin/challenges/StepBasicInfo.tsx"), h = gateHarness(), changes = [];
  const context = { operation: h.operation, disabled: false, onChange: (value) => changes.push(value.thumbnailUrl),
    setThumbnailError: () => {}, ta: (key) => key };
  evaluate(`globalThis.remove = ${code.callback("removeThumbnail")};`, context);
  for (const kind of ["upload", "save", "autosave"]) {
    const lease = h.operation.claim(kind); context.remove(); h.operation.release(lease);
    assert.deepEqual(changes, []);
  }
  context.remove(); assert.deepEqual(changes, [null]); assert.equal(h.operation.busy, false);
});

test("the React adapter invalidates the committed old screen synchronously and masks old pending UI", () => {
  const code = product("../src/lib/use-image-edit-operation.ts");
  const state = [], memos = [], effects = [];
  let stateIndex, memoIndex, effectIndex, auth = { id: "A" }, epoch = 1;
  const same = (left, right) => left?.length === right.length && left.every((value, index) => value === right[index]);
  const context = { exports: {}, require: (name) => {
    if (name === "react") return {
      useState: (initial) => { const index = stateIndex++; if (!(index in state)) state[index] = initial;
        return [state[index], (value) => { state[index] = value; }]; },
      useMemo: (factory, deps) => { const index = memoIndex++; if (!same(memos[index]?.deps, deps)) memos[index] = { deps, value: factory() }; return memos[index].value; },
      useLayoutEffect: (effect, deps) => { const index = effectIndex++;
        if (!same(effects[index]?.deps, deps)) effects[index] = { deps, effect, oldCleanup: effects[index]?.cleanup }; },
    };
    if (name === "@/providers/AuthProvider") return { useAuth: () => ({ user: auth }) };
    if (name === "@/lib/api-client") return { captureApiRequestOwner: (id) => ({ ownerId: id, epoch }),
      isCurrentApiRequestOwner: (owner) => owner.ownerId === auth.id && owner.epoch === epoch };
    if (name === "@/lib/image-edit-operation.mjs") return { createImageEditOperation };
    throw new Error(name);
  } };
  evaluate(code.text, context);
  const render = (scope) => {
    stateIndex = 0; memoIndex = 0; effectIndex = 0;
    const result = context.exports.useImageEditOperation(scope);
    for (const entry of effects) if (entry.effect) { entry.oldCleanup?.(); entry.cleanup = entry.effect(); entry.effect = null; }
    return result;
  };
  let screen = render("one");
  const oldGate = screen.operation, oldLease = oldGate.claim("upload");
  screen = render("one"); assert.equal(screen.uploading, true);
  screen = render("two"); assert.equal(screen.busy, false); assert.equal(oldGate.isCurrent(oldLease), false);
  const newer = screen.operation.claim("save"); oldGate.release(oldLease);
  screen = render("two"); assert.equal(screen.kind, "save"); assert.equal(screen.operation.isCurrent(newer), true);
  auth = { id: "B" }; epoch += 1;
  screen = render("two"); assert.equal(screen.busy, false); assert.equal(screen.operation.claim("upload").owner.ownerId, "B");
  screen = render("two"); assert.equal(screen.uploading, true);
  epoch += 1;
  screen = render("two"); assert.equal(screen.busy, false);
  assert.equal(screen.operation.claim("save").owner.epoch, epoch);
  for (const entry of effects) entry.cleanup?.();
  assert.equal(screen.operation.claim("upload"), null);
});

for (const [path, handler] of [["../src/components/admin/competitions/BannerUploadZone.tsx", "handleUpload"],
  ["../src/components/admin/challenges/StepBasicInfo.tsx", "handleThumbnailUpload"]]) {
  test(`${handler} uses the shared lease, existing owner-bound API, visible errors, and a native keyboard button`, async () => {
    const code = product(path), h = gateHarness(), pending = deferred(), calls = [], errors = [], results = [];
    const context = { operation: h.operation, disabled: false, FormData, uploadEditorImage,
      tc: (key) => key, t: (key) => key, ta: (key) => key, tco: (key) => key,
      onBannerChange: (url) => results.push(url), onChange: (value) => results.push(value.thumbnailUrl),
      onError: (error) => errors.push(error), setThumbnailError: (error) => errors.push(error),
      apiFormFetch: (url, form, options) => { calls.push({ url, form, options }); return pending.promise; } };
    evaluate(`globalThis.upload = ${code.callback(handler)};`, context);
    const uploading = context.upload(png()); await context.upload(png());
    assert.equal(calls.length, 1); assert.equal(calls[0].options.requestOwner.ownerId, "admin");
    pending.resolve({ banner_url: "new.png", url: "new.png" }); await uploading;
    assert.deepEqual(results, ["new.png"]); assert.deepEqual(errors, [null]);
    const dropNodes = [];
    function visit(node) {
      if (ts.isJsxOpeningElement(node) && node.attributes.properties.some((prop) => prop.name?.text === "onDrop")) dropNodes.push(node);
      ts.forEachChild(node, visit);
    }
    visit(code.ast); assert.equal(dropNodes.length, 1);
    assert.equal(dropNodes[0].tagName.getText(code.ast), "button");
    assert.ok(dropNodes[0].attributes.properties.some((prop) => prop.name?.text === "disabled"));
  });
}
