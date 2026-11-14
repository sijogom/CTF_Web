const UUID = "[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}";
const UUID_PATTERN = new RegExp(`^${UUID}$`);
const FILE_PATTERN = new RegExp(`^(?:files/(?:0|[1-9][0-9]*)|attachments/${UUID})$`);

export function challengeAttachmentHref(
  downloadPath: string | null,
  challengeId: number,
  competitionId?: string,
): string | null {
  if (!Number.isSafeInteger(challengeId) || challengeId < 1 || typeof downloadPath !== "string") return null;
  if (/[\\\s?#%]/.test(downloadPath)) return null;
  if (competitionId !== undefined && !UUID_PATTERN.test(competitionId)) return null;
  const prefix = competitionId === undefined
    ? `/api/learn/challenges/${challengeId}/`
    : `/api/learn/competitions/${competitionId}/challenges/${challengeId}/`;
  if (!downloadPath.startsWith(prefix) || !FILE_PATTERN.test(downloadPath.slice(prefix.length))) return null;
  if (downloadPath.slice(prefix.length).startsWith("files/")
    && !Number.isSafeInteger(Number(downloadPath.slice(prefix.length + "files/".length)))) return null;
  const base = (process.env.NEXT_PUBLIC_BASE_PATH || "/ctf").replace(/\/$/, "");
  if (base && !/^\/[A-Za-z0-9/_-]+$/.test(base)) return null;
  return `${base}${downloadPath}`;
}

export function attachmentByteLabel(bytes: number | null): string | null {
  if (bytes === null || !Number.isSafeInteger(bytes) || bytes < 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  const unit = bytes < 1024 * 1024 ? 1024 : 1024 * 1024;
  const value = Math.round(bytes / unit * 10) / 10;
  return `${value} ${unit === 1024 ? "KiB" : "MiB"}`;
}
