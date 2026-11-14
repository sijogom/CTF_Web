"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";

interface Activity {
  id: string;
  user: string;
  actionKey: string;
  target: string;
  timeAgoKey: string;
  timeAgoParam?: number;
}

export function ActivityFeed() {
  const [activities, setActivities] = useState<Activity[]>([]);
  const [visibleActivities, setVisibleActivities] = useState<Activity[]>([]);
  const [error, setError] = useState<string | null>(null);
  const t = useTranslations("landing");

  useEffect(() => {
    const fetchActivities = () => {
      apiFetch<Activity[]>("/api/auth/activities")
        .then(setActivities)
        .catch((err) => {
          const message = err instanceof ApiError ? err.message : t("loadFailed");
          setError(message);
        });
    };
    fetchActivities();
    const interval = setInterval(fetchActivities, 30_000);
    return () => clearInterval(interval);
  }, [t]);

  // 순차 등장 효과 — activities가 실제로 변경된 경우에만 애니메이션 재실행
  const prevActivitiesRef = useRef<string>("");

  useEffect(() => {
    if (activities.length === 0) return;
    const key = JSON.stringify(activities.map(a => a.id));
    if (key === prevActivitiesRef.current) return;
    prevActivitiesRef.current = key;

    const timers = activities.map((activity, i) =>
      setTimeout(() => {
        setVisibleActivities((prev) =>
          i === 0 ? [activity] : [...prev, activity]
        );
      }, i * 400)
    );
    return () => timers.forEach(clearTimeout);
  }, [activities]);

  function formatTimeAgo(activity: Activity): string {
    if (activity.timeAgoKey === "justNow") return t("justNow");
    if (activity.timeAgoKey === "minutesAgo") return t("minutesAgo", { n: activity.timeAgoParam ?? 0 });
    if (activity.timeAgoKey === "hoursAgo") return t("hoursAgo", { n: activity.timeAgoParam ?? 0 });
    return "";
  }

  if (error) {
    return (
      <section className="px-6 py-20">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="mb-4 text-3xl font-bold">{t("activityTitle")}</h2>
          <p className="text-sm text-text-muted">{error}</p>
        </div>
      </section>
    );
  }

  return (
    <section className="px-6 py-20">
      <div className="mx-auto max-w-2xl">
        <h2 className="mb-8 text-center text-3xl font-bold">{t("activityTitle")}</h2>
        <div className="space-y-3">
          {visibleActivities.map((a) => (
            <div
              key={a.id}
              className="flex items-center gap-3 rounded-lg border border-border bg-bg-surface px-4 py-3 text-sm transition-all"
            >
              <span className="h-2 w-2 rounded-full bg-accent-success" />
              <span className="font-medium text-accent-primary">{a.user}</span>
              <span className="text-text-secondary">
                {(() => { try { return t(a.actionKey); } catch { return a.actionKey; } })()}
                {a.target && (
                  <>
                    {" "}
                    <strong className="text-text-primary">{a.target}</strong>
                  </>
                )}
              </span>
              <span className="ml-auto text-xs text-text-muted">{formatTimeAgo(a)}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
