import {
  AlertCircle,
  AlertTriangle,
  ArrowUpRight,
  CalendarClock,
  Check,
  CircleHelp,
  GitPullRequest,
  Link2,
  UserRoundCheck,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import type { Snapshot, Ticket } from '../model'
import { formatAge, formatTimestamp, safeHttpUrl, stageLabel, ticketNeedsReview } from '../preferences'

export function TicketDetails({ snapshot, ticket, onClose }: { snapshot: Snapshot; ticket: Ticket | null; onClose: () => void }) {
  const stage = ticket === null ? undefined : snapshot.stages.find((item) => item.id === ticket.stage)
  const issueUrl = ticket === null ? null : safeHttpUrl(ticket.url)
  const attentionUrl = ticket === null ? null : safeHttpUrl(ticket.attention.url)
  const review = ticket !== null && ticketNeedsReview(ticket)
  const genericAttention = ticket?.attention.kind === 'attention'

  return (
    <Sheet open={ticket !== null} onOpenChange={(open) => { if (!open) onClose() }}>
      <SheetContent side="right" className="ticket-sheet">
        {ticket !== null && <>
          <SheetHeader className="ticket-sheet-header">
            <div className="sheet-eyebrow"><span className="sheet-ticket-key">{ticket.id}</span><span className="sheet-repo">{ticket.repository}</span>{ticket.state === 'closed' && <Badge variant="outline" className="closed-badge">Closed</Badge>}<Badge variant="outline" className={`sheet-stage-badge stage-kind-${stage?.kind ?? 'unknown'}`}>{stage?.name ?? `Unknown stage: ${ticket.stage}`}</Badge></div>
            <SheetTitle>{ticket.title}</SheetTitle>
            <SheetDescription>{ticket.environments.length ? ticket.environments.join(', ') : 'No environment recorded'}</SheetDescription>
          </SheetHeader>
          <div className="sheet-body">
            {ticket.warning && <div className="sheet-warning"><AlertTriangle size={15} /><div><strong>Active stage age is over one hour</strong><span>Current stage age: {formatAge(ticket.ageSeconds)}. This warning comes from the snapshot.</span></div></div>}
            {review && <section className="sheet-human-review"><div className="sheet-human-heading"><UserRoundCheck size={16} /><strong>Human review required</strong><Badge variant="outline">Review</Badge></div><p>{ticket.attention.reason ?? 'No review reason was supplied.'}</p><div className="sheet-reviewers"><span>Reviewers</span><strong>{ticket.attention.reviewers.length ? ticket.attention.reviewers.join(', ') : 'Unknown'}</strong></div>{attentionUrl && <a className="review-evidence-link" href={attentionUrl} target="_blank" rel="noreferrer">Review evidence <ArrowUpRight size={13} /></a>}</section>}
            {genericAttention && <section className="sheet-generic-attention"><div><AlertCircle size={15} /><strong>Human attention requested</strong></div><p>{ticket.attention.reason ?? 'No attention reason was supplied.'}</p>{ticket.attention.reviewers.length > 0 && <span>People: {ticket.attention.reviewers.join(', ')}</span>}{attentionUrl && <a className="review-evidence-link" href={attentionUrl} target="_blank" rel="noreferrer">Attention evidence <ArrowUpRight size={13} /></a>}</section>}
            {ticket.attention.kind === 'none' && <div className="no-attention"><CircleHelp size={14} /> No human attention is recorded for this ticket.</div>}

            <section className="sheet-section">
              <h3>Source</h3>
              <div className="source-link-list">
                <SourceLink href={issueUrl} title={ticket.id} detail={ticket.repository} />
                {attentionUrl && <SourceLink href={attentionUrl} title="Attention evidence" detail="Source record" />}
              </div>
              {issueUrl === null && <p className="field-note">Ticket source link is unavailable.</p>}
            </section>

            <section className="sheet-section">
              <div className="section-title-row"><h3>Pull requests</h3><span>{ticket.prs.length}</span></div>
              {ticket.evidenceErrors.length > 0 && <div className="pr-evidence-warning" role="status"><AlertCircle size={14} /><div><strong>Pull-request evidence is incomplete</strong><span>Some PR reads failed. Listed details may be stale, and a missing PR does not mean none exists.</span><ul>{ticket.evidenceErrors.map((error, index) => <li key={`${error}:${index}`}>{error}</li>)}</ul></div></div>}
              {ticket.prs.length === 0 ? <p className="quiet-empty">{ticket.evidenceErrors.length ? 'No pull requests could be confirmed from the available evidence.' : 'No pull requests are attached to this ticket.'}</p> : <div className="pr-list">{ticket.prs.map((pr, index) => {
                const url = safeHttpUrl(pr.url)
                return <article className="pr-card" key={`${pr.repository}:${pr.number}:${index}`}>
                  <div className="pr-heading"><GitPullRequest size={15} /><strong>{pr.repository} · {pr.number}</strong>{url ? <a href={url} target="_blank" rel="noreferrer" aria-label={`Open pull request ${pr.number}`}><ArrowUpRight size={14} /></a> : <span className="link-unavailable">Link unavailable</span>}</div>
                  <div className="pr-facts"><span><b>State</b>{unknownLabel(pr.state)}</span><span><b>CI</b><span className={`ci-value ci-${pr.ci.toLowerCase()}`}>{unknownLabel(pr.ci)}</span></span><span><b>Head</b><code>{unknownLabel(pr.head)}</code></span></div>
                  <div className="pr-reviewers"><UserRoundCheck size={12} /><span>Requested reviewers</span><strong>{pr.requestedReviewers.length ? pr.requestedReviewers.join(', ') : 'None recorded'}</strong></div>
                </article>
              })}</div>}
            </section>

            <section className="sheet-section">
              <h3>Blockers</h3>
              {ticket.blockers.length ? <ul className="blocker-list">{ticket.blockers.map((blocker, index) => <li key={`${blocker}:${index}`}><span className="blocker-dot" />{blocker}</li>)}</ul> : <p className="quiet-empty">No blockers are recorded.</p>}
            </section>

            <section className="sheet-section">
              <h3>Stage timing</h3>
              <div className="timing-facts"><span><CalendarClock size={14} />Entered stage <strong>{formatTimestamp(ticket.stageEnteredAt)}</strong></span><span><CircleHelp size={14} />Time in stage <strong>{formatAge(ticket.ageSeconds)}</strong></span></div>
            </section>

            <section className="sheet-section">
              <h3>Node eligibility</h3>
              {ticket.nodes.length === 0 ? <p className="quiet-empty">No node eligibility results are recorded.</p> : <div className="eligibility-list">{ticket.nodes.map((evaluation, index) => {
                const definition = snapshot.nodes.find((node) => node.id === evaluation.id)
                return <article className="eligibility-card" key={`${evaluation.id}:${index}`}>
                  <div className="eligibility-heading"><div><strong>{evaluation.id}</strong><span>{definition?.role ?? 'Node definition unavailable'}</span></div><ResultBadge result={evaluation.eligibility} /></div>
                  {definition === undefined && <div className="field-note">This node is not present in the current workflow definition.</div>}
                  <div className="eligibility-fact"><span>Trigger</span><ResultBadge result={evaluation.trigger} /></div>
                  {evaluation.conditions.length > 0 && <div className="evaluation-conditions">{evaluation.conditions.map((condition, conditionIndex) => <div className="evaluation-condition" key={`${condition.key}:${conditionIndex}`}><span><code>{condition.key}</code> = {condition.value}</span><ResultBadge result={condition.result} /><small>{condition.reason || 'No additional reason recorded.'}</small></div>)}</div>}
                  <div className="evaluation-reason"><span>Eligibility detail</span><p>{evaluation.reason || 'No additional reason recorded.'}</p></div>
                  <div className="unknown-execution"><CircleHelp size={13} /><span>Execution: unknown</span><small>The snapshot does not report whether this node ran.</small></div>
                </article>
              })}</div>}
            </section>

            <section className="sheet-section">
              <div className="section-title-row"><h3>Stage history</h3><span>{ticket.history.length}</span></div>
              {ticket.history.length === 0 ? <p className="quiet-empty">No stage history is recorded.</p> : <ol className="history-list">{ticket.history.map((entry, index) => <li key={`${entry.stage}:${entry.at ?? 'unknown'}:${index}`}><span className={`history-marker ${index === 0 ? 'history-current' : ''}`} /><div><strong>{stageLabel(snapshot, entry.stage)}</strong><span>{entry.stage}</span></div><time>{formatTimestamp(entry.at)}</time></li>)}</ol>}
            </section>
          </div>
          <div className="sheet-footer"><span><span className="footer-status" />Read-only detail</span><Button variant="outline" onClick={onClose}>Close</Button></div>
        </>}
      </SheetContent>
    </Sheet>
  )
}

function SourceLink({ href, title, detail }: { href: string | null; title: string; detail: string }) {
  if (href === null) return <div className="source-link unavailable"><Link2 size={14} /><strong>{title}</strong><span>{detail} · link unavailable</span></div>
  return <a className="source-link" href={href} target="_blank" rel="noreferrer"><Link2 size={14} /><strong>{title}</strong><span>{detail}<ArrowUpRight size={12} /></span></a>
}

function ResultBadge({ result }: { result: 'matched' | 'not-matched' | 'unknown' }) {
  const label = result === 'matched' ? 'Matched' : result === 'not-matched' ? 'Not matched' : 'Unknown'
  const icon = result === 'matched' ? <Check size={11} /> : result === 'not-matched' ? <AlertCircle size={11} /> : <CircleHelp size={11} />
  return <Badge variant="outline" className={`result-badge result-${result}`}>{icon}{label}</Badge>
}

function unknownLabel(value: string): string {
  return value.trim().length > 0 ? value : 'Unknown'
}
