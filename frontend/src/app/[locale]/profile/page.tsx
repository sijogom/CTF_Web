"use client";

import { useState, useEffect } from "react";
import { useTranslations } from "next-intl";
import { ActivityHeatmap } from "@/components/charts/ActivityHeatmap";
import { ProfileHeader, CategoryRadar } from "@/components/profile/StatsSummary";
import { BadgeSection } from "@/components/profile/BadgeSection";
import { SolveHistory } from "@/components/profile/SolveHistory";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { Profile, CategoryStat, Badge, SolveRecord } from "@/types/profile";
import { useAuth } from "@/providers/AuthProvider";
import { AccountContentBoundary } from "@/components/layout/AccountContentBoundary";

export default function ProfilePage() {
  return <AccountContentBoundary><ProfileContent /></AccountContentBoundary>;
}

function ProfileContent() {
  const t = useTranslations("profile");
  const { user } = useAuth();
  const ownerId = user?.id;

  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileOwner, setProfileOwner] = useState<string | null>(null);
  const [categoryStats, setCategoryStats] = useState<CategoryStat[]>([]);
  const [badges, setBadges] = useState<Badge[]>([]);
  const [history, setHistory] = useState<SolveRecord[]>([]);
  const [activity, setActivity] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ownerId) return;
    let cancelled = false;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
    setLoading(true);
    setError(null);

    Promise.allSettled([
      apiFetch<Profile>("/api/auth/users/me/profile", { signal: controller.signal }),
      apiFetch<CategoryStat[]>("/api/auth/users/me/stats", { signal: controller.signal }),
      apiFetch<Badge[]>("/api/auth/users/me/badges", { signal: controller.signal }),
      apiFetch<SolveRecord[]>("/api/auth/users/me/history", { signal: controller.signal }),
      apiFetch<Record<string, number>>("/api/auth/users/me/activity", { signal: controller.signal }),
    ])
      .then(([profileRes, statsRes, badgesRes, historyRes, activityRes]) => {
        if (cancelled) return;
        if (profileRes.status === "fulfilled") { setProfile(profileRes.value); setProfileOwner(ownerId); }
        else setError(profileRes.reason instanceof ApiError ? profileRes.reason.message : t("loadFailed"));

        setCategoryStats(statsRes.status === "fulfilled" ? statsRes.value : []);
        setBadges(badgesRes.status === "fulfilled" ? badgesRes.value : []);
        setHistory(historyRes.status === "fulfilled" ? historyRes.value : []);
        setActivity(activityRes.status === "fulfilled" ? activityRes.value : {});
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    }, 0);

    return () => { cancelled = true; window.clearTimeout(timer); controller.abort(); };
  }, [ownerId, t]);

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

  if (!profile || profileOwner !== ownerId) return null;

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      {/* 프로필 헤더 + XP 프로그레스 */}
      <ProfileHeader profile={profile} />

      {/* 레이더 차트 + 뱃지 컬렉션 */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <CategoryRadar categoryStats={categoryStats} />
        <BadgeSection badges={badges} />
      </div>

      {/* 활동 히트맵 */}
      <div className="rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-4 text-lg font-semibold">{t("heatmap")}</h2>
        <ActivityHeatmap data={activity} weeks={26} />
      </div>

      {/* 풀이 이력 */}
      <SolveHistory solves={history} />
    </div>
  );
}
