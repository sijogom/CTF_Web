import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { AuthRequestBlockedError, createAuthRequestCoordinator } from "../src/lib/auth-request-coordinator.mjs";
import { normalizeApiErrorPayload } from "../src/lib/api-error.mjs";

const source = (relative) => readFileSync(new URL(relative, import.meta.url), "utf8");
function compile(text, context) {
  vm.runInNewContext(ts.transpileModule(text, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, context, { timeout: 1000 });
  return context.exports;
}
const download = compile(source("../src/lib/challenge-attachment-download.ts"), {
  exports: {}, process: { env: {} },
});
const asset = "978c473c-5a76-4b2d-bf75-c93c2ee8da81";
const competition = "bb22cbdd-8ed7-4a4b-8c10-266269b73c69";
const ordinary = "/api/learn/challenges/17/";
const team = `/api/learn/competitions/${competition}/challenges/17/`;

test("actual Next config routes UUID competition attachments to Learn before the Auth catch-all", async () => {
  for (const env of [
    {
      BACKEND_HOST: "fallback.test", AUTH_HOST: "auth.test", AUTH_PORT: "25643",
      LEARN_HOST: "learn.test", LEARN_PORT: "25644", COMMUNITY_HOST: "community.test",
      COMMUNITY_PORT: "28301", ENV_ORCH_HOST: "env.test", ENV_ORCH_PORT: "28201",
      MINIO_HOST: "objects.test", MINIO_PORT: "25641",
    },
    { BACKEND_HOST: "shared.test" },
  ]) {
    const cwd = "/fixture/frontend";
    const config = compile(source("../next.config.ts"), {
      exports: {}, process: { env, cwd: () => cwd },
      require: (name) => {
        assert.equal(name, "next-intl/plugin");
        return { default: (requestPath) => {
          assert.equal(requestPath, "./src/i18n/request.ts");
          return (value) => value;
        } };
      },
    }).default;
    assert.equal(config.basePath, "/ctf");
    assert.equal(config.turbopack.root, cwd);
    const host = (service) => env[`${service}_HOST`] || env.BACKEND_HOST;
    const auth = `http://${host("AUTH")}:${env.AUTH_PORT || "8020"}`;
    const learn = `http://${host("LEARN")}:${env.LEARN_PORT || "8100"}`;
    const community = `http://${host("COMMUNITY")}:${env.COMMUNITY_PORT || "8300"}`;
    const environment = `http://${host("ENV_ORCH")}:${env.ENV_ORCH_PORT || "8200"}`;
    const objects = `http://${host("MINIO")}:${env.MINIO_PORT || "9000"}`;
    const rewrites = await config.rewrites();
    const uuidPath = "/api/learn/competitions/:competitionId/challenges/:challengeId/attachments/:assetId";
    const catchAll = "/api/learn/competitions/:path*";
    assert(rewrites.findIndex((rule) => rule.source === uuidPath) < rewrites.findIndex((rule) => rule.source === catchAll));
    assert.deepEqual(Array.from(rewrites, ({ source, destination }) => [source, destination]), [
      ["/api/auth/:path*", `${auth}/:path*`],
      ["/api/learn/competitions/:competitionId/challenges/:challengeId/files/:fileIndex", `${learn}/competitions/:competitionId/challenges/:challengeId/files/:fileIndex`],
      [uuidPath, `${learn}/competitions/:competitionId/challenges/:challengeId/attachments/:assetId`],
      ["/api/learn/competitions/:competitionId/challenges/:challengeId", `${learn}/competitions/:competitionId/challenges/:challengeId`],
      [catchAll, `${auth}/competitions/:path*`],
      ["/api/learn/:path*", `${learn}/:path*`],
      ["/api/community/posts/:path*", `${community}/posts/:path*`],
      ["/api/community/reports/:path*", `${community}/reports/:path*`],
      ["/api/community/moderation/:path*", `${community}/moderation/:path*`],
      ["/api/community/marketplace/:path*", `${community}/marketplace/:path*`],
      ["/api/community/inquiries/:path*", `${community}/inquiries/:path*`],
      ["/api/community/inquiries", `${community}/inquiries`],
      ["/api/community/teams/:path*", `${auth}/teams/:path*`],
      ["/api/community/teams", `${auth}/teams`],
      ["/api/env/:path*", `${environment}/:path*`],
      ["/api/ctf/:path*", `${auth}/:path*`],
      ["/storage/:path*", `${objects}/:path*`],
    ]);
  }
});

test("downloads accept only the exact current parent and explicit private/legacy route", () => {
  for (const suffix of ["files/0", "files/21", `attachments/${asset}`]) {
    assert.equal(download.challengeAttachmentHref(ordinary + suffix, 17), "/ctf" + ordinary + suffix);
    assert.equal(download.challengeAttachmentHref(team + suffix, 17, competition), "/ctf" + team + suffix);
    assert.equal(download.challengeAttachmentHref(team + suffix, 17), null);
    assert.equal(download.challengeAttachmentHref(ordinary + suffix, 17, competition), null);
    assert.equal(download.challengeAttachmentHref(ordinary + suffix, 18), null);
  }
});

test("foreign objects, URL escapes, whitespace and malformed identities cannot become links", () => {
  for (const candidate of [null, "/storage/challenge-private-assets/file", "https://foreign.test/" + ordinary,
    "//foreign.test/file", ordinary + "files/-1", ordinary + "files/01", ordinary + "files/9007199254740992",
    ordinary + "files/0?token=hidden", ordinary + "files/0#section", ordinary + "files/0\n",
    ordinary + "files/%30", ordinary + "files/..", ordinary + "attachments/other", ordinary + "files\\0"]) {
    assert.equal(download.challengeAttachmentHref(candidate, 17), null, String(candidate));
  }
  for (const id of [0, -1, NaN, Infinity, 1.5]) assert.equal(download.challengeAttachmentHref(ordinary + "files/0", id), null);
  assert.equal(download.challengeAttachmentHref(team + "files/0", 17, ""), null);
});

test("byte labels retain empty files and do not display invalid counters", () => {
  assert.equal(download.attachmentByteLabel(0), "0 B");
  assert.equal(download.attachmentByteLabel(1024), "1 KiB");
  assert.equal(download.attachmentByteLabel(100 * 1024 * 1024), "100 MiB");
  for (const value of [null, -1, NaN, Infinity, 1.1]) assert.equal(download.attachmentByteLabel(value), null);
});

function renderAttachments(props, locale = "ko") {
  const labels = JSON.parse(source(`../messages/${locale}.json`)).wargame;
  const jsx = (type, props, key) => ({ type, props, key });
  const exported = compile(source("../src/components/wargame/AttachmentSection.tsx"), {
    exports: {}, require: (name) => {
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "next-intl") return { useTranslations: () => (key, values = {}) => {
        assert.equal(typeof labels[key], "string", key);
        return Object.entries(values).reduce((value, [name, text]) => value.replace(`{${name}}`, text), labels[key]);
      } };
      if (name === "lucide-react") return { Download: "DownloadIcon", FileText: "FileIcon" };
      if (name === "@/lib/challenge-attachment-download") return download;
      throw new Error(`Unexpected dependency: ${name}`);
    },
  });
  return exported.AttachmentSection(props);
}
function elements(tree, predicate) {
  if (tree === null || tree === undefined || typeof tree !== "object") return [];
  if (Array.isArray(tree)) return tree.flatMap((item) => elements(item, predicate));
  return [...(predicate(tree) ? [tree] : []), ...elements(tree.props?.children, predicate)];
}

