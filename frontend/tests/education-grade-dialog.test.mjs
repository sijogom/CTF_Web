import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/teaching/TeachingAssignmentDetail.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("TeachingAssignmentDetail.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
assert.equal(ast.parseDiagnostics.length, 0);
function find(predicate) {
  const result = [];
  function visit(node) {
    if (predicate(node)) result.push(node);
    ts.forEachChild(node, visit);
  }
  visit(ast);
  return result;
}
function callback(name) {
  const matches = find((node) => ts.isVariableDeclaration(node) && node.name.getText(ast) === name);
  assert.equal(matches.length, 1, name);
  const value = matches[0].initializer;
  return (ts.isCallExpression(value) ? value.arguments[0] : value).getText(ast);
}
function effect(fragment) {
  const matches = find((node) => ts.isCallExpression(node) && node.expression.getText(ast) === "useEffect"
    && node.arguments[0].getText(ast).includes(fragment));
  assert.equal(matches.length, 1, fragment);
  return matches[0].arguments[0].getText(ast);
}
const gradeButton = find((node) => ts.isJsxOpeningElement(node) && node.tagName.getText(ast) === "button"
  && node.attributes.getText(ast).includes("openGrade(submission)"))[0];
assert(gradeButton);
const disabledExpression = gradeButton.attributes.properties.find((node) => node.name?.getText(ast) === "disabled").initializer.expression.getText(ast);
const dialog = find((node) => ts.isJsxOpeningElement(node) && node.tagName.getText(ast) === "dialog");
const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const submission = (id) => ({ id: `submission-${id}`, student_id: id, assignment_id: "assignment" });
const detail = (id) => ({ submission: { ...submission(id), content_markdown: `<p>${id} body</p>` },
  grade: { score: 17, feedback_markdown: "<p>persisted feedback</p>", status: "draft" } });

function harness() {
  const state = { gradeTarget: null, gradeDetail: null, score: "", feedback: "", gradeStatus: "published",
    gradeLoading: false, gradeError: null, savingGrade: false, confirmDiscardOpen: false };
  const reads = [], puts = [], writes = [], frames = [], elements = new Map();
  let pendingDiscard = null, refresh = async () => {};
  const document = { activeElement: null, getElementById: (id) => elements.get(id) ?? null };
  class Element {
    constructor({ disabled = false, inert = false, visible = true, connected = true } = {}) {
      Object.assign(this, { disabled, inert, visible, isConnected: connected, focuses: 0 });
    }
    closest() { return this.inert ? {} : null; }
    matches() { return this.disabled; }
    getClientRects() { return this.visible ? [{}] : []; }
    focus() { this.focuses++; document.activeElement = this; }
  }
  const opener = new Element();
  document.activeElement = opener;
  const dialogElement = new Element();
  Object.assign(dialogElement, { open: false, opens: 0, closes: 0, children: [],
    showModal() { this.open = true; this.opens++; }, close() { this.open = false; this.closes++; },
    querySelectorAll() { return this.children; } });
  const context = { state, document, HTMLElement: Element,
    window: { requestAnimationFrame: (action) => frames.push(action) },
    assignmentId: "assignment", assignment: { max_score: 100 },
    assignmentRequestGenerationRef: { current: 0 }, gradeRequestGenerationRef: { current: 0 },
    gradeTargetRef: { current: null }, loadedGradeRef: { current: null }, gradeSaveRef: { current: null },
    gradeDialogRef: { current: dialogElement }, gradeReturnFocusRef: { current: null }, gradeReturnTargetRef: { current: null },
    initialGradeSnapshotRef: { current: null },
    EDUCATION_API: "/api/learn/education", ApiError: class ApiError extends Error {},
    t: (key) => key, tc: (key) => key,
    isRichContentEmpty: (value) => !value.replace(/<[^>]*>/g, "").trim(),
    requiresRichContentEditorReview: () => false, legacyRichContentToHtml: (value) => value,
    loadAssignment: async () => { h.refreshCount++; await refresh(); },
    requestDiscard: (action) => {
      const snapshot = JSON.stringify({ score: state.score, feedback: state.feedback, gradeStatus: state.gradeStatus });
      if (state.gradeDetail && context.initialGradeSnapshotRef.current !== snapshot) {
        pendingDiscard = action; state.confirmDiscardOpen = true;
      } else action();
    },
    getAssignmentSubmissionForGrading: (assignmentId, studentId) => {
      const request = { assignmentId, studentId, ...deferred() }; reads.push(request); return request.promise;
    },
    apiFetch: (pathname, options) => {
      const request = { pathname, options, ...deferred() }; puts.push(request); return request.promise;
    },
  };
  for (const key of Object.keys(state)) context[`set${key[0].toUpperCase()}${key.slice(1)}`] = (value) => {
    state[key] = value; writes.push(key);
  };
  const names = ["restoreGradeFocus", "loadGrade", "openGrade", "discardGradeDraft", "closeGrade", "cancelGrade", "trapGradeFocus", "saveGrade"];
  const program = `globalThis.bind = (gradeTarget, gradeDetail, score, feedback, gradeStatus, savingGrade, confirmDiscardOpen) => {
    const gradeOpen = gradeTarget !== null; const showEdit = false;
    ${names.map((name) => `const ${name} = ${callback(name)};`).join("\n")}
    const dialogLifecycle = ${effect("dialog.showModal()")};
    const assignmentLifecycle = ${effect("assignmentRequestGenerationRef.current += 1")};
    return { ${names.join(", ")}, dialogLifecycle, assignmentLifecycle, backgroundDisabled: ${disabledExpression} };
  };`;
  vm.runInNewContext(ts.transpileModule(program, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  } }).outputText, context, { timeout: 1000 });
  const render = () => context.bind(state.gradeTarget, state.gradeDetail, state.score, state.feedback,
    state.gradeStatus, state.savingGrade, state.confirmDiscardOpen);
  const h = { state, context, reads, puts, writes, elements, opener, dialogElement, Element, render, refreshCount: 0,
    flushFrames() { while (frames.length) frames.shift()(); },
    edit(score = "42", feedback = "<p>unsaved feedback</p>", gradeStatus = "published") {
      Object.assign(state, { score, feedback, gradeStatus });
    },
    cancelDiscard() { pendingDiscard = null; state.confirmDiscardOpen = false; },
    confirmDiscard() { const action = pendingDiscard; pendingDiscard = null; state.confirmDiscardOpen = false; action?.(); },
    setRefresh(action) { refresh = action; },
    async ready(id = "A") {
      render().openGrade(submission(id)); reads.at(-1).resolve(detail(id)); await tick();
    },
    forceSelection(id, pendingSave = null) {
      const target = submission(id), loaded = detail(id);
      context.gradeRequestGenerationRef.current++;
      context.gradeTargetRef.current = target; context.loadedGradeRef.current = loaded;
      context.gradeSaveRef.current = pendingSave;
      Object.assign(state, { gradeTarget: target, gradeDetail: loaded, score: "88", feedback: "B draft",
        gradeStatus: "draft", savingGrade: Boolean(pendingSave), gradeError: null });
    },
  };
  h.unmount = render().assignmentLifecycle();
  h.refreshCount = 0;
  return h;
}

