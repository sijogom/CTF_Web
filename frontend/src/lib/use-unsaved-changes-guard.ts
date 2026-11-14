"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getUnsavedNavigationState,
  registerUnsavedNavigationGuard,
  requestCoordinatedNavigation,
  type CoordinatedNavigationResult,
} from "./unsaved-navigation-coordinator.mjs";

interface UnsavedChangesGuardOptions {
  /** Blocks external screen navigation without offering discard while a write/read transition is active. */
  busy?: boolean;
}

interface NavigationDestinationLike {
  key: string;
  url: string;
  sameDocument: boolean;
}

interface NavigateEventLike extends Event {
  navigationType: "push" | "replace" | "reload" | "traverse";
  destination: NavigationDestinationLike;
  canIntercept: boolean;
  hashChange: boolean;
}

interface NavigationResultLike {
  finished?: Promise<unknown>;
}

interface NavigationLike extends EventTarget {
  traverseTo: (key: string) => NavigationResultLike;
}

let navigationListenerUsers = 0;
let removeNavigationListeners: (() => void) | null = null;
let approvedNavigationInProgress = false;
let allowedTraverseKey: string | null = null;

function navigationApi(): NavigationLike | null {
  return (window as Window & { navigation?: NavigationLike }).navigation ?? null;
}

function runApprovedNavigation(action: () => void) {
  approvedNavigationInProgress = true;
  try {
    action();
  } finally {
    window.setTimeout(() => {
      approvedNavigationInProgress = false;
    }, 0);
  }
}

/** Runs a programmatic screen transition through every active draft guard. */
export function runGuardedNavigation(
  action: () => void,
  options?: { onCancel?: () => void },
): CoordinatedNavigationResult {
  return requestCoordinatedNavigation(() => runApprovedNavigation(action), options);
}

function retainNavigationListeners() {
  navigationListenerUsers += 1;
  if (navigationListenerUsers === 1) {
    const handleDocumentClick = (event: MouseEvent) => {
      if (
        event.defaultPrevented
        || event.button !== 0
        || event.metaKey
        || event.ctrlKey
        || event.shiftKey
        || event.altKey
        || !(event.target instanceof Element)
        || !getUnsavedNavigationState().active
      ) return;

      const anchor = event.target.closest("a[href]");
      if (!(anchor instanceof HTMLAnchorElement) || anchor.target || anchor.hasAttribute("download")) return;
      const destination = new URL(anchor.href, window.location.href);
      if (destination.origin !== window.location.origin) return;
      const current = new URL(window.location.href);
      if (
        destination.href === current.href
        || (destination.pathname === current.pathname && destination.search === current.search && destination.hash)
      ) return;

      event.preventDefault();
      runGuardedNavigation(() => window.location.assign(destination.href));
    };

    const navigation = navigationApi();
    const handleNavigate = (rawEvent: Event) => {
      const event = rawEvent as NavigateEventLike;
      if (
        event.navigationType !== "traverse"
        || !event.destination.sameDocument
        || event.hashChange
      ) return;

      const destinationKey = event.destination.key;
      if (allowedTraverseKey === destinationKey) {
        allowedTraverseKey = null;
        return;
      }

      if (!getUnsavedNavigationState().active) return;
      if (!event.cancelable || !event.canIntercept || !destinationKey || !navigation) return;

      event.preventDefault();
      runGuardedNavigation(() => {
        allowedTraverseKey = destinationKey;
        let result: NavigationResultLike;
        try {
          result = navigation.traverseTo(destinationKey);
        } catch {
          if (allowedTraverseKey === destinationKey) allowedTraverseKey = null;
          return;
        }
        void result.finished?.catch(() => undefined).finally(() => {
          if (allowedTraverseKey === destinationKey) allowedTraverseKey = null;
        });
      });
    };

    document.addEventListener("click", handleDocumentClick, true);
    navigation?.addEventListener("navigate", handleNavigate);
    removeNavigationListeners = () => {
      document.removeEventListener("click", handleDocumentClick, true);
      navigation?.removeEventListener("navigate", handleNavigate);
    };
  }

  let released = false;
  return () => {
    if (released) return;
    released = true;
    navigationListenerUsers -= 1;
    if (navigationListenerUsers === 0) {
      removeNavigationListeners?.();
      removeNavigationListeners = null;
      allowedTraverseKey = null;
    }
  };
}

/**
 * Protects in-memory drafts from browser unload, same-origin links,
 * guarded router actions, and cancelable same-document history traversal.
 * Callers keep rendering their domain-specific confirmation dialog.
 */
export function useUnsavedChangesGuard(
  dirty: boolean,
  { busy = false }: UnsavedChangesGuardOptions = {},
) {
  const [confirmDiscardOpen, setConfirmDiscardOpen] = useState(false);
  const pendingActionRef = useRef<(() => void) | null>(null);
  const pendingCancelRef = useRef<(() => void) | null>(null);
  const bypassRef = useRef(false);
  const dirtyRef = useRef(dirty);
  const busyRef = useRef(busy);
  const confirmationOpenRef = useRef(false);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    dirtyRef.current = dirty;
    busyRef.current = busy;
  }, [busy, dirty]);

  const openConfirmation = useCallback((action: () => void, onCancel: (() => void) | null) => {
    if (confirmationOpenRef.current) {
      onCancel?.();
      return;
    }
    pendingActionRef.current = action;
    pendingCancelRef.current = onCancel;
    returnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    confirmationOpenRef.current = true;
    setConfirmDiscardOpen(true);
  }, []);

  const requestDiscard = useCallback((action: () => void) => {
    if (!dirtyRef.current) {
      action();
      return;
    }
    openConfirmation(action, null);
  }, [openConfirmation]);

  const cancelDiscard = useCallback(() => {
    const onCancel = pendingCancelRef.current;
    pendingActionRef.current = null;
    pendingCancelRef.current = null;
    confirmationOpenRef.current = false;
    setConfirmDiscardOpen(false);
    onCancel?.();
    const returnFocus = returnFocusRef.current;
    returnFocusRef.current = null;
    window.requestAnimationFrame(() => {
      if (returnFocus?.isConnected && !returnFocus.closest("[inert]")) returnFocus.focus();
    });
  }, []);

  const confirmDiscard = useCallback(() => {
    const action = pendingActionRef.current;
    pendingActionRef.current = null;
    pendingCancelRef.current = null;
    returnFocusRef.current = null;
    confirmationOpenRef.current = false;
    setConfirmDiscardOpen(false);
    bypassRef.current = true;
    action?.();
    // A confirmed same-origin navigation may synchronously trigger
    // beforeunload. Keep the bypass only for that transition; if the caller
    // merely closes an in-page form, the next draft must be guarded again.
    window.setTimeout(() => {
      bypassRef.current = false;
    }, 0);
  }, []);

  useEffect(() => {
    const unregister = registerUnsavedNavigationGuard({
      isDirty: () => dirtyRef.current,
      isBusy: () => busyRef.current || confirmationOpenRef.current,
      requestConfirmation: (onConfirm, onCancel) => openConfirmation(onConfirm, onCancel),
    });
    const releaseListeners = retainNavigationListeners();
    return () => {
      unregister();
      releaseListeners();
    };
  }, [openConfirmation]);

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (
        (!dirtyRef.current && !busyRef.current)
        || bypassRef.current
        || approvedNavigationInProgress
      ) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, []);

  return { confirmDiscardOpen, requestDiscard, confirmDiscard, cancelDiscard };
}
