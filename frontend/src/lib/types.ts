// 后端 API 响应类型（与 Flask 路由实际返回字段对齐，经契约审查锚定）

export interface Me {
  id: number
  username: string
  roles: string[]
  must_change_password?: boolean
}

export interface Dashboard {
  total_skus: number
  inventory_value: number
  sales_7d: number
  low_stock_count: number
  pending_suggestions: number
}

export interface PurchaseOrderView {
  id: number
  order_no: string
  product: string
  supplier_name: string
  qty: number
  amount: number
  created_at: string
  status: string
}

export interface InventoryItem {
  id: number
  sku: string
  name: string
  stock_qty: number
  safety_stock: number
  is_below_safety: boolean
}

export interface AccountSummary {
  payable: number
  net_receivable: number
  refund: number
}

export interface Warehouse {
  id: number
  code: string
  name: string
  location: string
}

export interface Quote {
  supplier: string
  product: string
  price: number
  source: string
  date: string
}

export interface SyncResultItem {
  status: 'ok' | 'fail'
  ext_order_id: string
  platform?: string
  product?: string
  order_no?: string
  reason?: string
}

export interface ScheduleStatus {
  schedule_enabled: boolean
  quote_cron: string | null
  sync_cron: string | null
}

export interface LayerStats {
  [table: string]: number
}

export interface FdeStats {
  ODS: LayerStats
  DWD: LayerStats
  DWS: LayerStats
  ADS: LayerStats
}

export interface EtlResult {
  ods: number
  dwd: number
  dws: number
  ads: number
}

export interface AdsSuggestion {
  id: number
  product_id: number
  product_name: string
  recent_7d_sales: number
  current_stock: number
  in_transit: number
  suggested_qty: number
  suggested_supplier_name: string | null
}

export interface LineageItem {
  upstream: string
  downstream: string
  layer: string
}

export interface DqTest {
  status: 'pass' | 'fail'
  failures: number | null
  column?: string
  checked_at?: string
}

export type DqReport = Record<string, Record<string, DqTest>>

export interface Suggestion {
  id: number
  product_name: string
  suggested_supplier_name: string | null
  suggested_qty: number
  original_qty: number
  unit_price: number
  amount: number
  reason: string
  status: string
  confidence: number
  created_at: string
}

export interface ChatMessage {
  role: 'user' | 'bot'
  content: string
  time?: string
}

export interface LoopStep {
  step: number
  name: string
  owner: string
  status: 'done' | 'running' | 'pending' | 'failed' | 'rolled_back'
  detail: string
}

export interface LoopStatus {
  run_id: number | null
  current_step: number
  status: string
  steps: LoopStep[]
  done?: boolean
}

export interface NegotiationQuote {
  supplier_name: string
  price: number
  lead_days: number
  rating: number
  score: number
  reason: string
}

export interface NegotiationResult {
  product_id: number
  product_name: string
  quotes: NegotiationQuote[]
  best: NegotiationQuote | null
  negotiation_log: string[]
}
