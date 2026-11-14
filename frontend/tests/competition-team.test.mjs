import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url), React = require("react");
const root = fileURLToPath(new URL("../", import.meta.url));
function loadModule(relative, dependencies = {}) {
  const context = { exports: {}, URL, process: { env: { NEXT_PUBLIC_BASE_PATH: "/ctf" } }, require: (name) => dependencies[name] ?? require(name) };
  vm.runInNewContext(ts.transpileModule(readFileSync(path.join(root, relative), "utf8"), { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, context);
  return context.exports;
}
const presentation = loadModule("src/lib/competition-presentation.ts");
const attachments = loadModule("src/lib/challenge-attachment-download.ts");
const COMP = "11111111-1111-4111-8111-111111111111", OTHER = "22222222-2222-4222-8222-222222222222";
const USER = "33333333-3333-4333-8333-333333333333", TEAM = "44444444-4444-4444-8444-444444444444";
const TEAM2 = "55555555-5555-4555-8555-555555555555", ASSET = "66666666-6666-4666-8666-666666666666";
const ownerKey = `${COMP}:${USER}:authenticated`;
const ownTeam = { id: TEAM, name: "Team snapshot", captain_id: USER, member_count: 2 };
const competition = (extra = {}) => ({ id: COMP, title: "Competition", description: "", mode: "team", team_contract: "fixed_roster_v1",
  status: "ongoing", joined: false, participant_count: 0, challenges: [], my_challenges: [], assignment: { state: "not_joined", categories: [] }, ...extra });
const challenge = { id: 21, name: "Challenge", category: "Web", value: 100, solved: false, team_solved: true, env_type: "none" };
const flush = async () => { for (let index = 0; index < 24; index++) await Promise.resolve(); };
const plain = (value) => JSON.parse(JSON.stringify(value));
function elements(node) {
  if (Array.isArray(node)) return node.flatMap(elements);
  return React.isValidElement(node) ? [node, ...elements(node.props.children)] : [];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join("");
  if (React.isValidElement(node)) return text(node.props.children);
  return typeof node === "string" || typeof node === "number" ? String(node) : "";
}
class ApiError extends Error {}
function harness(kind = "detail", locale = "ko", overrides = {}) {
  const relative = kind === "admin" ? "src/app/[locale]/admin/competitions/[id]/page.tsx" : kind === "graph" ? "src/components/ctf/ScoreboardGraph.tsx"
    : kind === "scoreboard" ? "src/app/[locale]/ctf/[id]/scoreboard/page.tsx" : "src/app/[locale]/ctf/[id]/page.tsx";
  const source = readFileSync(path.join(root, relative), "utf8"), ast = ts.createSourceFile(relative, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX), names = [];
  assert.equal(ast.parseDiagnostics.length, 0);
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isArrayBindingPattern(node.name) && node.initializer && ts.isCallExpression(node.initializer)
      && node.initializer.expression.getText(ast) === "useState") names.push(node.name.elements[0].getText(ast));
    ts.forEachChild(node, visit);
  }
  visit(ast);
  const values = new Map(), refs = [], callbacks = [], memos = [], effects = [], translations = new Map(), calls = [];
  const messages = JSON.parse(readFileSync(path.join(root, `messages/${locale}.json`), "utf8"));
  let stateIndex = 0, refIndex = 0, callbackIndex = 0, memoIndex = 0, effectIndex = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const memo = (store, index, fn, deps) => { if (!store[index] || !same(store[index].deps, deps)) store[index] = { deps, value: fn() }; return store[index].value; };
  const h = { compId: COMP, graphContract: "fixed_roster_v1", generation: 0, owner: { user: { id: USER }, sessionStatus: "authenticated" }, values, calls, messages,
    respond: async (url) => url === "/api/auth/teams" ? [ownTeam] : competition() };
  const mockReact = { ...React, useState(initial) { const name = names[stateIndex++]; assert(name); if (!values.has(name)) values.set(name, name in overrides ? overrides[name] : typeof initial === "function" ? initial() : initial);
    return [values.get(name), (update) => values.set(name, typeof update === "function" ? update(values.get(name)) : update)]; },
    useRef(initial) { const index = refIndex++; return refs[index] ??= { current: initial }; },
    useCallback(fn, deps) { return memo(callbacks, callbackIndex++, () => fn, deps); }, useMemo(fn, deps) { return memo(memos, memoIndex++, fn, deps); },
    useEffect(fn, deps) { const index = effectIndex++; if (!effects[index] || !same(effects[index].deps, deps)) effects[index] = { fn, deps, dirty: true, cleanup: effects[index]?.cleanup }; } };
  const context = { exports: {}, URL, AbortController, process: { env: {} }, setInterval: () => 1, clearInterval() {}, require(name) {
    if (name === "react") return mockReact;
    if (name === "next/navigation") return { useParams: () => ({ id: h.compId }) };
    if (name === "next-intl") return { useTranslations(namespace) { if (!translations.has(namespace)) translations.set(namespace, (key, args = {}) => {
      let value = messages[namespace][key]; assert.equal(typeof value, "string", `${locale}:${namespace}.${key}`);
      for (const [name, replacement] of Object.entries(args)) value = value.replace(`{${name}}`, String(replacement)); return value;
    }); return translations.get(namespace); } };
    if (name === "@/providers/AuthProvider") return { useAuth: () => h.owner };
    if (name === "@/providers/ToastProvider") return { useToast: () => ({ toast() {} }) };
    if (name === "@/lib/use-image-edit-operation") return { useImageEditOperation: () => ({ operation: { busy: false }, busy: false, uploading: false, kind: null, key: "owned-operation" }) };
    if (name === "@/lib/use-unsaved-changes-guard") return { useUnsavedChangesGuard: () => ({}) };
    if (name === "@/lib/competition-presentation") return presentation;
    if (name === "@/lib/challenge-attachment-download") return attachments;
    if (name === "@/lib/api-client") return { ApiError, captureApiRequestOwner: (id) => ({ id, generation: h.generation }),
      isCurrentApiRequestOwner: (owner) => owner.id === h.owner.user?.id && owner.generation === h.generation,
      apiFetch(...args) { calls.push(args); return h.respond(...args); } };
    if (name === "@/components/ui/Button") return { Button: "button" };
    if (name === "@/i18n/navigation") return { Link: "a", useRouter: () => ({ back() {} }) };
    if (name === "@/components/ui/ConfirmModal") return { ConfirmModal: "confirm-modal" };
    if (name === "@/components/ui/RichTextEditor") return { RichTextEditor: "rich-editor" };
    if (name === "@/components/admin/competitions/BannerUploadZone") return { BannerUploadZone: "banner-zone" };
    if (name === "@/components/admin/competitions/CompetitionChallengeManager") return { CompetitionChallengeManager: "competition-challenges" };
    if (name === "@/components/wargame/FlagSubmitForm") return { FlagSubmitForm: "flag-form" };
    if (name === "@/components/wargame/EnvInstanceSection") return { EnvInstanceSection: "environment" };
    if (name === "@/components/learn/MarkdownRenderer") return { MarkdownRenderer: "markdown" };
    if (name === "@/components/ctf/ScoreboardGraph") return { ScoreboardGraph: "score-graph" };
    if (name === "recharts") return Object.fromEntries(["LineChart", "Line", "XAxis", "YAxis", "Tooltip", "Legend", "ResponsiveContainer", "CartesianGrid"].map((key) => [key, key]));
    return require(name);
  } };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText, context);
  h.render = () => { stateIndex = refIndex = callbackIndex = memoIndex = effectIndex = 0;
    return elements(kind === "graph" ? context.exports.ScoreboardGraph({ compId: h.compId, teamContract: h.graphContract }) : context.exports.default()); };
  h.commit = async () => { for (const effect of effects) if (effect.dirty) { effect.cleanup?.(); effect.dirty = false; effect.cleanup = effect.fn(); } await flush(); };
  h.settle = async () => { for (let index = 0; index < 3; index++) { h.render(); await h.commit(); } return h.render(); };
  return h;
}
const ready = (extra = {}) => ({ loadedComp: competition(), loadedOwnerKey: ownerKey, loading: false, teams: [ownTeam], selectedTeamId: TEAM, ...extra });
const button = (tree, label) => tree.find((node) => node.type === "button" && text(node) === label);

