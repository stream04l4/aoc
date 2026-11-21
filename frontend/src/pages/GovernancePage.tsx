import React, { useEffect, useState } from 'react';
import { Shield, AlertTriangle, Wrench, Check, X } from 'lucide-react';
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from 'recharts';
import {
  apiRequest,
  formatNumber,
  formatTime,
  hasScope,
  PrincipalInfo,
  shortId,
} from '../api/client';
import {
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorBanner,
  LoadingPanel,
  StatCard,
  StatusBadge,
} from '../components/ui';

export function GovernancePage({
  windowHours,
  refreshTick,
  principal,
  onOpenTask,
}: {
  windowHours: number;
  refreshTick: number;
  principal: PrincipalInfo;
  onOpenTask: (taskId: string) => void;
}) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = () => {
    apiRequest(`/v2/control-center/governance?hours=${windowHours}`)
      .then((res) => {
        setData(res);
        setErr(null);
      })
      .catch((e) => setErr(e?.detail || e?.message || '加载治理与预算数据失败'));
  };

  useEffect(() => {
    load();
  }, [windowHours, refreshTick]);

  const canApprove = hasScope(principal, 'approve');

  const handleApproval = async (approvalId: string, action: 'approve' | 'reject') => {
    setBusyId(approvalId);
    setActionMsg(null);
    try {
      await apiRequest(`/v2/approvals/${approvalId}/${action}`, {
        method: 'POST',
        body: {
          approver: principal.name,
          reason: `Control Center ${action} by ${principal.name}`,
        },
      });
      setActionMsg(`审批单 ${shortId(approvalId)} 已执行 ${action}`);
      load();
    } catch (e: any) {
      setActionMsg(`审批操作失败：${e?.detail || e?.message}`);
    } finally {
      setBusyId(null);
    }
  };

  if (!data && !err) return <LoadingPanel />;
  if (err && !data) return <ErrorBanner error={err} onRetry={load} />;

  const eff = data.effective_policy || {};
  const ta = data.token_analytics || {};
  const b = data.budget || {};

  return (
    <div className="space-y-5">
      {/* Effective Security Risk Advisories */}
      {(data.risk_advisories || []).length > 0 && (
        <div className="space-y-2">
          {data.risk_advisories.map((adv: any) => (
            <div
              key={adv.code}
              className={`flex items-start gap-3 rounded-xl border px-4 py-3 text-xs ${
                adv.severity === 'warning'
                  ? 'border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-200'
                  : 'border-cyan-500/30 bg-cyan-500/10 text-cyan-800 dark:text-cyan-200'
              }`}
            >
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <div className="font-semibold">{adv.title}</div>
                <p className="mt-0.5 opacity-90">{adv.detail}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Top Governance & Budget KPIs */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="生效自动审批等级"
          value={eff.auto_approve_max_level || '—'}
          subtext={`双语语义风控：${eff.semantic_policy_enabled ? '已开启' : '未开启'} · L3 硬拒绝`}
          tone={eff.auto_approve_max_level === 'L2' ? 'amber' : 'emerald'}
          icon={<Shield className="h-4 w-4" />}
        />
        <StatCard
          label={`窗口期 Token 总耗 (${windowHours === 0 ? '全部' : `${windowHours}h`})`}
          value={formatNumber(ta.total_tokens || 0)}
          subtext={`调用 ${formatNumber(ta.total_calls)} 次 · 平均延迟 ${ta.avg_latency_seconds || 0}s · 错误率 ${((ta.error_rate || 0) * 100).toFixed(1)}%`}
          tone="cyan"
        />
        <StatCard
          label="今日任务配额使用"
          value={`${b.tasks_used_today ?? 0} / ${b.tasks_limit_per_day ?? '—'}`}
          subtext={`配额状态：${b.quota_status || '—'} · 并发 ${b.running_tasks ?? 0}/${b.max_concurrent_tasks ?? 1}`}
        />
        <StatCard
          label="今日 Token 预算使用"
          value={formatNumber(b.tokens_used_today || 0)}
          subtext={`日限额：${formatNumber(b.tokens_limit_per_day || 0)}`}
        />
      </div>

      {/* V3.0 M1 Kernel Guard & Cognitive Efficiency Strip */}
      {data.kernel_v3_metrics && (
        <Card className="p-4">
          <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
            <div className="text-xs font-semibold text-slate-900 dark:text-slate-100">
              V3.0 内核守门与认知效率（ActionPreflight · GoalDedup · Verified IG）
            </div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-slate-500">
              <span>Preflight: {data.kernel_v3_metrics.plan_preflight_enabled ? 'ON' : 'OFF'}</span>
              <span>·</span>
              <span>Dedup: {data.kernel_v3_metrics.goal_dedup_enabled ? 'ON' : 'OFF'}</span>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-lg bg-slate-100/70 p-2.5 dark:bg-slate-900/70">
              <div className="text-[11px] text-slate-500">Preflight 前置拦截</div>
              <div className="mt-1 text-base font-semibold font-mono text-amber-600 dark:text-amber-400">
                {data.kernel_v3_metrics.preflight_rejected_count}
              </div>
            </div>
            <div className="rounded-lg bg-slate-100/70 p-2.5 dark:bg-slate-900/70">
              <div className="text-[11px] text-slate-500">重复目标拦截</div>
              <div className="mt-1 text-base font-semibold font-mono text-cyan-600 dark:text-cyan-400">
                {data.kernel_v3_metrics.goal_dedup_skipped_count}
              </div>
            </div>
            <div className="rounded-lg bg-slate-100/70 p-2.5 dark:bg-slate-900/70">
              <div className="text-[11px] text-slate-500">冷却中目标</div>
              <div className="mt-1 text-base font-semibold font-mono">
                {data.kernel_v3_metrics.goals_in_cooldown}
              </div>
            </div>
            <div className="rounded-lg bg-slate-100/70 p-2.5 dark:bg-slate-900/70">
              <div className="text-[11px] text-slate-500">Verified IG / 1M Tok</div>
              <div className="mt-1 text-base font-semibold font-mono text-emerald-600 dark:text-emerald-400">
                {Number(data.kernel_v3_metrics.verified_ig_per_1m_tokens || 0).toFixed(3)}
              </div>
            </div>
          </div>
          {Object.keys(data.kernel_v3_metrics.preflight_reason_breakdown || {}).length > 0 && (
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {Object.entries(data.kernel_v3_metrics.preflight_reason_breakdown).map(([code, cnt]) => (
                <span
                  key={code}
                  className="rounded bg-amber-500/15 px-2 py-0.5 text-[10px] font-mono text-amber-700 dark:text-amber-300"
                >
                  {code}: {String(cnt)}
                </span>
              ))}
            </div>
          )}
        </Card>
      )}

      {/* Token Breakdown Chart + Top Token-Heavy Tasks */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Brain vs Codex Token 消耗分布"
            subtitle={`统计口径：resource_usage(category='llm') · 窗口 ${windowHours === 0 ? '全部' : `近 ${windowHours}h`}`}
          />
          <div className="h-72 p-4">
            {(ta.by_source || []).length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={ta.by_source}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                  <XAxis dataKey="operation" tick={{ fontSize: 10, fill: '#94a3b8' }} />
                  <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#0f172a',
                      border: '1px solid #334155',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  />
                  <Bar dataKey="tokens" name="Tokens" fill="#06b6d4" radius={[6, 6, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState title="该时间窗内无 LLM 调用记录" />
            )}
          </div>
        </Card>

        <Card>
          <CardHeader title="高 Token 消耗任务 Top 10" subtitle="定位重复任务与单次高消耗 Codex 调用" />
          <div className="divide-y divide-slate-200/70 max-h-72 overflow-y-auto dark:divide-slate-800/80">
            {(ta.top_tasks || []).length === 0 ? (
              <EmptyState title="暂无任务级 Token 记录" />
            ) : (
              ta.top_tasks.map((t: any) => (
                <button
                  key={t.task_id}
                  onClick={() => onOpenTask(t.task_id)}
                  className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-slate-50 dark:hover:bg-slate-800/40 cursor-pointer"
                >
                  <div className="min-w-0">
                    <div className="text-xs truncate">{t.objective}</div>
                    <div className="text-[10px] font-mono text-slate-400">
                      {shortId(t.task_id)} · {t.llm_calls} 次调用
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className="font-mono text-xs font-semibold text-cyan-600 dark:text-cyan-400 tabular-nums">
                      {formatNumber(t.tokens)}
                    </span>
                    <StatusBadge value={t.status} />
                  </div>
                </button>
              ))
            )}
          </div>
        </Card>
      </div>

      {/* Approvals Table (Scope-Gated) */}
      <Card>
        <CardHeader
          title="审批单记录（approval_records）"
          subtitle={canApprove ? '当前凭证含 approve Scope，可对 PENDING 审批单执行受控审批' : '当前为只读凭证，无 approve Scope'}
        />
        {actionMsg && <div className="px-5 pt-3 text-xs text-cyan-600 dark:text-cyan-400">{actionMsg}</div>}
        {(data.approvals || []).length === 0 ? (
          <EmptyState title="暂无审批记录" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-200/70 text-[11px] text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2.5">Approval ID</th>
                  <th className="px-4 py-2.5">Task ID</th>
                  <th className="px-4 py-2.5">等级</th>
                  <th className="px-4 py-2.5">状态</th>
                  <th className="px-4 py-2.5">审批人 / 渠道</th>
                  <th className="px-4 py-2.5">创建时间</th>
                  <th className="px-4 py-2.5">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
                {data.approvals.map((a: any) => (
                  <tr key={a.approval_id}>
                    <td className="px-4 py-2.5 font-mono">{shortId(a.approval_id)}</td>
                    <td className="px-4 py-2.5 font-mono">
                      <button onClick={() => onOpenTask(a.task_id)} className="text-cyan-500 hover:underline cursor-pointer">
                        {shortId(a.task_id)}
                      </button>
                    </td>
                    <td className="px-4 py-2.5"><StatusBadge value={a.permission_level} /></td>
                    <td className="px-4 py-2.5"><StatusBadge value={a.status} /></td>
                    <td className="px-4 py-2.5 font-mono text-slate-500">{a.approver || '—'} / {a.channel || '—'}</td>
                    <td className="px-4 py-2.5 font-mono text-[11px] text-slate-500">{formatTime(a.created_at)}</td>
                    <td className="px-4 py-2.5">
                      {a.status === 'PENDING' && canApprove ? (
                        <div className="flex gap-1.5">
                          <Button
                            size="xs"
                            variant="primary"
                            disabled={busyId === a.approval_id}
                            onClick={() => handleApproval(a.approval_id, 'approve')}
                          >
                            <Check className="h-3 w-3" /> 批准
                          </Button>
                          <Button
                            size="xs"
                            variant="danger"
                            disabled={busyId === a.approval_id}
                            onClick={() => handleApproval(a.approval_id, 'reject')}
                          >
                            <X className="h-3 w-3" /> 拒绝
                          </Button>
                        </div>
                      ) : (
                        <span className="text-slate-400">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* MCP Tool Permissions + Recent Policy Evaluations */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="MCP ToolProvider 权限清单" subtitle="逐工具独立 L0–L3 权限与安全约束，未登记默认 L3 拒绝" icon={<Wrench className="h-4 w-4" />} />
          <div className="divide-y divide-slate-200/70 dark:divide-slate-800/80">
            {(data.mcp_tools || []).map((t: any) => (
              <div key={t.tool_name} className="p-4 space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold font-mono">{t.tool_name}</span>
                  <div className="flex gap-1.5">
                    <StatusBadge value={t.permission_level} />
                    <span className="font-mono text-[11px] text-slate-400">{t.timeout_seconds}s</span>
                  </div>
                </div>
                <p className="text-xs text-slate-500">{t.description}</p>
                <div className="text-[11px] font-mono text-slate-400">Server: {t.server_name}</div>
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <CardHeader title="最近策略审计判定（policy_evaluations）" subtitle="记录请求等级、确定性等级、语义等级与拒绝理由" />
          <div className="divide-y divide-slate-200/70 max-h-80 overflow-y-auto dark:divide-slate-800/80">
            {(data.policy_evaluations || []).slice(0, 20).map((pe: any) => (
              <div key={pe.id} className="px-4 py-2.5 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-mono">
                    {pe.subject_type} · {pe.requested_level} → <strong>{pe.effective_level}</strong>
                    {pe.semantic_level ? ` (sem:${pe.semantic_level})` : ''}
                  </span>
                  <StatusBadge value={pe.verdict} />
                </div>
                <div className="text-[11px] text-slate-500">{pe.reason}</div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
