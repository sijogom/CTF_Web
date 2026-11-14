const PLATFORM_TIME_ZONE = "Asia/Seoul";

const LOCALE_MAP: Record<string, string> = {
  ko: "ko-KR",
  en: "en-US",
  ja: "ja-JP",
  zh: "zh-CN",
};

function resolveLocale(locale: string): string {
  return LOCALE_MAP[locale] ?? "ko-KR";
}

function isValidDate(value: Date): boolean {
  return !Number.isNaN(value.getTime());
}

function seoulDateTimeParts(value: Date): Record<string, string> {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: PLATFORM_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(value).reduce<Record<string, string>>((parts, part) => {
    if (part.type !== "literal") parts[part.type] = part.value;
    return parts;
  }, {});
}

/** 서버의 UTC ISO 시간을 관리 입력용 Asia/Seoul 현지 시간으로 바꾼다. */
export function toSeoulDateTimeInput(iso: string): string {
  const value = new Date(iso);
  if (!isValidDate(value)) return "";
  const parts = seoulDateTimeParts(value);
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

/** datetime-local 입력을 브라우저 시간대와 관계없이 Asia/Seoul 기준 UTC ISO로 바꾼다. */
export function seoulDateTimeInputToUtcIso(input: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(input);
  if (!match) return null;

  const [, year, month, day, hour, minute] = match;
  const value = new Date(
    Date.UTC(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour) - 9,
      Number(minute),
    ),
  );
  if (!isValidDate(value)) return null;

  const normalized = toSeoulDateTimeInput(value.toISOString());
  if (normalized !== input) return null;
  return value.toISOString();
}

export function defaultSeoulDateTimeInput(offsetMinutes = 0): string {
  return toSeoulDateTimeInput(new Date(Date.now() + offsetMinutes * 60_000).toISOString());
}

export function formatEventDateTime(iso: string, locale: string): string {
  const value = new Date(iso);
  if (!isValidDate(value)) return "";
  return new Intl.DateTimeFormat(resolveLocale(locale), {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: PLATFORM_TIME_ZONE,
  }).format(value);
}

export function formatEventDateRange(
  startsAt: string,
  endsAt: string,
  locale: string,
): string {
  const start = formatEventDateTime(startsAt, locale);
  const end = formatEventDateTime(endsAt, locale);
  return start && end ? `${start} – ${end}` : start || end;
}

export { PLATFORM_TIME_ZONE };
