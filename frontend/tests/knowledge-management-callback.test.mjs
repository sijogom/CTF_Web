import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { parseOffsetListPage } from "../src/lib/list-response.mjs";
import { readBulkReviewResults } from "../src/lib/knowledge-review-state.mjs";

const source = readFileSync(new URL("../src/app/[locale]/admin/knowledge-relations/page.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("knowledge-relations.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
const declarations = new Map();
function visit(node) {
  if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
    const value = node.initializer;
    if (value && (ts.isArrowFunction(value) || (ts.isCallExpression(value) && value.expression.getText(ast) === "useCallback"))) declarations.set(node.name.text, ts.isCallExpression(value) ? value.arguments[0].getText(ast) : value.getText(ast));
  }
  ts.forEachChild(node, visit);
}
visit(ast);
const plain = (value) => JSON.parse(JSON.stringify(value));
function callback(name, context) {
  assert(declarations.has(name), name);
  const program = ts.transpileModule(`globalThis.actual = ${declarations.get(name)}`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  vm.runInNewContext(program, context, { timeout: 1000 });
  return context.actual;
}
function stateContext() {
  const state = {}, calls = [], toasts = [], urls = [];
  const context = { state, calls, toasts, urls, URL, URLSearchParams, AbortController, DOMException, Promise, Map, Error,
    user: { id: "author" }, isSuperAdmin: true, saving: false, loading: false, relationError: false, proposalLoading: false, proposalError: false,
    savingRef: { current: false }, reviewBusyRef: { current: false },
    sourceContent: { type: "course", id: "course-id", name: "Course" }, targetContent: { type: "challenge", id: "123", name: "Challenge" },
    form: { sourceType: "course", sourceId: "course-id", targetType: "challenge", targetId: "123", relationType: "helps", isDirected: true, strength: 0.7, confidence: 0.8, evidenceSummary: "Reviewable evidence" },
    editingRelation: null, editReason: "Clarify evidence", reviewReasons: {}, proposalReviewReasons: {},
    tk: (key) => key, m: (key) => key, toast: (...args) => toasts.push(args),
    apiFetch: async (...args) => { calls.push(args); return {}; }, cancelEdit: () => { state.cancelled = true; },
    loadRelations: async (...args) => calls.push(["loadRelations", ...args]), loadProposals: async (...args) => calls.push(["loadProposals", ...args]),
    writeUrlQuery: (...args) => urls.push(args),
  };
  context.reloadCurrentLists = async () => Promise.all([context.loadRelations(), context.loadProposals()]);
  for (const name of ["Saving", "PendingProposalId", "ActiveTab", "SelectedProposalIds", "ProposalStateFilter", "ProposalOriginFilter", "ProposalPage", "Filter", "RelationPage", "ReviewingProposalId", "ReviewReasons", "ProposalReviewReasons", "BulkBusy", "BulkResults"]) {
    context[`set${name}`] = (value) => { state[name] = typeof value === "function" ? value(state[name] ?? {}) : value; };
  }
  return context;
}

test("raw IDs without matching confirmed search selections never create a relation", async () => {
  for (const update of [{ sourceContent: null }, { targetContent: null }, { sourceContent: { type: "lesson", id: "course-id" } }, { targetContent: { type: "challenge", id: "different-id" } }]) {
    const h = Object.assign(stateContext(), update);
    await callback("saveRelation", h)();
    assert.equal(h.calls.length, 0);
    assert.equal(h.toasts[0][1], "selectBoth");
  }
});

test("confirmed selections create once and reload the pending relation list", async () => {
  const h = stateContext();
  let resolve;
  h.apiFetch = (...args) => { h.calls.push(args); return new Promise((done) => { resolve = done; }); };
  const save = callback("saveRelation", h);
  const first = save(); await save();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], "/api/learn/admin/knowledge-relations");
  assert.equal(h.calls[0][1].method, "POST");
  assert.deepEqual(plain(h.calls[0][1].body.source), { type: "course", id: "course-id" });
  resolve({}); await first;
  assert.equal(h.state.cancelled, true);
  assert.deepEqual(plain(h.calls[1]), ["loadRelations", "pending_review", 1]);
  assert.equal(h.savingRef.current, false);
});

