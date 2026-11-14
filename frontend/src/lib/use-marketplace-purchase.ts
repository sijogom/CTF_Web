"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, ApiError } from "@/lib/api-client";
import { parsePurchaseStatus, type PurchaseStatus } from "@/lib/marketplace-settlement.mjs";

interface Snapshot {
  identity: string;
  settlement: PurchaseStatus | null;
  points: number | null;
  busy: boolean;
  error: string | null;
}

function matchingSnapshot(snapshot: Snapshot, identity: string): Snapshot {
  return snapshot.identity === identity ? snapshot : { identity, settlement: null, points: null, busy: false, error: null };
}

export function useMarketplacePurchase(itemId: string, userId: string | undefined, errorMessage: string) {
  const identity = `${userId ?? ""}:${itemId}`;
  const identityRef = useRef(identity);
  const [snapshot, setSnapshot] = useState<Snapshot>({ identity, settlement: null, points: null, busy: false, error: null });
  const current = matchingSnapshot(snapshot, identity);
  const requestRef = useRef(0);
  const purchaseInFlight = useRef(false);

  const refreshStatus = useCallback(async (): Promise<PurchaseStatus | null> => {
    if (!userId) return null;
    const requestId = ++requestRef.current;
    try {
      const payload = await apiFetch<unknown>(`/api/community/marketplace/items/${itemId}/purchase-status`);
      const settlement = parsePurchaseStatus(payload);
      if (identityRef.current !== identity || requestId !== requestRef.current) return null;
      setSnapshot((previous) => ({ ...matchingSnapshot(previous, identity), settlement, error: null }));
      const balance = await apiFetch<{ points: number }>("/api/auth/me/points").catch(() => null);
      if (identityRef.current === identity && requestId === requestRef.current && balance !== null) {
        setSnapshot((previous) => ({ ...previous, points: balance.points }));
      }
      return identityRef.current === identity ? settlement : null;
    } catch (error) {
      if (identityRef.current === identity && requestId === requestRef.current) {
        setSnapshot((previous) => ({ ...matchingSnapshot(previous, identity), error: error instanceof ApiError ? error.message : errorMessage }));
      }
      return null;
    }
  }, [identity, itemId, userId, errorMessage]);

  useEffect(() => {
    identityRef.current = identity;
    const timer = window.setTimeout(() => void refreshStatus(), 0);
    return () => {
      window.clearTimeout(timer);
      requestRef.current += 1;
    };
  }, [refreshStatus, identity]);

  useEffect(() => {
    if (!userId || current.settlement?.status !== "processing") return;
    let remaining = 12;
    const timer = window.setInterval(() => {
      remaining -= 1;
      void refreshStatus();
      if (remaining <= 0) window.clearInterval(timer);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [userId, current.settlement?.status, refreshStatus]);

  const purchase = useCallback(async (expectedPrice?: number) => {
    if (!userId || purchaseInFlight.current) return null;
    purchaseInFlight.current = true;
    requestRef.current += 1;
    setSnapshot((previous) => ({ ...matchingSnapshot(previous, identity), busy: true, error: null }));
    try {
      const payload = await apiFetch<unknown>(`/api/community/marketplace/items/${itemId}/purchase`, {
        method: "POST", body: expectedPrice === undefined ? undefined : { expected_price: expectedPrice },
      });
      const settlement = parsePurchaseStatus(payload);
      if (identityRef.current !== identity) return null;
      setSnapshot((previous) => ({ ...previous, settlement }));
      await refreshStatus();
      return settlement;
    } catch (error) {
      // POST 응답 유실은 실패로 확정하지 않고 서버의 동일 구매 작업을 다시 조회한다.
      const confirmed = await refreshStatus();
      if (confirmed?.status === "completed" || confirmed?.status === "processing") return confirmed;
      if (identityRef.current === identity) {
        setSnapshot((previous) => ({ ...previous, error: error instanceof ApiError ? error.message : errorMessage }));
      }
      return null;
    } finally {
      purchaseInFlight.current = false;
      if (identityRef.current === identity) setSnapshot((previous) => ({ ...previous, busy: false }));
    }
  }, [identity, itemId, userId, errorMessage, refreshStatus]);

  return {
    purchased: current.settlement?.purchased === true,
    purchasing: current.busy,
    settlement: current.settlement,
    myPoints: current.points,
    purchaseError: current.error,
    refreshStatus,
    purchase,
  };
}
