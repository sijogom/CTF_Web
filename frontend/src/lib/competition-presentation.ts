import type { CaptainTeam, CompetitionGraphSeries, CompetitionMode, CompetitionScoreEntry, CompetitionTeamContract } from "@/types/competition";

export function isFixedRosterCompetition(value: { team_contract?: CompetitionTeamContract }): boolean {
  return value.team_contract === "fixed_roster_v1";
}

export function captainTeams(teams: CaptainTeam[], userId: string): CaptainTeam[] {
  return teams.filter((team) => team.captain_id === userId);
}

export function competitionModeChangeAllowed(original: CompetitionMode, next: CompetitionMode,
  status: string, participantCount: number, contract: CompetitionTeamContract): boolean {
  return original === next || status === "upcoming" && participantCount === 0
    && !(contract === "fixed_roster_v1" && next === "individual");
}

export function competitionEntryIdentity(entry: CompetitionScoreEntry | CompetitionGraphSeries, team: boolean): string {
  const value = team ? entry.team_id : entry.username;
  if (!value || team && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    throw new Error("invalid_competition_participant");
  }
  return `${team ? "team" : "user"}_${Array.from(team ? value.toLowerCase() : value, (character) => character.codePointAt(0)!.toString(16)).join("_")}`;
}

export function competitionEntryLabel(entry: CompetitionScoreEntry | CompetitionGraphSeries, team: boolean): string {
  const name = team ? entry.team_name : entry.username;
  if (typeof name !== "string" || !name) throw new Error("invalid_competition_participant");
  return name;
}

export function validateCompetitionEntries(entries: (CompetitionScoreEntry | CompetitionGraphSeries)[], team: boolean): void {
  const identities = entries.map((entry) => {
    competitionEntryLabel(entry, team);
    return competitionEntryIdentity(entry, team);
  });
  if (new Set(identities).size !== identities.length) throw new Error("duplicate_competition_participant");
}

export function mergeCompetitionSeries(series: CompetitionGraphSeries[], team: boolean) {
  validateCompetitionEntries(series, team);
  const times = new Set<number>();
  const indexed = series.map((entry) => {
    const values = new Map<number, number>();
    for (const point of entry.data) {
      const time = new Date(point.t).getTime();
      if (!Number.isFinite(time) || !Number.isFinite(point.score)) throw new Error("invalid_competition_graph");
      times.add(time);
      values.set(time, point.score);
    }
    return { id: competitionEntryIdentity(entry, team), values, last: 0 };
  });
  return [...times].sort((a, b) => a - b).map((time) => {
    const point: Record<string, number> = { time };
    for (const entry of indexed) {
      entry.last = entry.values.get(time) ?? entry.last;
      point[entry.id] = entry.last;
    }
    return point;
  });
}

