import assert from "node:assert/strict";
import test from "node:test";
import { randomUUID, createHash } from "node:crypto";
import { createChallengeAttachments, attachmentFileError } from "../src/lib/challenge-attachments.mjs";
import { createImageEditOperation } from "../src/lib/image-edit-operation.mjs";

const sha = "a".repeat(64), file = () => new File(["exact original bytes"], "원본.txt", { type: "text/plain" });
const existing = (id = randomUUID()) => ({ id, legacy_index: null, name: "old.zip", byte_size: 10, sha256: sha, state: "ready", download_path: "/api/learn/challenges/7/attachments/" + id });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function harness({ attachments = [], challengeId, revision = 0, intercept } = {}) {
  let epoch = 1, draft, snapshot, canonical;
  const calls = [], files = new Map(), uploaded = new Map(), operation = createImageEditOperation({ captureOwner: () => ({ ownerId: "own", epoch }), isCurrentOwner: owner => owner.epoch === epoch, onChange() {} });
  operation.setContext("own:7"); operation.mount("own:7");
  const controller = createChallengeAttachments({ operation, files, initial: { challengeId, revision, attachments }, onChange(value) { snapshot = value; }, onCanonical(value) { canonical = value; assert.equal(operation.busy, true); },
    async request(method, path, body, owner) {
      calls.push({ method, path, body, owner });
      if (intercept) { const result = await intercept({ method, path, body, calls, draft, uploaded }); if (result !== undefined) return result; }
      if (method === "POST" && path.endsWith("challenge-attachment-drafts")) {
        draft = { draft_id: body.draft_id, challenge_id: body.challenge_id ?? null, state: "open", base_attachment_revision: revision, current_attachment_revision: revision, attachments, uploads: [] }; return draft;
      }
      if (method === "GET" && path.endsWith(draft?.draft_id)) return { ...draft, uploads: [...uploaded.values()] };
      const id = path.match(/uploads\/([^/]+)/)?.[1];
      if (method === "PUT") {
        const value = { id, draft_id: draft.draft_id, original_name: body.name, byte_size: body.size, sha256: createHash("sha256").update(Buffer.from(await body.arrayBuffer())).digest("hex"), content_type: body.type,
          state: "ready", ready_for_attach: true, retry_allowed: false, reconcile_required: false };
        uploaded.set(id, value); return value;
      }
      if (method === "GET" && uploaded.has(id)) return uploaded.get(id);
      throw Object.assign(new Error("not found"), { status: 404 });
    } });
  return { controller, calls, files, operation, uploaded, get state() { return snapshot ?? controller.snapshot(); }, get canonical() { return canonical; }, changeOwner() { epoch++; } };
}
async function payload(h, legacy = false) { const lease = h.operation.claim("save"); try { return await h.controller.prepareSave(lease, legacy); } finally { h.operation.release(lease); } }

