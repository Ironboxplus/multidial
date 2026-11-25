// API client for the multi-dial daemon. The LuCI template injects
// window.__MULTIDIAL_API__ so fetches stay same-origin behind the LuCI session;
// it defaults to /api for standalone/dev use.

export interface Account {
  name: string
  userid: string
  dials: number
  enabled: boolean
  // per-account rollup of its dials' counters (since daemon start)
  reauth: number
  down: number
}

export interface Dial {
  device: string
  slot: number
  account_userid: string
  account_name: string
  macaddr: string
  online: boolean
  authed: boolean
  outcome: string | null
  detail: string
  stopped: boolean
  mark: string
  ts: number
  // health counters since daemon start; *_ts are epoch seconds, 0 = never
  reauth: number
  down: number
  last_reauth_ts: number
  last_down_ts: number
}

export interface RoutingPolicy {
  intranet_sticky: boolean
  intranet_ranges: string[]
  sticky_timeout: number
  dns_rebind_domains: string[]
}

export interface Snapshot {
  enabled: boolean
  interval: number
  phy_ifname: string
  accounts: Account[]
  dials: Dial[]
  aggregate: { total: number; online: number }
  policy?: RoutingPolicy
  reconcile_count: number
  reauth_count: number
  last_tick: number
}

export interface HistoryEvent {
  device: string
  account_userid: string
  kind: 'down' | 'up' | 'stopped' | 'started' | 'marked' | 'rotated' | 'removed'
  ts: number
  detail: string
}

export interface AccountInput {
  name: string
  userid: string
  password?: string
  dials: number
  enabled: boolean
}

const API: string =
  (window as unknown as { __MULTIDIAL_API__?: string }).__MULTIDIAL_API__ || '/api'

async function asJson<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try {
      const e = (await r.json()) as { error?: string }
      if (e && e.error) msg = e.error
    } catch {
      /* ignore */
    }
    throw new Error(msg)
  }
  return r.json() as Promise<T>
}

// The LuCI proxy answers 200 with the daemon's body whatever its status, so a
// daemon 400/500 only shows up as an `error` field.
async function asJsonStrict<T>(r: Response): Promise<T> {
  const body = await asJson<T & { error?: string }>(r)
  if (body && typeof body.error === 'string') throw new Error(body.error)
  return body
}

