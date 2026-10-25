import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Play, RotateCcw, Undo2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, sanitizeError } from '@/lib/api'
import type { LoopStatus } from '@/lib/types'
import { useApp } from '@/stores/app'
import { useLoopStream } from '@/hooks/useLoopStream'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Skeleton } from '@/components/ui/skeleton'

const STATUS_META: Record<LoopStatus['steps'][number]['status'], { text: string; badge: 'default' | 'secondary' | 'destructive' | 'outline' }> = {
  done: { text: '✓ 完成', badge: 'default' },
  running: { text: '执行中', badge: 'secondary' },
  pending: { text: '待执行', badge: 'outline' },
  failed: { text: '✗ 失败', badge: 'destructive' },
  rolled_back: { text: '↩ 已回滚', badge: 'outline' },
}

export function LoopPage() {
  const qc = useQueryClient()
  const setTab = useApp((s) => s.setTab)
  const [confirmReset, setConfirmReset] = useState(false)

  const status = useQuery({
    queryKey: ['loop-status'],
    queryFn: () => api<LoopStatus>('/api/v1/loop/status'),
    refetchInterval: 15_000,
  })

  useLoopStream(
    () => {
      qc.invalidateQueries({ queryKey: ['loop-status'] })
    },
    useApp((s) => s.authed),
  )

  const runStep = useMutation({
    mutationFn: async (step: number) =>
      api<{ need_manual?: boolean; message?: string; error?: string }>('/api/v1/loop/run-step', {
        method: 'POST',
        body: JSON.stringify({ step }),
      }),
    onSuccess: (r) => {
      if (r.need_manual) {
        toast.warning(r.message || '步骤 2 需人工审核', {
          action: { label: '去审核', onClick: () => setTab('aigc') },
        })
      } else if (r.error) {
        toast.error(sanitizeError(r.error))
      } else {
        toast.success('步骤执行完成')
      }
      qc.invalidateQueries({ queryKey: ['loop-status'] })
      qc.invalidateQueries({ queryKey: ['dashboard'] })
    },
    onError: (e) => toast.error('执行失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  const rollback = useMutation({
    mutationFn: (step: number) =>
      api<{ status?: string }>('/api/v1/loop/rollback', {
        method: 'POST',
        body: JSON.stringify({ step }),
      }),
    onSuccess: (r) => {
      toast.success(r.status === 'rolled_back' ? '已回滚' : '回滚请求已提交')
      qc.invalidateQueries({ queryKey: ['loop-status'] })
      qc.invalidateQueries({ queryKey: ['dashboard'] })
    },
    onError: (e) => toast.error('回滚失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  const reset = useMutation({
    mutationFn: () => api('/api/v1/loop/reset', { method: 'POST' }),
    onSuccess: () => {
      toast.success('闭环已重置')
      qc.invalidateQueries({ queryKey: ['loop-status'] })
    },
    onError: (e) => toast.error('重置失败：' + sanitizeError(e instanceof Error ? e.message : e)),
  })

  const s = status.data
  const steps = s?.steps ?? []
  const isRunning = steps.some((x) => x.status === 'running')
  const allDone = steps.length > 0 && steps.every((x) => x.status === 'done')
  const current = s?.current_step ?? 0
  const waitingReview = current === 2 && !allDone

  const nextStep = Math.min(current, 5) || 1
  const nextStepName = steps.find((x) => x.step === nextStep)?.name ?? ''

  let btnLabel = `执行步骤 ${nextStep}：${nextStepName}`
  let btnDisabled = runStep.isPending
  if (isRunning) {
    btnLabel = '执行中…'
    btnDisabled = true
  } else if (allDone || current > 5) {
    btnLabel = '闭环已完成 ✓'
    btnDisabled = true
  } else if (waitingReview) {
    btnLabel = '等待人工审核（去 AIGC 审核）'
    btnDisabled = true
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">智能闭环</h1>
          {s?.run_id != null && (
            <p className="mt-1 text-sm text-muted-foreground">运行批次 #{s.run_id}</p>
          )}
        </div>
        <div className="flex gap-2">
          <Button
            size="sm"
            onClick={() => !btnDisabled && runStep.mutate(nextStep)}
            disabled={btnDisabled}
          >
            {runStep.isPending ? <Loader2 data-slot="icon" className="animate-spin" /> : <Play data-slot="icon" />}
            {btnLabel}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={rollback.isPending || !steps.some((x) => x.status === 'done')}
            onClick={() => rollback.mutate(Math.max(1, current - 1))}
          >
            <Undo2 data-slot="icon" />
            回滚上一步
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirmReset(true)}>
            <RotateCcw data-slot="icon" />
            重置
          </Button>
        </div>
      </div>

      <div className="space-y-3">
        {status.isPending
          ? Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-20 rounded-xl" />)
          : steps.length === 0
            ? (
              <Card>
                <CardContent className="py-12 text-center text-sm text-muted-foreground">
                  暂无闭环记录，点击「执行步骤 1」启动补货闭环
                </CardContent>
              </Card>
            )
            : (
              steps.map((step) => {
                const meta = STATUS_META[step.status] ?? STATUS_META.pending
                const active = step.step === current && step.status !== 'done'
                return (
                  <Card key={step.step} className={active ? 'border-primary/50 shadow-sm' : ''}>
                    <CardContent className="flex items-start gap-4 py-4">
                      <div
                        className={`flex size-10 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${
                          step.status === 'done'
                            ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                            : step.status === 'failed'
                              ? 'bg-destructive/15 text-destructive'
                              : active
                                ? 'bg-primary text-primary-foreground'
                                : 'bg-muted text-muted-foreground'
                        }`}
                      >
                        {step.step}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{step.name}</span>
                          <Badge variant={meta.badge}>{meta.text}</Badge>
                          {step.status === 'running' && (
                            <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
                          )}
                          <span className="text-xs text-muted-foreground">负责域：{step.owner}</span>
                        </div>
                        {step.detail && (
                          <p className="mt-1 break-all text-sm text-muted-foreground">{step.detail}</p>
                        )}
                      </div>
                      {step.status !== 'pending' && step.status !== 'running' && (
                        <Button
                          size="sm"
                          variant="ghost"
                          className="shrink-0"
                          disabled={rollback.isPending}
                          onClick={() => rollback.mutate(step.step)}
                        >
                          <Undo2 className="size-3.5" />
                          回滚到此步前
                        </Button>
                      )}
                    </CardContent>
                  </Card>
                )
              })
            )}
      </div>

      <AlertDialog open={confirmReset} onOpenChange={setConfirmReset}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>重置闭环状态？</AlertDialogTitle>
            <AlertDialogDescription>
              将清空当前闭环运行记录，所有步骤回到待执行状态。此操作不可撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => reset.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              确认重置
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
