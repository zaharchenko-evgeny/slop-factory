import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FilterBar } from "@/FilterBar";
import { type Filters, NO_FILTERS, filterSnapshot } from "@/filters";
import { loadSnapshot, mergeSnapshot, type Snapshot } from "@/snapshot";
import { registry as defaultRegistry, type ViewRegistry } from "@/views/registry";

// Prototype B, full canvas: a compact header and toolbar over a full-width canvas, no permanent sidebar.
export function App({ registry = defaultRegistry, load = loadSnapshot }: { registry?: ViewRegistry; load?: () => Promise<Snapshot> }) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [error, setError] = useState<string>();
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [active, setActive] = useState(registry.defaultId());

  const [loading, setLoading] = useState(false);
  const [lastOk, setLastOk] = useState<Date>();
  const [auto, setAuto] = useState(false); // manual refresh is the default
  const [interval, setIntervalMs] = useState(60_000); // 60s when first enabled
  const inflight = useRef(false);

  // One request at a time: a click or a tick while a request runs does nothing.
  const refresh = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    setLoading(true);
    try {
      const s = await load();
      setSnapshot((prev) => mergeSnapshot(prev, s)); // stale data stays on a failure
      setError(undefined);
      setLastOk(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      inflight.current = false;
      setLoading(false);
    }
  }, [load]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!auto) return;
    const id = setInterval(() => void refresh(), interval);
    return () => clearInterval(id);
  }, [auto, interval, refresh]);

  const view = active ? registry.get(active) : undefined;
  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2">
        <h1 className="text-sm font-semibold">Factory progress</h1>
        {snapshot && <Badge>{snapshot.tickets.length} tickets</Badge>}
      </header>
      <nav aria-label="Views" className="flex items-center gap-1 border-b border-border px-4 py-1">
        {registry.list().map((v) => (
          <Button key={v.id} size="sm" variant={v.id === active ? "default" : "ghost"} aria-pressed={v.id === active} onClick={() => setActive(v.id)}>
            {v.title}
          </Button>
        ))}
        <span data-testid="last-refresh" className="ml-auto text-xs text-muted-foreground">
          {lastOk ? `Last refreshed ${lastOk.toLocaleTimeString()}` : "Not refreshed yet"}
        </span>
        <label className="flex items-center gap-1 text-xs">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          Auto-refresh
        </label>
        <select aria-label="Refresh interval" className="rounded border border-border bg-background text-xs" value={interval} onChange={(e) => setIntervalMs(Number(e.target.value))}>
          <option value={15_000}>15s</option>
          <option value={30_000}>30s</option>
          <option value={60_000}>1m</option>
          <option value={300_000}>5m</option>
        </select>
        <Button size="sm" variant="ghost" disabled={loading} onClick={() => void refresh()}>{loading ? "Refreshing…" : "Refresh"}</Button>
      </nav>
      {snapshot && <FilterBar snapshot={snapshot} filters={filters} onChange={setFilters} />}
      <main className="min-h-0 flex-1 overflow-auto">
        {error && <p role="alert" className="p-4 text-sm text-red-600">Refresh failed (showing the last data): {error}</p>}
        {snapshot?.errors?.map((e, i) => (
          <p key={i} role="alert" data-testid="source-error" className="px-4 py-1 text-sm text-red-600">
            {e.source ?? "snapshot"}{e.ticket ? ` ${e.ticket}` : ""}: {e.message}
          </p>
        ))}
        {!snapshot && !error && <p className="p-4 text-sm text-muted-foreground">Loading…</p>}
        {snapshot && view && <view.component snapshot={filterSnapshot(snapshot, filters)} />}
      </main>
    </div>
  );
}
