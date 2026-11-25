import { useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ArrowRight, Globe, Pin, Shuffle, TriangleAlert, X } from 'lucide-react'
import type { Dial, Sticky, StickyPatch } from '@/api'
import { isCidr, isDomain, pinsByClient } from '@/lib/model'
import { cn } from '@/lib/utils'
import { LineBadge } from '@/components/line-badge'
import { Seg } from '@/components/seg'

export type SaveSticky = (patch: StickyPatch, text: string, undo: StickyPatch) => void

const TIMEOUTS: [number, string][] = [
  [300, '5 分'],
  [600, '10 分'],
  [1800, '30 分'],
  [3600, '60 分'],
]

const INTRA_RULES = ['md_intra0', 'md_intra1']
const SVC_RULES = ['md_svc']

// ─── routing: rules in match order, each pointing at where traffic goes ─────

export function Routing({ dials, sticky, save }: { dials: Dial[]; sticky: Sticky | null; save: SaveSticky }) {
  const live = dials.filter((d) => d.online && !d.stopped)
  const timeout = (
    <Seg
      value={sticky?.timeout ?? 0}
      onChange={(v) => sticky && save({ sticky_timeout: v }, `固定时长改为 ${v / 60} 分钟`, { sticky_timeout: sticky.timeout })}
      opts={TIMEOUTS.some(([v]) => v === sticky?.timeout) || !sticky ? TIMEOUTS : [[sticky.timeout, `${Math.round(sticky.timeout / 60)} 分`], ...TIMEOUTS]}
    />
  )
  const svcItems = sticky ? [...sticky.service.ranges, ...sticky.service.domains] : []
  return (
    <section className="mt-10">
      <div className="mb-2 text-sm font-medium text-muted-foreground">分流</div>
      <div className="space-y-3">
        {sticky && (
          <>
            <Lane
              n={1}
              icon={<Pin className="h-3.5 w-3.5" />}
              title="校园内网"
              items={sticky.intranet.ranges}
              onChange={(next, text) => save({ intranet_ranges: next }, text, { intranet_ranges: sticky.intranet.ranges })}
              dest={<StickyDest sticky={sticky} rules={INTRA_RULES} timeout={timeout} />}
              off={!sticky.intranet.enabled}
            />
            <Lane
              n={2}
              icon={<Globe className="h-3.5 w-3.5" />}
              title="指定网站"
              items={svcItems}
              domains={{ supported: sticky.domains_supported }}
              onChange={(next, text) => {
                const patch = {
                  sticky_ranges: next.filter((x) => !isDomain(x)),
                  sticky_domains: next.filter((x) => isDomain(x)),
                }
                save(patch, text, { sticky_ranges: sticky.service.ranges, sticky_domains: sticky.service.domains })
              }}
              collapseAfter={6}
              dest={<StickyDest sticky={sticky} rules={SVC_RULES} timeout={timeout} />}
            />
          </>
        )}
        {!sticky && <div className="px-1 text-xs text-muted-foreground">加载中…</div>}
        <Lane n={3} icon={<Shuffle className="h-3.5 w-3.5" />} title="其余流量" items={[]} dest={<Balance dials={dials} live={live.length} />} />
      </div>
    </section>
  )
}

function Balance({ dials, live }: { dials: Dial[]; live: number }) {
  const pct = Math.round(100 / Math.max(1, live))
  return (
    <div>
      <div className="mb-1.5 text-xs text-muted-foreground">每个新连接分到一条在线的线</div>
      <div className="flex gap-[2px]">
        {dials.map((d) => {
          const on = d.online && !d.stopped
          return (
            <motion.div key={d.device} layout animate={{ flexGrow: on ? 1 : 0.25 }} className="min-w-5 basis-0" title={on ? `${d.device} ${pct}%` : `${d.device} 不参与`}>
              <div
                className={cn('h-3 rounded-full', on && 'bg-foreground/30')}
                style={on ? undefined : { background: 'repeating-linear-gradient(135deg, rgba(148,163,184,.35) 0 3px, transparent 3px 6px)' }}
              />
              <div className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground">
                <LineBadge device={d.device} />
                {on && live <= 5 && <span className="truncate tabular-nums">{pct}%</span>}
              </div>
            </motion.div>
          )
        })}
      </div>
    </div>
  )
}

