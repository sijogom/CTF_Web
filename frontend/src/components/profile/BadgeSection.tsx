"use client";

import { useState, useCallback } from "react";
import { useTranslations } from "next-intl";
import type { Badge } from "@/types/profile";

/* ── 뱃지 팝오버 ── */

function BadgePopover({
  badge,
  onClose,
}: {
  badge: Badge;
  onClose: () => void;
}) {
  const t = useTranslations("profile");

  return (
    <>
      {/* 배경 오버레이 */}
      <div className="fixed inset-0 z-40" onClick={onClose} />
      {/* 팝오버 카드 */}
      <div className="absolute left-1/2 -translate-x-1/2 top-full z-50 mt-2 w-64 max-w-[calc(100vw-2rem)] rounded-xl border border-border bg-bg-surface p-4 shadow-xl">
        {/* 화살표 */}
        <div className="absolute -top-2 left-1/2 -translate-x-1/2 h-4 w-4 rotate-45 border-l border-t border-border bg-bg-surface" />

        <div className="flex items-start gap-3">
          <span className="text-3xl">{badge.icon}</span>
          <div className="flex-1 min-w-0">
            <h4 className="font-semibold text-text-primary">{badge.name}</h4>
            {badge.earned ? (
              <span className="inline-block mt-1 rounded-full bg-accent-success/15 px-2 py-0.5 text-xs font-medium text-accent-success">
                {t("badgeEarned")}
              </span>
            ) : badge.hidden ? (
              <span className="mt-1 block text-xs text-text-muted italic">
                {t("badgeHiddenHint")}
              </span>
            ) : (
              <>
                <p className="mt-1 text-xs text-text-muted font-medium">
                  {t("badgeHowToEarn")}
                </p>
                <p className="mt-0.5 text-xs text-text-secondary">
                  {badge.description}
                </p>
              </>
            )}
            {/* 진행도 바: target이 있고 미획득 뱃지만 표시 */}
            {!badge.earned &&
              badge.target != null &&
              badge.target > 0 &&
              badge.progress != null && (
                <div className="mt-2">
                  <div className="flex items-center justify-between text-[10px] text-text-muted mb-0.5">
                    <span>
                      {badge.progress}/{badge.target}
                    </span>
                    <span>
                      {Math.round((badge.progress / badge.target) * 100)}%
                    </span>
                  </div>
                  <div className="h-1.5 rounded-full bg-bg-primary overflow-hidden">
                    <div
                      className="h-full rounded-full bg-accent-primary transition-all"
                      style={{
                        width: `${Math.min((badge.progress / badge.target) * 100, 100)}%`,
                      }}
                    />
                  </div>
                </div>
              )}
          </div>
        </div>
      </div>
    </>
  );
}

/* ── 뱃지 섹션 ── */

interface BadgeSectionProps {
  badges: Badge[];
}

const SEEN_BADGES_KEY = "ctf-seen-badges";

function loadSeenBadges(): Set<string> {
  try {
    const raw = localStorage.getItem(SEEN_BADGES_KEY);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch { return new Set(); }
}

function saveSeenBadges(ids: Set<string>) {
  localStorage.setItem(SEEN_BADGES_KEY, JSON.stringify([...ids]));
}

export function BadgeSection({ badges }: BadgeSectionProps) {
  const t = useTranslations("profile");
  const [activeBadge, setActiveBadge] = useState<string | null>(null);
  const [seenBadges, setSeenBadges] = useState<Set<string>>(() => loadSeenBadges());

  const handleBadgeClick = useCallback((badgeId: string) => {
    setActiveBadge((prev) => (prev === badgeId ? null : badgeId));
    setSeenBadges((prev) => {
      if (prev.has(badgeId)) return prev;
      const next = new Set(prev);
      next.add(badgeId);
      saveSeenBadges(next);
      return next;
    });
  }, []);

  const closeBadgePopover = useCallback(() => setActiveBadge(null), []);

  const visibleBadges = badges.filter((b) => !b.hidden || b.earned);
  const hiddenUnearnedCount = badges.filter(
    (b) => b.hidden && !b.earned,
  ).length;
  const earnedCount = badges.filter((b) => b.earned).length;

  return (
    <div className="rounded-xl border border-border bg-bg-surface p-6">
      <h2 className="mb-4 text-lg font-semibold">
        {t("badges")} ({earnedCount}/{badges.length})
      </h2>
      <div className="grid grid-cols-4 gap-3">
        {visibleBadges.map((badge) => (
          <div key={badge.id} className="relative">
            <button
              type="button"
              onClick={() => handleBadgeClick(badge.id)}
              className={`flex w-full flex-col items-center rounded-lg p-3 text-center transition-colors ${
                badge.earned
                  ? "hover:bg-bg-primary cursor-pointer"
                  : "opacity-30 cursor-pointer hover:opacity-50"
              } ${activeBadge === badge.id ? "ring-2 ring-accent-primary bg-bg-primary" : ""}`}
            >
              <span className="text-2xl">{badge.icon}</span>
              <span className="mt-1 text-xs text-text-muted">{badge.name}</span>
              {badge.hidden && badge.earned && !seenBadges.has(badge.id) && (
                <span className="absolute -right-1 -top-1 rounded-full bg-accent-warning px-1.5 py-0.5 text-[10px] font-bold text-white">
                  !
                </span>
              )}
            </button>
            {/* 뱃지 팝오버 */}
            {activeBadge === badge.id && (
              <BadgePopover badge={badge} onClose={closeBadgePopover} />
            )}
          </div>
        ))}
        {hiddenUnearnedCount > 0 && (
          <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-border p-3 text-center">
            <span className="text-lg font-bold text-text-muted">
              +{hiddenUnearnedCount}
            </span>
            <span className="mt-1 text-xs text-text-muted">
              {t("hiddenBadges")}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
