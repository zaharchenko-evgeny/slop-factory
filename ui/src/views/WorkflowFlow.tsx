import { useLayoutEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { HumanBadge } from "./HumanBadge";
import type { Branch, Condition, GraphNode, Outcome, Snapshot, Stage, Ticket } from "@/snapshot";

const describeCondition = (c: Condition | null) => (c ? Object.entries(c).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ") : "");

function OutcomeLine({ o }: { o: Outcome }) {
  const cond = describeCondition(o.condition);
  return (
    <li data-testid="outcome" className={o.enabled ? undefined : "opacity-60"}>
      <span className="font-mono">{o.name}</span>
      {o.to ? ` → ${o.to}` : o.comment_only ? " (comment only)" : ""}
      {cond && <span className="text-muted-foreground"> if {cond}</span>}
      {o.else && <span className="text-muted-foreground"> else {o.else}</span>}
      {!o.enabled && <span className="text-muted-foreground"> (disabled)</span>}
    </li>
  );
}

function NodeBox({ node }: { node: GraphNode }) {
  return (
    <div data-testid={`node-${node.id}`} className="rounded border border-border p-2 text-xs">
      <div className="flex items-center gap-1">
        <span className="font-medium">{node.id}</span>
        {node.role && <Badge>{node.role}</Badge>}
        {node.background && <Badge>background</Badge>}
        {!node.enabled && <Badge>disabled</Badge>}
      </div>
      {node.outcomes.length > 0 && <ul className="mt-1 space-y-0.5">{node.outcomes.map((o) => <OutcomeLine key={o.name} o={o} />)}</ul>}
    </div>
  );
}

const TicketCard = ({ ticket }: { ticket: Ticket }) => (
  <li data-testid="ticket-card" className="rounded border border-border bg-background p-2 text-xs">
    <div className="font-medium">{ticket.title ?? `#${ticket.id}`}</div>
    <div className="text-muted-foreground">#{ticket.id}{ticket.repository ? ` · ${ticket.repository}` : ""}</div>
    <HumanBadge human={ticket.human} />
  </li>
);

function StageNode({ stage, tickets, nodes, branches, loop, open, onToggle }: {
  stage: Stage; tickets: Ticket[]; nodes: GraphNode[]; branches: Branch[]; loop: boolean; open: boolean; onToggle: () => void;
}) {
  return (
    <section data-testid={`stage-${stage.id}`} aria-label={`Stage ${stage.id}`} className="w-64 shrink-0 rounded-lg border border-border p-2">
      <button type="button" aria-expanded={open} onClick={onToggle} className="flex w-full items-center gap-2 text-left text-sm font-semibold">
        <span aria-hidden>{open ? "▾" : "▸"}</span>
        <span>{stage.id}</span>
        <Badge>{tickets.length}</Badge>
        {loop && <Badge>loop</Badge>}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {branches.map((b, i) => (
            <p key={i} data-testid="branch" className="text-xs text-muted-foreground">
              {b.kind === "stage" ? `branches to ${(b.targets ?? []).join(", ")}` : `${b.node}.${b.outcome} else ${b.else}`}
            </p>
          ))}
          {nodes.map((n) => <NodeBox key={n.id} node={n} />)}
          {nodes.length === 0 && <p className="text-xs text-muted-foreground">No execution nodes.</p>}
        </div>
      )}
      <ul className="mt-2 space-y-1" aria-label={`Tickets in ${stage.id}`}>{tickets.map((t) => <TicketCard key={t.key} ticket={t} />)}</ul>
    </section>
  );
}

/** The default workflow flow view: one node per stage over the snapshot graph; read-only. The viewport (scroll) and
 * the expanded stages live in this component, so a new snapshot re-renders them in place. */
export function WorkflowFlow({ snapshot }: { snapshot: Snapshot }) {
  const { stages, nodes = [], loops = [], branches = [] } = snapshot.workflow;
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const canvas = useRef<HTMLDivElement>(null);
  const viewport = useRef({ left: 0, top: 0 });

  // A new snapshot must not move the graph: put the saved scroll back after every render.
  useLayoutEffect(() => {
    const el = canvas.current;
    if (el) { el.scrollLeft = viewport.current.left; el.scrollTop = viewport.current.top; }
  });

  const toggle = (id: string) => setExpanded((s) => { const n = new Set(s); if (!n.delete(id)) n.add(id); return n; });
  const background = nodes.filter((n) => n.background);
  const crossStage = nodes.filter((n) => !n.background && n.stages.length === 0);
  return (
    <div
      ref={canvas}
      data-testid="view-workflow"
      className="h-full overflow-auto p-4"
      onScroll={(e) => { viewport.current = { left: e.currentTarget.scrollLeft, top: e.currentTarget.scrollTop }; }}
    >
      <div className="flex items-start gap-3">
        {stages.map((stage) => (
          <StageNode
            key={stage.id}
            stage={stage}
            tickets={snapshot.tickets.filter((t) => t.stage === stage.id)}
            nodes={nodes.filter((n) => !n.background && n.stages.includes(stage.id))}
            branches={branches.filter((b) => (b.kind === "stage" ? b.stage === stage.id : nodes.some((n) => n.id === b.node && n.stages.includes(stage.id))))}
            loop={loops.some((l) => l.includes(stage.id))}
            open={expanded.has(stage.id)}
            onToggle={() => toggle(stage.id)}
          />
        ))}
      </div>
      {[["Background nodes", background], ["Nodes for any stage", crossStage]].map(([title, list]) =>
        (list as GraphNode[]).length > 0 && (
          <section key={title as string} aria-label={title as string} className="mt-4">
            <h2 className="mb-1 text-xs font-semibold text-muted-foreground">{title as string}</h2>
            <div className="flex flex-wrap gap-2">{(list as GraphNode[]).map((n) => <NodeBox key={n.id} node={n} />)}</div>
          </section>
        ),
      )}
    </div>
  );
}
