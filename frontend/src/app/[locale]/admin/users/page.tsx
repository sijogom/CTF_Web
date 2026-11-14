"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { ConfirmModal } from "@/components/ui/ConfirmModal";
import { UserTable } from "@/components/admin/users/UserTable";
import { UserDeleteModal } from "@/components/admin/users/UserDeleteModal";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import type { UserItem, UsersResponse } from "@/types/user";

interface AdminUserMutationResponse {
  is_active?: boolean;
  refresh_revocation_pending?: boolean;
}

export default function AdminUsersPage() {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const [users, setUsers] = useState<UserItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [error, setError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<UserItem | null>(null);
  const [deleteStep, setDeleteStep] = useState(0); // 0: closed, 1~3: confirm steps
  const [deleting, setDeleting] = useState(false);
  const [roleChangeTarget, setRoleChangeTarget] = useState<{ userId: string; username: string; newRole: string } | null>(null);
  const [tierOptions, setTierOptions] = useState<string[]>([]);
  const PAGE_SIZE = 20;

  // 검색 디바운스 (300ms)
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        page: String(page),
        size: String(PAGE_SIZE),
      });
      if (debouncedSearch) params.set("search", debouncedSearch);

      const data = await apiFetch<UsersResponse>(`/api/auth/admin/users?${params}`);
      setUsers(data.items);
      setTotal(data.total);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("loadUsersFailed");
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [page, debouncedSearch, t]);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  useEffect(() => {
    apiFetch<{ tiers?: { name: string }[] }>("/api/auth/admin/settings")
      .then((data) => setTierOptions((data.tiers || []).map((t) => t.name)))
      .catch(() => {});
  }, []);

  const handleTierChange = async (userId: string, tierName: string) => {
    const prev = users.find((u) => u.id === userId)?.tier_override;
    setUsers((p) => p.map((u) => (u.id === userId ? { ...u, tier_override: tierName || null } : u)));
    try {
      await apiFetch(`/api/auth/admin/users/${userId}/tier?tier=${encodeURIComponent(tierName)}`, { method: "PATCH" });
    } catch (err) {
      setUsers((p) => p.map((u) => (u.id === userId ? { ...u, tier_override: prev ?? null } : u)));
      toast("error", err instanceof ApiError ? err.message : "실패");
    }
  };

  const handleRoleChange = async (userId: string, newRole: string) => {
    const prevRole = users.find((u) => u.id === userId)?.role;
    // 낙관적 업데이트
    setUsers((prev) =>
      prev.map((u) => (u.id === userId ? { ...u, role: newRole } : u))
    );
    try {
      const response = await apiFetch<AdminUserMutationResponse>(`/api/auth/admin/users/${userId}/role?role=${newRole}`, {
        method: "PATCH",
      });
      if (response.refresh_revocation_pending) {
        toast("warning", t("refreshRevocationPending"));
      }
    } catch (err) {
      // 실패 시 롤백
      if (prevRole !== undefined) {
        setUsers((prev) =>
          prev.map((u) => (u.id === userId ? { ...u, role: prevRole } : u))
        );
      }
      const msg = err instanceof ApiError ? err.message : t("roleFailed");
      toast("error", msg);
    }
  };

  const toggleActive = async (userId: string) => {
    try {
      const res = await apiFetch<AdminUserMutationResponse>(
        `/api/auth/admin/users/${userId}/active`,
        { method: "PATCH" }
      );
      const nextIsActive = res.is_active;
      if (typeof nextIsActive !== "boolean") {
        throw new Error("사용자 활성 상태 응답이 올바르지 않습니다.");
      }
      setUsers((prev) =>
        prev.map((u) => (u.id === userId ? { ...u, is_active: nextIsActive } : u))
      );
      if (res.refresh_revocation_pending) {
        toast("warning", t("refreshRevocationPending"));
      }
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("statusFailed");
      toast("error", msg);
    }
  };

  const resendVerification = async (userId: string) => {
    try {
      await apiFetch(`/api/auth/admin/users/${userId}/resend-verification`, {
        method: "POST",
      });
      toast("success", t("resendVerificationSent"));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("resendVerificationFailed");
      toast("error", msg);
    }
  };

  const manualVerify = async (userId: string) => {
    try {
      await apiFetch(`/api/auth/admin/users/${userId}/verify`, {
        method: "PATCH",
      });
      setUsers((prev) =>
        prev.map((u) => (u.id === userId ? { ...u, email_verified: true } : u))
      );
      toast("success", t("manualVerifyDone"));
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : tc("errorOccurred");
      toast("error", msg);
    }
  };

  const openDeleteModal = (user: UserItem) => {
    setDeleteTarget(user);
    setDeleteStep(1);
  };

  const closeDeleteModal = () => {
    setDeleteTarget(null);
    setDeleteStep(0);
  };

  const handleDeleteConfirm = async () => {
    if (deleteStep < 3) {
      setDeleteStep(deleteStep + 1);
      return;
    }

    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await apiFetch(`/api/auth/admin/users/${deleteTarget.id}`, {
        method: "DELETE",
      });
      setUsers((prev) => prev.filter((u) => u.id !== deleteTarget.id));
      setTotal((prev) => prev - 1);
      closeDeleteModal();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : t("deleteFailed");
      toast("error", msg);
    } finally {
      setDeleting(false);
    }
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t("userManagement")}</h1>
        <span className="text-sm text-text-muted">{t("totalCount", { count: total })}</span>
      </div>

      <input
        value={search}
        onChange={(e) => {
          setSearch(e.target.value);
          setPage(1);
        }}
        placeholder={t("searchUsersPlaceholder")}
        aria-label={t("searchUsersPlaceholder")}
        className="w-full rounded-lg border border-border bg-bg-primary px-4 py-2.5 text-sm focus:border-accent-primary focus:outline-none"
      />

      {error && (
        <div className="rounded-lg border border-accent-error/30 bg-accent-error/10 px-4 py-3 text-sm text-accent-error">
          {error}
          <button onClick={fetchUsers} className="ml-2 underline">{t("retryAction")}</button>
        </div>
      )}

      <UserTable
        users={users}
        loading={loading}
        search={search}
        tierOptions={tierOptions}
        onRoleChange={(userId, username, newRole) =>
          setRoleChangeTarget({ userId, username, newRole })
        }
        onTierChange={handleTierChange}
        onToggleActive={toggleActive}
        onResendVerification={resendVerification}
        onManualVerify={manualVerify}
        onDelete={openDeleteModal}
      />

      {/* 페이지네이션 */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
          >
            {t("prev")}
          </button>
          <span className="text-sm text-text-muted">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="rounded-lg border border-border px-3 py-1.5 text-sm disabled:opacity-40"
          >
            {t("next")}
          </button>
        </div>
      )}

      {/* 계정 삭제 확인 모달 (3단계) */}
      <UserDeleteModal
        user={deleteTarget}
        step={deleteStep}
        deleting={deleting}
        onConfirm={handleDeleteConfirm}
        onClose={closeDeleteModal}
      />

      {/* 역할 변경 확인 모달 */}
      <ConfirmModal
        open={roleChangeTarget !== null}
        title={t("roleChangeTitle")}
        description={t("roleChangeDesc", { username: roleChangeTarget?.username ?? "", role: roleChangeTarget?.newRole ?? "" })}
        confirmLabel={tc("confirm")}
        variant="default"
        onConfirm={() => {
          if (roleChangeTarget) handleRoleChange(roleChangeTarget.userId, roleChangeTarget.newRole);
          setRoleChangeTarget(null);
        }}
        onCancel={() => setRoleChangeTarget(null)}
      />
    </div>
  );
}
