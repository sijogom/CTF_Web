import type { ImageEditLease, ImageEditOperation } from "./image-edit-operation.mjs";
import type { ApiRequestOwner } from "./api-client";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

export interface AttachmentItem {
  id: string; name: string; byte_size: number | null; sha256?: string | null;
  state: string; existing: boolean; ready_for_attach: boolean; retry_allowed: boolean;
  reconcile_required: boolean; attempted: boolean; error: string | null; canRetry: boolean;
}
export interface AttachmentSnapshot {
  items: AttachmentItem[]; attachments: ChallengeAttachmentDescriptor[];
  dirty: boolean; blocked: boolean; error: string | null; conflict: boolean;
}
export interface AttachmentMetadata {
  draftId: string; challengeId: number | null; revision: number;
  items: Pick<AttachmentItem, "id" | "name" | "byte_size" | "existing">[];
}
export interface AttachmentSaveResponse { id: number; attachments: ChallengeAttachmentDescriptor[]; attachment_revision: number; files?: string[] }
export interface AttachmentController {
  snapshot(): AttachmentSnapshot;
  add(files: File[]): Promise<void>;
  check(id: string, reconcile?: boolean): Promise<void>;
  retry(id: string): Promise<void>;
  remove(id: string): Promise<void>;
  move(id: string, delta: number): Promise<void>;
  prepareSave(lease: ImageEditLease, legacyChanged?: boolean): Promise<{ attachment_draft_id?: string; attachment_ids?: string[] }>;
  acceptSaved(value: AttachmentSaveResponse, lease: ImageEditLease): void;
  saveFailed(error: unknown): void;
  reload(): Promise<(AttachmentSaveResponse & { files: string[] }) | undefined>;
  metadata(): AttachmentMetadata | null;
  restore(metadata: AttachmentMetadata): Promise<true | undefined>;
}
export function attachmentFileError(file: Pick<File, "name" | "size">): "name" | "size" | null;
export function attachmentIds(value: ChallengeAttachmentDescriptor[]): string[];
export function createChallengeAttachments(options: {
  operation: ImageEditOperation;
  initial: { challengeId?: number; revision: number; attachments: ChallengeAttachmentDescriptor[] };
  files: Map<string, File>;
  request(method: string, path: string, body: unknown, owner: ApiRequestOwner): Promise<unknown>;
  onChange(value: AttachmentSnapshot): void;
  onCanonical?(value: AttachmentSaveResponse): void;
  makeId?(): string;
}): AttachmentController;
