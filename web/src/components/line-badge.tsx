import { cn } from '@/lib/utils'
import { lineNo } from '@/lib/model'

export type LineState = 'online' | 'offline' | 'stopped'

const STATE: Record<LineState | 'plain', string> = {
  plain: 'border-border bg-muted text-foreground',
  online: 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300',
  offline: 'animate-pulse border-red-500/60 bg-red-500/20 text-red-300',
  stopped: 'border-zinc-500/60 bg-transparent text-zinc-400',
}

// The one identity mark for a line: its number. Colour is reserved for `state`.
export function LineBadge({
  device,
  state,
  size = 'sm',
  className,
}: {
  device: string
  state?: LineState
  size?: 'sm' | 'lg'
  className?: string
}) {
  return (
    <span
      title={device}
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-md border font-semibold tabular-nums',
        size === 'lg' ? 'h-7 min-w-7 px-1.5 text-sm' : 'h-5 min-w-5 px-1 text-[11px]',
        STATE[state ?? 'plain'],
        className,
      )}
    >
      {lineNo(device)}
    </span>
  )
}

export const stateOf = (d: { online: boolean; stopped: boolean }): LineState =>
  d.stopped ? 'stopped' : d.online ? 'online' : 'offline'
