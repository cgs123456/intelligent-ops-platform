import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Bot,
  Boxes,
  FileText,
  TrendingUp,
  Wallet,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, sanitizeError } from '@/lib/api'
import { fmt, fmtNum } from '@/lib/format'
import type { AdsSuggestion, Dashboard, PurchaseOrderView } from '@/lib/types'
import { useApp } from '@/stores/app'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from 'recharts'

export function DashboardPage() {
  const qc = useQueryClient()
  const setTab = useApp((s) => s.setTab)

  const dash = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api<Dashboard>('/api/dashboard'),
    refetchInterval: 30_000,
  })
  const orders = useQuery({
    queryKey: ['orders'],
    queryFn: () => api<PurchaseOrderView[]>('/api/v1/erp/orders'),
  })
  const ads = useQuery({
    queryKey: ['ads'],
    queryFn: () => api<{ suggestions: AdsSuggestion[] }>('/api/v1/fde/ads'),
  })
  const report = useQuery({
    queryKey: ['report'],
    queryFn: () => api<{ text: string }>('/api/v1/aigc/report'),
  })

  const genReport = useMutation({
    mutationFn: () => api<{ text: string }>('/api/v1/aigc/generate-report', { method: 'POST' }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['report'] })
      qc.invalidateQueries({ queryKey: ['dashboard'] })
      toast.success('日报已生成')
      void r
    },
    onError: (e) => toast.error('生成失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  // 近 7 日销量 Top 8（库存对比着色）
  const chartData = (ads.data?.suggestions ?? [])
    .slice()
    .sort((a, b) => b.recent_7d_sales - a.recent_7d_sales)
    .slice(0, 8)
    .map((s) => ({
      name: s.product_name.length > 6 ? s.product_name.slice(0, 6) + '…' : s.product_name,
      近7日销量: s.recent_7d_sales,
      当前库存: s.current_stock,
    }))

  const kpis = dash.data
    ? [
        { label: 'SKU 总数', value: fmtNum(dash.data.total_skus), icon: Boxes, onClick: () => setTab('erp') },
        { label: '库存价值', value: fmt(dash.data.inventory_value), icon: Wallet, onClick: () => setTab('erp') },
        { label: '近 7 日销售额', value: fmt(dash.data.sales_7d), icon: TrendingUp, onClick: () => setTab('fde') },
        {
          label: '低库存 SKU',
          value: fmtNum(dash.data.low_stock_count),
          icon: AlertTriangle,
          danger: dash.data.low_stock_count > 0,
          onClick: () => setTab('erp'),
        },
        {
          label: '待审核建议',
          value: fmtNum(dash.data.pending_suggestions),
          icon: Bot,
          danger: dash.data.pending_suggestions > 0,
          onClick: () => setTab('aigc'),
        },
      ]
    : []

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">运营概览</h1>
        <Button size="sm" onClick={() => genReport.mutate()} disabled={genReport.isPending}>
          <FileText data-slot="icon" />
          {genReport.isPending ? '生成中…' : '生成运营日报'}
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-5">
        {dash.isPending
          ? Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)
          : kpis.map((k) => {
              const Icon = k.icon
              return (
                <Card
                  key={k.label}
                  className="cursor-pointer transition-shadow hover:shadow-md"
                  onClick={k.onClick}
                >
                  <CardHeader className="pb-2">
                    <CardTitle className="flex items-center justify-between text-sm font-medium text-muted-foreground">
                      {k.label}
                      <Icon className={`size-4 ${k.danger ? 'text-destructive' : 'text-muted-foreground/60'}`} />
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div
                      className={`text-2xl font-semibold tabular-nums ${k.danger ? 'text-destructive' : ''}`}
                    >
                      {k.value}
                    </div>
                  </CardContent>
                </Card>
              )
            })}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">近 7 日销量 Top 8</CardTitle>
          </CardHeader>
          <CardContent>
            {ads.isPending ? (
              <Skeleton className="h-64 w-full" />
            ) : chartData.length === 0 ? (
              <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
                暂无数据，可执行 ETL 刷新数仓
              </div>
            ) : (
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                    <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={-20} dy={8} height={40} />
                    <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                    <ReTooltip
                      contentStyle={{
                        borderRadius: 8,
                        border: '1px solid var(--border)',
                        background: 'var(--popover)',
                        fontSize: 12,
                      }}
                    />
                    <Bar dataKey="近7日销量" fill="var(--chart-1)" radius={[4, 4, 0, 0]} maxBarSize={36} />
                    <Bar dataKey="当前库存" fill="var(--chart-2)" radius={[4, 4, 0, 0]} maxBarSize={36} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">近期采购单</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {orders.isPending ? (
              Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)
            ) : (orders.data ?? []).length === 0 ? (
              <div className="py-8 text-center text-sm text-muted-foreground">暂无单据</div>
            ) : (
              (orders.data ?? []).slice(0, 8).map((o) => (
                <div
                  key={o.id}
                  className="flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <Badge variant="secondary">采购</Badge>
                    <span className="truncate">
                      {o.product} ×{o.qty}
                    </span>
                  </div>
                  <span className="shrink-0 text-muted-foreground">
                    {o.supplier_name} · {fmt(o.amount)} · {o.created_at || ''}
                  </span>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">AIGC 运营日报</CardTitle>
        </CardHeader>
        <CardContent>
          {report.isPending ? (
            <Skeleton className="h-24 w-full" />
          ) : (
            <pre className="whitespace-pre-wrap rounded-md bg-muted/50 p-4 font-mono text-xs leading-relaxed">
              {report.data?.text || '暂无日报，点击右上角「生成运营日报」'}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
