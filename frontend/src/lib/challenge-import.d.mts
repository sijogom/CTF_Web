export type ChallengeImportMode = "skip" | "overwrite";

export interface ChallengeImportPayload {
  version?: 1 | 2;
  challenges: Record<string, unknown>[];
  mode: ChallengeImportMode;
}

export interface ChallengeImportResult {
  created: number;
  updated: number;
  skipped: number;
}

export class InvalidChallengeImportFile extends Error {}

export function parseChallengeImport(text: string, mode: ChallengeImportMode): ChallengeImportPayload;
export function parseChallengeImportResult(value: unknown): ChallengeImportResult;
export function importChallengeFile(
  file: { text(): Promise<string> },
  mode: ChallengeImportMode,
  submit: (payload: ChallengeImportPayload) => Promise<unknown>,
): Promise<ChallengeImportResult>;
