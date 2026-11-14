"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiFetch } from "@/lib/api-client";

interface PlatformStats {
  users: number;
  challenges: number;
  solves: number;
  competitions: number;
}

interface StatItem {
  labelKey: string;
  value: number;
  suffixKey: string;
}

function useCountUp(target: number, isVisible: boolean, duration = 2000) {
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!isVisible || target === 0) {
      if (target !== 0) return;

      const resetTimer = setTimeout(() => {
        setCount(0);
      }, 0);
      return () => clearTimeout(resetTimer);
    }

    const startTime = Date.now();
    const timer = setInterval(() => {
      const elapsed = Date.now() - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 4);
      setCount(Math.round(eased * target));
      if (progress >= 1) clearInterval(timer);
    }, 16);

    return () => clearInterval(timer);
  }, [target, isVisible, duration]);

  return count;
}

function StatCounter({
  stat,
  isVisible,
  t,
}: {
  stat: StatItem;
  isVisible: boolean;
  t: ReturnType<typeof useTranslations>;
}) {
  const count = useCountUp(stat.value, isVisible);

  return (
    <div className="text-center">
      <p className="text-4xl font-bold text-accent-primary">
        {count.toLocaleString()}
        <span className="text-lg text-text-muted">{t(stat.suffixKey)}</span>
      </p>
      <p className="mt-2 text-sm text-text-secondary">{t(stat.labelKey)}</p>
    </div>
  );
}

export function LiveStats() {
  const ref = useRef<HTMLDivElement>(null);
  const [isVisible, setIsVisible] = useState(false);
  const [stats, setStats] = useState<PlatformStats | null>(null);
  const t = useTranslations("landing");

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true);
          observer.disconnect();
        }
      },
      { threshold: 0.3 }
    );

    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    apiFetch<PlatformStats>("/api/auth/stats/platform")
      .then((data) => { if (!cancelled) setStats(data); })
      .catch(() => {
        if (!cancelled) setStats({ users: 0, challenges: 0, solves: 0, competitions: 0 });
      });
    return () => { cancelled = true; };
  }, []);

  const statItems: StatItem[] = [
    { labelKey: "statLearners", value: stats?.users ?? 0, suffixKey: "suffixPeople" },
    { labelKey: "statChallenges", value: stats?.challenges ?? 0, suffixKey: "suffixCount" },
    { labelKey: "statSolves", value: stats?.solves ?? 0, suffixKey: "suffixTimes" },
    { labelKey: "statContests", value: stats?.competitions ?? 0, suffixKey: "suffixCount" },
  ];

  return (
    <section ref={ref} className="border-y border-border bg-bg-surface py-16">
      <div className="mx-auto grid max-w-4xl grid-cols-2 gap-8 px-6 lg:grid-cols-4">
        {statItems.map((stat) => (
          <StatCounter key={stat.labelKey} stat={stat} isVisible={isVisible} t={t} />
        ))}
      </div>
    </section>
  );
}
