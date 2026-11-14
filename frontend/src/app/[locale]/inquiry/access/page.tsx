"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";
import { apiFetch, ApiError } from "@/lib/api-client";

type InquiryReply = {
  admin_name: string;
  content: string;
  created_at: string | null;
};

type InquiryViewer = {
  id: string;
  subject: string;
  content: string;
  status: string;
  created_at: string | null;
  replies: InquiryReply[];
};

type ProofFragment = {
  ticketId: string;
  challengeId: string;
  proof: string;
};

function readProofFragment(): ProofFragment | null {
  if (typeof window === "undefined") return null;
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const ticketId = fragment.get("ticket");
  const challengeId = fragment.get("challenge");
  const proof = fragment.get("proof");
  if (!ticketId || !challengeId || !proof) return null;
  return { ticketId, challengeId, proof };
}

export default function InquiryAccessPage() {
  const t = useTranslations("community");
  const [ticketId, setTicketId] = useState("");
  const [ticket, setTicket] = useState<InquiryViewer | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadTicket = useCallback(async (id: string) => {
    const data = await apiFetch<InquiryViewer>(`/api/community/inquiries/${id}/viewer`);
    setTicket(data);
    setTicketId(data.id);
  }, []);

  const exchangeProof = useCallback(async () => {
    const fragment = readProofFragment();
    if (!fragment) return;
    setLoading(true);
    setMessage(null);
    try {
      await apiFetch("/api/community/inquiries/guest-access/exchange", {
        method: "POST",
        body: { challenge_id: fragment.challengeId, proof: fragment.proof },
      });
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      await loadTicket(fragment.ticketId);
      setMessage(t("inquiryAccessVerified"));
    } catch (error) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      setMessage(error instanceof ApiError ? error.message : t("inquiryAccessFailed"));
    } finally {
      setLoading(false);
    }
  }, [loadTicket, t]);

  useEffect(() => {
    void exchangeProof();
  }, [exchangeProof]);

  const handleLoad = async () => {
    if (!ticketId.trim()) return;
    setLoading(true);
    setMessage(null);
    try {
      await loadTicket(ticketId.trim());
    } catch (error) {
      setMessage(error instanceof ApiError ? error.message : t("inquiryAccessFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-text-primary">{t("inquiryGuestAccessTitle")}</h1>
        <p className="mt-1 text-sm text-text-secondary">{t("inquiryGuestAccessDesc")}</p>
      </div>

      <section className="space-y-3 rounded-lg border border-border bg-bg-surface p-6">
        <label className="block text-sm font-medium text-text-secondary" htmlFor="ticket-id">
          {t("inquiryTicketId")}
        </label>
        <div className="flex flex-col gap-3 sm:flex-row">
          <input
            id="ticket-id"
            type="text"
            value={ticketId}
            onChange={(event) => setTicketId(event.target.value)}
            placeholder={t("inquiryTicketIdPlaceholder")}
            className="min-w-0 flex-1 rounded-lg border border-border bg-bg-elevated px-4 py-3 text-sm text-text-primary focus:border-accent-primary focus:outline-none"
          />
          <Button onClick={handleLoad} disabled={loading || !ticketId.trim()}>
            {loading ? "..." : t("inquiryReadTicket")}
          </Button>
        </div>
        {message && <p className="text-sm text-text-secondary">{message}</p>}
      </section>

      {ticket && (
        <article className="space-y-4 rounded-lg border border-border bg-bg-surface p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-lg font-semibold text-text-primary">{ticket.subject}</h2>
            <span className="rounded-full bg-accent-primary/10 px-2.5 py-0.5 text-xs font-medium text-accent-primary">
              {t(`inquiryStatus_${ticket.status}`)}
            </span>
          </div>
          <MarkdownRenderer content={ticket.content} />
          {ticket.replies.length > 0 && (
            <div className="space-y-3 border-t border-border pt-4">
              <h3 className="text-sm font-semibold text-text-secondary">{t("inquiryReplies")}</h3>
              {ticket.replies.map((reply, index) => (
                <div key={`${reply.created_at ?? "reply"}-${index}`} className="rounded-lg bg-accent-primary/5 p-4">
                  <div className="mb-1.5 flex items-center justify-between gap-3 text-xs text-text-muted">
                    <span className="font-medium text-accent-primary">{reply.admin_name}</span>
                    <span>{reply.created_at ? new Date(reply.created_at).toLocaleString() : ""}</span>
                  </div>
                  <MarkdownRenderer content={reply.content} />
                </div>
              ))}
            </div>
          )}
        </article>
      )}
    </div>
  );
}
