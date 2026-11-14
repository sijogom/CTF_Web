"use client";

import { useState, useRef, useEffect } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";
import { apiFetch, ApiError, captureApiRequestOwner, isCurrentApiRequestOwner, type ApiRequestOwner } from "@/lib/api-client";
import { importChallengeFile, InvalidChallengeImportFile, type ChallengeImportResult } from "@/lib/challenge-import.mjs";
import { useToast } from "@/providers/ToastProvider";
import { useAuth } from "@/providers/AuthProvider";

interface DataOperation {
  kind: "import" | "export";
  generation: number;
  owner: ApiRequestOwner;
  controller: AbortController;
}

export default function DataPage() {
  const t = useTranslations("admin");
  const tc = useTranslations("common");
  const { toast } = useToast();
  const { user, isSuperAdmin } = useAuth();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const operationRef = useRef<DataOperation | null>(null);
  const mountedRef = useRef(false);
  const requestGenerationRef = useRef(0);
  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importMode, setImportMode] = useState<"skip" | "overwrite">("skip");
  const [lastResult, setLastResult] = useState<ChallengeImportResult | null>(null);
  const [importNotice, setImportNotice] = useState<string | null>(null);
  const busy = importing || exporting;

  useEffect(() => {
    mountedRef.current = true;
    setImporting(false);
    setExporting(false);
    setLastResult(null);
    setImportNotice(null);
    return () => {
      mountedRef.current = false;
      requestGenerationRef.current += 1;
      operationRef.current?.controller.abort();
      operationRef.current = null;
    };
  }, [user?.id]);

  const beginOperation = (kind: DataOperation["kind"]): DataOperation | null => {
    if (!mountedRef.current || operationRef.current || !isSuperAdmin || !user?.id) return null;
    try {
      const owner = captureApiRequestOwner(user.id);
      if (!isCurrentApiRequestOwner(owner)) return null;
      const operation = { kind, generation: requestGenerationRef.current, owner, controller: new AbortController() };
      operationRef.current = operation;
      return operation;
    } catch {
      return null;
    }
  };

  const isCurrentOperation = (operation: DataOperation) => {
    const current = mountedRef.current && operationRef.current === operation && operation.generation === requestGenerationRef.current
      && !operation.controller.signal.aborted && isCurrentApiRequestOwner(operation.owner);
    if (!current) operation.controller.abort();
    return current;
  };

  const finishOperation = (operation: DataOperation) => {
    if (operationRef.current !== operation) return;
    operationRef.current = null;
    if (mountedRef.current && operation.generation === requestGenerationRef.current) {
      setImporting(false);
      setExporting(false);
    }
  };

  const handleExport = async () => {
    const operation = beginOperation("export");
    if (!operation) return;
    setExporting(true);
    try {
      const data = await apiFetch<{ version: number; challenges: unknown[] }>(
        "/api/learn/challenges/export",
        { requestOwner: operation.owner, signal: operation.controller.signal },
      );
      if (!isCurrentOperation(operation)) return;
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `challenges-export-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      if (isCurrentOperation(operation)) toast("success", t("exportSuccess"));
    } catch (err) {
      if (isCurrentOperation(operation)) toast("error", err instanceof ApiError ? err.message : tc("errorOccurred"));
    } finally {
      finishOperation(operation);
    }
  };

  const handleImport = async (file: File) => {
    const operation = beginOperation("import");
    if (!operation) return;
    setImporting(true);
    setLastResult(null);
    setImportNotice(null);
    try {
      const result = await importChallengeFile(file, importMode, (payload) => {
        if (!isCurrentOperation(operation)) throw new Error("data_operation_stale");
        return apiFetch<unknown>("/api/learn/challenges/import/v2", {
          method: "POST",
          body: payload,
          requestOwner: operation.owner,
          signal: operation.controller.signal,
        });
      });
      if (!isCurrentOperation(operation)) return;
      setLastResult(result);
      toast("success", t("importSuccess", { created: result.created, updated: result.updated, skipped: result.skipped }));
    } catch (err) {
      if (!isCurrentOperation(operation)) return;
      if (err instanceof InvalidChallengeImportFile) {
        toast("error", t("importInvalidFile"));
      } else if (err instanceof ApiError) {
        if (err.code === "import_outcome_unknown") setImportNotice(err.message);
        toast(err.code === "import_outcome_unknown" ? "warning" : "error", err.message);
      } else {
        const message = t("importOutcomeUnknown");
        setImportNotice(message);
        toast("warning", message);
      }
    } finally {
      finishOperation(operation);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <h1 className="text-2xl font-bold">{t("dataManagement")}</h1>

      {/* Export */}
      <section className="rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-2 text-lg font-semibold">{t("exportTitle")}</h2>
        <p className="mb-4 text-sm text-text-muted">{t("exportDesc")}</p>
        <Button onClick={handleExport} disabled={busy || !isSuperAdmin}>
          {exporting ? tc("processing") : t("exportBtn")}
        </Button>
      </section>

      {/* Import */}
      <section className="rounded-xl border border-border bg-bg-surface p-6">
        <h2 className="mb-2 text-lg font-semibold">{t("importTitle")}</h2>
        <p className="mb-4 text-sm text-text-muted">{t("importDesc")}</p>

        <div className="mb-4 flex items-center gap-4">
          <label className="text-sm font-medium">{t("importMode")}:</label>
          <label className="flex items-center gap-1.5 text-sm">
            <input
              type="radio"
              name="importMode"
              checked={importMode === "skip"}
              onChange={() => { if (!operationRef.current) setImportMode("skip"); }}
              disabled={busy || !isSuperAdmin}
              className="accent-accent-primary"
            />
            {t("importModeSkip")}
          </label>
          <label className="flex items-center gap-1.5 text-sm">
            <input
              type="radio"
              name="importMode"
              checked={importMode === "overwrite"}
              onChange={() => { if (!operationRef.current) setImportMode("overwrite"); }}
              disabled={busy || !isSuperAdmin}
              className="accent-accent-primary"
            />
            {t("importModeOverwrite")}
          </label>
        </div>

        <input
          ref={fileInputRef}
          type="file"
          accept=".json"
          disabled={busy || !isSuperAdmin}
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void handleImport(file);
            e.target.value = "";
          }}
        />
        <Button
          onClick={() => { if (!operationRef.current) fileInputRef.current?.click(); }}
          disabled={busy || !isSuperAdmin}
          variant="secondary"
        >
          {importing ? tc("processing") : t("importBtn")}
        </Button>

        {lastResult && (
          <div className="mt-4 rounded-lg border border-accent-success/30 bg-accent-success/5 p-3 text-sm">
            {t("importResult", { created: lastResult.created, updated: lastResult.updated, skipped: lastResult.skipped })}
          </div>
        )}
        {importNotice && <p role="status" className="mt-4 text-sm text-accent-warning">{importNotice}</p>}
      </section>
    </div>
  );
}
