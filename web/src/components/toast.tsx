import { createContext, useCallback, useContext, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Check, TriangleAlert, Undo2 } from 'lucide-react'

interface Toast {
  id: number
  text: string
  undo?: () => void
  error?: boolean
}
type Show = (text: string, undo?: () => void, error?: boolean) => void
const Ctx = createContext<Show>(() => {})

// "Applied" with an undo button, gone after a few seconds; errors stay longer.
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toast, setToast] = useState<Toast | null>(null)
  const timer = useRef<number>()
  const show = useCallback<Show>((text, undo, error) => {
    window.clearTimeout(timer.current)
    setToast({ id: Date.now(), text, undo, error })
    timer.current = window.setTimeout(() => setToast(null), error ? 8000 : 5000)
  }, [])
  return (
    <Ctx.Provider value={show}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 bottom-5 z-[60] flex justify-center px-4">
        <AnimatePresence>
          {toast && (
            <motion.div
              key={toast.id}
              initial={{ opacity: 0, y: 16, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8 }}
              transition={{ type: 'spring', stiffness: 420, damping: 32 }}
              className="pointer-events-auto flex items-center gap-3 rounded-full border border-border bg-card/95 py-2 pl-3 pr-2 text-sm shadow-2xl shadow-black/60 backdrop-blur"
            >
              {toast.error ? (
                <TriangleAlert className="h-4 w-4 shrink-0 text-red-400" />
              ) : (
                <Check className="h-4 w-4 shrink-0 text-emerald-400" />
              )}
              <span className={toast.error ? 'text-red-200' : undefined}>{toast.text}</span>
              {toast.undo && (
                <button
                  className="flex items-center gap-1 rounded-full px-2.5 py-1 text-xs text-sky-300 hover:bg-sky-500/15"
                  onClick={() => {
                    toast.undo?.()
                    setToast(null)
                  }}
                >
                  <Undo2 className="h-3.5 w-3.5" />
                  撤销
                </button>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </Ctx.Provider>
  )
}
export const useToast = () => useContext(Ctx)
