import * as React from 'react'
import { Progress as ProgressPrimitive } from 'radix-ui'
import { cn } from 'cn'

function Progress({
  className,
  value,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root>) {
  const progressValue = value ?? null
  const translate = progressValue === null ? -100 : progressValue - 100

  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      className={cn('relative h-2 w-full overflow-hidden rounded-full bg-primary/20', className)}
      value={progressValue}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className="h-full w-full flex-1 bg-primary transition-transform"
        style={{ transform: `translateX(${translate}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}

export { Progress }
