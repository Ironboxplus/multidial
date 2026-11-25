import type { Dial, HistoryEvent, StickyPin } from '@/api'

// "dial6" -> "6": lines are told apart by this number, never by colour.
export const lineNo = (device: string): string => device.match(/\d+/)?.[0] ?? device

export interface Interval {
  start: number
  end: number // now when still ongoing
  ongoing: boolean
  kind: 'down' | 'stopped'
  detail: string
}

// Pair each down with the next up (stopped with started) of the same dial.
export function intervals(
  history: HistoryEvent[],
  device: string,
  from: number,
  now: number,
  dial?: Dial,
): Interval[] {
  const evs = history.filter((e) => e.device === device).sort((a, b) => a.ts - b.ts)
  const out: Interval[] = []
  let down: HistoryEvent | null = null
  let stop: HistoryEvent | null = null
  for (const e of evs) {
    if (e.kind === 'down') down = down ?? e
    else if (e.kind === 'up' && down) {
      out.push({ start: down.ts, end: e.ts, ongoing: false, kind: 'down', detail: down.detail })
      down = null
    } else if (e.kind === 'stopped') stop = stop ?? e
    else if (e.kind === 'started' && stop) {
      out.push({ start: stop.ts, end: e.ts, ongoing: false, kind: 'stopped', detail: '' })
      stop = null
    }
  }
  if (down && dial && !dial.online && !dial.stopped) {
    out.push({ start: down.ts, end: now, ongoing: true, kind: 'down', detail: down.detail })
  }
  if (stop && dial?.stopped) out.push({ start: stop.ts, end: now, ongoing: true, kind: 'stopped', detail: '' })
  return out.filter((iv) => iv.end >= from)
}

export function availability(ivs: Interval[], from: number, now: number): number {
  const lost = ivs
    .filter((iv) => iv.kind === 'down')
    .reduce((s, iv) => s + (iv.end - Math.max(iv.start, from)), 0)
  return Math.max(0, 1 - lost / Math.max(1, now - from))
}

// A client pinned by a sticky rule should map to exactly one dial; more than one
// means the rule is not holding (the restore step is missing).
export function pinsByClient(pins: StickyPin[], rules: string[]): { client: string; devices: string[] }[] {
  const m = new Map<string, Set<string>>()
  for (const p of pins) {
    if (!rules.includes(p.rule)) continue
    if (!m.has(p.client)) m.set(p.client, new Set())
    m.get(p.client)!.add(p.device)
  }
  const tail = (ip: string) => Number(ip.split('.')[3])
  return [...m.entries()]
    .map(([client, s]) => ({ client, devices: [...s].sort() }))
    .sort((a, b) => tail(a.client) - tail(b.client))
}

export function fmtMbit(kbit: number): string {
  const m = kbit / 1000
  if (m >= 100) return m.toFixed(0)
  if (m >= 10) return m.toFixed(1).replace(/\.0$/, '')
  return m.toFixed(2).replace(/0$/, '').replace(/\.0$/, '')
}

export function fmtDur(sec: number): string {
  sec = Math.max(0, Math.round(sec))
  if (sec < 60) return `${sec} 秒`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
  return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`
}

export function fmtClock(ts: number): string {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function isCidr(s: string): boolean {
  const m = s.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(\/(\d{1,2}))?$/)
  if (!m) return false
  if ([1, 2, 3, 4].some((i) => Number(m[i]) > 255)) return false
  return m[6] === undefined || Number(m[6]) <= 32
}

export function isDomain(s: string): boolean {
  return /^(?=.{3,253}$)([a-z0-9-]+\.)+[a-z]{2,}$/i.test(s)
}

export const errMsg = (e: unknown): string => (e instanceof Error ? e.message : String(e))