test("all locales display unavailable originals instead of an empty attachment box", () => {
  for (const locale of ["ko", "en", "ja", "zh"]) {
    const tree = renderAttachments({ files: ["old-name.pcap"], challengeId: 17 }, locale);
    assert.equal(elements(tree, (x) => x.type === "li").length, 1);
    assert.equal(elements(tree, (x) => x.type === "a").length, 0);
    assert.equal(elements(tree, (x) => x.props?.role === "status").length, 1);
    assert.equal(renderAttachments({ files: ["old-name.pcap"], attachments: [], challengeId: 17 }, locale), null);
  }
});

test("ready metadata never authorizes a different parent's object, and unavailable stays disabled", () => {
  const record = { id: asset, legacy_index: null, name: "original.pcap", byte_size: 0, sha256: "a".repeat(64), state: "ready", download_path: ordinary + `attachments/${asset}` };
  const good = renderAttachments({ files: [], attachments: [record], challengeId: 17 });
  const links = elements(good, (x) => x.type === "a");
  assert.equal(links.length, 1); assert.equal(links[0].props.download, true);
  assert.equal(links[0].props["aria-label"], "original.pcap 다운로드");
  for (const changed of [{ ...record, state: "unavailable" }, { ...record, download_path: ordinary.replace("17", "18") + "files/0" }]) {
    assert.equal(elements(renderAttachments({ files: [], attachments: [changed], challengeId: 17 }), (x) => x.type === "a").length, 0);
  }
});