test("native modal and disabled background buttons are wired to the product handlers", async () => {
  assert.equal(dialog.length, 1);
  const attrs = dialog[0].attributes.getText(ast);
  assert.match(attrs, /onCancel=\{cancelGrade\}/);
  assert.match(attrs, /onKeyDown=\{trapGradeFocus\}/);
  const h = harness(); assert.equal(h.render().backgroundDisabled, false);
  await h.ready(); assert.equal(h.render().backgroundDisabled, true);
  const cleanup = h.render().dialogLifecycle();
  assert.equal(h.dialogElement.opens, 1);
  h.render().dialogLifecycle(); assert.equal(h.dialogElement.opens, 1);
  h.render().discardGradeDraft(); cleanup(); h.flushFrames();
  assert.equal(h.dialogElement.closes, 1); assert.equal(h.opener.focuses, 1);
});

test("Tab and ShiftTab wrap only enabled visible non-inert modal controls", () => {
  const h = harness(), first = new h.Element(), last = new h.Element();
  const ignored = [new h.Element({ disabled: true }), new h.Element({ inert: true }), new h.Element({ visible: false })];
  h.dialogElement.children = [...ignored, first, last];
  function tab(active, shiftKey) {
    h.context.document.activeElement = active;
    let prevented = false;
    h.render().trapGradeFocus({ key: "Tab", shiftKey, currentTarget: h.dialogElement, preventDefault() { prevented = true; } });
    return prevented;
  }
  assert.equal(tab(first, true), true); assert.equal(h.context.document.activeElement, last);
  assert.equal(tab(last, false), true); assert.equal(h.context.document.activeElement, first);
  assert.equal(tab(h.dialogElement, true), true); assert.equal(h.context.document.activeElement, last);
  assert.equal(tab(first, false), false);
  h.dialogElement.children = ignored;
  assert.equal(tab(last, false), true); assert.equal(h.context.document.activeElement, h.dialogElement);
});

