import { z } from 'zod'
import { snapshotSchema, type Snapshot } from './model'

const refreshSourcePhaseSchema = z.enum(['pending', 'listing', 'prefetch', 'events', 'tickets', 'complete'])

const refreshSourceSchema = z.object({
  id: z.string(),
  name: z.string(),
  phase: refreshSourcePhaseSchema,
  completedTickets: z.number().int().nonnegative(),
  totalTickets: z.number().int().nonnegative().nullable(),
  errorCount: z.number().int().nonnegative(),
})

export const refreshStatusSchema = z.object({
  id: z.number().int().nonnegative(),
  state: z.enum(['idle', 'queued', 'running', 'complete', 'failed']),
  older: z.boolean(),
  startedAt: z.string().nullable(),
  finishedAt: z.string().nullable(),
  error: z.string().nullable(),
  sources: z.array(refreshSourceSchema),
})

export const snapshotEnvelopeSchema = z.object({
  snapshot: snapshotSchema.nullable(),
  snapshotRunId: z.number().int().nonnegative().nullable(),
  refresh: refreshStatusSchema,
})

export const refreshStatusEnvelopeSchema = z.object({
  snapshotRunId: z.number().int().nonnegative().nullable(),
  refresh: refreshStatusSchema,
})

export type RefreshStatus = z.infer<typeof refreshStatusSchema>
export type RefreshSource = RefreshStatus['sources'][number]
export type SnapshotScope = 'latest' | 'older'
type SnapshotEnvelope = z.infer<typeof snapshotEnvelopeSchema>
type RefreshStatusEnvelope = z.infer<typeof refreshStatusEnvelopeSchema>

export type RefreshApi = {
  start: (scope: SnapshotScope, signal: AbortSignal) => Promise<SnapshotEnvelope>
  read: (scope: SnapshotScope, signal: AbortSignal) => Promise<SnapshotEnvelope>
  status: (scope: SnapshotScope, signal: AbortSignal) => Promise<RefreshStatusEnvelope>
}

export type RefreshSummary = {
  completedSources: number
  totalSources: number
  percent: number | null
  label: string
}

export function summarizeRefresh(sources: readonly RefreshSource[]): RefreshSummary {
  const completedSources = sources.filter((source) => source.phase === 'complete').length
  const totalSources = sources.length
  const percent = totalSources === 0 ? null : Math.round((completedSources / totalSources) * 100)
  const label = totalSources === 0
    ? 'Preparing source list'
    : `${completedSources} of ${totalSources} sources read`

  return { completedSources, totalSources, percent, label }
}

export function refreshSourceLabel(source: RefreshSource): string {
  switch (source.phase) {
    case 'pending': return 'Waiting to start'
    case 'listing': return 'Listing tickets'
    case 'prefetch': return 'Reading repository data'
    case 'events': return 'Reading events'
    case 'tickets':
      return source.totalTickets === null
        ? 'Reading tickets'
        : `Reading tickets ${source.completedTickets} of ${source.totalTickets}`
    case 'complete':
      return source.errorCount === 0
        ? 'Complete'
        : `Finished with ${source.errorCount} ${source.errorCount === 1 ? 'warning' : 'warnings'}`
    default: {
      const exhaustive: never = source.phase
      return exhaustive
    }
  }
}

export const browserRefreshApi: RefreshApi = {
  start: (scope, signal) => requestSnapshot(scope, true, signal),
  read: (scope, signal) => requestSnapshot(scope, false, signal),
  status: async (scope, signal) => {
    const query = scope === 'older' ? '?older=1' : ''
    const response = await fetch(`/api/status${query}`, { cache: 'no-store', signal })
    const body: unknown = await readResponseBody(response, 'Refresh status')
    return refreshStatusEnvelopeSchema.parse(body)
  },
}

export async function refreshSnapshot({
  scope,
  signal,
  onSnapshot,
  onProgress,
  api = browserRefreshApi,
  pollIntervalMs = 1000,
}: {
  scope: SnapshotScope
  signal: AbortSignal
  onSnapshot: (snapshot: Snapshot) => void
  onProgress: (refresh: RefreshStatus) => void
  api?: RefreshApi
  pollIntervalMs?: number
}): Promise<void> {
  const started = await api.start(scope, signal)
  onProgress(started.refresh)
  if (started.snapshot !== null) onSnapshot(started.snapshot)

  const requestedRunId = started.refresh.id
  if (hasRequestedSnapshot(started.snapshotRunId, requestedRunId)) return
  if (isRequestedRunFailed(started.refresh, requestedRunId)) {
    throw new Error(started.refresh.error ?? 'The snapshot refresh failed.')
  }

  let consecutiveStatusErrors = 0
  let consecutiveSnapshotErrors = 0
  while (true) {
    await waitForPoll(signal, pollIntervalMs)

    let status: RefreshStatusEnvelope
    try {
      status = await api.status(scope, signal)
      consecutiveStatusErrors = 0
    } catch (error: unknown) {
      if (signal.aborted) throw error
      consecutiveStatusErrors += 1
      if (consecutiveStatusErrors >= 4) {
        throw new Error(`Refresh status could not be read: ${errorMessage(error)}`)
      }
      continue
    }

    onProgress(status.refresh)
    if (isRequestedRunFailed(status.refresh, requestedRunId) && !hasRequestedSnapshot(status.snapshotRunId, requestedRunId)) {
      throw new Error(status.refresh.error ?? 'The snapshot refresh failed.')
    }
    if (!hasRequestedSnapshot(status.snapshotRunId, requestedRunId)) continue

    let completed: SnapshotEnvelope
    try {
      completed = await api.read(scope, signal)
      consecutiveSnapshotErrors = 0
    } catch (error: unknown) {
      if (signal.aborted) throw error
      consecutiveSnapshotErrors += 1
      if (consecutiveSnapshotErrors >= 4) {
        throw new Error(`The refreshed snapshot could not be read: ${errorMessage(error)}`)
      }
      continue
    }

    if (completed.snapshot !== null && hasRequestedSnapshot(completed.snapshotRunId, requestedRunId)) {
      onProgress(completed.refresh)
      onSnapshot(completed.snapshot)
      return
    }
  }
}

async function requestSnapshot(scope: SnapshotScope, refresh: boolean, signal: AbortSignal): Promise<SnapshotEnvelope> {
  const params = new URLSearchParams()
  if (scope === 'older') params.set('older', '1')
  if (refresh) params.set('refresh', '1')
  const query = params.size > 0 ? `?${params.toString()}` : ''
  const response = await fetch(`/api/snapshot${query}`, { cache: 'no-store', signal })
  const body: unknown = await readResponseBody(response, 'Snapshot')
  return snapshotEnvelopeSchema.parse(body)
}

async function readResponseBody(response: Response, requestName: string): Promise<unknown> {
  if (!response.ok) throw new Error(`${requestName} request failed (${response.status} ${response.statusText}).`)
  const body: unknown = await response.json()
  return body
}

function hasRequestedSnapshot(snapshotRunId: number | null, requestedRunId: number): boolean {
  return snapshotRunId !== null && snapshotRunId >= requestedRunId
}

function isRequestedRunFailed(refresh: RefreshStatus, requestedRunId: number): boolean {
  return refresh.id >= requestedRunId && refresh.state === 'failed'
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'An unknown network error occurred.'
}

function waitForPoll(signal: AbortSignal, delayMs: number): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason)

  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, delayMs)
    const abort = () => {
      clearTimeout(timeout)
      signal.removeEventListener('abort', abort)
      reject(signal.reason)
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}
