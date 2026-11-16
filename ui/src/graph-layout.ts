import { graphlib, layout as runDagreLayout } from '@dagrejs/dagre'
import { z } from 'zod'
import type { Ticket } from './model'
import { ticketNeedsReview } from './preferences'
import type { DeclaredStageRoute } from './routes'

export const STAGE_NODE_WIDTH = 240
export const STAGE_NODE_HEIGHT = 180
const dagrePointSchema = z.object({ x: z.number().finite(), y: z.number().finite() })

export type LayoutStage = { id: string }
export type StagePosition = { x: number; y: number }
export type StageRoutePoint = { x: number; y: number }
export type WorkflowLayout = {
  positions: Map<string, StagePosition>
  edgePoints: Map<string, StageRoutePoint[]>
  width: number
  height: number
}
export type WorkflowGraphBounds = { x: number; y: number; width: number; height: number }
const dagreEdgeSchema = z.object({ points: z.array(dagrePointSchema).min(2) })

export function layoutWorkflowGraph(stages: readonly LayoutStage[], routes: readonly DeclaredStageRoute[], rankdir: 'TB' | 'LR' = 'TB'): WorkflowLayout {
  const graph = new graphlib.Graph()
  graph.setGraph({ rankdir, nodesep: 36, edgesep: 18, ranksep: 54, marginx: 32, marginy: 32 })
  graph.setDefaultEdgeLabel(() => ({}))

  const stageIds = new Set(stages.map((stage) => stage.id))
  for (const stage of stages) graph.setNode(stage.id, { width: STAGE_NODE_WIDTH, height: STAGE_NODE_HEIGHT })

  const seenRoutes = new Set<string>()
  for (const route of routes) {
    if (!stageIds.has(route.sourceId) || !stageIds.has(route.targetId)) continue
    const key = stageRouteKey(route.sourceId, route.targetId)
    if (seenRoutes.has(key)) continue
    seenRoutes.add(key)
    graph.setEdge(route.sourceId, route.targetId)
  }

  runDagreLayout(graph)
  const positions = new Map<string, StagePosition>()
  for (const stage of stages) {
    const center = dagrePointSchema.safeParse(graph.node(stage.id))
    if (center.success) positions.set(stage.id, { x: center.data.x - STAGE_NODE_WIDTH / 2, y: center.data.y - STAGE_NODE_HEIGHT / 2 })
  }
  const edgePoints = new Map<string, StageRoutePoint[]>()
  for (const route of routes) {
    const key = stageRouteKey(route.sourceId, route.targetId)
    if (edgePoints.has(key) || !stageIds.has(route.sourceId) || !stageIds.has(route.targetId)) continue
    const geometry = dagreEdgeSchema.safeParse(graph.edge(route.sourceId, route.targetId))
    if (geometry.success) edgePoints.set(key, geometry.data.points)
  }
  const bounds = graph.graph()
  return { positions, edgePoints, width: bounds.width ?? 0, height: bounds.height ?? 0 }
}

export function stageRouteKey(sourceId: string, targetId: string): string {
  return JSON.stringify([sourceId, targetId])
}

export function workflowGraphBounds(
  layout: WorkflowLayout,
  stages: readonly LayoutStage[],
  routes: readonly DeclaredStageRoute[],
): WorkflowGraphBounds {
  let left = 0
  let top = 0
  let right = layout.width
  let bottom = layout.height
  for (const stage of stages) {
    const position = layout.positions.get(stage.id)
    if (position === undefined) continue
    left = Math.min(left, position.x)
    top = Math.min(top, position.y)
    right = Math.max(right, position.x + STAGE_NODE_WIDTH)
    bottom = Math.max(bottom, position.y + STAGE_NODE_HEIGHT)
  }
  for (const route of routes) {
    const points = layout.edgePoints.get(stageRouteKey(route.sourceId, route.targetId)) ?? []
    for (const point of points) {
      left = Math.min(left, point.x)
      top = Math.min(top, point.y)
      right = Math.max(right, point.x)
      bottom = Math.max(bottom, point.y)
    }
    if (route.sourceId === route.targetId) {
      const position = layout.positions.get(route.sourceId)
      if (position !== undefined) right = Math.max(right, position.x + STAGE_NODE_WIDTH + 40)
    }
  }
  return { x: left, y: top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) }
}

export function routesForFocusedStage(routes: readonly DeclaredStageRoute[], stageId: string): DeclaredStageRoute[] {
  return routes.filter((route) => route.sourceId === stageId || route.targetId === stageId)
}

export function isReturnRoute(route: DeclaredStageRoute, positions: ReadonlyMap<string, StagePosition>, rankdir: 'TB' | 'LR' = 'TB'): boolean {
  if (route.sourceId === route.targetId) return true
  const source = positions.get(route.sourceId)
  const target = positions.get(route.targetId)
  if (source === undefined || target === undefined) return false
  return rankdir === 'LR'
    ? target.x < source.x || (target.x === source.x && target.y < source.y)
    : target.y < source.y || (target.y === source.y && target.x < source.x)
}

export function selfLoopPath(sourceX: number, sourceY: number, targetX: number, targetY: number, offset = 40): string {
  const outerX = Math.max(sourceX, targetX) + offset
  return `M ${sourceX},${sourceY} C ${outerX},${sourceY} ${outerX},${targetY} ${targetX},${targetY}`
}

export function roundedPolylinePath(points: readonly StageRoutePoint[], radius = 14): string {
  if (points.length < 2) return ''
  const first = points[0]
  if (first === undefined) return ''
  let path = `M ${first.x},${first.y}`
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = points[index - 1]
    const current = points[index]
    const next = points[index + 1]
    if (previous === undefined || current === undefined || next === undefined) continue
    const incomingX = current.x - previous.x
    const incomingY = current.y - previous.y
    const outgoingX = next.x - current.x
    const outgoingY = next.y - current.y
    const incomingLength = Math.hypot(incomingX, incomingY)
    const outgoingLength = Math.hypot(outgoingX, outgoingY)
    if (incomingLength === 0 || outgoingLength === 0) continue
    const cornerRadius = Math.min(radius, incomingLength / 2, outgoingLength / 2)
    const beforeX = current.x - incomingX / incomingLength * cornerRadius
    const beforeY = current.y - incomingY / incomingLength * cornerRadius
    const afterX = current.x + outgoingX / outgoingLength * cornerRadius
    const afterY = current.y + outgoingY / outgoingLength * cornerRadius
    path += ` L ${beforeX},${beforeY} Q ${current.x},${current.y} ${afterX},${afterY}`
  }
  const last = points.at(-1)
  if (last !== undefined) path += ` L ${last.x},${last.y}`
  return path
}

export function prioritizeStageTickets(tickets: readonly Ticket[]): Ticket[] {
  return tickets
    .map((ticket, order) => ({ ticket, order }))
    .sort((left, right) => Number(ticketNeedsReview(right.ticket)) - Number(ticketNeedsReview(left.ticket))
      || Number(right.ticket.warning) - Number(left.ticket.warning)
      || left.order - right.order)
    .map(({ ticket }) => ticket)
}
