import { useEffect, useRef } from 'react'

// Run `fn` now, then again `ms` after each call SETTLES. Unlike setInterval,
// a slow endpoint (/api/sqm shells out to tc/ps) can't pile up overlapping
// requests. Paused while the tab is hidden, with an immediate refresh on return,
// so a forgotten LuCI tab stops hammering the router.
export function usePoll(fn: () => Promise<void>, ms: number) {
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    let timer: number | null = null
    let running = false
    let stopped = false

    const schedule = () => {
      if (!stopped && !document.hidden) timer = window.setTimeout(tick, ms)
    }
    const tick = async () => {
      timer = null
      if (running) return
      running = true
      try {
        await fnRef.current()
      } finally {
        running = false
        schedule()
      }
    }
    const onVisibility = () => {
      if (document.hidden) {
        if (timer !== null) window.clearTimeout(timer)
        timer = null
      } else if (timer === null && !running) {
        tick()
      }
    }

    tick()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stopped = true
      if (timer !== null) window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [ms])
}
