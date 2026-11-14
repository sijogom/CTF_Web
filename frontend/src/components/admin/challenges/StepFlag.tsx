"use client";

import { useTranslations } from "next-intl";
import {
  createDefaultDynamicFlagPolicy,
  type ChallengeFlagType,
  type ChallengeFormData,
  type FlagEntry,
} from "./ChallengeWizard";

interface Props {
  data: ChallengeFormData;
  onChange: (partial: Partial<ChallengeFormData>) => void;
}

const EMPTY_FLAG: FlagEntry = { type: "static", value: "", caseSensitive: true };

export function StepFlag({ data, onChange }: Props) {
  const t = useTranslations("challenge");
  const useMultiFlag = data.flagType !== "dynamic_instance" && data.flags.length > 0;

  const handleAddFlag = () => {
    onChange({ flags: [...data.flags, { ...EMPTY_FLAG }] });
  };

  const handleRemoveFlag = (index: number) => {
    onChange({ flags: data.flags.filter((_, itemIndex) => itemIndex !== index) });
  };

  const handleUpdateFlag = (index: number, partial: Partial<FlagEntry>) => {
    onChange({
      flags: data.flags.map((flag, itemIndex) => (
        itemIndex === index ? { ...flag, ...partial } : flag
      )),
    });
  };

  const handleFlagTypeChange = (flagType: ChallengeFlagType) => {
    if (flagType === "dynamic_instance") {
      onChange({
        flagType,
        flagValue: "",
        flags: [],
        dynamicFlag: data.dynamicFlag ?? createDefaultDynamicFlagPolicy(),
        // 동적 플래그는 컨테이너가 필수다. 작성자는 환경 단계에서 image 모드로 바꿀 수 있다.
        envType: data.envType === "none" ? "dockerfile" : data.envType,
      });
      return;
    }

    onChange({
      flagType,
      dynamicFlag: null,
    });
  };

  const handleSwitchToMulti = () => {
    if (data.flagValue.trim()) {
      onChange({
        flags: [{ type: data.flagType as "static" | "regex", value: data.flagValue, caseSensitive: true }],
      });
      return;
    }
    onChange({ flags: [{ ...EMPTY_FLAG }] });
  };

  const handleSwitchToSingle = () => {
    const first = data.flags[0];
    if (first) {
      onChange({
        flags: [],
        flagType: first.type,
        flagValue: first.value,
      });
      return;
    }
    onChange({ flags: [] });
  };

  const dynamicPolicy = data.dynamicFlag ?? createDefaultDynamicFlagPolicy();

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{t("flagSetting")}</h2>
        {data.flagType !== "dynamic_instance" && (
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={useMultiFlag ? handleSwitchToSingle : handleSwitchToMulti}
              className="text-xs text-accent-primary hover:underline"
            >
              {useMultiFlag ? t("singleFlagMode") : t("multiFlagMode")}
            </button>
            {useMultiFlag && (
              <button
                type="button"
                onClick={() => handleFlagTypeChange("dynamic_instance")}
                className="text-xs text-accent-primary hover:underline"
              >
                {t("flagTypeDynamic")}
              </button>
            )}
          </div>
        )}
      </div>

      {data.flagType === "dynamic_instance" ? (
        <section className="space-y-4 rounded-xl border border-accent-primary/40 bg-accent-primary/5 p-5" aria-labelledby="dynamic-flag-title">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h3 id="dynamic-flag-title" className="text-base font-semibold text-text-primary">
                {t("flagTypeDynamic")}
              </h3>
              <p className="mt-1 text-sm leading-6 text-text-secondary">
                {t("dynamicFlagDescription")}
              </p>
            </div>
            <div className="flex flex-wrap gap-3" role="radiogroup" aria-label={t("flagType")}>
              {(["static", "regex", "dynamic_instance"] as const).map((flagType) => (
                <label key={flagType} className="flex cursor-pointer items-center gap-1.5 text-xs">
                  <input
                    type="radio"
                    name="dynamic-flag-type"
                    checked={data.flagType === flagType}
                    onChange={() => handleFlagTypeChange(flagType)}
                    className="h-3.5 w-3.5"
                  />
                  {flagType === "static"
                    ? t("flagTypeStatic")
                    : flagType === "regex"
                      ? t("flagTypeRegex")
                      : t("flagTypeDynamic")}
                </label>
              ))}
            </div>
          </div>

          <div className="rounded-lg border border-border bg-bg-primary p-4 text-sm leading-6 text-text-secondary">
            <p>{t("dynamicFlagDockerDefault")}</p>
            <p className="mt-2">{t("dynamicFlagNoBuildFile")}</p>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <label htmlFor="dynamic-flag-ttl" className="mb-1.5 block text-sm font-medium">
                {t("dynamicFlagTtlLabel")}
              </label>
              <input
                id="dynamic-flag-ttl"
                type="number"
                min={60}
                max={86400}
                step={60}
                value={dynamicPolicy.ttlSeconds}
                onChange={(event) => {
                  const ttlSeconds = Number(event.target.value);
                  onChange({
                    dynamicFlag: {
                      ...dynamicPolicy,
                      ttlSeconds: Number.isFinite(ttlSeconds) ? ttlSeconds : 0,
                    },
                  });
                }}
                className="w-full rounded-lg border border-border bg-bg-surface px-3 py-2 font-mono text-sm focus:border-accent-primary focus:outline-none"
              />
              <p className="mt-1 text-xs text-text-muted">{t("dynamicFlagTtlHint")}</p>
            </div>

            <div>
              <p className="mb-1.5 text-sm font-medium">{t("dynamicFlagRuntimePath")}</p>
              <code className="block rounded-lg border border-border bg-bg-surface px-3 py-2 font-mono text-sm text-accent-primary">
                {dynamicPolicy.runtimePath}
              </code>
            </div>

            <div>
              <p className="mb-1.5 text-sm font-medium">{t("dynamicFlagKeyVersion")}</p>
              <code className="block rounded-lg border border-border bg-bg-surface px-3 py-2 font-mono text-sm text-text-secondary">
                {dynamicPolicy.hmacKeyVersion}
              </code>
            </div>
          </div>
        </section>
      ) : useMultiFlag ? (
        <div className="space-y-3">
          {data.flags.map((flag, index) => (
            <div
              key={index}
              className="space-y-2 rounded-lg border border-border bg-bg-surface p-3"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-text-muted">
                  {t("flagNumber", { n: index + 1 })}
                </span>
                <button
                  type="button"
                  onClick={() => handleRemoveFlag(index)}
                  className="text-xs text-accent-error hover:underline"
                  disabled={data.flags.length <= 1}
                >
                  {t("removeFlag")}
                </button>
              </div>

              <div className="flex items-center gap-4">
                <div className="flex gap-3">
                  {(["static", "regex"] as const).map((flagType) => (
                    <label key={flagType} className="flex cursor-pointer items-center gap-1.5">
                      <input
                        type="radio"
                        name={`flagType-${index}`}
                        checked={flag.type === flagType}
                        onChange={() => handleUpdateFlag(index, { type: flagType })}
                        className="h-3.5 w-3.5"
                      />
                      <span className="text-xs">
                        {flagType === "static" ? t("flagTypeStatic") : t("flagTypeRegex")}
                      </span>
                    </label>
                  ))}
                </div>
                <label className="ml-auto flex cursor-pointer items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={flag.caseSensitive}
                    onChange={(event) => handleUpdateFlag(index, { caseSensitive: event.target.checked })}
                    className="h-3.5 w-3.5 rounded"
                  />
                  <span className="text-xs text-text-muted">{t("caseSensitive")}</span>
                </label>
              </div>

              <input
                value={flag.value}
                onChange={(event) => handleUpdateFlag(index, { value: event.target.value })}
                placeholder={
                  flag.type === "static"
                    ? "FLAG{example_flag_here}"
                    : "FLAG\\{[a-zA-Z0-9_]+\\}"
                }
                className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 font-mono text-sm focus:border-accent-primary focus:outline-none"
              />
              {flag.type === "regex" && (
                <p className="text-xs text-text-muted">{t("regexDesc")}</p>
              )}
            </div>
          ))}

          <button
            type="button"
            onClick={handleAddFlag}
            className="flex w-full items-center justify-center gap-1 rounded-lg border-2 border-dashed border-border py-2.5 text-sm text-text-muted transition-colors hover:border-accent-primary hover:text-accent-primary"
          >
            + {t("addFlag")}
          </button>
        </div>
      ) : (
        <>
          <div>
            <label className="mb-2 block text-sm font-medium">{t("flagType")}</label>
            <div className="flex flex-wrap gap-4">
              {(["static", "regex", "dynamic_instance"] as const).map((flagType) => (
                <label key={flagType} className="flex cursor-pointer items-center gap-2">
                  <input
                    type="radio"
                    name="flagType"
                    checked={data.flagType === flagType}
                    onChange={() => handleFlagTypeChange(flagType)}
                    className="h-4 w-4"
                  />
                  <span className="text-sm">
                    {flagType === "static"
                      ? t("flagTypeStatic")
                      : flagType === "regex"
                        ? t("flagTypeRegex")
                        : t("flagTypeDynamic")}
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <label htmlFor="flag-value" className="mb-1.5 block text-sm font-medium">
              {data.flagType === "static" ? t("flagValueLabel") : t("regexPatternLabel")} {" "}
              <span className="text-accent-error">*</span>
            </label>
            <input
              id="flag-value"
              value={data.flagValue}
              onChange={(event) => onChange({ flagValue: event.target.value })}
              placeholder={data.flagType === "static" ? "FLAG{example_flag_here}" : "FLAG\\{[a-zA-Z0-9_]+\\}"}
              className="w-full rounded-lg border border-border bg-bg-primary px-4 py-3 font-mono text-sm focus:border-accent-primary focus:outline-none"
            />
            {data.flagType === "regex" && (
              <p className="mt-1.5 text-xs text-text-muted">{t("regexDesc")}</p>
            )}
          </div>

          {data.flagValue && (
            <div className="rounded-lg border border-border bg-bg-surface p-4">
              <p className="mb-2 text-sm font-medium">{t("validationPreview")}</p>
              <code className="text-sm text-accent-primary">{data.flagValue}</code>
            </div>
          )}
        </>
      )}
    </div>
  );
}