test("only exact fixed-roster markers select the new team branch and captain identities are exact", () => {
  assert.equal(presentation.isFixedRosterCompetition({ mode: "team", team_contract: null }), false);
  assert.equal(presentation.isFixedRosterCompetition({ mode: "team" }), false);
  assert.equal(presentation.isFixedRosterCompetition({ team_contract: "fixed_roster_v1" }), true);
  assert.deepEqual(plain(presentation.captainTeams([ownTeam, { ...ownTeam, id: TEAM2, captain_id: OTHER }], USER)), [ownTeam]);
});

test("team identity is UUID based, duplicate display names remain separate, and server scores are not aggregated", () => {
  const series = [{ team_id: TEAM, team_name: "Same", data: [{ t: "2026-10-18T00:00:00Z", score: 100 }] },
    { team_id: TEAM2, team_name: "Same", data: [{ t: "2026-10-18T00:01:00Z", score: 75 }] }];
  const merged = presentation.mergeCompetitionSeries(series, true), first = presentation.competitionEntryIdentity(series[0], true), second = presentation.competitionEntryIdentity(series[1], true);
  assert.notEqual(first, second); assert.equal(merged[0][first], 100); assert.equal(merged[0][second], 0); assert.equal(merged[1][first], 100); assert.equal(merged[1][second], 75);
  assert.throws(() => presentation.mergeCompetitionSeries([series[0], series[0]], true), /duplicate/);
  assert.throws(() => presentation.competitionEntryIdentity({ team_id: "__proto__" }, true), /invalid/);
  assert.equal(presentation.competitionEntryIdentity({ team_id: "abcdabcd-abcd-4abc-8abc-abcdabcdabcd" }, true), presentation.competitionEntryIdentity({ team_id: "ABCDABCD-ABCD-4ABC-8ABC-ABCDABCDABCD" }, true));
  assert.throws(() => presentation.mergeCompetitionSeries([{ ...series[0], data: [{ t: "invalid", score: 1 }] }], true), /invalid/);
  assert.notEqual(presentation.competitionEntryIdentity({ username: "time" }, false), "time");
});

