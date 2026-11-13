import { z } from 'zod'
import type { Snapshot } from './model'

const filterSchema = z.object({
  query: z.string(),
  repository: z.string(),
  environment: z.string(),
  stage: z.string(),
  warningOnly: z.boolean(),
  reviewOnly: z.boolean(),
  hideCompleted: z.boolean(),
  activeOnly: z.boolean().default(false),
})

const preferencesSchema = z.object({
  view: z.string().min(1),
  theme: z.enum(['system', 'light', 'dark']),
  autoRefresh: z.boolean(),
  refreshSeconds: z.union([z.literal(15), z.literal(30), z.literal(60), z.literal(300)]),
  filters: filterSchema,
  selectedKey: z.string().nullable(),
})

export type Filters = z.infer<typeof filterSchema>
export type Preferences = z.infer<typeof preferencesSchema>

export const defaultFilters: Filters = {
  query: '',
  repository: 'all',
  environment: 'all',
  stage: 'all',
  warningOnly: false,
  reviewOnly: false,
  hideCompleted: false,
  activeOnly: false,
}

export const defaultPreferences: Preferences = {
  view: 'workflow',
  theme: 'system',
  autoRefresh: false,
  refreshSeconds: 60,
  filters: defaultFilters,
  selectedKey: null,
}

function storageKey(factoryId: string): string {
  return `factory-progress:${encodeURIComponent(factoryId)}:preferences:v1`
}

export function loadPreferences(factoryId: string): Preferences {
  try {
    const raw = window.localStorage.getItem(storageKey(factoryId))
    if (raw === null) return defaultPreferences
    const parsed: unknown = JSON.parse(raw)
    const result = preferencesSchema.safeParse(parsed)
    return result.success ? result.data : defaultPreferences
  } catch {
    return defaultPreferences
  }
}

export function savePreferences(factoryId: string, preferences: Preferences): void {
  try {
    window.localStorage.setItem(storageKey(factoryId), JSON.stringify(preferences))
  } catch {
    // The view still works when browser storage is disabled or full.
  }
}

export function filteredTickets(snapshot: Snapshot, filters: Filters): Snapshot['tickets'] {
  const query = filters.query.trim().toLocaleLowerCase()
  return snapshot.tickets.filter((ticket) => {
    const stage = snapshot.stages.find((item) => item.id === ticket.stage)
    const haystack = [ticket.key, ticket.id, ticket.title, ticket.repository, ticket.stage, stage?.name ?? '', ...ticket.environments]
      .join(' ')
      .toLocaleLowerCase()
    if (query.length > 0 && !haystack.includes(query)) return false
    if (filters.repository !== 'all' && ticket.repository !== filters.repository) return false
    if (filters.environment !== 'all' && !ticket.environments.includes(filters.environment)) return false
    if (filters.stage !== 'all' && ticket.stage !== filters.stage) return false
    if (filters.warningOnly && !ticket.warning) return false
    if (filters.hideCompleted && (ticket.state === 'closed' || stage?.kind === 'final')) return false
    if (filters.activeOnly && (ticket.state === 'closed' || ticket.stage === 'draft' || stage?.kind === 'final')) return false
    if (filters.reviewOnly && ticket.attention.kind !== 'review') return false
    return true
  })
}

export function ticketsForAttention(snapshot: Snapshot, filters: Filters): Snapshot['tickets'] {
  return filteredTickets(snapshot, { ...filters, reviewOnly: false })
}

export function ticketNeedsReview(ticket: Snapshot['tickets'][number]): boolean {
  return ticket.attention.kind === 'review'
}

export function formatAge(ageSeconds: number | null): string {
  if (ageSeconds === null) return 'Age unknown'
  if (!Number.isFinite(ageSeconds) || ageSeconds < 0) return 'Age unknown'
  const minutes = Math.floor(ageSeconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  if (hours < 24) return remainder === 0 ? `${hours}h` : `${hours}h ${remainder}m`
  const days = Math.floor(hours / 24)
  const remainingHours = hours % 24
  return remainingHours === 0 ? `${days}d` : `${days}d ${remainingHours}h`
}

export function formatTimestamp(value: string | null): string {
  if (value === null) return 'Unknown'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return 'Unknown'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

export function safeHttpUrl(value: string | null): string | null {
  if (value === null) return null
  try {
    const url = new URL(value)
    const safeProtocol = url.protocol === 'http:' || url.protocol === 'https:'
    const hasCredentials = url.username.length > 0 || url.password.length > 0
    return safeProtocol && !hasCredentials ? url.href : null
  } catch {
    return null
  }
}

export function stageLabel(snapshot: Snapshot, stageId: string): string {
  return snapshot.stages.find((stage) => stage.id === stageId)?.name ?? `Unknown stage: ${stageId}`
}
