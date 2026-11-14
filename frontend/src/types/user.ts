export interface UserItem {
  id: string;
  username: string;
  email: string;
  role: string;
  is_active: boolean;
  email_verified: boolean;
  tier_override: string | null;
  created_at: string | null;
}

export interface UsersResponse {
  total: number;
  page: number;
  size: number;
  items: UserItem[];
}
