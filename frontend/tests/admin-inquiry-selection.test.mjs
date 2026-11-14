import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/app/[locale]/admin/inquiries/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("admin-inquiries.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
function callback(name) {
  const matches = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(ast) === name) matches.push(node.initializer);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.equal(matches.length, 1);
  const value = matches[0];
  return (ts.isCallExpression(value) ? value.arguments[0] : value).getText(ast);
}
function cleanupEffect() {
  const matches = [];
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(ast) === "useEffect"
      && node.arguments[0].getText(ast).includes("pendingActionsRef.current")) matches.push(node.arguments[0]);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.equal(matches.length, 1);
  return matches[0].getText(ast);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const detail = (id, status = "open") => ({ id, subject: id, content: `<p>${id}</p>`, status, replies: [] });
const reply = { notification: { state: "queued" } };

function harness() {
  const state = { selection: { id: null, generation: 0 }, detailState: null, detailLoading: false,
    detailError: null, replyContent: "", pendingActions: {}, page: 1, statusFilter: "all",
    inquiries: [], total: 0, loading: false };
  const requests = [], listRequests = [], toasts = [], writes = [];
  const owner = { ownerId: "admin", epoch: 1 };
  let ownerCurrent = true;
  const context = { AbortController, URLSearchParams, ApiError: class ApiError extends Error {}, state, owner, user: { id: "admin" },
    selectionRef: { current: state.selection }, detailControllerRef: { current: null }, detailRequestRef: { current: 0 },
    loadedDetailRef: { current: null },
    listControllerRef: { current: null }, listRequestRef: { current: 0 }, mountedRef: { current: true },
    listQueryRef: { current: { key: "1:all", page: 1, statusFilter: "all" } }, latestFetchListRef: { current: null },
    replyDraftsRef: { current: new Map() }, pendingActionsRef: { current: new Map() },
    isRichContentEmpty: (value) => !value.replace(/<[^>]*>/g, "").trim(),
    captureApiRequestOwner: (id) => { assert.equal(id, "admin"); return owner; },
    isCurrentApiRequestOwner: (value) => value === owner && ownerCurrent,
    toast: (...args) => toasts.push(args), t: (key) => key,
    PAGE_SIZE: 20, holdLists: false,
    apiFetch: (pathname, options = {}) => {
      const pending = deferred();
      if (pathname.includes("/inquiries?")) {
        listRequests.push({ pathname, options, ...pending });
        if (!context.holdLists) pending.resolve({ items: [], total: 0 });
        return pending.promise;
      }
      requests.push({ pathname, options, ...pending });
      return pending.promise;
    },
  };
  for (const key of Object.keys(state)) {
    const setter = `set${key[0].toUpperCase()}${key.slice(1)}`;
    context[setter] = (value) => {
      state[key] = typeof value === "function" ? value(state[key]) : value;
      writes.push(key);
    };
  }
  const names = ["isCurrentSelection", "fetchList", "fetchDetail", "selectInquiry", "handleSelect", "handleReplyChange", "submitInquiryAction", "handleReply", "handleClose", "handleFilterChange", "handlePageChange"];
  const program = `globalThis.bind = (selection, detailState, detailLoading, replyContent, page, statusFilter) => {
    const listQueryKey = \`\${page}:\${statusFilter}\`;
    const selectedId = selection.id;
    const detail = detailState?.selection === selection ? detailState.data : null;
    const detailReady = detail?.id === selectedId && !detailLoading;
    ${names.map((name) => `const ${name} = ${callback(name)};`).join("\n")}
    return { ${names.join(", ")} };
  }; globalThis.lifecycle = ${cleanupEffect()};`;
  vm.runInNewContext(ts.transpileModule(program, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  } }).outputText, context, { timeout: 1000 });
  const cleanup = context.lifecycle();
  const render = () => {
    const callbacks = context.bind(state.selection, state.detailState, state.detailLoading, state.replyContent, state.page, state.statusFilter);
    context.latestFetchListRef.current = callbacks.fetchList;
    return callbacks;
  };
  return { state, context, requests, listRequests, toasts, writes, owner, render, unmount: cleanup,
    select: (id) => render().handleSelect(id), draft: (value) => render().handleReplyChange(value),
    async ready(id) {
      this.select(id);
      requests.at(-1).resolve(detail(id));
      await tick();
    },
    changeOwner() { ownerCurrent = false; }, restoreOwner() { ownerCurrent = true; },
  };
}

for (const outcome of ["success", "failure"]) {
  test(`late A detail ${outcome} cannot replace B detail, error, or loading`, async () => {
    const h = harness(); h.select("A"); h.select("B");
    assert.equal(h.requests[0].options.signal.aborted, true);
    if (outcome === "success") h.requests[0].resolve(detail("A"));
    else h.requests[0].reject(new Error("old A load failure"));
    await tick();
    assert.equal(h.state.detailState, null);
    assert.equal(h.state.detailLoading, true);
    assert.equal(h.state.detailError, null);
    h.requests[1].resolve(detail("B")); await tick();
    assert.equal(h.state.detailState.data.id, "B");
    assert.equal(h.state.detailLoading, false);
    assert.equal(h.toasts.length, 0);
  });
}

