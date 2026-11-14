export interface ChallengeAttachmentDescriptor {
  id: string | null;
  legacy_index: number | null;
  name: string;
  byte_size: number | null;
  sha256: string | null;
  state: "ready" | "unavailable";
  download_path: string | null;
}

export interface AttachmentUploadResponse {
  id: string;
  draft_id: string;
  original_name: string;
  byte_size: number;
  sha256: string;
  content_type: string;
  state: "pending" | "ready" | "missing";
  ready_for_attach: boolean;
  reconcile_required: boolean;
  retry_allowed: boolean;
}

export interface AttachmentDraftResponse {
  draft_id: string;
  challenge_id: number | null;
  state: "open" | "bound";
  base_attachment_revision: number;
  current_attachment_revision: number;
  attachments: ChallengeAttachmentDescriptor[];
  uploads: AttachmentUploadResponse[];
}
