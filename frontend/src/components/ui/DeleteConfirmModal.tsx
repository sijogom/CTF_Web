"use client";

interface DeleteConfirmModalProps {
  isOpen: boolean;
  step: number;
  targetName: string;
  messages: {
    title: string;
    step1: string;
    step2: string;
    step3: string;
    cancel: string;
    confirm: string;
    deleting: string;
  };
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

export function DeleteConfirmModal({
  isOpen,
  step,
  targetName,
  messages,
  onConfirm,
  onCancel,
  loading = false,
}: DeleteConfirmModalProps) {
  if (!isOpen || step < 1) return null;

  const stepMessages = [messages.step1, messages.step2, messages.step3];
  const currentMessage = stepMessages[step - 1] || messages.step1;

  const bgColors = [
    "bg-yellow-500/10 border-yellow-500/30",
    "bg-orange-500/10 border-orange-500/30",
    "bg-red-500/10 border-red-500/30",
  ];
  const btnColors = [
    "bg-yellow-600 hover:bg-yellow-700",
    "bg-orange-600 hover:bg-orange-700",
    "bg-red-600 hover:bg-red-700",
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-md rounded-xl border border-border bg-bg-surface p-6 shadow-xl">
        <h3 className="mb-4 text-lg font-semibold">{messages.title}</h3>

        <div className={`mb-4 rounded-lg border p-4 ${bgColors[step - 1]}`}>
          <p className="text-sm font-medium">{targetName}</p>
          <p className="mt-1 text-sm text-text-secondary">{currentMessage}</p>
        </div>

        <div className="mb-3 flex gap-1">
          {[1, 2, 3].map((s) => (
            <div
              key={s}
              className={`h-1.5 flex-1 rounded-full ${
                s <= step ? "bg-red-500" : "bg-border"
              }`}
            />
          ))}
        </div>
        <p className="mb-4 text-center text-xs text-text-muted">
          {step} / 3
        </p>

        <div className="flex justify-end gap-3">
          <button
            onClick={onCancel}
            disabled={loading}
            className="rounded-lg border border-border px-4 py-2 text-sm hover:bg-bg-primary"
          >
            {messages.cancel}
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={`rounded-lg px-4 py-2 text-sm font-medium text-white ${btnColors[step - 1]} disabled:opacity-50`}
          >
            {loading ? messages.deleting : messages.confirm}
          </button>
        </div>
      </div>
    </div>
  );
}