test("mode guards allow unchanged saves and never promote legacy markers", () => {
  for (const status of ["upcoming", "ongoing", "ended"]) assert.equal(presentation.competitionModeChangeAllowed("team", "team", status, 10, "fixed_roster_v1"), true);
  assert.equal(presentation.competitionModeChangeAllowed("team", "individual", "upcoming", 0, "fixed_roster_v1"), false);
  assert.equal(presentation.competitionModeChangeAllowed("individual", "team", "ongoing", 0, null), false);
  assert.equal(presentation.competitionModeChangeAllowed("individual", "team", "upcoming", 1, null), false);
  assert.equal(presentation.competitionModeChangeAllowed("individual", "team", "upcoming", 0, null), true);
});

test("actual administrator mode controls preserve same-mode edits and lock forbidden conversions", () => {
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const h = harness("admin", locale, { loading: false, loadedKey: "owned-operation", mode: "team", teamContract: "fixed_roster_v1", compStatus: "upcoming",
      savedSettings: { mode: "team", startDate: "", startTime: "09:00", endDate: "", endTime: "18:00", freezeMinutes: 60 } });
    let tree = h.render(); assert.equal(button(tree, h.messages.ctf.individualMode).props.disabled, true);
    assert.equal(button(tree, h.messages.ctf.teamMode).props.disabled, false); assert.equal(button(tree, h.messages.ctf.teamMode).props["aria-pressed"], true);
    h.values.set("teamContract", null); tree = h.render(); assert.equal(button(tree, h.messages.ctf.individualMode).props.disabled, false);
    h.values.set("participantCount", 1); assert.equal(button(h.render(), h.messages.ctf.individualMode).props.disabled, true);
    h.values.set("participantCount", 0); h.values.set("compStatus", "ended"); assert.equal(button(h.render(), h.messages.ctf.individualMode).props.disabled, true);
  }
});

