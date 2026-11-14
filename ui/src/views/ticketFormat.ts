import type { StageAge, Ticket } from "@/snapshot";

export const WARN_AFTER_SECONDS = 3600;

/** "unknown" when the entry time is missing; otherwise the age as `2d 3h`, `5h 10m`, `12m` or `<1m`. */
export function formatAge(age: StageAge | undefined): string {
  if (!age || age.seconds == null || !Number.isFinite(age.seconds)) {
    return "age unknown";
  }
  const m = Math.floor(age.seconds / 60);
  if (m < 1) return "<1m";
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m % 60}m`;
  return `${m}m`;
}

/** Amber only for an active stage held for MORE than one hour. Unknown or missing age never warns. */
export function isAmber(t: Ticket): boolean {
  const a = t.stage_age;
  return !!a && a.kind === "active" && a.seconds != null && a.seconds > WARN_AFTER_SECONDS;
}

/** Tracker ids whose short id also appears on another tracker: those cards must show their tracker. */
export function ambiguousIds(tickets: Ticket[]): Set<string> {
  const trackers = new Map<string, Set<string>>();
  for (const t of tickets) {
    const id = String(t.short_id ?? t.id);
    trackers.set(id, (trackers.get(id) ?? new Set()).add(t.tracker ?? ""));
  }
  return new Set([...trackers].filter(([, v]) => v.size > 1).map(([k]) => k));
}
