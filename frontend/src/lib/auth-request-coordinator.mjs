/**
 * @typedef {"authenticated" | "anonymous" | "unknown"} AuthSessionStatus
 * @typedef {{ status: AuthSessionStatus, epoch: number, authenticated: boolean, ownerId: string | null }} AuthSessionSnapshot
 * @typedef {{ token: symbol, epoch: number, ownerId: string }} AuthRequestOwner
 * @typedef {{
 *   snapshot: () => AuthSessionSnapshot,
 *   verify: (expectedEpoch: number) => Promise<AuthSessionStatus>,
 * }} AuthSessionBridge
 */

export class AuthRequestBlockedError extends Error {
  /** @param {"anonymous" | "unavailable" | "changed"} reason */
  constructor(reason) {
    super(`Authenticated request blocked: ${reason}`);
    this.name = "AuthRequestBlockedError";
    this.reason = reason;
  }
}

export async function verifySessionAfterActiveLogin({
  activeLogin,
  expectedEpoch,
  currentEpoch,
  verify,
}) {
  if (!activeLogin) return verify(expectedEpoch);
  try {
    const result = await activeLogin;
    return currentEpoch() === expectedEpoch ? result : "unknown";
  } catch {
    return "unknown";
  }
}

function throwIfAborted(signal) {
  if (!signal?.aborted) return;
  if (signal.reason !== undefined) throw signal.reason;
  const error = new Error("The request was aborted.");
  error.name = "AbortError";
  throw error;
}

export function createAuthRequestCoordinator() {
  /** @type {{ token: symbol, bridge: AuthSessionBridge } | null} */
  let registration = null;

  const register = (bridge) => {
    const current = { token: Symbol("auth-request-bridge"), bridge };
    registration = current;
    return () => {
      if (registration?.token === current.token) registration = null;
    };
  };

  const requireSameAuthenticatedSession = async (current, expectedEpoch) => {
    const status = await current.bridge.verify(expectedEpoch);
    const latest = current.bridge.snapshot();
    if (registration?.token !== current.token) {
      throw new AuthRequestBlockedError("changed");
    }
    if (status === "anonymous" || latest.status === "anonymous") {
      throw new AuthRequestBlockedError("anonymous");
    }
    if (latest.epoch !== expectedEpoch) {
      throw new AuthRequestBlockedError("changed");
    }
    if (status !== "authenticated" || latest.status !== "authenticated" || latest.authenticated !== true) {
      throw new AuthRequestBlockedError("unavailable");
    }
  };

  const assertEpoch = (current, expectedEpoch, expectedOwnerId) => {
    const latest = current.bridge.snapshot();
    if (
      registration?.token !== current.token
      || latest.epoch !== expectedEpoch
      || (expectedOwnerId !== undefined && latest.ownerId !== expectedOwnerId)
    ) {
      throw new AuthRequestBlockedError("changed");
    }
  };

  /** @param {string} expectedOwnerId @returns {AuthRequestOwner} */
  const captureOwner = (expectedOwnerId) => {
    const current = registration;
    const initial = current?.bridge.snapshot();
    if (!current || !initial) throw new AuthRequestBlockedError("unavailable");
    if (!initial.authenticated) {
      throw new AuthRequestBlockedError(initial.status === "anonymous" ? "anonymous" : "unavailable");
    }
    if (initial.ownerId !== expectedOwnerId) throw new AuthRequestBlockedError("changed");
    return Object.freeze({ token: current.token, epoch: initial.epoch, ownerId: expectedOwnerId });
  };

  /** @param {AuthRequestOwner} owner */
  const isCurrentOwner = (owner) => {
    const latest = registration?.bridge.snapshot();
    return registration?.token === owner.token && latest?.epoch === owner.epoch
      && latest.authenticated === true && latest.ownerId === owner.ownerId;
  };

  /**
   * @template [T=Response]
   * @param {{ execute: () => Promise<Response>, signal?: AbortSignal | null, bypass?: boolean,
   *   requestOwner?: AuthRequestOwner, consume?: (response: Response) => T | Promise<T> }} options
   * @returns {Promise<T>}
   */
  const run = async ({ execute, signal, bypass = false, requestOwner, consume = (response) => response }) => {
    const current = registration;
    const initial = current?.bridge.snapshot();
    const sessionBound = requestOwner !== undefined || (!bypass && current !== null && initial?.authenticated === true);
    const expectedEpoch = requestOwner?.epoch ?? initial?.epoch;
    const expectedOwnerId = requestOwner?.ownerId ?? initial?.ownerId;
    const assertCurrent = () => {
      if (!sessionBound) return;
      if (!current || (requestOwner && requestOwner.token !== current.token)) {
        throw new AuthRequestBlockedError("changed");
      }
      assertEpoch(current, expectedEpoch, expectedOwnerId);
    };

    throwIfAborted(signal);
    assertCurrent();
    if (sessionBound) {
      await requireSameAuthenticatedSession(current, expectedEpoch);
    }
    throwIfAborted(signal);
    assertCurrent();

    let response = await execute();
    assertCurrent();
    if (sessionBound && response.status === 401) {
      throwIfAborted(signal);
      await requireSameAuthenticatedSession(current, expectedEpoch);
      throwIfAborted(signal);
      assertCurrent();
      response = await execute();
      assertCurrent();
    }

    // JSON 해석 중에도 계정은 바뀔 수 있다. 실패 본문도 같은 소유자 경계로 폐기한다.
    try {
      const result = await consume(response);
      throwIfAborted(signal);
      assertCurrent();
      return result;
    } catch (error) {
      assertCurrent();
      throw error;
    }
  };

  return { register, run, captureOwner, isCurrentOwner };
}

export const authRequestCoordinator = createAuthRequestCoordinator();
