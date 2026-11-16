import { describe, expect, it, vi } from 'vitest'
import {
  refreshSnapshot,
  refreshSourceLabel,
  refreshStatusSchema,
  snapshotEnvelopeSchema,
  summarizeRefresh,
  type RefreshApi,
  type RefreshStatus,
} from '../src/refresh'
import type { Snapshot } from '../src/model'

function makeStatus(overrides: Partial<RefreshStatus> = {}): RefreshStatus {
  return {
    id: 8,
    state: 'running',
    older: false,
    startedAt: '2026-11-13T10:00:00Z',
    finishedAt: null,
    error: null,
    sources: [
      { id: 'alpha', name: 'Alpha', phase: 'tickets', completedTickets: 13, totalTickets: 58, errorCount: 0 },
      { id: 'beta', name: 'Beta', phase: 'pending', completedTickets: 0, totalTickets: null, errorCount: 0 },
    ],
    ...overrides,
  }
}

function makeSnapshot(): Snapshot {
  return {
    version: 1,
    factory: { id: 'factory-test', name: 'Test factory' },
    generatedAt: '2026-11-13T10:01:00Z',
    lastSuccessAt: '2026-11-13T10:01:00Z',
    stale: false,
    sources: [],
    stages: [],
    nodes: [],
    edges: [],
    tickets: [],
  }
}

function makeEnvelope({
  snapshot = null,
  snapshotRunId = null,
  refresh = makeStatus(),
}: {
  snapshot?: Snapshot | null
  snapshotRunId?: number | null
  refresh?: RefreshStatus
} = {}) {
  return { snapshot, snapshotRunId, refresh }
}

function makeStatusEnvelope(snapshotRunId: number | null, refresh: RefreshStatus) {
  return { snapshotRunId, refresh }
}

describe('refresh response boundaries', () => {
  it('validates the progress source shape and rejects unknown phases', () => {
    expect(refreshStatusSchema.parse(makeStatus()).sources.at(0)?.completedTickets).toBe(13)
    expect(() => refreshStatusSchema.parse({ ...makeStatus(), sources: [{ ...makeStatus().sources[0], phase: 'uploading' }] })).toThrow()
  })

  it('accepts a missing snapshot while a refresh runs', () => {
    const parsed = snapshotEnvelopeSchema.parse(makeEnvelope({ snapshot: null, snapshotRunId: null }))
    expect(parsed.snapshot).toBeNull()
    expect(parsed.snapshotRunId).toBeNull()
  })
})

describe('refresh progress summaries', () => {
  it('counts completed sources and keeps the label about work read', () => {
    const status = makeStatus({
      sources: [
        { id: 'alpha', name: 'Alpha', phase: 'complete', completedTickets: 58, totalTickets: 58, errorCount: 2 },
        { id: 'beta', name: 'Beta', phase: 'tickets', completedTickets: 13, totalTickets: 58, errorCount: 0 },
      ],
    })
    expect(summarizeRefresh(status.sources)).toEqual({
      completedSources: 1,
      totalSources: 2,
      percent: 50,
      label: '1 of 2 sources read',
    })
    const alpha = status.sources.find((source) => source.id === 'alpha')
    const beta = status.sources.find((source) => source.id === 'beta')
    expect(alpha === undefined ? null : refreshSourceLabel(alpha)).toBe('Finished with 2 warnings')
    expect(beta === undefined ? null : refreshSourceLabel(beta)).toBe('Reading tickets 13 of 58')
  })

  it('keeps progress indeterminate before source data arrives', () => {
    expect(summarizeRefresh([])).toEqual({
      completedSources: 0,
      totalSources: 0,
      percent: null,
      label: 'Preparing source list',
    })
  })
})