test("preview includes private originals once and retains unavailable legacy names", () => {
  const dictionaries = JSON.parse(source("../messages/ko.json"));
  const jsx = (type, props, key) => ({ type, props, key });
  const preview = compile(source("../src/components/admin/challenges/StepPreview.tsx"), {
    exports: {}, require: (name) => {
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "next-intl") return { useTranslations: (namespace) => (key) => dictionaries[namespace][key] ?? key };
      if (name === "lucide-react") return { FileText: "FileIcon" };
      if (name === "@/components/ui/Button") return { Button: "Button" };
      if (name === "@/components/ui/CategoryIcon") return { CategoryIcon: "CategoryIcon" };
      if (name === "@/components/learn/MarkdownRenderer") return { MarkdownRenderer: "MarkdownRenderer" };
      throw new Error(`Unexpected dependency: ${name}`);
    },
  });
  const tree = preview.StepPreview({ data: {
    title: "fixture", category: "web", difficulty: "easy", description: "fixture", points: 100,
    flagType: "static", flagValue: "fixture", flags: [], files: ["lost-original.pcap"],
    attachments: [{ id: null, legacy_index: 0, name: "lost-original.pcap", state: "unavailable" },
      { id: asset, legacy_index: null, name: "real-original.pcap", state: "ready" }],
    envType: "none", hints: [], isPublished: false,
  }, onChange() {}, onSubmit() {} });
  const rows = elements(tree, (x) => x.type === "li");
  assert.equal(rows.length, 2);
  const names = elements(tree, (x) => x.type === "span").map((x) => x.props.children);
  assert.equal(names.filter((x) => x === "real-original.pcap").length, 1);
  assert.equal(names.filter((x) => x === "lost-original.pcap").length, 1);
  assert(names.includes("사용 불가"));
});

test("actual FormData client keeps POST defaults and sends owner-bound PUT with CSRF and multipart bytes", async () => {
  const coordinator = createAuthRequestCoordinator(), calls = [];
  coordinator.register({ snapshot: () => ({ epoch: 1, ownerId: "A", status: "authenticated", authenticated: true }), verify: async () => "authenticated" });
  const api = compile(source("../src/lib/api-client.ts"), {
    exports: {}, process: { env: {} }, URL, document: { cookie: "ctf_csrf_token=fixture" },
    require: (name) => {
      if (name === "@/lib/auth-request-coordinator.mjs") return { AuthRequestBlockedError, authRequestCoordinator: coordinator };
      if (name === "@/lib/api-error.mjs") return { normalizeApiErrorPayload };
      throw new Error(`Unexpected dependency: ${name}`);
    },
    fetch: async (path, options) => { calls.push({ path, options }); return { ok: true, status: 200, json: async () => ({ ready: true }) }; },
  });
  const body = new FormData(); body.append("file", new Blob(["original-bytes"]), "original.pcap");
  const owner = api.captureApiRequestOwner("A");
  await api.apiFormFetch("/api/learn/challenge-attachment-drafts/d/uploads/u", body, { method: "PUT", requestOwner: owner });
  await api.apiFormFetch("/api/learn/challenges/upload-thumbnail", body, { requestOwner: owner });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.method, "PUT"); assert.equal(calls[1].options.method, "POST");
  for (const call of calls) {
    assert.equal(call.options.body, body); assert.equal(call.options.credentials, "include");
    assert.equal(call.options.headers["X-CSRF-Token"], "fixture"); assert.equal(call.options.headers["Content-Type"], undefined);
  }
  coordinator.register({ snapshot: () => ({ epoch: 2, ownerId: "B", status: "authenticated", authenticated: true }), verify: async () => "authenticated" });
  await assert.rejects(api.apiFormFetch("/api/learn/challenge-attachment-drafts/d/uploads/u", body,
    { method: "PUT", requestOwner: owner }), (error) => error.status === 409 && error.code === "auth_session_changed");
  assert.equal(calls.length, 2, "an old owner's upload must not transmit using the new account");
});
