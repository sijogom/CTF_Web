import assert from "node:assert/strict";
import test from "node:test";

import {
  AuthRequestBlockedError,
  createAuthRequestCoordinator,
  verifySessionAfterActiveLogin,
} from "../src/lib/auth-request-coordinator.mjs";

function response(status) {
  return { status };
}

function authenticatedBridge(overrides = {}) {
  const state = {
    status: "authenticated",
    epoch: 1,
    authenticated: true,
  };
  return {
    state,
    bridge: {
      snapshot: () => ({ ...state }),
      verify: async () => state.status,
      ...overrides,
    },
  };
}

test("checks the known session before sending and reads state prepared by that check", async () => {
  const coordinator = createAuthRequestCoordinator();
  let csrf = "stale";
  const order = [];
  const { bridge } = authenticatedBridge({
    verify: async () => {
      order.push("verify");
      csrf = "fresh";
      return "authenticated";
    },
  });
  coordinator.register(bridge);

  const result = await coordinator.run({
    execute: async () => {
      order.push(`send:${csrf}`);
      return response(200);
    },
  });

  assert.equal(result.status, 200);
  assert.deepEqual(order, ["verify", "send:fresh"]);
});

test("recovers one post-check 401 once and never loops on a second 401", async () => {
  const coordinator = createAuthRequestCoordinator();
  let checks = 0;
  let sends = 0;
  const { bridge } = authenticatedBridge({
    verify: async () => {
      checks += 1;
      return "authenticated";
    },
  });
  coordinator.register(bridge);

  const result = await coordinator.run({
    execute: async () => {
      sends += 1;
      return response(401);
    },
  });

  assert.equal(result.status, 401);
  assert.equal(checks, 2);
  assert.equal(sends, 2);
});

test("concurrent requests can share the provider's single in-flight session check", async () => {
  const coordinator = createAuthRequestCoordinator();
  let starts = 0;
  let inFlight = null;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { bridge } = authenticatedBridge({
    verify: () => {
      if (!inFlight) {
        starts += 1;
        inFlight = gate.then(() => "authenticated");
      }
      return inFlight;
    },
  });
  coordinator.register(bridge);

  let sends = 0;
  const first = coordinator.run({ execute: async () => { sends += 1; return response(200); } });
  const second = coordinator.run({ execute: async () => { sends += 1; return response(200); } });
  await Promise.resolve();
  assert.equal(starts, 1);
  assert.equal(sends, 0);
  release();
  await Promise.all([first, second]);
  assert.equal(sends, 2);
});

test("waits for an active login and rejects verification from a replaced account epoch", async () => {
  let releaseLogin;
  const activeLogin = new Promise((resolve) => { releaseLogin = resolve; });
  let epoch = 4;
  let directChecks = 0;
  const resultPromise = verifySessionAfterActiveLogin({
    activeLogin,
    expectedEpoch: 4,
    currentEpoch: () => epoch,
    verify: async () => { directChecks += 1; return "authenticated"; },
  });

  await Promise.resolve();
  assert.equal(directChecks, 0);
  epoch = 5;
  releaseLogin("authenticated");
  assert.equal(await resultPromise, "unknown");
  assert.equal(directChecks, 0);
});

test("uses the normal session check when no login is active and blocks a failed login", async () => {
  let directChecks = 0;
  const direct = await verifySessionAfterActiveLogin({
    activeLogin: null,
    expectedEpoch: 7,
    currentEpoch: () => 7,
    verify: async (epoch) => { directChecks += 1; return epoch === 7 ? "authenticated" : "unknown"; },
  });
  assert.equal(direct, "authenticated");
  assert.equal(directChecks, 1);

  const failed = verifySessionAfterActiveLogin({
    activeLogin: Promise.reject(new Error("login failed")),
    expectedEpoch: 7,
    currentEpoch: () => 7,
    verify: async () => "authenticated",
  });
  assert.equal(await failed, "unknown");
});

test("anonymous public requests remain available without a session check", async () => {
  const coordinator = createAuthRequestCoordinator();
  let checks = 0;
  coordinator.register({
    snapshot: () => ({ status: "anonymous", epoch: 2, authenticated: false }),
    verify: async () => { checks += 1; return "anonymous"; },
  });

  const result = await coordinator.run({ execute: async () => response(200) });
  assert.equal(result.status, 200);
  assert.equal(checks, 0);
});

test("distinguishes a missing session from an unavailable check and never sends", async () => {
  for (const [status, reason] of [["anonymous", "anonymous"], ["unknown", "unavailable"]]) {
    const coordinator = createAuthRequestCoordinator();
    let sends = 0;
    const { state, bridge } = authenticatedBridge({
      verify: async () => {
        state.status = status;
        if (status === "anonymous") {
          state.authenticated = false;
          state.epoch += 1;
        }
        return status;
      },
    });
    coordinator.register(bridge);

    await assert.rejects(
      coordinator.run({ execute: async () => { sends += 1; return response(200); } }),
      (error) => error instanceof AuthRequestBlockedError && error.reason === reason,
    );
    assert.equal(sends, 0);
  }
});

test("blocks an operation whose account epoch changes while verification is pending", async () => {
  const coordinator = createAuthRequestCoordinator();
  let sends = 0;
  const { state, bridge } = authenticatedBridge({
    verify: async () => {
      state.epoch += 1;
      return "authenticated";
    },
  });
  coordinator.register(bridge);

  await assert.rejects(
    coordinator.run({ execute: async () => { sends += 1; return response(200); } }),
    (error) => error instanceof AuthRequestBlockedError && error.reason === "changed",
  );
  assert.equal(sends, 0);
});

test("discards an old response when logout changes the epoch after the send starts", async () => {
  const coordinator = createAuthRequestCoordinator();
  const { state, bridge } = authenticatedBridge();
  coordinator.register(bridge);

  await assert.rejects(
    coordinator.run({
      execute: async () => {
        state.epoch += 1;
        state.status = "anonymous";
        state.authenticated = false;
        return response(401);
      },
    }),
    (error) => error instanceof AuthRequestBlockedError && error.reason === "changed",
  );
});

test("does not retry a 403 and does not send after cancellation during verification", async () => {
  const coordinator = createAuthRequestCoordinator();
  let checks = 0;
  let sends = 0;
  const { bridge } = authenticatedBridge({
    verify: async () => { checks += 1; return "authenticated"; },
  });
  coordinator.register(bridge);

  const forbidden = await coordinator.run({
    execute: async () => { sends += 1; return response(403); },
  });
  assert.equal(forbidden.status, 403);
  assert.equal(checks, 1);
  assert.equal(sends, 1);

  const networkError = new Error("offline");
  await assert.rejects(
    coordinator.run({
      execute: async () => { sends += 1; throw networkError; },
    }),
    (error) => error === networkError,
  );
  assert.equal(checks, 2);
  assert.equal(sends, 2);

  const controller = new AbortController();
  const abortError = new Error("cancelled");
  const secondCoordinator = createAuthRequestCoordinator();
  const second = authenticatedBridge({
    verify: async () => {
      controller.abort(abortError);
      return "authenticated";
    },
  });
  secondCoordinator.register(second.bridge);
  await assert.rejects(
    secondCoordinator.run({
      signal: controller.signal,
      execute: async () => { sends += 1; return response(200); },
    }),
    (error) => error === abortError,
  );
  assert.equal(sends, 2);
});