for (const action of ["close", "Escape"]) {
  test(`dirty ${action} requests confirmation, cancellation preserves draft, discard closes`, async () => {
    const h = harness(); await h.ready(); h.edit();
    if (action === "close") h.render().closeGrade();
    else {
      let prevented = false;
      h.render().cancelGrade({ preventDefault() { prevented = true; } });
      assert.equal(prevented, true, "native Escape must not auto-close");
    }
    assert.equal(h.state.confirmDiscardOpen, true);
    h.cancelDiscard(); assert.equal(h.state.gradeTarget.student_id, "A");
    assert.equal(h.state.score, "42"); assert.equal(h.state.feedback, "<p>unsaved feedback</p>");
    h.render().closeGrade(); h.confirmDiscard();
    assert.equal(h.state.gradeTarget, null); assert.equal(h.context.loadedGradeRef.current, null);
    assert.equal(h.puts.length, 0);
  });
}

test("a stale target-selection callback still requires dirty discard confirmation", async () => {
  const h = harness(); await h.ready(); h.edit();
  h.render().openGrade(submission("B"));
  assert.equal(h.reads.length, 1); assert.equal(h.state.confirmDiscardOpen, true);
  h.cancelDiscard(); assert.equal(h.state.gradeTarget.student_id, "A"); assert.equal(h.state.score, "42");
  h.render().openGrade(submission("B")); h.confirmDiscard();
  assert.equal(h.state.gradeTarget.student_id, "B"); assert.equal(h.state.gradeDetail, null);
  h.reads[1].resolve(detail("B")); await tick(); assert.equal(h.state.score, "17");
});

for (const outcome of ["success", "failure"]) {
  test(`reversed GET ${outcome} cannot alter the current target, inputs, error, or loading`, async () => {
    const h = harness(); h.render().openGrade(submission("A")); h.render().openGrade(submission("B"));
    if (outcome === "success") h.reads[0].resolve(detail("A")); else h.reads[0].reject(new Error("late A"));
    await tick();
    assert.equal(h.state.gradeTarget.student_id, "B"); assert.equal(h.state.gradeDetail, null);
    assert.equal(h.state.gradeError, null); assert.equal(h.state.gradeLoading, true);
    h.reads[1].resolve(detail("B")); await tick();
    assert.equal(h.state.gradeDetail.submission.student_id, "B"); assert.equal(h.state.gradeLoading, false);
  });
}

test("closing a pending GET invalidates it and old loaded callbacks cannot save during reload", async () => {
  const h = harness(); await h.ready(); const old = h.render();
  old.openGrade(submission("A")); await old.saveGrade(); assert.equal(h.puts.length, 0);
  h.render().closeGrade(); h.render().openGrade(submission("B"));
  h.reads[1].resolve(detail("A")); await tick(); assert.equal(h.state.gradeDetail, null);
  h.reads[2].resolve(detail("B")); await tick(); assert.equal(h.state.gradeDetail.submission.student_id, "B");
});

test("synchronous saving lock blocks duplicate save, close, Escape, discard and target changes", async () => {
  const h = harness(); await h.ready(); h.edit(); const old = h.render(); const pending = old.saveGrade();
  await old.saveGrade(); old.closeGrade(); old.cancelGrade({ preventDefault() {} });
  old.openGrade(submission("B")); old.discardGradeDraft(); await old.loadGrade(submission("B"));
  assert.equal(h.puts.length, 1); assert.equal(h.reads.length, 1); assert.equal(h.state.confirmDiscardOpen, false);
  assert.equal(h.state.gradeTarget.student_id, "A"); assert.equal(h.state.savingGrade, true);
  h.puts[0].reject(new h.context.ApiError("save failed")); await pending;
  assert.equal(h.state.savingGrade, false); assert.equal(h.context.gradeSaveRef.current, null);
});

test("failed PUT preserves target, score, feedback, status and dirty baseline for retry", async () => {
  const h = harness(); await h.ready(); h.edit(); const baseline = h.context.initialGradeSnapshotRef.current;
  const pending = h.render().saveGrade(); h.puts[0].reject(new h.context.ApiError("retryable failure")); await pending;
  assert.equal(h.state.gradeError, "retryable failure"); assert.equal(h.state.gradeTarget.student_id, "A");
  assert.equal(h.state.score, "42"); assert.equal(h.state.feedback, "<p>unsaved feedback</p>");
  assert.equal(h.state.gradeStatus, "published"); assert.equal(h.context.initialGradeSnapshotRef.current, baseline);
  const retry = h.render().saveGrade();
  assert.equal(h.puts[1].pathname, "/api/learn/education/assignments/assignment/submissions/A/grade");
  assert.deepEqual(JSON.parse(JSON.stringify(h.puts[1].options.body)), {
    score: 42, feedback_markdown: "<p>unsaved feedback</p>", status: "published",
  });
  h.puts[1].resolve({}); await retry; assert.equal(h.state.gradeTarget, null); assert.equal(h.refreshCount, 1);
});

