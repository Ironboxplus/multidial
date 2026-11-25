import * as React from 'react'
import * as P from '@radix-ui/react-popover'
import { cn } from '@/lib/utils'

export const Popover = P.Root
export const PopoverTrigger = P.Trigger
export const PopoverClose = P.Close

export const PopoverContent = React.forwardRef<
  React.ElementRef<typeof P.Content>,
  React.ComponentPropsWithoutRef<typeof P.Content>
>(({ className, align = 'start', sideOffset = 6, ...props }, ref) => (
  // portal into the app root: the `dark` class lives there, not on <body> (LuCI page)
  <P.Portal container={document.getElementById('multidial-root')}>
    <P.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      collisionPadding={8}
      className={cn(
        'z-50 rounded-xl border border-border bg-card p-3 text-sm text-foreground shadow-2xl shadow-black/50 outline-none',
        className,
      )}
      {...props}
    />
  </P.Portal>
))
PopoverContent.displayName = 'PopoverContent'
