import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

export type CompetitionMode = "individual" | "team";
export type CompetitionTeamContract = "fixed_roster_v1" | null;

export interface CompetitionTeamRegistration {
  team_id: string;
  team_name: string;
  registered_at: string;
  members: { user_id: string; username: string }[];
}

export interface CaptainTeam {
  id: string;
  name: string;
  captain_id: string;
  member_count: number;
}

export interface CompetitionSummary {
  id: string;
  title: string;
  description: string | null;
  mode: CompetitionMode;
  team_contract?: CompetitionTeamContract;
  start_at: string;
  end_at: string;
  freeze_minutes: number;
  banner_url: string | null;
  participant_count: number;
  status: string;
}

export type CompetitionEnvironmentStatus = "not_required" | "ready" | "legacy_unverified" | "verification_required";

export interface CompetitionChallenge {
  id: number;
  name: string;
  category: string;
  value: number;
  solved: boolean;
  team_solved?: boolean;
  first_blood?: string | null;
  description?: string;
  env_type?: string;
  container_port?: number | null;
  environment_ready?: boolean;
  environment_status?: CompetitionEnvironmentStatus;
}

export interface CompetitionAssignmentCategory {
  category: string;
  assigned: boolean;
  available_count: number;
}

export interface CompetitionDetail extends CompetitionSummary {
  is_published: boolean;
  joined: boolean;
  team_registration?: CompetitionTeamRegistration | null;
  my_challenges: { category: string; challenge_ids: number[]; team_id?: string }[];
  challenges: CompetitionChallenge[];
  visible_challenges?: CompetitionChallenge[];
  assignment?: {
    state: "not_joined" | "select_category" | "assigned";
    categories: CompetitionAssignmentCategory[];
  };
}

export interface CompetitionChallengeDetail {
  id: number;
  name: string;
  description: string;
  files: { id: number; name: string; download_path: string }[];
  attachments?: ChallengeAttachmentDescriptor[];
  hints: { id: number; cost: number }[];
  env_type: string;
  container_port: number | null;
  environment_ready: boolean;
  environment_status: CompetitionEnvironmentStatus;
}

export interface CompetitionScoreEntry {
  rank: number;
  username?: string;
  team_id?: string;
  team_name?: string;
  score: number;
  solves: number;
  first_bloods?: number;
}

export interface CompetitionScoreboard {
  team_contract?: CompetitionTeamContract;
  entries: CompetitionScoreEntry[];
  is_frozen: boolean;
}

export interface CompetitionGraphSeries {
  username?: string;
  team_id?: string;
  team_name?: string;
  data: { t: string; score: number }[];
}

export interface CompetitionGraph {
  team_contract?: CompetitionTeamContract;
  series: CompetitionGraphSeries[];
}
