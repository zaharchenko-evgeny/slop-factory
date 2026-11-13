import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  BaseEdge,
  ControlButton,
  Controls,
  getViewportForBounds,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge as FlowEdge,
  type EdgeProps,
  type Node as FlowNode,
  type NodeProps,
  type NodeTypes,
} from '@xyflow/react'
import { AlertTriangle, ArrowRight, Maximize, UserRoundCheck, Workflow } from 'lucide-react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select'
import type { Snapshot, Stage, Ticket, WorkflowNode } from '../model'
import { formatAge, ticketNeedsReview } from '../preferences'
import { declaredStageRoutes, type DeclaredStageRoute } from '../routes'
import {
  isReturnRoute,
  layoutWorkflowGraph,
  prioritizeStageTickets,
  routesForFocusedStage,
  selfLoopPath,
  roundedPolylinePath,
  stageRouteKey,
  STAGE_NODE_HEIGHT,
  STAGE_NODE_WIDTH,
  workflowGraphBounds,
  type StageRoutePoint,
} from '../graph-layout'
import { BackgroundNodes, Connections, NodeCard, TicketCard } from './WorkflowView'
import { Button } from './ui/button'

type DisplayStage = { id: string; name: string; kind: Stage['kind'] | 'unknown' }
type StageNodeData = {
  stage: DisplayStage
  tickets: Ticket[]
  focused: boolean
  onFocus: (stageId: string) => void
  onSelectTicket: (ticket: Ticket) => void
  direction: 'TB' | 'LR'
}
type StageFlowNode = FlowNode<StageNodeData, 'workflowStage'>
type StageFlowEdge = FlowEdge<{ labels: string[]; points: StageRoutePoint[] }>

const nodeTypes: NodeTypes = { workflowStage: StageNode }
const edgeTypes = { dagreRoute: DagreRouteEdge, selfLoop: SelfLoopEdge }

type WorkflowGraphProps = {
  snapshot: Snapshot
  tickets: Ticket[]
  onSelectTicket: (ticket: Ticket) => void
  selectedKey: string | null
}

export function WorkflowGraph(props: WorkflowGraphProps) {
  return <ReactFlowProvider><WorkflowGraphCanvas {...props} /></ReactFlowProvider>
}