test("filename and 100MiB boundaries preserve arbitrary original names without image-only restrictions", () => {
  assert.equal(attachmentFileError({ name: "자료.tar.gz", size: 100 * 1024 * 1024 }), null);
  for (const name of ["", "../a", "a..zip", "a/b", "a\\b", "a\0b"]) assert.equal(attachmentFileError({ name, size: 0 }), "name");
  assert.equal(attachmentFileError({ name: "a.bin", size: 100 * 1024 * 1024 + 1 }), "size");
});
test("upload sends exact File bytes, distinct duplicate-name IDs, and save preserves selected order", async () => {
  const h = harness(), original = file(); await h.controller.add([original, original]);
  const puts = h.calls.filter(call => call.method === "PUT"); assert.equal(puts.length, 2); assert.equal(puts[0].body, original); assert.equal(puts[1].body, original);
  assert.notEqual(h.state.items[0].id, h.state.items[1].id); assert.equal(h.state.dirty, true); assert.equal(h.state.blocked, false);
  await h.controller.move(h.state.items[1].id, -1); const selected = await payload(h); assert.deepEqual(selected.attachment_ids, h.state.items.map(item => item.id)); assert.equal(h.calls.filter(call => call.method === "POST").length, 1);
});
test("attachment upload synchronously excludes save, draft, navigation and a second upload", async () => {
  const pending = deferred(), h = harness({ intercept: ({ method }) => method === "PUT" ? pending.promise : undefined });
  const work = h.controller.add([file()]); await Promise.resolve(); await Promise.resolve();
  for (const kind of ["save", "autosave", "draft", "navigate", "upload"]) assert.equal(h.operation.claim(kind), null);
  await h.controller.add([file()]); assert.equal(h.state.items.length, 1); pending.reject(new Error("lost")); await work; assert.equal(h.operation.busy, false);
});
test("an uncertain PUT cannot retry until exact status permits it, and reconcile never PUTs", async () => {
  let lose = true, reconciled = false;
  const h = harness({ intercept: ({ method, path, draft, calls }) => {
    const put = calls.find(call => call.method === "PUT"), id = path.match(/uploads\/([^/]+)/)?.[1];
    if (method === "PUT" && lose) { lose = false; throw new Error("lost response"); }
    if (method === "POST" && path.endsWith("/reconcile")) reconciled = true;
    if (id && method !== "PUT") return { id, draft_id: draft.draft_id, original_name: put.body.name, byte_size: put.body.size, sha256: sha, content_type: put.body.type,
      state: reconciled ? "ready" : "pending", ready_for_attach: reconciled, reconcile_required: !reconciled, retry_allowed: false };
  } });
  await h.controller.add([file()]); const id = h.state.items[0].id;
  assert.equal(h.state.blocked, true); assert.equal(h.state.items[0].canRetry, false); await h.controller.retry(id); assert.equal(h.calls.filter(call => call.method === "PUT").length, 1);
  await h.controller.check(id); assert.equal(h.state.items[0].state, "pending"); await assert.rejects(payload(h), /pending/);
  await h.controller.check(id, true); assert.equal(h.state.blocked, false); assert.equal(h.calls.filter(call => call.method === "PUT").length, 1); assert.deepEqual((await payload(h)).attachment_ids, [id]);
});
test("confirmed missing with retry_allowed reuses precisely the UUID and original File", async () => {
  let failed = false;
  const h = harness({ intercept: ({ method, path, draft }) => {
    if (method === "PUT" && !failed) { failed = true; throw new Error("unknown"); }
    if (method === "GET" && path.includes("/uploads/")) return { id: path.split("/").at(-1), draft_id: draft.draft_id, original_name: file().name, byte_size: file().size, sha256: sha, content_type: "text/plain", state: "missing", retry_allowed: true, ready_for_attach: false, reconcile_required: false };
  } });
  const original = file(); await h.controller.add([original]); const id = h.state.items[0].id; await h.controller.check(id); assert.equal(h.state.items[0].canRetry, true); await h.controller.retry(id);
  const puts = h.calls.filter(call => call.method === "PUT"); assert.equal(puts.length, 2); assert.equal(puts[0].path, puts[1].path); assert.equal(puts[1].body, original); assert.equal(h.state.blocked, false);
});
test("upload404 requires a separately confirmed owner draft, not an owner404 assumption", async () => {
  let absent = false;
  const h = harness({ intercept: ({ method, path }) => { if (method === "PUT") throw new Error("unknown"); if (method === "GET" && (path.includes("/uploads/") || absent)) throw Object.assign(new Error("missing"), { status: 404 }); } });
  await h.controller.add([file()]); const id = h.state.items[0].id; absent = true; await h.controller.check(id); assert.equal(h.state.items[0].canRetry, false);
  absent = false; await h.controller.check(id); assert.equal(h.state.items[0].canRetry, true); assert.equal(h.calls.filter(call => call.method === "PUT").length, 1);
});
for (const outcome of ["success", "failure"]) test(`late upload ${outcome} cannot publish to a new owner or release its lease`, async () => {
  const pending = deferred(), started = deferred(), h = harness({ intercept: ({ method }) => { if (method === "PUT") { started.resolve(); return pending.promise; } } });
  const work = h.controller.add([file()]); await started.promise; const before = JSON.stringify(h.state); h.changeOwner(); const newer = h.operation.claim("save"); assert(newer);
  if (outcome === "success") pending.resolve({}); else pending.reject(new Error("late")); await work;
  assert.equal(JSON.stringify(h.state), before); assert(h.operation.isCurrent(newer));
});
test("unchanged legacy/private connections are omitted, explicit removal uses a draft and never DELETEs storage", async () => {
  const old = existing(), h = harness({ attachments: [old], challengeId: 7, revision: 4 }); assert.deepEqual(await payload(h), {}); assert.equal(h.calls.length, 0);
  await h.controller.remove(old.id); assert.equal(h.state.dirty, true); assert.deepEqual((await payload(h)).attachment_ids, []);
  assert.equal(h.calls[0].body.challenge_id, 7); assert.equal(h.calls.some(call => call.method === "DELETE"), false);
  const legacy = harness({ attachments: [], challengeId: 7, revision: 4 }); assert.deepEqual((await payload(legacy, true)).attachment_ids, []);
});
test("CAS conflict preserves desired files and canonical reload failure cannot erase them", async () => {
  const old = existing(), h = harness({ attachments: [old], challengeId: 7, intercept: ({ method, path, body }) => {
    if (method === "POST" && path.endsWith("drafts")) return { draft_id: body.draft_id, challenge_id: 7, state: "open", base_attachment_revision: 1, current_attachment_revision: 1, attachments: [old], uploads: [] };
  } });
  await h.controller.remove(old.id); await assert.rejects(payload(h), /conflict/); const before = h.state.items; await h.controller.reload(); assert.deepEqual(h.state.items, before); assert.equal(h.state.conflict, true);
});
test("mapping or environment409 is not an attachment CAS conflict", () => {
  const h = harness({ attachments: [existing()], challengeId: 7 });
  for (const code of [undefined, "mapping_revision_conflict", "environment_revision_conflict", "attachment_not_ready"]) { h.controller.saveFailed({ status: 409, code }); assert.equal(h.state.conflict, false); }
  h.controller.saveFailed({ status: 409, code: "attachment_revision_conflict" }); assert.equal(h.state.conflict, true); assert.equal(h.state.error, "conflict");
});
test("all selected original Files survive an earlier upload failure without implicitly retrying either file", async () => {
  const h = harness({ intercept: ({ method }) => { if (method === "PUT") throw new Error("unknown"); } }), first = file(), second = new File(["second"], "second.zip");
  await h.controller.add([first, second]); assert.equal(h.state.items.length, 2); assert.equal(h.files.get(h.state.items[0].id), first); assert.equal(h.files.get(h.state.items[1].id), second);
  assert.equal(h.state.items[0].canRetry, false); assert.equal(h.state.items[1].canRetry, true); assert.equal(h.calls.filter(call => call.method === "PUT").length, 1);
});
test("an uncertain draft POST is checked before the same client draft UUID is created again", async () => {
  let first = true; const h = harness({ intercept: ({ method, path }) => { if (method === "POST" && path.endsWith("drafts") && first) { first = false; throw new Error("unknown draft"); } } });
  await h.controller.add([file()]); assert.equal(h.calls.length, 1); assert.equal(h.state.items[0].attempted, false); await h.controller.retry(h.state.items[0].id);
  assert.deepEqual(h.calls.map(call => call.method), ["POST", "GET", "POST", "PUT"]); assert.equal(h.calls[0].body.draft_id, h.calls[2].body.draft_id);
});
test("ready object bytes without ready_for_attach still require reconciliation before save", async () => {
  const h = harness({ intercept: ({ method, path, body, draft }) => method === "PUT" ? { id: path.split("/").at(-1), draft_id: draft.draft_id, original_name: body.name, byte_size: body.size, sha256: sha, content_type: body.type, state: "ready", ready_for_attach: false, reconcile_required: true, retry_allowed: false } : undefined });
  await h.controller.add([file()]); assert.equal(h.state.items[0].state, "ready"); assert.equal(h.state.blocked, true); await assert.rejects(payload(h), /pending/);
});
test("explicit conflict reload applies the canonical files and attachments within the same lease", async () => {
  const old = existing(), canonical = existing(), h = harness({ attachments: [old], challengeId: 7, intercept: ({ method, path }) => method === "GET" && path === "/api/learn/challenges/7" ? { id: 7, files: ["untouched-legacy"], attachments: [canonical], attachment_revision: 8 } : undefined });
  await h.controller.remove(old.id); h.controller.saveFailed({ status: 409, code: "attachment_revision_conflict" }); await h.controller.reload();
  assert.deepEqual(h.canonical.files, ["untouched-legacy"]); assert.deepEqual(h.state.items.map(item => item.id), [canonical.id]); assert.equal(h.state.dirty, false); assert.equal(h.state.conflict, false); assert(h.calls.every(call => call.method === "GET"));
});
test("autosave canonical response binds to new target and the next upload creates a new target-specific draft", async () => {
  const h = harness(); await h.controller.add([file()]); const firstDraft = h.controller.metadata().draftId, value = existing(h.state.items[0].id), lease = h.operation.claim("autosave");
  h.controller.acceptSaved({ id: 42, attachments: [value], attachment_revision: 1 }, lease); assert.equal(h.canonical.id, 42); assert.equal(h.files.size, 0); assert.equal(h.state.dirty, false); h.operation.release(lease);
  await h.controller.add([file()]); const posts = h.calls.filter(call => call.method === "POST"); assert.equal(posts.length, 2); assert.notEqual(posts[1].body.draft_id, firstDraft); assert.equal(posts[1].body.challenge_id, 42);
});
test("draft restore verifies the actual ledger, never restores File bytes, and cannot retry absent RAM bytes", async () => {
  const h = harness(); await h.controller.add([file()]); const metadata = h.controller.metadata(); assert.equal(JSON.stringify(metadata).includes("exact original bytes"), false);
  const upload = h.uploaded.get(metadata.items[0].id), restored = harness({ intercept: ({ method }) => method === "GET" ? { draft_id: metadata.draftId, challenge_id: null, state: "open", base_attachment_revision: 0, current_attachment_revision: 0, attachments: [], uploads: [{ ...upload, state: "missing", ready_for_attach: false, retry_allowed: true }] } : undefined });
  await restored.controller.restore(metadata); assert.equal(restored.state.blocked, true); assert.equal(restored.state.items[0].canRetry, false); await restored.controller.retry(upload.id); assert.equal(restored.calls.every(call => call.method === "GET"), true); assert.equal(restored.state.error, "fileUnavailable");
});
test("failed restore retains unresolved IDs and blocks save instead of replacing them with an empty list", async () => {
  const h = harness(); await h.controller.add([file()]); const restored = harness(); await restored.controller.restore(h.controller.metadata());
  assert.deepEqual(restored.state.items.map(item => item.id), h.state.items.map(item => item.id)); assert.equal(restored.state.blocked, true); await assert.rejects(payload(restored), /pending/); assert.equal(restored.calls.filter(call => call.method === "PUT").length, 0);
});
