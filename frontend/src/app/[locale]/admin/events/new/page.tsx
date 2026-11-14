"use client";

import { useMemo } from "react";
import { useRouter } from "@/i18n/navigation";
import { useTranslations } from "next-intl";
import { EventForm, createEventFormValues } from "@/components/events/EventForm";
import { apiFetch } from "@/lib/api-client";
import { useToast } from "@/providers/ToastProvider";
import type { EventAdminDetail, EventWritePayload } from "@/types/event";

export default function NewEventPage() {
  const router = useRouter();
  const t = useTranslations("events");
  const { toast } = useToast();
  const initialValues = useMemo(() => createEventFormValues(), []);

  const handleSubmit = async (payload: EventWritePayload) => {
    const event = await apiFetch<EventAdminDetail>("/api/learn/admin/events", {
      method: "POST",
      body: payload,
    });
    toast("success", t("createSuccess"));
    router.replace(`/admin/events/${event.id}`);
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{t("createEvent")}</h1>
        <p className="mt-1 text-sm text-text-secondary">{t("createDescription")}</p>
      </div>
      <EventForm
        initialValues={initialValues}
        onSubmit={handleSubmit}
        onCancel={() => router.push("/admin/events")}
        submitLabel={t("createEvent")}
      />
    </div>
  );
}
