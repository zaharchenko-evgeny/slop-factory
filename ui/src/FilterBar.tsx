import type { Snapshot } from "@/snapshot";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type Filters, NO_FILTERS, olderCount, options } from "@/filters";

function Select({ label, value, values, onChange }: { label: string; value: string; values: string[]; onChange: (v: string) => void }) {
  return (
    <label className="flex items-center gap-1 text-xs">{label}
      <select aria-label={label} className="rounded border border-border bg-background px-1 py-0.5" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">all</option>
        {values.map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    </label>
  );
}

/** Attention strip (counts and the human-review filter) and the search and filter controls. */
export function FilterBar({ snapshot, filters, onChange }: { snapshot: Snapshot; filters: Filters; onChange: (f: Filters) => void }) {
  const set = (p: Partial<Filters>) => onChange({ ...filters, ...p });
  const t = snapshot.tickets, older = olderCount(snapshot);
  return (
    <div className="border-b border-border px-4 py-1 text-xs">
      <div data-testid="attention-strip" className="flex items-center gap-2">
        <Badge data-testid="count-human-review">{snapshot.attention.human_review} need human review</Badge>
        <Badge data-testid="count-human-attention">{snapshot.attention.human_attention} need human attention</Badge>
        <Button size="sm" variant={filters.humanReview ? "default" : "ghost"} aria-pressed={filters.humanReview} onClick={() => set({ humanReview: !filters.humanReview })}>
          Human review only
        </Button>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <input type="search" aria-label="Search" placeholder="Search title, ID, repository" className="rounded border border-border bg-background px-2 py-0.5"
          value={filters.search} onChange={(e) => set({ search: e.target.value })} />
        <Select label="Repository" value={filters.repository} values={options(t.map((x) => x.repository))} onChange={(repository) => set({ repository })} />
        <Select label="Environment" value={filters.environment} values={options(t.flatMap((x) => x.environments ?? []))} onChange={(environment) => set({ environment })} />
        <Select label="Stage" value={filters.stage} values={snapshot.workflow.stages.map((s) => s.id)} onChange={(stage) => set({ stage })} />
        <label className="flex items-center gap-1"><input type="checkbox" checked={filters.warnings} onChange={(e) => set({ warnings: e.target.checked })} />Warnings only</label>
        <label className="flex items-center gap-1"><input type="checkbox" checked={filters.hideCompleted} onChange={(e) => set({ hideCompleted: e.target.checked })} />Hide completed</label>
        {older > 0 && !filters.loadOlder && <Button size="sm" variant="ghost" onClick={() => set({ loadOlder: true })}>Load older ({older})</Button>}
        <Button size="sm" variant="ghost" onClick={() => onChange(NO_FILTERS)}>Clear filters</Button>
      </div>
    </div>
  );
}
