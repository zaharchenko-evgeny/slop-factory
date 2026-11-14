import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { loadSnapshot, type Snapshot } from "@/snapshot";
import { registry as defaultRegistry, type ViewRegistry } from "@/views/registry";

// Prototype B, full canvas: a compact header and toolbar over a full-width canvas, no permanent sidebar.
export function App({ registry = defaultRegistry, load = loadSnapshot }: { registry?: ViewRegistry; load?: () => Promise<Snapshot> }) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [error, setError] = useState<string>();
  const [active, setActive] = useState(registry.defaultId());

  const refresh = () => load().then((s) => { setSnapshot(s); setError(undefined); }, (e: Error) => setError(e.message));
  useEffect(() => { void refresh(); }, []); // manual refresh is the default

  const view = active ? registry.get(active) : undefined;
  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2">
        <h1 className="text-sm font-semibold">Factory progress</h1>
        {snapshot && <Badge>{snapshot.tickets.length} tickets</Badge>}
        {snapshot && snapshot.attention.human_review > 0 && <Badge>{snapshot.attention.human_review} need human review</Badge>}
      </header>
      <nav aria-label="Views" className="flex items-center gap-1 border-b border-border px-4 py-1">
        {registry.list().map((v) => (
          <Button key={v.id} size="sm" variant={v.id === active ? "default" : "ghost"} aria-pressed={v.id === active} onClick={() => setActive(v.id)}>
            {v.title}
          </Button>
        ))}
        <Button size="sm" variant="ghost" className="ml-auto" onClick={() => void refresh()}>Refresh</Button>
      </nav>
      <main className="min-h-0 flex-1 overflow-auto">
        {error && <p role="alert" className="p-4 text-sm text-red-600">{error}</p>}
        {!snapshot && !error && <p className="p-4 text-sm text-muted-foreground">Loading…</p>}
        {snapshot && view && <view.component snapshot={snapshot} />}
      </main>
    </div>
  );
}
