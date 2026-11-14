"use client";

import { useMemo, useRef, useState } from "react";
import { apiFetch, apiFormFetch } from "@/lib/api-client";
import { createChallengeAttachments } from "@/lib/challenge-attachments.mjs";
import type { AttachmentSnapshot, AttachmentSaveResponse } from "@/lib/challenge-attachments.mjs";
import type { ImageEditOperation } from "@/lib/image-edit-operation.mjs";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

export function useChallengeAttachments({ operation, files, initial, onChange, onCanonical }: {
  operation: ImageEditOperation;
  files: React.RefObject<Map<string, File>>;
  initial: { challengeId?: number; revision: number; attachments: ChallengeAttachmentDescriptor[] };
  onChange(value: AttachmentSnapshot): void;
  onCanonical(value: AttachmentSaveResponse): void;
}) {
  const callback = useRef({ onChange, onCanonical }); callback.current = { onChange, onCanonical };
  const [view, setView] = useState<{ controller: unknown; snapshot: AttachmentSnapshot } | null>(null);
  const controller = useMemo(() => {
    files.current = new Map();
    const next = createChallengeAttachments({ operation, initial, files: files.current, onChange(snapshot) {
      setView({ controller: next, snapshot }); callback.current.onChange(snapshot);
    }, onCanonical(value) { callback.current.onCanonical(value);
    }, request: async (method, path, body, owner) => {
      if (method === "PUT") { const form = new FormData(); form.append("file", body as File); return apiFormFetch(path, form, { method: "PUT", requestOwner: owner }); }
      return apiFetch(path, { method, ...(body === undefined ? {} : { body }), requestOwner: owner });
    } });
    return next;
    // Initial descriptors belong to this owner/screen, not subsequent local edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [operation]);
  return { controller, ...(view?.controller === controller ? view.snapshot : controller.snapshot()) };
}
