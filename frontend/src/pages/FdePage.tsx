import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { PlayCircle } from 'lucide-react'
import { toast } from 'sonner'
import { api, sanitizeError } from '@/lib/api'
import type {
  AdsSuggestion,
  DqReport,
  EtlResult,
  FdeStats,
  LineageItem,
} from '@/lib/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { DataTable, SearchInput } from '@/components/DataTable'
import type { Column } from '@/components/DataTable'

const LAYERS: { key: keyof FdeStats; name: string }[] = [
  { key: 'ODS', name: 'ODS 贴源层' },
  { key: 'DWD', name: 'DWD 明细层' },
  { key: 'DWS', name: 'DWS 汇总层' },
  { key: 'ADS', name: 'ADS 应用层' },
]

export function FdePage() {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')

  const stats = useQuery({ queryKey: ['fde-stats'], queryFn: () => api<FdeStats>('/api/v1/fde/stats') })
  const ads = useQuery({
    queryKey: ['ads'],
    queryFn: () => api<{ suggestions: AdsSuggestion[] }>('/api/v1/fde/ads'),
  })
  const lineage = useQuery({
    queryKey: ['lineage'],
    queryFn: () => api<LineageItem[]>('/api/v1/fde/lineage'),
  })
  const dq = useQuery({
    queryKey: ['dq'],
    queryFn: () => api<DqReport>('/api/v1/fde/data-quality'),
    enabled: false,
  })

  const runEtl = useMutation({
    mutationFn: () => api<EtlResult>('/api/v1/fde/run', { method: 'POST' }),
    onSuccess: (r) => {
      toast.success(`ETL 完成：ODS +${r.ods} / DWD +${r.dwd} / DWS +${r.dws} / ADS +${r.ads}`)
      qc.invalidateQueries({ queryKey: ['fde-stats'] })
      qc.invalidateQueries({ queryKey: ['ads'] })
      qc.invalidateQueries({ queryKey: ['dashboard'] })
    },
    onError: (e) => toast.error('ETL 失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  const adsColumns: Column<AdsSuggestion>[] = [
    { key: 'product_name', label: '产品', sortable: true },
    { key: 'recent_7d_sales', label: '近 7 日销量', sortable: true },
    { key: 'current_stock', label: '当前库存', sortable: true },
    { key: 'in_transit', label: '在途', sortable: true },
    {
      key: 'suggested_qty',
      label: '建议采购',
      sortable: true,
      render: (r) =>
        r.suggested_qty > 0 ? (
          <span className="font-semibold text-destructive">{r.suggested_qty}</span>
        ) : (
          <span className="text-muted-foreground">无需补货</span>
        ),
    },
    { key: 'suggested_supplier_name', label: '建议供应商' },
  ]

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">数仓 FDE</h1>
        <Button size="sm" onClick={() => runEtl.mutate()} disabled={runEtl.isPending}>
          <PlayCircle data-slot="icon" />
          {runEtl.isPending ? 'ETL 执行中…' : '执行 ETL 刷新'}
        </Button>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {stats.isPending
          ? Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-40 rounded-xl" />)
          : LAYERS.map((l) => {
              const entries = Object.entries(stats.data?.[l.key] ?? {})
              return (
                <Card key={l.key}>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-medium">{l.name}</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-1.5">
                    {entries.length === 0 ? (
                      <div className="text-sm text-muted-foreground">暂无数据</div>
                    ) : (
                      entries.map(([k, v]) => (
                        <div key={k} className="flex items-center justify-between text-sm">
                          <span className="text-muted-foreground">{k}</span>
                          <span className="font-medium tabular-nums">{v}</span>
                        </div>
                      ))
                    )}
                  </CardContent>
                </Card>
              )
            })}
      </div>

      <Tabs defaultValue="ads">
        <TabsList>
          <TabsTrigger value="ads">补货建议</TabsTrigger>
          <TabsTrigger value="lineage">数据血缘</TabsTrigger>
          <TabsTrigger value="dq">数据质量</TabsTrigger>
        </TabsList>
        <TabsContent value="ads" className="mt-4">
          <div className="space-y-3">
            <SearchInput value={search} onChange={setSearch} placeholder="搜索产品…" />
            <DataTable
              columns={adsColumns}
              data={ads.data?.suggestions}
              loading={ads.isPending}
              search={search}
              perPage={8}
              exportName="ads_suggestions.csv"
              emptyText="暂无建议，执行 ETL 后生成"
            />
          </div>
        </TabsContent>
        <TabsContent value="lineage" className="mt-4">
          <Card>
            <CardContent className="space-y-2 pt-6">
              {lineage.isPending ? (
                <Skeleton className="h-24 w-full" />
              ) : (lineage.data ?? []).length === 0 ? (
                <div className="py-8 text-center text-sm text-muted-foreground">暂无血缘</div>
              ) : (
                (lineage.data ?? []).map((l, i) => (
                  <div key={i} className="flex items-center gap-2 rounded-md border px-3 py-2 font-mono text-xs">
                    <span>{l.upstream}</span>
                    <span className="text-muted-foreground">→</span>
                    <span>{l.downstream}</span>
                    <Badge variant="secondary" className="ml-auto">
                      {l.layer}
                    </Badge>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="dq" className="mt-4">
          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">数据质量测试</CardTitle>
                <Button size="sm" variant="outline" onClick={() => dq.refetch()} disabled={dq.isFetching}>
                  {dq.isFetching ? '测试中…' : '运行质量测试'}
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              {dq.isFetching && <Skeleton className="h-16 w-full" />}
              {!dq.isFetching && dq.data &&
                Object.entries(dq.data).map(([table, tests]) => (
                  <div key={table} className="space-y-1.5">
                    <div className="font-mono text-sm font-medium">{table}</div>
                    <div className="flex flex-wrap gap-1.5">
                      {Object.entries(tests).map(([t, s]) => (
                        <Badge
                          key={t}
                          variant={s.status === 'pass' ? 'outline' : 'destructive'}
                          title={`${t} 失败数: ${s.failures ?? '-'}${s.column ? ' 列: ' + s.column : ''}`}
                        >
                          {t}: {s.status}
                        </Badge>
                      ))}
                    </div>
                  </div>
                ))}
              {!dq.isFetching && !dq.data && (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  运行数据质量测试查看结果
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
