import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Swords, Zap } from 'lucide-react'
import { toast } from 'sonner'
import { api, sanitizeError } from '@/lib/api'
import { fmt } from '@/lib/format'
import type {
  InventoryItem,
  NegotiationResult,
  Quote,
  ScheduleStatus,
  SyncResultItem,
} from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { DataTable, SearchInput } from '@/components/DataTable'
import type { Column } from '@/components/DataTable'

export function RpaPage() {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [syncResult, setSyncResult] = useState<SyncResultItem[] | null>(null)
  const [negotiation, setNegotiation] = useState<NegotiationResult | null>(null)

  const quotes = useQuery({
    queryKey: ['quotes'],
    queryFn: () => api<Quote[]>('/api/v1/rpa/quotes'),
  })
  const schedule = useQuery({
    queryKey: ['schedule'],
    queryFn: () => api<ScheduleStatus>('/api/v1/rpa/schedule/status'),
  })

  const syncOrders = useMutation({
    mutationFn: () => api<{ results: SyncResultItem[] }>('/api/v1/rpa/sync-orders', { method: 'POST' }),
    onSuccess: (r) => {
      setSyncResult(r.results ?? [])
      qc.invalidateQueries({ queryKey: ['dashboard'] })
      const ok = (r.results ?? []).filter((x) => x.status === 'ok').length
      toast.success(`同步完成：${ok} 成功 / ${(r.results ?? []).length - ok} 失败`)
    },
    onError: (e) => toast.error('同步失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  const negotiate = useMutation({
    mutationFn: async (productId: number) =>
      api<NegotiationResult>('/api/v1/aigc/negotiate', {
        method: 'POST',
        body: JSON.stringify({ product_id: productId }),
      }),
    onSuccess: (r) => {
      setNegotiation(r)
      qc.invalidateQueries({ queryKey: ['quotes'] })
      toast.success(`博弈完成：${r.quotes?.length ?? 0} 个供应商报价`)
    },
    onError: (e) => toast.error('博弈失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  // 与原版一致：找第一个 库存 < 安全线×2 的产品发起三方竞价
  const startNegotiation = async () => {
    const inv = await api<InventoryItem[]>('/api/v1/erp/inventory')
    const low = (Array.isArray(inv) ? inv : []).find(
      (it) => it.stock_qty < (it.safety_stock || 0) * 2,
    )
    if (!low) {
      toast.info('暂无低库存产品，无需博弈采购')
      return
    }
    negotiate.mutate(low.id)
  }

  const columns: Column<Quote>[] = [
    { key: 'supplier', label: '供应商', sortable: true },
    { key: 'product', label: '产品' },
    {
      key: 'price',
      label: '报价',
      sortable: true,
      render: (r) => fmt(r.price),
      csv: (r) => r.price,
    },
    { key: 'source', label: '来源' },
    { key: 'date', label: '日期', sortable: true },
  ]

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">RPA 采购</h1>
        <div className="flex gap-2">
          <Button size="sm" onClick={startNegotiation} disabled={negotiate.isPending}>
            <Swords data-slot="icon" />
            {negotiate.isPending ? '博弈进行中…' : '多 Agent 博弈采购'}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => syncOrders.mutate()}
            disabled={syncOrders.isPending}
          >
            <Zap data-slot="icon" />
            {syncOrders.isPending ? '同步中…' : '同步电商订单'}
          </Button>
        </div>
      </div>

      {schedule.data && (
        <div className="rounded-md border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">
          调度：{schedule.data.schedule_enabled ? '已启用' : '未启用'} · 报价 cron:{' '}
          <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{schedule.data.quote_cron || '-'}</code> ·
          同步 cron:{' '}
          <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{schedule.data.sync_cron || '-'}</code>
        </div>
      )}

      {(negotiation || syncResult || negotiate.isPending || syncOrders.isPending) && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">执行结果</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            {negotiate.isPending && <div className="text-muted-foreground">多 Agent 博弈进行中，请稍候…</div>}
            {syncOrders.isPending && <div className="text-muted-foreground">订单同步中…</div>}
            {negotiation?.negotiation_log?.map((line, i) => (
              <div key={i} className="text-muted-foreground">
                · {line}
              </div>
            ))}
            {negotiation?.best && (
              <div className="rounded-md bg-emerald-500/10 px-3 py-2 text-emerald-700 dark:text-emerald-400">
                ✓ 最优：{negotiation.best.supplier_name} ¥{negotiation.best.price}/件 · 交期{' '}
                {negotiation.best.lead_days} 天 · 得分 {negotiation.best.score}
              </div>
            )}
            {syncResult && syncResult.length > 0 && (
              <>
                <Separator className="my-3" />
                {syncResult.map((it, i) => (
                  <div key={i} className="flex items-start gap-2">
                    <span className={it.status === 'ok' ? 'text-emerald-600 dark:text-emerald-400' : 'text-destructive'}>
                      {it.status === 'ok' ? '✓' : '✗'}
                    </span>
                    <span>
                      {it.platform || ''} · {it.status === 'ok' ? `${it.product ?? ''} → ${it.order_no ?? ''}` : it.reason || '失败'}
                    </span>
                  </div>
                ))}
              </>
            )}
            {!negotiate.isPending && !syncOrders.isPending && !negotiation && !syncResult && (
              <div className="text-muted-foreground">暂无执行记录</div>
            )}
          </CardContent>
        </Card>
      )}

      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <SearchInput value={search} onChange={setSearch} placeholder="搜索供应商 / 产品…" />
          <Button size="sm" variant="ghost" onClick={() => quotes.refetch()}>
            <RefreshCw data-slot="icon" />
            刷新
          </Button>
        </div>
        <DataTable
          columns={columns}
          data={quotes.data}
          loading={quotes.isPending}
          search={search}
          perPage={10}
          exportName="quotes.csv"
          emptyText="暂无报价，可执行博弈采购"
        />
      </div>
    </div>
  )
}
