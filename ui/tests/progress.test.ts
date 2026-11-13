import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseSnapshot } from '../src/model'
import {
  defaultFilters,
  defaultPreferences,
  filteredTickets,
  loadPreferences,
  safeHttpUrl,
  savePreferences,
  ticketsForAttention,
  ticketNeedsReview,
} from '../src/preferences'
import { getProgressViews, registerProgressView } from '../src/views/registry'
import { declaredStageRoutes } from '../src/routes'
import { prioritizeStageTickets } from '../src/graph-layout'

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
        repository: 'alpha', environments: ['prod'], stage: 'in-dev', state: 'open',
        stageEnteredAt: '2026-11-13T08:00:00Z', ageSeconds: 7200, warning: true,
        attention: { kind: 'review', reason: 'Review requested', reviewers: ['Ada'], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:2', id: '#2', title: 'Check the alpha dependency', url: null,
        repository: 'alpha', environments: ['prod'], stage: 'blocked', state: 'open',
        stageEnteredAt: null, ageSeconds: null, warning: false,
        attention: { kind: 'attention', reason: 'Owner input needed', reviewers: [], url: null },
        prs: [], blockers: ['Wait for input'], history: [], nodes: [],
      },
      {
        key: 'ticket:3', id: '#3', title: 'Finish the beta report', url: null,
        repository: 'beta', environments: ['dev'], stage: 'done', state: 'open',
        stageEnteredAt: null, ageSeconds: 0, warning: false,
        attention: { kind: 'none', reason: null, reviewers: [], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:4', id: '#4', title: 'Draft alpha result', url: null,
        repository: 'alpha', environments: ['prod'], stage: 'draft', state: 'open',
        stageEnteredAt: null, ageSeconds: 0, warning: false,
        attention: { kind: 'review', reason: 'Draft review requested', reviewers: ['Lin'], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:5', id: '#5', title: 'Archive the gamma result', url: null,
        repository: 'gamma', environments: ['dev'], stage: 'archived', state: 'open',
        stageEnteredAt: null, ageSeconds: 0, warning: false,
        attention: { kind: 'none', reason: null, reviewers: [], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:6', id: '#6', title: 'Inspect the gamma queue', url: null,
        repository: 'gamma', environments: ['dev'], stage: 'custom-pending', state: 'open',
        stageEnteredAt: null, ageSeconds: null, warning: false,
        attention: { kind: 'none', reason: null, reviewers: [], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:7', id: '#7', title: 'Inspect the closed alpha ticket', url: null,
        repository: 'alpha', environments: ['prod'], stage: 'in-dev', state: 'closed',
        stageEnteredAt: '2026-11-13T08:00:00Z', ageSeconds: 7200, warning: false,
        attention: { kind: 'none', reason: null, reviewers: [], url: null },
        prs: [], blockers: [], history: [], nodes: [],
      },
      {
        key: 'ticket:8', id: '#8', title: 'Inspect a ticket with unknown state', url: null,
        repository: 'alpha', environments: ['prod'], stage: 'in-dev', state: 'unknown',
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

  it('defaults a missing ticket state in legacy snapshots to unknown', () => {
    const legacy = makeSnapshot()
    const ticketsWithoutState = legacy.tickets.map(({ state: _state, ...ticket }) => ticket)
    const snapshot = parseSnapshot({ ...legacy, tickets: ticketsWithoutState })
    expect(snapshot.tickets.every((ticket) => ticket.state === 'unknown')).toBe(true)
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
      visibility: 'active',
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
      visibility: 'active',
    }
    const visible = filteredTickets(snapshot, filters)
    const attentionCandidates = ticketsForAttention(snapshot, filters)
    expect(visible.map((ticket) => ticket.id)).toEqual(['#1'])
    expect(attentionCandidates.map((ticket) => ticket.id)).toEqual(['#1', '#2', '#8'])
  })

  it('keeps draft in unfinished mode and hides it in active mode', () => {
    const active = filteredTickets(snapshot, { ...defaultFilters, visibility: 'active' })
    expect(active.map((ticket) => ticket.id)).toEqual(['#1', '#2', '#6', '#8'])
    expect(filteredTickets(snapshot, { ...defaultFilters, visibility: 'unfinished' }).map((ticket) => ticket.id)).toEqual(['#1', '#2', '#4', '#6', '#8'])
    expect(filteredTickets(snapshot, defaultFilters).map((ticket) => ticket.id)).toContain('#7')
  })

  it('combines active visibility with search and review filters, and restores tickets in all mode', () => {
    const activeReview = filteredTickets(snapshot, {
      ...defaultFilters,
      visibility: 'active',
      query: 'alpha result',
      reviewOnly: true,
    })
    expect(activeReview.map((ticket) => ticket.id)).toEqual(['#1'])

    const hiddenDraftReview = filteredTickets(snapshot, {
      ...defaultFilters,
      visibility: 'active',
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

  it('prioritizes review and warning tickets in stable stage previews', () => {
    const ticketById = (id: string) => {
      const ticket = snapshot.tickets.find((item) => item.id === id)
      if (ticket === undefined) throw new Error(`Ticket ${id} is missing from the test snapshot`)
      return ticket
    }
    const warning = { ...ticketById('#3'), key: 'ticket:warning', id: '#warning', warning: true }
    const stageTickets = [ticketById('#2'), warning, ticketById('#4'), ticketById('#1')]
    expect(prioritizeStageTickets(stageTickets).map((ticket) => ticket.id)).toEqual(['#1', '#4', '#warning', '#2'])
    expect(stageTickets.map((ticket) => ticket.id)).toEqual(['#2', '#warning', '#4', '#1'])
  })
})

describe('saved preferences', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('migrates the old flags without resetting the saved view or filters', () => {
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
      warningOnly: true, visibility: 'unfinished',
    })
    expect(preferences.selectedKey).toBe('ticket:1')
  })

  it.each([
    { hideCompleted: false, activeOnly: false, visibility: 'all' },
    { hideCompleted: true, activeOnly: false, visibility: 'unfinished' },
    { hideCompleted: false, activeOnly: true, visibility: 'active' },
    { hideCompleted: true, activeOnly: true, visibility: 'active' },
  ])('maps legacy flags to $visibility visibility', ({ hideCompleted, activeOnly, visibility }) => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: vi.fn().mockReturnValue(JSON.stringify({
          ...defaultPreferences,
          filters: {
            query: '', repository: 'all', environment: 'all', stage: 'all',
            warningOnly: false, reviewOnly: false, hideCompleted, activeOnly,
          },
        })),
      },
    })

    expect(loadPreferences('test-factory').filters.visibility).toBe(visibility)
  })

  it('keeps a valid new visibility value and saves only the current filter shape', () => {
    const setItem = vi.fn()
    vi.stubGlobal('window', {
      localStorage: {
        getItem: vi.fn().mockReturnValue(JSON.stringify({
          ...defaultPreferences,
          filters: { ...defaultFilters, visibility: 'unfinished', hideCompleted: false, activeOnly: true },
        })),
        setItem,
      },
    })

    const preferences = loadPreferences('test-factory')
    expect(preferences.filters.visibility).toBe('unfinished')
    savePreferences('test-factory', preferences)
    expect(setItem).toHaveBeenCalledWith(
      'factory-progress:test-factory:preferences:v1',
      JSON.stringify({ ...defaultPreferences, filters: { ...defaultFilters, visibility: 'unfinished' } }),
    )
  })

  it('uses defaults when a saved visibility value is invalid', () => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: vi.fn().mockReturnValue(JSON.stringify({
          ...defaultPreferences,
          filters: { ...defaultFilters, visibility: 'invalid', hideCompleted: true },
        })),
      },
    })

    expect(loadPreferences('test-factory')).toEqual(defaultPreferences)
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
