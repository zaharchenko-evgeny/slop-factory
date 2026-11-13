import { Columns3, Workflow } from 'lucide-react'
import { WorkflowView } from '@/components/WorkflowView'
import { registerProgressView, type ProgressViewProps } from './registry'

function WorkflowCanvas(props: ProgressViewProps) {
  return <WorkflowView {...props} />
}

function TicketColumns(props: ProgressViewProps) {
  return <WorkflowView {...props} columns />
}

registerProgressView({
  id: 'workflow',
  label: 'Workflow',
  description: 'Stages, nodes, and declared routes from the current factory definition.',
  icon: Workflow,
  component: WorkflowCanvas,
})

registerProgressView({
  id: 'columns',
  label: 'Columns',
  description: 'Tickets grouped by their current stage.',
  icon: Columns3,
  component: TicketColumns,
})