test("four locales render an accessible team selector with a disabled empty selection", () => {
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const h = harness("detail", locale, ready({ selectedTeamId: "" })), tree = h.render(), select = tree.find((node) => node.props.id === "competition-team");
    assert(tree.some((node) => text(node) === h.messages.ctf.registeredTeamCount.replace("{count}", "0")));
    assert(select); assert(tree.some((node) => node.type === "label" && node.props.htmlFor === select.props.id));
    assert.equal(button(tree, h.messages.ctf.join).props.disabled, true);
    select.props.onChange({ target: { value: TEAM } }); assert.equal(button(h.render(), h.messages.ctf.join).props.disabled, false); assert.equal(h.calls.length, 0);
  }
});

test("retention impact labels preserve the old four keys and localize the four team ledgers", () => {
  const source = readFileSync(path.join(root, "src/app/[locale]/admin/competitions/page.tsx"), "utf8");
  const ast = ts.createSourceFile("competitions.tsx", source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX); let expression;
  function visit(node) { if (ts.isJsxAttribute(node) && node.name.getText(ast) === "impactLabels") expression = node.initializer.expression.getText(ast); ts.forEachChild(node, visit); }
  visit(ast); assert(expression);
  const mapping = { challenge_mappings: "deletionImpactChallengeMappings", assignments: "deletionImpactAssignments", participants: "deletionImpactParticipants", solves: "deletionImpactSolves",
    registered_teams: "deletionImpactRegisteredTeams", team_members: "deletionImpactTeamMembers", team_assignments: "deletionImpactTeamAssignments", team_solves: "deletionImpactTeamSolves" };
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const messages = JSON.parse(readFileSync(path.join(root, `messages/${locale}.json`), "utf8")), context = { t: (key) => messages.admin[key] };
    vm.runInNewContext(`result = (${expression});`, context);
    assert.deepEqual(plain(context.result), Object.fromEntries(Object.entries(mapping).map(([key, label]) => [key, messages.admin[label]])));
    assert(Object.values(context.result).every((value) => typeof value === "string" && value.trim()));
  }
});

test("actual team lookup filters current captain only and failures remain errors with retry", async () => {
  const h = harness(); h.respond = async (url) => url === "/api/auth/teams" ? [ownTeam, { ...ownTeam, id: TEAM2, captain_id: OTHER }] : competition();
  await h.settle(); assert.deepEqual(plain(h.values.get("teams")), [ownTeam]); assert.equal(h.calls.filter(([url]) => url === "/api/auth/teams").length, 1);
  const failed = harness(); failed.respond = async (url) => { if (url === "/api/auth/teams") throw new ApiError("lookup failed"); return competition(); };
  const tree = await failed.settle(); assert(tree.some((node) => node.props.role === "alert")); assert(button(tree, failed.messages.common.retry));
  assert(!tree.some((node) => text(node) === failed.messages.ctf.noCaptainTeams));
});

test("initial session confirmation precedes the competition GET rather than aborting a startup API request", async () => {
  const h = harness(); h.owner = { user: null, sessionStatus: "unknown", loading: true }; h.render(); await h.commit(); assert.equal(h.calls.length, 0);
  h.owner = { user: { id: USER }, sessionStatus: "authenticated", loading: false }; h.render(); await h.commit();
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0][0], `/api/ctf/competitions/${COMP}`); assert.equal(h.calls[0][1].signal.aborted, false);
});

