export interface Profile {
  username: string;
  bio: string;
  avatar_url: string | null;
  level: number;
  xp: number;
  xp_in_level: number;
  xp_for_level: number;
  xp_to_next: number;
  streak_days: number;
  total_solves: number;
  rank: number;
  join_date: string;
  discord_username?: string | null;
  baekjoon_handle?: string | null;
  baekjoon_tier?: number | null;
  tier_name?: string | null;
  tier_image?: string | null;
}

export interface CategoryStat {
  label: string;
  value: number;
  max: number;
}

export interface Badge {
  id: string;
  name: string;
  description: string;
  icon: string;
  earned: boolean;
  hidden: boolean;
  progress: number | null;
  target: number | null;
}

export interface PublicProfile {
  username: string;
  bio: string;
  avatar_url: string | null;
  level: number;
  xp: number;
  xp_in_level: number;
  xp_for_level: number;
  streak_days: number;
  rank: number;
  solves: number;
  stats: CategoryStat[];
  badges: Badge[];
  history: SolveRecord[];
  activity: Record<string, number>;
  joined_at: string | null;
  discord_username?: string | null;
  baekjoon_handle?: string | null;
  baekjoon_tier?: number | null;
  tier_name?: string | null;
  tier_image?: string | null;
}

export interface SolveRecord {
  id: string;
  name: string;
  category: string;
  value: number;
  solved_at: string;
}
