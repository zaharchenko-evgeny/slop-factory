import { describe, expect, it } from 'vitest'
import {
  isReturnRoute,
  layoutWorkflowGraph,
  roundedPolylinePath,
  routesForFocusedStage,
  selfLoopPath,
  STAGE_NODE_HEIGHT,
  STAGE_NODE_WIDTH,
  workflowGraphBounds,
} from '../src/graph-layout'
import type { DeclaredStageRoute } from '../src/routes'

const stages = [{ id: 'draft' }, { id: 'triage' }, { id: 'review' }, { id: 'done' }]
const routes: DeclaredStageRoute[] = [
  { sourceId: 'draft', targetId: 'triage', labels: ['create PR'] },
  { sourceId: 'triage', targetId: 'review', labels: ['request review', 'retry review'] },
  { sourceId: 'review', targetId: 'review', labels: ['submit changes'] },
  { sourceId: 'done', targetId: 'draft', labels: ['reopen'] },
]

describe('workflow graph layout', () => {
  it('returns deterministic non-overlapping positions, including a self-loop node', () => {
    const first = layoutWorkflowGraph(stages, routes)
    const second = layoutWorkflowGraph(stages, routes)
    expect([...first.positions.entries()]).toEqual([...second.positions.entries()])
    expect(first.positions.size).toBe(stages.length)
    expect(first.edgePoints.size).toBe(routes.length)
    const firstRoute = routes[0]
    expect(firstRoute).toBeDefined()
    expect([...layoutWorkflowGraph(stages, [...routes, ...(firstRoute === undefined ? [] : [firstRoute]), { sourceId: 'missing', targetId: 'done', labels: [] }]).positions.entries()])
      .toEqual([...first.positions.entries()])

    const placed = [...first.positions.values()]
    for (const [leftIndex, a] of placed.entries()) {
      for (const b of placed.slice(leftIndex + 1)) {
        const overlaps = a.x < b.x + STAGE_NODE_WIDTH && a.x + STAGE_NODE_WIDTH > b.x
          && a.y < b.y + STAGE_NODE_HEIGHT && a.y + STAGE_NODE_HEIGHT > b.y
        expect(overlaps).toBe(false)
      }
    }
  })

  it('lays out full workflow stages left to right with Dagre route waypoints', () => {
    const linearStages = [{ id: 'draft' }, { id: 'triage' }, { id: 'review' }]
    const linearRoutes: DeclaredStageRoute[] = [
      { sourceId: 'draft', targetId: 'triage', labels: [] },
      { sourceId: 'triage', targetId: 'review', labels: [] },
    ]
    const layout = layoutWorkflowGraph(linearStages, linearRoutes, 'LR')
    const draft = layout.positions.get('draft')
    const triage = layout.positions.get('triage')
    const review = layout.positions.get('review')
    expect(draft).toBeDefined()
    expect(triage).toBeDefined()
    expect(review).toBeDefined()
    expect(draft?.x).toBeLessThan(triage?.x ?? 0)
    expect(triage?.x).toBeLessThan(review?.x ?? 0)
    const placed = [...layout.positions.values()]
    for (const [leftIndex, a] of placed.entries()) {
      for (const b of placed.slice(leftIndex + 1)) {
        const overlaps = a.x < b.x + STAGE_NODE_WIDTH && a.x + STAGE_NODE_WIDTH > b.x
          && a.y < b.y + STAGE_NODE_HEIGHT && a.y + STAGE_NODE_HEIGHT > b.y
        expect(overlaps).toBe(false)
      }
    }
    const points = layout.edgePoints.get(JSON.stringify(['draft', 'triage']))
    expect(points).toBeDefined()
    expect(points?.length).toBeGreaterThanOrEqual(2)
    expect(roundedPolylinePath(points ?? [])).toContain('M ')
  })

  it('keeps focus connections direct and never infers neighboring-stage routes', () => {
    expect(routesForFocusedStage(routes, 'review')).toEqual([
      { sourceId: 'triage', targetId: 'review', labels: ['request review', 'retry review'] },
      { sourceId: 'review', targetId: 'review', labels: ['submit changes'] },
    ])
  })

  it('marks only self-loops or geometrically backward routes as returns', () => {
    const positions = new Map([
      ['draft', { x: 0, y: 0 }],
      ['review', { x: 0, y: 220 }],
      ['side-left', { x: -280, y: 220 }],
    ])
    expect(isReturnRoute({ sourceId: 'draft', targetId: 'review', labels: [] }, positions)).toBe(false)
    expect(isReturnRoute({ sourceId: 'review', targetId: 'draft', labels: [] }, positions)).toBe(true)
    expect(isReturnRoute({ sourceId: 'review', targetId: 'side-left', labels: [] }, positions)).toBe(true)
    expect(isReturnRoute({ sourceId: 'draft', targetId: 'draft', labels: [] }, positions)).toBe(true)
  })

  it('classifies backward routes along the active layout axis', () => {
    const positions = new Map([
      ['left', { x: 0, y: 120 }],
      ['right', { x: 240, y: 240 }],
      ['behind', { x: -240, y: 240 }],
      ['up', { x: 120, y: 0 }],
    ])
    expect(isReturnRoute({ sourceId: 'left', targetId: 'right', labels: [] }, positions, 'TB')).toBe(false)
    expect(isReturnRoute({ sourceId: 'left', targetId: 'right', labels: [] }, positions, 'LR')).toBe(false)
    expect(isReturnRoute({ sourceId: 'left', targetId: 'behind', labels: [] }, positions, 'LR')).toBe(true)
    expect(isReturnRoute({ sourceId: 'left', targetId: 'behind', labels: [] }, positions, 'TB')).toBe(false)
    expect(isReturnRoute({ sourceId: 'left', targetId: 'up', labels: [] }, positions, 'LR')).toBe(false)
    expect(isReturnRoute({ sourceId: 'left', targetId: 'up', labels: [] }, positions, 'TB')).toBe(true)
  })

  it('builds a visible outward path for self-loop edges', () => {
    expect(selfLoopPath(240, 76, 240, 104)).toBe('M 240,76 C 280,76 280,104 240,104')
  })

  it('includes route waypoints and custom self-loop extent in fit bounds', () => {
    const layout = layoutWorkflowGraph(stages, routes)
    const selfLoopNode = layout.positions.get('review')
    expect(selfLoopNode).toBeDefined()
    const bounds = workflowGraphBounds(layout, stages, routes)
    expect(bounds.width).toBeGreaterThan((selfLoopNode?.x ?? 0) + STAGE_NODE_WIDTH + 40)
    for (const points of layout.edgePoints.values()) {
      for (const point of points) {
        expect(point.x).toBeGreaterThanOrEqual(bounds.x)
        expect(point.y).toBeGreaterThanOrEqual(bounds.y)
        expect(point.x).toBeLessThanOrEqual(bounds.x + bounds.width)
        expect(point.y).toBeLessThanOrEqual(bounds.y + bounds.height)
      }
    }
  })

  it('includes route waypoints and custom self-loop extent in fit bounds', () => {
    const layout = layoutWorkflowGraph(stages, routes)
    const selfLoopNode = layout.positions.get('review')
    expect(selfLoopNode).toBeDefined()
    const bounds = workflowGraphBounds(layout, stages, routes)
    expect(bounds.width).toBeGreaterThan((selfLoopNode?.x ?? 0) + STAGE_NODE_WIDTH + 40)
    for (const points of layout.edgePoints.values()) {
      for (const point of points) {
        expect(point.x).toBeGreaterThanOrEqual(bounds.x)
        expect(point.y).toBeGreaterThanOrEqual(bounds.y)
        expect(point.x).toBeLessThanOrEqual(bounds.x + bounds.width)
        expect(point.y).toBeLessThanOrEqual(bounds.y + bounds.height)
      }
    }
  })

  it('rounds Dagre route bends without changing their waypoints', () => {
    expect(roundedPolylinePath([{ x: 0, y: 0 }, { x: 0, y: 100 }, { x: 100, y: 100 }]))
      .toBe('M 0,0 L 0,86 Q 0,100 14,100 L 100,100')
  })
})
