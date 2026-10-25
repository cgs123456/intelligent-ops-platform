import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, CheckCheck, Loader2, Send, User } from 'lucide-react'
import { toast } from 'sonner'
import { api, sanitizeError, streamQuery } from '@/lib/api'
import type { ChatMessage, Suggestion } from '@/lib/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { fmt } from '@/lib/format'

export function AigcPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">AIGC 助手</h1>
      <Tabs defaultValue="suggestions">
        <TabsList>
          <TabsTrigger value="suggestions">补货建议审核</TabsTrigger>
          <TabsTrigger value="chat">智能问答</TabsTrigger>
        </TabsList>
        <TabsContent value="suggestions" className="mt-4">
          <SuggestionsTab />
        </TabsContent>
        <TabsContent value="chat" className="mt-4">
          <ChatTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function SuggestionsTab() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [editing, setEditing] = useState<Suggestion | null>(null)
  const [editQtyVal, setEditQtyVal] = useState('')
  const [editNote, setEditNote] = useState('')

  const suggestions = useQuery({
    queryKey: ['suggestions'],
    queryFn: () => api<Suggestion[]>('/api/v1/aigc/suggestions'),
  })

  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: ['suggestions'] })
    qc.invalidateQueries({ queryKey: ['loop-status'] })
    qc.invalidateQueries({ queryKey: ['dashboard'] })
  }

  const review = useMutation({
    mutationFn: (vars: { id: number; action: 'approve' | 'reject'; final_qty?: number; note?: string }) =>
      api('/api/v1/aigc/review', {
        method: 'POST',
        body: JSON.stringify(vars.final_qty != null ? vars : { id: vars.id, action: vars.action, note: vars.note }),
      }),
    onSuccess: (_r, vars) => {
      toast.success(`建议 ${vars.id} 已${vars.action === 'approve' ? '批准' : '拒绝'}`, { duration: 2000 })
      invalidateAll()
    },
    onError: (e) => toast.error('审核失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  const batch = useMutation({
    mutationFn: (action: 'approve' | 'reject') =>
      api('/api/v1/aigc/batch-review', {
        method: 'POST',
        body: JSON.stringify({ ids: Array.from(selected), action }),
      }),
    onSuccess: (_r, action) => {
      toast.success(`已批量${action === 'approve' ? '批准' : '拒绝'} ${selected.size} 条`)
      setSelected(new Set())
      invalidateAll()
    },
    onError: (e) => toast.error('批量审核失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  const toggle = (id: number, checked: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const submitEdit = () => {
    if (!editing) return
    const qty = parseInt(editQtyVal, 10)
    if (isNaN(qty) || qty < 1) {
      toast.warning('数量必须为正整数')
      return
    }
    review.mutate({ id: editing.id, action: 'approve', final_qty: qty, note: editNote })
    setEditing(null)
  }

  const list = suggestions.data ?? []

  return (
    <div className="space-y-4">
      {selected.size > 0 && (
        <div className="flex items-center gap-3 rounded-lg border bg-muted/40 px-4 py-2.5">
          <span className="text-sm">
            已选 <strong>{selected.size}</strong> 条
          </span>
          <div className="ml-auto flex gap-2">
            <Button size="sm" onClick={() => batch.mutate('approve')} disabled={batch.isPending}>
              <CheckCheck data-slot="icon" />
              批量批准
            </Button>
            <Button size="sm" variant="destructive" onClick={() => batch.mutate('reject')} disabled={batch.isPending}>
              批量拒绝
            </Button>
          </div>
        </div>
      )}

      {suggestions.isPending ? (
        Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-32 rounded-xl" />)
      ) : list.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            暂无待审核建议。可执行闭环步骤 1 生成建议。
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-3 xl:grid-cols-2">
          {list.map((s) => (
            <Card key={s.id}>
              <CardHeader className="pb-2">
                <div className="flex items-center gap-3">
                  <input
                    type="checkbox"
                    className="size-4 accent-primary"
                    checked={selected.has(s.id)}
                    onChange={(e) => toggle(s.id, e.target.checked)}
                  />
                  <CardTitle className="text-base">{s.product_name}</CardTitle>
                  <span className="ml-auto font-semibold tabular-nums">{fmt(s.amount)}</span>
                  <Badge variant="secondary">置信度 {(s.confidence * 100).toFixed(0)}%</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="text-sm text-muted-foreground">
                  供应商：{s.suggested_supplier_name || '—'} · 数量：{s.suggested_qty} 件 · 单价：
                  {fmt(s.unit_price)}
                </div>
                <div className="rounded-md bg-muted/50 px-3 py-2 text-sm">{s.reason}</div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={() => review.mutate({ id: s.id, action: 'approve' })}
                    disabled={review.isPending}
                  >
                    批准
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setEditing(s)
                      setEditQtyVal(String(s.suggested_qty))
                      setEditNote('')
                    }}
                  >
                    修改数量
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => review.mutate({ id: s.id, action: 'reject' })}
                    disabled={review.isPending}
                  >
                    拒绝
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <Dialog open={editing != null} onOpenChange={(v) => !v && setEditing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>修改最终采购数量</DialogTitle>
            <DialogDescription>
              {editing?.product_name}（原建议 {editing?.suggested_qty} 件）
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="edit-qty">最终数量（正整数）</Label>
              <Input id="edit-qty" type="number" min={1} value={editQtyVal} onChange={(e) => setEditQtyVal(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-note">审核备注（可选）</Label>
              <Textarea id="edit-note" rows={2} value={editNote} onChange={(e) => setEditNote(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditing(null)}>
              取消
            </Button>
            <Button onClick={submitEdit}>确认批准</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

const QUICK_QUESTIONS = [
  '近7天哪些SKU销量最高？',
  '当前低库存商品有哪些？',
  '本月采购金额是多少？',
]

function ChatTab() {
  const qc = useQueryClient()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(
    () => localStorage.getItem('chat_session'),
  )
  const bottomRef = useRef<HTMLDivElement>(null)

  const history = useQuery({
    queryKey: ['chat-history', sessionId],
    queryFn: () => api<ChatMessage[]>(`/api/v1/aigc/chat-history/${sessionId}`),
    enabled: !!sessionId && messages.length === 0,
  })

  useEffect(() => {
    if (history.data && history.data.length > 0 && messages.length === 0) {
      setMessages(history.data)
    }
  }, [history.data, messages.length])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const send = async (question?: string) => {
    const q = (question ?? input).trim()
    if (!q || streaming) return
    setInput('')
    setStreaming(true)
    setMessages((m) => [...m, { role: 'user', content: q }, { role: 'bot', content: '思考中…' }])
    const botIdx = -1
    let firstChunk = true
    await streamQuery(q, sessionId, {
      onSession: (sid) => {
        setSessionId((prev) => {
          if (prev !== sid) localStorage.setItem('chat_session', sid)
          return sid
        })
      },
      onChunk: (text) => {
        if (firstChunk) {
          firstChunk = false
          setMessages((m) => {
            const copy = [...m]
            copy[copy.length + botIdx] = { role: 'bot', content: text }
            return copy
          })
        } else {
          setMessages((m) => {
            const copy = [...m]
            const cur = copy[copy.length + botIdx]
            copy[copy.length + botIdx] = { role: 'bot', content: cur.content + text }
            return copy
          })
        }
      },
      onError: (msg) => {
        setMessages((m) => {
          const copy = [...m]
          copy[copy.length + botIdx] = { role: 'bot', content: '错误：' + msg }
          return copy
        })
      },
    })
    setStreaming(false)
    qc.invalidateQueries({ queryKey: ['dashboard'] })
  }

  return (
    <Card className="flex h-[calc(100vh-16rem)] min-h-96 flex-col">
      <CardHeader className="shrink-0 pb-3">
        <CardTitle className="text-base">Data Agent 智能问答</CardTitle>
        <div className="flex flex-wrap gap-1.5 pt-1">
          {QUICK_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              disabled={streaming}
              onClick={() => send(q)}
              className="rounded-full border px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
            >
              {q}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col">
        <ScrollArea className="min-h-0 flex-1 pr-3">
          <div className="space-y-4 pb-2">
            {messages.length === 0 && (
              <div className="py-10 text-center text-sm text-muted-foreground">
                向 Data Agent 提问库存、销售、采购数据问题
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`flex gap-2.5 ${m.role === 'user' ? 'justify-end' : ''}`}>
                {m.role === 'bot' && (
                  <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
                    <Bot className="size-4" />
                  </div>
                )}
                <div
                  className={`max-w-[80%] rounded-xl px-3.5 py-2.5 text-sm leading-relaxed ${
                    m.role === 'user'
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-muted'
                  }`}
                >
                  <pre className="whitespace-pre-wrap font-sans">{m.content}</pre>
                </div>
                {m.role === 'user' && (
                  <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-muted">
                    <User className="size-4" />
                  </div>
                )}
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        </ScrollArea>
        <form
          className="mt-3 flex shrink-0 gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            void send()
          }}
        >
          <Input
            id="chatInput"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="输入问题，Enter 发送…"
            disabled={streaming}
          />
          <Button type="submit" size="icon" disabled={streaming || !input.trim()}>
            {streaming ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
