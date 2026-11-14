"use client";

import { cn } from "@/lib/utils";

interface Step {
  label: string;
  icon: string;
}

interface WizardStepperProps {
  steps: Step[];
  currentStep: number;
  onStepClick?: (index: number) => void;
}

export function WizardStepper({ steps, currentStep, onStepClick }: WizardStepperProps) {
  return (
    <div className="@container/wizard mb-8 min-w-0 w-full">
      <div className="grid grid-cols-2 gap-2 @min-[28rem]/wizard:grid-cols-3 @min-[48rem]/wizard:grid-cols-6">
        {steps.map((step, i) => (
          <button
            key={i}
            onClick={() => onStepClick?.(i)}
            disabled={i > currentStep + 1}
            className={cn(
              "flex min-w-0 w-full flex-wrap items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors",
              i === currentStep && "bg-accent-primary text-white",
              i < currentStep && "bg-accent-success/10 text-accent-success",
              i > currentStep && "text-text-muted"
            )}
          >
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs">
              {i < currentStep ? "✓" : i + 1}
            </span>
            <span className="min-w-0 whitespace-normal break-words text-left">
              {step.label}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
