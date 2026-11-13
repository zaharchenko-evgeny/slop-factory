import { z } from 'zod'

const conditionSchema = z.object({ key: z.string(), value: z.string() })

const evaluationSchema = z.object({
  key: z.string(),
  value: z.string(),
  result: z.enum(['matched', 'not-matched', 'unknown']),
  reason: z.string(),
})

const nodeSchema = z.object({
  id: z.string(),
  role: z.string(),
  stages: z.array(z.string()),
  triggers: z.array(z.string()),
  conditions: z.array(conditionSchema),
  outcomes: z.array(z.object({
    name: z.string(),
    conditions: z.array(z.string()),
    effects: z.array(z.string()),
    targets: z.array(z.string()),
  })),
  background: z.boolean(),
})

const pullRequestSchema = z.object({
  number: z.string(),
  url: z.string().nullable(),
  repository: z.string(),
  head: z.string(),
  state: z.string(),
  ci: z.string(),
  requestedReviewers: z.array(z.string()),
})

const ticketSchema = z.object({
  key: z.string(),
  id: z.string(),
  title: z.string(),
  url: z.string().nullable(),
  repository: z.string(),
  environments: z.array(z.string()),
  stage: z.string(),
  stageEnteredAt: z.string().nullable(),
  ageSeconds: z.number().nullable(),
  warning: z.boolean(),
  attention: z.object({
    kind: z.enum(['review', 'attention', 'none']),
    reason: z.string().nullable(),
    reviewers: z.array(z.string()),
    url: z.string().nullable(),
  }),
  prs: z.array(pullRequestSchema),
  evidenceErrors: z.array(z.string()).default([]),
  blockers: z.array(z.string()),
  history: z.array(z.object({ stage: z.string(), at: z.string().nullable() })),
  nodes: z.array(z.object({
    id: z.string(),
    trigger: z.enum(['matched', 'not-matched', 'unknown']),
    conditions: z.array(evaluationSchema),
    eligibility: z.enum(['matched', 'not-matched', 'unknown']),
    execution: z.literal('unknown'),
    reason: z.string(),
  })),
})

export const snapshotSchema = z.object({
  version: z.literal(1),
  factory: z.object({ id: z.string(), name: z.string() }),
  generatedAt: z.string(),
  lastSuccessAt: z.string().nullable(),
  stale: z.boolean(),
  sources: z.array(z.object({
    id: z.string(),
    name: z.string(),
    ok: z.boolean(),
    error: z.string().nullable(),
    updatedAt: z.string().nullable(),
  })),
  stages: z.array(z.object({ id: z.string(), name: z.string(), kind: z.enum(['active', 'waiting', 'final']) })),
  nodes: z.array(nodeSchema),
  edges: z.array(z.object({ source: z.string(), target: z.string(), label: z.string(), kind: z.enum(['stage', 'command', 'trigger', 'outcome']) })),
  tickets: z.array(ticketSchema),
})

export type Snapshot = z.infer<typeof snapshotSchema>
export type Ticket = Snapshot['tickets'][number]
export type WorkflowNode = Snapshot['nodes'][number]
export type Stage = Snapshot['stages'][number]
export type Edge = Snapshot['edges'][number]
export type NodeEvaluation = Ticket['nodes'][number]

export function parseSnapshot(input: unknown): Snapshot {
  return snapshotSchema.parse(input)
}