describe('refresh polling', () => {
  it('publishes the cached snapshot before it polls for the requested run', async () => {
    const cached = makeSnapshot()
    const updated = { ...cached, generatedAt: '2026-11-13T10:02:00Z' }
    const complete = makeStatus({ state: 'complete', finishedAt: '2026-11-13T10:02:00Z' })
    const api: RefreshApi = {
      start: vi.fn().mockResolvedValue(makeEnvelope({ snapshot: cached, snapshotRunId: 7 })),
      status: vi.fn()
        .mockResolvedValueOnce(makeStatusEnvelope(7, makeStatus()))
        .mockResolvedValueOnce(makeStatusEnvelope(8, complete)),
      read: vi.fn().mockResolvedValue(makeEnvelope({ snapshot: updated, snapshotRunId: 8, refresh: complete })),
    }
    const snapshots: Snapshot[] = []
    const progress: RefreshStatus[] = []

    await refreshSnapshot({
      scope: 'latest',
      signal: new AbortController().signal,
      pollIntervalMs: 0,
      api,
      onSnapshot: (snapshot) => snapshots.push(snapshot),
      onProgress: (status) => progress.push(status),
    })

    expect(snapshots).toEqual([cached, updated])
    expect(api.status).toHaveBeenCalledTimes(2)
    expect(progress.at(-1)?.state).toBe('complete')
  })

  it('stops after repeated cached snapshot read failures', async () => {
    const started = makeStatus()
    const complete = makeStatus({ state: 'complete' })
    const api: RefreshApi = {
      start: vi.fn().mockResolvedValue(makeEnvelope({ refresh: started })),
      status: vi.fn().mockResolvedValue(makeStatusEnvelope(8, complete)),
      read: vi.fn().mockRejectedValue(new Error('temporary read failure')),
    }

    await expect(refreshSnapshot({
      scope: 'latest',
      signal: new AbortController().signal,
      pollIntervalMs: 0,
      api,
      onSnapshot: () => undefined,
      onProgress: () => undefined,
    })).rejects.toThrow('The refreshed snapshot could not be read: temporary read failure')
    expect(api.read).toHaveBeenCalledTimes(4)
  })

  it('stops when a newer run failed before producing the requested snapshot', async () => {
    const api: RefreshApi = {
      start: vi.fn().mockResolvedValue(makeEnvelope({ refresh: makeStatus({ id: 8 }) })),
      status: vi.fn().mockResolvedValue(makeStatusEnvelope(7, makeStatus({ id: 9, state: 'failed', error: 'source failed' }))),
      read: vi.fn(),
    }

    await expect(refreshSnapshot({
      scope: 'latest',
      signal: new AbortController().signal,
      pollIntervalMs: 0,
      api,
      onSnapshot: () => undefined,
      onProgress: () => undefined,
    })).rejects.toThrow('source failed')
    expect(api.status).toHaveBeenCalledTimes(1)
    expect(api.read).not.toHaveBeenCalled()
  })

  it('stops polling when the server restarts with a lower refresh ID', async () => {
    const api: RefreshApi = {
      start: vi.fn().mockResolvedValue(makeEnvelope({ refresh: makeStatus({ id: 1 }) })),
      status: vi.fn().mockResolvedValue(makeStatusEnvelope(null, makeStatus({ id: 0, state: 'idle' }))),
      read: vi.fn(),
    }

    await expect(refreshSnapshot({
      scope: 'latest',
      signal: new AbortController().signal,
      pollIntervalMs: 0,
      api,
      onSnapshot: () => undefined,
      onProgress: () => undefined,
    })).rejects.toThrow('Factory server restarted. Refresh again.')
    expect(api.status).toHaveBeenCalledTimes(1)
    expect(api.read).not.toHaveBeenCalled()
  })

  it('finishes the requested run when snapshotRunId reaches it while a newer run is active', async () => {
    const snapshot = makeSnapshot()
    const requested = makeStatus({ id: 8 })
    const newerRun = makeStatus({ id: 9, state: 'running' })
    const api: RefreshApi = {
      start: vi.fn().mockResolvedValue(makeEnvelope({ snapshotRunId: 7, refresh: requested })),
      status: vi.fn().mockResolvedValue(makeStatusEnvelope(8, newerRun)),
      read: vi.fn().mockResolvedValue(makeEnvelope({ snapshot, snapshotRunId: 8, refresh: newerRun })),
    }
    const snapshots: Snapshot[] = []

    await refreshSnapshot({
      scope: 'latest',
      signal: new AbortController().signal,
      pollIntervalMs: 0,
      api,
      onSnapshot: (nextSnapshot) => snapshots.push(nextSnapshot),
      onProgress: () => undefined,
    })

    expect(api.status).toHaveBeenCalledTimes(1)
    expect(api.read).toHaveBeenCalledTimes(1)
    expect(snapshots).toEqual([snapshot])
  })

  it('polls and reads the older scope after a queued older refresh starts', async () => {
    const queued = makeStatus({ id: 12, state: 'queued', older: true })
    const complete = makeStatus({ id: 12, state: 'complete', older: true })
    const olderSnapshot = { ...makeSnapshot(), generatedAt: '2026-11-12T10:00:00Z' }
    const api: RefreshApi = {
      start: vi.fn().mockResolvedValue(makeEnvelope({ refresh: queued })),
      status: vi.fn().mockResolvedValue(makeStatusEnvelope(12, complete)),
      read: vi.fn().mockResolvedValue(makeEnvelope({ snapshot: olderSnapshot, snapshotRunId: 12, refresh: complete })),
    }
    const snapshots: Snapshot[] = []
    const progress: RefreshStatus[] = []

    await refreshSnapshot({
      scope: 'older',
      signal: new AbortController().signal,
      pollIntervalMs: 0,
      api,
      onSnapshot: (snapshot) => snapshots.push(snapshot),
      onProgress: (status) => progress.push(status),
    })

    expect(api.start).toHaveBeenCalledWith('older', expect.any(AbortSignal))
    expect(api.status).toHaveBeenCalledWith('older', expect.any(AbortSignal))
    expect(api.read).toHaveBeenCalledWith('older', expect.any(AbortSignal))
    expect(progress.map((status) => status.state)).toEqual(['queued', 'complete', 'complete'])
    expect(snapshots).toEqual([olderSnapshot])
  })

  it('cancels the poll wait on abort without sending a status request', async () => {
    const controller = new AbortController()
    const api: RefreshApi = {
      start: vi.fn().mockResolvedValue(makeEnvelope({ refresh: makeStatus() })),
      status: vi.fn(),
      read: vi.fn(),
    }

    const refresh = refreshSnapshot({
      scope: 'latest',
      signal: controller.signal,
      pollIntervalMs: 1000,
      api,
      onSnapshot: () => undefined,
      onProgress: () => undefined,
    })
    setTimeout(() => controller.abort(new DOMException('Unmounted', 'AbortError')), 0)

    await expect(refresh).rejects.toMatchObject({ name: 'AbortError' })
    expect(api.status).not.toHaveBeenCalled()
  })
})
