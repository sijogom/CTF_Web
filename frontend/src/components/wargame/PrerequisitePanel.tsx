"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import type { LearningPrerequisite, PrerequisiteDetail } from "@/types/knowledge";

interface Props {
  learningPrereqs: LearningPrerequisite[];
  challengePrereqs: PrerequisiteDetail[];
  locked?: boolean;
}

export function PrerequisitePanel({ learningPrereqs, challengePrereqs, locked = true }: Props) {
  const t = useTranslations("wargame");

  const hasRequired = learningPrereqs.length > 0 || challengePrereqs.length > 0;

  if (!hasRequired) return null;

  return (
    <div className={`rounded-xl border p-6 ${
      locked
        ? "border-accent-warning/30 bg-accent-warning/5"
        : "border-accent-primary/20 bg-accent-primary/5"
    }`}>
      {/* 잠금 상태: 필수 조건 */}
      {locked && hasRequired && (
        <>
          <h3 className="mb-4 flex items-center gap-2 text-base font-semibold text-accent-warning">
            <span>🔒</span> {t("prerequisitePanelTitle")}
          </h3>

          {/* 필수 학습 */}
          {learningPrereqs.length > 0 && (
            <div className="mb-4">
              <h4 className="mb-2 text-sm font-medium text-text-primary">{t("requiredLearning")}</h4>
              <ul className="space-y-2">
                {learningPrereqs.map((lp) => (
                  <li key={`${lp.type}-${lp.id}`} className="flex items-center gap-3 text-sm">
                    <span className={lp.completed ? "text-accent-success" : "text-text-muted"}>
                      {lp.completed ? "☑" : "☐"}
                    </span>
                    <span className="rounded bg-accent-primary/10 px-1.5 py-0.5 text-[10px] font-bold text-accent-primary uppercase">
                      {lp.type === "course" ? "코스" : "레슨"}
                    </span>
                    <span className="flex-1">{lp.name}</span>
                    {!lp.completed && (
                      <Link
                        href={`/learn/${lp.type === "lesson" && lp.course_id ? lp.course_id : lp.id}`}
                        className="text-xs text-accent-primary hover:underline"
                      >
                        {t("goToLearn")} →
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* 필수 문제 */}
          {challengePrereqs.length > 0 && (
            <div className="mb-4">
              <h4 className="mb-2 text-sm font-medium text-text-primary">{t("requiredChallenges")}</h4>
              <ul className="space-y-2">
                {challengePrereqs.map((cp) => (
                  <li key={cp.id} className="flex items-center gap-3 text-sm">
                    <span className={cp.solved ? "text-accent-success" : "text-text-muted"}>
                      {cp.solved ? "☑" : "☐"}
                    </span>
                    <span className="text-text-muted">#{cp.id}</span>
                    <span className="flex-1">{cp.name}</span>
                    {!cp.solved && (
                      <Link
                        href={`/wargame/${cp.id}`}
                        className="text-xs text-accent-primary hover:underline"
                      >
                        {t("goToChallenge")} →
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

    </div>
  );
}