test("an approved edit creates a pending revision, switches to its review tab, and refreshes both lists", async () => {
  const h = stateContext();
  h.editingRelation = { id: "published-relation", status: "approved" };
  h.apiFetch = async (...args) => { h.calls.push(args); return { pending_proposal_id: "revision-id" }; };
  await callback("saveRelation", h)();
  assert.equal(h.calls[0][0], "/api/learn/admin/knowledge-relations/published-relation");
  assert.equal(h.calls[0][1].method, "PUT");
  assert.equal(h.calls[0][1].body.reason, "Clarify evidence");
  assert.equal(h.state.PendingProposalId, "revision-id");
  assert.equal(h.state.ActiveTab, "proposals");
  assert(h.calls.some(([name]) => name === "loadRelations"));
  assert(h.calls.some(([name, state]) => name === "loadProposals" && state === "pending_review"));
  assert.equal(h.urls[0][0].knowledge_view, "proposals");
});

test("failed saves retain the selection and form for recovery and release the synchronous busy guard", async () => {
  const h = stateContext();
  h.apiFetch = async () => { throw new Error("conflict from server"); };
  await callback("saveRelation", h)();
  assert.equal(h.state.cancelled, undefined);
  assert.equal(h.form.evidenceSummary, "Reviewable evidence");
  assert.equal(h.sourceContent.name, "Course");
  assert.equal(h.toasts[0][1], "conflict from server");
  assert.equal(h.savingRef.current, false);
  assert.equal(h.state.Saving, false);
});

test("restored proposal review still prevents self approval before any write", async () => {
  const proposal = { id: "p1", state: "pending_review", created_by: "author", evidence: [{}] };
  const h = stateContext(); h.proposalReviewReasons[proposal.id] = "Reviewed carefully";
  await callback("reviewProposal", h)(proposal, "approve");
  assert.equal(h.calls.length, 0);
  assert.equal(h.reviewBusyRef.current, false);
});

test("proposal review refreshes both proposal and relation lists after deactivation", async () => {
  const h = stateContext(), proposal = { id: "p1", state: "approved", created_by: "other", evidence: [{}] };
  h.proposalReviewReasons.p1 = "No longer relevant";
  const review = callback("reviewProposal", h);
  await review(proposal, "retire");
  assert.equal(h.calls[0][0], "/api/learn/admin/knowledge-proposals/p1/review");
  assert.deepEqual(plain(h.calls[0][1].body), { action: "retire", reason: "No longer relevant" });
  assert(h.calls.some(([name]) => name === "loadRelations"));
  assert(h.calls.some(([name]) => name === "loadProposals"));
  assert.equal(h.reviewBusyRef.current, false);
});

test("restored proposal review submits draft rejection and delegates evidence eligibility to the server", async () => {
  for (const [state, action] of [["draft", "reject"], ["pending_review", "approve"]]) {
    const h = stateContext();
    const proposal = { id: "p1", state, created_by: "other", evidence: [] };
    h.proposalReviewReasons.p1 = "Reviewed carefully";
    await callback("reviewProposal", h)(proposal, action);
    assert.equal(h.calls[0][0], "/api/learn/admin/knowledge-proposals/p1/review");
    assert.deepEqual(plain(h.calls[0][1].body), { action, reason: "Reviewed carefully" });
    assert.equal(h.reviewBusyRef.current, false);
  }
});

test("restored bulk review submits mixed states and retains each server outcome and failed selection", async () => {
  for (const action of ["approve", "reject", "retire"]) {
    const h = stateContext();
    h.knowledgeExtensionsEnabled = () => true;
    h.proposals = ["draft", "pending_review", "approved"].map((state, index) => ({ id: `p${index}`, state, created_by: "other", evidence: [], source: { name: `Source ${index}` }, target: { name: `Target ${index}` } }));
    h.selectedProposalIds = h.proposals.map((item) => item.id);
    h.bulkReason = "Reviewed mixed selection";
    h.readBulkReviewResults = readBulkReviewResults;
    const rows = h.proposals.map((item) => {
      const allowed = action === "approve" ? item.state === "pending_review" : action === "reject" ? ["draft", "pending_review"].includes(item.state) : item.state === "approved";
      return { proposal_id: item.id, status: allowed ? "succeeded" : "failed", ...(allowed ? {} : { error: "Server state conflict" }) };
    });
    h.apiFetch = async (...args) => { h.calls.push(args); return { items: rows }; };
    await callback("reviewBulk", h)(action);
    assert.equal(h.calls[0][0], "/api/learn/admin/knowledge-proposals/bulk-review");
    assert.deepEqual(plain(h.calls[0][1].body), { proposal_ids: ["p0", "p1", "p2"], action, reason: h.bulkReason });
    assert.deepEqual(plain(h.state.BulkResults).map(({ proposal_id, status }) => ({ proposal_id, status })), rows.map(({ proposal_id, status }) => ({ proposal_id, status })));
    assert.deepEqual(plain(h.state.SelectedProposalIds), rows.filter((item) => item.status === "failed").map((item) => item.proposal_id));
    assert.equal(h.reviewBusyRef.current, false);
  }
});

