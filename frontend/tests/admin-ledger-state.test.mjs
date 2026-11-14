import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/admin-ledger-state.ts", import.meta.url), "utf8");
const context = { exports: {}, Date };
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, context);
const h = context.exports;

test("reveal reason matches raw Unicode length followed by Learn safe-text normalization", () => {
  assert.equal(h.normalizeRevealReason("  검토 사유\n").reason, "검토 사유");
  for (const value of ["", "\t\n\u0085\u3000", " ".repeat(1000)]) assert.equal(h.normalizeRevealReason(value).error, "required");
  for (const value of ["a".repeat(1001), " ".repeat(1000) + "a", "😀".repeat(1001)]) assert.equal(h.normalizeRevealReason(value).error, "length");
  assert.equal(h.normalizeRevealReason("😀".repeat(1000)).error, null);
  for (const value of ["a\u0000b", "a\u001fb", "a\u007fb", "<b>reason</b>", "<IMG src=x>"]) assert.equal(h.normalizeRevealReason(value).error, "unsafe");
  for (const value of ["one\ntwo", "one\ttwo", "<https://example.com>", "\ufeffreason"]) assert.equal(h.normalizeRevealReason(value).error, null);
  assert.equal(h.normalizeRevealReason("\u001creason\u001f").reason, "reason");
});

test("reveal results require exactly the current owner, target, list generation and request identity", () => {
  const request = { ownerId: "admin-1", submissionId: "submission-1", generation: 3, requestId: 8 };
  assert.equal(h.isCurrentFlagReveal(request, { ...request }, "admin-1", 3), true);
  for (const changed of [{ ownerId: "admin-2" }, { submissionId: "submission-2" }, { generation: 4 }, { requestId: 9 }]) {
    assert.equal(h.isCurrentFlagReveal(request, { ...request, ...changed }, "admin-1", 3), false);
  }
  assert.equal(h.isCurrentFlagReveal(request, null, "admin-1", 3), false);
  assert.equal(h.isCurrentFlagReveal(request, request, null, 3), false);
  assert.equal(h.isCurrentFlagReveal(request, request, "admin-2", 3), false);
  assert.equal(h.isCurrentFlagReveal(request, request, "admin-1", 4), false);
});

test("reveal success schema must bind the exact submission and never substitute an error or another item", () => {
  const value = { id: "one", submitted_flag: "<literal original>", submitted_at: null };
  assert.equal(h.parseFlagRevealResponse(value, "one"), "<literal original>");
  for (const invalid of [null, {}, [], { ...value, id: "other" }, { ...value, submitted_flag: 1 }, { ...value, submitted_at: undefined }]) {
    assert.throws(() => h.parseFlagRevealResponse(invalid, "one"));
  }
});

test("audit timestamps require valid calendar parts and an explicit time zone", () => {
  assert.equal(h.normalizeAuditInstant("2026-10-18T09:12:34.123456+09:00"), "2026-10-18T00:12:34.123456Z");
  assert.equal(h.normalizeAuditInstant("2024-02-29T00:00:00Z"), "2024-02-29T00:00:00Z");
  for (const invalid of ["2026-10-18T09:00", "2026-02-29T00:00:00Z", "2026-02-30T00:00:00Z", "0000-01-01T00:00:00Z", "2026-01-01T24:00:00Z",
    "2026-01-01T00:60:00Z", "2026-01-01T00:00:60Z", "2026-01-01T00:00:00+24:00", "2026-01-01T00:00:00+09:60", "not a date"]) {
    assert.equal(h.normalizeAuditInstant(invalid), null, invalid);
  }
});

test("audit inclusive boundaries retain microseconds, allow equality and reject reversed or invalid ranges", () => {
  assert.equal(h.auditTimeRange("", "").error, null);
  assert.equal(h.auditTimeRange("2026-10-18T00:00:00.000001Z", "2026-10-18T09:00:00.000001+09:00").error, null);
  assert.equal(h.auditTimeRange("2026-10-18T00:00:00.000002Z", "2026-10-18T00:00:00.000001Z").error, "reversed");
  assert.equal(h.auditTimeRange("2026-10-18T00:00:00", "").error, "invalid_from");
  assert.equal(h.auditTimeRange("", "invalid").error, "invalid_to");
  assert.equal(h.auditTimeRange("2026-10-18T09:00:00+09:00", "").from, "2026-10-18T00:00:00Z");
});

test("local datetime input round-trips browser-local time and retains fractional boundaries", () => {
  for (const instant of ["2026-10-18T00:12:34.123456Z", "2024-02-29T10:20:30Z", "2026-12-31T23:59:59.000001Z"]) {
    assert.equal(h.auditInstantFromLocal(h.auditLocalInput(instant)), instant);
  }
  assert.equal(h.auditLocalInput("invalid"), "");
  assert.equal(h.auditInstantFromLocal("2026-02-30T10:00"), null);
  assert.equal(h.auditInstantFromLocal("2026-10-18T25:00"), null);
  assert.equal(h.auditInstantFromLocal("2026-10-18T10:00Z"), null);
});

test("whole-second native values and a separate fractional input round-trip every supported precision losslessly", () => {
  for (const fraction of ["", "1", "01", "001", "0001", "00001", "000001", "123456", "000000"]) {
    const instant = `2026-10-18T00:12:34${fraction ? `.${fraction}` : ""}Z`;
    const parts = h.auditTimeInputParts(instant);
    assert.match(parts.dateTime, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/);
    assert.equal(parts.dateTime.includes("."), false);
    assert.equal(parts.fraction, fraction);
    assert.equal(h.auditInstantFromInputParts(parts), instant);
    const changedDate = { ...parts, dateTime: parts.dateTime.replace(/:\d{2}$/, ":35") };
    assert.equal(h.auditInstantFromInputParts(changedDate), instant.replace(":34", ":35"));
  }
  const offset = h.auditTimeInputParts("2026-10-18T09:12:34.000001+09:00");
  assert.equal(h.auditInstantFromInputParts(offset), "2026-10-18T00:12:34.000001Z");
});

test("split timestamp inputs reject orphan fractions and invalid precision without rounding or inventing a date", () => {
  assert.equal(h.auditInstantFromInputParts({ dateTime: "", fraction: "" }), "");
  for (const fraction of ["0", "000001", "1234567", "1.2", "-1", "１", " "]) {
    assert.equal(h.auditInstantFromInputParts({ dateTime: "", fraction }), null);
  }
  for (const fraction of ["1234567", "1.2", "-1", "１", " "]) {
    assert.equal(h.auditInstantFromInputParts({ dateTime: "2026-10-18T10:00:00", fraction }), null);
  }
  assert.equal(h.auditInstantFromInputParts({ dateTime: "2026-10-18T10:00:00.000001", fraction: "" }), null);
  assert.equal(h.auditInstantFromInputParts({ dateTime: "2026-02-30T10:00:00", fraction: "000001" }), null);
  assert.equal(h.auditTimeInputParts("invalid").dateTime, "");
  assert.equal(h.auditTimeInputParts("invalid").fraction, "");
});