const jsonPost = (path: string, body?: unknown) =>
  fetch(`${API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

// --- status + whole-daemon control ---
export function getStatus(): Promise<Snapshot> {
  return fetch(`${API}/status`, { headers: { Accept: 'application/json' } }).then(
    asJson<Snapshot>,
  )
}

export function reauth(): Promise<Snapshot> {
  return jsonPost('/reauth').then(asJson<Snapshot>)
}

export function reload(): Promise<Snapshot> {
  return jsonPost('/reload').then(asJson<Snapshot>)
}

export function setPower(enabled: boolean): Promise<Snapshot> {
  return jsonPost('/power', { enabled }).then(asJson<Snapshot>)
}

// --- per-dial actions ---
export function stopDial(device: string): Promise<Snapshot> {
  return jsonPost(`/dial/${encodeURIComponent(device)}/stop`).then(asJson<Snapshot>)
}

export function startDial(device: string): Promise<Snapshot> {
  return jsonPost(`/dial/${encodeURIComponent(device)}/start`).then(asJson<Snapshot>)
}

// Re-authenticate ONE dial.
export function reauthDial(device: string): Promise<Snapshot> {
  return jsonPost(`/dial/${encodeURIComponent(device)}/reauth`).then(asJson<Snapshot>)
}

export function markDial(device: string, mark: string): Promise<Snapshot> {
  return jsonPost(`/dial/${encodeURIComponent(device)}/mark`, { mark }).then(
    asJson<Snapshot>,
  )
}

// Rotate a blocked dial to a fresh MAC the AC has never seen (recovery).
export function rotateDial(device: string): Promise<Snapshot> {
  return jsonPost(`/dial/${encodeURIComponent(device)}/rotate`).then(asJson<Snapshot>)
}

// Decommission ONE dial (the only delete path; lowering the count never deletes).
export function decommissionDial(device: string): Promise<Snapshot> {
  return fetch(`${API}/dial/${encodeURIComponent(device)}`, {
    method: 'DELETE',
    headers: { Accept: 'application/json' },
  }).then(asJson<Snapshot>)
}

// --- account CRUD ---
export function saveAccount(input: AccountInput): Promise<Snapshot> {
  return jsonPost('/account', input).then(asJson<Snapshot>)
}

export function deleteAccount(name: string): Promise<Snapshot> {
  return fetch(`${API}/account/${encodeURIComponent(name)}`, {
    method: 'DELETE',
    headers: { Accept: 'application/json' },
  }).then(asJson<Snapshot>)
}

// --- SQM status ---

export interface SqmDial {
  device: string
  cake: boolean
  autorate: boolean
  dl_kbit: number | null
  ul_kbit: number | null
  dl_dropped: number | null
  ul_dropped: number | null
  dl_backlog_pkt: number | null
  ul_backlog_pkt: number | null
}

// cake-autorate range per dial (kbit/s)
export interface DialLimits {
  dl_min: number
  dl_base: number
  dl_max: number
  ul_min: number
  ul_base: number
  ul_max: number
}

export interface SqmStatus {
  enabled: boolean
  hnat_offload: boolean
  // NSS routers: ECM / NSS qdisc conditions that bypass or double the per-dial CAKE
  offload_warnings?: string[]
  dials: SqmDial[]
  aggregate: { total: number; active: number }
  limits?: Record<string, DialLimits>
}

export function getSqm(): Promise<SqmStatus> {
  return fetch(`${API}/sqm`, { headers: { Accept: 'application/json' } }).then(
    asJson<SqmStatus>,
  )
}

// --- log + history ---
export function getLog(limit = 200): Promise<string[]> {
  return fetch(`${API}/log?limit=${limit}`, { headers: { Accept: 'application/json' } })
    .then(asJson<{ lines: string[] }>)
    .then((d) => d.lines)
}

export function getHistory(limit = 50): Promise<HistoryEvent[]> {
  return fetch(`${API}/history?limit=${limit}`, { headers: { Accept: 'application/json' } })
    .then(asJson<{ events: HistoryEvent[] }>)
    .then((d) => d.events)
}

// --- traffic: cumulative tx bytes, the client turns deltas into rates ---

export interface Traffic {
  ts: number
  dials: Record<string, { down_bytes: number; up_bytes: number }>
}

export function getTraffic(): Promise<Traffic> {
  return fetch(`${API}/traffic`, { headers: { Accept: 'application/json' } }).then(asJsonStrict<Traffic>)
}

// --- sticky routing ---

export interface StickyPin {
  rule: string
  client: string
  device: string
  ttl: number | null // null: ipset member without a timeout
}

export interface StickyHealth {
  online: string[]
  restore: string[]
  ok: boolean
}

export interface Sticky {
  timeout: number
  intranet: { enabled: boolean; ranges: string[] }
  service: { ranges: string[]; domains: string[] }
  domains_supported: boolean
  pins: StickyPin[]
  health: Record<string, StickyHealth>
}

export interface StickyPatch {
  intranet_ranges?: string[]
  sticky_ranges?: string[]
  sticky_domains?: string[]
  sticky_timeout?: number
}

export function getSticky(): Promise<Sticky> {
  return fetch(`${API}/sticky`, { headers: { Accept: 'application/json' } }).then(asJsonStrict<Sticky>)
}

export function saveSticky(patch: StickyPatch): Promise<Sticky> {
  return jsonPost('/sticky', patch).then(asJsonStrict<Sticky>)
}