test("restored bulk review preserves the existing independent reviewer requirement", async () => {
  const h = stateContext();
  h.knowledgeExtensionsEnabled = () => true;
  h.proposals = [{ id: "own", state: "pending_review", created_by: "author", evidence: [] }];
  h.selectedProposalIds = ["own"];
  h.bulkReason = "Reviewed carefully";
  await callback("reviewBulk", h)("approve");
  assert.equal(h.calls.length, 0);
  assert.equal(h.toasts[0][1], "ownApproval");
});

test("restored selection controls include draft and mixed-state rows without evidence-count approval guards", () => {
  let bulk;
  const checks = [], approveButtons = [];
  function attribute(node, name) {
    return node.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.getText(ast) === name)?.initializer?.expression;
  }
  function find(node) {
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(ast) === "KnowledgeProposalBulkReview") bulk = node;
    if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(ast) === "input" && attribute(node, "checked")?.getText(ast).includes("selectedProposalIds")) checks.push(attribute(node, "disabled"));
    if (ts.isJsxOpeningElement(node) && node.tagName.getText(ast) === "Button" && attribute(node, "onClick")?.getText(ast).includes('reviewProposal(proposal, "approve")')) approveButtons.push(attribute(node, "disabled"));
    ts.forEachChild(node, find);
  }
  find(ast);
  assert(bulk); assert.equal(checks.length, 1); assert.equal(approveButtons.length, 1);
  const proposals = ["draft", "pending_review", "approved"].map((state, index) => ({ id: `p${index}`, state, created_by: "other", evidence: [] }));
  const context = { proposals, selectedProposalIds: [], user: { id: "author" }, saving: false, bulkBusy: false, reviewingProposalId: null, isReviewing: false, isOwnProposal: false,
    setSelectedProposalIds(value) { context.selectedProposalIds = value; } };
  const attributes = ["allSelected", "canApprove", "onSelectPage"];
  const expressions = attributes.map((name) => `${name}: ${attribute(bulk, name).getText(ast)}`).join(",");
  const program = ts.transpileModule(`globalThis.controls = () => ({${expressions}}); globalThis.checkboxDisabled = () => (${checks[0].getText(ast)}); globalThis.approveDisabled = () => (${approveButtons[0].getText(ast)});`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  vm.runInNewContext(program, context, { timeout: 1000 });
  context.controls().onSelectPage(true);
  assert.deepEqual(plain(context.selectedProposalIds), ["p0", "p1", "p2"]);
  assert.equal(context.controls().allSelected, true);
  assert.equal(context.controls().canApprove, true);
  assert.equal(context.checkboxDisabled(), false);
  assert.equal(context.approveDisabled(), false);
});

test("relation deactivation refreshes both lists, preventing a stale approved proposal", async () => {
  const h = stateContext(); h.reviewReasons.r1 = "Remove obsolete relation";
  await callback("reviewRelation", h)({ id: "r1", status: "approved", created_by: "other" }, "retire");
  assert.deepEqual(plain(h.calls[0][1].body), { action: "retire", reason: "Remove obsolete relation" });
  assert(h.calls.some(([name]) => name === "loadRelations"));
  assert(h.calls.some(([name]) => name === "loadProposals"));
});