function Lane({
  n,
  icon,
  title,
  items,
  onChange,
  dest,
  collapseAfter,
  domains,
  off,
}: {
  n: number
  icon: React.ReactNode
  title: string
  items: string[]
  onChange?: (next: string[], text: string) => void
  dest: React.ReactNode
  collapseAfter?: number
  domains?: { supported: boolean } // present = lane accepts domains too
  off?: boolean
}) {
  const [draft, setDraft] = useState('')
  const [open, setOpen] = useState(false)
  const shown = collapseAfter && !open ? items.slice(0, collapseAfter) : items
  const domainDraft = isDomain(draft)
  const blocked = domainDraft && !(domains?.supported ?? false)
  const ok = isCidr(draft) || (domainDraft && !blocked)
  return (
    <div className="grid grid-cols-1 items-start gap-3 rounded-2xl border border-border bg-card/40 p-3 md:grid-cols-[1fr_auto_1.1fr]">
      <div>
        <div className="mb-2 flex items-center gap-2 text-sm">
          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-muted text-[10px] text-muted-foreground">{n}</span>
          <span className="text-muted-foreground">{icon}</span>
          <span className="font-medium">{title}</span>
          {off && <span className="rounded-full border border-border px-1.5 text-[10px] text-muted-foreground">未启用</span>}
        </div>
        {!onChange && <div className="text-xs text-muted-foreground">前面都没匹配上的连接</div>}
        {onChange && (
          <div className="flex flex-wrap items-center gap-1.5">
            <AnimatePresence initial={false}>
              {shown.map((r) => (
                <motion.span
                  key={r}
                  layout
                  initial={{ opacity: 0, scale: 0.8 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.8 }}
                  className="group flex items-center gap-1 rounded-md border border-border bg-background/60 py-0.5 pl-2 pr-1 font-mono text-[11px]"
                >
                  {isDomain(r) && <Globe className="h-3 w-3 text-sky-300" />}
                  {r}
                  <button
                    className="rounded p-0.5 text-muted-foreground opacity-40 hover:bg-red-500/20 hover:text-red-300 group-hover:opacity-100"
                    onClick={() => onChange(items.filter((x) => x !== r), `已移除 ${r}`)}
                    aria-label={`移除 ${r}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </motion.span>
              ))}
            </AnimatePresence>
            {collapseAfter && items.length > collapseAfter && (
              <button className="rounded-md px-2 py-0.5 text-[11px] text-sky-300 hover:bg-sky-500/10" onClick={() => setOpen(!open)}>
                {open ? '收起' : `+${items.length - collapseAfter}`}
              </button>
            )}
            <form
              className="flex items-center gap-1.5"
              onSubmit={(e) => {
                e.preventDefault()
                if (!ok || items.includes(draft)) return
                onChange([...items, draft], `已添加 ${draft}`)
                setDraft('')
              }}
            >
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value.trim())}
                placeholder={domains ? '+ 网段或域名' : '+ 网段'}
                className={cn(
                  'h-6 w-36 rounded-md !border !border-dashed bg-transparent px-2 font-mono text-[11px] outline-none focus:!border-sky-400',
                  draft && !ok && '!border-red-500/60 focus:!border-red-500/60',
                )}
              />
              {blocked && <span className="text-[11px] text-red-300">需 dnsmasq ipset</span>}
            </form>
          </div>
        )}
      </div>
      <ArrowRight className="hidden h-4 w-4 self-center text-muted-foreground md:block" />
      <div>{dest}</div>
    </div>
  )
}

function StickyDest({ sticky, rules, timeout }: { sticky: Sticky; rules: string[]; timeout: React.ReactNode }) {
  const clients = useMemo(() => pinsByClient(sticky.pins, rules), [sticky.pins, rules])
  const split = clients.filter((c) => c.devices.length > 1)
  const health = rules.map((r) => sticky.health[r]).filter(Boolean)
  // the restore rules are the cause, so health decides; pins are only the fallback evidence
  const broken = health.length ? health.some((h) => !h.ok) : split.length > 0
  const tip = health.map((h) => `在线 ${h.online.join(' ')} · 恢复 ${h.restore.join(' ')}`).join(' / ')
  const [more, setMore] = useState(false)
  const LIMIT = 14
  const shown = more ? clients : clients.slice(0, LIMIT)
  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        每台设备固定走一条线
        {timeout}
        {broken && (
          <span title={tip} className="flex items-center gap-1 rounded-full bg-red-500/15 px-2 py-0.5 text-red-300">
            <TriangleAlert className="h-3 w-3" />
            未生效{!health.length && ` · ${split.length}/${clients.length} 台设备分到了多条线`}
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-1">
        {shown.map((c) => (
          <span
            key={c.client}
            title={c.client}
            className={cn(
              'flex items-center gap-1 rounded-md border py-0.5 pl-1.5 pr-0.5 font-mono text-[11px]',
              c.devices.length > 1 ? 'border-red-500/50' : 'border-border',
            )}
          >
            .{c.client.split('.')[3]}
            {c.devices.map((dv) => (
              <LineBadge key={dv} device={dv} />
            ))}
          </span>
        ))}
        {clients.length > LIMIT && (
          <button className="rounded-md px-2 py-0.5 text-[11px] text-sky-300 hover:bg-sky-500/10" onClick={() => setMore(!more)}>
            {more ? '收起' : `+${clients.length - LIMIT}`}
          </button>
        )}
      </div>
    </div>
  )
}
