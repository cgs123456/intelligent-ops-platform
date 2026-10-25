import { useEffect, useRef } from 'react'

// 闭环 SSE 订阅：EventSource 自动重连（CONNECTING 由浏览器处理），
// 永久失败（CLOSED）时指数退避手动重启，上限 5 次（对齐原实现 H3 修复）。
export function useLoopStream(onMessage: () => void, enabled: boolean): void {
  const cbRef = useRef(onMessage)
  cbRef.current = onMessage

  useEffect(() => {
    if (!enabled) return
    let es: EventSource | null = null
    let retries = 0
    let timer: ReturnType<typeof setTimeout> | null = null
    let stopped = false

    const start = () => {
      es = new EventSource('/api/v1/loop/stream', { withCredentials: true })
      es.onopen = () => {
        retries = 0
      }
      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data)
          if (data.event === 'end') return
        } catch {
          return
        }
        cbRef.current()
      }
      es.onerror = () => {
        if (es && es.readyState === EventSource.CLOSED) {
          retries = Math.min(retries + 1, 5)
          timer = setTimeout(start, 1000 * 2 ** retries)
        }
      }
    }
    start()
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
      es?.close()
      void stopped
    }
  }, [enabled])
}
