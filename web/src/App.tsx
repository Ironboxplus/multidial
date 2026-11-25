import { useCallback, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import { KeyRound, Network, Pause, Plus, Power, TriangleAlert } from 'lucide-react'
import {
  decommissionDial,
  deleteAccount,
  getHistory,
  getSqm,
  getStatus,
  getSticky,
  markDial,
  reauth,
  reauthDial,
  reload,
  rotateDial,
  saveAccount,
  saveSticky,
  setPower,
  startDial,
  stopDial,
  type AccountInput,
  type HistoryEvent,
  type Snapshot,
  type SqmStatus,
  type Sticky,
  type StickyPatch,
} from '@/api'
import { cn } from '@/lib/utils'
import { errMsg, fmtMbit } from '@/lib/model'
import { usePoll } from '@/lib/poll'
import { useTraffic } from '@/lib/traffic'
import { Switch } from '@/components/ui/switch'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { ToastProvider, useToast } from '@/components/toast'
import { LineBadge, stateOf } from '@/components/line-badge'
import { Seg } from '@/components/seg'
import { TimeAxis } from '@/components/viz'
import { AccountBox, AccountPopover } from '@/components/account'
import { DialRow } from '@/components/dial-row'
import { Routing } from '@/components/routing'
import { Maintenance } from '@/components/maintenance'

export default function Root() {
  return (
    <ToastProvider>
      <App />
    </ToastProvider>
  )
}

function App() {
  const toast = useToast()
  const [status, setStatus] = useState<Snapshot | null>(null)
  const [connError, setConnError] = useState<string | null>(null)
  const [sqm, setSqm] = useState<SqmStatus | null>(null)
  const [sticky, setSticky] = useState<Sticky | null>(null)
  const [history, setHistory] = useState<HistoryEvent[]>([])
  const series = useTraffic()

  const [busy, setBusy] = useState<string[]>([])
  const [powerAsk, setPowerAsk] = useState(false)
  const [range, setRange] = useState<'24h' | '3d'>('3d')
  const [hoverT, setHoverT] = useState<number | null>(null)

  usePoll(
    useCallback(async () => {
      try {
        setStatus(await getStatus())
        setConnError(null)
      } catch (e) {
        setConnError(errMsg(e))
      }
    }, []),
    5000,
  )
  usePoll(
    useCallback(async () => {
      try {
        setSqm(await getSqm())
      } catch {
        // keep the last picture
      }
    }, []),
    4000,
  )
  usePoll(
    useCallback(async () => {
      try {
        setSticky(await getSticky())
      } catch {
        // keep the last picture
      }
    }, []),
    10000,
  )
  usePoll(
    useCallback(async () => {
      try {
        setHistory(await getHistory(500))
      } catch {
        // keep the last picture
      }
    }, []),
    30000,
  )

  // Run one daemon action: show the returned snapshot, report the outcome, offer undo.
  const run = async (key: string, fn: () => Promise<Snapshot>, ok: string, undo?: () => void) => {
    setBusy((b) => [...b, key])
    try {
      setStatus(await fn())
      toast(ok, undo)
    } catch (e) {
      toast(errMsg(e), undefined, true)
    } finally {
      setBusy((b) => b.filter((k) => k !== key))
    }
  }

  const saveSticky_ = async (patch: StickyPatch, text: string, undo: StickyPatch) => {
    const before = sticky
    setSticky((s) =>
      s && {
        ...s,
        timeout: patch.sticky_timeout ?? s.timeout,
        intranet: { ...s.intranet, ranges: patch.intranet_ranges ?? s.intranet.ranges },
        service: {
          ranges: patch.sticky_ranges ?? s.service.ranges,
          domains: patch.sticky_domains ?? s.service.domains,
        },
      },
    )
    try {
      setSticky(await saveSticky(patch))
      toast(text, () => saveSticky_(undo, '已撤销', patch))
    } catch (e) {
      setSticky(before)
      toast(errMsg(e), undefined, true)
    }
  }

  const dials = status?.dials ?? []
  const accounts = status?.accounts ?? []
  const enabled = status?.enabled ?? true
  const now = status?.last_tick || Date.now() / 1000
  const from = now - (range === '3d' ? 3 * 86400 : 86400)
  const peak = useMemo(() => Math.max(1000, ...Object.values(series).flat().flat()), [series])
  const last = (dev: string) => series[dev]?.[series[dev].length - 1] ?? [0, 0]

  const offline = dials.filter((d) => !d.online && !d.stopped)
  const live = dials.filter((d) => d.online && !d.stopped)
  const totDl = live.reduce((s, d) => s + last(d.device)[0], 0)
  const totUl = live.reduce((s, d) => s + last(d.device)[1], 0)
  const bypassed = !!sqm && (sqm.hnat_offload || (sqm.offload_warnings ?? []).length > 0)

  const groups = accounts
    .filter((a) => a.enabled && dials.some((d) => d.account_name === a.name))
    .map((a) => ({ a, ds: dials.filter((d) => d.account_name === a.name) }))
  const parked = accounts.filter((a) => !groups.some((g) => g.a.name === a.name))

  const reauthAll = () => run('all', reauth, '已对全部线路重新认证')
  const saveAcc = async (input: AccountInput, text: string) => {
    const snap = await saveAccount(input)
    setStatus(snap)
    toast(text)
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <Modal open={powerAsk} onClose={() => setPowerAsk(false)} title="关闭多拨">
        <p className="mb-5 text-sm text-muted-foreground">所有拨号断开，流量回到单条 WAN。</p>
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={() => setPowerAsk(false)}>
            取消
          </Button>
          <Button
            size="sm"
            className="bg-red-600 text-white hover:bg-red-700"
            onClick={() => {
              setPowerAsk(false)
              run('power', () => setPower(false), '多拨已关闭', () => run('power', () => setPower(true), '多拨已开启'))
            }}
          >
            关闭
          </Button>
        </div>
      </Modal>

      <div className="mx-auto max-w-6xl px-3 py-5 sm:px-5 md:py-8">
        {/* ── header: what the whole thing is doing right now ── */}
        <div className="flex flex-wrap items-center gap-x-6 gap-y-4">
          <div className="flex items-center gap-3">
            <div
              className={cn(
                'flex h-11 w-11 items-center justify-center rounded-2xl border',
                !enabled
                  ? 'border-border bg-muted text-muted-foreground'
                  : offline.length
                    ? 'border-amber-500/40 bg-amber-500/10 text-amber-300'
                    : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
              )}
            >
              <Network className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-semibold">多拨</h1>
                <label className="flex cursor-pointer items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-[11px]">
                  <Power className={cn('h-3 w-3', enabled ? 'text-emerald-400' : 'text-red-400')} />
                  {enabled ? '运行中' : '已关闭'}
                  <Switch
                    checked={enabled}
                    disabled={!status || busy.includes('power')}
                    onCheckedChange={(v) => (v ? run('power', () => setPower(true), '多拨已开启') : setPowerAsk(true))}
                    className="scale-75"
                    aria-label="总开关"
                  />
                </label>
                {connError && (
                  <span title={connError} className="flex items-center gap-1 text-[11px] text-amber-300">
                    <TriangleAlert className="h-3 w-3" />
                    {status ? '连接中断' : '无法连接'}
                  </span>
                )}
              </div>
              <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                {dials.map((d) => (
                  <LineBadge key={d.device} device={d.device} state={stateOf(d)} />
                ))}
                <span className="ml-1 text-xs text-muted-foreground">
                  {status ? `${live.length}/${dials.length} 条线在线 · ${status.phy_ifname}` : '连接中…'}
                </span>
              </div>
            </div>
          </div>

          {/* aggregate throughput: hero number + who carries it */}
          <div className="ml-auto flex min-w-[260px] flex-1 items-end gap-5 sm:flex-none">
            <div>
              <div className="flex items-baseline gap-1">
                <span className="text-3xl font-semibold tracking-tight tabular-nums">{fmtMbit(totDl)}</span>
                <span className="text-xs text-muted-foreground">↓ Mbit/s</span>
                <span className="ml-3 text-lg tabular-nums text-muted-foreground">{fmtMbit(totUl)}</span>
                <span className="text-xs text-muted-foreground">↑</span>
              </div>
              <div className="mt-1.5 flex w-64 max-w-full gap-[2px]">
                {live.map((d) => {
                  const dl = last(d.device)[0]
                  return (
                    <motion.div
                      key={d.device}
                      title={`${d.device} ↓${fmtMbit(dl)}`}
                      animate={{ flexGrow: Math.max(0.02, dl) }}
                      className="min-w-0 basis-0"
                    >
                      <div className="h-2 rounded-full bg-sky-400" />
                      {/* number only where the segment is wide enough to own it */}
                      {dl >= totDl * 0.1 && <LineBadge device={d.device} className="mt-1" />}
                    </motion.div>
                  )
                })}
              </div>
            </div>
          </div>
        </div>

        {/* ── lines ── */}
        <section className="mt-8">
          <div className="mb-2 flex flex-wrap items-end gap-3">
            <div className="text-sm font-medium text-muted-foreground">线路</div>
            {offline.length >= 2 && (
              <button
                disabled={busy.includes('all')}
                onClick={reauthAll}
                className="flex items-center gap-1.5 rounded-full !border !border-red-500/40 bg-red-500/10 px-2.5 py-0.5 text-xs text-red-300 hover:bg-red-500/20 disabled:opacity-50"
              >
                <KeyRound className="h-3 w-3" />
                {offline.length} 条线离线 · 全部重新认证
              </button>
            )}
            {bypassed && (
              <span className="flex items-center gap-1 text-xs text-amber-300">
                <TriangleAlert className="h-3 w-3" />
                整形被硬件加速绕过
              </span>
            )}
            <div className="ml-auto hidden w-[38%] items-end gap-2 lg:flex">
              <div className="flex-1">
                <TimeAxis from={from} now={now} />
              </div>
              <Seg value={range} onChange={setRange} opts={[['24h', '24 小时'], ['3d', '3 天']]} />
            </div>
          </div>

          <div className="space-y-3">
            {groups.map(({ a, ds }) => (
              <AccountBox
                key={a.name}
                a={a}
                onSave={(input) => saveAcc(input, `${a.name} 已更新`)}
                onDelete={() => run('acc', () => deleteAccount(a.name), `已删除 ${a.name}`)}
                onAddLine={() =>
                  run(
                    'acc',
                    () => saveAccount({ name: a.name, userid: a.userid, dials: a.dials + 1, enabled: a.enabled }),
                    `${a.name} 增加一条线`,
                  )
                }
              >
                {ds.map((d) => {
                  const dev = d.device
                  const key = `dial:${dev}`
                  return (
                    <DialRow
                      key={dev}
                      d={d}
                      sq={sqm?.dials.find((s) => s.device === dev)}
                      limits={sqm?.limits?.[dev]}
                      series={series[dev] ?? []}
                      peak={peak}
                      history={history}
                      from={from}
                      now={now}
                      hoverT={hoverT}
                      setHoverT={setHoverT}
                      busy={busy.includes(key)}
                      act={{
                        reauth: () => run(key, () => reauthDial(dev), `${dev} 已重新认证`),
                        stop: () => run(key, () => stopDial(dev), `${dev} 已停止`, () => run(key, () => startDial(dev), `${dev} 已启动`)),
                        start: () => run(key, () => startDial(dev), `${dev} 已启动`, () => run(key, () => stopDial(dev), `${dev} 已停止`)),
                        rotate: () => run(key, () => rotateDial(dev), `${dev} 已换 MAC`),
                        remove: () => run(key, () => decommissionDial(dev), `已删除 ${dev}`),
                        mark: (m) =>
                          run(key, () => markDial(dev, m), m ? `${dev} 标记为 ${m}` : `${dev} 已清除标记`, () =>
                            run(key, () => markDial(dev, d.mark), `${dev} 标记已恢复`),
                          ),
                      }}
                    />
                  )
                })}
              </AccountBox>
            ))}

            {/* parked: disabled or zero-dial accounts, plus add */}
            {status && (
              <div className="flex flex-wrap items-center gap-2 pt-1">
                {parked.map((a) => (
                  <AccountPopover
                    key={a.name}
                    a={a}
                    onSave={(input) => saveAcc(input, `${a.name} 已更新`)}
                    onDelete={() => run('acc', () => deleteAccount(a.name), `已删除 ${a.name}`)}
                    triggerClass="flex items-center gap-2 rounded-full border border-dashed border-border px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
                    trigger={
                      <>
                        <Pause className="h-3 w-3" />
                        {a.name} · {a.userid}
                        <span className="text-muted-foreground/60">{a.enabled ? '0 条线' : '已停用'}</span>
                      </>
                    }
                  />
                ))}
                <AccountPopover
                  onSave={(input) => saveAcc(input, '已添加账号')}
                  triggerClass="flex items-center gap-1.5 rounded-full border border-dashed border-border px-3 py-1 text-xs text-muted-foreground hover:border-foreground/40 hover:text-foreground"
                  trigger={
                    <>
                      <Plus className="h-3 w-3" />
                      添加账号
                    </>
                  }
                />
              </div>
            )}
          </div>
        </section>

        <Routing dials={dials} sticky={sticky} save={saveSticky_} />

        <Maintenance
          active={dials.filter((d) => !d.stopped).length}
          reauthCount={status?.reauth_count ?? 0}
          reconcileCount={status?.reconcile_count ?? 0}
          busy={busy.length > 0}
          onReauthAll={reauthAll}
          onReload={() => run('reload', reload, '已重新加载配置')}
        />
      </div>
    </div>
  )
}
