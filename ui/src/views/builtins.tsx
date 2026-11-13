import { Columns3, Workflow } from 'lucide-react'
import { WorkflowGraph } from '@/components/WorkflowGraph'
import { WorkflowView } from '@/components/WorkflowView'
import { registerProgressView, type ProgressViewProps } from './registry'

function WorkflowCanvas(props: ProgressViewProps) {
  return <WorkflowGraph {...props} />
}

function TicketColumns(props: ProgressViewProps) {
  return <WorkflowView {...props} />
}

registerProgressView({
  id: 'workflow',
  label: 'Workflow',
  description: 'Explore declared stage routes and inspect tickets and execution nodes.',
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
