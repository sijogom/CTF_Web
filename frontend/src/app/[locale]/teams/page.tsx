"use client";

import { useState, useEffect } from "react";
import { Link } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError } from "@/lib/api-client";
import { richContentToPlainText } from "@/lib/rich-content.mjs";
import { useAuth } from "@/providers/AuthProvider";

interface Team {
  id: string;
  name: string;
  description: string;
  member_count: number;
  total_score: number;
}

export default function TeamsPage() {
  const t = useTranslations("teams");
  const tc = useTranslations("common");
  const { user } = useAuth();

  const [teams, setTeams] = useState<Team[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<Team[]>("/api/auth/teams")
      .then(setTeams)
      .catch((err) => setError(err instanceof ApiError ? err.message : t("loadFailed")))
      .finally(() => setLoading(false));
  }, [t]);

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
        <div>
          <h1 className="text-3xl font-bold">{t("title")}</h1>
          <p className="mt-2 text-text-secondary">{t("description")}</p>
        </div>
        {user && <Link href="/teams/create">
          <Button>{t("createTeam")}</Button>
        </Link>}
      </div>

      {teams.length === 0 ? (
        <div className="rounded-xl border border-border bg-bg-surface py-16 text-center">
          <p className="text-text-muted">{t("noTeams")}</p>
        </div>
      ) : (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {teams.map((team) => (
          <Link
            key={team.id}
            href={`/teams/${team.id}`}
            className="group rounded-xl border border-border bg-bg-surface p-5 transition-all hover:border-accent-primary hover:shadow-lg"
          >
            <h3 className="text-lg font-semibold group-hover:text-accent-primary">
              {team.name}
            </h3>
            {team.description && (
              <p className="mt-2 line-clamp-3 text-sm text-text-secondary">
                {richContentToPlainText(team.description)}
              </p>
            )}
            <div className="mt-3 flex items-center gap-4 text-sm text-text-muted">
              <span>{t("memberCount", { count: team.member_count })}</span>
              <span>{team.total_score.toLocaleString()}{tc("pointsUnit")}</span>
            </div>
          </Link>
        ))}
      </div>
      )}
    </div>
  );
}
