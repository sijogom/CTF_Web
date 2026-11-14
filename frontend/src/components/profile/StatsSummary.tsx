"use client";

import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { RadarChart } from "@/components/charts/RadarChart";
import type { Profile, CategoryStat } from "@/types/profile";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";

/* ── ProfileHeader: 아바타, 이름, 레벨, 스트릭, 날짜 ── */

interface ProfileHeaderProps {
  profile: Profile;
}

export function ProfileHeader({ profile }: ProfileHeaderProps) {
  const t = useTranslations("profile");
  const tc = useTranslations("common");

  const progressPercent =
    profile.xp_for_level > 0
      ? (profile.xp_in_level / profile.xp_for_level) * 100
      : 0;

  return (
    <>
      {/* 프로필 헤더 */}
      <div className="flex items-start gap-6 rounded-xl border border-border bg-bg-surface p-6">
        {profile.avatar_url ? (
          <img
            src={profile.avatar_url}
            alt={profile.username}
            className="h-20 w-20 rounded-full object-cover border border-border"
          />
        ) : (
          <div className="flex h-20 w-20 items-center justify-center rounded-full bg-accent-primary/10 text-3xl font-bold text-accent-primary">
            {(profile.username?.[0] ?? "?").toUpperCase()}
          </div>
        )}
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">{profile.username}</h1>
            {/* 티어/백준/디스코드 아이콘 */}
            {profile.tier_image && (
              <img src={profile.tier_image} alt="" className="h-5 w-5 object-contain" title={profile.tier_name || ""} />
            )}
            {profile.baekjoon_tier != null && profile.baekjoon_tier > 0 && (
              <img src={`https://static.solved.ac/tier_small/${profile.baekjoon_tier}.svg`} alt="" className="h-5 w-5" title={profile.baekjoon_handle || ""} />
            )}
            {profile.discord_username && (
              <span title={profile.discord_username}>
                <svg className="h-5 w-5 text-[#5865F2]" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
                </svg>
              </span>
            )}
            <Link
              href="/settings/profile"
              className="rounded-md border border-border px-3 py-1 text-xs text-text-secondary transition-colors hover:bg-bg-hover"
            >
              {t("editProfile")}
            </Link>
          </div>
          <MarkdownRenderer content={profile.bio} />
          <div className="mt-3 flex items-center gap-4 text-sm text-text-muted">
            <span>{t("rank", { n: profile.rank })}</span>
            <span>{t("streak", { n: profile.streak_days })}</span>
            <span>
              {t("joinDate", {
                date: (() => {
                  const d = new Date(profile.join_date);
                  return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")}`;
                })(),
              })}
            </span>
          </div>
        </div>
        <div className="text-right">
          <div className="text-sm text-text-muted">
            {t("level", { n: profile.level })}
          </div>
          <div className="text-3xl font-bold text-accent-primary">
            Lv.{profile.level}
          </div>
        </div>
      </div>

      {/* XP 프로그레스 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <div className="mb-2 flex items-center justify-between text-sm">
          <span className="text-text-secondary">
            {tc("xpLabel")}: {profile.xp.toLocaleString()}
          </span>
          <span className="text-text-muted">
            {t("nextLevel", { xp: profile.xp_to_next.toLocaleString() })}
          </span>
        </div>
        <div className="h-3 w-full overflow-hidden rounded-full bg-bg-primary">
          <div
            className="h-full rounded-full bg-gradient-to-r from-accent-primary to-purple-400 transition-all"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </div>
    </>
  );
}

/* ── CategoryRadar: 레이더 차트 + 카테고리별 프로그레스 바 ── */

interface CategoryRadarProps {
  categoryStats: CategoryStat[];
}

export function CategoryRadar({ categoryStats }: CategoryRadarProps) {
  const t = useTranslations("profile");

  const totalCatSolves = categoryStats.reduce((s, c) => s + c.value, 0);
  const totalCatMax = categoryStats.reduce((s, c) => s + c.max, 0);

  return (
    <div className="rounded-xl border border-border bg-bg-surface p-6">
      <h2 className="mb-4 text-lg font-semibold">{t("radarChart")}</h2>
      <div className="flex justify-center">
        <RadarChart data={categoryStats} size={260} />
      </div>

      {categoryStats.length > 0 && (
        <div className="mt-5 space-y-2.5">
          {categoryStats.map((cat) => {
            const percent =
              cat.max > 0 ? Math.round((cat.value / cat.max) * 100) : 0;
            return (
              <div key={cat.label} className="flex items-center gap-3">
                <span className="w-16 text-xs font-medium text-text-secondary truncate">
                  {cat.label}
                </span>
                <div className="flex-1 h-2 rounded-full bg-bg-primary overflow-hidden">
                  <div
                    className="h-full rounded-full bg-accent-primary transition-all"
                    style={{ width: `${percent}%` }}
                  />
                </div>
                <span className="w-14 text-right text-xs tabular-nums text-text-muted">
                  {cat.value}/{cat.max}
                </span>
                <span className="w-10 text-right text-xs tabular-nums font-medium text-accent-primary">
                  {percent}%
                </span>
              </div>
            );
          })}
          {/* 총합 */}
          <div className="flex items-center justify-between border-t border-border pt-2 text-xs">
            <span className="font-medium text-text-secondary">
              {t("totalSolves", { n: totalCatSolves })}
            </span>
            <span className="font-medium text-accent-primary">
              {totalCatMax > 0
                ? Math.round((totalCatSolves / totalCatMax) * 100)
                : 0}
              %
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
