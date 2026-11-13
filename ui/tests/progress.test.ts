import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseSnapshot } from '../src/model'
import {
  defaultFilters,
  defaultPreferences,
  filteredTickets,
  loadPreferences,
  safeHttpUrl,
  ticketsForAttention,
  ticketNeedsReview,
} from '../src/preferences'
import { getProgressViews, registerProgressView } from '../src/views/registry'
import { declaredStageRoutes } from '../src/routes'

function makeSnapshot() {
  return {
    version: 1,
    factory: { id: 'test-factory', name: 'Test factory' },
    generatedAt: '2026-11-13T10:00:00Z',
    lastSuccessAt: '2026-11-13T09:59:00Z',
    stale: false,
    sources: [],
    stages: [
      { id: 'in-dev', name: 'In development', kind: 'active' },
      { id: 'blocked', name: 'Blocked', kind: 'waiting' },
      { id: 'done', name: 'Done', kind: 'final' },
      { id: 'draft', name: 'Draft', kind: 'active' },
      { id: 'archived', name: 'Archived', kind: 'final' },
    ],
    nodes: [],
    edges: [],
    tickets: [
      {
        key: 'ticket:1', id: '#1', title: 'Review the alpha result', url: null,
        repository: 'alpha', environments: ['prod'], stage: 'in-dev',
        stageEnteredAt: '2026-11-13T08:00:00Z', ageSeconds: 7200, warning: true,
        attention: { kind: 'review', reason: 'Review requested', reviewers: ['Ada'], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:2', id: '#2', title: 'Check the alpha dependency', url: null,
        repository: 'alpha', environments: ['prod'], stage: 'blocked',
        stageEnteredAt: null, ageSeconds: null, warning: false,
        attention: { kind: 'attention', reason: 'Owner input needed', reviewers: [], url: null },
        prs: [], blockers: ['Wait for input'], history: [], nodes: [],
      },
      {
        key: 'ticket:3', id: '#3', title: 'Finish the beta report', url: null,
        repository: 'beta', environments: ['dev'], stage: 'done',
        stageEnteredAt: null, ageSeconds: 0, warning: false,
        attention: { kind: 'none', reason: null, reviewers: [], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:4', id: '#4', title: 'Draft alpha result', url: null,
        repository: 'alpha', environments: ['prod'], stage: 'draft',
        stageEnteredAt: null, ageSeconds: 0, warning: false,
        attention: { kind: 'review', reason: 'Draft review requested', reviewers: ['Lin'], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:5', id: '#5', title: 'Archive the gamma result', url: null,
        repository: 'gamma', environments: ['dev'], stage: 'archived',
        stageEnteredAt: null, ageSeconds: 0, warning: false,
        attention: { kind: 'none', reason: null, reviewers: [], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:6', id: '#6', title: 'Inspect the gamma queue', url: null,
        repository: 'gamma', environments: ['dev'], stage: 'custom-pending',
        stageEnteredAt: null, ageSeconds: null, warning: false,
        attention: { kind: 'none', reason: null, reviewers: [], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
    ],
  }
}

describe('snapshot boundary', () => {
  it('parses v1 data and defaults newly optional PR evidence errors', () => {
    const snapshot = parseSnapshot(makeSnapshot())
    expect(snapshot.tickets[0]?.evidenceErrors).toEqual([])
  })

  it('rejects unknown snapshot versions', () => {
    expect(() => parseSnapshot({ ...makeSnapshot(), version: 2 })).toThrow()
  })
})

describe('declared stage routes', () => {
  it('deduplicates declared stage edges and ignores inferred, invalid, and human-node routes', () => {
    const snapshot = parseSnapshot({
      ...makeSnapshot(),
      nodes: [{
        id: 'manual-gate', role: 'human', stages: ['in-dev'], triggers: [], conditions: [], outcomes: [], background: false,
      }],
      edges: [
        { source: 'stage:in-dev', target: 'stage:blocked', label: 'worker: request review', kind: 'stage' },
        { source: 'stage:in-dev', target: 'stage:blocked', label: 'worker: request review', kind: 'stage' },
        { source: 'stage:in-dev', target: 'stage:blocked', label: 'worker: retry', kind: 'stage' },
        { source: 'stage:in-dev', target: 'stage:done', label: 'manual-gate: approve', kind: 'stage' },
        { source: 'stage:in-dev', target: 'stage:blocked', label: 'neighbor order', kind: 'trigger' },
        { source: 'node:worker', target: 'stage:done', label: 'finish', kind: 'stage' },
        { source: 'stage:done', target: 'stage:missing', label: 'unknown target', kind: 'stage' },
      ],
    })

    expect(declaredStageRoutes(snapshot)).toEqual([
      { sourceId: 'in-dev', targetId: 'blocked', labels: ['worker: request review', 'worker: retry'] },
    ])
  })
})

describe('ticket filters', () => {
  const snapshot = parseSnapshot(makeSnapshot())

  it('intersects search, repository, environment, stage, warning, review, and completed filters', () => {
    const filtered = filteredTickets(snapshot, {
      ...defaultFilters,
      query: 'alpha result',
      repository: 'alpha',
      environment: 'prod',
      stage: 'in-dev',
      warningOnly: true,
      reviewOnly: true,
      hideCompleted: true,
      activeOnly: true,
    })
    expect(filtered.map((ticket) => ticket.id)).toEqual(['#1'])
    expect(ticketNeedsReview(filtered[0]!)).toBe(true)
  })

  it('keeps attention counts intersected with other filters when review-only is active', () => {
    const filters = {
      ...defaultFilters,
      repository: 'alpha',
      environment: 'prod',
      reviewOnly: true,
      hideCompleted: true,
      activeOnly: true,
    }
    const visible = filteredTickets(snapshot, filters)
    const attentionCandidates = ticketsForAttention(snapshot, filters)
    expect(visible.map((ticket) => ticket.id)).toEqual(['#1'])
    expect(attentionCandidates.map((ticket) => ticket.id)).toEqual(['#1', '#2'])
  })

  it('hides draft and every configured final stage while keeping waiting and unknown stages', () => {
    const active = filteredTickets(snapshot, { ...defaultFilters, activeOnly: true })
    expect(active.map((ticket) => ticket.id)).toEqual(['#1', '#2', '#6'])
    expect(filteredTickets(snapshot, { ...defaultFilters, hideCompleted: true }).map((ticket) => ticket.id)).toEqual(['#1', '#2', '#4', '#6'])
  })

  it('combines active-only with search and review filters, and restores tickets when turned off', () => {
    const activeReview = filteredTickets(snapshot, {
      ...defaultFilters,
      activeOnly: true,
      query: 'alpha result',
      reviewOnly: true,
    })
    expect(activeReview.map((ticket) => ticket.id)).toEqual(['#1'])

    const hiddenDraftReview = filteredTickets(snapshot, {
      ...defaultFilters,
      activeOnly: true,
      query: 'draft',
      reviewOnly: true,
    })
    const restoredDraftReview = filteredTickets(snapshot, {
      ...defaultFilters,
      query: 'draft',
      reviewOnly: true,
    })
    expect(hiddenDraftReview).toEqual([])
    expect(restoredDraftReview.map((ticket) => ticket.id)).toEqual(['#4'])
    expect(filteredTickets(snapshot, defaultFilters).map((ticket) => ticket.id)).toEqual(snapshot.tickets.map((ticket) => ticket.id))
  })
})

describe('saved preferences', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('loads old preferences without resetting the saved view or filters', () => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: vi.fn().mockReturnValue(JSON.stringify({
          view: 'ticket-list',
          theme: 'dark',
          autoRefresh: true,
          refreshSeconds: 30,
          filters: {
            query: 'alpha', repository: 'alpha', environment: 'prod', stage: 'in-dev',
            warningOnly: true, reviewOnly: false, hideCompleted: true,
          },
          selectedKey: 'ticket:1',
        })),
      },
    })

    const preferences = loadPreferences('test-factory')
    expect(preferences.view).toBe('ticket-list')
    expect(preferences.filters).toEqual({
      ...defaultFilters,
      query: 'alpha', repository: 'alpha', environment: 'prod', stage: 'in-dev',
      warningOnly: true, hideCompleted: true,
    })
    expect(preferences.selectedKey).toBe('ticket:1')
  })
})

describe('refresh preferences', () => {
  it('starts in manual mode and selects one minute for the next enable action', () => {
    expect(defaultPreferences.autoRefresh).toBe(false)
    expect(defaultPreferences.refreshSeconds).toBe(60)
  })
})

describe('safe links', () => {
  it('allows plain HTTP and HTTPS URLs', () => {
    expect(safeHttpUrl('https://example.test/ticket')).toBe('https://example.test/ticket')
    expect(safeHttpUrl('http://localhost:8000/')).toBe('http://localhost:8000/')
  })

  it('rejects non-web schemes, malformed values, and credential-bearing URLs', () => {
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull()
    expect(safeHttpUrl('not a url')).toBeNull()
    expect(safeHttpUrl('https://user:secret@example.test/')).toBeNull()
    expect(safeHttpUrl(null)).toBeNull()
  })
})

describe('progress view registry', () => {
  it('accepts and removes a view outside the built-in IDs', () => {
    const view = { id: 'custom-audit', label: 'Audit', description: 'Audit view', component: () => null }
    const unregister = registerProgressView(view)
    expect(getProgressViews().some((registration) => registration.id === 'custom-audit')).toBe(true)
    unregister()
    expect(getProgressViews().some((registration) => registration.id === 'custom-audit')).toBe(false)
  })

  it('rejects duplicate IDs so a plugin cannot silently replace a registered view', () => {
    const view = { id: 'custom-duplicate', label: 'Duplicate', description: 'Duplicate view', component: () => null }
    const unregister = registerProgressView(view)
    expect(() => registerProgressView(view)).toThrow('already registered')
    unregister()
  })
})
