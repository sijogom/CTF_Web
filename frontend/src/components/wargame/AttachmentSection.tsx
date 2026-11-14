"use client";

import { useTranslations } from "next-intl";
import { Download, FileText } from "lucide-react";
import { attachmentByteLabel, challengeAttachmentHref } from "@/lib/challenge-attachment-download";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

interface AttachmentSectionProps {
  files: string[];
  attachments?: ChallengeAttachmentDescriptor[];
  challengeId: number;
}

export function AttachmentSection({ files, attachments, challengeId }: AttachmentSectionProps) {
  const t = useTranslations("wargame");
  const rows: ChallengeAttachmentDescriptor[] = attachments ?? files.map((file, index) => ({
    id: null, legacy_index: index, name: file.split("/").pop() || file,
    byte_size: null, sha256: null, state: "unavailable", download_path: null,
  }));
  if (rows.length === 0) return null;

  return (
    <section className="border-t border-border pt-6">
      <h2 className="mb-3 text-lg font-semibold">{t("attachments")}</h2>
      <ul className="divide-y divide-border">
        {rows.map((file, index) => {
          const href = file.state === "ready" ? challengeAttachmentHref(file.download_path, challengeId) : null;
          const size = attachmentByteLabel(file.byte_size);
          return (
            <li key={file.id ?? `legacy-${file.legacy_index ?? index}`} className="flex min-w-0 items-center gap-3 py-3">
              <FileText size={18} className="shrink-0 text-text-muted" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="break-all text-sm">{file.name}</p>
                {size && <p className="text-xs text-text-muted">{size}</p>}
              </div>
              {href ? (
                <a href={href} download className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md text-accent-primary hover:bg-bg-surface focus-visible:outline-2 focus-visible:outline-accent-primary"
                  aria-label={t("downloadAttachment", { name: file.name })} title={t("downloadAttachment", { name: file.name })}>
                  <Download size={18} aria-hidden="true" />
                </a>
              ) : <span className="max-w-[40%] shrink-0 break-words text-right text-xs text-accent-warning" role="status">{t("attachmentUnavailable")}</span>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
