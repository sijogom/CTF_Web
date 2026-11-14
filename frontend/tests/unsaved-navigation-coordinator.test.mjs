import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import {
  getUnsavedNavigationState,
  registerUnsavedNavigationGuard,
  requestCoordinatedNavigation,
  resetUnsavedNavigationCoordinatorForTests,
} from "../src/lib/unsaved-navigation-coordinator.mjs";

afterEach(() => resetUnsavedNavigationCoordinatorForTests());

function guard({ dirty = true, busy = false } = {}) {
  const confirmations = [];
  return {
    confirmations,
    registration: {
      isDirty: () => dirty,
      isBusy: () => busy,
      requestConfirmation: (confirm, cancel) => confirmations.push({ confirm, cancel }),
    },
    setBusy: (value) => { busy = value; },
    setDirty: (value) => { dirty = value; },
  };
}

test("runs immediately when no draft or busy action is registered", () => {
  let calls = 0;
  assert.equal(requestCoordinatedNavigation(() => { calls += 1; }), "executed");
  assert.equal(calls, 1);
  assert.deepEqual(getUnsavedNavigationState(), { dirty: false, busy: false, active: false });
});

test("blocks external navigation while any registered action is busy", () => {
  const current = guard({ dirty: true, busy: true });
  registerUnsavedNavigationGuard(current.registration);
  let calls = 0;
  assert.equal(requestCoordinatedNavigation(() => { calls += 1; }), "blocked");
  assert.equal(calls, 0);
  assert.equal(current.confirmations.length, 0);
});

test("executes the original destination exactly once after confirmation", () => {
  const current = guard();
  registerUnsavedNavigationGuard(current.registration);
  let calls = 0;
  assert.equal(requestCoordinatedNavigation(() => { calls += 1; }), "pending");
  assert.equal(current.confirmations.length, 1);
  current.confirmations[0].confirm();
  current.confirmations[0].confirm();
  assert.equal(calls, 1);
});

test("requires each simultaneously dirty guard without opening duplicate dialogs", () => {
  const first = guard();
  const second = guard();
  registerUnsavedNavigationGuard(first.registration);
  registerUnsavedNavigationGuard(second.registration);
  let calls = 0;

  requestCoordinatedNavigation(() => { calls += 1; });
  assert.equal(first.confirmations.length, 1);
  assert.equal(second.confirmations.length, 0);
  first.confirmations[0].confirm();
  assert.equal(first.confirmations.length, 1);
  assert.equal(second.confirmations.length, 1);
  assert.equal(calls, 0);
  second.confirmations[0].confirm();
  assert.equal(calls, 1);
});

test("canceling any dirty guard preserves every draft and destination", () => {
  const first = guard();
  const second = guard();
  registerUnsavedNavigationGuard(first.registration);
  registerUnsavedNavigationGuard(second.registration);
  let firstDestinationCalls = 0;
  let replacementCalls = 0;
  let cancelCalls = 0;

  requestCoordinatedNavigation(
    () => { firstDestinationCalls += 1; },
    { onCancel: () => { cancelCalls += 1; } },
  );
  assert.equal(requestCoordinatedNavigation(() => { replacementCalls += 1; }), "pending");
  first.confirmations[0].confirm();
  second.confirmations[0].cancel();
  assert.equal(firstDestinationCalls, 0);
  assert.equal(replacementCalls, 0);
  assert.equal(cancelCalls, 1);
  assert.equal(first.confirmations.length, 1);
  assert.equal(second.confirmations.length, 1);
});
