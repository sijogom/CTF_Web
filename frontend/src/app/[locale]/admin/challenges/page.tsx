"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";
import { ChallengeFilters } from "@/components/admin/challenges/ChallengeFilters";
import { ChallengeTable } from "@/components/admin/challenges/ChallengeTable";
import { ChallengeDeleteModal } from "@/components/admin/challenges/ChallengeDeleteModal";
import { ChallengeCleanupJobs } from "@/components/admin/challenges/ChallengeCleanupJobs";
import type { AdminChallenge } from "@/types/challenge";

export default function AdminChallengesPage() {
  const t = useTranslations("admin");
  const { toast } = useToast();
  const { isSuperAdmin } = useAuth();

  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("");
  const [challenges, setChallenges] = useState<AdminChallenge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<number | null>(null);

  // 삭제 모달 (3단계)
  const [deleteTarget, setDeleteTarget] = useState<AdminChallenge | null>(null);
  const [deleteStep, setDeleteStep] = useState(0);
  const [deleting, setDeleting] = useState(false);
  const [cleanupRefreshVersion, setCleanupRefreshVersion] = useState(0);

  const fetchChallenges = useCallback(() => {
    setLoading(true);
    const typeParam = typeFilter ? `&challenge_type=${typeFilter}` : "";
    apiFetch<{ items: AdminChallenge[] } | AdminChallenge[]>(
      `/api/learn/challenges?show_all=true${typeParam}`,
    )
      .then((res) => {
        setChallenges(Array.isArray(res) ? res : res.items ?? []);
        setError(null);
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : t("loadStatsFailed")),
      )
      .finally(() => setLoading(false));
  }, [t, typeFilter]);

  useEffect(() => {
    fetchChallenges();
  }, [fetchChallenges]);

  // ──── 공개/비공개 토글 ────
  const handleTogglePublish = async (c: AdminChallenge) => {
    setActionLoading(c.id);
    try {
      await apiFetch(`/api/learn/challenges/${c.id}`, {
        method: "PATCH",
        body: { is_published: !c.is_published },
      });
      setChallenges((prev) =>
        prev.map((ch) =>
          ch.id === c.id ? { ...ch, is_published: !ch.is_published } : ch,
        ),
      );
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("statusFailed"));
    } finally {
      setActionLoading(null);
    }
  };

  // ──── 삭제 모달 (3단계) ────
  const openDeleteModal = (c: AdminChallenge) => {
    if (!isSuperAdmin) return;
    setDeleteTarget(c);
    setDeleteStep(1);
  };

  const closeDeleteModal = () => {
    setDeleteTarget(null);
    setDeleteStep(0);
  };

  const handleDeleteConfirm = async () => {
    if (!isSuperAdmin) return;
    if (deleteStep < 3) {
      setDeleteStep(deleteStep + 1);
      return;
    }
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      const result = await apiFetch<{ cleanup_status?: string }>(`/api/learn/challenges/${deleteTarget.id}`, {
        method: "DELETE",
      });
      setChallenges((prev) => prev.filter((c) => c.id !== deleteTarget.id));
      closeDeleteModal();
      setCleanupRefreshVersion((version) => version + 1);
      if (result.cleanup_status && result.cleanup_status !== "completed") {
        toast("info", t("cleanupQueued"));
      }
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : t("deleteFailed"));
    } finally {
      setDeleting(false);
    }
  };

  // ──── 필터링 ────
  const filtered = challenges.filter((c) => {
    const matchSearch = c.name.toLowerCase().includes(search.toLowerCase());
    const matchCategory =
      categoryFilter === "all" || c.category === categoryFilter;
    return matchSearch && matchCategory;
  });

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return <div className="py-20 text-center text-status-error">{error}</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t("challengeManagement")}</h1>
        <Link href="/admin/challenges/new">
          <Button>{t("newChallenge")}</Button>
        </Link>
      </div>

      <ChallengeFilters
        search={search}
        categoryFilter={categoryFilter}
        typeFilter={typeFilter}
        onSearchChange={setSearch}
        onCategoryChange={setCategoryFilter}
        onTypeChange={setTypeFilter}
      />

      {/* 빈 상태 */}
      {filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border p-12 text-center">
          <span className="mb-3 text-4xl">{"\u{1F3F4}"}</span>
          <h3 className="mb-1 text-lg font-semibold">{t("noChallenges")}</h3>
          <p className="mb-4 text-sm text-text-secondary">
            {t("noChallengesDesc")}
          </p>
          <Link href="/admin/challenges/new">
            <Button>{t("newChallenge")}</Button>
          </Link>
        </div>
      ) : (
        <ChallengeTable
          challenges={filtered}
          loading={loading}
          actionLoading={actionLoading}
          onTogglePublish={handleTogglePublish}
          onDelete={openDeleteModal}
        />
      )}

      {isSuperAdmin && <ChallengeCleanupJobs refreshVersion={cleanupRefreshVersion} />}

      <ChallengeDeleteModal
        challenge={deleteTarget}
        step={deleteStep}
        deleting={deleting}
        onConfirm={handleDeleteConfirm}
        onClose={closeDeleteModal}
      />
    </div>
  );
}
