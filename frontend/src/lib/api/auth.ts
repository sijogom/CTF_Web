import { apiFetch } from "../api-client";

export interface UserProfile {
  id: string;
  username: string;
  email: string;
  role: string;
  level: number;
  xp: number;
  avatar_url: string | null;
  bio: string | null;
  email_verified: boolean;
  totp_enabled: boolean;
}

export async function getMe(): Promise<UserProfile> {
  return apiFetch<UserProfile>("/api/auth/me");
}

export async function login(
  email: string,
  password: string
): Promise<{ token_type: string; expires_in: number }> {
  return apiFetch("/api/auth/login", {
    method: "POST",
    body: { email, password },
  });
}

export async function register(
  email: string,
  username: string,
  password: string
): Promise<{ id: string; username: string }> {
  return apiFetch("/api/auth/register", {
    method: "POST",
    body: { email, username, password },
  });
}

export async function refreshToken(): Promise<{
  token_type: string;
  expires_in: number;
}> {
  return apiFetch("/api/auth/refresh", {
    method: "POST",
  });
}

export async function logout(): Promise<void> {
  await apiFetch("/api/auth/logout", {
    method: "POST",
  });
}
