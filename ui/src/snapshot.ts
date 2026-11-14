// The part of the S1 snapshot (`factory/ui/snapshot.py`, `snapshot.build`) the app shell reads.
export interface Stage { id: string; kind?: string | null }
export interface Ticket { key: string; id: string; title: string | null; repository: string | null; stage: string }
export interface Snapshot {
  generated_at: string;
  workflow: { stages: Stage[] };
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
