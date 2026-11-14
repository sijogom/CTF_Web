export type ReasonError = "required" | "length" | "unsafe";

/** Match Learn's existing raw-length check, then Python strip/safe-text validation. */
export function normalizeRevealReason(value: string): { reason: string; error: ReasonError | null } {
  if (Array.from(value).length > 1000) return { reason: value, error: "length" };
  const reason = value.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g, "");
  if (!reason) return { reason, error: "required" };
  if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(reason) || /<\/?(?!https?:\/\/)[A-Za-z][^>]*>/i.test(reason)) {
    return { reason, error: "unsafe" };
  }
  return { reason, error: null };
}

export interface FlagRevealScope {
  ownerId: string;
  submissionId: string;
  generation: number;
  requestId: number;
}

export function isCurrentFlagReveal(request: FlagRevealScope, current: FlagRevealScope | null, ownerId: string | null, generation: number): boolean {
  return current !== null && ownerId === request.ownerId && generation === request.generation
    && current.ownerId === request.ownerId && current.submissionId === request.submissionId
    && current.generation === request.generation && current.requestId === request.requestId;
}

export function parseFlagRevealResponse(value: unknown, submissionId: string): string {
  if (!value || typeof value !== "object" || !("id" in value) || value.id !== submissionId
    || !("submitted_flag" in value) || typeof value.submitted_flag !== "string"
    || !("submitted_at" in value) || (value.submitted_at !== null && typeof value.submitted_at !== "string")) {
    throw new Error("Invalid submission reveal response");
  }
  return value.submitted_flag;
}

const ISO_INSTANT = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/;
const LOCAL_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$/;
const pad = (value: number) => String(value).padStart(2, "0");
function validParts(parts: RegExpMatchArray): boolean {
  const [year, month, day, hour, minute, second] = parts.slice(1, 7).map((value) => Number(value ?? 0));
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= new Date(Date.UTC(year, month, 0)).getUTCDate()
    && hour <= 23 && minute <= 59 && second <= 59;
}

/** Keep sub-millisecond boundaries rather than widening an inclusive audit range. */
export function normalizeAuditInstant(value: string): string | null {
  const parts = value.match(ISO_INSTANT);
  if (!parts || !validParts(parts)) return null;
  if (parts[8] !== "Z" && (Number(parts[8].slice(1, 3)) > 23 || Number(parts[8].slice(4, 6)) > 59)) return null;
  const time = new Date(value);
  if (!Number.isFinite(time.getTime())) return null;
  const utc = time.toISOString();
  if (!/^\d{4}-/.test(utc) || utc.startsWith("0000-")) return null;
  return `${utc.slice(0, 19)}${parts[7] ? `.${parts[7]}` : ""}Z`;
}

export function auditLocalInput(value: string): string {
  const instant = normalizeAuditInstant(value);
  if (instant === null) return "";
  const time = new Date(instant), fraction = instant.match(/\.\d+(?=Z)/)?.[0] ?? "";
  return `${String(time.getFullYear()).padStart(4, "0")}-${pad(time.getMonth() + 1)}-${pad(time.getDate())}T${pad(time.getHours())}:${pad(time.getMinutes())}:${pad(time.getSeconds())}${fraction}`;
}

export interface AuditTimeInputParts {
  dateTime: string;
  fraction: string;
}

/** Native datetime-local controls cannot reliably retain six fractional digits. */
export function auditTimeInputParts(value: string): AuditTimeInputParts {
  const local = auditLocalInput(value);
  const separator = local.indexOf(".");
  return separator < 0 ? { dateTime: local, fraction: "" }
    : { dateTime: local.slice(0, separator), fraction: local.slice(separator + 1) };
}

export function auditInstantFromInputParts({ dateTime, fraction }: AuditTimeInputParts): string | null {
  if (!/^[0-9]{0,6}$/.test(fraction) || dateTime.includes(".")) return null;
  if (!dateTime) return fraction ? null : "";
  return auditInstantFromLocal(`${dateTime}${fraction ? `.${fraction}` : ""}`);
}

export function auditInstantFromLocal(value: string): string | null {
  const parts = value.match(LOCAL_TIME);
  if (!parts || !validParts(parts)) return null;
  const [year, month, day, hour, minute, second] = parts.slice(1, 7).map((part) => Number(part ?? 0));
  const time = new Date(0); time.setFullYear(year, month - 1, day); time.setHours(hour, minute, second, 0);
  if (time.getFullYear() !== year || time.getMonth() !== month - 1 || time.getDate() !== day
    || time.getHours() !== hour || time.getMinutes() !== minute || time.getSeconds() !== second) return null;
  return `${time.toISOString().slice(0, 19)}${parts[7] ? `.${parts[7]}` : ""}Z`;
}

export function auditTimeRange(from: string, to: string): { from: string | null; to: string | null; error: "invalid_from" | "invalid_to" | "reversed" | null } {
  const start = from ? normalizeAuditInstant(from) : null, end = to ? normalizeAuditInstant(to) : null;
  if (from && start === null) return { from: null, to: end, error: "invalid_from" };
  if (to && end === null) return { from: start, to: null, error: "invalid_to" };
  const micros = (value: string) => BigInt(Date.parse(value.slice(0, 19) + "Z")) * BigInt(1000) + BigInt((value.match(/\.(\d+)Z$/)?.[1] ?? "").padEnd(6, "0"));
  return { from: start, to: end, error: start && end && micros(start) > micros(end) ? "reversed" : null };
}
