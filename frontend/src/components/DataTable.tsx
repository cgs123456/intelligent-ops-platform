import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowDown, ArrowUp, Download } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { exportCsv } from '@/lib/format'

export interface Column<T> {
  key: string
  label: string
  sortable?: boolean
  render?: (row: T) => ReactNode
  csv?: (row: T) => string | number
}

interface DataTableProps<T> {
  columns: Column<T>[]
  data: T[] | undefined
  loading?: boolean
  search: string
  perPage?: number
  exportName?: string
  emptyText?: string
}

export function DataTable<T extends object>({
  columns,
  data,
  loading,
  search,
  perPage = 10,
  exportName,
  emptyText = '暂无数据',
}: DataTableProps<T>) {
  const [sortKey, setSortKey] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState(1)
  const [page, setPage] = useState(1)

  const rows = useMemo(() => {
    let r = data ?? []
    if (search) {
      const q = search.toLowerCase()
      r = r.filter((row) =>
        columns.some((c) => String((row as Record<string, unknown>)[c.key] ?? '').toLowerCase().includes(q)),
      )
    }
    if (sortKey) {
      const col = columns.find((c) => c.key === sortKey)
      r = [...r].sort((a, b) => {
        const av = col?.csv ? col.csv(a) : (a as Record<string, unknown>)[sortKey]
        const bv = col?.csv ? col.csv(b) : (b as Record<string, unknown>)[sortKey]
        if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sortDir
        return String(av ?? '').localeCompare(String(bv ?? '')) * sortDir
      })
    }
    return r
  }, [data, search, sortKey, sortDir, columns])

  const pages = Math.max(1, Math.ceil(rows.length / perPage))
  const cur = Math.min(page, pages)
  const pageRows = rows.slice((cur - 1) * perPage, cur * perPage)

  const toggleSort = (key: string) => {
    if (sortKey === key) setSortDir((d) => -d)
    else {
      setSortKey(key)
      setSortDir(1)
    }
  }

  const doExport = () => {
    if (!exportName) return
    exportCsv(
      columns.map((c) => c.label),
      rows.map((row) => columns.map((c) => (c.csv ? c.csv(row) : ((row as Record<string, unknown>)[c.key] ?? '')))),
      exportName,
    )
  }

  return (
    <div className="space-y-3">
      {exportName && (
        <div className="flex justify-end">
          <Button variant="outline" size="sm" onClick={doExport}>
            <Download data-slot="icon" />
            导出 CSV
          </Button>
        </div>
      )}
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="bg-muted/50 hover:bg-muted/50">
              {columns.map((c) => (
                <TableHead key={c.key}>
                  {c.sortable ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 font-medium hover:text-foreground"
                      onClick={() => toggleSort(c.key)}
                    >
                      {c.label}
                      {sortKey === c.key &&
                        (sortDir > 0 ? (
                          <ArrowUp className="size-3" />
                        ) : (
                          <ArrowDown className="size-3" />
                        ))}
                    </button>
                  ) : (
                    c.label
                  )}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading ? (
              Array.from({ length: Math.min(perPage, 5) }).map((_, i) => (
                <TableRow key={i}>
                  {columns.map((c) => (
                    <TableCell key={c.key}>
                      <Skeleton className="h-4 w-full max-w-24" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : pageRows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length} className="h-16 text-center text-muted-foreground">
                  {emptyText}
                </TableCell>
              </TableRow>
            ) : (
              pageRows.map((row, i) => (
                <TableRow key={i}>
                  {columns.map((c) => (
                    <TableCell key={c.key}>{c.render ? c.render(row) : String((row as Record<string, unknown>)[c.key] ?? '')}</TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>共 {rows.length} 条</span>
        {pages > 1 && (
          <div className="flex items-center gap-2">
            <span>
              第 {cur}/{pages} 页
            </span>
            <Button variant="outline" size="sm" disabled={cur === 1} onClick={() => setPage(cur - 1)}>
              上一页
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={cur === pages}
              onClick={() => setPage(cur + 1)}
            >
              下一页
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}

// 搜索框 + 表格组合（搜索状态由调用方持有）
export function SearchInput({
  value,
  onChange,
  placeholder = '搜索…',
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  return (
    <Input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="max-w-64"
    />
  )
}
