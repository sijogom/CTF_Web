"use client";

import { useCallback, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { RichTextEditor } from "@/components/ui/RichTextEditor";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { apiFetch, ApiError } from "@/lib/api-client";
import { isRichContentEmpty } from "@/lib/rich-content.mjs";
import { useAuth } from "@/providers/AuthProvider";
import { useToast } from "@/providers/ToastProvider";

type InquiryReply = {
  admin_name: string;
  content: string;
  created_at: string | null;
};

type MyInquiry = {
  id: string;
  subject: string;
  status: string;
  created_at: string | null;
  replies: InquiryReply[];
};

type InquiryCreateResponse = {
  id: string;
  status: string;
  guest_access_required: boolean;
};

export default function InquiryPage() {
  const t = useTranslations("community");
  const locale = useLocale();
  const { user } = useAuth();
  const { toast } = useToast();

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [myInquiries, setMyInquiries] = useState<MyInquiry[]>([]);
  const [createdTicketId, setCreatedTicketId] = useState<string | null>(null);
  const [lookupTicketId, setLookupTicketId] = useState("");
  const [lookupEmail, setLookupEmail] = useState("");
  const [lookupLoading, setLookupLoading] = useState(false);
  const [lookupMessage, setLookupMessage] = useState<string | null>(null);

  const loadMyInquiries = useCallback(() => {
    void apiFetch<MyInquiry[]>("/api/community/inquiries/my")
      .then(setMyInquiries)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (user) {
      setName(user.username ?? "");
      setEmail(user.email ?? "");
      loadMyInquiries();
    } else {
      setMyInquiries([]);
      setName("");
      setEmail("");
    }
  }, [loadMyInquiries, user]);

  const isValid =
    name.trim().length > 0 &&
    email.trim().length > 0 &&
    subject.trim().length > 0 &&
    !isRichContentEmpty(content);

  const resetForm = () => {
    setSubject("");
    setContent("");
    if (!user) {
      setName("");
      setEmail("");
    }
  };

  const handleSubmit = async () => {
    if (!isValid) return;
    setLoading(true);
    setError(null);
    setCreatedTicketId(null);

    try {
      const result = await apiFetch<InquiryCreateResponse>("/api/community/inquiries", {
        method: "POST",
        body: {
          email: email.trim(),
          name: name.trim(),
          subject: subject.trim(),
          content,
          locale,
        },
      });
      if (result.guest_access_required) {
        setCreatedTicketId(result.id);
        setLookupTicketId(result.id);
        setLookupEmail(email.trim());
        toast("success", t("inquiryGuestSuccess"));
      } else {
        toast("success", t("inquirySuccess"));
        loadMyInquiries();
      }
      resetForm();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("submitFailed"));
    } finally {
      setLoading(false);
    }
  };

  const handleGuestAccessRequest = async () => {
    if (!lookupTicketId.trim() || !lookupEmail.trim()) return;
    setLookupLoading(true);
    setLookupMessage(null);
    try {
      await apiFetch(`/api/community/inquiries/${lookupTicketId.trim()}/guest-access/request`, {
        method: "POST",
        body: { email: lookupEmail.trim() },
      });
      setLookupMessage(t("inquiryGuestAccessRequested"));
    } catch (err) {
      setLookupMessage(err instanceof ApiError ? err.message : t("submitFailed"));
    } finally {
      setLookupLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-text-primary">{t("inquiry")}</h1>
        <p className="mt-1 text-sm text-text-secondary">{t("inquiryDesc")}</p>
      </div>

      <div className="space-y-5 rounded-lg border border-border bg-bg-surface p-6">
        <div>
          <label className="mb-2 block text-sm font-medium text-text-secondary">
            {t("inquiryName")}
          </label>
          <input
            type="text"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t("inquiryNamePlaceholder")}
            maxLength={100}
            readOnly={Boolean(user)}
            aria-describedby={user ? "inquiry-member-contact" : undefined}
            className="w-full rounded-lg border border-border bg-bg-elevated px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none read-only:cursor-not-allowed read-only:opacity-75"
          />
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium text-text-secondary">
            {t("inquiryEmail")}
          </label>
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder={t("inquiryEmailPlaceholder")}
            maxLength={254}
            readOnly={Boolean(user)}
            aria-describedby={user ? "inquiry-member-contact" : undefined}
            className="w-full rounded-lg border border-border bg-bg-elevated px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none read-only:cursor-not-allowed read-only:opacity-75"
          />
          {user && (
            <p id="inquiry-member-contact" className="mt-2 text-xs text-text-muted">
              {t("inquiryMemberContact")}
            </p>
          )}
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium text-text-secondary">
            {t("inquirySubject")}
          </label>
          <input
            type="text"
            value={subject}
            onChange={(event) => setSubject(event.target.value)}
            placeholder={t("inquirySubjectPlaceholder")}
            maxLength={300}
            className="w-full rounded-lg border border-border bg-bg-elevated px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
        </div>

        <div>
          <label className="mb-2 block text-sm font-medium text-text-secondary">
            {t("inquiryContent")}
          </label>
          <RichTextEditor
            value={content}
            onChange={setContent}
            placeholder={t("inquiryContentPlaceholder")}
          />
        </div>

        {error && (
          <div className="rounded-lg bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
            {error}
          </div>
        )}

        <div className="flex justify-end">
          <Button onClick={handleSubmit} disabled={loading || !isValid}>
            {loading ? "..." : t("inquirySubmit")}
          </Button>
        </div>
      </div>

      {!user && (
        <section className="space-y-4 rounded-lg border border-border bg-bg-surface p-6">
          <div>
            <h2 className="text-lg font-semibold text-text-primary">{t("inquiryGuestAccessTitle")}</h2>
            <p className="mt-1 text-sm text-text-secondary">{t("inquiryGuestAccessDesc")}</p>
          </div>

          {createdTicketId && (
            <div className="rounded-lg bg-accent-primary/10 px-4 py-3 text-sm text-text-primary">
              <span className="font-medium">{t("inquiryTicketId")}: </span>
              <code className="break-all">{createdTicketId}</code>
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <input
              type="text"
              value={lookupTicketId}
              onChange={(event) => setLookupTicketId(event.target.value)}
              placeholder={t("inquiryTicketIdPlaceholder")}
              className="w-full rounded-lg border border-border bg-bg-elevated px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
            <input
              type="email"
              value={lookupEmail}
              onChange={(event) => setLookupEmail(event.target.value)}
              maxLength={254}
              placeholder={t("inquiryEmailPlaceholder")}
              className="w-full rounded-lg border border-border bg-bg-elevated px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
            />
          </div>
          {lookupMessage && <p className="text-sm text-text-secondary">{lookupMessage}</p>}
          <div className="flex justify-end">
            <Button
              onClick={handleGuestAccessRequest}
              disabled={lookupLoading || !lookupTicketId.trim() || !lookupEmail.trim()}
            >
              {lookupLoading ? "..." : t("inquiryGuestAccessRequest")}
            </Button>
          </div>
        </section>
      )}

      {user && myInquiries.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-lg font-semibold text-text-primary">{t("myInquiries")}</h2>
          {myInquiries.map((inquiry) => (
            <article key={inquiry.id} className="space-y-2 rounded-lg border border-border bg-bg-surface p-4">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium">{inquiry.subject}</span>
                <span
                  className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                    inquiry.status === "answered"
                      ? "bg-accent-success/10 text-accent-success"
                      : inquiry.status === "closed"
                        ? "bg-text-muted/10 text-text-muted"
                        : "bg-accent-warning/10 text-accent-warning"
                  }`}
                >
                  {t(`inquiryStatus_${inquiry.status}`)}
                </span>
              </div>
              <div className="text-xs text-text-muted">
                {inquiry.created_at ? new Date(inquiry.created_at).toLocaleString() : ""}
              </div>
              {inquiry.replies.length > 0 && (
                <div className="mt-2 space-y-2 border-t border-border pt-2">
                  {inquiry.replies.map((reply, index) => (
                    <div key={`${reply.created_at ?? "reply"}-${index}`} className="rounded-lg bg-accent-primary/5 p-3 text-sm">
                      <div className="mb-1 flex items-center gap-2">
                        <span className="font-medium text-accent-primary">{reply.admin_name}</span>
                        <span className="text-xs text-text-muted">
                          {reply.created_at ? new Date(reply.created_at).toLocaleString() : ""}
                        </span>
                      </div>
                      <MarkdownRenderer content={reply.content} />
                    </div>
                  ))}
                </div>
              )}
            </article>
          ))}
        </section>
      )}
    </div>
  );
}
