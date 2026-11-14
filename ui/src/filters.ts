import type { Snapshot, Ticket } from "@/snapshot";

export const COMPLETED_WINDOW_SECONDS = 7 * 86400;

export interface Filters {
  search: string; repository: string; environment: string; stage: string;
  warnings: boolean; humanReview: boolean; hideCompleted: boolean; loadOlder: boolean;
}
export const NO_FILTERS: Filters = { search: "", repository: "", environment: "", stage: "", warnings: false, humanReview: false, hideCompleted: false, loadOlder: false };

const isFinal = (t: Ticket) => t.stage_kind === "final" || t.stage_age?.kind === "final";
/** A completed ticket is older when its known age exceeds seven days; an unknown age is never hidden. */
const isOlder = (t: Ticket) => isFinal(t) && t.stage_age?.seconds != null && t.stage_age.seconds > COMPLETED_WINDOW_SECONDS;

export function matches(t: Ticket, f: Filters): boolean {
  const q = f.search.trim().toLowerCase();
  if (q && ![t.title, t.id, String(t.short_id ?? ""), t.repository].some((v) => (v ?? "").toLowerCase().includes(q))) return false;
  if (f.repository && t.repository !== f.repository) return false;
  if (f.environment && !(t.environments ?? []).includes(f.environment)) return false;
  if (f.stage && t.stage !== f.stage) return false;
  if (f.warnings && (t.warnings ?? []).length === 0) return false;
  if (f.humanReview && t.human?.kind !== "review") return false;
  if (isFinal(t) && f.hideCompleted) return false;
  if (!f.loadOlder && isOlder(t)) return false;
  return true;
}

export const filterSnapshot = (s: Snapshot, f: Filters): Snapshot => ({ ...s, tickets: s.tickets.filter((t) => matches(t, f)) });
/** Completed tickets the seven-day window hides, before the other filters. */
export const olderCount = (s: Snapshot) => s.tickets.filter(isOlder).length;
export const options = (values: (string | null | undefined)[]) => [...new Set(values.filter((v): v is string => !!v))].sort();
