import type { Snapshot } from './model'

export type DeclaredStageRoute = {
  sourceId: string
  targetId: string
  labels: string[]
}

export function declaredStageRoutes(snapshot: Snapshot): DeclaredStageRoute[] {
  const stageIds = new Set(snapshot.stages.map((stage) => stage.id))
  const humanNodeIds = snapshot.nodes.filter((node) => node.role === 'human').map((node) => node.id)
  const routes = new Map<string, DeclaredStageRoute>()

  for (const edge of snapshot.edges) {
    if (edge.kind !== 'stage') continue
    const sourceId = stageEndpoint(edge.source)
    const targetId = stageEndpoint(edge.target)
    if (sourceId === null || targetId === null || !stageIds.has(sourceId) || !stageIds.has(targetId)) continue
    if (humanNodeIds.some((nodeId) => edge.label === nodeId || edge.label.startsWith(`${nodeId}:`))) continue

    const key = JSON.stringify([sourceId, targetId])
    const route = routes.get(key)
    if (route === undefined) {
      routes.set(key, { sourceId, targetId, labels: edge.label ? [edge.label] : [] })
    } else if (edge.label && !route.labels.includes(edge.label)) {
      route.labels.push(edge.label)
    }
  }

  return [...routes.values()]
}

function stageEndpoint(endpoint: string): string | null {
  return endpoint.startsWith('stage:') ? endpoint.slice('stage:'.length) : null
}
