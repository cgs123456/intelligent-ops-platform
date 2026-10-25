import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { Me } from '@/lib/types'
import { TAB_ORDER, useApp } from '@/stores/app'
import { Sidebar } from '@/components/Sidebar'
import { LoginPage } from '@/pages/LoginPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { LoopPage } from '@/pages/LoopPage'
import { ErpPage } from '@/pages/ErpPage'
import { RpaPage } from '@/pages/RpaPage'
import { FdePage } from '@/pages/FdePage'
import { AigcPage } from '@/pages/AigcPage'

export default function App() {
  const { authed, tab, setTab, setMe, setAuthed } = useApp()

  const meQuery = useQuery({
    queryKey: ['me'],
    queryFn: () => api<Me>('/api/v1/auth/me'),
    retry: false,
  })

  useEffect(() => {
    if (meQuery.isSuccess) setMe(meQuery.data)
    else if (meQuery.isError) setAuthed(false)
  }, [meQuery.isSuccess, meQuery.isError, meQuery.data, setMe, setAuthed])

  // Ctrl/Cmd + 1~6 切换 Tab
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key >= '1' && e.key <= '6') {
        e.preventDefault()
        setTab(TAB_ORDER[parseInt(e.key, 10) - 1])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setTab])

  if (meQuery.isPending) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <div className="text-muted-foreground">加载中…</div>
      </div>
    )
  }

  if (!authed) return <LoginPage />

  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar />
      <main className="flex-1 overflow-x-hidden p-6 lg:p-8">
        {tab === 'dashboard' && <DashboardPage />}
        {tab === 'loop' && <LoopPage />}
        {tab === 'erp' && <ErpPage />}
        {tab === 'rpa' && <RpaPage />}
        {tab === 'fde' && <FdePage />}
        {tab === 'aigc' && <AigcPage />}
      </main>
    </div>
  )
}
