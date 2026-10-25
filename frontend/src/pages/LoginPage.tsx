import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { api, sanitizeError } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const qc = useQueryClient()

  const login = useMutation({
    mutationFn: () =>
      api<{ access_token?: string; error?: string; must_change_password?: boolean }>(
        '/api/v1/auth/login',
        { method: 'POST', body: JSON.stringify({ username, password }) },
      ),
    onSuccess: (r) => {
      if (r.error) {
        toast.error(sanitizeError(r.error))
        return
      }
      if (r.must_change_password) {
        toast.warning('当前为初始密码，建议尽快修改')
      }
      qc.invalidateQueries({ queryKey: ['me'] })
    },
    onError: (e) => toast.error(sanitizeError(e instanceof Error ? e.message : '登录失败')),
  })

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-background via-muted/40 to-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <div className="mx-auto mb-2 flex size-12 items-center justify-center rounded-xl bg-primary text-primary-foreground">
            <RefreshCw className="size-6" />
          </div>
          <CardTitle className="text-xl">智能运营平台</CardTitle>
          <CardDescription>AIGC · RPA · ERP · FDE 一体化闭环</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault()
              login.mutate()
            }}
          >
            <div className="space-y-2">
              <Label htmlFor="username">用户名</Label>
              <Input
                id="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="admin"
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">密码</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
              />
            </div>
            <Button type="submit" className="w-full" disabled={login.isPending}>
              {login.isPending ? '登录中…' : '登 录'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
