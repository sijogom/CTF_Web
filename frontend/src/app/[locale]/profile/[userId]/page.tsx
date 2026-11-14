"use client";

import { use, useState, useEffect } from "react";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { CategoryRadar } from "@/components/profile/StatsSummary";
import { BadgeSection } from "@/components/profile/BadgeSection";
import { SolveHistory } from "@/components/profile/SolveHistory";
import { ActivityHeatmap } from "@/components/charts/ActivityHeatmap";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { PublicProfile } from "@/types/profile";
import { MarkdownRenderer } from "@/components/learn/MarkdownRenderer";

export default function PublicProfilePage({
  params,
}: {
  params: Promise<{ userId: string }>;
}) {
  const { userId } = use(params);
  const t = useTranslations("profile");
  const tc = useTranslations("common");

  const [profile, setProfile] = useState<PublicProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    apiFetch<PublicProfile>(`/api/auth/users/${userId}/public-profile`)
      .then((data) => {
        if (!cancelled) setProfile(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? err.message : t("loadFailed"),
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent-primary border-t-transparent" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="py-20 text-center">
        <p className="text-status-error">{error}</p>
        <Link
          href="/ranking"
          className="mt-3 inline-block text-sm text-accent-primary hover:underline"
        >
          {tc("back")}
        </Link>
      </div>
    );
  }

  if (!profile) return null;

  const progressPercent =
    profile.xp_for_level > 0
      ? (profile.xp_in_level / profile.xp_for_level) * 100
      : 0;

  const xpToNext =
    profile.xp_for_level > 0
      ? profile.xp_for_level - profile.xp_in_level
      : 0;

  const joinDateFormatted = profile.joined_at
    ? (() => {
        const d = new Date(profile.joined_at);
        return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")}`;
      })()
    : null;

  return (
    <div className="mx-auto max-w-5xl space-y-8">
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
          </div>
          {profile.bio && (
            <MarkdownRenderer content={profile.bio} />
          )}
          <div className="mt-3 flex items-center gap-4 text-sm text-text-muted">
            <span>{t("rank", { n: profile.rank })}</span>
            <span>{t("streak", { n: profile.streak_days })}</span>
            {joinDateFormatted && (
              <span>{t("joinDate", { date: joinDateFormatted })}</span>
            )}
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
            {t("nextLevel", { xp: xpToNext.toLocaleString() })}
          </span>
        </div>
        <div className="h-3 w-full overflow-hidden rounded-full bg-bg-primary">
          <div
            className="h-full rounded-full bg-gradient-to-r from-accent-primary to-purple-400 transition-all"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </div>

      {/* 레이더 차트 + 뱃지 컬렉션 */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <CategoryRadar categoryStats={profile.stats} />
        <BadgeSection badges={profile.badges} />
      </div>

      {/* 활동 히트맵 */}
      {Object.keys(profile.activity).length > 0 && (
        <div className="rounded-xl border border-border bg-bg-surface p-6">
          <h2 className="mb-4 text-lg font-semibold">{t("heatmap")}</h2>
          <ActivityHeatmap data={profile.activity} weeks={26} />
        </div>
      )}

      {/* 풀이 이력 */}
      {profile.history.length > 0 && (
        <SolveHistory solves={profile.history} />
      )}

      {/* 뒤로가기 */}
      <div className="flex justify-center">
        <Link
          href="/ranking"
          className="rounded-lg border border-border px-6 py-2 text-sm text-text-secondary transition-colors hover:bg-bg-hover"
        >
          {tc("back")}
        </Link>
      </div>
    </div>
  );
}
