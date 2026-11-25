import { useState } from 'react'
import { motion } from 'framer-motion'
import { ChevronDown, KeyRound, Pause, Play, Repeat, Tag, Trash2, TriangleAlert } from 'lucide-react'
import type { Dial, DialLimits, HistoryEvent, SqmDial } from '@/api'
import { availability, fmtDur, fmtMbit, intervals, type Interval } from '@/lib/model'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { LineBadge } from '@/components/line-badge'
import { MenuBtn } from '@/components/menu-btn'
import { OutageStrip, ShaperBar, Spark } from '@/components/viz'

export interface DialActions {
  reauth: () => void
  stop: () => void
  start: () => void
  rotate: () => void
  remove: () => void
  mark: (mark: string) => void
}

function Chip({ className, children }: { className?: string; children: React.ReactNode }) {
  return <span className={cn('inline-flex items-center gap-1 rounded-full border px-1.5 py-px text-[11px]', className)}>{children}</span>
}

// ─── one dial: identity, live load, shaper, history — on one row ────────────

export function DialRow({
  d,
  sq,
  limits,
  series,
  peak,
  history,
  from,
  now,
  hoverT,
  setHoverT,
  busy,
  act,
}: {
  d: Dial
  sq?: SqmDial
  limits?: DialLimits
  series: [number, number][]
  peak: number
  history: HistoryEvent[]
  from: number
  now: number
  hoverT: number | null
  setHoverT: (t: number | null) => void
  busy: boolean
  act: DialActions
}) {
  const ivs: Interval[] = intervals(history, d.device, from, now, d)
  const avail = availability(ivs, from, now)
  const last = series[series.length - 1] ?? [0, 0]
  const live = d.online && !d.stopped
  const [dl, ul] = live ? last : [0, 0]
  const down = !d.online && !d.stopped
  const range = (dir: 'dl' | 'ul') =>
    limits ? { min: limits[`${dir}_min`], base: limits[`${dir}_base`], max: limits[`${dir}_max`] } : null
  return (
    <motion.div
      layout
      className={cn(
        'relative grid grid-cols-1 gap-x-5 gap-y-2 px-3 py-3 md:grid-cols-[150px_1fr_1fr] lg:grid-cols-[150px_minmax(150px,1fr)_minmax(170px,1fr)_38%]',
        down && 'bg-red-500/[0.06]',
      )}
    >
      {(down || d.stopped) && (
        <span className={cn('absolute inset-y-2 left-0 w-1 rounded-r-full', down ? 'bg-red-500' : 'bg-zinc-500/60')} />
      )}

      {/* identity + status (+ the one action that matters when it is down) */}
      <div className="flex items-center gap-2 md:block">
        <DialMenu d={d} busy={busy} act={act} />
        <div className="flex flex-wrap items-center gap-1.5 md:mt-1">
          {d.stopped ? (
            <Chip className="border-zinc-500/40 text-zinc-300">
              <Pause className="h-3 w-3" />已停止
            </Chip>
          ) : down ? (
            <Chip className="border-red-500/50 bg-red-500/15 text-red-300">
              <TriangleAlert className="h-3 w-3" />离线{d.last_down_ts ? ` ${fmtDur(now - d.last_down_ts)}` : ''}
            </Chip>
          ) : (
            <Chip className="border-emerald-500/30 text-emerald-300">在线</Chip>
          )}
          {d.mark && (
            <Chip className="border-amber-500/40 text-amber-300">
              <Tag className="h-3 w-3" />{d.mark}
            </Chip>
          )}
        </div>
        {down && (
          <button
            disabled={busy}
            onClick={act.reauth}
            className="ml-auto flex items-center gap-1 rounded-md !border !border-red-500/40 px-2 py-0.5 text-xs text-red-200 hover:bg-red-500/20 disabled:opacity-50 md:ml-0 md:mt-1.5"
          >
            <KeyRound className="h-3 w-3" />
            重新认证
          </button>
        )}
      </div>

      {/* live load */}
      <div>
        <Spark series={series} peak={peak} dim={!live} />
        <div className="mt-0.5 flex gap-3 text-[11px] tabular-nums text-muted-foreground">
          <span>
            ↓ <span className="text-foreground">{fmtMbit(dl)}</span>
          </span>
          <span>↑ {fmtMbit(ul)}</span>
          <span>Mbit/s</span>
        </div>
      </div>

      {/* shaper: cap inside the autorate range vs actual load */}
      <div className="flex flex-col justify-center gap-2">
        <ShaperBar device={d.device} dir="↓" actual={dl} cap={sq?.cake ? sq.dl_kbit : null} limits={range('dl')} autorate={!!sq?.autorate} />
        <ShaperBar device={d.device} dir="↑" actual={ul} cap={sq?.cake ? sq.ul_kbit : null} limits={range('ul')} autorate={!!sq?.autorate} />
      </div>

      {/* history on the shared axis */}
      <div className="flex items-center gap-2 md:col-span-3 lg:col-span-1">
        <div className="flex-1">
          <OutageStrip ivs={ivs} from={from} now={now} hoverT={hoverT} setHoverT={setHoverT} />
        </div>
        <span
          className={cn(
            'w-14 rounded-full px-1.5 py-0.5 text-center text-[11px] font-medium tabular-nums',
            avail > 0.999 ? 'bg-emerald-500/15 text-emerald-300' : avail > 0.99 ? 'bg-amber-500/15 text-amber-300' : 'bg-red-500/15 text-red-300',
          )}
          title={`${ivs.filter((i) => i.kind === 'down').length} 次掉线`}
        >
          {(avail * 100).toFixed(avail > 0.999 ? 2 : 1)}%
        </span>
      </div>
    </motion.div>
  )
}

