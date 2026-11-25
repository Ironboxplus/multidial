import { useCallback, useRef, useState } from 'react'
import { getTraffic, type Traffic } from '@/api'
import { usePoll } from '@/lib/poll'

export type Series = Record<string, [number, number][]> // dial -> [down, up] kbit/s

export const KEEP = 150
export const TRAFFIC_MS = 2000

// Poll cumulative byte counters and turn the deltas into kbit/s, newest last.
export function useTraffic(): Series {
  const [series, setSeries] = useState<Series>({})
  const prev = useRef<Traffic | null>(null)

  const refresh = useCallback(async () => {
    try {
      const cur = await getTraffic()
      const p = prev.current
      prev.current = cur
      if (!p || cur.ts <= p.ts) return
      const dt = cur.ts - p.ts
      setSeries((old) => {
        const next: Series = {}
        for (const [dev, c] of Object.entries(cur.dials)) {
          const b = p.dials[dev]
          // counter reset (dial re-created): skip the point rather than plot a spike
          if (!b || c.down_bytes < b.down_bytes || c.up_bytes < b.up_bytes) {
            next[dev] = old[dev] ?? []
            continue
          }
          const pt: [number, number] = [
            ((c.down_bytes - b.down_bytes) * 8) / 1000 / dt,
            ((c.up_bytes - b.up_bytes) * 8) / 1000 / dt,
          ]
          next[dev] = [...(old[dev] ?? []), pt].slice(-KEEP)
        }
        return next
      })
    } catch {
      // keep the last picture
    }
  }, [])

  usePoll(refresh, TRAFFIC_MS)
  return series
}
