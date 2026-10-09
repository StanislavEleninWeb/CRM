export function formatDateTime(value: string | null | undefined, timeZone?: string): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone,
  }).format(new Date(value));
}

/** A date-only value (YYYY-MM-DD) shown without any time-zone conversion. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  if (!year || !month || !day) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(
    new Date(Date.UTC(year, month - 1, day)),
  );
}

export function formatMoney(amount: string | null | undefined, currency: string | null | undefined) {
  if (amount == null || !currency) return "—";
  return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(Number(amount));
}

/** Convert a datetime-local input value to an ISO timestamp, or null when empty. */
export function localInputToIso(value: string): string | null {
  return value ? new Date(value).toISOString() : null;
}