function DialMenu({ d, busy, act }: { d: Dial; busy: boolean; act: DialActions }) {
  const [open, setOpen] = useState(false)
  const [mark, setMark] = useState(d.mark)
  const [arm, setArm] = useState(false)
  const run = (fn: () => void) => () => {
    setOpen(false)
    fn()
  }
  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o)
        setArm(false)
        if (o) setMark(d.mark)
      }}
    >
      <PopoverTrigger className="flex items-center gap-1.5 rounded-md text-sm font-semibold hover:text-sky-300">
        <LineBadge device={d.device} size="lg" />
        <span className="font-mono">{d.device}</span>
        <ChevronDown className="h-3 w-3 text-muted-foreground" />
      </PopoverTrigger>
      <PopoverContent className="w-60 space-y-2">
        <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
          <span className="text-muted-foreground">MAC</span>
          <span className="font-mono">{d.macaddr}</span>
          <span className="text-muted-foreground">认证</span>
          <span className="truncate">{d.detail || '—'}</span>
          <span className="text-muted-foreground">累计</span>
          <span>
            掉线 {d.down} · 重认证 {d.reauth}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-1.5 pt-1">
          <MenuBtn icon={<KeyRound />} disabled={busy} onClick={run(act.reauth)}>
            重新认证
          </MenuBtn>
          {d.stopped ? (
            <MenuBtn icon={<Play />} disabled={busy} onClick={run(act.start)}>
              启动
            </MenuBtn>
          ) : (
            <MenuBtn icon={<Pause />} disabled={busy} onClick={run(act.stop)}>
              停止
            </MenuBtn>
          )}
          <MenuBtn icon={<Repeat />} disabled={busy} onClick={run(act.rotate)}>
            换 MAC
          </MenuBtn>
          <MenuBtn
            icon={<Trash2 />}
            disabled={busy}
            className={cn('text-rose-300', arm && 'bg-rose-500/20')}
            onClick={() => (arm ? run(act.remove)() : setArm(true))}
          >
            {arm ? '再点删除' : '删除'}
          </MenuBtn>
        </div>
        <form
          className="flex gap-1 pt-1"
          onSubmit={(e) => {
            e.preventDefault()
            run(() => act.mark(mark.trim()))()
          }}
        >
          <input
            className="h-7 min-w-0 flex-1 rounded-md !border bg-transparent px-2 text-xs outline-none focus:!border-sky-400"
            placeholder="标记"
            value={mark}
            onChange={(e) => setMark(e.target.value)}
          />
          <Button type="submit" size="sm" variant="outline" className="h-7 px-2" disabled={busy}>
            <Tag />
          </Button>
        </form>
      </PopoverContent>
    </Popover>
  )
}
