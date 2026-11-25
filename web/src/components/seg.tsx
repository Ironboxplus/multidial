import { cn } from '@/lib/utils'

// Segmented buttons: the single control for "pick one of a few values".
export function Seg<T extends string | number>({
  value,
  onChange,
  opts,
}: {
  value: T
  onChange: (v: T) => void
  opts: [T, string][]
}) {
  return (
    <div className="flex rounded-full border border-border p-0.5 text-[11px]">
      {opts.map(([v, l]) => (
        <button
          key={v}
          onClick={() => v !== value && onChange(v)}
          className={cn('rounded-full px-2 py-0.5', v === value ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground')}
        >
          {l}
        </button>
      ))}
    </div>
  )
}
