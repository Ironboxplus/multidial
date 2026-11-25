import { useState } from 'react'
import { ChevronDown, KeyRound, Plus, Trash2 } from 'lucide-react'
import type { Account, AccountInput } from '@/api'
import { errMsg } from '@/lib/model'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { MenuBtn } from '@/components/menu-btn'

const field = 'h-8 w-full rounded-md border border-border bg-transparent px-2 text-sm outline-none focus:border-sky-400'

// Add (no `a`) or edit one account. Only the userid is ever shown; the password
// field is write-only and an empty value keeps the stored one.
function AccountForm({
  a,
  onSave,
  onDelete,
  done,
}: {
  a?: Account
  onSave: (input: AccountInput) => Promise<void>
  onDelete?: () => void
  done: () => void
}) {
  const [name, setName] = useState('')
  const [userid, setUserid] = useState(a?.userid ?? '')
  const [pw, setPw] = useState('')
  const [on, setOn] = useState(a?.enabled ?? true)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [arm, setArm] = useState(false)
  return (
    <form
      className="space-y-2"
      onSubmit={async (e) => {
        e.preventDefault()
        setBusy(true)
        setErr(null)
        try {
          await onSave({ name: a?.name ?? name.trim(), userid: userid.trim(), password: pw, dials: a?.dials ?? 1, enabled: on })
          done()
        } catch (x) {
          setErr(errMsg(x))
        } finally {
          setBusy(false)
        }
      }}
    >
      {!a && <input required className={field} placeholder="名称" value={name} onChange={(e) => setName(e.target.value)} />}
      <input required className={field} placeholder="学号" value={userid} onChange={(e) => setUserid(e.target.value)} />
      <input
        required={!a}
        className={field}
        type="password"
        autoComplete="new-password"
        placeholder={a ? '密码（不改留空）' : '密码'}
        value={pw}
        onChange={(e) => setPw(e.target.value)}
      />
      {err && <p className="text-xs text-red-400">{err}</p>}
      <div className="flex items-center justify-between pt-1">
        <label className="flex items-center gap-2 text-xs">
          <Switch checked={on} onCheckedChange={setOn} className="scale-75" />
          {on ? '启用' : '停用'}
        </label>
        <div className="flex items-center gap-1">
          {onDelete && (
            <MenuBtn
              icon={<Trash2 />}
              className={cn('text-rose-300', arm && 'bg-rose-500/20')}
              onClick={() => (arm ? onDelete() : setArm(true))}
            >
              {arm ? '再点删除' : '删除'}
            </MenuBtn>
          )}
          <Button type="submit" size="sm" className="h-7" disabled={busy}>
            {a ? '保存' : '添加'}
          </Button>
        </div>
      </div>
    </form>
  )
}

export function AccountPopover({
  a,
  trigger,
  triggerClass,
  onSave,
  onDelete,
}: {
  a?: Account
  trigger: React.ReactNode
  triggerClass?: string
  onSave: (input: AccountInput) => Promise<void>
  onDelete?: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger className={triggerClass}>{trigger}</PopoverTrigger>
      <PopoverContent className="w-64">
        <AccountForm
          a={a}
          onSave={onSave}
          onDelete={
            onDelete &&
            (() => {
              setOpen(false)
              onDelete()
            })
          }
          done={() => setOpen(false)}
        />
      </PopoverContent>
    </Popover>
  )
}

// ─── account: a box that holds its dials ─────────────────────────────────────

export function AccountBox({
  a,
  children,
  onSave,
  onDelete,
  onAddLine,
}: {
  a: Account
  children: React.ReactNode
  onSave: (input: AccountInput) => Promise<void>
  onDelete: () => void
  onAddLine: () => void
}) {
  return (
    <div className="rounded-2xl border border-border bg-card/40">
      <div className="flex items-center gap-2 px-3 pt-2 text-xs text-muted-foreground">
        <AccountPopover
          a={a}
          onSave={onSave}
          onDelete={onDelete}
          triggerClass="flex items-center gap-1.5 rounded-md px-1 py-0.5 hover:bg-accent hover:text-foreground"
          trigger={
            <>
              <KeyRound className="h-3 w-3" />
              <span className="font-medium text-foreground/90">{a.name}</span>
              <span className="font-mono">{a.userid}</span>
              <ChevronDown className="h-3 w-3" />
            </>
          }
        />
        <span className="tabular-nums">掉线 {a.down} 次</span>
        <button className="ml-auto flex items-center gap-1 rounded-md px-1.5 py-0.5 hover:bg-accent hover:text-foreground" onClick={onAddLine}>
          <Plus className="h-3 w-3" />
          线路
        </button>
      </div>
      <div className="divide-y divide-border/60">{children}</div>
    </div>
  )
}
