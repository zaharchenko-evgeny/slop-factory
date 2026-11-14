import { Badge } from "@/components/ui/badge";
import type { Ticket } from "@/snapshot";
import { formatAge, isAmber } from "./ticketFormat";
import { HumanBadge } from "./HumanBadge";

/** A ticket card: ID, title, repository, stage, stage age and warnings. `showTracker` tells equal short IDs of
 * different tracker instances apart. */
export function TicketCard({ ticket, showTracker, selected, onSelect }: {
  ticket: Ticket; showTracker: boolean; selected: boolean; onSelect: () => void;
}) {
  const amber = isAmber(ticket);
  return (
    <li data-testid="ticket-card" data-amber={amber} className="list-none">
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        className={`w-full rounded border p-2 text-left text-xs focus-visible:outline focus-visible:outline-2 ${selected ? "border-foreground" : "border-border"} ${amber ? "bg-amber-100 dark:bg-amber-950" : "bg-background"}`}
      >
        <div className="font-medium">{ticket.title ?? <span className="text-muted-foreground">Title missing</span>}</div>
        <div className="text-muted-foreground">
          #{ticket.short_id ?? ticket.id}
          {showTracker && <span data-testid="card-tracker"> · {ticket.tracker ?? "unknown tracker"}</span>}
          {" · "}{ticket.repository ?? "repository missing"}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1">
          <Badge>{ticket.stage}</Badge>
          <span data-testid="card-age">{formatAge(ticket.stage_age)}</span>
          <HumanBadge human={ticket.human} />
          {amber && <Badge className="border-amber-500 text-amber-700 dark:text-amber-300">amber: over 1h</Badge>}
          {(ticket.warnings ?? []).filter((w) => w.kind !== "stage-age").map((w, i) => <Badge key={i}>{w.message}</Badge>)}
        </div>
      </button>
    </li>
  );
}