test("same-account profile updates preserve the chosen team without refetching or resetting registration", async () => {
  const h = harness(); await h.settle();
  h.render().find((node) => node.props.id === "competition-team").props.onChange({ target: { value: TEAM } });
  const calls = h.calls.length; h.owner = { ...h.owner, user: { id: USER, xp: 1 } }; h.render(); await h.commit();
  assert.equal(h.calls.length, calls); assert.equal(h.values.get("selectedTeamId"), TEAM);
});

test("actual join sends the selected team once, locks pending, and retains the selection on failure", async () => {
  const h = harness("detail", "ko", ready()); let reject;
  h.respond = () => new Promise((_, fail) => { reject = fail; });
  const submit = button(h.render(), h.messages.ctf.join).props.onClick, first = submit(); await flush();
  await submit(); assert.equal(h.calls.length, 1); assert.deepEqual(plain(h.calls[0][1].body), { team_id: TEAM });
  assert.equal(button(h.render(), h.messages.common.loading).props.disabled, true);
  reject(new ApiError("roster conflict")); await first; assert.equal(h.values.get("selectedTeamId"), TEAM); assert.equal(h.values.get("actionError"), "roster conflict");
});

test("individual and NULL legacy team registration keep their original body-free request", async () => {
  for (const mode of ["individual", "team"]) {
    const h = harness("detail", "ko", ready({ loadedComp: competition({ mode, team_contract: null }) }));
    h.respond = async (url) => url.endsWith("/join") ? { message: "joined" } : competition({ mode, team_contract: null, joined: true });
    await button(h.render(), h.messages.ctf.join).props.onClick(); assert.equal(h.calls[0][1].method, "POST"); assert.equal("body" in h.calls[0][1], false);
    assert(!h.render().some((node) => node.props.id === "competition-team"));
  }
});

test("fixed registration renders server snapshots and team solved never locks another member's personal flag form", () => {
  const snapshot = { team_id: TEAM, team_name: "Original name", registered_at: "2026-10-18T00:00:00Z", members: [{ user_id: USER, username: "Original member" }] };
  const h = harness("detail", "ko", ready({ loadedComp: competition({ joined: true, team_registration: snapshot, challenges: [challenge], assignment: { state: "assigned", categories: [] } }), selectedChallenge: 21 }));
  const tree = h.render(); assert(tree.some((node) => text(node) === "Original member")); assert(tree.some((node) => text(node).includes("Original name")));
  assert.equal(tree.find((node) => node.type === "flag-form").props.solved, false);
  assert(tree.some((node) => text(node).includes(h.messages.ctf.teamSolved)));
  assert(!tree.some((node) => node.props.id === "competition-team"));
});

test("registered roster readback and retry do not depend on current live captain membership", async () => {
  const registered = competition({ joined: true, team_registration: { team_id: TEAM, team_name: "Snapshot", registered_at: "2026-10-18T00:00:00Z", members: [{ user_id: USER, username: "Fixed member" }] } });
  const h = harness("detail", "ko", ready({ loadedComp: registered })); let failed = true;
  h.respond = async (url) => { assert.notEqual(url, "/api/auth/teams"); if (failed) throw new ApiError("detail temporarily unavailable"); return registered; };
  let tree = await h.settle(); assert(tree.some((node) => node.props.role === "alert") || text(tree).includes("detail temporarily unavailable"));
  failed = false; button(tree, h.messages.common.retry).props.onClick(); await flush(); tree = h.render();
  assert(tree.some((node) => text(node) === "Fixed member")); assert(h.calls.every(([url, options]) => url === `/api/ctf/competitions/${COMP}` && !options.method));
  assert(!tree.some((node) => node.props.id === "competition-team"));
});

