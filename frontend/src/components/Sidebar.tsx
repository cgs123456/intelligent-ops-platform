import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  Boxes,
  Cpu,
  Database,
  LayoutDashboard,
  LogOut,
  Moon,
  RefreshCw,
  Repeat,
  Sun,
} from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/lib/api'
import { TAB_ORDER, useApp } from '@/stores/app'
import type { TabId } from '@/stores/app'
import { Button } from '@/components/ui/button'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Separator } from '@/components/ui/separator'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'

const TAB_META: Record<TabId, { label: string; icon: typeof LayoutDashboard }> = {
  dashboard: { label: '运营概览', icon: LayoutDashboard },
  loop: { label: '智能闭环', icon: Repeat },
  erp: { label: '库存财务', icon: Boxes },
  rpa: { label: 'RPA 采购', icon: Cpu },
  fde: { label: '数仓 FDE', icon: Database },
  aigc: { label: 'AIGC 助手', icon: Bot },
}

export function Sidebar() {
  const { tab, setTab, me, setAuthed, setMe } = useApp()
  const [dark, setDark] = useState(() => document.documentElement.classList.contains('dark'))
  const qc = useQueryClient()

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])

  const logout = useMutation({
    mutationFn: () =>
      api('/api/v1/auth/logout', { method: 'POST', body: JSON.stringify({}) }),
    onSettled: () => {
      qc.clear()
      setMe(null)
      setAuthed(false)
    },
  })

  return (
    <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col border-r bg-card">
      <div className="flex items-center gap-2 px-5 py-5">
        <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <RefreshCw className="size-4" />
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight">智能运营平台</div>
          <div className="text-[11px] text-muted-foreground">AIGC · RPA · ERP · FDE</div>
        </div>
      </div>
      <Separator />
      <nav className="flex-1 space-y-1 p-3">
        {TAB_ORDER.map((id, i) => {
          const meta = TAB_META[id]
          const Icon = meta.icon
          const active = tab === id
          return (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={`flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors ${
                active
                  ? 'bg-primary text-primary-foreground font-medium'
                  : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
              }`}
            >
              <Icon className="size-4" />
              {meta.label}
              <span
                className={`ml-auto text-[10px] ${active ? 'text-primary-foreground/60' : 'text-muted-foreground/50'}`}
              >
                ⌘{i + 1}
              </span>
            </button>
          )
        })}
      </nav>
      <Separator />
      <div className="flex items-center gap-2 p-3">
        <Avatar className="size-8">
          <AvatarFallback className="text-xs">
            {(me?.username ?? '?').slice(0, 2).toUpperCase()}
          </AvatarFallback>
        </Avatar>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{me?.username ?? '—'}</div>
          <div className="truncate text-[11px] text-muted-foreground">
            {me?.roles?.join(', ') || '用户'}
          </div>
        </div>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="size-8" onClick={() => setDark(!dark)}>
              {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </Button>
          </TooltipTrigger>
          <TooltipContent>切换主题</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 text-destructive hover:text-destructive"
              onClick={() => logout.mutate()}
            >
              <LogOut className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>退出登录</TooltipContent>
        </Tooltip>
      </div>
      <div className="px-3 pb-3">
        {logout.isError && (
          <div className="text-[11px] text-destructive">退出失败，请重试</div>
        )}
        {me?.must_change_password && (
          <div
            className="rounded-md bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-600 dark:text-amber-400"
            onClick={() => toast.info('请联系管理员或使用修改密码功能')}
          >
            提示：初始密码未修改，建议尽快更换
          </div>
        )}
      </div>
    </aside>
  )
}
