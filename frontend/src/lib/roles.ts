export const ROLES = {
  USER: "user",
  MODERATOR: "moderator",
  ADMIN: "admin",
} as const;

export type UserRole = (typeof ROLES)[keyof typeof ROLES];

export function isAdminRole(role: string | undefined | null): boolean {
  return role === ROLES.ADMIN || role === ROLES.MODERATOR;
}

export function isSuperAdminRole(role: string | undefined | null): boolean {
  return role === ROLES.ADMIN;
}
