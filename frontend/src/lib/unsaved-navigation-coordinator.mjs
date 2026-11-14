const guards = new Map();
let pendingNavigation = null;

function activeState() {
  let dirty = false;
  let busy = false;
  for (const guard of guards.values()) {
    dirty ||= guard.isDirty();
    busy ||= guard.isBusy();
  }
  return { dirty, busy };
}

function advancePendingNavigation() {
  const pending = pendingNavigation;
  if (!pending) return;

  if (activeState().busy) {
    pendingNavigation = null;
    pending.onCancel?.();
    return;
  }

  const next = [...guards.entries()].find(
    ([id, guard]) => guard.isDirty() && !pending.approved.has(id),
  );
  if (!next) {
    pendingNavigation = null;
    pending.action();
    return;
  }

  const [id, guard] = next;
  pending.awaiting = id;
  guard.requestConfirmation(
    () => {
      if (pendingNavigation !== pending || pending.awaiting !== id) return;
      pending.approved.add(id);
      pending.awaiting = null;
      advancePendingNavigation();
    },
    () => {
      if (pendingNavigation === pending && pending.awaiting === id) {
        pendingNavigation = null;
        pending.onCancel?.();
      }
    },
  );
}

export function registerUnsavedNavigationGuard(guard) {
  const id = Symbol("unsaved-navigation-guard");
  guards.set(id, guard);
  return () => {
    guards.delete(id);
    if (pendingNavigation?.awaiting === id) {
      const pending = pendingNavigation;
      pendingNavigation = null;
      pending.onCancel?.();
    }
  };
}

export function getUnsavedNavigationState() {
  const state = activeState();
  return { ...state, active: state.dirty || state.busy };
}

export function requestCoordinatedNavigation(action, { onCancel } = {}) {
  if (pendingNavigation) return "pending";
  const state = activeState();
  if (state.busy) return "blocked";
  if (!state.dirty) {
    action();
    return "executed";
  }

  pendingNavigation = {
    action,
    onCancel,
    approved: new Set(),
    awaiting: null,
  };
  advancePendingNavigation();
  return "pending";
}

export function resetUnsavedNavigationCoordinatorForTests() {
  guards.clear();
  pendingNavigation = null;
}
