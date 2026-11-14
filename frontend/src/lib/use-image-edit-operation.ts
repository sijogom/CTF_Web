"use client";

import { useLayoutEffect, useMemo, useState } from "react";
import { useAuth } from "@/providers/AuthProvider";
import { captureApiRequestOwner, isCurrentApiRequestOwner } from "@/lib/api-client";
import { createImageEditOperation } from "@/lib/image-edit-operation.mjs";

export function useImageEditOperation(scope: string) {
  const { user } = useAuth();
  const userId = user?.id ?? "";
  const key = JSON.stringify([scope, userId]);
  const [pending, setPending] = useState<{ key: string; kind: string | null } | null>(null);
  const operation = useMemo(() => {
    const gate = createImageEditOperation({
      captureOwner: () => captureApiRequestOwner(userId),
      isCurrentOwner: isCurrentApiRequestOwner,
      onChange: (key, kind) => setPending({ key, kind }),
    });
    gate.setContext(key);
    return gate;
  }, [key, userId]);
  useLayoutEffect(() => {
    operation.mount(key);
    return () => operation.unmount(key);
  }, [key, operation]);
  const kind = pending?.key === key && operation.busy ? pending.kind : null;
  return { operation, busy: kind !== null, uploading: kind === "upload", kind, key };
}
