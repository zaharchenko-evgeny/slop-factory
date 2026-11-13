import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  ArrowDownToLine,
  ArrowRight,
  Check,
  Clock3,
  GitPullRequest,
  LoaderCircle,
  Moon,
  RefreshCw,
  Search,
  Settings2,
  Sun,
  UserRoundCheck,
  Workflow,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import './views/builtins'
import { useProgressViews } from './views/registry'

const TicketDetails = lazy(async () => {
  const module = await import('@/components/TicketDetails')
  return { default: module.TicketDetails }
})
import { snapshotSchema, type Snapshot, type Ticket } from './model'
import {
  defaultPreferences,
  filteredTickets,
  formatTimestamp,
  loadPreferences,
  savePreferences,
  ticketNeedsReview,
  ticketsForAttention,
  type Filters,
  type Preferences,
} from './preferences'

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return 'The snapshot could not be loaded.'
}

type RefreshChoice = Preferences['refreshSeconds'] | 'off'

function parseRefreshInterval(value: string): RefreshChoice | null {
  switch (value) {
    case 'off': return 'off'
    case '15': return 15
    case '30': return 30
    case '60': return 60
    case '300': return 300
    default: return null
  }
}

function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [olderSnapshot, setOlderSnapshot] = useState<Snapshot | null>(null)
  const [viewingOlder, setViewingOlder] = useState(false)
  const [loading, setLoading] = useState(true)
  const [fetching, setFetching] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [olderError, setOlderError] = useState<string | null>(null)
  const [olderLoading, setOlderLoading] = useState(false)
  const [preferences, setPreferences] = useState<Preferences>(defaultPreferences)
  const [loadedFactoryId, setLoadedFactoryId] = useState<string | null>(null)
  const [selectedTicket, setSelectedTicket] = useState<Ticket | null>(null)
  const inFlight = useRef(false)
  const alive = useRef(true)
  const registeredViews = useProgressViews()

  const refreshSnapshot = useCallback(async (older: boolean) => {
    if (inFlight.current) return false
    inFlight.current = true
    setFetching(true)
    if (older) {
      setOlderLoading(true)
      setOlderError(null)
    } else {
      setLoadError(null)
    }
    try {
      const response = await fetch(older ? '/api/snapshot?older=1' : '/api/snapshot', { cache: 'no-store' })
      if (!response.ok) throw new Error(`Snapshot request failed (${response.status} ${response.statusText}).`)
      const body: unknown = await response.json()
      const parsed = snapshotSchema.parse(body)
      if (!alive.current) return false
      if (older) {
        setOlderSnapshot(parsed)
      } else {
        setSnapshot(parsed)
      }
      return true
    } catch (error: unknown) {
      if (!alive.current) return false
      const message = errorMessage(error)
      if (older) setOlderError(message)
      else setLoadError(message)
      return false
    } finally {
      inFlight.current = false
      if (alive.current) {
        setFetching(false)
        setLoading(false)
        setOlderLoading(false)
      }
    }
  }, [])

  const displaySnapshot = viewingOlder && olderSnapshot !== null ? olderSnapshot : snapshot
  const refreshVisibleSnapshot = useCallback(() => refreshSnapshot(viewingOlder), [refreshSnapshot, viewingOlder])

  useEffect(() => {
    alive.current = true
    void refreshSnapshot(false)
    return () => { alive.current = false }
  }, [refreshSnapshot])

  useEffect(() => {
    if (snapshot === null) return
    const stored = loadPreferences(snapshot.factory.id)
    setPreferences(stored)
    setLoadedFactoryId(snapshot.factory.id)
  }, [snapshot?.factory.id])

  useEffect(() => {
    if (snapshot === null || loadedFactoryId !== snapshot.factory.id) return
    savePreferences(snapshot.factory.id, preferences)
  }, [loadedFactoryId, preferences, snapshot])

  useEffect(() => {
    if (registeredViews.some((view) => view.id === preferences.view) || registeredViews[0] === undefined) return
    setPreferences((current) => ({ ...current, view: registeredViews[0].id }))
  }, [preferences.view, registeredViews])

  useEffect(() => {
    if (!preferences.autoRefresh) return
    const timer = window.setInterval(() => { void refreshVisibleSnapshot() }, preferences.refreshSeconds * 1000)
    return () => window.clearInterval(timer)
  }, [preferences.autoRefresh, preferences.refreshSeconds, refreshVisibleSnapshot])

  useEffect(() => {
    if (preferences.theme === 'system') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = preferences.theme
  }, [preferences.theme])

  useEffect(() => {
    if (snapshot === null || preferences.selectedKey === null) {
      setSelectedTicket(null)
      return
    }
    const selected = displaySnapshot?.tickets.find((ticket) => ticket.key === preferences.selectedKey) ?? null
    setSelectedTicket(selected)
  }, [preferences.selectedKey, displaySnapshot])

  const visibleTickets = useMemo(() => displaySnapshot === null ? [] : filteredTickets(displaySnapshot, preferences.filters), [displaySnapshot, preferences.filters])
  const filteredWithoutReview = useMemo(() => {
    if (displaySnapshot === null) return []
    return ticketsForAttention(displaySnapshot, preferences.filters)
  }, [displaySnapshot, preferences.filters])
  const reviews = filteredWithoutReview.filter(ticketNeedsReview)
  const attention = filteredWithoutReview.filter((ticket) => ticket.attention.kind === 'attention')
  const repositories = useMemo(() => displaySnapshot === null ? [] : [...new Set(displaySnapshot.tickets.map((ticket) => ticket.repository))].sort(), [displaySnapshot])
  const environments = useMemo(() => displaySnapshot === null ? [] : [...new Set(displaySnapshot.tickets.flatMap((ticket) => ticket.environments))].sort(), [displaySnapshot])
  const activeView = registeredViews.find((view) => view.id === preferences.view) ?? registeredViews[0]

  const updateFilters = (change: Partial<Filters>) => {
    setPreferences((current) => ({ ...current, filters: { ...current.filters, ...change } }))
  }
  const selectTicket = (ticket: Ticket) => {
    setSelectedTicket(ticket)
    setPreferences((current) => ({ ...current, selectedKey: ticket.key }))
  }
  const clearFilters = () => setPreferences((current) => ({ ...current, filters: { ...defaultPreferences.filters } }))
  const setAutoRefresh = (enabled: boolean) => setPreferences((current) => ({
    ...current,
    autoRefresh: enabled,
    refreshSeconds: enabled ? 60 : current.refreshSeconds,
  }))
  const chooseRefreshInterval = (value: string) => {
    const interval = parseRefreshInterval(value)
    if (interval === null) return
    if (interval === 'off') {
      setPreferences((current) => ({ ...current, autoRefresh: false }))
      return
    }
    setPreferences((current) => ({ ...current, autoRefresh: true, refreshSeconds: interval }))
  }

  const loadOlder = async () => {
    if (olderSnapshot === null) {
      const loaded = await refreshSnapshot(true)
      if (!loaded) return
    }
    setViewingOlder(true)
  }

  if ((loading || fetching) && snapshot === null) return <LoadingState />
  if (snapshot === null) return <InitialError message={loadError ?? 'The snapshot could not be loaded.'} onRetry={() => { void refreshSnapshot(false) }} busy={fetching} />
  if (displaySnapshot === null) return <LoadingState />

  const activeSourceFailures = displaySnapshot.sources.filter((source) => !source.ok)
  const warningCount = visibleTickets.filter((ticket) => ticket.warning).length
  const filters = preferences.filters
  const hasFilters = filters.query !== '' || filters.repository !== 'all' || filters.environment !== 'all' || filters.stage !== 'all' || filters.warningOnly || filters.reviewOnly || filters.hideCompleted || filters.activeOnly
  const lastUpdated = formatTimestamp(displaySnapshot.generatedAt)

  return (
    <TooltipProvider>
      <div className="app-shell" data-theme={preferences.theme === 'system' ? undefined : preferences.theme}>
        <header className="topbar">
          <div className="brand-lockup">
            <span className="brand-icon"><Workflow size={17} strokeWidth={2.2} /></span>
            <div className="brand-copy"><strong>Factory progress</strong><span>Read-only workflow map</span></div>
          </div>
          <div className="topbar-right">
            <span className={`connection-state ${displaySnapshot.stale || activeSourceFailures.length ? 'connection-warn' : ''}`}>
              <span className="connection-dot" />
              {displaySnapshot.stale || activeSourceFailures.length ? 'Snapshot has stale sources' : 'Snapshot current'}
            </span>
            <span className="topbar-divider" />
            <span className="updated-time">Updated {lastUpdated}</span>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button className="icon-button" variant="outline" size="icon" aria-label="Refresh displayed snapshot" disabled={fetching} onClick={() => { void refreshVisibleSnapshot() }}>
                  <RefreshCw size={15} className={fetching ? 'spin' : ''} />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Refresh displayed snapshot</TooltipContent>
            </Tooltip>
            <label className="auto-refresh-toggle" htmlFor="auto-refresh-switch"><Switch id="auto-refresh-switch" checked={preferences.autoRefresh} onCheckedChange={setAutoRefresh} /><span>Auto</span></label>
            <Select value={preferences.autoRefresh ? String(preferences.refreshSeconds) : 'off'} onValueChange={chooseRefreshInterval}>
              <SelectTrigger aria-label="Automatic refresh interval" className="select-trigger refresh-select"><SelectValue /></SelectTrigger>
              <SelectContent align="end">
                <SelectItem value="off">Off</SelectItem><SelectItem value="15">Every 15 sec</SelectItem><SelectItem value="30">Every 30 sec</SelectItem><SelectItem value="60">Every minute</SelectItem><SelectItem value="300">Every 5 min</SelectItem>
              </SelectContent>
            </Select>
            <Select value={preferences.theme} onValueChange={(value) => {
              if (value === 'system' || value === 'light' || value === 'dark') setPreferences((current) => ({ ...current, theme: value }))
            }}>
              <SelectTrigger aria-label="Color theme" className="theme-select"><SelectValue /></SelectTrigger>
              <SelectContent align="end">
                <SelectItem value="system"><span className="select-with-icon"><Settings2 size={14} /> System</span></SelectItem>
                <SelectItem value="light"><span className="select-with-icon"><Sun size={14} /> Light</span></SelectItem>
                <SelectItem value="dark"><span className="select-with-icon"><Moon size={14} /> Dark</span></SelectItem>
              </SelectContent>
            </Select>
          </div>
        </header>

        <main className="workspace">
          <section className="workspace-heading">
            <div className="heading-copy">
              <div className="factory-overline"><span className="overline-mark" /> WORKSPACE <ArrowRight size={12} /> <span>{displaySnapshot.factory.name}</span></div>
              <h1>Factory map</h1>
              <p>Workflow, execution rules, and tickets in one view.</p>
            </div>
            <div className="snapshot-meta">
              <span><Clock3 size={13} /> Last successful sync <strong>{formatTimestamp(displaySnapshot.lastSuccessAt)}</strong></span>
              {viewingOlder && <Badge variant="outline" className="older-badge"><Clock3 size={12} /> Older snapshot</Badge>}
            </div>
          </section>

          {(displaySnapshot.stale || activeSourceFailures.length > 0) && (
            <section className="source-notice" role="status">
              <AlertCircle size={16} />
              <div><strong>Showing the last available snapshot</strong><span>{activeSourceFailures.length ? `${activeSourceFailures.map((source) => `${source.name}: ${source.error ?? 'refresh failed'}`).join(' · ')}` : 'A source did not complete its latest refresh.'}</span></div>
              <div className="source-notice-links">{displaySnapshot.sources.map((source) => <span key={source.id} className={source.ok ? 'source-ok' : 'source-failed'}><i />{source.name}</span>)}</div>
            </section>
          )}
          {(viewingOlder ? olderError : loadError) && <div className="refresh-error" role="status"><AlertCircle size={14} /><span>{viewingOlder ? 'Older snapshot refresh failed.' : 'Latest refresh failed.'} Showing the previous snapshot: {viewingOlder ? olderError : loadError}</span><Button variant="ghost" onClick={() => { void refreshVisibleSnapshot() }}>Retry</Button></div>}

          {reviews.length + attention.length > 0 && (
            <section className="attention-strip" aria-label="Human attention">
              {reviews.length > 0 && <div className="attention-stat review-stat"><span className="attention-symbol"><UserRoundCheck size={15} /></span><span><strong>{reviews.length}</strong> {reviews.length === 1 ? 'review needs' : 'reviews need'} a person</span><Button variant="ghost" className={`attention-action ${preferences.filters.reviewOnly ? 'attention-action-active' : ''}`} onClick={() => updateFilters({ reviewOnly: !preferences.filters.reviewOnly })}>{preferences.filters.reviewOnly ? 'Show all tickets' : 'Show reviews'}</Button></div>}
              {attention.length > 0 && <div className="attention-stat"><span className="attention-symbol generic"><AlertCircle size={15} /></span><span><strong>{attention.length}</strong> {attention.length === 1 ? 'ticket needs' : 'tickets need'} attention</span></div>}
            </section>
          )}

          <section className="summary-strip" aria-label="Filtered snapshot summary">
            <div><span className="summary-number">{visibleTickets.length}</span><span>{visibleTickets.length === 1 ? 'ticket' : 'tickets'}</span><span className="summary-caption">matching filters</span></div>
            <span className="summary-divider" />
            <div><span className="summary-number warning-number">{warningCount}</span><span>{warningCount === 1 ? 'age warning' : 'age warnings'}</span></div>
            <span className="summary-divider" />
            <div><span className="summary-number review-number">{reviews.length}</span><span>{reviews.length === 1 ? 'review needed' : 'reviews needed'}</span></div>
            <div className="summary-right"><span>{displaySnapshot.tickets.length} in snapshot</span><span className="summary-dot">·</span><span>{displaySnapshot.stages.length} stages</span></div>
          </section>

          <section className="toolbar" aria-label="Ticket filters and view controls">
            <Tabs value={activeView?.id ?? ''} onValueChange={(value) => {
              if (registeredViews.some((view) => view.id === value)) setPreferences((current) => ({ ...current, view: value }))
            }} className="view-tabs">
              <TabsList>{registeredViews.map((view) => <TabsTrigger key={view.id} value={view.id}>{view.icon && <view.icon size={14} />}{view.label}</TabsTrigger>)}</TabsList>
            </Tabs>
            <span className="toolbar-rule" />
            <label className="search-control"><Search size={15} /><Input type="search" value={preferences.filters.query} placeholder="Search tickets" aria-label="Search tickets" onChange={(event) => updateFilters({ query: event.target.value })} /></label>
            <Select value={preferences.filters.repository} onValueChange={(value) => updateFilters({ repository: value })}>
              <SelectTrigger aria-label="Filter by repository" className="filter-select"><SelectValue placeholder="All repositories" /></SelectTrigger>
              <SelectContent><SelectItem value="all">All repositories</SelectItem>{repositories.map((repository) => <SelectItem key={repository} value={repository}>{repository}</SelectItem>)}</SelectContent>
            </Select>
            <Select value={preferences.filters.environment} onValueChange={(value) => updateFilters({ environment: value })}>
              <SelectTrigger aria-label="Filter by environment" className="filter-select"><SelectValue placeholder="All environments" /></SelectTrigger>
              <SelectContent><SelectItem value="all">All environments</SelectItem>{environments.map((environment) => <SelectItem key={environment} value={environment}>{environment}</SelectItem>)}</SelectContent>
            </Select>
            <Select value={preferences.filters.stage} onValueChange={(value) => updateFilters({ stage: value })}>
              <SelectTrigger aria-label="Filter by stage" className="filter-select stage-filter"><SelectValue placeholder="All stages" /></SelectTrigger>
              <SelectContent><SelectItem value="all">All stages</SelectItem>{displaySnapshot.stages.map((stage) => <SelectItem key={stage.id} value={stage.id}>{stage.name}</SelectItem>)}</SelectContent>
            </Select>
            <Button variant={preferences.filters.warningOnly ? 'secondary' : 'outline'} className={`filter-button ${preferences.filters.warningOnly ? 'warning-filter-active' : ''}`} aria-pressed={preferences.filters.warningOnly} onClick={() => updateFilters({ warningOnly: !preferences.filters.warningOnly })}><AlertTriangle size={14} /> Warnings</Button>
            <Button variant={preferences.filters.reviewOnly ? 'secondary' : 'outline'} className={`filter-button review-filter ${preferences.filters.reviewOnly ? 'review-filter-active' : ''}`} aria-pressed={preferences.filters.reviewOnly} onClick={() => updateFilters({ reviewOnly: !preferences.filters.reviewOnly })}><UserRoundCheck size={14} /> Review</Button>
            <Button variant="outline" className="hide-completed" aria-pressed={preferences.filters.hideCompleted} onClick={() => updateFilters({ hideCompleted: !preferences.filters.hideCompleted })}><Check size={13} /> {preferences.filters.hideCompleted ? 'Completed hidden' : 'Hide completed'}</Button>
            <Button variant={preferences.filters.activeOnly ? 'secondary' : 'outline'} className={`filter-button ${preferences.filters.activeOnly ? 'active-only-active' : ''}`} aria-pressed={preferences.filters.activeOnly} title="Hide draft and completed tickets" onClick={() => updateFilters({ activeOnly: !preferences.filters.activeOnly })}>Active only</Button>
            {hasFilters && <Button variant="ghost" className="clear-filters" onClick={clearFilters}>Clear</Button>}
            {olderSnapshot !== null
              ? <Button variant={viewingOlder ? 'secondary' : 'outline'} className="older-toggle" onClick={() => setViewingOlder((current) => !current)}><Clock3 size={14} />{viewingOlder ? 'Latest snapshot' : 'Older snapshot'}</Button>
              : <Button variant="outline" className="older-toggle" disabled={olderLoading || fetching} onClick={() => { void loadOlder() }}>{olderLoading ? <LoaderCircle size={14} className="spin" /> : <ArrowDownToLine size={14} />}{olderLoading ? 'Loading…' : 'Load older'}</Button>}
          </section>
          {olderError && <div className="inline-error" role="alert"><AlertCircle size={14} /> Older snapshot could not be loaded: {olderError} <Button variant="ghost" onClick={() => { void loadOlder() }}>Retry</Button></div>}

          <section className="view-heading-row">
            <div><h2>{activeView?.label ?? 'Progress view'}</h2><p>{activeView?.description ?? 'Select a registered progress view.'}</p></div>
            <div className="view-count"><span>{visibleTickets.length}</span> visible</div>
          </section>

          {visibleTickets.length === 0
            ? <EmptyState hasFilters={hasFilters} onClear={clearFilters} />
            : activeView
              ? <activeView.component snapshot={displaySnapshot} tickets={visibleTickets} onSelectTicket={selectTicket} selectedKey={preferences.selectedKey} />
              : <div className="empty-state"><strong>No progress views registered</strong></div>}

          <footer className="workspace-footer"><span><span className="footer-status" /> Read-only snapshot</span><span>{displaySnapshot.sources.length} sources <span className="footer-separator">·</span> Generated {lastUpdated}</span></footer>
        </main>
        <Suspense fallback={null}>
          <TicketDetails snapshot={displaySnapshot} ticket={selectedTicket} onClose={() => {
            setSelectedTicket(null)
            setPreferences((current) => ({ ...current, selectedKey: null }))
          }} />
        </Suspense>
      </div>
    </TooltipProvider>
  )
}

function LoadingState() {
  return <main className="loading-screen" role="status" aria-live="polite"><span className="loading-mark"><Workflow size={20} /></span><strong>Loading factory snapshot</strong><span>Reading the latest progress data…</span></main>
}

function InitialError({ message, onRetry, busy }: { message: string; onRetry: () => void; busy: boolean }) {
  return <main className="error-screen"><span className="error-icon"><AlertCircle size={21} /></span><h1>Snapshot unavailable</h1><p>{message}</p><Button onClick={onRetry} disabled={busy}>{busy ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />} Try again</Button></main>
}

function EmptyState({ hasFilters, onClear }: { hasFilters: boolean; onClear: () => void }) {
  return <section className="empty-state"><span className="empty-icon">{hasFilters ? <Search size={19} /> : <GitPullRequest size={20} />}</span><strong>{hasFilters ? 'No tickets match these filters' : 'No tickets in this snapshot'}</strong><p>{hasFilters ? 'Change or clear a filter to see more tickets.' : 'The sources did not report any tickets for this factory.'}</p>{hasFilters && <Button variant="outline" onClick={onClear}>Clear filters</Button>}</section>
}

export default App