function WorkflowGraphCanvas({ snapshot, tickets, onSelectTicket, selectedKey }: WorkflowGraphProps) {
  const stages = useMemo<DisplayStage[]>(() => {
    const knownStageIds = new Set(snapshot.stages.map((stage) => stage.id))
    const unknownStageIds = [...new Set(snapshot.tickets.map((ticket) => ticket.stage).filter((stageId) => !knownStageIds.has(stageId)))].sort()
    return [
      ...snapshot.stages,
      ...unknownStageIds.map((id): DisplayStage => ({ id, name: `Unknown stage: ${id}`, kind: 'unknown' })),
    ]
  }, [snapshot.stages, snapshot.tickets])
  const routes = useMemo(() => declaredStageRoutes(snapshot), [snapshot])
  const structureKey = JSON.stringify([
    stages.map(({ id, name, kind }) => [id, name, kind]),
    routes.map(({ sourceId, targetId }) => [sourceId, targetId]),
  ])
  const ticketsByStage = useMemo(() => {
    const grouped = new Map<string, Ticket[]>()
    for (const ticket of tickets) {
      const current = grouped.get(ticket.stage) ?? []
      current.push(ticket)
      grouped.set(ticket.stage, current)
    }
    return grouped
  }, [tickets])
  const [fullWorkflow, setFullWorkflow] = useState(false)
  const defaultStage = stages.reduce<DisplayStage | undefined>((best, stage) => {
    const count = ticketsByStage.get(stage.id)?.length ?? 0
    const bestCount = best === undefined ? -1 : ticketsByStage.get(best.id)?.length ?? 0
    return count > bestCount ? stage : best
  }, undefined)
  const [selectedStageId, setSelectedStageId] = useState<string | null>(() => defaultStage?.id ?? null)
  const focusedStage = stages.find((stage) => stage.id === selectedStageId) ?? defaultStage
  const focusedStageId = focusedStage?.id ?? null
  const focusedRoutes = useMemo(
    () => focusedStageId === null ? [] : routesForFocusedStage(routes, focusedStageId),
    [routes, focusedStageId],
  )
  const neighborhoodStageIds = useMemo(() => new Set([
    ...(focusedStageId === null ? [] : [focusedStageId]),
    ...focusedRoutes.flatMap((route) => [route.sourceId, route.targetId]),
  ]), [focusedRoutes, focusedStageId])
  const visibleStages = fullWorkflow ? stages : stages.filter((stage) => neighborhoodStageIds.has(stage.id))
  const layoutRoutes = fullWorkflow ? routes : focusedRoutes
  const rankdir = fullWorkflow ? 'LR' as const : 'TB' as const
  const layoutKey = JSON.stringify([
    rankdir,
    visibleStages.map(({ id, name, kind }) => [id, name, kind]),
    layoutRoutes.map(({ sourceId, targetId }) => [sourceId, targetId]),
  ])
  const layout = useMemo(
    () => layoutWorkflowGraph(visibleStages, layoutRoutes, rankdir),
    [layoutKey],
  )
  const incomingRoutes = focusedStageId === null ? [] : routes.filter((route) => route.targetId === focusedStageId)
  const outgoingRoutes = focusedStageId === null ? [] : routes.filter((route) => route.sourceId === focusedStageId)
  const flow = useReactFlow<StageFlowNode, StageFlowEdge>()
  const canvasRef = useRef<HTMLDivElement>(null)
  const [flowReady, setFlowReady] = useState(false)
  const initialFocusLocked = useRef(false)
  const userMovedViewport = useRef(false)
  const userFocusPending = useRef(false)
  const modeFitPending = useRef(false)
  const previousStructureKey = useRef(structureKey)

  const fitVisibleGraph = useCallback((duration = 0, options: { minZoom?: number; fallbackToFocus?: boolean } = {}) => {
    const minZoom = options.minZoom ?? (fullWorkflow ? 0.1 : 0.6)
    const fallbackToFocus = options.fallbackToFocus ?? !fullWorkflow
    const canvas = canvasRef.current
    if (canvas === null || canvas.clientWidth === 0 || canvas.clientHeight === 0 || visibleStages.length === 0) {
      void flow.fitView({ padding: 0.12, minZoom, maxZoom: 1, duration })
      return
    }
    const viewport = getViewportForBounds(
      workflowGraphBounds(layout, visibleStages, layoutRoutes),
      canvas.clientWidth,
      canvas.clientHeight,
      0.01,
      1,
      0.12,
    )
    if (fallbackToFocus && viewport.zoom < minZoom && focusedStageId !== null) {
      const position = layout.positions.get(focusedStageId)
      if (position !== undefined) {
        void flow.setCenter(position.x + STAGE_NODE_WIDTH / 2, position.y + STAGE_NODE_HEIGHT / 2, { zoom: 0.8, duration })
        return
      }
    }
    void flow.setViewport(viewport, { duration })
  }, [flow, focusedStageId, fullWorkflow, layout, layoutRoutes, visibleStages])

  useEffect(() => {
    if (previousStructureKey.current === structureKey) return
    previousStructureKey.current = structureKey
    if (!flowReady) return
    const frame = window.requestAnimationFrame(() => fitVisibleGraph())
    return () => window.cancelAnimationFrame(frame)
  }, [fitVisibleGraph, flowReady, structureKey])

  const focusStage = useCallback((stageId: string) => {
    initialFocusLocked.current = true
    userFocusPending.current = true
    if (stageId === focusedStageId) {
      userFocusPending.current = false
      if (fullWorkflow) {
        const position = layout.positions.get(stageId)
        if (position !== undefined) void flow.setCenter(position.x + STAGE_NODE_WIDTH / 2, position.y + STAGE_NODE_HEIGHT / 2, { zoom: 0.8, duration: 0 })
      } else {
        fitVisibleGraph()
      }
      return
    }
    setSelectedStageId(stageId)
  }, [fitVisibleGraph, flow, focusedStageId, fullWorkflow, layout])

  useEffect(() => {
    if (!flowReady || initialFocusLocked.current || focusedStageId === null) return
    const frame = window.requestAnimationFrame(() => {
      if (initialFocusLocked.current || userMovedViewport.current) return
      initialFocusLocked.current = true
      setSelectedStageId(focusedStageId)
      fitVisibleGraph()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [fitVisibleGraph, flow, flowReady, focusedStageId])

  useEffect(() => {
    if (!flowReady || !userFocusPending.current || focusedStageId === null) return
    const frame = window.requestAnimationFrame(() => {
      if (!userFocusPending.current) return
      userFocusPending.current = false
      if (fullWorkflow) {
        const position = layout.positions.get(focusedStageId)
        if (position !== undefined) void flow.setCenter(position.x + STAGE_NODE_WIDTH / 2, position.y + STAGE_NODE_HEIGHT / 2, { zoom: 0.8, duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 180 })
      } else {
        fitVisibleGraph(window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 180)
      }
    })
    return () => window.cancelAnimationFrame(frame)
  }, [fitVisibleGraph, flow, flowReady, focusedStageId, fullWorkflow, layout, selectedStageId])

  useEffect(() => {
    if (!flowReady || !modeFitPending.current) return
    const frame = window.requestAnimationFrame(() => {
      if (!modeFitPending.current) return
      modeFitPending.current = false
      fitVisibleGraph()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [fitVisibleGraph, flowReady, fullWorkflow, layout])

  useEffect(() => {
    if (selectedStageId === null || stages.some((stage) => stage.id === selectedStageId)) return
    const replacement = defaultStage
    setSelectedStageId(replacement?.id ?? null)
    if (replacement === undefined) return
    initialFocusLocked.current = true
    userFocusPending.current = true
  }, [defaultStage, selectedStageId, stages])

  const flowNodes = useMemo<StageFlowNode[]>(() => visibleStages.flatMap((stage) => {
    const position = layout.positions.get(stage.id)
    if (position === undefined) return []
    return [{
      id: stage.id,
      type: 'workflowStage',
      position,
      width: STAGE_NODE_WIDTH,
      height: STAGE_NODE_HEIGHT,
      ariaRole: 'group',
      focusable: false,
      selectable: false,
      draggable: false,
      connectable: false,
      data: {
        stage,
        tickets: ticketsByStage.get(stage.id) ?? [],
        focused: stage.id === focusedStageId,
        onFocus: focusStage,
        onSelectTicket,
        direction: rankdir,
      },
    }]
  }), [focusStage, focusedStageId, layout, onSelectTicket, rankdir, ticketsByStage, visibleStages])

  const visibleRoutes = fullWorkflow ? routes : focusedRoutes
  const flowEdges = useMemo<StageFlowEdge[]>(() => visibleRoutes.map((route) => {
    const returning = isReturnRoute(route, layout.positions, rankdir)
    const selfLoop = route.sourceId === route.targetId
    return {
      id: JSON.stringify([route.sourceId, route.targetId]),
      source: route.sourceId,
      target: route.targetId,
      sourceHandle: selfLoop ? 'stage-loop-source' : 'stage-source',
      targetHandle: selfLoop ? 'stage-loop-target' : 'stage-target',
      type: selfLoop ? 'selfLoop' : 'dagreRoute',
      markerEnd: { type: MarkerType.ArrowClosed, color: returning ? 'var(--amber)' : 'var(--teal)' },
      className: `workflow-route-edge ${returning ? 'workflow-return-edge' : ''}`,
      data: { labels: route.labels, points: layout.edgePoints.get(stageRouteKey(route.sourceId, route.targetId)) ?? [] },
      selectable: false,
      focusable: false,
      reconnectable: false,
    }
  }), [layout, rankdir, visibleRoutes])

  const inspectorTickets = focusedStageId === null ? [] : ticketsByStage.get(focusedStageId) ?? []
  const stageNodes = focusedStageId === null
    ? []
    : snapshot.nodes.filter((node) => node.stages.includes(focusedStageId) && !isSharedHumanNode(node))
  return <section className="workflow-surface workflow-graph-surface" aria-label="Workflow stages">
    <div className="surface-topline">
      <div className="surface-caption"><span className="surface-mark"><Workflow size={14} /></span><strong>Workflow stages</strong><span className="surface-caption-separator">·</span><span>Declared routes and filtered tickets</span></div>
      <div className="surface-legend"><span><i className="legend-dot active-dot" />Active</span><span><i className="legend-dot waiting-dot" />Waiting</span><span><i className="legend-dot final-dot" />Final</span><span><i className="legend-dot unknown-dot" />Unknown</span></div>
    </div>
    <div className="workflow-focus-row">
      <p className="workflow-focus-caption" role="status"><strong>{fullWorkflow ? 'Full workflow' : 'Focused neighborhood'}</strong><span>Focused stage: <b>{focusedStage?.name ?? 'None'}</b>. Showing {visibleStages.length} of {stages.length} stages and {visibleRoutes.length} of {routes.length} routes.</span></p>
      <span className="return-route-note" title="Dashed amber connections mark a backward route or a stage that routes to itself."><i />Return route</span>
      {focusedStageId !== null && <Select value={focusedStageId} onValueChange={focusStage}>
        <SelectTrigger className="workflow-focus-select" aria-label="Inspect stage"><SelectValue placeholder="Inspect stage" /></SelectTrigger>
        <SelectContent align="end">{stages.map((stage) => <SelectItem key={stage.id} value={stage.id}>{stage.name} · {ticketsByStage.get(stage.id)?.length ?? 0}</SelectItem>)}</SelectContent>
      </Select>}
      <Button variant={fullWorkflow ? 'secondary' : 'outline'} className={`workflow-mode-toggle ${fullWorkflow ? 'workflow-mode-active' : ''}`} aria-pressed={fullWorkflow} title={fullWorkflow ? 'Switch to the focused neighborhood' : 'Show every configured stage in a left-to-right layout'} onClick={() => { modeFitPending.current = true; setFullWorkflow((current) => !current) }}>Full workflow</Button>
    </div>
    <div ref={canvasRef} className="workflow-graph-canvas" role="group" aria-label="Workflow graph. Use stage buttons to inspect a stage, and pan or zoom to explore the map.">
      <ReactFlow<StageFlowNode, StageFlowEdge>
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        nodesFocusable={false}
        edgesFocusable={false}
        elementsSelectable={false}
        selectNodesOnDrag={false}
        deleteKeyCode={null}
        ariaLabelConfig={{
          'node.a11yDescription.default': 'Read-only workflow stage. Use its stage button to inspect tickets and routes.',
          'node.a11yDescription.keyboardDisabled': 'Read-only workflow stage. Use its stage button to inspect tickets and routes.',
        }}
        zoomOnScroll={false}
        preventScrolling={false}
        minZoom={0.1}
        maxZoom={1.5}
        onInit={() => setFlowReady(true)}
        onNodeClick={(_, node) => focusStage(node.id)}
        onMoveStart={() => {
          userMovedViewport.current = true
          initialFocusLocked.current = true
          setSelectedStageId((current) => current ?? focusedStageId)
        }}
        fitViewOptions={{ padding: 0.14, minZoom: 0.1, maxZoom: 1 }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--line-strong)" />
        <Controls showFitView={false} showInteractive={false} aria-label="Workflow map controls">
          <ControlButton title="Fit view" aria-label="Fit view" onClick={() => fitVisibleGraph(0, { minZoom: 0.1, fallbackToFocus: false })}><Maximize size={13} /></ControlButton>
        </Controls>
        <MiniMap position="top-right" className="workflow-minimap" ariaLabel="Workflow map overview" nodeColor={miniMapNodeColor} nodeStrokeColor="var(--line-strong)" nodeBorderRadius={5} pannable zoomable />
      </ReactFlow>
      {stages.length === 0 && <div className="workflow-graph-empty"><strong>No stages are defined</strong><span>Tickets remain listed under their available stage IDs.</span></div>}
    </div>

    {focusedStage !== undefined && <section className="selected-stage-inspector" aria-label={`Details for ${focusedStage.name}`}>
      <header className="selected-stage-heading">
        <div><span className="inspector-overline">Selected stage</span><h3>{focusedStage.name}</h3></div>
        <div className="selected-stage-count"><strong>{inspectorTickets.length}</strong><span>{inspectorTickets.length === 1 ? 'ticket' : 'tickets'}</span><StageKind kind={focusedStage.kind} /></div>
      </header>
      <div className="focused-route-groups">
        <RouteEvidenceGroup heading={`Incoming routes (${incomingRoutes.length})`} routes={incomingRoutes} direction="incoming" stages={stages} onFocusStage={focusStage} />
        <RouteEvidenceGroup heading={`Outgoing routes (${outgoingRoutes.length})`} routes={outgoingRoutes} direction="outgoing" stages={stages} onFocusStage={focusStage} />
      </div>
      <div className="selected-stage-columns">
        <section className="selected-stage-tickets" aria-label={`Tickets in ${focusedStage.name}`}>
          <div className="inspector-section-heading"><strong>Tickets</strong><span>{inspectorTickets.length} filtered</span></div>
          {inspectorTickets.length === 0
            ? <div className="lane-empty"><span>No filtered tickets in this stage</span></div>
            : <div className="selected-stage-ticket-grid">{inspectorTickets.map((ticket) => <TicketCard key={ticket.key} ticket={ticket} onSelect={onSelectTicket} selected={selectedKey === ticket.key} />)}</div>}
        </section>
        <details className="selected-stage-execution">
          <summary className="inspector-section-heading"><strong>Execution nodes</strong><span>{stageNodes.length}</span></summary>
          {stageNodes.length === 0
            ? <div className="inspector-empty">No execution node is attached to this stage.</div>
            : <div className="selected-stage-node-list">{stageNodes.map((node) => <NodeCard key={node.id} node={node} snapshot={snapshot} />)}</div>}
        </details>
      </div>
    </section>}
    {!focusedStage && <section className="selected-stage-inspector"><h3>No stage to inspect</h3></section>}
    <BackgroundNodes snapshot={snapshot} />
    <Connections snapshot={snapshot} />
  </section>
}

function StageNode({ data }: NodeProps<StageFlowNode>) {
  const previews = prioritizeStageTickets(data.tickets).slice(0, 2)
  const reviewCount = data.tickets.filter(ticketNeedsReview).length
  const warningCount = data.tickets.filter((ticket) => ticket.warning).length
  const moreCount = data.tickets.length - previews.length
  const targetPosition = data.direction === 'LR' ? Position.Left : Position.Top
  const sourcePosition = data.direction === 'LR' ? Position.Right : Position.Bottom
  return <article className={`workflow-stage-node stage-node-${data.stage.kind} ${data.focused ? 'stage-node-focused' : ''}`}>
    <Handle type="target" id="stage-target" position={targetPosition} isConnectable={false} />
    <Handle type="source" id="stage-source" position={sourcePosition} isConnectable={false} />
    <Handle type="source" id="stage-loop-source" position={Position.Right} style={{ top: '42%' }} isConnectable={false} />
    <Handle type="target" id="stage-loop-target" position={Position.Right} style={{ top: '58%' }} isConnectable={false} />
    <button className="workflow-stage-heading" type="button" aria-pressed={data.focused} aria-label={`Focus ${data.stage.name}, ${data.tickets.length} ${data.tickets.length === 1 ? 'ticket' : 'tickets'}`} onClick={(event) => { event.stopPropagation(); data.onFocus(data.stage.id) }}>
      <strong title={data.stage.name}>{data.stage.name}</strong><StageKind kind={data.stage.kind} /><span className="stage-count">{data.tickets.length}</span>
    </button>
    <div className="workflow-stage-signals" aria-label={`${reviewCount} reviews, ${warningCount} age warnings`}>
      {reviewCount > 0 && <span className="workflow-stage-review-count"><UserRoundCheck size={11} />{reviewCount} {reviewCount === 1 ? 'review' : 'reviews'}</span>}
      {warningCount > 0 && <span className="workflow-stage-warning-count"><AlertTriangle size={11} />{warningCount} {warningCount === 1 ? 'warning' : 'warnings'}</span>}
      {reviewCount === 0 && warningCount === 0 && <span className="workflow-stage-quiet-count">{data.tickets.length === 0 ? 'No filtered tickets' : 'No attention signals'}</span>}
    </div>
    <div className="workflow-stage-previews">
      {previews.map((ticket) => <TicketPreview key={ticket.key} ticket={ticket} onFocusStage={data.onFocus} onSelectTicket={data.onSelectTicket} stageId={data.stage.id} />)}
      {moreCount > 0 && <button className="workflow-stage-more" type="button" onClick={(event) => { event.stopPropagation(); data.onFocus(data.stage.id) }}>+{moreCount} more</button>}
    </div>
  </article>
}

function TicketPreview({ ticket, stageId, onFocusStage, onSelectTicket }: {
  ticket: Ticket
  stageId: string
  onFocusStage: (stageId: string) => void
  onSelectTicket: (ticket: Ticket) => void
}) {
  return <button className="workflow-ticket-preview" type="button" aria-label={`Open ${ticket.id}: ${ticket.title}`} onClick={(event) => {
    event.stopPropagation()
    onFocusStage(stageId)
    onSelectTicket(ticket)
  }}>
    <span className="workflow-ticket-preview-copy"><strong>{ticket.id}</strong><span title={ticket.title}>{ticket.title}</span></span>
    <span className="workflow-ticket-preview-signals">
      {ticket.warning && <i className="preview-warning-mark" aria-label={`Age warning: ${formatAge(ticket.ageSeconds)}`} title={`Age warning: ${formatAge(ticket.ageSeconds)}`} />}
      {ticketNeedsReview(ticket) && <i className="preview-review-mark" aria-label="Needs human review" title="Needs human review" />}
      {ticket.attention.kind === 'attention' && <i className="preview-attention-mark" aria-label="Human attention" title="Human attention" />}
    </span>
  </button>
}

function StageKind({ kind }: { kind: DisplayStage['kind'] }) {
  const label = kind === 'final' ? 'Final' : kind === 'waiting' ? 'Waiting' : kind === 'unknown' ? 'Unknown' : 'Active'
  return <span className={`stage-kind stage-kind-${kind}`}>{label}</span>
}

function RouteEvidenceGroup({ heading, routes, direction, stages, onFocusStage }: {
  heading: string
  routes: DeclaredStageRoute[]
  direction: 'incoming' | 'outgoing'
  stages: DisplayStage[]
  onFocusStage: (stageId: string) => void
}) {
  return <section className="focused-route-group">
    <h4>{heading}</h4>
    {routes.length === 0
      ? <span className="focused-route-empty">None declared</span>
      : <div className="focused-route-list">{routes.map((route) => {
        const adjacentStageId = direction === 'incoming' ? route.sourceId : route.targetId
        const adjacentStage = stages.find((stage) => stage.id === adjacentStageId)
        const source = stages.find((stage) => stage.id === route.sourceId)
        const target = stages.find((stage) => stage.id === route.targetId)
        const title = `${source?.name ?? route.sourceId} to ${target?.name ?? route.targetId}`
        return <button key={JSON.stringify([route.sourceId, route.targetId])} className="focused-route-chip" type="button" onClick={() => onFocusStage(adjacentStageId)} aria-label={`Focus ${adjacentStage?.name ?? adjacentStageId} via route ${title}`}>
          <span className="focused-route-endpoints">{source?.name ?? route.sourceId}<ArrowRight size={12} />{target?.name ?? route.targetId}</span>
          {route.labels.length > 0
            ? <span className="focused-route-evidence">{route.labels.map((label) => <code key={label}>{label}</code>)}</span>
            : <span className="focused-route-evidence"><i>No outcome label declared</i></span>}
        </button>
      })}</div>}
  </section>
}

function DagreRouteEdge({ data, sourceX, sourceY, targetX, targetY, markerEnd }: EdgeProps<StageFlowEdge>) {
  const dagrePath = roundedPolylinePath(data?.points ?? [])
  const path = dagrePath || `M ${sourceX},${sourceY} L ${targetX},${targetY}`
  return <BaseEdge path={path} markerEnd={markerEnd} />
}

function SelfLoopEdge({ sourceX, sourceY, targetX, targetY, markerEnd }: EdgeProps<StageFlowEdge>) {
  const path = selfLoopPath(sourceX, sourceY, targetX, targetY)
  return <BaseEdge path={path} markerEnd={markerEnd} />
}

function miniMapNodeColor(node: StageFlowNode): string {
  switch (node.data.stage.kind) {
    case 'waiting': return 'var(--amber-soft)'
    case 'final': return 'var(--green-soft)'
    case 'unknown': return 'var(--soft)'
    default: return 'var(--teal-soft)'
  }
}

function isSharedHumanNode(node: WorkflowNode): boolean {
  return node.role === 'human' && node.stages.length > 1
}
