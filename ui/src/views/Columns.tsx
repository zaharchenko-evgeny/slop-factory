import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import type { ViewProps } from "./registry";
import { TicketCard } from "./TicketCard";
import { TicketDetails } from "./TicketDetails";
import { ambiguousIds } from "./ticketFormat";

/** The columns view: one column per workflow stage over the same snapshot; a card opens its details panel.
 * The selection is a ticket key, so a new snapshot keeps it. */
export function Columns({ snapshot }: ViewProps) {
  const [selected, setSelected] = useState<string>();
  const ambiguous = ambiguousIds(snapshot.tickets);
  const ticket = snapshot.tickets.find((t) => t.key === selected);
  return (
    <div data-testid="view-columns" className="flex h-full flex-col">
      <div className="flex min-h-0 flex-1 items-start gap-3 overflow-auto p-4">
        {snapshot.workflow.stages.map((stage) => {
          const tickets = snapshot.tickets.filter((t) => t.stage === stage.id);
          return (
            <section key={stage.id} data-testid={`column-${stage.id}`} aria-label={`Column ${stage.id}`} className="w-64 shrink-0">
              <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">{stage.id}<Badge>{tickets.length}</Badge></h2>
              <ul className="space-y-1">
                {tickets.map((t) => (
                  <TicketCard key={t.key} ticket={t} showTracker={ambiguous.has(String(t.short_id ?? t.id))} selected={t.key === selected} onSelect={() => setSelected(t.key)} />
                ))}
              </ul>
            </section>
          );
        })}
      </div>
      {ticket && <TicketDetails ticket={ticket} onClose={() => setSelected(undefined)} />}
    </div>
  );
}
