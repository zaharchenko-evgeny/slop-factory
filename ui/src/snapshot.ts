// The part of the S1 snapshot (`factory/ui/snapshot.py`, `snapshot.build`) the app shell reads.
export interface Stage { id: string; kind?: string | null }
export interface Ticket { key: string; id: string; title: string | null; repository: string | null; stage: string }
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