test("all list callbacks ignore reversed success and distinguish failed current loads", async () => {
  for (const [name, prefix, defaults] of [["loadRelations", "relation", { filter: "all", relationPage: 1 }], ["loadProposals", "proposal", { proposalStateFilter: "all", proposalOriginFilter: "all", proposalPage: 1 }], ["loadCandidateRuns", "candidateRun", { candidateRunStateFilter: "all", candidateRunPage: 1 }]]) {
    const h = Object.assign(stateContext(), defaults), pending = [], values = {};
    h.LIST_PAGE_SIZE = 20; h.parseOffsetListPage = parseOffsetListPage;
    h[`${prefix}RequestRef`] = { current: 0 }; h[`${prefix}AbortRef`] = { current: null };
    h.apiFetch = (_, options) => new Promise((resolve, reject) => pending.push({ resolve, reject, options }));
    for (const key of ["Loading", "RelationError", "ProposalLoading", "ProposalError", "CandidateRunLoading", "CandidateRunError", "Relations", "RelationTotal", "Proposals", "ProposalTotal", "CandidateRuns", "CandidateRunTotal", "SelectedProposalIds"]) h[`set${key}`] = (value) => { values[key] = typeof value === "function" ? value([]) : value; };
    const load = callback(name, h), old = load(), current = load();
    assert.equal(pending[0].options.signal.aborted, true);
    pending[1].resolve({ items: [{ id: "new" }], total: 1, offset: 0, limit: 20 }); await current;
    pending[0].resolve({ items: [{ id: "old" }], total: 1, offset: 0, limit: 20 }); await old;
    const listKey = prefix === "relation" ? "Relations" : prefix === "proposal" ? "Proposals" : "CandidateRuns";
    assert.equal(values[listKey][0].id, "new");
    const failed = load(); pending[2].reject(new Error("network")); await failed;
    const errorKey = prefix === "relation" ? "RelationError" : prefix === "proposal" ? "ProposalError" : "CandidateRunError";
    assert.equal(values[errorKey], true);
    assert.equal(values[listKey][0].id, "new", "failed request must not erase prior data and pretend it is an empty result");
  }
});

test("review completion refreshes the current URL filters instead of a stale action closure", async () => {
  const h = stateContext();
  h.window = { location: { href: "https://example.test/ko/admin/knowledge-relations?relation_status=approved&relation_page=3&proposal_state=pending_review&proposal_origin=rule&proposal_page=2" } };
  h.RELATION_STATUS_FILTERS = ["all", "approved"];
  h.PROPOSAL_STATE_FILTERS = ["all", "pending_review"];
  h.PROPOSAL_ORIGIN_FILTERS = ["all", "rule"];
  h.readEnum = (value, choices, fallback) => choices.includes(value) ? value : fallback;
  h.readPositiveInt = (value) => Number(value) || 1;
  await callback("reloadCurrentLists", h)();
  assert.deepEqual(plain(h.calls), [["loadRelations", "approved", 3], ["loadProposals", "pending_review", "rule", 2]]);
});

test("revision pending hint is limited to unapproved drafts and pending revisions", () => {
  const conditions = [];
  function find(node) {
    if (ts.isJsxExpression(node) && node.expression && ts.isBinaryExpression(node.expression) && node.expression.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken && node.expression.right.getText(ast).includes('m("pendingRevision")')) conditions.push(node.expression.left.getText(ast));
    ts.forEachChild(node, find);
  }
  find(ast);
  assert.equal(conditions.length, 1);
  const context = { Boolean };
  vm.runInNewContext(ts.transpileModule(`globalThis.visible = (proposal) => Boolean(${conditions[0]});`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context, { timeout: 1000 });
  for (const state of ["draft", "pending_review", "approved", "rejected", "retired", "superseded"]) {
    assert.equal(context.visible({ base_relation_id: "published-relation", state }), state === "draft" || state === "pending_review", state);
    assert.equal(context.visible({ base_relation_id: null, state }), false, `${state} without a base relation`);
  }
});

test("four locales expose all management and tag-deletion keys with matching interpolation variables", () => {
  function flatten(value, prefix = "") { return Object.entries(value).flatMap(([key, item]) => typeof item === "object" ? flatten(item, `${prefix}${key}.`) : [[`${prefix}${key}`, item]]); }
  const locales = ["ko", "en", "ja", "zh"].map((locale) => JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8")).knowledgeAdmin);
  const expected = flatten({ management: locales[0].management, tagDeletion: locales[0].tagDeletion });
  for (const locale of locales) {
    const actual = flatten({ management: locale.management, tagDeletion: locale.tagDeletion });
    assert.deepEqual(actual.map(([key]) => key), expected.map(([key]) => key));
    for (const [index, [key, text]] of actual.entries()) {
      assert.equal(typeof text, "string", key); assert(text.trim().length > 0, key);
      assert.deepEqual([...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort(), [...expected[index][1].matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort(), key);
    }
  }
});
