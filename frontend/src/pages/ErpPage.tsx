import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { fmt } from '@/lib/format'
import type { AccountSummary, InventoryItem, Warehouse } from '@/lib/types'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { DataTable, SearchInput } from '@/components/DataTable'
import type { Column } from '@/components/DataTable'

export function ErpPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">库存财务</h1>
      <Tabs defaultValue="inventory">
        <TabsList>
          <TabsTrigger value="inventory">库存清单</TabsTrigger>
          <TabsTrigger value="account">财务账户</TabsTrigger>
          <TabsTrigger value="warehouses">仓库网络</TabsTrigger>
        </TabsList>
        <TabsContent value="inventory" className="mt-4">
          <InventoryTab />
        </TabsContent>
        <TabsContent value="account" className="mt-4">
          <AccountTab />
        </TabsContent>
        <TabsContent value="warehouses" className="mt-4">
          <WarehouseTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function InventoryTab() {
  const [search, setSearch] = useState('')
  const inv = useQuery({
    queryKey: ['inventory'],
    queryFn: () => api<InventoryItem[]>('/api/v1/erp/inventory'),
  })

  const columns: Column<InventoryItem>[] = [
    { key: 'sku', label: 'SKU', sortable: true },
    { key: 'name', label: '品名' },
    { key: 'stock_qty', label: '库存', sortable: true },
    { key: 'safety_stock', label: '安全线', sortable: true },
    {
      key: 'is_below_safety',
      label: '状态',
      render: (r) =>
        r.is_below_safety ? (
          <Badge variant="destructive">低于安全线</Badge>
        ) : (
          <Badge variant="outline" className="text-emerald-600 dark:text-emerald-400">
            正常
          </Badge>
        ),
      csv: (r) => (r.is_below_safety ? '低于安全线' : '正常'),
    },
  ]

  return (
    <div className="space-y-3">
      <SearchInput value={search} onChange={setSearch} placeholder="搜索 SKU / 品名…" />
      <DataTable
        columns={columns}
        data={inv.data}
        loading={inv.isPending}
        search={search}
        perPage={8}
        exportName="inventory.csv"
        emptyText="暂无库存数据"
      />
    </div>
  )
}

function AccountTab() {
  const acct = useQuery({
    queryKey: ['account'],
    queryFn: () => api<AccountSummary>('/api/v1/erp/account'),
  })

  const a = acct.data
  const rows = a
    ? [
        { label: '应付账款', value: fmt(a.payable), tone: 'text-destructive' },
        { label: '应收账款（净）', value: fmt(a.net_receivable), tone: 'text-emerald-600 dark:text-emerald-400' },
        { label: '退款', value: fmt(a.refund), tone: '' },
      ]
    : []

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {acct.isPending
        ? Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="p-6">
                <div className="h-16 animate-pulse rounded bg-muted" />
              </CardContent>
            </Card>
          ))
        : rows.map((r) => (
            <Card key={r.label}>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">{r.label}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className={`text-2xl font-semibold tabular-nums ${r.tone}`}>{r.value}</div>
              </CardContent>
            </Card>
          ))}
    </div>
  )
}

function WarehouseTab() {
  const ws = useQuery({
    queryKey: ['warehouses'],
    queryFn: () => api<Warehouse[]>('/api/v1/erp/warehouses'),
  })

  return (
    <div className="space-y-2">
      {ws.isPending ? (
        Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 animate-pulse rounded-md bg-muted" />)
      ) : (ws.data ?? []).length === 0 ? (
        <div className="py-8 text-center text-sm text-muted-foreground">暂无仓库</div>
      ) : (
        (ws.data ?? []).map((w) => (
          <div key={w.id} className="flex items-center gap-3 rounded-md border px-4 py-3 text-sm">
            <Badge variant="secondary">{w.code}</Badge>
            <span className="font-medium">{w.name}</span>
            <span className="text-muted-foreground">{w.location || '—'}</span>
          </div>
        ))
      )}
    </div>
  )
}
