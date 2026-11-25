import { cn } from '@/lib/utils'

export function MenuBtn({
  icon,
  children,
  onClick,
  className,
  disabled,
}: {
  icon: React.ReactNode
  children: React.ReactNode
  onClick: () => void
  className?: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn('flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs hover:bg-accent disabled:opacity-50 [&_svg]:h-3.5 [&_svg]:w-3.5', className)}
    >
      {icon}
      {children}
    </button>
  )
}
