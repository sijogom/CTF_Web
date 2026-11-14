"use client";

import { useTranslations, useLocale } from "next-intl";
import type { UserItem } from "@/types/user";

interface UserTableProps {
  users: UserItem[];
  loading: boolean;
  search: string;
  tierOptions: string[];
  onRoleChange: (userId: string, username: string, newRole: string) => void;
  onTierChange: (userId: string, tierName: string) => void;
  onToggleActive: (userId: string) => void;
  onResendVerification: (userId: string) => void;
  onManualVerify: (userId: string) => void;
  onDelete: (user: UserItem) => void;
}

export function UserTable({
  users,
  loading,
  search,
  tierOptions,
  onRoleChange,
  onTierChange,
  onToggleActive,
  onResendVerification,
  onManualVerify,
  onDelete,
}: UserTableProps) {
  const t = useTranslations("admin");
  const locale = useLocale();

  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full text-sm">
        <thead className="border-b border-border bg-bg-surface">
          <tr>
            <th className="whitespace-nowrap px-4 py-3 text-left font-medium">{t("colUsername")}</th>
            <th className="whitespace-nowrap px-4 py-3 text-left font-medium">{t("colEmail")}</th>
            <th className="whitespace-nowrap px-4 py-3 text-left font-medium">{t("colRole")}</th>
            <th className="whitespace-nowrap px-4 py-3 text-left font-medium">{t("colTier")}</th>
            <th className="whitespace-nowrap px-4 py-3 text-center font-medium">{t("colActive")}</th>
            <th className="whitespace-nowrap px-4 py-3 text-center font-medium">{t("colEmailVerified")}</th>
            <th className="whitespace-nowrap px-4 py-3 text-left font-medium">{t("createdAt")}</th>
            <th className="whitespace-nowrap px-4 py-3 text-center font-medium">{t("actions")}</th>
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr>
              <td colSpan={8} className="px-4 py-8 text-center text-text-muted">
                {t("loadingData")}
              </td>
            </tr>
          ) : users.length === 0 ? (
            <tr>
              <td colSpan={8} className="px-4 py-8 text-center text-text-muted">
                {search ? t("noSearchResults") : t("noRegisteredUsers")}
              </td>
            </tr>
          ) : (
            users.map((user) => (
              <tr key={user.id} className="border-b border-border last:border-0">
                <td className="whitespace-nowrap px-4 py-3 font-medium">{user.username}</td>
                <td className="whitespace-nowrap px-4 py-3 text-text-secondary">{user.email}</td>
                <td className="px-4 py-3">
                  <select
                    value={user.role}
                    onChange={(e) => onRoleChange(user.id, user.username, e.target.value)}
                    aria-label={t("colRole")}
                    className="rounded border border-border bg-bg-primary px-2 py-1 text-sm"
                  >
                    <option value="user">{t("roleUser")}</option>
                    <option value="moderator">{t("roleModerator")}</option>
                    <option value="admin">{t("roleAdmin")}</option>
                  </select>
                </td>
                <td className="px-4 py-3">
                  <select
                    value={user.tier_override || ""}
                    onChange={(e) => onTierChange(user.id, e.target.value)}
                    className="rounded border border-border bg-bg-primary px-2 py-1 text-sm"
                  >
                    <option value="">{t("tierAuto")}</option>
                    {tierOptions.map((name) => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </select>
                </td>
                <td className="px-4 py-3 text-center">
                  <span
                    className={`inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-xs ${
                      user.is_active
                        ? "bg-accent-success/10 text-accent-success"
                        : "bg-accent-error/10 text-accent-error"
                    }`}
                  >
                    {user.is_active ? t("active") : t("inactive")}
                  </span>
                </td>
                <td className="px-4 py-3 text-center">
                  <span
                    className={`inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-xs ${
                      user.email_verified
                        ? "bg-accent-success/10 text-accent-success"
                        : "bg-accent-error/10 text-accent-error"
                    }`}
                  >
                    {user.email_verified ? t("emailVerified") : t("emailNotVerified")}
                  </span>
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-text-muted">
                  {user.created_at ? new Date(user.created_at).toLocaleDateString(locale) : "-"}
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-center">
                  <div className="flex items-center justify-center gap-2">
                    <button
                      onClick={() => onToggleActive(user.id)}
                      className="text-sm text-accent-primary hover:underline"
                    >
                      {user.is_active ? t("deactivate") : t("activate")}
                    </button>
                    {!user.email_verified && (
                      <>
                        <button
                          onClick={() => onResendVerification(user.id)}
                          className="text-sm text-yellow-400 hover:underline"
                        >
                          {t("resendVerification")}
                        </button>
                        <button
                          onClick={() => onManualVerify(user.id)}
                          className="text-sm text-accent-success hover:underline"
                        >
                          {t("manualVerify")}
                        </button>
                      </>
                    )}
                    <button
                      onClick={() => onDelete(user)}
                      className="text-sm text-accent-error hover:underline"
                    >
                      {t("deleteUser")}
                    </button>
                  </div>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
