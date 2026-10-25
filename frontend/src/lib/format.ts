export function fmt(n: number | null | undefined): string {
  return '¥' + Number(n ?? 0).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function fmtNum(n: number | null | undefined): string {
  return Number(n ?? 0).toLocaleString('zh-CN')
}

// CSV 导出：公式注入防护（OWASP：=+-@ 前缀转义）+ BOM 头
function csvCell(v: unknown): string {
  let t = String(v ?? '')
    .replace(/[\n\r]/g, ' ')
    .trim()
  if (/^[=+\-@]/.test(t)) t = "'" + t
  return `"${t.replace(/"/g, '""')}"`
}

export function exportCsv(
  headers: string[],
  rows: unknown[][],
  filename: string,
): void {
  const csv = [
    headers.map(csvCell).join(','),
    ...rows.map((r) => r.map(csvCell).join(',')),
  ].join('\n')
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
