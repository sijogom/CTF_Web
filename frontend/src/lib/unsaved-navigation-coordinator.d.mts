export type CoordinatedNavigationResult = "executed" | "pending" | "blocked";

export interface UnsavedNavigationGuardRegistration {
  isDirty: () => boolean;
  isBusy: () => boolean;
  requestConfirmation: (onConfirm: () => void, onCancel: () => void) => void;
}

export function registerUnsavedNavigationGuard(
  guard: UnsavedNavigationGuardRegistration,
): () => void;
export function getUnsavedNavigationState(): {
  dirty: boolean;
  busy: boolean;
  active: boolean;
};
export function requestCoordinatedNavigation(
  action: () => void,
  options?: { onCancel?: () => void },
): CoordinatedNavigationResult;
export function resetUnsavedNavigationCoordinatorForTests(): void;
