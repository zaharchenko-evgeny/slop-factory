import type { ReactNode } from "react";
import type { Ticket } from "@/snapshot";

const Missing = ({ what }: { what: string }) => <span className="text-muted-foreground">{what} missing</span>;
const Section = ({ title, children }: { title: string; children: ReactNode }) => (
  <section aria-label={title} className="mt-3">
    <h3 className="text-xs font-semibold text-muted-foreground">{title}</h3>
    <div className="text-xs">{children}</div>
  </section>
);

/** Details of one ticket. A field the snapshot does not hold is shown as missing, never left blank. */
export function TicketDetails({ ticket, onClose }: { ticket: Ticket; onClose: () => void }) {
  const prs = ticket.prs ?? [], history = ticket.history ?? [], nodes = ticket.nodes ?? [];
  const prLinks = ticket.links?.prs ?? [];
  return (
    <aside data-testid="ticket-details" aria-label={`Details of #${ticket.short_id ?? ticket.id}`} className="border-t border-border p-4">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">{ticket.title ?? <Missing what="Title" />}</h2>
        <button type="button" className="ml-auto text-xs underline" onClick={onClose}>Close</button>
      </div>
      {ticket.facts_error && <p role="status" className="text-xs text-red-600">{ticket.facts_error}</p>}
      {ticket.human?.kind && (
        <Section title="Human attention">
          <p data-testid="details-human">
            {ticket.human.label ?? (ticket.human.kind === "review" ? "Needs human review" : "Needs human attention")}
            {" · reason: "}{ticket.human.reason ?? "unknown"}
            {" · reviewer: "}{ticket.human.reviewer ?? "unknown"}
          </p>
        </Section>
      )}
      <Section title="Linked PRs">
        {prs.length === 0 ? <Missing what="Linked PR" /> : (
          <ul>{prs.map((p, i) => (
            <li key={i} data-testid="details-pr">
              {p.repo ?? "repository missing"}#{p.number ?? "?"} · {p.state ?? "state missing"} · CI {p.ci ?? "missing"}
            </li>
          ))}</ul>
        )}
      </Section>
      <Section title="CI">{ticket.ci ?? <Missing what="CI" />}</Section>
      <Section title="Blockers">{ticket.blockers_open == null ? <Missing what="Blocker count" /> : `${ticket.blockers_open} open`}</Section>
      <Section title="Node matches">
        {nodes.length === 0 ? <Missing what="Node match" /> : (
          <ul>{nodes.map((n) => (
            <li key={n.node} data-testid="details-node">
              {n.node}: eligible {n.eligible}
              {n.guards?.reason ? ` (${n.guards.reason})` : ""}
            </li>
          ))}</ul>
        )}
      </Section>
      <Section title="Stage history">
        {history.length === 0 ? <Missing what="Stage history" /> : (
          <ol>{history.map((h, i) => (
            <li key={i} data-testid="details-history">{h.stage} · {h.at ?? "time missing"}{h.actor ? ` · ${h.actor}` : ""}</li>
          ))}</ol>
        )}
      </Section>
      <Section title="Source links">
        <ul>
          <li>{ticket.links?.ticket ? <a className="underline" href={ticket.links.ticket}>Ticket</a> : <Missing what="Ticket link" />}</li>
          {prLinks.map((u) => <li key={u}><a className="underline" href={u}>{u}</a></li>)}
          {prLinks.length === 0 && <li><Missing what="PR link" /></li>}
        </ul>
      </Section>
      {(ticket.missing ?? []).length > 0 && <Section title="Missing fields">{(ticket.missing ?? []).join(", ")}</Section>}
    </aside>
  );
}
