// The part of the S1 snapshot (`factory/ui/snapshot.py`, `snapshot.build`) the app shell reads.
export interface Stage { id: string; kind?: string | null }
export interface StageAge { state: "ok" | "warn" | "exempt" | "unknown"; seconds: number | null; entered_at?: string | null; kind?: string | null; warn: boolean; reason?: string }
export interface TicketPr { repo?: string | null; number?: number | null; head?: string | null; state?: string | null; ci?: string | null }
export interface NodeMatch { node: string; eligible: string; outcomes?: { name: string; status: string; reason?: string | null }[]; guards?: { reason?: string | null } }
export interface Human { kind: "review" | "attention" | null; label?: string | null; reason?: string | null; reviewer?: string | null }
export interface Ticket {
  key: string; id: string; title: string | null; repository: string | null; stage: string;
  // Fields below come from the S1 ticket record; each can be absent, and the UI says so.
  tracker?: string; short_id?: number | string; stage_kind?: string | null; stage_age?: StageAge;
  environments?: string[]; human?: Human;
  warnings?: { kind: string; level: string; message: string }[];
  prs?: TicketPr[]; ci?: string | null; blockers_open?: number | null; nodes?: NodeMatch[];
  history?: { stage: string; at?: string | null; actor?: string | null }[];
  links?: { ticket?: string | null; prs?: string[] }; missing?: string[]; facts_error?: string | null;
}
export interface Condition { if?: unknown; ci?: unknown }
export interface Outcome { name: string; to: string | null; else?: string | null; condition: Condition | null; enabled: boolean; comment_only: boolean }
export interface GraphNode { id: string; role?: string | null; enabled: boolean; background: boolean; stages: string[]; outcomes: Outcome[] }
export interface Branch { kind: "stage" | "else"; stage?: string; targets?: string[]; node?: string; outcome?: string; else?: string }
export interface Workflow { stages: Stage[]; nodes?: GraphNode[]; loops?: string[][]; branches?: Branch[] }
export interface Snapshot {
  generated_at: string;
  workflow: Workflow;
  tickets: Ticket[];
  sources: { id: string; ok: boolean }[];
  errors: { source: string | null; ticket?: string; message: string }[];
  attention: { human_review: number; human_attention: number };
}

export const SNAPSHOT_URL = "api/snapshot";

/** Load the snapshot from the server (`factory ui`, or the dev stub). Read-only: one GET. */
export async function loadSnapshot(fetcher: typeof fetch = fetch): Promise<Snapshot> {
  const response = await fetcher(SNAPSHOT_URL, { method: "GET" });
  if (!response.ok) throw new Error(`snapshot request failed: HTTP ${response.status}`);
  return (await response.json()) as Snapshot;
}

/**
 * Apply a refreshed snapshot over the previous one. A source that failed has no tickets in `next`; keep its tickets
 * from `prev` (stale) so a source error does not blank the board, and take every successful source as it is.
 */
export function mergeSnapshot(prev: Snapshot | undefined, next: Snapshot): Snapshot {
  if (!prev) return next;
  const failed = new Set<string>();
  for (const s of next.sources ?? []) if (!s.ok) failed.add(s.id);
  for (const e of next.errors ?? []) if (e.source && !e.ticket) failed.add(e.source);
  const failedTickets = new Set((next.errors ?? []).filter((e) => e.ticket).map((e) => e.ticket));
  const have = new Set(next.tickets.map((t) => t.key));
  const stale = prev.tickets.filter(
    (t) => !have.has(t.key) && ((t.tracker !== undefined && failed.has(t.tracker)) || failedTickets.has(t.key) || failedTickets.has(t.id)),
  );
  return stale.length ? { ...next, tickets: [...next.tickets, ...stale] } : next;
}
