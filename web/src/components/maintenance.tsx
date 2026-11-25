import { useCallback, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { BookOpen, ChevronDown, KeyRound, RefreshCw, Wrench } from 'lucide-react'
import { getLog } from '@/api'
import { usePoll } from '@/lib/poll'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'

// Only mounted while the log is open, so it polls only then.
function LogView() {
  const [lines, setLines] = useState<string[]>([])
  const refresh = useCallback(async () => {
    try {
      setLines(await getLog(200))
    } catch {
      // keep the last lines
    }
  }, [])
  usePoll(refresh, 5000)
  return (
    <motion.pre
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      className="mt-3 max-h-72 overflow-auto rounded-lg bg-black/40 p-3 font-mono text-[11px] leading-relaxed text-emerald-300/80"
    >
      {lines.join('\n')}
    </motion.pre>
  )
}

// ─── maintenance: fallback tools live here, next to the log ─────────────────

export function Maintenance({
  active,
  reauthCount,
  reconcileCount,
  busy,
  onReauthAll,
  onReload,
}: {
  active: number
  reauthCount: number
  reconcileCount: number
  busy: boolean
  onReauthAll: () => void
  onReload: () => void
}) {
  const [log, setLog] = useState(false)
  return (
    <section className="mt-10 rounded-2xl border border-dashed border-border p-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Wrench className="h-3.5 w-3.5" />
        <span className="mr-2">维护</span>
        <Button variant="outline" size="sm" className="h-7" disabled={busy} onClick={onReauthAll}>
          <KeyRound />
          全部重新认证（{active} 条线）
        </Button>
        <Button variant="outline" size="sm" className="h-7" disabled={busy} onClick={onReload} title="手动改过 /etc/config/multidial 之后用">
          <RefreshCw />
          重新加载配置
        </Button>
        <span className="tabular-nums">
          累计重认证 {reauthCount} 次 · 重载 {reconcileCount} 次
        </span>
        <button className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 hover:bg-accent hover:text-foreground" onClick={() => setLog(!log)}>
          <BookOpen className="h-3.5 w-3.5" />
          日志
          <ChevronDown className={cn('h-3 w-3 transition-transform', log && 'rotate-180')} />
        </button>
      </div>
      <AnimatePresence initial={false}>{log && <LogView />}</AnimatePresence>
    </section>
  )
}
