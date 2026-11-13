import { useSyncExternalStore, type ComponentType } from 'react'
import type { Snapshot, Ticket } from '../model'

export type ProgressViewProps = {
  snapshot: Snapshot
  tickets: Ticket[]
  onSelectTicket: (ticket: Ticket) => void
  selectedKey: string | null
}

export type ProgressViewRegistration = {
  id: string
  label: string
  description: string
  icon?: ComponentType<{ size?: number }>
  component: ComponentType<ProgressViewProps>
}

let registrations: readonly ProgressViewRegistration[] = []
const listeners = new Set<() => void>()

function notify(): void {
  for (const listener of listeners) listener()
}

export function registerProgressView(registration: ProgressViewRegistration): () => void {
  if (registrations.some((view) => view.id === registration.id)) {
    throw new Error(`Progress view is already registered: ${registration.id}`)
  }
  registrations = [...registrations, registration]
  notify()
  return () => {
    registrations = registrations.filter((view) => view !== registration)
    notify()
  }
}

export function getProgressViews(): readonly ProgressViewRegistration[] {
  return registrations
}

export function useProgressViews(): readonly ProgressViewRegistration[] {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    getProgressViews,
    getProgressViews,
  )
}
