import { useMemo, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  ArrowDownRight,
  ArrowRight,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  GitPullRequest,
  Network,
  UserRoundCheck,
  Workflow,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { Edge, Snapshot, Stage, Ticket, WorkflowNode } from '../model'
import { formatAge, stageLabel, ticketNeedsReview } from '../preferences'

type WorkflowViewProps = {
  snapshot: Snapshot
  tickets: Ticket[]
  onSelectTicket: (ticket: Ticket) => void
  selectedKey: string | null
}

type DisplayStage = { id: string; name: string; kind: Stage['kind'] | 'unknown' }

export function WorkflowView({ snapshot, tickets, onSelectTicket, selectedKey }: WorkflowViewProps) {
  const stages: DisplayStage[] = snapshot.stages
  const unknownStageIds = [...new Set(tickets.map((ticket) => ticket.stage).filter((stageId) => !stages.some((stage) => stage.id === stageId)))].sort()
  const displayStages = useMemo<DisplayStage[]>(() => [
    ...stages,
    ...unknownStageIds.map((id): DisplayStage => ({ id, name: `Unknown stage: ${id}`, kind: 'unknown' })),
  ], [stages, unknownStageIds.join('\u0000')])
  const grouped = displayStages.map((stage) => ({
    stage,
    tickets: tickets.filter((ticket) => ticket.stage === stage.id),
  }))

  return (
    <section className="workflow-surface columns-surface" aria-label="Ticket columns">
      <div className="surface-topline">
        <div className="surface-caption"><span className="surface-mark"><Workflow size={14} /></span><strong>Ticket columns</strong><span className="surface-caption-separator">·</span><span>Grouped by current stage</span></div>
        <div className="surface-legend"><span><i className="legend-dot active-dot" />Active</span><span><i className="legend-dot waiting-dot" />Waiting</span><span><i className="legend-dot final-dot" />Final</span></div>
      </div>
      <div className="flow-scroll" tabIndex={0} aria-label="Horizontally scrollable ticket columns">
        <div className="flow-track column-track" style={{ gridTemplateColumns: `repeat(${Math.max(displayStages.length, 1)}, minmax(218px, 1fr))` }}>
          {grouped.map(({ stage, tickets: stageTickets }, index) => (
            <StageLane key={stage.id} stage={stage} index={index} tickets={stageTickets} onSelectTicket={onSelectTicket} selectedKey={selectedKey} />
          ))}
          {displayStages.length === 0 && <div className="no-stages"><CircleHelp size={18} /><strong>No stages are defined</strong><span>Tickets remain listed under their available stage IDs.</span></div>}
        </div>
      </div>
    </section>
  )
}

function StageLane({ stage, index, tickets, onSelectTicket, selectedKey }: {
  stage: DisplayStage
  index: number
  tickets: Ticket[]
  onSelectTicket: (ticket: Ticket) => void
  selectedKey: string | null
}) {
  return (
    <article className={`stage-lane stage-${stage.kind}`} data-stage-id={stage.id}>
      <div className="stage-lane-header">
        <span className="stage-index">{String(index + 1).padStart(2, '0')}</span>
        <div className="stage-heading"><strong>{stage.name}</strong><span className={`stage-kind stage-kind-${stage.kind}`}>{stage.kind === 'final' ? 'Final' : stage.kind === 'waiting' ? 'Waiting' : stage.kind === 'unknown' ? 'Unknown' : 'Active'}</span></div>
        <span className="stage-count">{tickets.length}</span>
      </div>
      <div className="lane-ticket-section">
        {tickets.length === 0
          ? <div className="lane-empty"><span>No matching tickets</span></div>
          : <div className="ticket-stack">{tickets.map((ticket) => <TicketCard key={ticket.key} ticket={ticket} onSelect={onSelectTicket} selected={selectedKey === ticket.key} />)}</div>}
      </div>
    </article>
  )
}

export function NodeCard({ node, snapshot }: { node: WorkflowNode; snapshot: Snapshot }) {
  const [expanded, setExpanded] = useState(false)
  const routes = snapshot.edges.filter((edge) => edge.source === `node:${node.id}`)
  return (
    <article className={`node-card ${node.background ? 'node-background' : ''}`}>
      <Button variant="ghost" className="node-toggle" aria-expanded={expanded} onClick={() => setExpanded((current) => !current)}>
        <span className="node-toggle-icon">{expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
        <span className="node-summary"><strong>{node.id}</strong><span>{node.role}</span></span>
        {node.background && <Badge variant="outline" className="background-badge">Background</Badge>}
      </Button>
      {!expanded && <div className="node-teaser">{node.triggers.length ? node.triggers.slice(0, 2).join(' · ') : 'No trigger declared'}<span className="execution-unknown-label">Execution unknown</span></div>}
      {expanded && <div className="node-details">
        <div className="node-metadata"><span>Triggers</span><div>{node.triggers.length ? node.triggers.map((trigger) => <code key={trigger}>{trigger}</code>) : <i>No trigger declared</i>}</div></div>
        <div className="node-metadata"><span>Conditions</span><div>{node.conditions.length ? node.conditions.map((condition) => <code key={`${condition.key}:${condition.value}`}>{condition.key} = {condition.value}</code>) : <i>No conditions declared</i>}</div></div>
        <div className="node-outcomes"><span>Outcomes and effects</span>
          {node.outcomes.length ? node.outcomes.map((outcome, outcomeIndex) => <div className="outcome-row" key={`${outcome.name}:${outcomeIndex}`}>
            <div className="outcome-name"><ArrowDownRight size={13} /><strong>{outcome.name}</strong></div>
            {outcome.conditions.length > 0 && <p><b>When</b> {outcome.conditions.join(' · ')}</p>}
            {outcome.effects.length > 0 && <p><b>Effects</b> {outcome.effects.join(' · ')}</p>}
            {outcome.targets.length > 0 && <p><b>Targets</b> {outcome.targets.map((target) => stageLabel(snapshot, target)).join(' · ')}</p>}
          </div>) : <i>No outcomes declared</i>}
        </div>
        <div className="node-routing"><span>Routing</span>{routes.length ? routes.map((edge, edgeIndex) => <RouteChip key={`${edge.target}:${edgeIndex}`} edge={edge} snapshot={snapshot} />) : <i>No outgoing edge declared</i>}</div>
        <div className="node-execution-note"><CircleHelp size={12} /> Ticket execution is unknown in this snapshot.</div>
      </div>}
    </article>
  )
}

export function Connections({ snapshot }: { snapshot: Snapshot }) {
  return <details className="connections-panel">
    <summary className="connections-heading"><div><Network size={15} /><strong>Declared routes</strong></div><span>{snapshot.edges.length} {snapshot.edges.length === 1 ? 'connection' : 'connections'}</span><ChevronDown size={14} className="connections-chevron" /></summary>
    <div className="connections-content">
      {snapshot.edges.length === 0
        ? <div className="connections-empty">No workflow connections are declared.</div>
        : <div className="connection-list">{snapshot.edges.map((edge, index) => <RouteChip key={`${edge.source}:${edge.target}:${index}`} edge={edge} snapshot={snapshot} />)}</div>}
    </div>
  </details>
}

export function BackgroundNodes({ snapshot }: { snapshot: Snapshot }) {
  const nodes = snapshot.nodes.filter((node) => node.stages.length === 0 || isSharedHumanNode(node))
  if (nodes.length === 0) return null
  return <details className="background-nodes-panel">
    <summary className="background-nodes-heading"><span>Background and other nodes</span><strong>{nodes.length}</strong><ChevronDown size={14} /></summary>
    <div className="background-node-list">{nodes.map((node) => <NodeCard key={node.id} node={node} snapshot={snapshot} />)}</div>
  </details>
}

function isSharedHumanNode(node: WorkflowNode): boolean {
  return node.role === 'human' && node.stages.length > 1
}

function RouteChip({ edge, snapshot }: { edge: Edge; snapshot: Snapshot }) {
  const selfLoop = edge.source === edge.target
  return <div className="route-chip"><span className="route-kind">{edge.kind}</span><strong>{routeEndpoint(snapshot, edge.source)}</strong><ArrowRight size={13} /><strong>{routeEndpoint(snapshot, edge.target)}</strong>{edge.label && <span className="route-label">{edge.label}</span>}{selfLoop && <Badge variant="outline" className="loop-badge">Self-loop</Badge>}</div>
}

function routeEndpoint(snapshot: Snapshot, id: string): string {
  const stageId = id.startsWith('stage:') ? id.slice('stage:'.length) : null
  if (stageId !== null) return snapshot.stages.find((item) => item.id === stageId)?.name ?? `Unknown stage: ${stageId}`
  const nodeId = id.startsWith('node:') ? id.slice('node:'.length) : id
  const node = snapshot.nodes.find((item) => item.id === nodeId)
  if (node !== undefined) return node.id
  return id
}

export function TicketCard({ ticket, onSelect, selected }: { ticket: Ticket; onSelect: (ticket: Ticket) => void; selected: boolean }) {
  const review = ticketNeedsReview(ticket)
  const genericAttention = ticket.attention.kind === 'attention'
  return (
    <button className={`ticket-card ${ticket.warning ? 'ticket-warning' : ''} ${review ? 'ticket-review' : ''} ${selected ? 'ticket-selected' : ''}`} type="button" onClick={() => onSelect(ticket)} aria-label={`Open ${ticket.id}: ${ticket.title}`} aria-current={selected ? 'true' : undefined}>
      <span className="ticket-card-top"><span className="ticket-key">{ticket.id}</span>{ticket.prs.length > 0 && <span className="ticket-pr-count"><GitPullRequest size={11} />{ticket.prs.length}</span>}</span>
      <strong className="ticket-title">{ticket.title}</strong>
      {(ticket.warning || review || genericAttention) && <span className="ticket-signals">
        {ticket.warning && <span className="ticket-signal signal-warning"><AlertTriangle size={11} />Stage age over 1h <b>{formatAge(ticket.ageSeconds)}</b></span>}
        {review && <span className="ticket-signal signal-review"><UserRoundCheck size={11} />Needs human review</span>}
        {genericAttention && <span className="ticket-signal signal-attention"><AlertCircle size={11} />Human attention</span>}
      </span>}
      <span className="ticket-card-bottom"><span className="ticket-repository">{ticket.repository}</span><span className="ticket-age">{ticket.warning && <AlertTriangle size={11} />}{formatAge(ticket.ageSeconds)}</span></span>
    </button>
  )
}
