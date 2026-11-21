import React, { useState } from 'react';
import { ShieldCheck, KeyRound, Lock } from 'lucide-react';
import { apiRequest, PrincipalInfo, setStoredToken } from '../api/client';
import { Button, Card } from './ui';

export function AuthGate({
  onAuthenticated,
}: {
  onAuthenticated: (token: string, principal: PrincipalInfo) => void;
}) {
  const [token, setToken] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const cleaned = token.trim();
    if (!cleaned) {
      setError('请输入 Bearer Token');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const principal = await apiRequest<PrincipalInfo>('/v2/auth/me', { token: cleaned });
      setStoredToken(cleaned);
      onAuthenticated(cleaned, principal);
    } catch (err: any) {
      setError(err?.detail || err?.message || '鉴权失败，请确认 Bearer Token 与权限 Scope');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 dark:bg-slate-950">
      <div className="w-full max-w-md space-y-6">
        <div className="text-center">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl border border-cyan-500/30 bg-cyan-500/10 text-cyan-500">
            <ShieldCheck className="h-6 w-6" />
          </div>
          <h1 className="mt-4 text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-100">
            AOC Control Center
          </h1>
          <p className="mt-1.5 text-xs text-slate-500 dark:text-slate-400">
            Artificial Organism Core V2.1 · 受 Bearer Token 与 RBAC Scope 保护的可观测控制中心
          </p>
        </div>

        <Card className="p-6">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-slate-700 dark:text-slate-300">
                API Bearer Token
              </label>
              <div className="relative mt-1.5">
                <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-400" />
                <input
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="输入 AOC_API__ADMIN_KEY 或带 read Scope 的凭证"
                  autoComplete="off"
                  className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-cyan-500 focus:outline-none dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                />
              </div>
            </div>

            {error && (
              <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-600 dark:text-rose-300">
                {error}
              </div>
            )}

            <Button type="submit" variant="primary" size="md" disabled={loading} className="w-full">
              {loading ? '正在验证权限…' : '验证并进入 Control Center'}
            </Button>
          </form>

          <div className="mt-5 flex items-start gap-2 rounded-lg bg-slate-100/80 p-3 text-[11px] leading-relaxed text-slate-500 dark:bg-slate-900 dark:text-slate-400">
            <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" />
            <span>
              Token 仅保存在当前浏览器标签页的 <code className="text-cyan-600 dark:text-cyan-400">sessionStorage</code> 中，不会写入代码仓库或后端日志。仅限 <code className="text-cyan-600 dark:text-cyan-400">127.0.0.1</code> 或 SSH Tunnel 访问。
            </span>
          </div>
        </Card>
      </div>
    </div>
  );
}
