export class InvalidChallengeImportFile extends Error {
  constructor() {
    super("Invalid challenge import file");
    this.name = "InvalidChallengeImportFile";
  }
}

export function parseChallengeImport(text, mode) {
  if (mode !== "skip" && mode !== "overwrite") throw new InvalidChallengeImportFile();
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw new InvalidChallengeImportFile();
  }
  const legacy = Array.isArray(value);
  if (!legacy && (!value || typeof value !== "object" || Object.hasOwn(value, "version") && ![1, 2].includes(value.version))) {
    throw new InvalidChallengeImportFile();
  }
  const challenges = legacy ? value : value.challenges;
  if (!Array.isArray(challenges) || challenges.some((record) => !record || typeof record !== "object"
    || Array.isArray(record) || typeof record.name !== "string" || !record.name.trim())) {
    throw new InvalidChallengeImportFile();
  }
  return { ...(legacy || !Object.hasOwn(value, "version") ? {} : { version: value.version }), challenges, mode };
}

export function parseChallengeImportResult(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid challenge import result");
  const result = { created: value.created, updated: Object.hasOwn(value, "updated") ? value.updated : 0, skipped: value.skipped };
  if (Object.values(result).some((count) => !Number.isSafeInteger(count) || count < 0)) throw new Error("Invalid challenge import result");
  return result;
}

export async function importChallengeFile(file, mode, submit) {
  let payload;
  try {
    payload = parseChallengeImport(await file.text(), mode);
  } catch {
    throw new InvalidChallengeImportFile();
  }
  const response = await submit(payload);
  return parseChallengeImportResult(response);
}
