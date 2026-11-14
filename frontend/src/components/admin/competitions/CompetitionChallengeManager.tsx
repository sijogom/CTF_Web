"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { useTranslations } from "next-intl";
import { apiFetch, ApiError } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import { CompetitionChallengeTable } from "./CompetitionChallengeTable";
import { ChallengeSelector } from "./ChallengeSelector";
import { ScoringConfig } from "./ScoringConfig";

export interface CompChallenge {
  challenge_id: number;
  category: string;
  difficulty: string;
  scoring_type: string;
  initial_value: number;
  decay: number;
  minimum_value: number;
}

export interface WargameChallenge {
  id: number;
  name: string;
  category: string;
  difficulty: string;
  value: number;
  solves: number;
  is_published: boolean;
}

interface WargameListResponse {
  items: WargameChallenge[];
  total: number;
}

interface Props {
  compId: string;
}

export function CompetitionChallengeManager({ compId }: Props) {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();

  const [challenges, setChallenges] = useState<CompChallenge[]>([]);
  const [wargameChallenges, setWargameChallenges] = useState<WargameChallenge[]>([]);
  const [wargameLoaded, setWargameLoaded] = useState(false);
  const [wargameError, setWargameError] = useState(false);
  const [chalSearch, setChalSearch] = useState("");
  const [chalCategoryFilter, setChalCategoryFilter] = useState("all");
  const [selectedChalId, setSelectedChalId] = useState<number | null>(null);
  const [addScoringType, setAddScoringType] = useState("static");
  const [addInitialValue, setAddInitialValue] = useState(500);
  const [addDecay, setAddDecay] = useState(100);
  const [addMinimumValue, setAddMinimumValue] = useState(50);
  const [addingChallenge, setAddingChallenge] = useState(false);

  const chalNameMap = useMemo(() => {
    const map = new Map<number, WargameChallenge>();
    for (const c of wargameChallenges) map.set(c.id, c);
    return map;
  }, [wargameChallenges]);

  const addedIds = useMemo(
    () => new Set(challenges.map((c) => c.challenge_id)),
    [challenges],
  );

  const filteredWargame = useMemo(() => {
    return wargameChallenges.filter((c) => {
      if (addedIds.has(c.id)) return false;
      if (chalCategoryFilter !== "all" && c.category !== chalCategoryFilter) return false;
      if (chalSearch && !c.name.toLowerCase().includes(chalSearch.toLowerCase())) return false;
      return true;
    });
  }, [wargameChallenges, addedIds, chalCategoryFilter, chalSearch]);

  const fetchChallenges = useCallback(async () => {
    try {
      const data = await apiFetch<CompChallenge[]>(`/api/ctf/competitions/${compId}/challenges`);
      setChallenges(data);
    } catch {
      // 챌린지 목록 로드 실패는 무시
    }
  }, [compId]);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const data = await apiFetch<CompChallenge[]>(`/api/ctf/competitions/${compId}/challenges`);
        if (!cancelled) setChallenges(data);
      } catch {
        // 챌린지 목록 로드 실패는 무시
      }

      try {
        if (cancelled) return;
        setWargameError(false);
        const first = await apiFetch<WargameListResponse>(
          "/api/learn/challenges?show_all=true&size=1",
        );
        if (cancelled) return;
        const total = first.total;
        if (total <= 1) {
          setWargameChallenges(first.items);
        } else {
          const data = await apiFetch<WargameListResponse>(
            `/api/learn/challenges?show_all=true&size=${total}`,
          );
          if (!cancelled) setWargameChallenges(data.items);
        }
      } catch {
        if (!cancelled) setWargameError(true);
      } finally {
        if (!cancelled) setWargameLoaded(true);
      }
    };

    load();
    return () => { cancelled = true; };
  }, [compId]);

  const handleAddChallenge = async () => {
    if (!selectedChalId) return;
    const wc = chalNameMap.get(selectedChalId);
    if (!wc) return;
    setAddingChallenge(true);
    try {
      await apiFetch(`/api/ctf/competitions/${compId}/challenges`, {
        method: "POST",
        body: {
          challenge_id: wc.id,
          category: wc.category,
          difficulty: wc.difficulty,
          scoring_type: addScoringType,
          initial_value: addInitialValue,
          decay: addDecay,
          minimum_value: addMinimumValue,
        },
      });
      toast("success", t("challengeAdded"));
      setSelectedChalId(null);
      setAddScoringType("static");
      setAddInitialValue(500);
      setAddDecay(100);
      setAddMinimumValue(50);
      await fetchChallenges();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      setAddingChallenge(false);
    }
  };

  const handleRemoveChallenge = async (challengeId: number) => {
    try {
      await apiFetch(`/api/ctf/competitions/${compId}/challenges/${challengeId}`, {
        method: "DELETE",
      });
      toast("success", t("challengeRemoved"));
      setChallenges((prev) => prev.filter((c) => c.challenge_id !== challengeId));
    } catch (err) {
      toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    }
  };

  const selectedChal = selectedChalId ? chalNameMap.get(selectedChalId) : undefined;

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">{t("challengeListTitle")}</h2>

      <CompetitionChallengeTable
        challenges={challenges}
        chalNameMap={chalNameMap}
        onRemove={handleRemoveChallenge}
      />

      <div className="rounded-lg border border-border bg-bg-surface p-4 space-y-3">
        <h3 className="text-sm font-medium">{t("addChallenge")}</h3>

        <ChallengeSelector
          chalSearch={chalSearch}
          onSearchChange={setChalSearch}
          chalCategoryFilter={chalCategoryFilter}
          onCategoryChange={setChalCategoryFilter}
          filteredWargame={filteredWargame}
          selectedChalId={selectedChalId}
          onSelect={setSelectedChalId}
          wargameError={wargameError}
          wargameLoaded={wargameLoaded}
        />

        {selectedChal && (
          <ScoringConfig
            selectedChal={selectedChal}
            scoringType={addScoringType}
            onScoringTypeChange={setAddScoringType}
            initialValue={addInitialValue}
            onInitialValueChange={setAddInitialValue}
            decay={addDecay}
            onDecayChange={setAddDecay}
            minimumValue={addMinimumValue}
            onMinimumValueChange={setAddMinimumValue}
            onAdd={handleAddChallenge}
            adding={addingChallenge}
          />
        )}
      </div>
    </div>
  );
}
