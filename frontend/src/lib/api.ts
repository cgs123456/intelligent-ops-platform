import { useApp } from '@/stores/app'

// 错误脱敏：不向前端暴露 Python 堆栈（与原版 sanitizeError 一致）
export function sanitizeError(msg: unknown): string {
  if (!msg) return '未知错误'
  const s = String(msg)
  if (/Traceback|File "[^"]+", line \d+|raise \w+Error|sqlalchemy|werkzeug/i.test(s)) {
    return '服务异常，请联系管理员（详见服务端日志）'
  }
  if (s.length > 200) return s.slice(0, 200) + '...'
  return s
}

let refreshPromise: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({}),
    })
      .then((r) => r.ok)
      .finally(() => {
        refreshPromise = null
      })
  }
  return refreshPromise
}

export class ApiError extends Error {}

export async function api<T = unknown>(path: string, opts: RequestInit = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      headers: { 'Content-Type': 'application/json', ...opts.headers },
      credentials: 'include',
      ...opts,
    })
  } catch (e) {
    throw new ApiError(sanitizeError(e instanceof Error ? e.message : e))
  }
  if (res.status === 401) {
    if (await tryRefresh()) return api<T>(path, opts)
    useApp.getState().setAuthed(false)
    throw new ApiError('请重新登录')
  }
  let data: unknown
  try {
    data = await res.json()
  } catch {
    data = null
  }
  const obj = data as Record<string, unknown> | null
  if (!res.ok || obj?.error) {
    throw new ApiError(sanitizeError(obj?.error ?? obj?.message ?? res.statusText))
  }
  return data as T
}

export interface StreamHandlers {
  onSession?: (sessionId: string) => void
  onChunk?: (text: string) => void
  onDone?: () => void
  onError?: (message: string) => void
}

// Data Agent 问答流（POST + ReadableStream，SSE 行协议 data: {...}\n）
export async function streamQuery(
  question: string,
  sessionId: string | null,
  handlers: StreamHandlers,
): Promise<void> {
  let res: Response
  try {
    res = await fetch('/api/v1/aigc/query-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(sessionId ? { question, session_id: sessionId } : { question }),
    })
  } catch {
    handlers.onError?.('网络异常，请重试')
    return
  }
  if (res.status === 401) {
    if (await tryRefresh()) return streamQuery(question, sessionId, handlers)
    useApp.getState().setAuthed(false)
    handlers.onError?.('请重新登录')
    return
  }
  if (!res.ok || !res.body) {
    handlers.onError?.(`请求失败（${res.status}）`)
    return
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed.startsWith('data:')) continue
      try {
        const evt = JSON.parse(trimmed.slice(5).trim())
        if (evt.type === 'session') handlers.onSession?.(evt.session_id)
        else if (evt.type === 'chunk') handlers.onChunk?.(evt.text ?? '')
        else if (evt.type === 'done') handlers.onDone?.()
        else if (evt.type === 'error') handlers.onError?.(sanitizeError(evt.error))
      } catch {
        // 心跳注释行等非 JSON 内容忽略
      }
    }
  }
  handlers.onDone?.()
}