test("selection ref rejects old render callbacks before React commit and blocks loading mutations", async () => {
  const h = harness(); await h.ready("A"); h.draft("A draft");
  const old = h.render(); old.handleSelect("B");
  await old.handleReply(); await old.handleClose();
  await h.render().handleReply(); await h.render().handleClose();
  old.handleReplyChange("old editor event");
  assert.equal(h.requests.length, 2);
  assert.equal(h.state.replyContent, "");
  assert.equal(h.context.replyDraftsRef.current.get("A"), "A draft");
  h.requests[1].resolve(detail("B")); await tick();
  await old.handleReply(); await old.handleClose();
  assert.equal(h.requests.length, 2);
});

test("wrong detail identity fails closed and a current retry restores the correct ticket", async () => {
  const h = harness(); h.select("A"); h.requests[0].resolve(detail("B")); await tick();
  assert.equal(h.state.detailState, null); assert.equal(h.state.detailError, "loadFailed");
  h.draft("saved A draft"); await h.render().handleReply(); await h.render().handleClose();
  assert.equal(h.requests.length, 1);
  const retry = h.render().fetchDetail(h.state.selection);
  h.requests[1].resolve(detail("A")); await retry;
  assert.equal(h.state.detailState.data.id, "A"); assert.equal(h.state.detailError, null);
  assert.equal(h.state.replyContent, "saved A draft");
});

test("current reply failure preserves its draft and permits a retry with the captured payload", async () => {
  const h = harness(); await h.ready("A"); h.draft("A original draft");
  const first = h.render().handleReply();
  assert.equal(h.requests[1].pathname, "/api/community/inquiries/A/reply");
  assert.equal(h.requests[1].options.requestOwner, h.owner);
  await h.render().handleReply(); await h.render().handleClose();
  assert.equal(h.requests.length, 2, "synchronous duplicate and cross-action gate");
  h.requests[1].reject(new h.context.ApiError("reply unavailable")); await first;
  assert.equal(h.state.replyContent, "A original draft"); assert.equal(h.state.pendingActions.A, undefined);
  assert.equal(h.toasts.at(-1)[1], "reply unavailable");
  const retry = h.render().handleReply();
  assert.equal(h.requests[2].options.body.content, "A original draft");
  h.draft("A revised during save"); h.requests[2].resolve(reply); await retry;
  assert.equal(h.state.replyContent, "A revised during save");
  assert.equal(h.context.replyDraftsRef.current.get("A"), "A revised during save");
  h.requests[3].resolve(detail("A")); await tick();
});

test("an old loaded callback cannot send while the same ticket is reloading before React commit", async () => {
  const h = harness(); await h.ready("A"); h.draft("A submitted");
  const loaded = h.render(); const pending = loaded.handleReply();
  h.requests[1].resolve(reply); await pending;
  assert.equal(h.state.detailLoading, true);
  assert.equal(h.context.pendingActionsRef.current.has("A"), false);
  await loaded.handleReply(); await loaded.handleClose();
  assert.equal(h.requests.length, 3, "only the original reply and its detail refresh may be sent");
  h.requests[2].resolve(detail("A")); await tick();
});

for (const outcome of ["success", "failure"]) {
  test(`late A reply ${outcome} preserves B detail/draft/busy and handles only A's submitted draft`, async () => {
    const h = harness(); await h.ready("A"); h.draft("A draft");
    const a = h.render().handleReply();
    await h.ready("B"); h.draft("B draft"); const b = h.render().handleReply();
    if (outcome === "success") h.requests[1].resolve(reply);
    else h.requests[1].reject(new Error("late A failure"));
    await a;
    assert.equal(h.state.detailState.data.id, "B"); assert.equal(h.state.replyContent, "B draft");
    assert.equal(h.state.pendingActions.B, "reply"); assert.equal(h.toasts.length, 0);
    assert.equal(h.requests[1].options.body.content, "A draft");
    assert.equal(h.requests[3].options.body.content, "B draft");
    h.requests[3].reject(new Error("current B failure")); await b;
    h.select("A");
    assert.equal(h.state.replyContent, outcome === "success" ? "" : "A draft");
    h.requests[4].resolve(detail("A")); await tick();
  });
}

test("confirmed late A success retains an A draft edited after submission", async () => {
  const h = harness(); await h.ready("A"); h.draft("A submitted");
  const pending = h.render().handleReply(); h.draft("A newer unsent draft");
  await h.ready("B"); h.draft("B draft");
  h.requests[1].resolve(reply); await pending;
  assert.equal(h.state.replyContent, "B draft");
  assert.equal(h.context.replyDraftsRef.current.get("A"), "A newer unsent draft");
  h.select("A"); assert.equal(h.state.replyContent, "A newer unsent draft");
  h.requests[3].resolve(detail("A")); await tick();
});

