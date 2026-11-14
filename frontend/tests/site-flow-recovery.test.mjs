import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { renderToStaticMarkup } from "react-dom/server";
import { Fragment } from "react";
import { BookOpen, GraduationCap, RefreshCw } from "lucide-react";
import ts from "typescript";

const require = createRequire(import.meta.url);
function product(relative) {
  const source = readFileSync(new URL(relative, import.meta.url), "utf8");
  const syntax = ts.createSourceFile(relative, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  assert.equal(syntax.parseDiagnostics.length, 0);
  function find(predicate) {
    const nodes = [];
    function visit(node) { if (predicate(node)) nodes.push(node); ts.forEachChild(node, visit); }
    visit(syntax);
    assert.equal(nodes.length, 1, "a production callback must be uniquely identified");
    return nodes[0];
  }
  return {
    source, syntax, find,
    callback(name) {
      const value = find((node) => ts.isVariableDeclaration(node) && node.name.getText(syntax) === name).initializer;
      return (ts.isCallExpression(value) ? value.arguments[0] : value).getText(syntax);
    },
    effect(marker) {
      return find((node) => ts.isCallExpression(node) && node.expression.getText(syntax) === "useEffect"
        && node.arguments[0].getText(syntax).includes(marker)).arguments[0].getText(syntax);
    },
  };
}
function execute(source, context) {
  const program = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  vm.runInNewContext(program, context, { timeout: 1000 });
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("the actual Next rewrite connects all moderation endpoints to Community", async () => {
  const source = readFileSync(new URL("../next.config.ts", import.meta.url), "utf8");
  const context = { exports: {}, process: { env: { COMMUNITY_HOST: "community.fixture", COMMUNITY_PORT: "18301" }, cwd: () => "/fixture/frontend" },
    require: (name) => { assert.equal(name, "next-intl/plugin"); return { default: () => (config) => config }; } };
  execute(source, context);
  const rewrites = await context.exports.default.rewrites();
  const moderation = rewrites.find((item) => item.source === "/api/community/moderation/:path*");
  assert.equal(moderation?.destination, "http://community.fixture:18301/moderation/:path*");
});

for (const [page, restore, remove, refresh] of [
  ["courses", "handleRestoreCourse", "handleDeleteCourse", "fetchCourses"],
  ["competitions", "handleRestore", "handleDelete", "fetchCompetitions"],
]) {
  const code = product(`../src/app/[locale]/admin/${page}/page.tsx`);
  for (const isSuperAdmin of [false, true]) {
    test(`${page} destructive handlers ${isSuperAdmin ? "retain the admin flow" : "make zero requests for moderator"}`, async () => {
      const calls = [], writes = [];
      const context = { isSuperAdmin, deleteTarget: { id: "fixture", name: "Fixture" }, ApiError: Error,
        apiFetch: async (path, options = {}) => { calls.push({ path, method: options.method ?? "GET" }); return { confirmation_token: "fixture-only" }; },
        toast: () => {}, t: (key) => key, tc: (key) => key, [refresh]: async () => {} };
      for (const state of ["ActionLoading", "PublishLoading", "DeleteTarget", "DeleteImpact", "DeleteImpactLoading", "DeleteSubmitting"]) {
        context[`set${state}`] = (value) => writes.push([state, value]);
      }
      execute(`globalThis.actions = { restore: ${code.callback(restore)}, impact: ${code.callback("openDeletionDialog")}, remove: ${code.callback(remove)} };`, context);
      await context.actions.restore("fixture");
      await context.actions.impact({ id: "fixture", title: "Fixture" });
      await context.actions.remove({ reason: "fixture reason", targetName: "Fixture" });
      assert.deepEqual(calls.map((call) => call.method), isSuperAdmin ? ["POST", "GET", "POST", "DELETE"] : []);
      if (!isSuperAdmin) assert.equal(writes.length, 0);
    });
  }
}

const lessonCode = product("../src/app/[locale]/learn/[courseId]/[lessonId]/page.tsx");
function lessonHarness() {
  const calls = [];
  const context = { AbortController, ApiError: Error, identityRef: { current: "" }, generationRef: { current: 0 },
    completionController: { current: null }, tc: (key) => key, loadedIdentity: "", lesson: null, completed: false };
  for (const state of ["LoadedIdentity", "Loading", "Error", "Lesson", "Siblings", "Completed", "Completing"]) {
    const key = state[0].toLowerCase() + state.slice(1);
    context[`set${state}`] = (value) => { context[key] = value; };
  }
  context.apiFetch = (path, options = {}) => { const pending = deferred(); calls.push({ path, options, ...pending }); return pending.promise; };
  execute(`globalThis.bind = (identity, courseId, lessonId, loadedIdentity, lesson, completed) => ({ load: ${lessonCode.effect("apiFetch<LessonResponse>")}, complete: ${lessonCode.callback("handleComplete")} });`, context);
  let identity, lessonId, cleanup;
  function callbacks() { return context.bind(identity, "course", lessonId, context.loadedIdentity, context.lesson, context.completed); }
  return { context, calls,
    render(id, user = "buyer") {
      identity = `course:${id}:${user}`;
      lessonId = id;
      context.identityRef.current = identity;
      cleanup?.();
      cleanup = callbacks().load();
    },
    complete: () => callbacks().complete(),
    unmount: () => cleanup?.(),
  };
}
const lessonData = (id) => ({ lesson: { id, completed: false }, siblings: { prev: null, next: null } });

for (const outcome of ["success", "failure"]) {
  test(`late lesson A GET ${outcome} cannot replace B or clear its loading state`, async () => {
    const h = lessonHarness();
    h.render("A");
    h.render("B");
    if (outcome === "success") h.calls[0].resolve(lessonData("A"));
    else h.calls[0].reject(new Error("old failure"));
    await tick();
    assert.equal(h.context.lesson, null);
    assert.equal(h.context.loading, true);
    assert.equal(h.context.error, null);
    h.calls[1].resolve(lessonData("B"));
    await tick();
    assert.equal(h.context.lesson.id, "B");
    assert.equal(h.context.loading, false);
  });
}

test("late completion of A cannot mark B complete or release B's active request", async () => {
  const h = lessonHarness();
  h.render("A"); h.calls[0].resolve(lessonData("A")); await tick();
  const first = h.complete();
  assert.equal(h.calls[1].path, "/api/learn/progress/A/complete");
  await h.complete();
  assert.equal(h.calls.length, 2, "the completion gate must hold before React rerenders");
  h.render("B"); h.calls[2].resolve(lessonData("B")); await tick();
  const second = h.complete();
  h.calls[1].resolve({}); await first;
  assert.equal(h.context.completed, false);
  assert.equal(h.context.completing, true);
  assert.ok(h.context.completionController.current);
  h.calls[3].resolve({}); await second;
  assert.equal(h.context.completed, true);
  assert.equal(h.context.completing, false);
});

test("account change and unmount discard prior lesson completion errors and results", async () => {
  const h = lessonHarness();
  h.render("A"); h.calls[0].resolve(lessonData("A")); await tick();
  const pending = h.complete();
  h.render("A", "another-buyer");
  h.calls[1].reject(new Error("old account failure")); await pending;
  assert.equal(h.context.error, null);
  assert.equal(h.context.completed, false);
  h.unmount(); h.calls[2].resolve(lessonData("A")); await tick();
  assert.equal(h.context.lesson, null);
});

test("current lesson failures remain errors and a later load can recover", async () => {
  const h = lessonHarness(); h.render("A");
  h.calls[0].reject(new Error("current failure")); await tick();
  assert.equal(h.context.error, "current failure"); assert.equal(h.context.loading, false);
  h.render("A"); h.calls[1].resolve(lessonData("A")); await tick();
  assert.equal(h.context.error, null); assert.equal(h.context.lesson.id, "A");
  const pending = h.complete(); h.calls[2].reject(new Error("complete failure")); await pending;
  assert.equal(h.context.completed, false); assert.equal(h.context.completing, false);
  assert.equal(h.context.completionController.current, null);
});

const accessCode = product("../src/components/teaching/EducationAccessProvider.tsx");
test("education permission effect cleanup cancels its owned refresh timer and invalidates pending work", () => {
  const timers = new Map();
  let calls = 0;
  const context = { requestGenerationRef: { current: 4 }, refresh: () => { calls += 1; }, window: {
    setTimeout: (callback) => { timers.set(1, callback); return 1; }, clearTimeout: (id) => timers.delete(id),
  } };
  execute(`globalThis.effect = ${accessCode.effect("window.setTimeout")};`, context);
  const cleanup = context.effect();
  assert.equal(timers.size, 1); assert.equal(calls, 0);
  cleanup();
  assert.equal(timers.size, 0); assert.equal(context.requestGenerationRef.current, 5);
});

test("education permission snapshots hide immediately on account change and ignore late old results", async () => {
  const requests = [];
  const context = { ApiError: Error, identityRef: { current: "authenticated:professor" }, requestGenerationRef: { current: 0 },
    snapshot: { identity: "authenticated:professor", permissions: { is_education_manager: true }, loading: false, errorStatus: null },
    getEducationPermissions: () => { const pending = deferred(); requests.push(pending); return pending.promise; } };
  context.setSnapshot = (value) => { context.snapshot = value; };
  execute(`globalThis.bind = (identity, sessionStatus) => { const refresh = ${accessCode.callback("refresh")}; return { refresh, value: ${accessCode.callback("value")} }; };`, context);
  const professor = context.bind("authenticated:professor", "authenticated");
  const first = professor.refresh();
  context.identityRef.current = "authenticated:student";
  const student = context.bind("authenticated:student", "authenticated");
  assert.equal(student.value().permissions, null);
  assert.equal(student.value().loading, true);
  const second = student.refresh();
  requests[0].resolve({ is_education_manager: true }); await first;
  assert.equal(context.snapshot.identity, "authenticated:student");
  assert.equal(context.snapshot.permissions, null);
  requests[1].resolve({ is_education_manager: false }); await second;
  assert.equal(student.value().permissions.is_education_manager, false);
  await professor.refresh();
  assert.equal(requests.length, 2, "a stale refresh callback must not invalidate a current account request");
});

test("education permission errors remain fail-closed and can be retried", async () => {
  const context = { ApiError: class ApiError extends Error {}, identityRef: { current: "authenticated:student" }, requestGenerationRef: { current: 0 }, snapshot: {},
    getEducationPermissions: async () => { throw new TypeError("offline"); } };
  context.setSnapshot = (value) => { context.snapshot = value; };
  execute(`globalThis.refresh = (${accessCode.callback("refresh")});`, Object.assign(context, { identity: "authenticated:student", sessionStatus: "authenticated" }));
  await context.refresh();
  assert.equal(context.snapshot.permissions, null); assert.equal(context.snapshot.errorStatus, 0);
  context.getEducationPermissions = async () => ({ is_education_manager: true });
  await context.refresh();
  assert.equal(context.snapshot.permissions.is_education_manager, true); assert.equal(context.snapshot.errorStatus, null);
});

const entryCode = product("../src/components/learn/LearningEntryNavigation.tsx");
test("learning entry links require authentication and confirmed education permission, never a role guess", () => {
  const node = entryCode.find((candidate) => ts.isFunctionDeclaration(candidate) && candidate.name?.text === "LearningEntryLinks");
  const context = { exports: {}, require, BookOpen, GraduationCap, RefreshCw, Link: "a", useTranslations: () => (key) => key };
  context.useAuth = () => context.auth;
  context.useEducationAccess = () => context.access;
  execute(`${node.getText(entryCode.syntax)}; globalThis.render = LearningEntryLinks;`, context);
  for (const [authenticated, permission, loading, errorStatus] of [[false, false, false, null], [true, false, false, null], [true, true, false, null], [true, true, true, null], [true, true, false, 503]]) {
    context.auth = { user: authenticated ? { id: "fixture", role: "professor" } : null, sessionStatus: authenticated ? "authenticated" : "anonymous" };
    context.access = { permissions: { is_education_manager: permission }, loading, errorStatus, refresh: async () => {} };
    const markup = renderToStaticMarkup(context.render());
    assert.equal(markup.includes('href="/learn/classes"'), authenticated);
    assert.equal(markup.includes('href="/teaching"'), authenticated && permission && !loading && errorStatus === null);
  }
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const messages = JSON.parse(readFileSync(new URL(`../messages/${locale}.json`, import.meta.url), "utf8"));
    assert.ok(messages.education.nav.dashboard); assert.ok(messages.education.studentClasses.title); assert.ok(messages.teams.title);
  }
});

test("the main subtree remounts only for changed account owners and preserves unknown-account drafts", () => {
  const code = product("../src/components/layout/MainLayout.tsx");
  const node = code.find((candidate) => ts.isFunctionDeclaration(candidate) && candidate.name?.text === "MainLayout");
  const context = { exports: {}, require, Header: "header", Footer: "footer", useAuth: () => context.auth };
  execute(`${node.getText(code.syntax)}; globalThis.render = MainLayout;`, context);
  const key = (auth) => { context.auth = auth; return context.render({ children: "account draft" }).props.children[1].key; };
  assert.equal(key({ user: { id: "A" }, sessionStatus: "authenticated" }), "A");
  assert.equal(key({ user: { id: "A" }, sessionStatus: "unknown", loading: true }), "A");
  assert.equal(key({ user: { id: "B" }, sessionStatus: "authenticated" }), "B");
  assert.equal(key({ user: null, sessionStatus: "anonymous" }), "anonymous");
});

test("account content hides private data on logout and never treats unknown as a confirmed login", () => {
  const code = product("../src/components/layout/AccountContentBoundary.tsx");
  const node = code.find((candidate) => ts.isFunctionDeclaration(candidate) && candidate.name?.text === "AccountContentBoundary");
  const context = { exports: {}, require, Fragment, Link: "a", useAuth: () => context.auth, useTranslations: () => (key) => key };
  execute(`${node.getText(code.syntax)}; globalThis.render = AccountContentBoundary;`, context);
  const markup = (auth) => { context.auth = { refresh: async () => {}, ...auth }; return renderToStaticMarkup(context.render({ children: "PRIVATE ACCOUNT DATA" })); };
  assert.ok(markup({ user: { id: "A" }, sessionStatus: "authenticated", loading: false }).includes("PRIVATE ACCOUNT DATA"));
  assert.ok(markup({ user: { id: "A" }, sessionStatus: "unknown", loading: true }).includes("PRIVATE ACCOUNT DATA"));
  assert.ok(!markup({ user: null, sessionStatus: "anonymous", loading: false }).includes("PRIVATE ACCOUNT DATA"));
  assert.ok(!markup({ user: null, sessionStatus: "unknown", loading: false }).includes("PRIVATE ACCOUNT DATA"));
  assert.ok(markup({ user: null, sessionStatus: "unknown", loading: false }).includes("retry"));
});

test("a departed profile save cannot continue from an avatar response into another account's profile PATCH", async () => {
  const code = product("../src/app/[locale]/settings/profile/page.tsx");
  for (const deleting of [true, false]) {
    const pending = deferred(), calls = [];
    const context = { userId: "A", saving: false, hasChanges: true, ownerGeneration: { current: 1 }, pendingAvatarDelete: deleting,
      pendingAvatarFile: deleting ? null : new Blob(["fixture"]), pendingAvatarPreview: null,
      username: "changed", originalUsername: "original", bio: "new bio", originalBio: "old bio", FormData,
      ApiError: Error, t: (key) => key, URL: { revokeObjectURL: () => {} },
      captureApiRequestOwner: () => ({ ownerId: "A" }), isCurrentApiRequestOwner: () => true };
    for (const state of ["Saving", "Message", "AvatarUrl", "OriginalAvatarUrl", "PendingAvatarDelete", "PendingAvatarFile", "PendingAvatarPreview", "OriginalUsername", "OriginalBio"]) context[`set${state}`] = () => {};
    context.apiFetch = (path, options) => { calls.push({ path, method: options?.method }); return pending.promise; };
    context.apiFormFetch = (path) => { calls.push({ path, method: "POST" }); return pending.promise; };
    execute(`globalThis.save = ${code.callback("handleSave")};`, context);
    const saving = context.save();
    const layout = code.find((node) => ts.isCallExpression(node) && node.expression.getText(code.syntax) === "useLayoutEffect");
    execute(`globalThis.layout = ${layout.arguments[0].getText(code.syntax)};`, context);
    context.layout()();
    pending.resolve({ avatar_url: "fixture/avatar.png" }); await saving;
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, "/api/auth/me/avatar");
  }
});

for (const outcome of ["success", "failure"]) {
  test(`late ranking tab ${outcome} does not replace the current tab or clear its loading`, async () => {
    const code = product("../src/app/[locale]/ranking/page.tsx");
    const requests = [];
    const context = { AbortController, ApiError: Error, requestGeneration: { current: 0 }, requestController: { current: null }, t: (key) => key,
      setLoading: (value) => { context.loading = value; }, setError: (value) => { context.error = value; }, setRankings: (value) => { context.rankings = value; },
      apiFetch: () => { const pending = deferred(); requests.push(pending); return pending.promise; } };
    execute(`globalThis.load = ${code.callback("fetchRankings")};`, context);
    context.load("overall"); context.load("web");
    if (outcome === "success") requests[0].resolve([{ username: "wrong tab" }]); else requests[0].reject(new Error("stale failure"));
    await tick(); assert.equal(context.loading, true); assert.equal(context.error, null); assert.equal(context.rankings, undefined);
    requests[1].resolve([{ username: "current tab" }]); await tick();
    assert.equal(context.rankings[0].username, "current tab"); assert.equal(context.loading, false);
  });
}