test("a fixed roster member selects the common category without sending an arbitrary team id", async () => {
  const category = { category: "Web", assigned: false, available_count: 5 };
  const h = harness("detail", "ko", ready({ loadedComp: competition({ joined: true, assignment: { state: "select_category", categories: [category] } }) }));
  h.respond = async (url) => url.includes("select-category") ? { category: "Web", challenge_ids: [21], already_assigned: false } : competition({ joined: true, challenges: [challenge], assignment: { state: "assigned", categories: [] } });
  const select = h.render().find((node) => node.type === "button" && text(node).includes("Web")); await select.props.onClick();
  assert.equal(h.calls[0][0], `/api/ctf/competitions/${COMP}/select-category?category=Web`); assert.equal(h.calls[0][1].method, "POST"); assert.equal("body" in h.calls[0][1], false);
});

test("late join from another account cannot refresh or display that account's registration", async () => {
  const h = harness("detail", "ko", ready()); let complete; h.respond = () => new Promise((done) => { complete = done; });
  const pending = button(h.render(), h.messages.ctf.join).props.onClick(); await flush();
  h.owner = { user: { id: OTHER }, sessionStatus: "authenticated" }; h.generation += 1; h.render(); complete({ message: "joined" }); await pending;
  assert.equal(h.calls.length, 1); assert(!h.render().some((node) => node.props.id === "registered-team-title"));
});

test("route changes abort the old detail response and only the new competition may populate the page", async () => {
  const h = harness(), pending = [];
  h.respond = (url, options) => new Promise((done) => pending.push({ url, signal: options.signal, done }));
  h.render(); await h.commit(); h.compId = OTHER; h.render(); await h.commit();
  assert.equal(pending.length, 2); assert.equal(pending[0].signal.aborted, true);
  pending[0].done(competition({ title: "old" })); await flush(); assert.equal(h.values.get("loadedComp"), null);
  pending[1].done(competition({ id: OTHER, title: "new" })); await flush(); assert.equal(h.values.get("loadedComp").id, OTHER);
  assert(h.render().some((node) => node.type === "h1" && text(node) === "new"));
});

test("CTF attachments show ready and unavailable descriptors and never fall back over an explicit new list", () => {
  const base = { id: 21, name: "Challenge", description: "", hints: [], env_type: "none", environment_ready: true, environment_status: "not_required", container_port: null,
    files: [{ id: 0, name: "legacy", download_path: `/api/learn/competitions/${COMP}/challenges/21/files/0` }] };
  const make = (detail) => harness("detail", "ko", ready({ loadedComp: competition({ joined: true, challenges: [challenge], assignment: { state: "assigned", categories: [] } }), selectedChallenge: 21, challengeDetail: detail }));
  const descriptor = { id: ASSET, legacy_index: null, name: "private.png", byte_size: 70, sha256: "a".repeat(64), state: "ready", download_path: `/api/learn/competitions/${COMP}/challenges/21/attachments/${ASSET}` };
  let h = make({ ...base, attachments: [descriptor, { ...descriptor, id: TEAM, name: "missing.png", state: "unavailable", download_path: null }] }), tree = h.render();
  assert(tree.some((node) => node.type === "a" && node.props.href === `/ctf${descriptor.download_path}`));
  assert(tree.some((node) => text(node) === h.messages.ctf.attachmentUnavailable)); assert(!tree.some((node) => node.type === "a" && node.props.href.includes("/files/")));
  h = make({ ...base, attachments: [{ ...descriptor, download_path: descriptor.download_path.replace(COMP, OTHER) }] }); assert(!h.render().some((node) => node.type === "a" && node.props.download));
  h = make({ ...base, attachments: [] }); assert(!h.render().some((node) => node.type === "a" && node.props.download));
  h = make(base); assert(h.render().some((node) => node.type === "a" && node.props.href.endsWith("/files/0")));
});