for (const revised of [false, true]) {
  test(`A to B to A confirmed reply clears only the submitted draft (${revised ? "revised draft kept" : "unchanged draft cleared"})`, async () => {
    const h = harness(); await h.ready("A"); h.draft("A submitted");
    const pending = h.render().handleReply();
    await h.ready("B"); h.draft("B unsent"); await h.ready("A");
    if (revised) h.draft("A newer draft after return");
    h.requests[1].resolve(reply); await pending;
    assert.equal(h.state.replyContent, revised ? "A newer draft after return" : "");
    assert.equal(h.context.replyDraftsRef.current.get("B"), "B unsent");
    assert.equal(h.context.replyDraftsRef.current.get("A"), revised ? "A newer draft after return" : undefined);
    assert.equal(h.requests[4].pathname, "/api/community/inquiries/A");
    h.requests[4].resolve(detail("A", "answered")); await tick();
    assert.equal(h.state.detailState.selection, h.state.selection);
    assert.equal(h.state.detailState.data.status, "answered");
  });
}

test("late reply after page navigation refreshes the current page and old query callbacks cannot start", async () => {
  const h = harness(); await h.ready("A"); h.draft("A page draft");
  const old = h.render(); const pending = old.handleReply();
  old.handlePageChange(2);
  await old.fetchList(); assert.equal(h.listRequests.length, 0, "old page rejected before commit");
  const current = h.render();
  h.requests[1].resolve(reply); await pending; await tick();
  assert.equal(h.state.page, 2);
  assert.equal(h.listRequests.length, 1);
  assert.match(h.listRequests[0].pathname, /page=2/);
  await old.fetchList(); assert.equal(h.listRequests.length, 1);
  h.requests[2].resolve(detail("A")); await tick();
  await current.fetchList(); assert.equal(h.listRequests.length, 2);
  assert.match(h.listRequests[1].pathname, /page=2/);
});

test("a list response from the previous page cannot commit after synchronous query change", async () => {
  const h = harness(); h.context.holdLists = true;
  const old = h.render(); const request = old.fetchList();
  old.handlePageChange(2);
  h.listRequests[0].resolve({ items: [{ id: "old-page-row" }], total: 99 }); await request;
  assert.deepEqual(h.state.inquiries, []); assert.equal(h.state.total, 0);
  const current = h.render().fetchList();
  h.listRequests[1].resolve({ items: [{ id: "current-page-row" }], total: 40 }); await current;
  assert.equal(h.state.inquiries[0].id, "current-page-row"); assert.equal(h.state.total, 40);
});

test("late close of A cannot close or refresh B and failure preserves A's draft", async () => {
  const h = harness(); await h.ready("A"); h.draft("A unsent draft");
  const operation = h.render().handleClose();
  assert.equal(h.requests[1].pathname, "/api/community/inquiries/A/status?status=closed");
  await h.ready("B"); h.draft("B unsent draft");
  h.requests[1].resolve({}); await operation;
  assert.equal(h.requests.length, 3); assert.equal(h.state.detailState.data.status, "open");
  assert.equal(h.state.replyContent, "B unsent draft"); assert.equal(h.toasts.length, 0);
  h.select("A"); assert.equal(h.state.replyContent, "A unsent draft");
  h.requests[3].resolve(detail("A", "closed")); await tick();
  await h.render().handleReply(); await h.render().handleClose();
  assert.equal(h.requests.length, 4);
});

test("filter deselection cancels the GET while retaining per-ticket drafts", async () => {
  const h = harness(); await h.ready("A"); h.draft("A filter draft");
  h.render().handleFilterChange("answered");
  assert.equal(h.state.selection.id, null); assert.equal(h.state.replyContent, "");
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.select("A"); assert.equal(h.state.replyContent, "A filter draft");
  h.requests[1].resolve(detail("A")); await tick();
});

for (const outcome of ["success", "failure"]) {
  test(`unmount aborts and ignores pending reply ${outcome} and detail work`, async () => {
    const h = harness(); await h.ready("A"); h.draft("A draft");
    const pending = h.render().handleReply(); const count = h.writes.length;
    h.unmount();
    assert.equal(h.requests[1].options.signal.aborted, true);
    if (outcome === "success") h.requests[1].resolve(reply);
    else h.requests[1].reject(new Error("unmounted failure"));
    await pending;
    assert.equal(h.writes.length, count); assert.equal(h.toasts.length, 0);
    assert.equal(h.requests.length, 2);
  });
}

test("owner change suppresses old results and releases only the completed operation for manual retry", async () => {
  const h = harness(); await h.ready("A"); h.draft("A draft");
  const pending = h.render().handleReply(); h.changeOwner(); h.requests[1].resolve(reply); await pending;
  assert.equal(h.state.replyContent, "A draft"); assert.equal(h.toasts.length, 0);
  assert.equal(h.state.pendingActions.A, undefined); assert.equal(h.requests.length, 2);
  h.restoreOwner(); const retry = h.render().handleReply(); h.requests[2].resolve(reply); await retry;
  assert.equal(h.state.replyContent, ""); h.requests[3].resolve(detail("A")); await tick();
});
