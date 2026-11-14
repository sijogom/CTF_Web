import type {
  LearningPrerequisite,
  PrerequisiteDetail,
  RecommendedPrerequisite,
} from "@/types/knowledge";
import type { ChallengeAttachmentDescriptor } from "@/types/challenge-attachment";

export interface Challenge {
  id: number;
  name: string;
  category: string;
  difficulty?: "beginner" | "easy" | "medium_low" | "medium" | "medium_high" | "hard" | "expert" | "impossible";
  value: number;
  xp?: number;
  solves: number;
  tags: string[];
  author?: string;
  thumbnail_url?: string | null;
  prerequisite_ids?: number[];
  locked?: boolean;
  solved?: boolean;
  knowledge_tags?: string[];
  learning_prerequisites_detail?: LearningPrerequisite[];
  prerequisite_details?: PrerequisiteDetail[];
  recommended_prerequisites?: RecommendedPrerequisite[];
}

export interface ChallengeDetail extends Challenge {
  description: string;
  files: string[];
  attachments?: ChallengeAttachmentDescriptor[];
  attachment_revision?: number;
  hints: { id: number; cost: number }[];
  max_attempts: number;
  env_type: "none" | "dockerfile" | "image";
  container_port: number;
  environment_ready?: boolean;
  environment_status?: "not_required" | "ready" | "legacy_unverified" | "verification_required";
  prerequisite_ids?: number[];
  locked?: boolean;
  knowledge_tags?: string[];
  learning_prerequisites_detail?: LearningPrerequisite[];
  prerequisite_details?: PrerequisiteDetail[];
  recommended_prerequisites?: RecommendedPrerequisite[];
}

export interface AdminChallenge {
  id: number;
  name: string;
  category: string;
  difficulty?: string;
  value: number;
  solves: number;
  challenge_type: string;
  is_published: boolean;
}

export interface SubmitResult {
  status: "correct" | "incorrect" | "already_solved" | "score_sync_pending" | "error";
  message: string;
}

export interface EnvInstance {
  id: string;
  port: number;
  status: string;
  expires_at: string;
  extend_count?: number;
  max_extend_count?: number;
}