for (const outcome of ["success", "failure"]) {
  test(`late PUT ${outcome} cannot change a newer target or release its saving lock`, async () => {
    const h = harness(); await h.ready(); h.edit(); const pending = h.render().saveGrade();
    const newer = { targetId: "submission-B", generation: h.context.gradeRequestGenerationRef.current + 1 };
    h.forceSelection("B", newer); const before = h.writes.length;
    if (outcome === "success") h.puts[0].resolve({}); else h.puts[0].reject(new h.context.ApiError("old A failure"));
    await pending;
    assert.equal(h.writes.length, before); assert.equal(h.state.gradeTarget.student_id, "B");
    assert.equal(h.state.score, "88"); assert.equal(h.state.feedback, "B draft");
    assert.equal(h.state.gradeError, null); assert.equal(h.context.gradeSaveRef.current, newer);
    assert.equal(h.state.savingGrade, true); assert.equal(h.refreshCount, 0);
    assert.match(h.puts[0].pathname, /\/submissions\/A\/grade$/);
    assert.equal(h.puts[0].options.body.score, 42);
  });
}

for (const outcome of ["success", "failure"]) {
  test(`unmount prevents pending PUT ${outcome} from writing state or refreshing`, async () => {
    const h = harness(); await h.ready(); h.edit(); const pending = h.render().saveGrade();
    h.unmount(); const before = h.writes.length;
    if (outcome === "success") h.puts[0].resolve({}); else h.puts[0].reject(new Error("unmounted"));
    await pending; assert.equal(h.writes.length, before); assert.equal(h.refreshCount, 0);
  });
}

for (const score of ["", " ", "\t\n", "1.5", "-1", "101", "NaN"]) {
  test(`invalid score ${JSON.stringify(score)} never sends a PUT`, async () => {
    const h = harness(); await h.ready(); h.edit(score); await h.render().saveGrade();
    assert.equal(h.puts.length, 0); assert.equal(h.state.gradeError, "grading.invalidScore");
    assert.equal(h.state.gradeTarget.student_id, "A"); assert.equal(h.state.score, score);
  });
}
for (const score of ["0", "100"]) {
  test(`explicit integer boundary ${score} remains valid`, async () => {
    const h = harness(); await h.ready(); h.edit(score); const pending = h.render().saveGrade();
    assert.equal(h.puts.length, 1); assert.equal(h.puts[0].options.body.score, Number(score));
    h.puts[0].resolve({}); await pending; assert.equal(h.state.gradeTarget, null);
  });
}

test("successful save restores the same target after refresh replaces its original button", async () => {
  const h = harness(); await h.ready(); h.edit(); h.opener.isConnected = false;
  const refresh = deferred(); h.setRefresh(() => refresh.promise);
  const pending = h.render().saveGrade(); h.puts[0].resolve({}); await tick();
  h.render().restoreGradeFocus(); h.flushFrames();
  assert.equal(h.context.gradeReturnTargetRef.current, "submission-A", "loading skeleton must retain restoration target");
  const replacement = new h.Element(); h.elements.set("education-grade-target-submission-A", replacement);
  refresh.resolve(); await pending; h.flushFrames();
  assert.equal(replacement.focuses, 1); assert.equal(h.context.gradeReturnTargetRef.current, null);
});

for (const fallback of ["teaching-assignment-title", "teaching-assignment-error"]) {
  test(`focus falls back to ${fallback} if the submitted row is absent`, async () => {
    const h = harness(); await h.ready(); h.edit(); h.opener.isConnected = false;
    const target = new h.Element(); h.elements.set(fallback, target);
    const pending = h.render().saveGrade(); h.puts[0].resolve({}); await pending; h.flushFrames();
    assert.equal(target.focuses, 1);
  });
}

test("a late refresh cannot steal focus from a newer grade dialog", async () => {
  const h = harness(); await h.ready(); h.edit(); const refresh = deferred(); h.setRefresh(() => refresh.promise);
  const pending = h.render().saveGrade(); h.puts[0].resolve({}); await tick();
  await h.ready("B"); const target = new h.Element(); h.elements.set("education-grade-target-submission-A", target);
  refresh.resolve(); await pending; h.flushFrames(); assert.equal(target.focuses, 0);
});
