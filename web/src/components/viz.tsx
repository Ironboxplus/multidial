import { useMemo, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { Gauge, TriangleAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import { fmtClock, fmtDur, fmtMbit, type Interval } from '@/lib/model'
import { LineBadge } from '@/components/line-badge'
import { KEEP, TRAFFIC_MS } from '@/lib/traffic'

// ─── live throughput: download grows up, upload mirrors down ────────────────

export function Spark({
  series,
  peak,
  dim,
}: {
  series: [number, number][]
  peak: number // shared kbit/s scale so rows are comparable
  dim?: boolean
}) {
  const W = 240
  const H = 40
  const mid = 26 // download gets the larger half
  const n = series.length
  // fixed KEEP slots, newest at the right edge: a fresh page fills in from the right
  // instead of stretching a handful of samples across the whole width
  const x = (i: number) => W - ((n - 1 - i) / (KEEP - 1)) * W
  const yDl = (v: number) => mid - Math.sqrt(v / peak) * (mid - 2)
  const yUl = (v: number) => mid + Math.sqrt(v / peak) * (H - mid - 2)
  const dl = series.map((p, i) => `${x(i)},${yDl(p[0])}`).join(' ')
  const ul = series.map((p, i) => `${x(i)},${yUl(p[1])}`).join(' ')
  const [hover, setHover] = useState<number | null>(null)
  const ref = useRef<SVGSVGElement>(null)
  const hv = hover !== null ? series[hover] : null
  return (
    <div className="relative">
      <svg
        ref={ref}
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className={cn('h-10 w-full overflow-visible', dim && 'opacity-40')}
        onPointerMove={(e) => {
          const r = ref.current!.getBoundingClientRect()
          const back = Math.round((1 - (e.clientX - r.left) / r.width) * (KEEP - 1)) // slots from the right
          setHover(n - 1 - back >= 0 ? Math.min(n - 1, n - 1 - back) : null)
        }}
        onPointerLeave={() => setHover(null)}
      >
        <line x1="0" x2={W} y1={mid} y2={mid} stroke="currentColor" className="text-border" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        {n > 1 && (
          <>
            <polygon points={`${x(0)},${mid} ${dl} ${W},${mid}`} className="fill-sky-400" opacity="0.28" />
            <polyline points={dl} fill="none" className="stroke-sky-400" strokeWidth="2" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
            <polygon points={`${x(0)},${mid} ${ul} ${W},${mid}`} className="fill-sky-400" opacity="0.14" />
          </>
        )}
        {hover !== null && n > 1 && (
          <line x1={x(hover)} x2={x(hover)} y1="0" y2={H} stroke="currentColor" className="text-foreground/40" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        )}
      </svg>
      {hv && (
        <div className="pointer-events-none absolute -top-7 right-0 z-10 whitespace-nowrap rounded-md border border-border bg-card px-2 py-0.5 text-[11px] tabular-nums shadow-lg">
          {Math.round(((n - 1 - hover!) * TRAFFIC_MS) / 1000)} 秒前 · ↓{fmtMbit(hv[0])} ↑{fmtMbit(hv[1])} Mbit/s
        </div>
      )}
    </div>
  )
}

// ─── shaper: where the cap sits in min..max, and how close traffic is to it ──

export interface Range {
  min: number
  base: number
  max: number
}

export function ShaperBar({
  device,
  dir,
  actual,
  cap,
  limits,
  autorate,
}: {
  device: string
  dir: '↓' | '↑'
  actual: number
  cap: number | null
  limits: Range | null
  autorate: boolean
}) {
  // no autorate range known: scale to what is on screen
  const max = limits?.max ?? Math.max(cap ?? 0, actual, 1000) * 1.25
  const pos = (v: number) => `${Math.min(100, (v / max) * 100)}%`
  const hot = cap !== null && actual >= cap * 0.85
  return (
    <div className="group relative flex items-center gap-1.5">
      <span className="w-3 text-[10px] text-muted-foreground">{dir}</span>
      <div className="relative h-2.5 flex-1 rounded-full bg-muted/60">
        {/* the range autorate may move the cap in */}
        {limits && (
          <div className="absolute inset-y-0 rounded-full bg-foreground/[0.06]" style={{ left: pos(limits.min), right: 0 }} />
        )}
        <motion.div
          className="absolute inset-y-0 left-0 rounded-full bg-sky-400"
          animate={{ width: pos(actual) }}
          transition={{ type: 'spring', stiffness: 120, damping: 20 }}
        />
        {/* base rate tick */}
        {limits && <div className="absolute -inset-y-0.5 w-px bg-foreground/25" style={{ left: pos(limits.base) }} />}
        {cap !== null && (
          <motion.div
            className={cn(
              'absolute -inset-y-1 w-[3px] -translate-x-1/2 rounded-full',
              hot ? 'bg-amber-400 shadow-[0_0_8px] shadow-amber-400/70' : 'bg-foreground/80',
              !autorate && !hot && 'bg-foreground/40',
            )}
            animate={{ left: pos(cap) }}
            transition={{ type: 'spring', stiffness: 160, damping: 22 }}
          />
        )}
      </div>
      <span className={cn('w-14 text-right text-[11px] tabular-nums', hot ? 'text-amber-300' : 'text-muted-foreground')}>
        {/* the base tick already says "at base"; a number only when the cap moved */}
        {cap === null ? '无整形' : !limits || cap !== limits.base || hot ? fmtMbit(cap) : ''}
        {hot && <TriangleAlert className="ml-0.5 inline h-3 w-3 -translate-y-px" />}
      </span>
      <div className="pointer-events-none absolute -top-9 left-4 z-10 hidden items-center gap-1 whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-[11px] shadow-lg group-hover:flex">
        <LineBadge device={device} />
        <Gauge className="h-3 w-3" />
        {dir === '↓' ? '下行' : '上行'}实际 {fmtMbit(actual)} · 上限 {cap === null ? '无' : fmtMbit(cap)} Mbit/s
        {limits && autorate ? `（自适应 ${fmtMbit(limits.min)}–${fmtMbit(limits.max)}）` : '（固定）'}
      </div>
    </div>
  )
}

// ─── outage strip: one row per dial on a shared time axis ───────────────────

export function OutageStrip({
  ivs,
  from,
  now,
  hoverT,
  setHoverT,
}: {
  ivs: Interval[]
  from: number
  now: number
  hoverT: number | null
  setHoverT: (t: number | null) => void
}) {
  const span = now - from
  const pct = (t: number) => ((Math.min(Math.max(t, from), now) - from) / span) * 100
  const ref = useRef<HTMLDivElement>(null)
  const near = useMemo(() => {
    if (hoverT === null) return null
    const tol = span / 120
    return ivs.find((iv) => hoverT >= iv.start - tol && hoverT <= iv.end + tol) ?? null
  }, [hoverT, ivs, span])
  return (
    <div
      ref={ref}
      className="relative h-6 cursor-crosshair"
      onPointerMove={(e) => {
        const r = ref.current!.getBoundingClientRect()
        setHoverT(from + ((e.clientX - r.left) / r.width) * span)
      }}
      onPointerLeave={() => setHoverT(null)}
    >
      <div className="absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-emerald-500/25" />
      {ivs.map((iv, i) => (
        <div
          key={i}
          className={cn(
            'absolute top-1/2 h-4 -translate-y-1/2 rounded-[3px]',
            iv.kind === 'down' ? 'bg-red-500' : 'bg-zinc-500',
            iv.ongoing && 'animate-pulse',
            near === iv && 'ring-2 ring-foreground/70',
          )}
          style={{ left: `${pct(iv.start)}%`, width: `max(3px, ${pct(iv.end) - pct(iv.start)}%)` }}
        />
      ))}
      {hoverT !== null && (
        <div className="pointer-events-none absolute inset-y-0 w-px bg-foreground/50" style={{ left: `${pct(hoverT)}%` }} />
      )}
      {near && (
        <div
          className="pointer-events-none absolute bottom-7 z-10 -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-[11px] shadow-lg"
          style={{ left: `${Math.min(85, Math.max(15, pct(near.start)))}%` }}
        >
          {fmtClock(near.start)} {near.kind === 'down' ? '掉线' : '停止'}
          {near.ongoing ? ' · 至今 ' : ' · '}
          {fmtDur(near.end - near.start)}
          {near.detail && <span className="text-muted-foreground"> · {near.detail}</span>}
        </div>
      )}
    </div>
  )
}

export function TimeAxis({ from, now }: { from: number; now: number }) {
  const span = now - from
  const ticks = span > 2 * 86400 ? [0, 1, 2, 3].map((d) => now - d * 86400) : [0, 6, 12, 18, 24].map((h) => now - h * 3600)
  return (
    <div className="relative h-4 text-[10px] text-muted-foreground">
      {ticks.map((t) => (
        <span
          key={t}
          className="absolute -translate-x-1/2 whitespace-nowrap first:translate-x-[-100%]"
          style={{ left: `${((t - from) / span) * 100}%` }}
        >
          {t === now ? '现在' : span > 2 * 86400 ? `${Math.round((now - t) / 86400)} 天前` : `${Math.round((now - t) / 3600)} 小时前`}
        </span>
      ))}
    </div>
  )
}