test("actual team table and graph use distinct UUID identities and snapshot labels", () => {
  const rows = [TEAM, TEAM2].map((id, index) => ({ rank: index + 1, team_id: id, team_name: "Same name", score: 125 - index, solves: 1, first_bloods: 0 }));
  const h = harness("scoreboard", "ko", { entries: rows, isFrozen: false, loading: false, loadedCompId: COMP, teamContract: "fixed_roster_v1" });
  const tree = h.render(), rowNodes = tree.filter((node) => node.type === "tr" && node.key !== null);
  assert.equal(new Set(rowNodes.map((node) => node.key)).size, 2); assert(tree.some((node) => node.type === "th" && text(node) === h.messages.ctf.teamName));
  const series = rows.map((row) => ({ ...row, data: [{ t: "2026-10-18T00:00:00Z", score: row.score }] }));
  const graph = harness("graph", "ko", { series, loading: false, loadedKey: `${COMP}:fixed_roster_v1` });
  const lines = graph.render().filter((node) => node.type === "Line"); assert.equal(lines.length, 2); assert.notEqual(lines[0].props.dataKey, lines[1].props.dataKey);
  assert.equal(lines[0].props.name, "Same name"); assert.equal(lines[1].props.name, "Same name");
});

test("graph failures stay visible and retry only the same actual GET", async () => {
  const h = harness("graph"); h.respond = async () => { throw new ApiError("graph failed"); };
  let tree = await h.settle(); assert(tree.some((node) => node.props.role === "alert"));
  h.respond = async () => ({ team_contract: "fixed_roster_v1", series: [] }); button(tree, h.messages.common.retry).props.onClick(); await flush();
  tree = h.render(); assert(!tree.some((node) => node.props.role === "alert")); assert.equal(h.calls.length, 2); assert.equal(h.calls[0][0], h.calls[1][0]);
});

test("late scoreboard responses cannot replace another route and malformed team results are errors, not empty ranks", async () => {
  const h = harness("scoreboard"), pending = [];
  h.respond = (url, options) => new Promise((done) => pending.push({ signal: options.signal, done }));
  h.render(); await h.commit(); h.compId = OTHER; h.render(); await h.commit(); assert(pending[0].signal.aborted);
  pending[0].done({ team_contract: "fixed_roster_v1", is_frozen: false, entries: [{ rank: 1, team_id: TEAM, team_name: "old", score: 100, solves: 1 }] });
  await flush(); assert.equal(h.values.get("entries").length, 0);
  pending[1].done({ team_contract: "fixed_roster_v1", is_frozen: false, entries: [{ rank: 1, username: "not a team", score: 100, solves: 1 }] });
  await flush(); assert(h.render().some((node) => node.props.role === "alert"));
});

test("the owned TypeScript files pass the existing compiler in memory", () => {
  const config = ts.readConfigFile(path.join(root, "tsconfig.json"), ts.sys.readFile); assert.equal(config.error, undefined);
  const options = ts.parseJsonConfigFileContent(config.config, ts.sys, root).options;
  const files = ["src/app/[locale]/ctf/page.tsx", "src/app/[locale]/ctf/[id]/page.tsx", "src/app/[locale]/ctf/[id]/scoreboard/page.tsx",
    "src/components/ctf/ScoreboardGraph.tsx", "src/app/[locale]/admin/competitions/new/page.tsx", "src/app/[locale]/admin/competitions/[id]/page.tsx", "src/app/[locale]/admin/competitions/page.tsx",
    "src/types/competition.ts", "src/lib/competition-presentation.ts"].map((file) => path.join(root, file));
  const owned = new Set(files.map((file) => file.toLowerCase()));
  const diagnostics = ts.getPreEmitDiagnostics(ts.createProgram(files, { ...options, noEmit: true, incremental: false })).filter((item) => !item.file || owned.has(path.resolve(item.file.fileName).toLowerCase()));
  assert.equal(diagnostics.length, 0, ts.formatDiagnosticsWithColorAndContext(diagnostics, { getCurrentDirectory: () => root, getCanonicalFileName: (file) => file, getNewLine: () => "\n" }));
});
